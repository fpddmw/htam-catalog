"""One approved build's delegated testing; ordinary scientific approval unchanged."""
import json
import re

from .contracts import tree
from .isolation import Limits

from ..common import digest, require
from .journal import Journal, hashed, human, safe_path

SCOPE = 'F09-native-synthetic-20261020'
PREFIX = '.harness-local/harness-v4-build-20261020/b06/f09-native-subscription/validation'
ROOT_SESSION = '01a15520-3a8c-7292-b9b1-9bacb52060f5'
USER_MESSAGE = '01a1589e-0a3f-7d21-99bc-d073ee78b4b1'
GENERATION_COMPONENT = 'synthetic_generation_wiring'
GENERATION_PREFIX = PREFIX + '/p2-meta-wiring-draft-01'
GENERATION_ACTIONS = (
    {'experiment': 'f09-native-p2-wiring', 'run_id': 'f09-p2-generate-04', 'role': 'p2',
     'arm': 'H1', 'split': 'proposal_generate', 'case': '__proposal__', 'generation': True, 'units': 1},
    {'experiment': 'f09-native-meta-wiring', 'run_id': 'f09-meta-generate-05', 'role': 'meta',
     'arm': 'H0', 'split': 'meta_generate', 'case': '__meta__', 'generation': True, 'units': 1},
    {'experiment': 'f09-native-meta-wiring', 'run_id': 'f09-generated-h-execute-06', 'role': 'generated_h',
     'arm': 'H0', 'split': 'dev', 'case': 'dev', 'generation': False, 'units': 1},
)


CORRECTION_PREFIX = PREFIX + '/p2-meta-wiring-correction-01'
CORRECTION_ACTIONS = (
    {'experiment': 'f09-native-p2-wiring-correction-01', 'run_id': 'f09-p2-generate-05', 'role': 'p2',
     'arm': 'H1', 'split': 'proposal_generate', 'case': '__proposal__', 'generation': True, 'units': 1},
    {'experiment': 'f09-native-meta-wiring-correction-01', 'run_id': 'f09-meta-generate-06', 'role': 'meta',
     'arm': 'H0', 'split': 'meta_generate', 'case': '__meta__', 'generation': True, 'units': 1},
    {'experiment': 'f09-native-meta-wiring-correction-01', 'run_id': 'f09-generated-h-execute-07', 'role': 'generated_h',
     'arm': 'H0', 'split': 'dev', 'case': 'dev', 'generation': False, 'units': 1},
)
SCHEMA_FAILURE_SHA256 = '1f4614302ed035a0e15bbf420490943de046fb971cda2c7cfa3ab14344d6263c'
SCHEMA_FAILURE_MESSAGE = ("Invalid schema for response_format 'codex_output_schema': "
    "'schema.properties.files.required' is required to include every key in properties. Missing 'H.prompt.txt'.")


def _generation_batch(scope):
    kind = scope.get('test_scope_type')
    require(kind in ('p2_meta_three_turn_v1', 'p2_meta_strict_schema_correction_v1'),
        'Unknown construction generation test scope', 'CONSTRUCTION_SCOPE')
    return ((GENERATION_PREFIX, GENERATION_ACTIONS, 3) if kind == 'p2_meta_three_turn_v1'
        else (CORRECTION_PREFIX, CORRECTION_ACTIONS, 4))


def _schema_correction_prerequisite(state, lab_root, work):
    """Only this exact closed provider schema failure permits the new batch."""
    path = safe_path(state.root, PREFIX + '/root-p2-failure-04.json')
    require(path.is_file() and digest(path) == SCHEMA_FAILURE_SHA256,
        'Exact root-reviewed provider schema failure evidence required', 'CONSTRUCTION_PREDECESSOR')
    report = json.loads(path.read_text(encoding='utf-8'))
    require(report.get('schema') == 'htam.root-native-generation-result/v1' and
        report.get('decision') == 'known_provider_schema_failure_stop_original_three_step_batch' and
        report.get('run') == 'f09-p2-generate-04' and report.get('status') == 'failed' and
        report.get('scope_before') == 3 and report.get('scope_after') == 4 and
        report.get('native_candidates') == 0 and report.get('remaining_batch_started') is False,
        'Original batch failure/stop is not the exact reviewed event', 'CONSTRUCTION_PREDECESSOR')
    reader = Journal(); reader.state = state
    p2, meta = reader.get('f09-native-p2-wiring'), reader.get('f09-native-meta-wiring')
    require(all((v.get('end') or {}).get('outcome') == 'observed' and v['contract']['spec']['work'] == work and not v['candidates'] and not v['generations'] and
        v['contract'].get('lab_root') == str(lab_root.relative_to(state.root)) for v in (p2, meta)) and
        set(p2['runs']) == {'f09-p2-generate-04'} and not meta['runs'],
        'Original P2/M batches must remain ended with no candidate/M execution', 'CONSTRUCTION_PREDECESSOR')
    run = p2['runs']['f09-p2-generate-04']; receipt = run.get('receipt', {})
    require(run.get('status') == 'failed' and run.get('generation') is True and run.get('arm') == 'H1' and
        run.get('split') == 'proposal_generate' and run.get('case') == '__proposal__' and
        run.get('invocation', {}).get('role') == 'p2' and run.get('native_authorization_scope') == SCOPE and
        run.get('reserved_units') == 1 and run.get('receipt_sha256') == hashed(receipt) and
        receipt.get('execution_succeeded') is False and not receipt.get('artifacts') and not receipt.get('blocking_failure'),
        'Original exact failed P2 intent/receipt is not closed known failure', 'CONSTRUCTION_PREDECESSOR')
    root = safe_path(state.root, receipt['run_dir'])
    require(root.is_relative_to(lab_root / p2['id'] / 'runs' / run['run_id']), 'Original failure run root differs', 'GENERATION_PROVENANCE')
    evidence = report.get('evidence', {})
    require(set(evidence) == {'request.json', 'facts.jsonl', 'terminal.json', 'run.json'},
        'Complete original failure evidence required', 'GENERATION_PROVENANCE')
    files = {}
    for name, ref in evidence.items():
        file = safe_path(state.root, ref['path'])
        require(file == root / name and file.is_file() and file.stat().st_size <= 2 * 1024 * 1024 and digest(file) == ref['sha256'],
            'Original schema failure protected evidence changed', 'GENERATION_PROVENANCE')
        files[name] = file
    request = json.loads(files['request.json'].read_text(encoding='utf-8')); context = request.get('context', {})
    original = json.loads(files['run.json'].read_text(encoding='utf-8'))
    terminal = json.loads(files['terminal.json'].read_text(encoding='utf-8'))
    facts = [json.loads(line) for line in files['facts.jsonl'].read_text(encoding='utf-8').splitlines()]
    native, closed = report['terminal'], report['close']
    error = json.loads(native.get('error', {}).get('message', '{}'))
    require(native in facts and closed in facts and native.get('kind') == 'turn_terminal' and
        native.get('nativeStatus') == 'failed' and native.get('runtimeId') == closed.get('runtimeId') and
        closed.get('kind') == 'runtime_closed' and closed.get('transport', {}).get('closed') is True and
        closed.get('transport', {}).get('pendingRequests') == 0 and native.get('threadId') in closed.get('bindingThreadIds', []) and
        error.get('status') == 400 and error.get('error') == {'message': SCHEMA_FAILURE_MESSAGE,
            'type': 'invalid_request_error', 'param': 'text.format.schema', 'code': 'invalid_json_schema'},
        'Only the exact provider missing-required schema error and actual closure permit correction', 'CONSTRUCTION_PREDECESSOR')
    require({**original, 'run_dir': str(root)} == receipt and
        all(context.get(k) == run[k] for k in ('run_id', 'request_sha256', 'intent_observation', 'executor_identity')) and
        context.get('experiment') == p2['id'] and request.get('output_contract') == run['invocation']['output_contract'] and
        receipt.get('request_file_sha256') == terminal.get('request_file_sha256') == digest(files['request.json']) and
        receipt.get('facts_sha256') == terminal.get('facts_sha256') == digest(files['facts.jsonl']) and
        receipt.get('terminal_sha256') == digest(files['terminal.json']) and receipt.get('native') == terminal.get('native') and
        all(receipt['native'].get(k) == native.get(v) for k, v in
            (('runtime_id', 'runtimeId'), ('thread_id', 'threadId'), ('turn_id', 'turnId'), ('terminal_status', 'nativeStatus'))),
        'Original schema failure receipt/intent/terminal identities differ', 'GENERATION_PROVENANCE')


def _generation_scope(scope, *, frozen=False):
    _, expected_actions, baseline = _generation_batch(scope)
    require(
        scope.get('state_store') == PREFIX + '/store' and scope.get('lab_roots') == [PREFIX + '/lab'] and
        type(scope.get('baseline_reserved')) is int and scope['baseline_reserved'] == baseline and
        type(scope.get('new_units_max')) is int and scope['new_units_max'] == 3 and
        scope['native_turn_attempts_max'] == 8, 'Exact three-step construction scope required', 'CONSTRUCTION_SCOPE')
    if not frozen:
        require('actions' not in scope and 'frozen' not in scope,
            'Executor review precedes prepared contract actions; it grants no run', 'CONSTRUCTION_SCOPE')
        return None
    actions = scope.get('actions')
    require(isinstance(actions, list) and len(actions) == 3, 'Exact three construction actions required', 'CONSTRUCTION_SCOPE')
    for actual, expected in zip(actions, expected_actions):
        require(isinstance(actual, dict) and set(actual) == set(expected) | {'contract_sha256'} and
            all(type(actual[k]) is type(v) and actual[k] == v for k, v in expected.items()) and
            isinstance(actual['contract_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', actual['contract_sha256']),
            'Construction action table differs from the three approved steps', 'CONSTRUCTION_SCOPE')
    require(actions[0]['contract_sha256'] != actions[1]['contract_sha256'] and
        actions[1]['contract_sha256'] == actions[2]['contract_sha256'], 'M and its H must use the same contract', 'APPROVAL_HASH')
    return actions


def _generation_contract(state, scope, contract):
    actions = _generation_scope(scope, frozen=True); spec = contract['spec']
    expected_component = 'H' if contract['sha256'] == actions[0]['contract_sha256'] else 'M'
    require(contract['sha256'] in {a['contract_sha256'] for a in actions} and
        contract['sha256'] == hashed({k: contract[k] for k in ('spec', 'baseline_manifest', 'targets')}) and
        spec['component'] == expected_component and spec.get('generation') and
        not spec.get('evaluation_reassessment_of') and
        not any(spec[k].get('value', {}).get('asset_id') for k in ('H', 'M')),
        'Generation contract is not the exact reviewed synthetic H/M contract', 'CONSTRUCTION_SCOPE')
    budget = spec['budget']; units = 1 if expected_component == 'H' else 2
    require(budget.get('budget_unit') == 'native_turn_attempt' and
        all(type(budget.get(k)) is int and budget[k] == units for k in ('max_runs', 'max_calls', 'per_arm_runs', 'per_arm_calls')) and
        budget.get('max_candidates') == 1 and budget.get('max_revisions') == 0 and
        budget.get('max_wall_seconds') == 180 * units and budget.get('per_arm_wall_seconds') == 180 * units and
        spec['limits']['wall_seconds'] == 120 and spec['A']['value'].get('network') is False and
        spec['A']['value'].get('production_write') is False, 'Generation case/cost ceiling changed', 'CONSTRUCTION_SCOPE')
    require([(c['id'], c['split']) for c in spec['cases']] == [('dev', 'dev'), ('select', 'select'), ('holdout', 'holdout')] and
        spec.get('execution_entrypoint') == 'H.prompt.txt' and
        spec['generation']['entrypoint'] == ('P2.prompt.txt' if expected_component == 'H' else 'M.prompt.txt') and
        spec['entrypoint'] == ('H.prompt.txt' if expected_component == 'H' else 'M.prompt.txt'),
        'Generation entry/cases differ from this batch', 'CONSTRUCTION_SCOPE')
    if scope['test_scope_type'] == 'p2_meta_strict_schema_correction_v1':
        require(spec['generation']['output'].get('protocol') == 'htam.native-output/v2' and
            spec['generation']['output'].get('optional_null_files') == ['H.prompt.txt'] and
            spec['native_output'].get('protocol') == 'htam.native-output/v2' and
            spec['native_output'].get('optional_null_files') == [],
            'Schema correction requires the reviewed v2 nullable generation wire', 'CONSTRUCTION_SCOPE')
    paths = [spec['baseline_dir'], *[t['path'] for t in contract['targets'].values()]]
    paths += [value for c in spec['cases'] for value in (c['input_dir'], c['answer'])]
    paths += [spec[k]['path'] for k in ('U', 'A', 'H', 'M', 'E') if spec[k].get('path')]
    paths += [spec[k] for k in ('starting_H_dir',) if k in spec]
    if spec['generation'].get('producer_dir'): paths.append(spec['generation']['producer_dir'])
    prefix = safe_path(state.root, _generation_batch(scope)[0])
    require(all(safe_path(state.root, p).is_relative_to(prefix) for p in paths),
        'Generation input/method/target outside exact synthetic batch', 'CONSTRUCTION_SCOPE')


def contract_details(contract):
    """Exact material facts pinned by root, not a model's description of data."""
    spec = contract['spec']
    result = {'targets': contract['targets'], 'baseline_dir': spec['baseline_dir'],
        'baseline_manifest': contract['baseline_manifest'],
        'cases': [{k: c[k] for k in ('id', 'split', 'family', 'source_ref', 'input_dir', 'answer', 'input_manifest', 'answer_sha256')}
            for c in spec['cases']], 'U_A_E_sha256': {k: hashed(spec[k]) for k in ('U', 'A', 'E')}}
    if spec.get('generation'):
        # Legacy H-only detail projection is unchanged. The new explicit scope
        # also pins normalized generation/output/starting-H and task/cost facts.
        result['generation_details'] = {k: spec[k] for k in ('work', 'task', 'work_request_revision', 'component',
            'depth', 'H', 'M', 'budget', 'limits', 'entrypoint', 'execution_entrypoint', 'generation', 'native_output',
            'environment', 'producer_sessions')}
        result['generation_details'].update({k: spec[k] for k in ('starting_H_dir', 'starting_H_manifest',
            'generated_H_editable', 'first_layer_experiments', 'meta_reason') if k in spec})
    return result


def construction_authority(state, admission, subject_hash, executor_identity, *, subject_kind, lab_root, contract=None):
    require(isinstance(admission, dict) and admission.get('schema') == 'htam.construction-admission/v1' and
        admission.get('mode') == 'delegated_construction', 'Exact construction admission required', 'CONSTRUCTION_SCOPE')
    scope = admission.get('scope', {}); original = admission.get('original_authority', {})
    require(scope.get('id') == SCOPE and scope.get('data') == 'synthetic_only' and
        scope.get('model') == 'gpt-6.1-sol' and scope.get('effort') == 'xhigh' and
        type(scope.get('native_turn_attempts_max')) is int and 1 <= scope['native_turn_attempts_max'] <= 8 and
        scope.get('candidate_network') is False and scope.get('production_write') is False and
        scope.get('component') in ('H_prompt_or_structured_decision', GENERATION_COMPONENT) and scope.get('permitted_local_prefix') == PREFIX,
        'Delegation is limited to this exact synthetic F09 build', 'CONSTRUCTION_SCOPE')
    generation_scope = scope.get('component') == GENERATION_COMPONENT
    if generation_scope: _generation_scope(scope, frozen=subject_kind == 'frozen_contract')
    require(executor_identity.get('executor') == 'codex-subscription-profiled/v1' and
        executor_identity.get('native_authorization_scope') == SCOPE and
        executor_identity.get('native_scope_cap') == scope['native_turn_attempts_max'] and
        executor_identity.get('model') == scope['model'] and executor_identity.get('reasoning_effort') == scope['effort'] and
        executor_identity.get('candidate_network') is False, 'Executor differs from delegated construction scope', 'CONSTRUCTION_SCOPE')
    require(original.get('user_message_id') == USER_MESSAGE and original.get('root_thread_id') == ROOT_SESSION and
        isinstance(original.get('approved_plan_sha256'), str), 'Original v4 human authority must remain identified', 'PROVENANCE_UNVERIFIED')
    plan_ref = original.get('approved_plan_ref', {}); original_ref = original.get('original_record_ref', {})
    require(plan_ref.get('path') == '.harness-local/harness-diagnosis-20261020/09-完整框架建设工作计划.md' and
        digest(safe_path(state.root, plan_ref['path'])) == plan_ref.get('sha256') == original['approved_plan_sha256'],
        'Original approved v4 plan content changed', 'APPROVAL_HASH')
    record_path = safe_path(state.root, original_ref.get('path', ''))
    require(record_path.is_file() and digest(record_path) == original_ref.get('sha256'), 'Original native authority reference changed', 'PROVENANCE_UNVERIFIED')
    native_record = json.loads(record_path.read_text())
    messages = [m for m in native_record.get('items', []) if m.get('id') == USER_MESSAGE]
    received = state.input_record(original['approval']['input_id'])
    require(native_record.get('threadId') == ROOT_SESSION and len(messages) == 1 and
        ''.join(p.get('text', '') for p in messages[0].get('content', []) if p.get('type') == 'text') == received['prompt'],
        'B02 input text does not match the exact original v4 native message', 'PROVENANCE_UNVERIFIED')
    # This approves the original displayed v4 plan. No statement is made that
    # the human had already seen the later executor/contract implementation.
    authority = human(state, scope['work'], original['approval'], original['approved_plan_sha256'])
    prefix = safe_path(state.root, PREFIX)
    labs = scope.get('lab_roots')
    require(isinstance(labs, list) and 1 <= len(labs) <= 4 and all(isinstance(v, str) for v in labs), 'Exact bounded construction lab roots required')
    require(all(safe_path(state.root, p).is_relative_to(prefix) for p in labs) and
        str(lab_root.relative_to(state.root)) in labs, 'Lab lies outside delegated synthetic build', 'CONSTRUCTION_SCOPE')
    require(subject_kind in ('executor_verification', 'frozen_contract') and
        admission.get('subject') == {'kind': subject_kind, 'sha256': subject_hash}, 'Construction subject was revised', 'APPROVAL_HASH')
    producers = scope.get('producer_sessions')
    require(isinstance(producers, list) and producers and all(isinstance(s, str) and s for s in producers), 'Known construction producers required')
    if contract is not None:
        spec = contract['spec']
        require(subject_kind == 'frozen_contract' and spec['work'] == scope['work'], 'Construction work/subject changed', 'CONSTRUCTION_SCOPE')
        if generation_scope:
            require(subject_hash == contract['sha256'], 'Generation subject hash differs', 'APPROVAL_HASH')
            _generation_contract(state, scope, contract)
        else:
            require(spec['component'] == 'H' and not spec.get('evaluation_reassessment_of') and
                not spec['H']['value'].get('asset_id') and not spec.get('generation'),
                'Construction delegation cannot authorize formal assets, M or E changes', 'CONSTRUCTION_SCOPE')
        require(set(spec['producer_sessions']) <= set(producers) and scope.get('frozen') == contract_details(contract),
            'Original files/cases/U/A/E differ from root-reviewed construction proposal', 'APPROVAL_HASH')
        paths = [spec['baseline_dir'], *[t['path'] for t in contract['targets'].values()]]
        paths += [value for c in spec['cases'] for value in (c['input_dir'], c['answer'])]
        require(all(safe_path(state.root, value).is_relative_to(prefix) for value in paths),
            'Construction data/target path outside synthetic prefix', 'CONSTRUCTION_SCOPE')
    provenance = admission.get('root_review', {})
    require(provenance.get('kind') == 'agent_result' and provenance.get('session') == ROOT_SESSION and ROOT_SESSION not in producers,
        'Root review needs the original independent session provenance', 'REVIEW_IDENTITY')
    task = state.task(provenance['task_id'])
    task_sessions = {task['body'].get('executor_session'), task['body'].get('producer')} - {None, ''}
    task_sessions.update(r[0] for r in state.db.execute('SELECT target FROM dispatches WHERE task_id=?', (task['id'],)))
    require(task['work_id'] == scope['work'] and provenance['session'] in task_sessions and task['status'] in ('returned', 'adopted'),
        'Root review does not match original returned review task', 'REVIEW_IDENTITY')
    row = state.db.execute('SELECT * FROM events WHERE seq=?', (provenance['event_seq'],)).fetchone()
    require(row is not None and row['kind'] == 'task_returned', 'Original root task return event required', 'REVIEW_IDENTITY')
    event = json.loads(row['payload']); returned = event.get('review', {})
    path = safe_path(state.root, provenance['path'])
    require(event.get('task') == task['id'] and provenance['path'] in returned.get('artifacts', []) and
        provenance.get('sha256') == digest(path) and provenance['sha256'] in returned.get('answer', ''),
        'Root review bytes are not the original returned report', 'REVIEW_IDENTITY')
    report = json.loads(path.read_text())
    require(report.get('schema') == 'htam.construction-review/v1' and report.get('decision') == 'accept' and
        report.get('subject') == admission['subject'] and report.get('scope_sha256') == hashed(scope) and
        report.get('original_authority_sha256') == hashed(original) and report.get('executor_identity_sha256') == hashed(executor_identity) and
        isinstance(report.get('reason'), str) and report['reason'].strip() and isinstance(report.get('limitations'), list),
        'Root review does not accept this exact identity/scope/subject', 'APPROVAL_HASH')
    if generation_scope and scope['test_scope_type'] == 'p2_meta_strict_schema_correction_v1':
        _schema_correction_prerequisite(state, lab_root, scope['work'])
    return {**admission, 'verified_original_plan_authority': authority,
        'root_review_identity': {**provenance, 'event_source': row['source'], 'event_at': row['at']},
        'meaning': 'Original human v4 delegation; root accepted later exact synthetic construction subject, not a later human source-code approval'}


def _completed_generation(lab, view, run, expected):
    """Read the original accepted receipt and both archived/registered bundles."""
    require(run.get('status') == 'completed' and run.get('generation') is True and
        run.get('invocation', {}).get('role') == expected['role'] and run.get('arm') == expected['arm'],
        'A prior construction generation did not complete; stop this three-step batch', 'CONSTRUCTION_PREDECESSOR')
    receipt = run.get('receipt', {})
    require(run.get('receipt_sha256') == hashed(receipt),
        'Prior committed construction receipt changed', 'GENERATION_PROVENANCE')
    root = safe_path(lab.state.root, receipt['run_dir'])
    require(root.is_relative_to(lab.directory / view['id'] / 'runs' / expected['run_id']),
        'Prior receipt root differs from its exact run', 'GENERATION_PROVENANCE')
    # A new host object need not own this closed predecessor runtime. Check
    # the immutable evidence already accepted at run_result, without invoking
    # recovery, importing a receipt or changing the observer's ownership set.
    files = {name: safe_path(lab.state.root, str(root / name)) for name in
        ('run.json', 'request.json', 'facts.jsonl', 'terminal.json')}
    require(all(p.is_file() and p.stat().st_size <= 2 * 1024 * 1024 for p in files.values()),
        'Prior protected construction evidence unavailable/beyond bounds', 'GENERATION_PROVENANCE')
    original = json.loads(files['run.json'].read_text(encoding='utf-8'))
    request = json.loads(files['request.json'].read_text(encoding='utf-8')); context = request.get('context', {})
    require({**original, 'run_dir': str(root)} == receipt and receipt.get('execution_succeeded') is True and
        receipt.get('executor_identity') == run['executor_identity'] == lab.executor.identity() and
        receipt.get('source_manifest') == {'candidate': run['method_manifest'], 'input': run['input_manifest']} and
        receipt.get('request_file_sha256') == digest(files['request.json']) and
        receipt.get('facts_sha256') == digest(files['facts.jsonl']) and receipt.get('terminal_sha256') == digest(files['terminal.json']) and
        context.get('request_sha256') == run['request_sha256'] and context.get('run_id') == run['run_id'] and
        context.get('intent_observation') == run['intent_observation'] and context.get('executor_identity') == run['executor_identity'] and
        request.get('output_contract') == run['invocation']['output_contract'],
        'Prior committed receipt/request/facts/terminal identity differs', 'GENERATION_PROVENANCE')
    produced = tree(root / 'result/artifacts/generated', Limits(**view['contract']['spec']['limits']))
    accepted = {a['path'].removeprefix('generated/'): {'sha256': a['sha256'], 'bytes': a['bytes']}
        for a in receipt['artifacts'] if a['path'].startswith('generated/')}
    require(produced == accepted and produced, 'Prior generated artifacts differ from their receipt', 'CONTENT_CHANGED')
    if expected['role'] == 'p2':
        candidate = view['candidates'].get(expected['arm'], {})
        require(candidate.get('producer_run') == expected['run_id'] and
            candidate.get('producer_receipt_sha256') == run['receipt_sha256'] and
            candidate.get('files') == produced and candidate.get('sha256') == hashed(produced) and
            tree(lab.directory / view['id'] / 'candidates' / expected['arm'], Limits(**view['contract']['spec']['limits'])) == produced,
            'Exact prior P2 candidate was not registered or changed', 'GENERATION_PROVENANCE')
    else:
        spec = view['contract']['spec']; generated = view['generations'].get(expected['arm'], {})
        destination = lab.directory / view['id'] / 'generated' / expected['arm']
        require(generated.get('generation_run') == expected['run_id'] and
            generated.get('path') == str(destination.relative_to(lab.state.root)) and
            generated.get('files') == produced and generated.get('sha256') == hashed(produced) and
            generated.get('generator_sha256') == hashed(run['method_manifest']) and
            generated.get('starting_H_sha256') == hashed(spec['starting_H_manifest']) and
            generated.get('common_input_sha256') == hashed(run['input_manifest']) and
            generated.get('depth') == 2 and generated.get('recursive_generation_allowed') is False and
            tree(destination, Limits(**spec['limits'])) == produced,
            'Exact generated H was not registered from its original M run', 'GENERATION_PROVENANCE')
    return produced


def guard_construction_action(lab, name, view, run, *, stage, action=None):
    """Admission only; the caller commits intent/send once in its own transaction.

    Ordinary human approval and the legacy H scope retain their old paths. This
    explicit branch adds no counters, grants, receipt imports or retry mechanism.
    It is never used for exact owned interrupt or recovery-only collection.
    """
    approval = view['contract']['approval']; scope = approval.get('scope', {})
    if approval.get('mode') != 'delegated_construction' or scope.get('component') != GENERATION_COMPONENT:
        return None
    require(stage in ('reserve', 'send') and lab.state.db.in_transaction and not lab.state.readonly,
        'Construction admission must share the actual intent/send transaction', 'CONSTRUCTION_SCOPE')
    actions = _generation_scope(scope, frozen=True)
    construction_authority(lab.state, approval, view['contract']['sha256'], lab.executor.identity(),
        subject_kind='frozen_contract', lab_root=lab.directory, contract=view['contract'])
    actual_store = next((r[2] for r in lab.state.db.execute('PRAGMA database_list') if r[1] == 'main'), '')
    require(actual_store and safe_path(lab.state.root, actual_store) ==
        safe_path(lab.state.root, scope['state_store'] + '/state.sqlite'),
        'Construction actions require the original same Store', 'CONSTRUCTION_SCOPE')
    role = run.get('invocation', {}).get('role') if run.get('generation') else 'generated_h'
    matches = [i for i, a in enumerate(actions) if (a['experiment'], a['run_id'], a['role'], a['arm'], a['split'], a['case'], a['generation']) ==
        (name, run.get('run_id'), role, run.get('arm'), run.get('split'), run.get('case'), run.get('generation'))]
    require(len(matches) == 1 and type(run.get('generation')) is bool, 'Run/case/arm/role is outside the three reviewed construction actions', 'CONSTRUCTION_SCOPE')
    index = matches[0]; expected = actions[index]
    require(view['id'] == name and view['contract']['sha256'] == expected['contract_sha256'] and
        type(run.get('reserved_units')) is int and run['reserved_units'] == expected['units'] and
        run.get('budget_unit') == 'native_turn_attempt' and run.get('native_authorization_scope') == SCOPE and
        run.get('native_scope_cap') == 8 and run.get('executor_identity') == lab.executor.identity() == view['contract']['spec']['environment'] and
        run.get('run_root') == str((lab.directory / name / 'runs' / run['run_id']).relative_to(lab.state.root)),
        'Construction reservation identity/cost/root changed', 'CONSTRUCTION_SCOPE')
    usage = lab._native_scope_usage(SCOPE)
    require(usage['reserved'] == scope['baseline_reserved'] + index + (1 if stage == 'send' else 0) and
        not usage['blocking'] and usage['unknown'] == ([{'experiment': name, 'run_id': run['run_id']}] if stage == 'send' else []),
        'Construction order, unknown execution or same-Store budget changed', 'CONSTRUCTION_ORDER')
    for prior in actions[:index]:
        old = lab.get(prior['experiment']); old_approval = old['contract']['approval']
        require(old['contract']['sha256'] == prior['contract_sha256'] and
            old_approval.get('scope', {}).get('actions') == actions,
            'Previous construction contract/action table differs', 'APPROVAL_HASH')
        construction_authority(lab.state, old_approval, old['contract']['sha256'], lab.executor.identity(),
            subject_kind='frozen_contract', lab_root=lab.directory, contract=old['contract'])
        previous = old['runs'].get(prior['run_id'], {})
        produced = _completed_generation(lab, old, previous, prior)
        if expected['role'] == 'generated_h' and prior['role'] == 'meta':
            require(run.get('method_manifest') == produced and run.get('output_contract') == view['contract']['spec']['native_output'],
                'P3 must execute the exact H produced by the reviewed M run', 'GENERATION_PROVENANCE')
    if stage == 'reserve':
        require(run['run_id'] not in view['runs'], 'An existing construction reservation is not a new action', 'ALREADY_ATTEMPTED')
    else:
        stored = view['runs'].get(run['run_id'], {})
        require(run.get('status') == 'unknown' and run.get('intent_observation') == stored.get('intent_observation') and
            run.get('request_sha256') == stored.get('request_sha256') and
            isinstance(action, dict) and action.get('method') in ('thread/start', 'turn/start'),
            'Only the original unsent reservation may proceed to its exact native action', 'GUARD_DENIED')
        # The normal send guard also checks/records one method and exact ACK
        # ownership. Reject prior public send_attempted even if a caller tries
        # to reuse an ID after a lost parent acknowledgement.
        records = [json.loads(r[0]) for r in lab.state.db.execute(
            "SELECT body FROM observations WHERE asset='experiment-native-send' AND kind='note'")]
        require(not any(r.get('experiment') == name and r.get('run_id') == run['run_id'] and
            (r.get('action', {}).get('intentId') == action.get('intentId') or
             r.get('action', {}).get('method') in (action['method'], 'turn/start')) for r in records),
            'Native construction action already attempted; no resend', 'ALREADY_ATTEMPTED')
        paths = list(safe_path(lab.state.root, run['run_root']).glob('*/facts.jsonl'))
        require(len(paths) <= 1, 'Ambiguous original construction runtime facts', 'GUARD_DENIED')
        if paths:
            require(paths[0].stat().st_size <= 2 * 1024 * 1024, 'Construction runtime facts exceed read boundary', 'GUARD_DENIED')
            facts = [json.loads(line) for line in paths[0].read_text().splitlines()]
            require(not any(f.get('kind') == 'send_attempted' and f.get('method') == 'turn/start' for f in facts),
                'Original native turn already attempted; no resend', 'ALREADY_ATTEMPTED')
    return {'scope': SCOPE, 'test_scope_type': scope['test_scope_type'], 'step': index + 1,
        'experiment': name, 'run_id': run['run_id'], 'contract_sha256': expected['contract_sha256'], 'stage': stage}
