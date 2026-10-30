"""Current decision inputs and scoped admission; semantic choices belong to the reviewer.

No scheduler or independent store. This module consumes existing work, task,
judgment, input, review and runtime records. It never grants user authorization.
"""
from copy import deepcopy
import json

from .common import HarnessError, digest, local_path, require
from . import goals

VERSION = 'b04-v1'
OPERATIONS = ('dispatch', 'adopt')
TRIGGERS = ('opportunity', 'no_gain', 'common_obstacle', 'purpose_correction', 'session_mismatch')


def coordinator_write_scope(state, work_id):
    """Opt-in after an explicit handoff; existing runtime owners stay compatible."""
    if state.schema_version < 3:
        return None
    row = state.db.execute("SELECT payload FROM events WHERE work_id=? AND kind='coordinator_write_scope' ORDER BY seq DESC LIMIT 1", (work_id,)).fetchone()
    if not row:
        return None
    owner = state.db.execute('SELECT * FROM runtime_coordinators WHERE work_id=?', (work_id,)).fetchone()
    require(owner, 'Protected work has no coordinator', 'COORDINATOR_REQUIRED')
    return {**dict(owner), 'handoff': json.loads(row['payload'])}


def check_coordinator_write(state, work_id, actor, payload):
    """Collaboration consistency, not authentication or an authorization grant."""
    current = coordinator_write_scope(state, work_id)
    if current is None:
        return None
    require(isinstance(actor, dict) and actor.get('coordinator') == current['owner']
            and actor.get('owner_revision') == current['revision'],
            'Overall write must bind the current coordinator and owner revision', 'COORDINATOR_REQUIRED')
    require(isinstance(actor.get('writer_session'), str) and actor['writer_session'], 'Actual local writer session required')
    origin = actor.get('origin')
    require(isinstance(origin, dict) and origin.get('author') == current['owner']
            and origin.get('reference') and origin.get('provider')
            and origin.get('payload_sha256') == goals.fingerprint(payload),
            'Bind the coordinator message reference and exact overall payload', 'IDENTITY_MISMATCH')
    return {**deepcopy(actor), 'work_id': work_id,
            'coverage': 'Declared coordinator and exact payload checked against same-store ownership; not human or OS authentication'}


def _managed_work(state, work):
    if work.get('decision', {}).get('coordination', {}).get('format_version') == VERSION:
        return True
    tables = _tables(state)
    if 'runtime_coordinators' in tables and state.db.execute(
            'SELECT 1 FROM runtime_coordinators WHERE work_id=?', (work['id'],)).fetchone():
        return True
    return 'runtime_routes' in tables and state.db.execute(
        'SELECT 1 FROM runtime_routes r JOIN tasks t ON t.id=r.task_id WHERE t.work_id=?', (work['id'],)).fetchone() is not None


def _substantive(value):
    # Generated timestamps are not instructions. Source/predicate/identity data are.
    if isinstance(value, dict):
        return {k: _substantive(v) for k, v in value.items() if k not in ('generated_at', 'updated_at', 'created_at')}
    return [_substantive(v) for v in value] if isinstance(value, list) else value


def _requirement_shape(values):
    # Match bind_requirements/bind_claims semantic defaults before generated
    # revisions and evidence hashes. Otherwise a raw omitted default can make
    # an active saved authorization look superseded after binding.
    result = []
    for item in values or []:
        claims = [{**claim, 'required': claim.get('required', True),
                   'applicability': claim.get('applicability', 'required')} for claim in item.get('claims', [])]
        result.append({**{k: _substantive(v) for k, v in item.items() if k not in ('revision', 'completion', 'claims')},
                       'required': item.get('required', True), 'completion': goals.criteria(item.get('completion')),
                       'claims': goals.claim_criteria(claims)})
    return result


def _semantic_value(body, key):
    if key == 'completion':
        return goals.criteria(body.get(key))
    if key == 'requirements':
        return _requirement_shape(body.get(key))
    if key.startswith('representation.'):
        return _substantive(body.get('representation', {}).get(key.split('.', 1)[1]))
    return _substantive(body.get(key))


def change_preview(state, work_id, patch, *, task_id=None, change=None):
    """Read-only exact change identity; human reviewers still judge the quote's meaning.

    Call with the supplied patch before binding generated evidence hashes/revisions.
    Ordinary progress and evidence locations/hashes do not enter this identity.
    """
    require(isinstance(patch, dict), 'Change preview needs an object')
    work = state.get(work_id); task = state.task(task_id) if task_id else None
    require(not task or task['work_id'] == work_id, 'Change task belongs to another work')
    before = task['body'] if task else work
    changes = {}
    def include(key):
        after_body = {**before, **patch}
        old, new = _semantic_value(before, key), _semantic_value(after_body, key)
        if old != new:
            changes[key] = {'before': old, 'after': new}
    if not task:
        for key in ('request', 'objective', 'constraints', 'execution_policy', 'requirements'):
            if key in patch:
                include(key)
        representation = patch.get('representation')
        if isinstance(representation, dict):
            if representation.get('changes_use'):
                include('representation')
            for key in ('scientific_assumptions', 'execution_authorization', 'intended_use'):
                # An omitted old declaration is a removal, including clarification labels.
                if key in representation or key in before.get('representation', {}):
                    include('representation.' + key)
    if 'completion' in patch:
        old, new = before.get('completion'), patch['completion']
        first_evidence = (not old and isinstance(change, dict) and change.get('kind') == 'evidence'
            and change.get('purpose_basis') == before.get('purpose', before.get('objective'))
            and (new or {}).get('purpose') == change.get('purpose_basis'))
        if first_evidence:
            goals.check_change(change)
        if (not first_evidence and (old or {}).get('purpose') != (new or {}).get('purpose')) or goals.reduced_criteria(old, new):
            include('completion')
    statement = {'operation': 'task_completion' if task else 'work_update', 'work_id': work_id,
                 'base_revision': work['revision'], 'request_revision': work['request_revision'],
                 'task_id': task_id, 'changes': changes}
    managed = _managed_work(state, work)
    return {'format_version': VERSION, 'managed': managed, 'required': managed and bool(changes),
            'reasons': list(changes), 'statement': statement, 'mutation_sha256': goals.fingerprint(statement),
            'meaning': 'Exact proposed protected changes; quote applicability requires human-source semantic review'}


def _scope_identity(state, record):
    row = state.db.execute('SELECT relation,source,body FROM input_scopes WHERE input_id=? AND work_id=?',
                           (record['id'], record['scope_work_id'])).fetchone()
    require(row is not None, 'Authorization input scope is missing', 'INPUT_SCOPE_UNRESOLVED')
    return goals.fingerprint({'work_id': record['scope_work_id'], 'relation': row['relation'],
                              'source': row['source'], 'body': json.loads(row['body'])})


def _verify_authorization(state, work_id, authorization, *, receipt=False):
    require(isinstance(authorization, dict), 'Protected change needs exact human input authorization', 'AUTHORIZATION_REQUIRED')
    fields = ('input_id', 'input_sha256', 'assessment_revision', 'scope_revision', 'quote', 'mutation_sha256')
    require(all(key in authorization for key in fields), 'Authorization is missing exact input/scope/change fields', 'AUTHORIZATION_REQUIRED')
    require(all(type(authorization[key]) is int for key in ('input_id', 'assessment_revision', 'scope_revision'))
            and all(isinstance(authorization[key], str) and authorization[key].strip()
                    for key in ('input_sha256', 'quote', 'mutation_sha256')), 'Invalid authorization field types', 'AUTHORIZATION_INVALID')
    record = state._input_scope(authorization['input_id'], work_id)
    require(record['input_sha256'] == authorization['input_sha256'], 'Authorization input content differs', 'INPUT_IDENTITY_CONFLICT')
    assessment = state.input_assessment(record['id'])
    require(assessment['kind'] == 'human_direct' and assessment['status'] == 'reviewed'
            and assessment['revision'] == authorization['assessment_revision'],
            'Protected change needs the current verified human-origin input', 'PROVENANCE_UNVERIFIED')
    require(authorization['quote'] in record['prompt'], 'Authorization quote is outside the captured input', 'AUTHORIZATION_INVALID')
    acknowledged_only = (receipt and not authorization.get('scope_acknowledged') and record['scope_acknowledged']
                         and record['scope_revision'] == authorization['scope_revision'] + 1)
    require(record['scope_revision'] == authorization['scope_revision'] or acknowledged_only,
            'Authorization input scope changed', 'REVISION_CONFLICT')
    scope_sha = _scope_identity(state, record)
    if receipt:
        require(scope_sha == authorization.get('scope_sha256'), 'Authorization applicability changed', 'REVISION_CONFLICT')
    return record, assessment, scope_sha


def authorize_change(state, work_id, patch, change=None, *, task_id=None):
    """State write hook; reason/source/approval_source strings never grant authority."""
    preview = change_preview(state, work_id, patch, task_id=task_id, change=change)
    if not preview['required']:
        return None
    authorization = change.get('authorization') if isinstance(change, dict) else None
    record, assessment, scope_sha = _verify_authorization(state, work_id, authorization)
    require(authorization['mutation_sha256'] == preview['mutation_sha256'],
            'Approval is for a different work/revision/substantive change', 'AUTHORIZATION_MUTATION_MISMATCH')
    return {**{key: authorization[key] for key in ('input_id', 'input_sha256', 'assessment_revision', 'scope_revision', 'quote')},
            'mutation_sha256': preview['mutation_sha256'], 'statement': preview['statement'],
            'scope_sha256': scope_sha, 'scope_acknowledged': bool(record['scope_acknowledged']),
            'origin_reviewer': assessment['reviewer'], 'origin_source': assessment['source'],
            'coverage': 'Current reviewed human origin, exact explicit work scope and exact change binding; semantic approval remains reviewer judgment'}


def check_authorization_bindings(state, work, task=None):
    """Recheck effective protected changes only, without rewriting historical adoption."""
    checked, seen = [], set()
    scopes = [(work, None)] + ([(task['body'], task['id'])] if task else [])
    for body, bound_task in scopes:
        for key, receipt in body.get('change_authorizations', {}).items():
            statement = receipt.get('statement', {})
            require(statement.get('work_id') == work['id'] and statement.get('task_id') == bound_task
                    and key in statement.get('changes', {}) and receipt.get('mutation_sha256') == goals.fingerprint(statement),
                    'Stored authorization has an invalid scope', 'AUTHORIZATION_INVALID')
            if _semantic_value(body, key) != statement['changes'][key]['after']:
                continue  # Superseded declarations remain historical, not active authority.
            identity = receipt['mutation_sha256']
            if identity in seen:
                continue
            _verify_authorization(state, work['id'], receipt, receipt=True)
            seen.add(identity); checked.append({'mutation_sha256': identity, 'input_id': receipt['input_id']})
    return checked


def _tables(state):
    return {row[0] for row in state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _managed(state, task):
    return _managed_work(state, state.get(task['work_id']))


def _scope(task):
    return goals.fingerprint({'task': task['id'], 'work': task['work_id'], 'request_revision': task['work_revision'],
                              'body': {k: v for k, v in task['body'].items() if k != 'reviews'}})


def _current_tasks(state, work):
    return {task['id']: task for task in state.tasks(work['id']) if task['work_revision'] == work['request_revision']}


def _affected(tasks, roots):
    """Only actual requires edges propagate. Parent grouping never propagates."""
    result = set(roots)
    changed = True
    while changed:
        changed = False
        for identifier, task in tasks.items():
            if identifier not in result and result.intersection(task['body'].get('requires', task['body'].get('depends_on', []))):
                result.add(identifier); changed = True
    return sorted(result)


def validate_trigger(state, work_id, body, historical=False):
    """Optional State.judge write hook. Reject new malformed trigger records early."""
    spec = body.get('coordination')
    if spec is None:
        return None
    require(isinstance(spec, dict) and set(spec) <= {'trigger', 'affected', 'mode', 'operations'}
            and spec.get('trigger') in TRIGGERS, 'Invalid scoped coordination trigger', 'COORDINATION_INVALID')
    tasks = state.tasks(work_id); known = {task['id'] for task in tasks}
    current = {task['id'] for task in tasks if task['work_revision'] == state.get(work_id)['request_revision']}
    roots = spec.get('affected')
    require(isinstance(roots, list) and roots and all(isinstance(item, str) for item in roots)
            and len(roots) == len(set(roots)) and set(roots) <= (known if historical else current),
            'Coordination trigger needs exact applicable task identities', 'COORDINATION_INVALID')
    operations = spec.get('operations', list(OPERATIONS))
    require(isinstance(operations, list) and operations and all(isinstance(item, str) for item in operations)
            and len(operations) == len(set(operations)) and set(operations) <= set(OPERATIONS),
            'Coordination trigger has unsupported operation scope', 'COORDINATION_INVALID')
    require(spec.get('mode') in ('attention', 'reconsider'), 'Coordination trigger needs attention/reconsider mode', 'COORDINATION_INVALID')
    return {**deepcopy(spec), 'operations': operations}


def _triggers(state, work, tasks=None):
    tasks = tasks or _current_tasks(state, work)
    result = []
    for row in state.db.execute('SELECT id FROM judgments WHERE work_id=? ORDER BY id', (work['id'],)):
        judgment = state.judgment(row[0]); body = judgment['body']; spec = body.get('coordination')
        if not spec or judgment['status'] == 'retracted':
            continue
        invalid = None
        try:
            spec = validate_trigger(state, work['id'], body, historical=True)
        except HarnessError as error:
            # Corrupt/old records remain visible. Only defensibly identified task
            # roots are held; an invalid local note does not freeze the portfolio.
            invalid = str(error)
            raw_roots = spec.get('affected', []) if isinstance(spec, dict) else []
            spec = {'trigger': 'invalid_record', 'affected': [item for item in raw_roots if isinstance(item, str) and item in tasks] if isinstance(raw_roots, list) else [],
                    'mode': 'reconsider', 'operations': list(OPERATIONS)}
        roots = spec['affected']; operations = spec['operations']
        applicable = [item for item in roots if item in tasks]
        result.append({'id': judgment['id'], 'revision': judgment['revision'], 'status': judgment['status'],
                       'trigger': spec['trigger'], 'mode': spec['mode'], 'operations': operations,
                       'roots': roots, 'affected': _affected(tasks, applicable), 'claim': body['claim'],
                       'applicability': 'current_task_scope' if applicable else 'historical_or_unresolved_scope', 'invalid': invalid,
                       'scope': body['scope'], 'evidence': body['evidence'],
                       'coverage': body.get('coverage'), 'independent_actions': body.get('independent_actions', []),
                       'checked_recovery_routes': body.get('checked_recovery_routes', []),
                       'next_discriminator': body.get('next_discriminator'),
                       'meaning': 'Recorded reviewer judgment; no user authorization or causal truth inferred'})
    return result


def _resources(state, tasks):
    tables = _tables(state); policies, returns, usage = [], [], []
    if 'runtime_policies' in tables:
        policies = [{**dict(row), 'body': json.loads(row['body'])} for row in state.db.execute('SELECT * FROM runtime_policies ORDER BY id')]
    if 'runtime_review_queue' in tables:
        returns = [{key: row[key] for key in ('task_id', 'owner', 'revision', 'status', 'at')}
                   for row in state.db.execute('SELECT * FROM runtime_review_queue ORDER BY task_id') if row['task_id'] in tasks]
    if 'runtime_usage' in tables and 'runtime_actions' in tables:
        for row in state.db.execute('SELECT u.*,a.task_id FROM runtime_usage u JOIN runtime_actions a ON a.id=u.action_id ORDER BY u.rowid DESC LIMIT 100'):
            if row['task_id'] in tasks:
                usage.append({**dict(row), 'body': json.loads(row['body'])})
    return {'policies': policies, 'review_queue': returns, 'usage_records': usage,
            'coverage': 'Recorded policy/review/cost observations; usage may be cumulative or unknown, no fabricated price or scientific gain estimate'}


def _review_owner(state, task, resources):
    queue = next((item for item in resources['review_queue'] if item['task_id'] == task['id']), None)
    if queue:
        return {'owner': queue['owner'], 'review_status': queue['status'], 'source': 'runtime_review_queue'}
    rows = [row['session'] for row in state.db.execute("SELECT session FROM bindings WHERE work_id=? AND role='main' ORDER BY session", (task['work_id'],))]
    return {'owner': rows[0] if len(rows) == 1 else None, 'responsible_role': 'main',
            'source': 'single_recorded_main_binding' if len(rows) == 1 else 'owner_not_uniquely_recorded'}


def session_options(state, task, limit=12):
    """Recorded reuse candidates; no native activity or physical host claim."""
    require(type(limit) is int and 1 <= limit <= 100, 'Session option bound must be 1..100')
    bindings = [dict(row) for row in state.db.execute('SELECT * FROM bindings WHERE work_id=? ORDER BY session', (task['work_id'],))]
    recorded = task['body'].get('executor_session') or task['body'].get('producer')
    values = []
    for binding in bindings[:limit]:
        active = state.active_dispatches(binding['session'])
        values.append({**binding, 'same_recorded_executor': binding['session'] == recorded,
                       'active_dispatches': [{'key': item['key'], 'task_id': item['task_id'], 'status': item['status']} for item in active],
                       'new_scope_available': not active, 'native_status': 'not_rechecked'})
    route = None
    if 'runtime_routes' in _tables(state):
        row = state.db.execute('SELECT * FROM runtime_routes WHERE task_id=?', (task['id'],)).fetchone()
        if row:
            route = dict(row); route['body'] = json.loads(route['body'])
    return {'items': values, 'omitted': max(0, len(bindings) - limit), 'current_native_route': route,
            'meaning': 'Prefer suitable existing context; splitting benefit/independence is a semantic decision. UNKNOWN must be reconciled.'}


def decision_context(state, work_id, limit=12):
    """Bounded current planning projection; no scientific files or remote state recheck."""
    require(type(limit) is int and 1 <= limit <= 100, 'Decision context bound must be 1..100')
    work = state.get(work_id); tasks = _current_tasks(state, work); triggers = _triggers(state, work, tasks)
    judgments = [{'id': row['id'], 'revision': row['revision'], 'status': row['status'], 'body': row['body']}
                 for row in (state.judgment(row[0]) for row in state.db.execute('SELECT id FROM judgments WHERE work_id=? ORDER BY id', (work_id,)))]
    pending = state.pending(work_id); resources = _resources(state, tasks)
    event = state.db.execute('SELECT MAX(seq) FROM events WHERE work_id=?', (work_id,)).fetchone()[0]
    # Full identity snapshot is hashed, while the displayed projection is bounded.
    identity = {'work_revision': work['revision'], 'request_revision': work['request_revision'],
                'decision_revision': work.get('decision', {}).get('revision'), 'latest_event_seq': event,
                'tasks': [{'id': key, 'scope': _scope(task), 'status': task['status'],
                           'reviews': task['body'].get('reviews', [])} for key, task in sorted(tasks.items())],
                'judgments': judgments,
                'resources': resources,
                'bindings': [dict(row) for row in state.db.execute('SELECT * FROM bindings WHERE work_id=? ORDER BY session', (work_id,))],
                'pending_inputs': [{'id': item['id'], 'input_sha256': item.get('input_sha256'),
                                    'scope_revision': item.get('scope_revision'), 'source_kind': item.get('source_kind')} for item in pending]}
    candidates = []
    order = {'returned': 0, 'revise': 1, 'proposed': 2, 'adopted': 3, 'running': 4, 'dispatched': 4, 'reserved': 4}
    eligible = sorted(tasks.values(), key=lambda item: (order.get(item['status'], 9), item['id']))
    for task in eligible[:limit]:
        body = task['body']; reviews = body.get('reviews', [])
        requires = body.get('requires', body.get('depends_on', []))
        candidates.append({'id': task['id'], 'status': task['status'], 'purpose': body['purpose'],
                           'contributes_to': body['contributes_to'], 'requirement_versions': body['requirement_versions'],
                           'parent_task': body.get('parent_task'), 'requires': requires,
                           'dependency_states': [{'id': dependency, 'status': state.task(dependency)['status']} for dependency in requires],
                           'completion_purpose': body.get('completion', {}).get('purpose'),
                           'use_claims': body.get('use_claims', []),
                           'latest_review': reviews[-1] if reviews else None,
                           'sessions': session_options(state, task, limit=4), 'readiness': 'metadata_only_not_professional_recheck'})
    returns = [task for task in eligible if task['status'] in ('returned', 'revise')]
    from .portfolio import ancestry
    return {'format_version': VERSION, 'work': {key: work.get(key) for key in ('id', 'revision', 'request_revision', 'request', 'request_source', 'objective', 'constraints', 'status')},
            'requirements': deepcopy(work.get('requirements', [])), 'ancestry': ancestry(state, work),
            'current_decision': goals.decision_view(state, work), 'previous_outcomes': work.get('decision', {}).get('previous_outcome_comparison'),
            'pending_inputs': [{'id': item['id'], 'source_kind': item.get('source_kind', 'unknown'),
                                'source_status': item.get('source_status', 'unreviewed')} for item in pending],
            'candidates': candidates, 'omitted_candidates': max(0, len(eligible) - limit),
            'pending_returns': [{'task': task['id'], 'status': task['status'], **_review_owner(state, task, resources),
                                 'latest_review': task['body'].get('reviews', [])[-1:]} for task in returns[:limit]],
            'omitted_returns': max(0, len(returns) - limit), 'triggers': triggers,
            'resources': resources,
            'input_sha256': goals.fingerprint(identity),
            'semantic_questions': ['Which available result best advances the original use now?',
                                   'What is the opportunity cost of more local refinement?',
                                   'Which failures share tested tool/account/version conditions?',
                                   'Which existing session is suitable and where is independent review needed?'],
            'coverage': 'Current recorded identities, scoped triggers and opportunities; no semantic route selection, scientific recheck or Dot wakeup inferred.'}


def _verify_trigger_evidence(state, trigger):
    for ref in trigger['evidence']:
        if 'path' in ref:
            target = local_path(state.root, ref['path'])
            require(target.is_file() and ref.get('sha256') and digest(target) == ref['sha256'],
                    'Scoped coordination evidence changed: ' + trigger['id'], 'COORDINATION_EVIDENCE_CHANGED')


def prepare_decision(state, work, decision):
    """State.decide hook. Bind current inputs, not a fabricated strategy score."""
    check_authorization_bindings(state, work)
    value = deepcopy(decision); raw = value.get('coordination', {})
    require(isinstance(raw, dict), 'Coordination plan must be an object', 'COORDINATION_INVALID')
    allowed = {'input_sha256', 'reviewed_triggers', 'choices', 'session_choices'}
    require(set(raw) <= allowed, 'Pass new coordination choices, not generated receipt fields', 'COORDINATION_INVALID')
    tasks = _current_tasks(state, work); packet = decision_context(state, work['id'])
    previous = work.get('decision', {}).get('coordination', {})
    previous = previous if previous.get('format_version') == VERSION and previous.get('request_revision') == work['request_revision'] else {}
    reviewed = raw.get('reviewed_triggers', previous.get('reviewed_triggers', []))
    require(isinstance(reviewed, list), 'reviewed_triggers must be exact references', 'COORDINATION_INVALID')
    current = {item['id']: item for item in packet['triggers']}; bound = []
    for ref in reviewed:
        require(isinstance(ref, dict) and set(ref) == {'id', 'revision'}, 'Trigger reference needs exact id/revision', 'COORDINATION_INVALID')
        require(isinstance(ref['id'], str) and type(ref['revision']) is int and ref['revision'] > 0, 'Invalid trigger reference identity', 'COORDINATION_INVALID')
        if ref['id'] in current and current[ref['id']]['revision'] == ref['revision']:
            require(not current[ref['id']].get('invalid'), 'Repair the exact malformed scoped trigger before using it', 'COORDINATION_REQUIRED')
            require(current[ref['id']]['status'] == 'adopted', 'Resolve scoped judgment review before continuation', 'COORDINATION_REQUIRED')
            if current[ref['id']]['mode'] == 'reconsider' and ref not in previous.get('reviewed_triggers', []):
                require(raw.get('input_sha256') == packet['input_sha256'],
                        'Bind the actual current inputs before consuming a new scoped trigger', 'COORDINATION_REQUIRED')
            _verify_trigger_evidence(state, current[ref['id']]); bound.append(deepcopy(ref))
        elif 'reviewed_triggers' in raw:
            raise HarnessError('Coordination trigger reference is stale or retracted', 'COORDINATION_REQUIRED')
    require(len({ref['id'] for ref in bound}) == len(bound), 'Duplicate reviewed coordination trigger', 'COORDINATION_INVALID')
    # Keep stop/defer until explicitly revised. An old run basis for an unselected
    # task is historical, not the current coordinator's next-step judgment.
    selected_now = set(value.get('tasks', []))
    choices = {(item['task'], item['operations'][0]): deepcopy(item) for item in previous.get('choices', [])
        if item['action'] != 'run' or item['task'] in selected_now}
    incoming = raw.get('choices', [])
    require(isinstance(incoming, list), 'choices must be scoped records', 'COORDINATION_INVALID')
    seen = set()
    for choice in incoming:
        require(isinstance(choice, dict) and set(choice) <= {'task', 'action', 'operations', 'basis'}, 'Invalid task choice', 'COORDINATION_INVALID')
        target = choice.get('task'); operations = choice.get('operations', ['dispatch'])
        require(isinstance(target, str) and target in tasks and choice.get('action') in ('run', 'defer', 'stop'), 'Choice needs current task and run/defer/stop', 'COORDINATION_INVALID')
        require(isinstance(operations, list) and operations and all(isinstance(item, str) for item in operations)
                and len(operations) == len(set(operations)) and set(operations) <= set(OPERATIONS), 'Invalid choice operation scope', 'COORDINATION_INVALID')
        if choice['action'] != 'run':
            require(isinstance(choice.get('basis'), str) and choice['basis'].strip(), 'Explain the concrete defer/stop judgment', 'COORDINATION_INVALID')
        if choice['action'] == 'run' and 'dispatch' in operations:
            require(target in value.get('tasks', []), 'A run choice must be selected by the overall decision', 'COORDINATION_INVALID')
        for operation in operations:
            key = (target, operation); require(key not in seen, 'Overlapping task choices', 'COORDINATION_INVALID'); seen.add(key)
            choices[key] = {**deepcopy(choice), 'operations': [operation], 'scope_sha256': _scope(tasks[target]),
                            'request_revision': work['request_revision']}
    sessions = {item['task']: deepcopy(item) for item in previous.get('session_choices', []) if item['task'] in selected_now}
    incoming_sessions = raw.get('session_choices', [])
    require(isinstance(incoming_sessions, list), 'session_choices must be a list', 'COORDINATION_INVALID')
    seen_sessions = set()
    for choice in incoming_sessions:
        require(isinstance(choice, dict) and set(choice) <= {'task', 'mode', 'thread_id', 'basis'}, 'Invalid session choice', 'COORDINATION_INVALID')
        require(isinstance(choice.get('task'), str) and choice['task'] in tasks and choice.get('mode') in ('reuse', 'new', 'independent_review', 'handoff'), 'Session choice needs current task and mode', 'COORDINATION_INVALID')
        require(choice['task'] not in seen_sessions, 'Duplicate session choice', 'COORDINATION_INVALID'); seen_sessions.add(choice['task'])
        if choice['mode'] == 'reuse':
            require(choice.get('thread_id'), 'Reuse needs the exact existing session', 'COORDINATION_INVALID')
        else:
            require(isinstance(choice.get('basis'), str) and choice['basis'].strip(), 'New/independent/handoff session needs its concrete benefit or isolation basis', 'COORDINATION_INVALID')
        sessions[choice['task']] = {**deepcopy(choice), 'scope_sha256': _scope(tasks[choice['task']])}
    selected = set(value.get('tasks', []))
    reviewed_keys = {(item['id'], item['revision']) for item in bound}
    for trigger in packet['triggers']:
        if trigger['mode'] == 'reconsider' and 'dispatch' in trigger['operations'] and selected.intersection(trigger['affected']):
            require((trigger['id'], trigger['revision']) in reviewed_keys and trigger['status'] == 'adopted',
                    'Review the scoped coordination trigger before selecting affected work: ' + trigger['id'], 'COORDINATION_REQUIRED')
            if (trigger['id'], trigger['revision']) not in {(item['id'], item['revision']) for item in previous.get('reviewed_triggers', [])}:
                require(raw.get('input_sha256') == packet['input_sha256'], 'Bind the actual current planning inputs for this new reconsideration', 'COORDINATION_REQUIRED')
    for target in selected:
        choice = choices.get((target, 'dispatch'))
        if choice:
            require(choice['scope_sha256'] == _scope(tasks[target]), 'Refresh the changed choice scope explicitly', 'CHOICE_SCOPE_CHANGED')
            require(choice['action'] == 'run', 'Selected task remains explicitly ' + choice['action'], 'CHOICE_' + ('STOPPED' if choice['action'] == 'stop' else 'DEFERRED'))
    scopes = {target: _scope(task) for target, task in tasks.items()}
    value['coordination'] = {'format_version': VERSION, 'request_revision': work['request_revision'],
                             'input_sha256': packet['input_sha256'], 'input_explicitly_supplied': raw.get('input_sha256') == packet['input_sha256'],
                             'reviewed_triggers': bound, 'choices': list(choices.values()), 'session_choices': list(sessions.values()),
                             'task_scopes': scopes, 'coverage': 'Current recorded inputs bound; semantic route quality is reviewed separately; no user authorization granted'}
    return value


def check_admission(state, task, operation='dispatch', target=None, review=None, continuous=False):
    """Actual reserve/native guard/adoption hook; narrow to this operation's scope."""
    require(operation in OPERATIONS, 'Unknown coordination admission operation', 'COORDINATION_INVALID')
    work = goals.current_task(state, task); plan = work.get('decision', {}).get('coordination', {})
    require(work['status'] != 'paused', 'Work is explicitly paused', 'SCOPE_PAUSED')
    require(not state.blocking_inputs(work['id'], task=task['id'], operation=operation),
            'Reconcile input affecting this action', 'INPUT_ATTENTION')
    if not continuous:
        from .native_entry import NativeEntry, LOCAL_PROVIDER
        continuous = any(item['task_id'] == task['id'] and
            (item.get('origin') == 'observed' or item['provider'] == LOCAL_PROVIDER) for item in NativeEntry(state).actions(work['id']))
    authorization_bindings = check_authorization_bindings(state, work, task)
    triggers = [item for item in _triggers(state, work) if task['id'] in item['affected'] and operation in item['operations'] and item['mode'] == 'reconsider']
    managed = _managed(state, task)
    if plan.get('format_version') != VERSION:
        require((not managed or continuous) and not triggers, 'A current v4 planning decision is required for this managed/affected action', 'COORDINATION_REQUIRED')
        coverage = 'native_continuous_scope' if continuous else 'legacy_unconfigured_action; existing core validation remains required'
    else:
        require(plan.get('request_revision') == work['request_revision'], 'Coordination plan belongs to an old request', 'COORDINATION_REQUIRED')
        stamp = plan.get('task_scopes', {}).get(task['id'])
        require((stamp == _scope(task)) or ((not managed or continuous) and not triggers and not any(
                c['task'] == task['id'] for c in plan.get('choices', []) + plan.get('session_choices', []))),
                'Refresh the affected task/criterion choice scope', 'CHOICE_SCOPE_CHANGED')
        keys = {(item['id'], item['revision']) for item in plan.get('reviewed_triggers', [])}
        for trigger in triggers:
            require(not trigger.get('invalid') and trigger['status'] == 'adopted' and (trigger['id'], trigger['revision']) in keys,
                    'Current scoped judgment has not been consumed: ' + trigger['id'], 'COORDINATION_REQUIRED')
            _verify_trigger_evidence(state, trigger)
        for choice in plan.get('choices', []):
            if choice['task'] == task['id'] and operation in choice['operations']:
                require(choice['scope_sha256'] == _scope(task), 'Explicit choice scope changed', 'CHOICE_SCOPE_CHANGED')
                require(choice['action'] == 'run', 'This scoped operation is explicitly ' + choice['action'],
                        'CHOICE_' + ('STOPPED' if choice['action'] == 'stop' else 'DEFERRED'))
        for choice in plan.get('session_choices', []):
            if choice['task'] == task['id'] and operation == 'dispatch':
                require(choice['scope_sha256'] == _scope(task), 'Session choice scope changed', 'CHOICE_SCOPE_CHANGED')
                require(not choice.get('thread_id') or target is None or target == choice['thread_id'],
                        'Actual target differs from the reviewed session choice', 'SESSION_CHOICE_MISMATCH')
        coverage = 'v4 current task scope and exact declared triggers/choices'
    result = {'format_version': VERSION, 'task': task['id'], 'operation': operation,
              'request_revision': work['request_revision'], 'scope_sha256': _scope(task),
              'reviewed_triggers': [{'id': item['id'], 'revision': item['revision']} for item in triggers], 'coverage': coverage,
              'authorization_bindings': authorization_bindings}
    if operation == 'adopt':
        from .fulfillment import validate_adoption_use
        result['use'] = validate_adoption_use(state, task, review or {})
    return result
