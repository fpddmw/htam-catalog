"""Trusted subscription adapter. Candidate output is data; no API or auth handling.

Construction must be supplied by reviewed host code, never by a request-file
module name. Actual authorization is checked by ExperimentLab/B02, not labels.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import subprocess
import queue
import threading
import time
import uuid

from ..common import digest, require, HarnessError
from .contracts import tree
from .isolation.launcher import _snapshot
from .journal import hashed, identifier
from .native_artifacts import freeze_output_contract, validate_output, _verified_contract, _names, _config_fail

PROTOCOL = 'htam.native-subscription/v1'
ARTIFACT_PROTOCOL = 'htam.native-subscription/v2'
FIXED_NATIVE_PROCESS_PATH = r'C:\Windows\System32\WindowsPowerShell\v1.0;C:\Windows\System32;C:\Windows'


@dataclass(frozen=True)
class NativeHost:
    node_command: str
    codex_command: str
    windows_workspace: str
    authorization_scope: str
    cli_sha256: str
    mcp_names: tuple = ()
    distribution: str = 'Ubuntu-24.04'
    scope_cap: int = 8
    code_mode_host_sha256: str = ''


def windows_path(path, distribution='Ubuntu-24.04'):
    value = str(path)
    if os.name == 'nt':
        return value
    require(distribution == 'Ubuntu-24.04' and value.startswith('/'), 'Explicit supported WSL namespace required')
    return '\\\\wsl$\\' + distribution + value.replace('/', '\\')


class NativeSubscriptionExecutor:
    def __init__(self, host: NativeHost):
        require(type(host) is NativeHost and type(host.scope_cap) is int and 1 <= host.scope_cap <= 2147483647, 'Reviewed native host and bounded scope required')
        identifier(host.authorization_scope)
        require(host.windows_workspace.startswith(('C:\\', 'D:\\')) and '\n' not in host.windows_workspace,
            'Exact local Windows workspace required')
        require(len(host.cli_sha256) == 64 and all(c in '0123456789abcdef' for c in host.cli_sha256), 'Exact CLI binary hash required')
        require(len(host.code_mode_host_sha256) == 64 and all(c in '0123456789abcdef' for c in host.code_mode_host_sha256),
            'Exact packaged Code Mode host hash required')
        require(isinstance(host.mcp_names, tuple) and all(isinstance(v, str) and v.replace('_', '').replace('-', '').isalnum() for v in host.mcp_names), 'Only exact publicly discovered MCP keys accepted')
        self.host = host
        self.code_mode_host_command = str(PureWindowsPath(host.codex_command).with_name('codex-code-mode-host.exe'))
        self.runner = Path(__file__).with_name('native_runner.mjs')
        self._owned_roots = set()

    def identity(self):
        base = Path(__file__).parent
        adapter = base.parent / 'adapters'
        files = sorted(base.glob('*.py')) + [self.runner] + [adapter / name for name in
            ('profiled.mjs', 'transport.mjs', 'codex.mjs', 'events.mjs', 'timeline.mjs')]
        require(all(p.is_file() for p in files), 'Reviewed native implementation is incomplete', 'EXECUTOR_UNVERIFIED')
        return {'executor': 'codex-subscription-profiled/v1', 'network': False,
            'candidate_network': False, 'provider_transport_network': True, 'provider_transport': 'existing_codex_subscription',
            'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh', 'cli_version': '0.163.0-alpha.10.2', 'cli_sha256': self.host.cli_sha256,
            'code_mode_host_binary': {'command': self.code_mode_host_command, 'sha256': self.host.code_mode_host_sha256},
            'process_environment_policy': {'PATH': FIXED_NATIVE_PROCESS_PATH, 'caller_overrides': False,
                'scope': 'dedicated_app_server_and_version_processes'},
            'budget_unit': 'native_turn_attempt', 'maximum_budget_units_per_run': 1, 'provider_model_calls_observable': False,
            'maximum_wall_overhead_seconds': 60,
            'native_authorization_scope': self.host.authorization_scope, 'native_scope_cap': self.host.scope_cap,
            'host_configuration': {'node_command': self.host.node_command, 'codex_command': self.host.codex_command,
                'windows_workspace': self.host.windows_workspace, 'distribution': self.host.distribution},
            'profile': 'htam_rsi_eval', 'workspace_policy': {':root': 'deny', ':minimal': 'read', ':workspace_roots': {'.': 'read', 'output': 'write'}},
            'mcp_keys_disabled': list(self.host.mcp_names),
            'files': {str(p.relative_to(base.parent)): digest(p) for p in files},
            'coverage': 'Reviewed known surfaces and same-runtime concrete MXC canaries; full unexposed host tool inventory remains a trust boundary'}

    def _request(self, candidate, inputs, directory, *, entrypoint, args, limits, execution_context):
        context = deepcopy(execution_context)
        require(isinstance(context, dict) and context.get('budget_unit') == 'native_turn_attempt' and
            context.get('native_authorization_scope') == self.host.authorization_scope and
            type(context.get('intent_observation')) is int and context['intent_observation'] > 0 and
            context.get('executor_identity') == self.identity(), 'Native execution requires the exact committed host reservation', 'EXECUTOR_UNVERIFIED')
        require(len(args) == 1 and args[0] == context['case'], 'Exact reserved case required')
        require(isinstance(entrypoint, str) and entrypoint in context['candidate_manifest'], 'Frozen prompt entrypoint required')
        require(tree(candidate, limits) == context['candidate_manifest'] and tree(inputs, limits) == context['input_manifest'], 'Frozen native source changed', 'CONTENT_CHANGED')
        for manifest in (context['candidate_manifest'], context['input_manifest']):
            _names(list(manifest), _config_fail, max_files=limits.source_files)
        output = context.get('output_contract')
        invocation = context.get('invocation', {})
        if invocation:
            output = freeze_output_contract(output, generation=True,
                baseline_manifest=output['baseline_manifest'], editable=output['editable'])
            require(output == invocation['output_contract'], 'Generation output differs from frozen invocation')
        else:
            output = freeze_output_contract(output)
        context['output_contract'] = output
        directory = Path(directory).absolute()
        require(not any(p.is_symlink() for p in [directory, *directory.parents]), 'Native run path symlink rejected', 'PATH_ESCAPE')
        directory.mkdir(parents=True, exist_ok=True)
        run = directory / str(uuid.uuid4()); run.mkdir()
        self._owned_roots.add(str(run))
        (run / 'frozen').mkdir()
        require(_snapshot(candidate, run / 'frozen/candidate', limits) == context['candidate_manifest'] and
            _snapshot(inputs, run / 'frozen/input', limits) == context['input_manifest'], 'Source changed during native snapshot', 'CONTENT_CHANGED')
        # The input grammar is intentionally bounded: UTF-8 text/JSON only.
        for root in ('candidate', 'input'):
            for path in (run / 'frozen' / root).rglob('*'):
                if path.is_file():
                    require(path.stat().st_size <= 32768, 'Native input file exceeds construction bound')
                    path.read_text(encoding='utf-8')
        request = {'protocol': PROTOCOL, 'mode': 'execute', 'run_dir': windows_path(run, self.host.distribution),
            'view_parent': self.host.windows_workspace, 'codex_command': self.host.codex_command,
            'host_cli_sha256': self.host.cli_sha256, 'mcp_names': list(self.host.mcp_names),
            'code_mode_host_command': self.code_mode_host_command, 'code_mode_host_sha256': self.host.code_mode_host_sha256,
            'host_process_path_policy': FIXED_NATIVE_PROCESS_PATH,
            'context': context, 'entrypoint': entrypoint, 'case_id': args[0], 'wall_ms': min(120000, max(1000, int(limits.wall_seconds * 1000))),
            'output_limit': 32768, 'model': 'gpt-6.1-sol', 'effort': 'xhigh',
            'source_manifest': {'candidate': context['candidate_manifest'], 'input': context['input_manifest']},
            'output_contract': output, 'schema': output['schema']}
        # Generic structure only. No E/answer/schema feedback is available here.
        (run / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return run, request

    def _invoke_runner(self, run, request, *, recover=False, guard=None):
        command = [self.host.node_command, windows_path(self.runner.resolve(), self.host.distribution),
            '--request-file', windows_path(run / 'request.json', self.host.distribution)]
        if recover:
            command.append('--recover-only')
        # No shell, no credentials in argv/stdin, bounded host capture. The
        # runner creates its native child with an explicit environment allowlist.
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frames = queue.Queue(maxsize=32)
        def read_frames():
            try:
                while True:
                    line = child.stdout.readline(32769)
                    frames.put(line, timeout=1)
                    if not line:
                        break
            except (OSError, queue.Full):
                pass
        reader = threading.Thread(target=read_frames, daemon=True); reader.start()
        deadline = time.monotonic() + request['wall_ms'] / 1000 + 45; total = 0
        try:
            while True:
                require(time.monotonic() < deadline, 'Native runner deadline exceeded; terminal remains unknown', 'RUN_UNKNOWN')
                try:
                    line = frames.get(timeout=min(1, max(.01, deadline - time.monotonic())))
                except queue.Empty:
                    if child.poll() is not None:
                        break
                    continue
                if not line:
                    break
                total += len(line)
                require(total <= 131072 and len(line) <= 32768, 'Native control channel exceeded bound', 'RUN_UNKNOWN')
                frame = json.loads(line)
                if frame.get('kind') == 'guard_request':
                    require(not recover and callable(guard), 'Native send requires the current trusted parent guard', 'GUARD_DENIED')
                    try:
                        response = guard(frame['action'])
                    except HarnessError as error:
                        response = {'allowed': False, 'error': error.code}
                    child.stdin.write((json.dumps({'kind': 'guard_response', 'nonce': frame['nonce'], 'result': response}) + '\n').encode()); child.stdin.flush()
                else:
                    require(frame.get('kind') == 'runner_result', 'Unknown native control frame', 'RUN_UNKNOWN')
            child.wait(timeout=max(.01, deadline - time.monotonic()))
            require(child.returncode == 0, 'Native runner did not produce a verified terminal receipt; keep unknown', 'RUN_UNKNOWN')
        except (ValueError, subprocess.TimeoutExpired) as error:
            raise HarnessError('Native control channel failed; keep reservation unknown, no resend', 'RUN_UNKNOWN') from error
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close(); reader.join(timeout=2)

    def execute(self, candidate, inputs, directory, *, entrypoint, args, limits, execution_context, guard):
        require(callable(guard), 'Current trusted parent guard required before starting native runtime', 'GUARD_DENIED')
        run, request = self._request(candidate, inputs, directory, entrypoint=entrypoint, args=args, limits=limits, execution_context=execution_context)
        self._invoke_runner(run, request, guard=guard)
        if request.get('output_contract'):
            self._materialize_terminal(run, request)
        receipt = json.loads((run / 'run.json').read_text())
        # Local path is needed by the existing trusted E. The native evidence
        # retains the exact Windows run path independently.
        receipt['run_dir'] = str(run)
        return receipt

    def _terminal_receipt(self, run, request):
        """Pure readback: terminal certainty is separate from model data validity."""
        terminal_file = run / 'terminal.json'; facts_file = run / 'facts.jsonl'
        require(not any(path.is_symlink() for path in (run / 'request.json', terminal_file, facts_file)),
            'Protected evidence link rejected', 'PATH_ESCAPE')
        require(terminal_file.stat().st_size <= 1024 * 1024 and facts_file.stat().st_size <= 2 * 1024 * 1024,
            'Protected native evidence exceeded bounds', 'RUN_UNKNOWN')
        terminal = json.loads(terminal_file.read_text(encoding='utf-8'))
        facts = [json.loads(line) for line in facts_file.read_text(encoding='utf-8').splitlines()]
        require(terminal['protocol'] == 'htam.native-terminal/v1' and terminal['run_dir'] == request['run_dir'] and
            terminal['request_file_sha256'] == digest(run / 'request.json') and terminal['facts_sha256'] == digest(facts_file) and
            terminal['executor_identity'] == self.identity() == request['context']['executor_identity'] and
            terminal['source_manifest'] == request['source_manifest'], 'Protected terminal identity changed', 'RUN_UNKNOWN')
        runtimes = {f.get('runtimeId') for f in facts if isinstance(f.get('runtimeId'), str) and f['runtimeId']}
        require(len(runtimes) == 1 and all(f.get('runtimeId') == next(iter(runtimes)) for f in facts),
            'Owned runtime identity missing/conflicting', 'RUN_UNKNOWN')
        runtime = next(iter(runtimes)); closures = [f for f in facts if f.get('kind') == 'runtime_closed']
        require(len(closures) == 1 and isinstance(closures[0].get('transport'), dict) and closures[0]['transport'].get('closed') is True,
            'Owned runtime closure absent', 'RUN_UNKNOWN')
        target = terminal['native']; thread, turn = target['thread_id'], target['turn_id']
        require(target.get('runtime_id') == runtime, 'Terminal belongs to a different runtime', 'RUN_UNKNOWN')
        sent = [f for f in facts if f.get('kind') == 'send_attempted' and f.get('method') == 'turn/start']
        bindings = [f for f in facts if f.get('kind') == 'f09_binding']
        if not sent:
            require(not bindings and thread is None and turn is None and target['terminal_status'] == 'not_started',
                'Unexpected turn identity without send', 'RUN_UNKNOWN')
        else:
            require(len(sent) == len(bindings) == 1 and bindings[0]['threadId'] == thread and bindings[0]['turnId'] == turn and
                sent[0]['threadId'] == thread and sent[0]['clientUserMessageId'] == bindings[0]['clientUserMessageId'] == target['client_user_message_id'],
                'Original send/binding differs', 'RUN_UNKNOWN')
            statuses = {f.get('nativeStatus') for f in facts if f.get('kind') == 'turn_terminal' and f.get('threadId') == thread and f.get('turnId') == turn}
            require(len(statuses) == 1 and target['terminal_status'] in statuses and target['terminal_status'] in ('completed', 'failed', 'interrupted'),
                'Original terminal absent/conflicting', 'RUN_UNKNOWN')
        policies = [f for f in facts if f.get('kind') == 'f09_policy_probe']
        passed = len(policies) == 1 and policies[0].get('passed') is True
        blocked = any(f.get('kind') in ('f09_boundary_failure', 'f09_runner_error') for f in facts) or not passed
        require(target['policy_probe_passed'] is passed and bool(terminal.get('blocking_failure')) is blocked and
            target['model'] == request['model'] == 'gpt-6.1-sol' and target['effort'] == request['effort'] == 'xhigh' and
            target['permissionProfile'] == 'htam_rsi_eval' and target['code_mode_host_sha256'] == self.host.code_mode_host_sha256 and
            target['process_path_policy'] == FIXED_NATIVE_PROCESS_PATH, 'Actual settings/boundary receipt differs', 'RUN_UNKNOWN')
        require(target['activePermissionProfile'] == (bindings[0].get('activePermissionProfile') if bindings else None) and
            target['view_root'] == (policies[0].get('view') if policies else None), 'Profile/view ownership differs', 'RUN_UNKNOWN')
        usage = [f.get('tokenUsage') for f in facts if f.get('kind') == 'token_usage' and f.get('threadId') == thread]
        require(terminal['cost'] == {'native_turn_attempts': 1, 'model_calls': None, 'tokens': usage[-1] if usage else None, 'money': None},
            'Public native usage receipt differs', 'RUN_UNKNOWN')
        raw = [f for f in facts if f.get('kind') == 'f09_raw_final' and f.get('threadId') == thread and f.get('turnId') == turn]
        if not raw:
            raw = [f for f in facts if f.get('kind') == 'item_completed' and f.get('itemType') == 'agentMessage' and f.get('threadId') == thread and f.get('turnId') == turn]
        messages = {}; conflict = False
        for fact in raw:
            record = {k: fact.get(k) for k in ('itemId', 'phase', 'text', 'textBytes', 'textSha256')}
            record['textTruncated'] = fact.get('textTruncated', False)
            old = messages.get(record['itemId'])
            if old and [old[k] for k in ('phase', 'text', 'textTruncated')] != [record[k] for k in ('phase', 'text', 'textTruncated')]:
                conflict = True
            messages[record['itemId']] = record
        records = list(messages.values()); selected = [f for f in records if f['phase'] == 'final_answer'] or [f for f in records if not f['phase']]
        error = ('CONFLICTING_FINAL' if conflict else 'NON_UNIQUE_FINAL' if len(selected) != 1 else
            'MALFORMED_FINAL' if not isinstance(selected[0]['text'], str) else 'TRUNCATED_FINAL' if selected[0]['textTruncated'] else None)
        final = None if error else selected[0]
        require(terminal['public_final_records'] == records and terminal['selected_public_final'] == final and terminal['final_error'] == error,
            'Public final capture differs from exact original facts', 'RUN_UNKNOWN')
        # Original v1 receipts are verified against their original wire contract,
        # while _request only admits newly frozen v2 contracts for sending.
        output = _verified_contract(request['output_contract']); generation = output['generation']
        base_files = None
        if generation:
            base = run / 'frozen/input/H0'; base_files = {}
            for name, expected in output['baseline_manifest'].items():
                path = base / name
                require(not any(p.is_symlink() for p in [path, *path.parents]), 'Generation base symlink rejected', 'PATH_ESCAPE')
                data = path.read_bytes()
                require({'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} == expected, 'Frozen generation base changed', 'CONTENT_CHANGED')
                base_files[name] = data
        if target['terminal_status'] == 'completed' and not blocked and not error:
            validated = validate_output(final['text'], output, base_files=base_files)
        else:
            validated = {'valid': False, 'error_code': 'NATIVE_BOUNDARY_UNVERIFIED' if blocked else error or 'NATIVE_' + target['terminal_status'].upper(),
                'files': {}, 'manifest': {}, 'raw_text_sha256': hashlib.sha256(final['text'].encode()).hexdigest() if final else None}
        prefix = 'generated/' if generation else ''
        files = {prefix + name: data for name, data in validated['files'].items()}
        artifacts = [{'path': prefix + name, **body} for name, body in sorted(validated['manifest'].items())]
        elapsed = terminal['elapsed_seconds']
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, 'Native elapsed time is unverified', 'RUN_UNKNOWN')
        receipt = {k: deepcopy(terminal[k]) for k in ('run_dir', 'executor_identity', 'source_manifest', 'request_file_sha256', 'facts_sha256', 'native', 'cost', 'elapsed_seconds', 'blocking_failure')}
        receipt.update({'protocol': ARTIFACT_PROTOCOL, 'terminal_sha256': digest(terminal_file), 'output_contract_sha256': hashed(output),
            'execution_succeeded': validated['valid'], 'artifacts': artifacts,
            'artifact_validation': {k: validated[k] for k in ('valid', 'error_code', 'raw_text_sha256')},
            'coverage': 'Exact owned terminal and bounded public final; model data validation is independent of OS/tool boundary verification'})
        return receipt, files

    def _materialize_terminal(self, run, request):
        receipt, files = self._terminal_receipt(run, request)
        for name, data in files.items():
            path = run / 'result/artifacts' / name
            require(not any(p.is_symlink() for p in [path, *path.parents]), 'Artifact symlink rejected', 'PATH_ESCAPE')
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                require(path.read_bytes() == data, 'Existing accepted artifact differs', 'CONTENT_CHANGED')
            else:
                with path.open('xb') as stream:
                    stream.write(data)
        output = run / 'run.json'
        if output.exists():
            require(json.loads(output.read_text(encoding='utf-8')) == receipt, 'Existing immutable receipt differs', 'CONTENT_CHANGED')
        else:
            with output.open('x', encoding='utf-8') as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
        return receipt

    def verify_receipt(self, receipt, intent, expected_files):
        try:
            root = Path(receipt['run_dir'])
            require(str(root) in self._owned_roots and root.is_absolute() and not any(p.is_symlink() for p in [root, *root.parents]), 'Receipt was not produced by this host invocation/recovery')
            request = json.loads((root / 'request.json').read_text())
            original = json.loads((root / 'run.json').read_text())
            require({**original, 'run_dir': str(root)} == receipt, 'Imported or altered native receipt rejected')
            require(request['context']['request_sha256'] == intent['request_sha256'] and
                request['context']['run_id'] == intent['run_id'] and
                request['context']['intent_observation'] == intent['intent_observation'], 'Native reservation mismatch')
            if receipt.get('protocol') == ARTIFACT_PROTOCOL:
                expected_output = intent.get('invocation', {}).get('output_contract', intent.get('output_contract'))
                require(request['output_contract'] == expected_output, 'Output contract differs from frozen intent')
            require(request['context']['executor_identity'] == self.identity() and receipt['executor_identity'] == self.identity(), 'Native implementation changed')
            require(request.get('code_mode_host_command') == self.code_mode_host_command and
                request.get('code_mode_host_sha256') == self.host.code_mode_host_sha256 and
                receipt['native'].get('code_mode_host_sha256') == self.host.code_mode_host_sha256,
                'Native packaged host identity changed')
            require(request.get('host_process_path_policy') == FIXED_NATIVE_PROCESS_PATH and
                receipt['native'].get('process_path_policy') == FIXED_NATIVE_PROCESS_PATH,
                'Native process PATH policy changed')
            require(receipt['protocol'] in (PROTOCOL, ARTIFACT_PROTOCOL) and receipt['source_manifest'] == {'candidate': expected_files, 'input': intent['input_manifest']}, 'Native source mismatch')
            require(receipt['request_file_sha256'] == digest(root / 'request.json') and receipt['facts_sha256'] == digest(root / 'facts.jsonl'), 'Original host evidence changed')
            if receipt['protocol'] == ARTIFACT_PROTOCOL:
                expected, files = self._terminal_receipt(root, request)
                require(expected == original, 'Terminal/artifact receipt differs from original protected facts')
                for name, data in files.items():
                    path = root / 'result/artifacts' / name
                    require(not any(p.is_symlink() for p in [path, *path.parents]) and path.read_bytes() == data,
                        'Native accepted artifact changed')
                actual = root / 'result/artifacts'
                require(not actual.exists() or {str(p.relative_to(actual)).replace('\\', '/') for p in actual.rglob('*') if p.is_file()} == set(files),
                    'Unexpected native accepted artifact')
                return True
            require(receipt['native']['model'] == 'gpt-6.1-sol' and receipt['native']['effort'] == 'xhigh' and
                receipt['native']['permissionProfile'] == 'htam_rsi_eval' and receipt['native']['policy_probe_passed'] is True,
                'Actual native settings/profile are unverified')
            facts = [json.loads(line) for line in (root / 'facts.jsonl').read_text().splitlines()]
            target = receipt['native']; turn = target['turn_id']; thread = target['thread_id']
            require(any(f['kind'] == 'send_attempted' and f.get('method') == 'turn/start' and
                f.get('threadId') == thread and f.get('clientUserMessageId') == target['client_user_message_id'] for f in facts), 'Original native send fact absent')
            require(any(f['kind'] == 'turn_terminal' and f.get('threadId') == thread and f.get('turnId') == turn and
                f.get('nativeStatus') == target['terminal_status'] for f in facts), 'Original matching terminal absent')
            require(target['terminal_status'] in ('completed', 'failed', 'interrupted') and
                receipt['execution_succeeded'] == (target['terminal_status'] == 'completed'), 'Conflicting native terminal')
            for artifact in receipt['artifacts']:
                require(artifact['path'] == 'result.json' and digest(root / 'result/artifacts/result.json') == artifact['sha256'], 'Native final artifact changed')
            require(not receipt['execution_succeeded'] or len(receipt['artifacts']) == 1, 'Completed native output missing')
            if receipt['execution_succeeded']:
                finals = [f for f in facts if f.get('kind') == 'item_completed' and f.get('itemType') == 'agentMessage' and
                    f.get('threadId') == thread and f.get('turnId') == turn and not f.get('textTruncated') and isinstance(f.get('text'), str)]
                # Duplicate notifications with the same original item/event are
                # harmless; two distinct final messages remain ambiguous.
                finals = {f.get('itemId'): f for f in finals}
                declared = {k: f for k, f in finals.items() if f.get('phase') == 'final_answer'}
                finals = declared or {k: f for k, f in finals.items() if not f.get('phase')}
                require(len(finals) == 1 and json.loads(next(iter(finals.values()))['text']) ==
                    json.loads((root / 'result/artifacts/result.json').read_text()), 'Artifact is not the exact original native final JSON')
            return True
        except (OSError, ValueError, TypeError, KeyError, HarnessError):
            return False

    def recover_receipt(self, intent, expected_files):
        root = Path(intent['absolute_run_root'])
        require(root.is_absolute() and root.name == intent['run_id'] and not any(p.is_symlink() for p in [root, *root.parents]), 'Invalid recorded native run root', 'PATH_ESCAPE')
        if not root.is_dir():
            return None
        attempts = list(root.glob('*/request.json'))
        require(len(attempts) == 1, 'No unique original native invocation; no resend', 'RUN_UNKNOWN')
        run = attempts[0].parent
        request = json.loads(attempts[0].read_text())
        require(request.get('context', {}).get('request_sha256') == intent['request_sha256'] and
            request['context'].get('intent_observation') == intent['intent_observation'] and
            request['context'].get('executor_identity') == self.identity(), 'Original native request identity changed', 'RUN_UNKNOWN')
        self._owned_roots.add(str(run))
        if not (run / 'run.json').is_file():
            if not (run / 'facts.jsonl').is_file():
                return None
            # The runner's recover-only branch reads original protected facts;
            # it cannot open an app-server, send a turn, or take over a runtime.
            try:
                if not (run / 'terminal.json').is_file():
                    self._invoke_runner(run, request, recover=True)
                if request.get('output_contract'):
                    self._materialize_terminal(run, request)
            except (HarnessError, OSError):
                return None
        receipt = json.loads((run / 'run.json').read_text()); receipt['run_dir'] = str(run)
        return receipt if self.verify_receipt(receipt, intent, expected_files) else None
