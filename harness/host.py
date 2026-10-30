from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from .common import HarnessError, ValidationTimeout, encoded, require, now, remaining_timeout
from .context import check_evidence, hook_context
from .main_messages import check_main_message
from . import goals


def _coordination_messages(state):
    latest = {}
    for row in state.db.execute("SELECT seq,payload FROM events WHERE kind='coordination_message_state' ORDER BY seq"):
        value = json.loads(row['payload']); latest[value['id']] = {**value, 'event_seq': row['seq']}
    return list(latest.values())


def coordination_message_show(state, identifier):
    item = next((v for v in _coordination_messages(state) if v['id'] == identifier), None)
    require(item, 'Unknown coordination message')
    return item


def _coordination_snapshot(state, session, role='main'):
    binding = state.binding(session)
    require(binding and binding['role'] == role, 'Coordination requires the exact existing '+role+' binding', 'IDENTITY_MISMATCH')
    work = state.get(binding['work_id'])
    return {'binding': binding, 'work_revision': work['revision'],
            'pending_inputs': [{key: value.get(key) for key in ('id', 'scope_revision', 'input_sha256')} for value in state.pending(work['id'])]}


def _coordination_expected(request, key, snapshot):
    require(request.get(key) == {'id': snapshot['binding']['work_id'], 'revision': snapshot['work_revision'],
                                'binding_revision': snapshot['binding']['revision']},
            'Stale or different '+key+' work/binding revision', 'REVISION_CONFLICT')


def _coordination_origin(state, delegation, caller, message, author_work, target_work):
    """Check the exact saved agent intent. Its approved meaning remains a review judgment."""
    origin = delegation.get('origin')
    require(isinstance(origin, dict) and set(origin) == {'judgment_id', 'revision', 'body_sha256'},
            'Delegation needs an exact saved judgment revision and body hash')
    record = state.judgment(origin['judgment_id'])
    require(record['revision'] == origin['revision'] and goals.fingerprint(record['body']) == origin['body_sha256'],
            'Delegation source changed after its exact reference', 'REVISION_CONFLICT')
    require(record['status'] in ('proposed', 'adopted'), 'Delegation source is retracted or needs review')
    body = record['body']; provenance = body.get('source_provenance', {}); intent = body.get('intended_message', {})
    require(isinstance(provenance, dict) and isinstance(intent, dict) and isinstance(intent.get('provider'), str),
            'Saved delegation provenance and intended provider must be explicit objects/strings')
    require(record['work_id'] in (author_work, target_work) and provenance.get('kind') == 'agent_message'
            and provenance.get('author_thread') == delegation['author'] and provenance.get('local_writer_session') == caller
            and intent.get('provider', '').endswith('send_message_to_thread')
            and intent.get('params') == message and intent.get('sent') is False,
            'Saved agent intent must bind this exact author, local executor and unsent message', 'IDENTITY_MISMATCH')
    return {key: record[key] for key in ('id', 'work_id', 'revision', 'status')} | {'body_sha256': origin['body_sha256']}


def _save_coordination(state, item, source):
    value = {key: val for key, val in item.items() if key != 'event_seq'}
    state.event(item['target_snapshot']['binding']['work_id'], 'coordination_message_state', source, value,
                request_id=item['id'], session=item['caller'])
    return coordination_message_show(state, item['id'])


def prepare_coordination_message(state, request):
    """Single exact administrative message; never a scientific dispatch or human-origin assessment."""
    require(isinstance(request, dict) and request.get('id') and request.get('source') and request.get('caller'), 'Coordination id/caller/source required')
    message = request.get('message')
    require(isinstance(message, dict) and set(message) <= {'threadId', 'hostId', 'prompt'}
            and isinstance(message.get('threadId'), str) and message['threadId']
            and isinstance(message.get('prompt'), str) and message['prompt'].strip(),
            'Bind exact coordination message without model/thinking overrides')
    require(request['caller'] != message['threadId'], 'Coordination target must be another exact main session')
    purposes = request.get('purposes')
    require(isinstance(purposes, list) and purposes and len(set(purposes)) == len(purposes)
            and set(purposes) <= {'deployment_window', 'responsibility_handoff'}, 'Only approved deployment/handoff coordination purposes are supported')
    require(isinstance(request.get('target_scope'), str) and request['target_scope'].strip()
            and isinstance(request.get('authorization_reference'), str) and request['authorization_reference'].strip(),
            'Exact target scope and actual user-approved authorization source reference required')
    with state.transaction():
        check_main_message(state, message['threadId'])
        records = _coordination_messages(state)
        previous = next((v for v in records if v['id'] == request['id']), None)
        if previous:
            require(previous['request_sha256'] == goals.fingerprint(request), 'Coordination id already binds different content', 'IDENTITY_MISMATCH')
            return {'message': previous, 'send_once': False, 'meaning': 'Existing preparation; inspect actual claim/receipt rather than resend'}
        delegation = request.get('delegation')
        author = origin = None
        if delegation is not None:
            require(isinstance(delegation, dict) and set(delegation) == {'author', 'author_work', 'origin'}
                    and isinstance(delegation['author'], str) and delegation['author'] not in (request['caller'], message['threadId']),
                    'Delegation requires the distinct registered main author and exact source')
            caller = _coordination_snapshot(state, request['caller'], 'executor')
            author = _coordination_snapshot(state, delegation['author'])
            require(caller['binding']['work_id'] == author['binding']['work_id'],
                    'Administrative executor must belong to the author\'s exact management work', 'IDENTITY_MISMATCH')
            _coordination_expected(delegation, 'author_work', author)
        else:
            caller = _coordination_snapshot(state, request['caller'])
        target = _coordination_snapshot(state, message['threadId'])
        require(state.get(caller['binding']['work_id'])['status'] == 'active', 'Coordination caller must use its current active authorized work')
        for key, snapshot in (('caller_work', caller), ('target_work', target)):
            _coordination_expected(request, key, snapshot)
        if delegation is not None:
            origin = _coordination_origin(state, delegation, request['caller'], message,
                                          author['binding']['work_id'], target['binding']['work_id'])
            require(not any(v.get('delegation_origin', {}).get('id') == origin['id'] and v['status'] in ('claimed', 'unknown', 'sent') for v in records),
                    'This administrative intent was already consumed; a source revision/status change does not authorize resending', 'INTENT_ALREADY_USED')
        require(not any(v['caller'] == request['caller'] and v['target'] == message['threadId']
                        and v['status'] in ('prepared', 'claimed', 'unknown') for v in records),
                'Resolve existing prepared/claimed/unknown coordination message; do not reissue', 'ACTION_UNKNOWN')
        item = {'id': request['id'], 'caller': request['caller'], 'target': message['threadId'], 'status': 'prepared',
                'message': message, 'message_sha256': goals.fingerprint(message), 'request_sha256': goals.fingerprint(request),
                'purposes': purposes, 'target_scope': request['target_scope'], 'authorization_reference': request['authorization_reference'],
                'caller_snapshot': caller, 'target_snapshot': target, 'prepared_at': now(),
                'authorization_coverage': 'Declared source reference for reviewer verification; no human origin, scientific permission or handoff ACK inferred'}
        if delegation is not None:
            item.update(delegation=delegation, author_snapshot=author, delegation_origin=origin,
                delegation_coverage='Approved agent author/executor relationship declared by the saved intent; actual caller is checked only by the real PreToolUse. No cloud/local identity mapping or human authentication inferred.')
        return {'message': _save_coordination(state, item, request['source']), 'send_once': True}


def _matching_coordination(state, caller, target, message):
    checksum = goals.fingerprint(message)
    return [v for v in _coordination_messages(state) if v['caller'] == caller and v['target'] == target
            and v['message_sha256'] == checksum]


def claim_coordination_message(state, item, caller, target, message, correlation, evaluation_id):
    """Called only inside the real PreToolUse transaction; one preparation has one claim."""
    check_main_message(state, target)
    current = coordination_message_show(state, item['id'])
    require(isinstance(correlation, str) and correlation, 'Coordination requires the actual supplied host call identity', 'IDENTITY_MISMATCH')
    require(current['status'] == 'prepared', 'Coordination already claimed or observed; reconcile before any resend', 'INTENT_ALREADY_USED')
    require(current['caller'] == caller and current['target'] == target and current['message_sha256'] == goals.fingerprint(message),
            'Actual coordination caller/target/message differs', 'IDENTITY_MISMATCH')
    delegation = current.get('delegation')
    if delegation:
        require(not any(value['id'] != current['id'] and value.get('delegation_origin', {}).get('id') == current['delegation_origin']['id']
                        and value['status'] in ('claimed', 'unknown', 'sent') for value in _coordination_messages(state)),
                'This intent was consumed after preparation; reconcile without another send', 'INTENT_ALREADY_USED')
    require(_coordination_snapshot(state, caller, 'executor' if delegation else 'main') == current['caller_snapshot'] and
            _coordination_snapshot(state, target) == current['target_snapshot'],
            'Coordination work, binding or pending input changed after preparation', 'REVISION_CONFLICT')
    if delegation:
        require(_coordination_snapshot(state, delegation['author']) == current['author_snapshot'],
                'Coordination author work, binding or pending input changed after preparation', 'REVISION_CONFLICT')
        require(_coordination_origin(state, delegation, caller, message, current['author_snapshot']['binding']['work_id'],
                                     current['target_snapshot']['binding']['work_id']) == current['delegation_origin'],
                'Coordination source changed after preparation', 'REVISION_CONFLICT')
    current.update(status='claimed', host_call_id=correlation, guard_evaluation_id=evaluation_id, claimed_at=now())
    return _save_coordination(state, current, 'actual PreToolUse coordination claim')


def _coordination_post(state, caller, target, message, correlation, response, receipt):
    matches = _matching_coordination(state, caller, target, message)
    claimed = [v for v in matches if v['status'] != 'prepared' and correlation and v.get('host_call_id') == correlation]
    if not matches:
        return False
    with state.transaction():
        if len(claimed) != 1:
            state.event(state.binding(target)['work_id'], 'coordination_message_receipt_unmatched', 'actual PostToolUse',
                        {**receipt, 'meaning': 'Observed tool return cannot establish a missing/ambiguous coordination claim'})
            prepared = [v for v in matches if v['status'] == 'prepared']
            if len(prepared) == 1:
                unverified = prepared[0]
                unverified.update(status='unknown', unverified_tool_response=response, post_host_call_id=correlation,
                                  send_coverage='Tool return observed without a matching actual claim; quarantine, never infer guard success or resend')
                _save_coordination(state, unverified, 'unmatched actual PostToolUse coordination observation')
            return True
        item = claimed[0]; outcome = 'unknown' if response is None or response.get('isError', False) else 'sent'
        checksum = goals.fingerprint({'host_call_id': correlation, 'response': response})
        if item.get('receipt_sha256') == checksum:
            return True
        if item.get('receipt_sha256') and not (item['status'] == 'unknown' and outcome == 'sent'):
            state.event(item['target_snapshot']['binding']['work_id'], 'coordination_message_receipt_unmatched', 'actual PostToolUse',
                        {**receipt, 'id': item['id'], 'meaning': 'Different repeated observation requires reconciliation; original send record preserved'})
            return True
        rejected = any(value.get('status') in ('rejected', 'failed') for value in _coordination_guards(state, item))
        item.update(status='unknown' if item.get('native_conflicts') or rejected else outcome,
                    post_outcome=outcome, receipt_sha256=checksum, observed_at=now(),
                    tool_response=response, post_host_call_id=correlation,
                    send_coverage='Actual tool-return observation only; recipient adoption and natural-boundary handoff ACK remain separate')
        _save_coordination(state, item, 'actual PostToolUse coordination observation')
    return True


def _coordination_guards(state, item):
    first = state.db.execute("SELECT MIN(seq) FROM events WHERE kind='coordination_message_state' AND json_extract(payload,'$.id')=?", (item['id'],)).fetchone()[0]
    return [{'event_seq': row['seq'], **json.loads(row['payload'])} for row in state.db.execute(
        "SELECT seq,payload FROM events WHERE seq>? AND kind='guard_evaluated' AND json_extract(payload,'$.target')=? AND json_extract(payload,'$.message_sha256')=? ORDER BY seq",
        (first, item['target'], item['message_sha256']))]


def _coordination_observation_context(state, item):
    snapshots = {}; prepared_works = {}; changes = []
    sessions = {'caller': item['caller'], 'target': item['target']}
    if item.get('delegation'): sessions['author'] = item['delegation']['author']
    for role, session in sessions.items():
        binding = state.binding(session)
        current = {'binding': binding, 'work_revision': None, 'pending_inputs': []}
        if binding:
            current.update(work_revision=state.get(binding['work_id'])['revision'],
                pending_inputs=[{key: value.get(key) for key in ('id', 'scope_revision', 'input_sha256')}
                                for value in state.pending(binding['work_id'])])
        snapshots[role] = current
        original = item[role+'_snapshot']; work = state.get(original['binding']['work_id'])
        prepared_works[role] = {'work_id': work['id'], 'work_revision': work['revision'],
            'pending_inputs': [{key: value.get(key) for key in ('id', 'scope_revision', 'input_sha256')} for value in state.pending(work['id'])]}
        if current != original or work['revision'] != original['work_revision'] or prepared_works[role]['pending_inputs'] != original['pending_inputs']:
            changes.append(role)
    if item.get('delegation_origin'):
        old = item['delegation_origin']; row = state.db.execute('SELECT * FROM judgments WHERE id=?', (old['id'],)).fetchone()
        current = ({key: row[key] for key in ('id', 'work_id', 'revision', 'status')} |
                   {'body_sha256': goals.fingerprint(json.loads(row['body']))}) if row else None
        snapshots['delegation_origin'] = current
        if current != old: changes.append('delegation_origin')
    return {'snapshots_at_observation': snapshots, 'prepared_work_states_at_observation': prepared_works, 'changed_since_prepare': changes,
            'coverage': 'Observation-time state only; does not establish versions at the actual native send'}


def coordination_message_fact(state, request):
    """Record existing native facts without manufacturing a hook claim or authorizing a send."""
    from .native_entry import native_projection
    from .runtime import TERMINAL
    require(isinstance(request, dict) and request.get('id') and request.get('source'), 'Coordination id/source required')
    facts = request.get('receipts', [request.get('receipt')])
    require(isinstance(facts, list) and 1 <= len(facts) <= 8, 'Supply one to eight exact native receipts')
    saved = []
    for fact in facts:
        require(isinstance(fact, dict) and fact.get('id') and fact.get('reference') and fact.get('observed_at')
                and fact.get('kind') in ('send', 'read', 'notification', 'unknown')
                and isinstance(fact.get('provider'), str) and fact['provider'], 'Exact native receipt identity/kind/provider/reference/time required')
        datetime.fromisoformat(fact['observed_at'].replace('Z', '+00:00'))
        require(fact.get('id_kind', 'local_observation') in ('native', 'local_observation'), 'Declare native/local observation identity accurately')
        with state.transaction():
            item = coordination_message_show(state, request['id'])
            old = state.db.execute("SELECT payload FROM events WHERE kind='coordination_message_receipt' AND json_extract(payload,'$.receipt.id')=?", (fact['id'],)).fetchone()
            if old:
                prior = json.loads(old['payload'])
                require(prior['id'] == item['id'] and prior['receipt'] == fact, 'Native receipt id already binds different content', 'IDENTITY_MISMATCH')
                saved.append({'id': fact['id'], 'duplicate': True}); continue
            raw = fact.get('raw', {}); require(isinstance(raw, dict), 'Keep the actual raw native tool envelope')
            conflicts = item.setdefault('native_conflicts', [])
            try:
                data = native_projection(raw, item['target'])
            except HarnessError as exc:
                data = {}; conflicts.append({'receipt_id': fact['id'], 'reason': str(exc)})
            latest = data.get('latestTurn') if isinstance(data.get('latestTurn'), dict) else {}
            thread = data.get('threadId', data.get('childThreadId')); turn = data.get('turnId', data.get('childTurnId', latest.get('id')))
            terminal = data.get('terminalStatus', latest.get('status'))
            valid_ids = all(value is None or isinstance(value, str) and value for value in (thread, turn))
            target_matches = valid_ids and thread == item['target']
            if not valid_ids or thread and not target_matches:
                conflicts.append({'receipt_id': fact['id'], 'reason': 'Native target/identity conflicts with the prepared message'})
            successful = not raw.get('isError', False) and fact['kind'] != 'unknown'
            send = fact['kind'] == 'send'
            exact_send = (send and fact['provider'].endswith(('send_message_to_thread', 'cloud_threads.send_message'))
                          and fact.get('request_sha256') == item['message_sha256'] and fact.get('params') == item['message'])
            if send and not exact_send:
                conflicts.append({'receipt_id': fact['id'], 'reason': 'Native send observation differs from the exact prepared tool/message'})
            if exact_send and successful and target_matches: item['native_send_observed'] = True
            messages = data.get('items') if isinstance(data.get('items'), list) else []
            prompt_match = any(isinstance(message, dict) and message.get('role') == 'user' and message.get('turnId') == turn
                               and message.get('text') == item['message']['prompt'] and message.get('truncated') is False for message in messages)
            assessment = fact.get('correlation', {})
            assessed = (isinstance(assessment, dict) and assessment.get('reference') and assessment.get('basis') and assessment.get('reviewer')
                        and assessment.get('request_sha256') == item['message_sha256'] and assessment.get('thread_id') == thread
                        and assessment.get('turn_id') == turn)
            if isinstance(assessment, dict) and any(key in assessment and assessment[key] != expected for key, expected in
                    (('request_sha256', item['message_sha256']), ('thread_id', thread), ('turn_id', turn))):
                conflicts.append({'receipt_id': fact['id'], 'reason': 'Correlation assessment differs from the exact observed send/target/turn'})
            if target_matches and turn and successful:
                existing = item.get('native_turn_id')
                if existing and existing != turn:
                    conflicts.append({'receipt_id': fact['id'], 'reason': 'Native observation belongs to a different turn'})
                elif existing or exact_send or prompt_match or assessed:
                    item['native_turn_id'] = turn
                    if prompt_match: item['native_received_prompt_observed'] = True
                    if not existing:
                        item['turn_binding_coverage'] = ('Exact received prompt observed' if prompt_match else
                            'Exact send acknowledgement supplied turn' if exact_send else
                            'Declared correlation assessment with actual native target/turn checked; original received prompt not mechanically verified')
                    if terminal in TERMINAL:
                        item.setdefault('native_terminal_observations', []).append({'status': terminal, 'receipt_id': fact['id']})
                        if item.get('native_terminal_status') and item['native_terminal_status'] != terminal:
                            conflicts.append({'receipt_id': fact['id'], 'reason': 'Conflicting native terminal observations'})
                        else: item['native_terminal_status'] = terminal
            guard_observations = _coordination_guards(state, item)
            denied = any(value.get('status') in ('rejected', 'failed') for value in guard_observations)
            context = _coordination_observation_context(state, item)
            item['native_observation_context'] = context
            item['handoff_review_required'] = bool(context['changed_since_prepare'] or conflicts or denied or 'responsibility_handoff' in item['purposes'])
            proven = bool(item.get('native_turn_id') and (item.get('native_send_observed') or item.get('native_received_prompt_observed')))
            item['status'] = 'sent' if proven and not conflicts and not denied else 'unknown'
            item['native_coverage'] = {'guard': 'actual_rejection_observed' if denied else 'actual_records_observed' if guard_observations else 'not_observed',
                'guard_observations': guard_observations, 'actual_caller': 'Not established by explicit native observation',
                'meaning': 'Native send/turn/terminal facts only. No claim, host-call id, PrePassed, safe-boundary ACK, owner transfer or scientific adoption inferred.'}
            seq = state.event(item['target_snapshot']['binding']['work_id'], 'coordination_message_receipt', request['source'],
                {'id': item['id'], 'receipt': fact, 'observation_context': context})
            item.setdefault('native_receipts', []).append(seq)
            item['missing_native_fields'] = ([] if item.get('native_turn_id') else ['exact_turn_correlation'])
            _save_coordination(state, item, request['source']); saved.append({'id': fact['id'], 'duplicate': False})
    return {'message': coordination_message_show(state, request['id']), 'receipts': saved, 'send_once': False}


def claim_dispatch(state, target, caller, message=None):
    """Consume a prepared dispatch once. This guards only this explicit managed path."""
    with state.transaction():
        check_main_message(state, target)
        binding = state.binding(target)
        if not binding:
            return None
        work = binding['work_id']
        current_work = state.get(work)
        caller_binding = state.binding(caller)
        rows = state.db.execute("SELECT dispatches.* FROM dispatches JOIN tasks ON dispatches.task_id=tasks.id WHERE dispatches.target=? AND dispatches.status='prepared' AND tasks.work_id=? AND tasks.work_revision=?", (target, work, current_work['request_revision'])).fetchall()
        require(len(rows) == 1,
                "Managed target needs exactly one current prepared dispatch. Inspect native-entry recover/show and dispatch state first; "
                "reconcile any unknown/unconfirmed send from actual receipts before preparing or retrying. "
                "For ordinary scientific review feedback or continuation confirmed unsent, use "
                "python3 scripts/harness.py native-entry prepare --file REQUEST.json with provider=codex.desktop, "
                "method=send_message and the exact work/task/params, then call mcp__codex_app__send_message_to_thread "
                "with those same native params. See harness/README.md; coordination-message is for management handoffs to main sessions.")
        item = rows[0]
        task = state.task(item["task_id"])
        check = state.check_work(work, task=task['id'])
        require(check["allowed"], "; ".join(check["reasons"]))
        require(caller_binding and caller_binding['role'] == 'main' and caller_binding['work_id'] == work, 'Managed dispatch requires the bound main reviewer of this work')
        plan = state.dispatch_plan(item['key'])
        require(isinstance(message, dict) and goals.fingerprint(message) == plan['message_sha256'], 'Actual dispatch differs from the prepared prompt/model/target; reprepare explicitly')
        goals.ready(state, task)
        state.check_decision(task)
        from .coordination import check_admission
        coordination = check_admission(state, task, operation='dispatch', target=target)
        if state.schema_version >= 3:
            from .runtime import Runtime
            from .native_inputs import NativeInputs
            route = Runtime(state).route(task['id'], True)
            if route:
                require(route['thread_id'] == target, 'Actual host send differs from the mapped native thread', 'IDENTITY_MISMATCH')
                require(not message.get('hostId') or message['hostId'] == route['host_id'], 'Actual host send differs from the mapped native host', 'IDENTITY_MISMATCH')
                NativeInputs(state).assert_ready(route, task)
        active = state.active_dispatches(target, item['key'])
        if plan['intent'] == 'new':
            require(not active, 'Another execution became active after reservation')
        else:
            require(len(active) == 1 and active[0]['key'] == plan['relates_to'] and active[0]['status'] in ('sent', 'started'), 'Related dispatch changed before amend/replace')
        goals.judgment_dependencies(state, task, strict=True)
        state.db.execute("UPDATE dispatches SET status='claimed' WHERE key=?", (item["key"],))
        state.event(work, "dispatch_claimed", "PreToolUse", {"key": item["key"], "target": target,
            'coordination_result': coordination})
        return item["key"]


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _message(event):
    message = _object(event.get('tool_input'))
    require(message is not None, 'Controlled message arguments must be a JSON object or JSON object string')
    require(set(message) <= {'threadId', 'hostId', 'prompt', 'model', 'thinking'}, 'Unsupported controlled message argument fields')
    require(isinstance(message.get('threadId'), str) and bool(message['threadId']), 'Controlled message needs a target threadId')
    require(isinstance(message.get('prompt'), str) and bool(message['prompt'].strip()), 'Controlled message needs a nonempty prompt')
    return message


def _prepared(state, target, message):
    rows = state.db.execute("SELECT key FROM dispatches WHERE target=? AND status IN ('prepared','claimed')", (target,)).fetchall()
    return [r[0] for r in rows if state.dispatch_plan(r[0]).get('message_sha256') == goals.fingerprint(message)]


def _ordinary_prepared(state, target, message):
    from .native_entry import NativeEntry, LOCAL_PROVIDER
    return [item for item in NativeEntry(state).actions() if item['provider'] == LOCAL_PROVIDER
            and item['method'] == 'send_message' and item.get('thread_id') == target
            and item['params'] == message and item['phase'] in ('prepared', 'unconfirmed')]


def _guard_record(state, work, receipt, key=None):
    with state.transaction():
        if key:
            plan = state.dispatch_plan(key)
            plan['guard_evaluation'] = receipt
            state.db.execute('UPDATE dispatch_plans SET body=? WHERE key=?', (encoded(plan), key))
        state.event(work, 'guard_evaluated', 'actual host hook invocation', receipt)


def handle_hook(state, event, root=None):
    started = time.monotonic()
    name = event.get('hook_event_name', '')
    session = event.get('session_id', '')
    tool = event.get('tool_name', '')
    with state.transaction():
        binding = state.binding(session)
        work = binding['work_id'] if binding else None
        receipt_seq = state.event(work, 'hook_observed', 'host hook protocol', {'event': name, 'session': session,
                    'turn': event.get('turn_id'), 'tool': tool, 'source': event.get('source')},
                    session=session, request_id=event.get('request_id'),
                    external_event_id=event.get('event_id'), occurred_at=event.get('occurred_at'), origin=event.get('source'))
        if name == 'UserPromptSubmit':
            state.input(session, event.get('turn_id', ''), event.get('prompt', ''), raw_source=event.get('source'),
                        receipt_event_seq=receipt_seq, occurred_at=event.get('occurred_at'))
    if name in ('SessionStart', 'UserPromptSubmit'):
        context = hook_context(state, session)
        with state.transaction():
            state.event(work, 'hook_context_emitted', 'hook handler', {'event': name, 'session': session,
                'turn': event.get('turn_id'), 'bytes': len(context.encode('utf-8')), 'sha256': goals.fingerprint(context),
                'format': 'goal-context-v3', 'scope': 'Emission observed; display and semantic use are separate'})
        return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': context}}
    from .submission import guard as submission_guard, observe_hook as submission_observe_hook
    submission_observe_hook(state, event)
    submission_result = submission_guard(state, event)
    if submission_result is not None:
        return submission_result
    if not tool.endswith('send_message_to_thread') or name not in ('PreToolUse', 'PostToolUse'):
        return {}
    raw = _object(event.get('tool_input'))
    target = raw.get('threadId') if raw and isinstance(raw.get('threadId'), str) else None
    target_binding = state.binding(target) if target else None
    ordinary = _ordinary_prepared(state, target, raw) if target else []
    if not target_binding and not ordinary and (target or not binding):
        return {}  # The harness has no authority over an unbound destination.
    work = ordinary[0]['work_id'] if ordinary else target_binding['work_id'] if target_binding else work
    correlation = event.get('tool_use_id') or event.get('call_id')
    receipt = {'evaluation_id': str(uuid.uuid4()), 'host_call_id': correlation,
        'host_call_id_status': 'supplied' if correlation else 'not_supplied',
        'event': name, 'caller': session, 'target': target, 'at': now(),
        'input_shape': type(event.get('tool_input')).__name__,
        'known_argument_fields': sorted(set(raw or {}) & {'threadId','hostId','prompt','model','thinking'}),
        'message_sha256': goals.fingerprint(raw) if raw else None,
        'basis': 'Actual handler invocation; host trust and future coverage are separate'}
    if name == 'PreToolUse':
        with state.transaction():
            state.event(work, 'guard_started', 'actual host hook invocation', receipt)
        key = None
        coordination_message = None
        native_entry = None
        try:
            message = _message(event)
            require(target_binding is not None or ordinary, 'Cannot determine a managed destination from these arguments')
            check_main_message(state, target)
            administrative = _matching_coordination(state, session, target, message) if target_binding else []
            reporting = (binding and target_binding and binding['role'] == 'executor' and target_binding['role'] == 'main'
                         and binding['work_id'] == work and not administrative)
            if ordinary:
                require(len(ordinary) == 1, 'Ambiguous ordinary native preparation', 'ACTION_UNKNOWN')
                native_entry = ordinary[0]
                receipt.update(route='ordinary_native', native_entry_id=native_entry['id'])
            elif reporting:
                require('model' not in message and 'thinking' not in message, 'Executor reports cannot override main reviewer model settings')
                receipt['route'] = 'executor_report_to_bound_main'
            else:
                if administrative:
                    prepared = [item for item in administrative if item['status'] == 'prepared']
                    require(len(prepared) == 1, 'Coordination message was already claimed/observed; inspect before retrying', 'INTENT_ALREADY_USED')
                    coordination_message = prepared[0]
                    receipt.update(route='prepared_delegated_coordination' if coordination_message.get('delegation') else 'prepared_main_coordination',
                                   coordination_message_id=coordination_message['id'])
                else:
                    receipt['route'] = 'managed_dispatch'
                    matches = _prepared(state, target, message)
                    key = matches[0] if len(matches) == 1 else None
            # Source invalidation is a durable fact even when dispatch is then rejected.
            if not reporting and not coordination_message and root:
                check_evidence(root, state, work)
            # Claim and passed receipt commit together. Budget failure rolls both back.
            with state.transaction():
                check_main_message(state, target)
                if native_entry:
                    from .native_entry import NativeEntry
                    manager = NativeEntry(state)
                    item = manager.get(native_entry['id'])
                    require(item['phase'] == 'prepared', 'Native action was already claimed; reconcile its actual receipt', 'ACTION_UNKNOWN')
                    require(not item.get('coordinator') or session == item['coordinator'], 'Native sender differs from current coordinator', 'COORDINATOR_REQUIRED')
                    manager._current(item, target); manager._conflicts(item)
                    item.update(phase='unconfirmed', native_guard={'caller': session, 'host_call_id': correlation,
                        'evaluation_id': receipt['evaluation_id']})
                    manager._save(item, 'actual ordinary native pre-tool claim')
                elif coordination_message:
                    claim_coordination_message(state, coordination_message, session, target, message, correlation, receipt['evaluation_id'])
                elif not reporting:
                    key = claim_dispatch(state, target, session, message)
                remaining_timeout(1)
                receipt.update(status='passed', elapsed_ms=round((time.monotonic()-started)*1000, 2))
                _guard_record(state, work, receipt, key)
            return {}
        except Exception as exc:
            status = 'rejected' if isinstance(exc, HarnessError) and not isinstance(exc, ValidationTimeout) else 'failed'
            reason = str(exc) if isinstance(exc, HarnessError) else 'Guard execution failed: ' + type(exc).__name__
            receipt.update(status=status, reason=reason, error_type=type(exc).__name__, elapsed_ms=round((time.monotonic()-started)*1000, 2))
            if coordination_message and status == 'rejected' and getattr(exc, 'code', None) == 'REVISION_CONFLICT':
                with state.transaction():
                    stale = coordination_message_show(state, coordination_message['id'])
                    if stale['status'] == 'prepared':
                        stale.update(status='invalidated', invalidated_by=receipt['evaluation_id'],
                            invalidation_coverage='Actual PreToolUse rejected stale work/binding/input before any claim or native send; prepare a fresh id after review')
                        _save_coordination(state, stale, 'actual stale coordination preparation rejected')
            _guard_record(state, work, receipt, key)
            return {'hookSpecificOutput': {'hookEventName': name, 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}
    try:
        message = _message(event)
    except HarnessError:
        receipt.update(status='unverified', reason='PostToolUse arguments could not be normalized', elapsed_ms=round((time.monotonic()-started)*1000, 2))
        _guard_record(state, work, receipt)
        return {'systemMessage': receipt['reason']}
    response = _object(event.get('tool_response'))
    outcome = 'unknown' if response is None or response.get('isError', False) else 'sent'
    if ordinary:
        matches = [item for item in ordinary if item.get('native_guard', {}).get('caller') == session
                   and item.get('native_guard', {}).get('host_call_id') == correlation]
        if len(matches) == 1:
            from .native_entry import NativeEntry
            NativeEntry(state).fact({'id': matches[0]['id'], 'source': 'actual ordinary native post-tool receipt',
                'receipt': {'id': 'native-hook:' + str(correlation or receipt['evaluation_id']), 'kind': 'send' if response else 'unknown',
                    'reference': 'actual PostToolUse ' + str(correlation or receipt['evaluation_id']), 'observed_at': receipt['at'],
                    'request_sha256': matches[0]['request_sha256'], 'raw': response or {}}})
            receipt.update(route='ordinary_native', native_entry_id=matches[0]['id'], status='observed')
            _guard_record(state, work, receipt)
            return {}
    if _coordination_post(state, session, target, message, correlation, response, receipt):
        return {}
    reporting = binding and binding['role'] == 'executor' and target_binding and target_binding['role'] == 'main' and binding['work_id'] == work
    if reporting:
        with state.transaction():
            state.event(work, 'executor_report_' + outcome, 'PostToolUse', {**receipt, 'status': outcome,
                'meaning': 'Observed report delivery only; no dispatch or task adoption inferred'})
        return {}
    matches = _prepared(state, target, message)
    if len(matches) == 1:
        state.settle(matches[0], outcome, {'observed': 'actual PostToolUse response', 'host_call_id': correlation,
            'message_sha256': goals.fingerprint(message), 'requires_readback': True,
            'meaning': 'Tool return does not prove recipient startup or guard success'}, 'PostToolUse')
    else:
        with state.transaction():
            state.event(work, 'dispatch_receipt_unmatched', 'PostToolUse', {**receipt, 'status': outcome,
                'matching_prepared_dispatches': len(matches), 'guard_status': 'unverified'})
    return {}


def fallback_hook(directory, event, error):
    """State failures block only managed sends; ordinary reads remain available."""
    try:
        snapshot = json.loads((directory / 'state-snapshot.json').read_text())
    except (OSError, ValueError):
        snapshot = {}
    raw = _object(event.get('tool_input'))
    target = raw.get('threadId') if raw else None
    known = any(b['session'] == target for b in snapshot.get('bindings', []))
    caller_known = any(b['session'] == event.get('session_id') for b in snapshot.get('bindings', []))
    from .submission import browser_action
    controlled = (event.get('tool_name', '').endswith('send_message_to_thread') and (known or (not target and caller_known))) or (caller_known and browser_action(event))
    message = 'HTAM state/guard unavailable (' + type(error).__name__ + '); this action is unverified'
    if event.get('hook_event_name') == 'PreToolUse' and controlled:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': message}}
    return {'systemMessage': message + '; ordinary reads remain available'}
