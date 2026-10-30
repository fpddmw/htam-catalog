from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from .common import HarnessError, atomic_text, digest, encoded, local_path, now, require, validation_scope
from .context import check_evidence, render_context, retrieval_scope
from .discovery import Discovery
from .host import fallback_hook, handle_hook
from .fulfillment import check_contract
from .state import State
from . import goals
from .assets import AssetRegistry
from . import portfolio
from . import migrations

EXPERIMENT_READONLY = {'prepare', 'get', 'summary', 'budget', 'final_assessment', 'resolve_method', 'meta_compare', 'prepare_evaluation_revision'}


def read_json(path):
    return json.loads(sys.stdin.read(8 * 1024 * 1024).lstrip('\ufeff') if str(path) == '-' else Path(path).read_text(encoding="utf-8-sig"))


def snapshot(state):
    data = {"at": now(), "works": state.works(), "bindings": [dict(r) for r in state.db.execute("SELECT * FROM bindings")]}
    atomic_text(state.directory / "state-snapshot.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def parser():
    class Arguments(argparse.ArgumentParser):
        def error(self, message):
            raise HarnessError(message, 'INVALID_ARGUMENTS')
    p = Arguments(description="Local work state, evidence discovery and scoped host integration")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--store", type=Path, help="Explicit isolated store for tests or recovery")
    p.add_argument('--read-only', action='store_true', help='Require observational commands and query-only business/index connections')
    p.add_argument('--api-version', type=int, choices=(1, 2), default=1, help='Default keeps existing JSON shapes; v2 opts into a response envelope')
    subs = p.add_subparsers(dest="command", required=True)
    def cmd(name, opts=()):
        c = subs.add_parser(name)
        for flag, kwargs in opts:
            c.add_argument(flag, **kwargs)
        return c
    work = ("--work", {"required": True})
    expected = ("--expected", {"required": True, "type": int})
    source = ("--source", {"required": True})
    file = ("--file", {"required": True})
    cmd("status", [("--work", {}), ("--full", {"action": "store_true"})])
    cmd('planning', [work, ('--limit', {'type': int, 'default': 12})])
    cmd('change-preview', [work, file, ('--task', {})])
    cmd("update", [work, expected, file, source, ("--input-id", {"type": int}), ('--input-expected', {'type': int}), ('--actor-file', {})])
    cmd("bind", [work, ("--session", {"required": True}), ("--role", {"default": "main", "choices": ["main", "executor", "observer"]}), source,
        ('--expected-binding', {'type': int})])
    cmd("input-ack", [work, expected, ("--id", {"type": int, "required": True}), source, ('--scope-expected', {'type': int})])
    cmd('input-query', [('--id', {'type': int}), ('--work', {}), ('--session', {}), ('--limit', {'type': int, 'default': 8})])
    cmd('input-link', [work, expected, ('--id', {'type': int, 'required': True}), ('--scope-expected', {'type': int, 'required': True}), file, source])
    cmd('input-assess', [('--id', {'type': int, 'required': True}), expected, file, source])
    cmd('input-effect', [work, ('--id', {'type': int, 'required': True}), expected, file, source])
    cmd('coordination-message', [('operation', {'choices': ['prepare', 'show', 'fact']}), file])
    cmd('submission', [('operation', {'choices':['authorize','prepare','check','observe','show','retract']}), file])
    cmd('version')
    dot = subs.add_parser('dot-package', help='Export/check the fixed Dot framework excerpt package without opening State')
    dot_ops = dot.add_subparsers(dest='dot_operation', required=True)
    dot_export = dot_ops.add_parser('export', help='Create a new immutable local package directory; no native delivery')
    dot_export.add_argument('--output', type=Path, required=True)
    dot_export.add_argument('--source-thread', required=True, help='Explicit source native thread identity')
    dot_export.add_argument('--authorization-reference', required=True, help='Exact prior authorization reference; metadata does not grant permission')
    dot_export.add_argument('--cloud-directory', required=True, help='Explicit approved absolute POSIX cloud directory; not probed or written')
    dot_check = dot_ops.add_parser('check', help='Read-only schema/content validation and optional current-source comparison')
    dot_check.add_argument('--package', type=Path, required=True)
    dot_check.add_argument('--check-sources', action='store_true', help='Compare selected sections and full files against --root; otherwise freshness is unknown')
    dot_check.add_argument('--manifest-sha256', help='Optional trusted external manifest SHA256 to match')
    cmd('migration-plan', [('--output', {'type': Path})])
    cmd('migration-apply', [file, ('--backup', {'type': Path, 'required': True})])
    cmd('migration-verify')
    cmd('recovery-export', [('--baseline', {'type': Path, 'required': True}), ('--output', {'type': Path, 'required': True})])
    cmd('recovery-restore', [('--baseline', {'type': Path, 'required': True}), ('--output', {'type': Path, 'required': True}), ('--delta', {})])
    cmd('runtime-owner', [work, ('--owner', {'required': True}), expected, source, ('--handoff-file', {})])
    cmd('runtime-owner-preview', [work, ('--owner', {'required': True}), expected])
    cmd('native-entry', [('operation', {'choices': ['prepare', 'fact', 'accept', 'check', 'return', 'receive', 'finish', 'review', 'release', 'resolve', 'show', 'recover', 'input']}), file])
    cmd('runtime-policy', [('--id', {'required': True}), expected, file, source])
    cmd('runtime-route', [('--task', {'required': True}), expected, file, source])
    cmd('runtime-prepare', [('--id', {'required': True}), ('--task', {'required': True}), expected,
        ('--owner', {'required': True}), ('--owner-expected', {'type': int, 'required': True}), file, source])
    cmd('runtime-guard', [file])
    cmd('runtime-fact', [file])
    cmd('runtime-action', [('--id', {'required': True})])
    cmd('runtime-overview', [('--work', {}), ('--limit', {'type': int, 'default': 30})])
    cmd('runtime-scope', [('--task', {'action': 'append', 'required': True}), ('--no-dependents', {'action': 'store_true'})])
    cmd('runtime-control', [('--id', {'required': True}), ('--kind', {'choices': ['pause', 'correct', 'resume'], 'required': True}), file, source])
    cmd('runtime-control-view', [('--id', {'required': True})])
    cmd('runtime-review-claim', [('--task', {'required': True}), ('--owner', {'required': True}), expected, ('--policy', {'default': 'workspace'})])
    cmd('runtime-review-assign', [('--task', {'required': True}), ('--owner', {'required': True}), expected, source])
    cmd('runtime-usage', [('--id', {'required': True}), ('--action', {'required': True}), file, source])
    cmd('runtime-release', [('--resource', {'required': True}), ('--action', {'required': True}), ('--evidence-event', {'required': True}), ('--file', {'type': Path}), source])
    cmd('runtime-resolve', [('--id', {'required': True}), expected, file, source])
    cmd('runtime-inputs', [('--host', {'required': True}), ('--thread', {'required': True}), ('--limit', {'type': int, 'default': 8}),
        ('--offset', {'type': int, 'default': 0}), ('--event', {})])
    cmd('runtime-input-resolve', [('--host', {'required': True}), ('--thread', {'required': True}), ('--epoch', {'required': True}), file, source])
    cmd('runtime-input-quarantine', [('--host', {'required': True}), ('--thread', {'required': True}), ('--epoch', {'required': True}), file, source])
    cmd("task", [work, expected, ("--id", {"required": True}), file, source])
    cmd('task-link', [("--task", {"required": True}), expected, file, source])
    cmd('decide', [work, expected, file, source, ('--actor-file', {})])
    cmd("review", [("--task", {"required": True}), ("--disposition", {"required": True}), file, source])
    cmd("reserve", [("--task", {"required": True}), ("--key", {"required": True}), ("--target", {"required": True}), source,
        ('--message-file', {'required': True}), ('--intent', {'default': 'new', 'choices': ['new', 'amend', 'replace']}), ('--relates-to', {})])
    cmd("receipt", [("--key", {"required": True}), ("--status", {"required": True}), file, source])
    cmd("dispatch", [("--key", {"required": True})])
    cmd("observe", [("--asset", {"required": True}), ("--kind", {"required": True}), file, source])
    cmd("judge", [work, expected, ("--id", {"required": True}), ("--status", {"required": True}), file, source])
    cmd("history", [work])
    cmd("improvement-review", [work, ("--event", {"required": True, "type": int}), file, source])
    cmd('improvement-assign', [work, ('--event', {'required': True, 'type': int}), expected, file, source])
    cmd('improvement-query', [work, ('--relation', {'choices': ['all', 'origin', 'maintenance'], 'default': 'all'}),
        ('--include-resolved', {'action': 'store_true'}), ('--event', {'type': int}),
        ('--limit', {'type': int, 'default': 8}), ('--offset', {'type': int, 'default': 0}),
        ('--history-limit', {'type': int, 'default': 8}), ('--history-offset', {'type': int, 'default': 0})])
    cmd("check", [work])
    cmd("completion", [("--work", {}), ("--task", {})])
    cmd("completion-set", [("--task", {"required": True}), expected, file, source, ('--change-file', {})])
    cmd("context", [("--work", {}), ("--session", {}), ("--query", {}), ('--task', {}),
        ('--context-file', {}),
        ('--full', {'action': 'store_true'}), ('--verify', {'action': 'store_true'}),
        ('--budget', {'type': int, 'default': 24000}), ('--limit', {'type': int, 'default': 8})])
    cmd("index", [("--config", {})])
    cmd("find", [("query", {}), ("--limit", {"type": int, "default": 10}), ("--cached", {"action": "store_true"}), ("--config", {})])
    retrieval = [('--context-file', {}), ('--work', {}), ('--task', {}),
        ('--limit', {'type': int, 'default': 8}), ('--budget', {'type': int, 'default': 16000})]
    cmd('retrieve', [('query', {'nargs': '?', 'default': ''}), *retrieval])
    cmd('entry-show', [('--id', {'required': True}), ('--max-members', {'type': int, 'default': 20}), ('--text-chars', {'type': int, 'default': 2400})])
    cmd('asset-search', [('--query', {'default': ''}), *[item for item in retrieval if item[0] != '--budget'], ('--current-only', {'action': 'store_true'})])
    cmd('asset-handoff', [('--id', {'required': True}), ('--version', {'required': True}), ('--purpose', {'required': True}), ('--verify', {'action': 'store_true'}), ('--context-file', {'type': Path}), ('--work', {}), ('--task', {})])
    cmd('experience-show', [('--id', {'required': True}), ('--history-limit', {'type': int, 'default': 8})])
    cmd('experience-select', [('--query', {'default': ''}), *retrieval])
    cmd('experiment', [('operation', {}), ('--request-file', {'required': True}), ('--lab', {'required': True})])
    for name in ('experience-propose', 'experience-relate', 'experience-retract-relation', 'experience-mark', 'experience-approve', 'experience-publish-method'):
        cmd(name, [('--id', {'required': True}), expected, file, source])
    cmd("backup", [("--output", {"required": True, "type": Path})])
    cmd('asset-put', [("--id", {"required": True}), expected, file, source])
    cmd('asset-revise', [("--id", {"required": True}), expected, file, source, ("--version", {})])
    cmd('asset-history', [("--id", {"required": True})])
    cmd('asset-show', [("--id", {"required": True}), ("--version", {})])
    cmd('assets', [("--work", {}), ("--query", {}), ('--verify', {'action': 'store_true'})])
    cmd('asset-use', [work, ("--requirement", {"required": True}), ("--id", {"required": True}), ("--version", {"required": True}), ("--purpose", {}), ("--file", {}), source])
    cmd('asset-impact', [("--id", {"required": True}), ("--version", {})])
    cmd('portfolio', [work, ("--format", {"choices": ['json', 'markdown'], "default": 'json'}), ('--verify', {'action': 'store_true'})])
    cmd("render")
    cmd("doctor")
    cmd("hook", [('--guard-timeout', {'type': float, 'default': 8.0})])
    cmd("hooks-config", [("--windows-distro", {}), ("--output", {"required": True, "type": Path})])
    return p


def render(state):
    lines = ["# 当前工作状态", "", "由管理状态生成；修改请使用管理入口。此页不是状态写入源。", ""]
    for w in state.works():
        lines.extend(["## " + w["title"], "", f"工作：`{w['id']}`；状态：**{w['status']}**；修订：{w['revision']}。", "", w["request"], "", "依据：" + w["request_source"], "", "下一步：" + w.get("next_action", "未指定"), ""])
        lineage = portfolio.ancestry(state, w)
        for parent in lineage['items']:
            lines.extend(['上层目标：`' + parent['id'] + '` ' + parent['title'] + '；对应要求：' + ', '.join(parent['contributes_to']),
                          '阶段范围：' + parent['scope'], ''])
        if w.get('parent') or portfolio.children(state, w['id']):
            lines.extend(['[总体／阶段资产进度视图](portfolios/' + w['id'] + '.md)', ''])
        overall = goals.overview(state, w, verify=False)
        if overall['migration_required']:
            lines.extend(['总体要求尚未迁移；原资料与历史继续有效，新增受管理续派前显式迁移。', ''])
        else:
            lines.extend(['总体判断（生成页不重跑专业回执；采用和结项以实时检查为准）：', ''])
            for requirement in overall['requirements']:
                lines.append('- `' + requirement['id'] + '`：' + requirement['state'] + '；' + requirement['description'])
                for remaining in requirement['remaining']:
                    lines.append('  - 剩余：' + remaining)
            decision = goals.decision_view(state, w)
            lines.extend(['', '整体决策：' + decision.get('action', '待协调') + '；' + decision.get('reason', ''),
                          '预期增量：' + decision.get('expected_gain', ''), ''])
    output = state.directory / "current.md"
    atomic_text(output, "\n".join(lines))
    portfolios = []
    for w in state.works():
        if w.get('parent') or portfolio.children(state, w['id']):
            data = portfolio.view(state, w['id'])
            dest = state.directory / 'portfolios' / (w['id'] + '.md')
            atomic_text(dest, portfolio.markdown(data))
            atomic_text(dest.with_suffix('.json'), json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            portfolios.append(str(dest))
    return {"path": str(output), "works": len(state.works()), "portfolios": portfolios}


def hooks_config(root, distro):
    # Generated per installation; the core has no hardcoded host or repository identity.
    import shlex
    native = "python3 " + shlex.quote(str(root / "scripts/harness.py")) + " hook"
    handler = {"type": "command", "command": native, "timeout": 10}
    if distro:
        import subprocess
        handler["commandWindows"] = subprocess.list2cmdline(["wsl.exe", "-d", distro, "--cd", str(root), "--exec", "python3", str(root / "scripts/harness.py"), "hook"])
    context_handler = {**handler, "additionalContextLimit": 7000}
    return {"description": "HTAM current-state recovery and managed-thread dispatch checks; review and trust exact hooks in the host.", "hooks": {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [context_handler]}],
        "UserPromptSubmit": [{"hooks": [context_handler]}],
        "PreToolUse": [{"matcher": ".*(send_message_to_thread|(?:cua_repl|node_repl).*js)$", "hooks": [handler]}],
        "PostToolUse": [{"matcher": ".*(send_message_to_thread|(?:cua_repl|node_repl).*js|exec_command|write_stdin|functions[._]exec)$", "hooks": [handler]}]}}


def execute(args, root, directory, state):
    c = args.command
    if c == 'coordination-message':
        from .host import prepare_coordination_message, coordination_message_show, coordination_message_fact
        request = read_json(args.file)
        require(isinstance(request, dict), 'Coordination request must be an object')
        if args.operation == 'prepare': return prepare_coordination_message(state, request)
        if args.operation == 'fact': return coordination_message_fact(state, request)
        return coordination_message_show(state, request.get('id'))
    if c == 'submission':
        from . import submission
        request = read_json(args.file)
        if args.operation == 'retract': return submission.retract(state,request)
        if args.operation == 'show': return [dict(r) | {'payload':json.loads(r['payload'])} for r in state.db.execute("SELECT seq,kind,payload FROM events WHERE work_id=? AND kind IN ('submission_scope_reviewed','submission_prepared','submission_denied','submission_scope_retracted') ORDER BY seq DESC LIMIT 12",(request['work'],))]
        if args.operation == 'observe': return {'events': submission.observe(state,request['work'],request['verification_reports'],request['source'],request.get('session'))}
        return submission.authorize(state, request) if args.operation == 'authorize' else submission.prepare(state, request) if args.operation == 'prepare' else {'evidence': submission.validate(state, request), 'ready': True}
    if c == 'native-entry':
        from .native_entry import NativeEntry
        return NativeEntry(state).call(args.operation, read_json(args.file))
    if c == 'experiment':
        from .experiments import ExperimentLab
        request = read_json(args.request_file)
        require(isinstance(request, dict), 'Experiment request must be an object')
        try:
            return ExperimentLab(state, args.lab).call(args.operation, request)
        except (TypeError, KeyError) as exc:
            raise HarnessError('Invalid experiment operation arguments: ' + str(exc), 'INVALID_ARGUMENTS') from exc
    if c == 'planning': return portfolio.planning(state, args.work, args.limit)
    if c == 'change-preview':
        from .coordination import change_preview
        return change_preview(state, args.work, read_json(args.file), task_id=args.task)
    if c in ('retrieve', 'entry-show', 'asset-search', 'asset-handoff') or c.startswith('experience-'):
        from .experiences import ExperienceRegistry
        index = Discovery(root, directory, state=state, readonly=True)
        try:
            registry = ExperienceRegistry(state, can_read=index._permitted)
            if c == 'entry-show': return index.inspect_entry(args.id, max_members=args.max_members, text_chars=args.text_chars)
            if c == 'asset-handoff':
                scope = retrieval_scope(state, read_json(args.context_file) if args.context_file else None, work=args.work, task=args.task)
                return AssetRegistry(state).handoff(args.id, args.version, args.purpose, verify=args.verify, can_read=index._permitted, context=scope)
            if c in ('retrieve', 'asset-search', 'experience-select'):
                context = retrieval_scope(state, read_json(args.context_file) if args.context_file else {}, work=args.work, task=args.task)
                if c == 'retrieve': return index.retrieve(args.query, context, limit=args.limit, budget=args.budget)
                if c == 'asset-search': return AssetRegistry(state).search(args.query, context, limit=args.limit, can_read=index._permitted, include_history=not args.current_only)
                return registry.select(context, args.query, limit=args.limit, budget=args.budget)
            if c == 'experience-show':
                require(0 <= args.history_limit <= 100, 'History limit must be 0..100')
                result = registry.get(args.id)
                history = result.get('history', [])
                result['history'] = history[-args.history_limit:] if args.history_limit else []
                result['omitted_history'] = len(history) - len(result['history'])
                return result
            body = read_json(args.file)
            require(isinstance(body, dict), 'Experience operation body must be an object')
            if c == 'experience-propose': return registry.propose(args.id, args.expected, body, args.source)
            if c == 'experience-approve': return registry.approve(args.id, args.expected, body, args.source)
            if c == 'experience-publish-method': return registry.publish_method(args.id, args.expected, body, args.source)
            if c == 'experience-relate':
                require(all(key in body for key in ('relation', 'targets', 'reason')), 'Relation needs relation/targets/reason')
                return registry.relate(args.id, args.expected, body['relation'], body['targets'], body['reason'], args.source)
            if c == 'experience-retract-relation':
                require(all(key in body for key in ('relation_observation_id', 'reason')), 'Retraction needs relation_observation_id/reason')
                return registry.retract_relation(args.id, args.expected, body['relation_observation_id'], body['reason'], args.source)
            require(all(key in body for key in ('status', 'reason')), 'Lifecycle needs status/reason')
            return registry.mark(args.id, args.expected, body['status'], body['reason'], args.source)
        finally:
            index.close()
    if c.startswith('runtime-'):
        from .runtime import Runtime
        runtime = Runtime(state)
        if c in ('runtime-inputs', 'runtime-input-resolve', 'runtime-input-quarantine'):
            from .native_inputs import NativeInputs
            native = NativeInputs(state)
            if c == 'runtime-inputs': return native.view(args.host, args.thread, limit=args.limit, offset=args.offset, event_id=args.event)
            return (native.quarantine if c == 'runtime-input-quarantine' else native.resolve)(args.host, args.thread, args.epoch, read_json(args.file), args.source)
        if c == 'runtime-owner': return runtime.owner(args.work, args.owner, args.expected, args.source, read_json(args.handoff_file) if args.handoff_file else None)
        if c == 'runtime-owner-preview':
            statement = runtime.owner_statement(args.work, args.owner, args.expected)
            return {'statement': statement, 'mutation_sha256': goals.fingerprint(statement)}
        if c == 'runtime-policy': return runtime.policy(args.id, args.expected, read_json(args.file), args.source)
        if c == 'runtime-route': return runtime.map(args.task, args.expected, read_json(args.file), args.source)
        if c == 'runtime-prepare': return runtime.prepare(args.id, args.task, args.expected, args.owner, args.owner_expected, read_json(args.file), args.source)
        if c == 'runtime-guard': return runtime.guard(read_json(args.file))
        if c == 'runtime-fact':
            receipt = runtime.ingest(read_json(args.file))
            failure = receipt.get('error') or {}
            require(receipt['persisted'], failure.get('message', 'Native fact rejected'), failure.get('code', 'IDENTITY_MISMATCH'))
            return receipt
        if c == 'runtime-action': return runtime.action(args.id)
        if c == 'runtime-overview': return runtime.overview(args.work, args.limit)
        if c == 'runtime-scope': return runtime.scope_plan(args.task, not args.no_dependents)
        if c == 'runtime-control': return runtime.control(args.id, args.kind, read_json(args.file), args.source)
        if c == 'runtime-control-view': return runtime.control_view(args.id)
        if c == 'runtime-review-claim': return runtime.review_claim(args.task, args.owner, args.expected, args.policy)
        if c == 'runtime-review-assign': return runtime.review_assign(args.task, args.owner, args.expected, args.source)
        if c == 'runtime-usage': return runtime.usage(args.id, args.action, read_json(args.file), args.source)
        if c == 'runtime-release': return runtime.release(args.resource, args.action, args.evidence_event, args.source, read_json(args.file) if args.file else None)
        if c == 'runtime-resolve': return runtime.resolve(args.id, args.expected, read_json(args.file), args.source)
    if c == 'asset-put':
        return AssetRegistry(state).put(args.id, args.expected, read_json(args.file), args.source)
    if c == 'asset-revise':
        return AssetRegistry(state).revise(args.id, args.expected, read_json(args.file), args.source, args.version)
    if c == 'asset-history':
        return AssetRegistry(state).history(args.id)
    if c == 'asset-show':
        return AssetRegistry(state).get(args.id, args.version)
    if c == 'assets':
        registry = AssetRegistry(state)
        if args.verify:
            return registry.list(work=args.work, query=args.query)
        values = registry.metadata(work=args.work)
        return [v for v in values if not args.query or args.query.casefold() in encoded({'id': v['id'], 'body': v['body'], 'annotations': v['annotations']}).casefold()]
    if c == 'asset-use':
        require(bool(args.purpose) != bool(args.file), 'Specify either purpose text or a structured use file')
        return AssetRegistry(state).use(args.work, args.requirement, args.id, args.version, read_json(args.file) if args.file else args.purpose, args.source)
    if c == 'asset-impact':
        return AssetRegistry(state).impact(args.id, args.version)
    if c == 'portfolio':
        data = portfolio.view(state, args.work, verify=args.verify)
        return portfolio.markdown(data) if args.format == 'markdown' else data
    if c == "status":
        if args.work:
            return state.get(args.work)  # Preserve the existing exact-work API.
        if args.full:
            return state.works()
        def short(value, count=300):
            value = str(value or '')
            return value if len(value) <= count else value[:count] + '…'
        return [{'id': w['id'], 'title': short(w['title'], 120), 'status': w['status'],
                 'revision': w['revision'], 'objective': short(w['objective']),
                 'parent': w.get('parent'), 'requirements': len(w.get('requirements', [])),
                 'next_action': short(w.get('next_action')),
                 'detail': 'context --work ' + w['id'],
                 'coverage': 'Discovery summary; no history or professional evidence expanded'} for w in state.works()]
    if c == "update":
        return state.update(args.work, args.expected, read_json(args.file), args.source, args.input_id, args.input_expected, read_json(args.actor_file) if args.actor_file else None)
    if c == "bind":
        state.bind(args.session, args.work, args.role, args.source, args.expected_binding)
        return state.binding(args.session)
    if c == 'input-query':
        return state.input_query(args.id, args.work, args.session, args.limit)
    if c == 'input-link':
        return state.input_link(args.id, args.work, args.expected, args.scope_expected, read_json(args.file), args.source)
    if c == 'input-assess':
        return state.assess_input(args.id, args.expected, read_json(args.file), args.source)
    if c == 'input-effect':
        return state.interpret_input(args.id, args.work, args.expected, read_json(args.file), args.source)
    if c == "input-ack":
        state.ack(args.id, args.work, args.expected, args.source, args.scope_expected)
        return {"acknowledged": args.id}
    if c == "task":
        return state.create_task(args.id, args.work, args.expected, read_json(args.file), args.source)
    if c == 'task-link':
        return state.relink_task(args.task, args.expected, read_json(args.file), args.source)
    if c == 'decide':
        return state.decide(args.work, args.expected, read_json(args.file), args.source, read_json(args.actor_file) if args.actor_file else None)
    if c == "review":
        review = read_json(args.file)
        if args.disposition == "adopted":
            for ref in review.get("artifacts", []):
                require(isinstance(ref, str) and local_path(root, ref).is_file(), "Adoption artifact must exist in this workspace")
        return state.review_task(args.task, args.disposition, review, args.source)
    if c == "reserve":
        task = state.task(args.task)
        check_evidence(root, state, task["work_id"])
        return state.reserve(args.key, args.task, args.target, args.source, read_json(args.message_file), args.intent, args.relates_to)
    if c == "receipt":
        return state.settle(args.key, args.status, read_json(args.file), args.source)
    if c == "dispatch":
        return state.dispatch(args.key)
    if c == "observe":
        return {"observation": state.observation(args.asset, args.kind, read_json(args.file), args.source)}
    if c == "judge":
        body = read_json(args.file)
        for ref in body.get("evidence", []):
            if "path" in ref:
                path = local_path(root, ref["path"])
                require(path.is_file(), "Evidence file is missing")
                actual = digest(path)
                require(not ref.get("sha256") or ref["sha256"] == actual, "Evidence changed before adoption")
                ref["sha256"] = actual
            elif "judgment" in ref:
                parent = state.judgment(ref["judgment"])
                require(parent["id"] != args.id, "A judgment cannot depend on itself")
                ref["revision"] = parent["revision"]
                if args.status == "adopted":
                    require(parent["status"] == "adopted", "Dependency is not adopted")
        return state.judge(args.id, args.work, args.expected, args.status, body, args.source)
    if c == "improvement-review":
        return state.triage(args.work, args.event, read_json(args.file), args.source)
    if c == 'improvement-assign':
        from .improvements import assign_maintenance
        return assign_maintenance(state, args.work, args.event, args.expected, read_json(args.file), args.source)
    if c == 'improvement-query':
        from .improvements import query_improvements
        return query_improvements(state, args.work, relation=args.relation, include_resolved=args.include_resolved,
            event=args.event, limit=args.limit, offset=args.offset, history_limit=args.history_limit,
            history_offset=args.history_offset)
    if c == "history":
        return state.history(args.work)
    if c == "check":
        return {**state.check_work(args.work), "changed_evidence": check_evidence(root, state, args.work), "completion": goals.completion(state, state.get(args.work)), "overall": goals.overview(state, state.get(args.work)), "tasks": state.tasks(args.work)}
    if c == "completion-set":
        return state.set_task_completion(args.task, args.expected, read_json(args.file), args.source, read_json(args.change_file) if args.change_file else None)
    if c == "completion":
        require(bool(args.work) != bool(args.task), "Select exactly one work or task")
        target = state.get(args.work) if args.work else state.task(args.task)["body"]
        if args.work:
            return goals.completion(state, target)
        return check_contract(root, target.get("completion"), state, {args.task} if args.task else None)
    if c in ("index", "find", "context"):
        index = Discovery(root, directory, state=state, readonly=args.read_only or c == 'context' or (c == 'find' and args.cached))
        try:
            if c in ("index", "find") and not getattr(args, "cached", False):
                config = read_json(args.config or root / "harness/scopes.json")
                report = index.scan(config)
                if c == "index":
                    return report
            if c == "find":
                result = index.find(args.query, args.limit)
                for item in result["results"]:
                    item["observations"] = state.observations("sha256:" + item["sha"]) if item["sha"] else []
                return result
            return render_context(state, args.work, args.session, index, args.query,
                full=args.full, verify=args.verify, task=args.task, budget=args.budget, limit=args.limit,
                retrieval=read_json(args.context_file) if args.context_file else None)
        finally:
            index.close()
    if c == "backup":
        state.backup(args.output)
        return {"path": str(args.output), "sha256": digest(args.output)}
    if c == "render":
        return render(state)
    if c == "doctor":
        hooks = [json.loads(r[0]) for r in state.db.execute("SELECT payload FROM events WHERE kind='hook_observed' ORDER BY seq DESC LIMIT 10")]
        return {"root": str(root), "store": str(directory), "database": state.db.execute("PRAGMA integrity_check").fetchone()[0],
                "hooks_configured": (root / ".codex/hooks.json").exists(), "observed_hook_records": hooks,
                "hook_status_note": "Config existence and protocol test records do not prove host trust or actual triggering. Verify in the host.",
                "guard_evaluations": [json.loads(r[0]) for r in state.db.execute("SELECT payload FROM events WHERE kind='guard_evaluated' ORDER BY seq DESC LIMIT 10")],
                "guard_started_without_completion": [json.loads(r[0]) for r in state.db.execute("SELECT s.payload FROM events s WHERE s.kind='guard_started' AND NOT EXISTS (SELECT 1 FROM events e WHERE e.kind='guard_evaluated' AND json_extract(e.payload,'$.evaluation_id')=json_extract(s.payload,'$.evaluation_id')) ORDER BY s.seq DESC LIMIT 10")],
                "read_only_business_connection": state.readonly,
                "coverage": "Explicitly managed paths; missing completion records remain unverified, historical observed/sent does not prove guard completion"}
    raise HarnessError("Unsupported command")


def build_version():
    from hashlib import sha256
    package = Path(__file__).resolve().parent
    content = [(str(path.relative_to(package)), digest(path)) for path in sorted(package.rglob('*.py'))]
    return {'code_build': sha256(encoded(content).encode()).hexdigest(), 'schema_version': migrations.VERSION,
            'read_schemas': [0, 1, 2, 3], 'write_schemas': [3], 'api_versions': [1, 2],
            'migration_id': migrations.MIGRATION, 'migration_checksum': migrations.checksum(),
            'origin_support': 'Raw host source is retained; human origin requires explicit evidence review'}


def _write_new_json(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return {'path': str(path.resolve()), 'sha256': digest(path)}


def _error_code(exc):
    if isinstance(exc, HarnessError):
        return 'REVISION_CONFLICT' if exc.code == 'VALIDATION_ERROR' and str(exc).startswith('Stale') else exc.code
    if isinstance(exc, sqlite3.Error):
        return 'BUSY' if getattr(exc, 'sqlite_errorcode', 0) & 255 in (5, 6) else 'CORRUPT_STORE'
    return 'IO_ERROR' if isinstance(exc, OSError) else 'INVALID_INPUT'


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(arguments)
    except HarnessError as exc:
        v2 = any(value == '--api-version=2' or (value == '--api-version' and index + 1 < len(arguments)
                 and arguments[index + 1] == '2') for index, value in enumerate(arguments))
        if v2:
            print(encoded({'format': 'htam.response/v2', 'observed_at': now(),
                           'error': {'code': exc.code, 'message': str(exc)}}), file=sys.stderr)
        else:
            p = parser(); p.print_usage(sys.stderr)
            print(p.prog + ': error: ' + str(exc), file=sys.stderr)
        return 2
    root = args.root.resolve()
    directory = (args.store or root / ".harness-local/manager").resolve()
    event = None
    state = None
    def emit(result, *, compact=False):
        if args.api_version == 2 and event is None:
            result = {'format': 'htam.response/v2', **build_version(), 'observed_at': now(),
                      'data_schema_version': state.schema_version if state else (result.get('schema_version', result.get('source_version')) if isinstance(result, dict) else None),
                      'coverage': 'Requested command only; native and scientific effects require their own receipts', 'data': result}
        print(json.dumps(result, ensure_ascii=False, indent=None if compact else 2))
    try:
        if args.command == 'version':
            emit(build_version())
            return 0
        if args.command == 'dot-package':
            from . import dot_package
            require(not args.read_only or args.dot_operation == 'check', 'Read-only mode refuses Dot package export')
            if args.dot_operation == 'export':
                result = dot_package.export(root, args.output, source_thread=args.source_thread,
                    authorization_reference=args.authorization_reference, cloud_directory=args.cloud_directory)
            else:
                result = dot_package.check(args.package, source_root=root if args.check_sources else None,
                    manifest_sha256=args.manifest_sha256)
            emit(result)
            return 0
        if args.command in ('migration-plan', 'migration-apply', 'migration-verify', 'recovery-export', 'recovery-restore'):
            require(not args.read_only or args.command in ('migration-plan', 'migration-verify', 'recovery-export'),
                    'Read-only mode refuses this mutating command')
            path = directory / 'state.sqlite'
            if args.command == 'migration-plan':
                result = migrations.preflight(path)
                if args.output:
                    _write_new_json(args.output, result)
            elif args.command == 'migration-apply':
                result = migrations.apply(path, read_json(args.file), args.backup)
            elif args.command == 'migration-verify':
                result = migrations.verify(path)
            elif args.command == 'recovery-export':
                result = _write_new_json(args.output, migrations.recovery_export(args.baseline, path))
            else:
                result = migrations.restore(args.baseline, args.output, read_json(args.delta) if args.delta else None)
            emit(result)
            return 0
        if args.command == "hooks-config":
            require(not args.output.exists(), "Hook destination exists; inspect and merge explicitly")
            config = hooks_config(root, args.windows_distro)
            atomic_text(args.output, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
            emit({"path": str(args.output), "status": "configured; host trust and activation not verified"})
            return 0
        if args.command == "hook":
            event = json.loads(sys.stdin.read(1024 * 1024).lstrip("\ufeff"))
        if not (directory / "state.sqlite").exists() and args.command != "update":
            raise HarnessError("No state store; explicitly initialize a work with update --expected 0")
        observational = {'status', 'planning', 'change-preview', 'history', 'find', 'index', 'context', 'dispatch', 'doctor', 'backup', 'completion', 'assets', 'asset-show', 'asset-impact', 'asset-history', 'portfolio', 'input-query',
                         'retrieve', 'entry-show', 'asset-search', 'asset-handoff', 'experience-show', 'experience-select', 'runtime-owner-preview',
                         'runtime-action', 'runtime-overview', 'runtime-scope', 'runtime-control-view', 'runtime-inputs', 'improvement-query'}
        if args.command == 'experiment' and args.operation in EXPERIMENT_READONLY:
            observational.add('experiment')
        if args.command == 'native-entry' and args.operation in ('show', 'recover', 'check'):
            observational.add('native-entry')
        if args.command == 'coordination-message' and args.operation == 'show':
            observational.add('coordination-message')
        if args.read_only:
            require(args.command in observational and args.command != 'index', 'Read-only mode refuses this mutating command')
            require(args.command != 'find' or args.cached, 'Use find --cached in read-only mode')
        state = State(directory, root, readonly=args.command in observational,
                      readonly_snapshot=args.command in observational)
        if args.command == "hook":
            state.db.execute('PRAGMA busy_timeout=750')
            with validation_scope(timeout=args.guard_timeout):
                result = handle_hook(state, event, root)
            snapshot(state)
        else:
            with validation_scope():
                result = execute(args, root, directory, state)
            if args.command not in observational and args.command not in ('experiment', 'native-entry', 'coordination-message', 'improvement-assign') and not args.command.startswith(('runtime-', 'experience-')):
                snapshot(state)
                render(state)
        compact = event is not None or (args.command == 'context' and not args.full and not args.task)
        if args.command == 'portfolio' and args.format == 'markdown' and args.api_version == 1:
            print(result, end='')
        else:
            emit(result, compact=compact)
        return 0
    except (HarnessError, sqlite3.Error, OSError, ValueError) as exc:
        if event is not None:
            print(encoded(fallback_hook(directory, event, exc)))
            return 0
        error = {'error': str(exc)} if args.api_version == 1 else {
            'format': 'htam.response/v2', 'observed_at': now(),
            'error': {'code': _error_code(exc), 'message': str(exc)}}
        print(encoded(error), file=sys.stderr)
        return 2
    finally:
        if state:
            state.close()
