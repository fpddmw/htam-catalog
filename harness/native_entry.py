"""Ordinary native admission; no transport or fabricated runtime ownership.

Native envelopes live in State.events. Core dispatches, policy, inputs, reviews
and decisions remain authoritative; no extra database or schema is needed.
"""
from copy import deepcopy
from collections import Counter
from datetime import datetime
import json
from xml.etree import ElementTree

from .common import HarnessError, encoded, now, require
from .main_messages import check_main_message
from . import goals
from .runtime import Runtime, LIVE, TERMINAL, native_hash

FORMAT = 'htam.native-entry/v1'
PROVIDER = 'dot.cloud_threads'
LOCAL_PROVIDER = 'codex.desktop'
ACTIVE = {'prepared', 'unconfirmed', 'sent', 'accepted', 'returned'}


def _desktop_delegation(message, turn_id):
    """Only the observed native cross-thread delivery envelope is a message."""
    if (message.get('type') != 'functionCallOutput'
            or message.get('namespace') != 'codex_app'
            or message.get('name') not in {'send_message_to_thread', 'create_thread'}):
        return None
    output = message.get('output')
    text = output.get('text') if isinstance(output, dict) else None
    if not isinstance(text, str) or '<!DOCTYPE' in text or '<!ENTITY' in text:
        return None
    try:
        envelope = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    children = list(envelope)
    if (envelope.tag != 'codex_delegation' or envelope.attrib or (envelope.text or '').strip()
            or [child.tag for child in children] != ['source_thread_id', 'input']
            or any(child.attrib or len(child) or (child.tail or '').strip() for child in children)):
        return None
    sender, prompt = (child.text for child in children)
    if not sender or sender != sender.strip() or not isinstance(prompt, str):
        return None
    return {'id': message.get('id'), 'turnId': message.get('turnId', turn_id), 'role': 'agent',
        'source_thread_id': sender, 'text': prompt,
        'truncated': not (output.get('truncated') is False and message.get('truncated', False) is False
                          and message.get('contentTruncated', False) is False)}


def native_projection(raw, thread_id=None, turn_id=None):
    """Read public tool envelopes; the original JSON envelope stays in events."""
    data = raw.get('structuredContent')
    if data is None:
        text = [part['text'] for part in raw.get('content', []) if part.get('type') == 'text' and isinstance(part.get('text'), str)]
        if len(text) == 1:
            try:
                data = json.loads(text[0])
            except ValueError:
                pass
    data = raw if data is None else data
    require(isinstance(data, dict), 'Native structured result must be an object')
    if isinstance(data.get('polls'), list):
        polls = [poll for poll in data['polls'] if isinstance(poll, dict) and isinstance(poll.get('thread'), dict)
                 and (thread_id is None or poll['thread'].get('id') == thread_id)]
        require(len(polls) <= 1, 'Native wait contains ambiguous target observations', 'IDENTITY_MISMATCH')
        data = polls[0] if polls else {}
    if isinstance(data.get('thread'), dict) and isinstance(data.get('turns'), list):
        turns = data['turns']
        require(data.get('page', {}).get('order') == 'newest_first' and turns and isinstance(turns[0], dict),
                'Desktop read needs its actual newest-first turn coverage', 'IDENTITY_MISMATCH')
        turn = turns[0]
        if turn_id is not None:
            require(thread_id is None or data['thread'].get('id') == thread_id,
                    'Desktop history belongs to another thread', 'IDENTITY_MISMATCH')
            matches = [item for item in turns if isinstance(item, dict) and item.get('id') == turn_id]
            require(len(matches) == 1, 'Desktop history needs one exact bound turn; retain current action', 'IDENTITY_MISMATCH')
            turn = matches[0]
        messages = []
        for message in turn.get('items', []):
            if message.get('type') != 'userMessage':
                delegation = _desktop_delegation(message, turn.get('id'))
                if delegation:
                    messages.append(delegation)
                continue
            delegation = message.get('codexDelegation', {})
            text = delegation.get('input')
            if text is None:
                blocks = message.get('content', [])
                text = '\n'.join(block['text'] for block in blocks if block.get('type') == 'text' and isinstance(block.get('text'), str))
            messages.append({'id': message.get('id'), 'turnId': turn.get('id'), 'role': 'user', 'text': text,
                'truncated': message.get('truncated', message.get('contentTruncated', False))})
        result = {'threadId': data['thread'].get('id'), 'latestTurn': {'id': turns[0].get('id'), 'status': turns[0].get('status')}, 'items': messages,
                  'public_read_metadata': {key: data['thread'][key] for key in ('hostId', 'cwd', 'projectId') if key in data['thread']}}
        if turn_id is not None:
            result.update(turnId=turn.get('id'), terminalStatus=turn.get('status'))
        return result
    if isinstance(data.get('thread'), dict) and isinstance(data.get('latestTurn'), dict):
        return {**data, 'threadId': data['thread'].get('id')}
    return data


class NativeEntry:
    def __init__(self, state):
        self.state, self.db = state, state.db
        self.runtime = Runtime(state)

    def get(self, identifier):
        row = self.db.execute("SELECT seq,payload FROM events WHERE kind='native_entry_state' AND json_extract(payload,'$.id')=? ORDER BY seq DESC LIMIT 1", (identifier,)).fetchone()
        require(row, 'Unknown native entry: ' + str(identifier))
        return {**json.loads(row['payload']), 'event_seq': row['seq']}

    def actions(self, work=None):
        rows = self.db.execute("SELECT payload FROM events WHERE kind='native_entry_state'" +
            (' AND work_id=?' if work else '') + ' ORDER BY seq', (work,) if work else ())
        latest = {}
        for row in rows:
            item = json.loads(row[0]); latest[item['id']] = item
        return list(latest.values())

    @staticmethod
    def live(item):
        if item.get('origin') == 'observed':
            return False  # Reading an existing session acquires no execution claim.
        return item['phase'] in ACTIVE and not item.get('native_terminal_status')

    def _save(self, item, source):
        item = {k: v for k, v in item.items() if k != 'event_seq'}
        item['revision'] = item.get('revision', 0) + 1
        self.state.event(item['work_id'], 'native_entry_state', source, item,
                         request_id=item['id'], session=item.get('thread_id'))
        return self.get(item['id'])

    def _current(self, item, target=None):
        if item['method'] == 'send_message':
            check_main_message(self.state, target or item.get('thread_id'))
        task = self.state.task(item['task_id']); work = self.state.get(task['work_id'])
        if item.get('coordinator') is not None:
            self.runtime._check_owner(task, item['coordinator'], item['owner_revision'])
        require(work['request_revision'] == item['request_revision'], 'Work correction invalidated this native entry', 'REVISION_CONFLICT')
        require(goals.fingerprint({k: v for k, v in task['body'].items() if k != 'reviews'}) == item['task_sha256'],
                'Task scope changed after native preparation', 'REVISION_CONFLICT')
        check = self.state.check_work(work['id'], task=task['id'])
        require(check['allowed'], '; '.join(check['reasons']), 'ENTRY_NOT_READY')
        goals.ready(self.state, task)
        continuous = item['provider'] == LOCAL_PROVIDER or item.get('origin') == 'observed'
        if not continuous:
            self.state.check_decision(task)
        from .coordination import check_admission
        check_admission(self.state, task, operation='dispatch', target=target or item.get('thread_id'), continuous=continuous)
        row = self.db.execute('SELECT kind,body FROM runtime_controls WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task['id'],)).fetchone()
        if row:
            control = json.loads(row['body']); plan = control['plan']
            require(self.runtime.scope_plan(plan['roots'], plan['include_dependents']) == plan,
                    'Dependencies changed after local scope control', 'SCOPE_GRAPH_CHANGED')
            require(row['kind'] == 'resume', 'Task or a real dependent scope is locally held', 'SCOPE_PAUSED')
        for dependency in task['body'].get('requires', []):
            held = self.db.execute('SELECT kind FROM runtime_controls WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (dependency,)).fetchone()
            require(not held or held['kind'] == 'resume', 'A real prerequisite is locally held', 'DEPENDENCY_PAUSED')
        return task

    def occupied(self, *, excluding=None):
        return [item for item in self.actions() if item['id'] != excluding and not item.get('resources_released')
                and item.get('resources') and item['phase'] != 'cancelled']

    def _stage_ready(self, item, previous):
        review = previous.get('stage_review', {})
        return (item['provider'] == previous['provider'] == LOCAL_PROVIDER and item['method'] == 'send_message'
                and not item.get('entry_only') and not item.get('resources') and previous.get('origin') != 'observed'
                and not previous.get('entry_only') and previous['phase'] == 'returned' and previous.get('return_event')
                and not previous.get('conflict') and previous.get('review_disposition') in ('returned', 'adopted')
                and previous.get('return_review', {}).get('outcome') not in ('unresolved', 'revise')
                and previous.get('review_request_sha256') and review.get('mode') == 'independent'
                and review.get('reviewer') == item.get('coordinator') and review.get('artifact_evidence')
                and all(item.get(key) == previous.get(key) for key in
                        ('work_id', 'task_id', 'thread_id', 'request_revision', 'task_sha256')))

    def _continuation_ancestors(self, item):
        related = {}; current = item
        while current.get('continuation_of'):
            link = current['continuation_of']; previous = self.get(link['id'])
            require(previous['id'] not in related and self._stage_ready(item, previous)
                    and all(link.get(key) == previous.get(key) for key in ('return_event', 'review_request_sha256', 'turn_id')),
                    'Stage return/review or exact continuation scope changed', 'REVISION_CONFLICT')
            self.state._file_evidence(previous['stage_review']['artifact_evidence'], 'runtime')
            related[previous['id']] = previous; current = previous
        if related:
            owner = self.db.execute('SELECT owner,revision FROM runtime_coordinators WHERE work_id=?', (item['work_id'],)).fetchone()
            binding = self.state.binding(item['thread_id'])
            require(owner and item.get('coordinator') == owner['owner'] and item.get('owner_revision') == owner['revision']
                    and binding and binding['work_id'] == item['work_id'] and binding['role'] == 'executor',
                    'Stage feedback needs the current owner and exact executor binding', 'COORDINATOR_REQUIRED')
        return related

    def _link_stage_feedback(self, item):
        candidates = [previous for previous in self.actions(item['work_id'])
                      if self.live(previous) and self._stage_ready(item, previous)]
        ancestors = {previous['continuation_of']['id'] for previous in candidates if previous.get('continuation_of')}
        latest = [previous for previous in candidates if previous['id'] not in ancestors]
        if len(latest) == 1:
            previous = latest[0]
            item['continuation_of'] = {key: previous[key] for key in ('id', 'return_event', 'review_request_sha256', 'turn_id')}

    @staticmethod
    def _received_prompt(item, data, turn, *, sender_required=False):
        messages = data.get('items', [])
        require(isinstance(messages, list) and all(isinstance(message, dict) for message in messages), 'Native message items must be objects')
        senders = {sender for sender in (item.get('native_guard', {}).get('caller'), item.get('coordinator')) if sender}
        matching = [message for message in messages if ((not sender_required and message.get('role') == 'user') or
            message.get('role') == 'agent' and len(senders) == 1 and message.get('source_thread_id') in senders)
            and message.get('turnId') == turn and message.get('text') == item['params']['prompt']
            and message.get('truncated') is False]
        require(len(matching) == 1, 'Read needs one exact untruncated received prompt for this turn and the prepared sender for an agent delegation', 'IDENTITY_MISMATCH')

    def _conflicts(self, item):
        related = self._continuation_ancestors(item)
        for other in self.actions():
            if other['id'] == item['id'] or other['id'] in related:
                continue
            require(not ((self.live(other) or other.get('conflict')) and (other['task_id'] == item['task_id'] or
                    item.get('thread_id') and other.get('thread_id') == item['thread_id'])),
                    'Another ordinary entry owns the task/thread; reconcile first', 'ACTION_UNKNOWN')
        for other in self.runtime._actions():
            require(not (other['body']['phase'] in LIVE and (other['task_id'] == item['task_id'] or
                    item.get('thread_id') and other['thread_id'] == item['thread_id'])),
                    'An owned runtime already has this task/thread', 'ACTION_UNKNOWN')
        active = self.state.active_dispatches(item.get('thread_id')) if item.get('thread_id') else []
        require(not [d for d in active if d['key'] != item.get('dispatch_key')], 'Target has active/unknown dispatch', 'ACTION_UNKNOWN')
        for resource in item['resources']:
            claim = self.runtime._get('runtime_resource_claims', 'resource', resource, True)
            require(not claim or claim['status'] == 'released', 'Owned runtime holds shared object: ' + resource, 'RESOURCE_OCCUPIED')
            require(not any(resource in other['resources'] for other in self.occupied(excluding=item['id'])),
                    'Ordinary entry holds shared object: ' + resource, 'RESOURCE_OCCUPIED')

    def prepare(self, request):
        require(isinstance(request, dict), 'Native entry request must be an object')
        identifier = request.get('id')
        require(isinstance(identifier, str) and identifier and request.get('source'), 'Local correlation id and source required')
        with self.state.transaction():
            if request.get('method') == 'send_message' and isinstance(request.get('params'), dict):
                check_main_message(self.state, request['params'].get('threadId'))
            previous = self.db.execute("SELECT 1 FROM events WHERE kind='native_entry_state' AND json_extract(payload,'$.id')=?", (identifier,)).fetchone()
            if previous:
                old = self.get(identifier)
                require(old['preparation_sha256'] == native_hash(request), 'Reused local entry id has different preparation', 'IDENTITY_MISMATCH')
                return {'entry': old, 'send_once': False, 'reason': 'Already prepared; reconcile observed receipt before any resend'}
            provider = request.get('provider')
            require(provider in (PROVIDER, LOCAL_PROVIDER) and request.get('method') in ('create', 'send_message'), 'Unsupported ordinary native provider/method')
            params = request.get('params'); method = request['method']
            allowed = {'threadId', 'hostId', 'prompt', 'model', 'thinking'} if method == 'send_message' else {
                'title', 'prompt', 'cwd', 'environmentId', 'environmentConfigId', 'projectId', 'fork_turns', 'model', 'thinking', 'service_tier', 'skills', 'target'}
            require(isinstance(params, dict) and set(params) <= allowed and isinstance(params.get('prompt'), str) and params['prompt'].strip(), 'Bind exact native tool parameters')
            require(not (params.get('environmentId') and params.get('environmentConfigId')), 'Native environment selectors are mutually exclusive')
            require(method != 'create' or params.get('title') and request.get('session_choice_reason'), 'New task requires title and reuse/splitting reason')
            require(method != 'send_message' or isinstance(params.get('threadId'), str) and params['threadId'], 'Existing thread must be exact')
            require(isinstance(request.get('entry_coverage'), dict) and request['entry_coverage'].get('execution_location'),
                    'Declare actual entry/configuration coverage; unknown fields stay unknown')
            work = self.state.get(request['work'])
            require(request.get('expected') == work['revision'], 'Stale work revision', 'REVISION_CONFLICT')
            execution = work.get('execution_policy', {})
            for field in ('model', 'thinking'):
                require(not execution.get(field) or params.get(field) == execution[field], 'Native preparation violates required ' + field)
            require(not execution.get('target') or params.get('threadId') == execution['target'], 'Native preparation violates required existing target')
            if request.get('task_body'):
                self.state.create_task(request['task'], work['id'], work['revision'], request['task_body'], request['source'])
            prior_task = self.state.task(request['task'])
            if prior_task['status'] == 'revise':
                require(request.get('repair_review_sha256') == goals.fingerprint(prior_task['body']['reviews'][-1]),
                        'Bind the exact current repair review before another execution')
                self.db.execute("UPDATE tasks SET status='proposed' WHERE id=?", (prior_task['id'],))
            if request.get('decision'):
                self.state.decide(work['id'], work['revision'], request['decision'], request['source'], actor=request.get('actor'))
            task = self.state.task(request['task'])
            allowed_statuses = ('proposed', 'returned', 'adopted') if provider == LOCAL_PROVIDER else ('proposed',)
            require(task['work_id'] == work['id'] and task['status'] in allowed_statuses, 'Native entry requires a current available unit task')
            require(task['body'].get('kind') == 'unit', 'Aggregate work is not an execution package')
            require(not self.runtime.route(task['id'], True), 'Keep an existing owned-runtime route on its original path', 'IDENTITY_MISMATCH')
            policy = self.runtime._get('runtime_policies', 'id', request['policy_id']) if request.get('policy_id') else None
            require(provider == LOCAL_PROVIDER or policy, 'Legacy Dot admission requires its explicit policy')
            owner = self.db.execute('SELECT owner,revision FROM runtime_coordinators WHERE work_id=?', (work['id'],)).fetchone()
            require(not owner or request.get('coordinator') == owner['owner'] and request.get('owner_revision') == owner['revision'],
                    'Bind current coordinator and revision for a new managed action', 'REVISION_CONFLICT')
            require(request.get('review_owner'), 'Return receiver required')
            resources = request.get('resources', [])
            require(isinstance(resources, list) and len(resources) == len(set(resources)) and all(isinstance(r, str) and r for r in resources), 'Unique shared resource identities required')
            require(not request.get('entry_only') or not resources, 'Entry-only creation cannot claim scientific shared objects')
            item = {'format': FORMAT, 'id': identifier, 'revision': 0, 'work_id': work['id'], 'task_id': task['id'],
                'provider': provider, 'method': method, 'params': deepcopy(params), 'request_sha256': native_hash(params),
                'preparation_sha256': native_hash(request), 'request_revision': work['request_revision'],
                'task_sha256': goals.fingerprint({k: v for k, v in task['body'].items() if k != 'reviews'}),
                'coordinator': request.get('coordinator'), 'owner_revision': request.get('owner_revision'),
                'policy_id': policy['id'] if policy else None, 'review_owner': request['review_owner'], 'resources': resources,
                'entry_coverage': deepcopy(request['entry_coverage']), 'thread_id': params.get('threadId'),
                'entry_only': bool(request.get('entry_only')),
                'turn_id': None, 'native_terminal_status': None, 'dispatch_key': None, 'phase': 'prepared',
                'budget_claimed': bool(policy), 'prepared_at': now(), 'receipts': [], 'return_event': None,
                'missing_fields': ['runtimeId', 'clientMessageId', 'effective_permissions'],
                'identity_coverage': 'id is a local correlation only; no stdio/runtime ownership acquired'}
            self._current(item); self._link_stage_feedback(item); self._conflicts(item)
            if policy:
                self.runtime._capacity({'id': identifier, 'body': {'policy_id': policy['id']}})
            return {'entry': self._save(item, request['source']), 'send_once': True,
                'native_request': {'provider': provider, 'method': method, 'params': params},
                'coverage': 'Local preparation committed; native send is a separate real operation; unknown send must be reconciled'}

    def _queue(self, item, source):
        if self._return_handled(item) and not item.get('conflict'):
            return
        old = self.runtime._get('runtime_review_queue', 'task_id', item['task_id'], True)
        if old and old['body'].get('native_entry_id') and old['body']['native_entry_id'] != item['id']:
            newer = self.get(old['body']['native_entry_id'])
            if (newer.get('return_event') and (not item.get('return_event') or newer['return_event'] > item['return_event'])
                    or not newer.get('return_event') and newer['prepared_at'] > item['prepared_at']):
                return
        if old and old['body'].get('native_entry_id') == item['id']:
            if item.get('return_event') and not old['body'].get('return_event'):
                self.db.execute('UPDATE runtime_review_queue SET body=?,revision=revision+1,at=? WHERE task_id=?',
                    (encoded({**old['body'], 'return_event': item['return_event']}), now(), item['task_id']))
            return  # Later terminal/replayed facts do not reopen a consumed return.
        self.db.execute('INSERT INTO runtime_review_queue VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET action_id=excluded.action_id,owner=excluded.owner,revision=excluded.revision,status=excluded.status,body=excluded.body,at=excluded.at',
            (item['task_id'], None, item['review_owner'], (old['revision'] if old else 0) + 1, 'pending',
             encoded({'native_entry_id': item['id'], 'policy_id': item['policy_id'], 'return_event': item.get('return_event'),
                'ordinary_observation': item.get('origin') == 'observed' or item['provider'] == LOCAL_PROVIDER,
                'meaning': 'Local result and actual native termination are separate; review the artifacts before adoption'}), now()))

    @staticmethod
    def _return_handled(item):
        review = item.get('return_review', {})
        if review:
            return review.get('return_event') == item.get('return_event') and review.get('outcome') == 'handled'
        # Explicit historical task adoption/abandonment remains compatible. A
        # timestamp or an independent-review file alone states no stage outcome.
        return bool(item.get('review_request_sha256') and item.get('review_disposition') in ('adopted', 'abandoned'))

    def fact(self, request):
        with self.state.transaction():
            item = self.get(request['id']); fact = request.get('receipt', {})
            previous_phase = item['phase']
            was_terminal = bool(item.get('native_terminal_status'))
            was_cancelled = item['phase'] == 'cancelled' and item.get('native_terminal_status') in TERMINAL
            require(isinstance(fact, dict) and fact.get('id') and fact.get('reference') and fact.get('observed_at')
                    and fact.get('kind') in ('send', 'read', 'notification', 'unknown', 'not_sent'), 'Exact native receipt identity, source, time and kind required')
            datetime.fromisoformat(fact['observed_at'].replace('Z', '+00:00'))
            old = self.db.execute("SELECT seq,payload FROM events WHERE kind='native_entry_receipt' AND json_extract(payload,'$.receipt.id')=?", (fact['id'],)).fetchone()
            if old:
                saved = json.loads(old['payload'])
                require(saved['id'] == item['id'] and saved['receipt'] == fact, 'Native receipt identity has conflicting content', 'IDENTITY_MISMATCH')
                return {'entry': item, 'duplicate': True}
            raw = fact.get('raw', {})
            require(isinstance(raw, dict), 'Keep actual structured native result as raw object')
            data = native_projection(raw, item.get('thread_id'), item.get('turn_id'))
            latest = data.get('latestTurn') or {}
            require(isinstance(latest, dict), 'Native latestTurn must be an object or unknown')
            thread = data.get('threadId', data.get('childThreadId'))
            turn = data.get('turnId', data.get('childTurnId', latest.get('id')))
            terminal = data.get('terminalStatus', latest.get('status'))
            require(all(value is None or isinstance(value, str) and value for value in (thread, turn)), 'Native identities must be actual strings or unknown')
            outcome = data.get('admissionOutcome')
            if thread:
                require(not item.get('thread_id') or thread == item['thread_id'], 'Receipt thread differs from exact native entry', 'IDENTITY_MISMATCH')
            if turn and item.get('turn_id'):
                require(turn == item['turn_id'], 'Read/notification belongs to another turn; retain current action', 'IDENTITY_MISMATCH')
            if fact['kind'] == 'send':
                require(fact.get('request_sha256') == item['request_sha256'], 'Send receipt must bind exact original native parameters', 'IDENTITY_MISMATCH')
            require(fact.get('id_kind', 'local_observation') in ('native', 'local_observation'), 'Receipt id must declare native or local-observation provenance')
            for key in ('runtimeId', 'clientMessageId', 'effective_permissions'):
                if key in data and data[key] is not None:
                    item.setdefault('native_metadata_observed', {})[key] = deepcopy(data[key])
                    item['missing_fields'] = [field for field in item['missing_fields'] if field != key]
            steered_pending = item.get('steered_receipt_id') and not item.get('steered_received_receipt_id')
            if thread and turn and fact['kind'] in ('read', 'notification') and item.get('origin') != 'observed':
                if not item.get('turn_id') or steered_pending:
                    self._received_prompt(item, data, turn, sender_required=bool(steered_pending))
                if steered_pending:
                    related = self._continuation_ancestors(item)
                    previous = related[item['continuation_of']['id']]
                    require(turn == previous['turn_id'] and not previous.get('native_terminal_status'),
                            'Steered feedback must be received in the exact still-running predecessor turn', 'IDENTITY_MISMATCH')
                    item['steered_received_receipt_id'] = fact['id']
            if fact['kind'] == 'not_sent':
                require(not item['receipts'] and not item.get('dispatch_key') and fact.get('reason'),
                        'Cancellation requires positive no-send evidence before any observed native receipt', 'ACTION_UNKNOWN')
                item.update(phase='cancelled', resources_released=True, budget_claimed=False)
            elif fact['kind'] == 'unknown' or not thread or not turn:
                item['phase'] = 'unconfirmed'
            else:
                item.update(thread_id=thread, turn_id=turn)
                if outcome == 'steered':
                    try:
                        related = self._continuation_ancestors(item)
                        previous = related[item['continuation_of']['id']] if related else None
                    except HarnessError:
                        previous = None
                    if previous and turn == previous['turn_id'] and not previous.get('native_terminal_status'):
                        item['steered_receipt_id'] = fact['id']
                    else:
                        item['conflict'] = 'Native send steered an already active turn; new business admission requires reconciliation'
                if terminal in TERMINAL:
                    item.setdefault('terminal_observations', []).append({'status': terminal, 'receipt_id': fact['id']})
                    previous = item.get('native_terminal_status')
                    if previous and previous != terminal:
                        item['conflict'] = 'Conflicting actual terminal observations: ' + previous + '/' + terminal
                    else:
                        item['native_terminal_status'] = terminal
                    if item['entry_only']:
                        item['phase'] = 'entry_ready' if terminal == 'completed' else 'entry_failed'
                    else:
                        self._queue(item, request['source'])
                if not item.get('return_event'):
                    if not item['entry_only'] or not item.get('native_terminal_status'):
                        item['phase'] = 'accepted' if item.get('accepted_by') else 'sent'
                if fact['kind'] == 'send' and (raw.get('isError') is True or outcome not in ('started', 'steered')) and not item.get('native_terminal_status'):
                    item['phase'] = 'unconfirmed'
                if item.get('steered_receipt_id') and not item.get('steered_received_receipt_id'):
                    item['phase'] = 'unconfirmed'
            if was_cancelled:
                item['phase'] = 'cancelled'  # Late receipts preserve closure; actual conflicts remain attention.
            elif item.get('return_event'):
                item['phase'] = 'returned'
            elif was_terminal:
                item['phase'] = previous_phase
            seq = self.state.event(item['work_id'], 'native_entry_receipt', request['source'],
                {'id': item['id'], 'receipt': fact}, external_event_id=str(fact['id']) if fact.get('id_kind') == 'native' else None, session=thread)
            item['receipts'].append(seq); item['last_observed_at'] = fact['observed_at']
            if (item['provider'] == LOCAL_PROVIDER and item.get('origin') != 'observed' and not item.get('entry_only')
                    and thread and turn and not item.get('conflict') and item['phase'] not in ('unconfirmed', 'cancelled')
                    and not item.get('return_event') and not item.get('accepted_by')):
                binding = self.state.binding(thread)
                # This prepared dispatch already carries its execution scope.
                # Preserve a real send receipt when another binding needs explicit
                # reconciliation; observations and old accepted entries acquire none.
                if item.get('native_terminal_status'):
                    item['accepted_by'] = thread  # Producer may return the result; closed execution acquires no binding.
                elif not binding or binding['work_id'] == item['work_id'] and binding['role'] == 'executor':
                    self.state.bind(thread, item['work_id'], 'executor', request['source'])
                    item.update(accepted_by=thread, accepted_at=now(), phase='accepted')
            return {'entry': self._save(item, request['source']), 'duplicate': False}

    def accept(self, request):
        if request.get('prior_review'):
            # A precisely authored Dot review can travel with the next actual
            # message. Persist it even if this new task is subsequently blocked.
            self.review(request['prior_review'])
        # Fact registration is deliberately committed first: rejected admission must
        # not discard an observed native send/terminal or its recovery pointer.
        if request.get('receipt'):
            require(request.get('expected') == self.get(request['id'])['revision'], 'Stale native entry revision', 'REVISION_CONFLICT')
            observed = self.fact(request)
            request = {**request, 'expected': observed['entry']['revision']}
        with self.state.transaction():
            item = self.get(request['id'])
            require(not item.get('entry_only'), 'Entry-only create does not admit scientific work; continue the same exact thread after entry preparation')
            require(request.get('expected') == item['revision'], 'Stale native entry revision', 'REVISION_CONFLICT')
            self._identity(item, request)
            require(not item.get('conflict') and item['phase'] == 'sent' and not item.get('native_terminal_status'),
                    'Resolve unknown/conflicting/terminal native entry before business', 'ENTRY_NOT_READY')
            require(not item.get('accepted_by'), 'Entry was already claimed; recover its existing context', 'INTENT_ALREADY_USED')
            task = self._current(item, item['thread_id']); self._conflicts(item)
            binding = self.state.binding(item['thread_id'])
            require(not binding or binding['work_id'] == item['work_id'] and binding['role'] == 'executor', 'Executor has another work/role; explicit binding reconciliation required', 'IDENTITY_MISMATCH')
            self.state.bind(item['thread_id'], item['work_id'], 'executor', request['source'])
            key = 'native-entry:' + item['id']
            message = {k: v for k, v in item['params'].items() if k in ('prompt', 'model', 'thinking')}
            message['threadId'] = item['thread_id']
            self.state.reserve(key, task['id'], item['thread_id'], request['source'], message, native_entry_id=item['id'])
            self.state.settle(key, 'sent', {'native_entry': item['id'], 'receipt_events': item['receipts']}, request['source'])
            self.state.settle(key, 'started', {'executor_session': request['executor_session'], 'native_turn': item['turn_id']}, request['source'])
            item.update(phase='accepted', accepted_by=request['executor_session'], accepted_at=now(), dispatch_key=key,
                execution_location=deepcopy(request.get('execution_location')))
            saved = self._save(item, request['source'])
            return {'entry': saved, 'task': self.state.task(task['id']), 'work': self.state.get(item['work_id']),
                'pending_inputs': self.state.pending(item['work_id']),
                'coverage': 'Current same-store admission only; free tools and already running external actions are not globally intercepted'}

    @staticmethod
    def _identity(item, request):
        require(item.get('thread_id') and item.get('turn_id') and request.get('executor_session') == item['thread_id']
                and request.get('thread_id') == item['thread_id'] and request.get('turn_id') == item['turn_id'],
                'Actual executor and native thread/turn must exactly match observed receipt', 'IDENTITY_MISMATCH')

    def receive(self, request):
        """Associate an already real native session/result, without inventing a send."""
        require(request.get('id') and request.get('source') and request.get('review_owner'), 'Local id, source and return receiver required')
        receipt = request.get('receipt', {})
        require(isinstance(receipt, dict) and receipt.get('id') and receipt.get('reference')
                and receipt.get('observed_at') and isinstance(receipt.get('raw'), dict), 'Keep the actual native receipt, reference and observation time')
        datetime.fromisoformat(receipt['observed_at'].replace('Z', '+00:00'))
        provider = request.get('provider', LOCAL_PROVIDER)
        require(provider in (LOCAL_PROVIDER, PROVIDER, 'codex.subagent'), 'Unknown native observation provider')
        data = native_projection(receipt['raw'], request.get('thread_id'))
        latest = data.get('latestTurn') or {}
        thread = data.get('threadId', data.get('childThreadId'))
        turn = data.get('turnId', data.get('childTurnId', latest.get('id')))
        require(isinstance(thread, str) and thread, 'Actual native receipt must identify its thread', 'IDENTITY_MISMATCH')
        require(not request.get('thread_id') or request['thread_id'] == thread, 'Observed native thread differs', 'IDENTITY_MISMATCH')
        require(turn is None or isinstance(turn, str) and turn, 'Native turn must be actual or unknown')
        with self.state.transaction():
            existing = self.db.execute("SELECT 1 FROM events WHERE kind='native_entry_state' AND json_extract(payload,'$.id')=?", (request['id'],)).fetchone()
            if existing:
                item = self.get(request['id'])
                require(item.get('receive_sha256') == native_hash(request), 'Native observation id has conflicting content', 'IDENTITY_MISMATCH')
                return {'entry': item, 'duplicate': True, 'send_once': False}
            work = self.state.get(request['work'])
            require(request.get('expected') == work['revision'], 'Stale work revision', 'REVISION_CONFLICT')
            task_id = request.get('task', request['id'])
            task_row = self.db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not task_row:
                body = request.get('task_body') or {'purpose': request.get('purpose'), 'expected': request.get('expected_result')}
                self.state.create_task(task_id, work['id'], work['revision'], body, request['source'])
            task = self.state.task(task_id)
            require(task['work_id'] == work['id'], 'Native observation task belongs to another work', 'IDENTITY_MISMATCH')
            receiver = self.runtime._get('runtime_review_queue', 'task_id', task_id, True)
            coordinator = self.runtime._get('runtime_coordinators', 'work_id', work['id'], True)
            assigned = receiver['owner'] if receiver else coordinator['owner'] if coordinator else None
            require(assigned is None or request['review_owner'] == assigned,
                    'Native observation must retain the assigned receiver or current coordinator', 'COORDINATOR_REQUIRED')
            old_receipt = self.db.execute("SELECT payload FROM events WHERE kind='native_entry_receipt' AND json_extract(payload,'$.receipt.id')=?", (receipt['id'],)).fetchone()
            require(not old_receipt, 'Receipt already associated; recover that existing native entry', 'IDENTITY_MISMATCH')
            for other in self.actions(work['id']):
                require(not (turn is not None and other['task_id'] == task_id and other.get('thread_id') == thread and other.get('turn_id') == turn),
                        'Exact task/native observation already associated; use its existing entry', 'IDENTITY_MISMATCH')
            status = data.get('terminalStatus', latest.get('status'))
            item = {'format': FORMAT, 'id': request['id'], 'revision': 0, 'origin': 'observed',
                'work_id': work['id'], 'task_id': task_id, 'provider': provider, 'method': 'observe',
                'params': {}, 'request_sha256': None, 'receive_sha256': native_hash(request),
                'request_revision': task['work_revision'], 'task_sha256': goals.fingerprint({k:v for k,v in task['body'].items() if k != 'reviews'}),
                'coordinator': None, 'owner_revision': None, 'policy_id': None, 'review_owner': request['review_owner'],
                'resources': [], 'thread_id': thread, 'turn_id': turn, 'accepted_by': thread, 'entry_only': False,
                'native_terminal_status': status if status in TERMINAL else None, 'dispatch_key': None,
                'phase': 'accepted', 'budget_claimed': False, 'prepared_at': now(), 'last_observed_at': receipt['observed_at'],
                'receipts': [], 'return_event': None,
                'entry_coverage': {'observation_reference': receipt['reference']},
                'missing_fields': [key for key, value in (('turnId', turn), ('runtimeId', data.get('runtimeId')), ('effective_permissions', data.get('effective_permissions'))) if value is None],
                'identity_coverage': 'Actual native observation only; no send, authority, owned runtime or native stop inferred'}
            if status in TERMINAL:
                item['terminal_observations'] = [{'status': status, 'receipt_id': receipt['id']}]
            seq = self.state.event(work['id'], 'native_entry_receipt', request['source'],
                {'id': request['id'], 'receipt': {**deepcopy(receipt), 'kind': 'read'}}, session=thread)
            item['receipts'].append(seq)
            saved = self._save(item, request['source'])
            if 'result' in request:
                return self.finish({**request, 'expected': saved['revision'], 'executor_session': thread, 'turn_id': turn})
            return {'entry': saved, 'duplicate': False, 'send_once': False}

    def finish(self, request):
        """Save an actual result even when planning, ownership or native freshness changed."""
        item = self.get(request['id'])
        require(item.get('origin') == 'observed' or item['provider'] == LOCAL_PROVIDER,
                'Direct finish is for ordinary native observations')
        require(request.get('executor_session') == item['thread_id'], 'Result producer differs from native observation', 'IDENTITY_MISMATCH')
        if item.get('turn_id'):
            require(request.get('turn_id') == item['turn_id'], 'Result belongs to another observed turn', 'IDENTITY_MISMATCH')
        # returned() checks result identity, files and CAS. Unknown native turn stays
        # unknown while the saved producer/thread association remains explicit.
        return self.returned({**request, 'thread_id': item['thread_id'], 'turn_id': item.get('turn_id')}, observed=True)

    def returned(self, request, observed=False):
        with self.state.transaction():
            item = self.get(request['id'])
            from .improvements import check_origin, record_findings, declared_findings
            check_origin(item, request)
            if not observed:
                self._identity(item, request)
            result = request.get('result')
            require(isinstance(result, dict) and result.get('answer') and isinstance(result.get('artifacts'), list)
                    and all(isinstance(ref, str) and ref for ref in result['artifacts'])
                    and isinstance(result.get('remaining'), list), 'Return needs actual answer, artifact references and remaining limits')
            if item.get('return_event'):
                require(item['return_sha256'] == native_hash(result), 'Return replay differs; preserve original and prepare an explicit repair', 'IDENTITY_MISMATCH')
                findings = record_findings(self.state, item, declared_findings(self.state, request), request['source'],
                    request.get('writer_session', request.get('executor_session')))
                return {'entry': item, 'duplicate': True, **({'findings': findings} if findings or 'findings' in request else {})}
            require(request.get('expected') == item['revision'] and item.get('accepted_by') == request['executor_session'],
                    'Only the current admitted executor may return this result', 'REVISION_CONFLICT')
            # Capture actual files now. A changed request never discards an already
            # produced result; State's adoption checks decide whether it is usable.
            evidence, issues = [], []
            for ref in result['artifacts']:
                try:
                    evidence.extend(self.state._file_evidence([{'path': ref}], 'runtime'))
                except (HarnessError, OSError) as exc:
                    if not (observed or item['provider'] == LOCAL_PROVIDER):
                        raise
                    issues.append({'path': ref, 'status': 'unverified', 'reason': str(exc)})
            review = {**deepcopy(result), 'reviewer': request['executor_session'], 'review_mode': 'self',
                'native_entry_id': item['id'], 'artifact_evidence': evidence, 'artifact_issues': issues}
            self.state.review_task(item['task_id'], 'returned', review, request['source'])
            seq = self.state.event(item['work_id'], 'native_entry_result', request['source'], {'id': item['id'], 'result': review})
            item.update(return_event=seq, return_sha256=native_hash(result), phase='returned',
                return_review={'return_event': seq, 'outcome': 'unreviewed'})
            findings = record_findings(self.state, item, declared_findings(self.state, request), request['source'],
                request.get('writer_session', request.get('executor_session')))
            if item.get('dispatch_key'):
                dispatch = self.state.dispatch(item['dispatch_key'])
                if dispatch['status'] != 'returned':
                    self.state.settle(item['dispatch_key'], 'returned', {'local_return_event': seq, 'native_terminal_status': item.get('native_terminal_status')}, request['source'])
            self._queue(item, request['source'])
            return {'entry': self._save(item, request['source']), 'duplicate': False,
                **({'findings': findings} if findings or 'findings' in request else {}),
                'coverage': 'Artifacts and self-check saved; actual native terminal status remains exactly as observed'}

    def review(self, request):
        with self.state.transaction():
            item = self.get(request['id']); work = self.state.get(item['work_id'])
            if native_hash(request) in (item.get('review_request_sha256'), item.get('return_review', {}).get('review_request_sha256')):
                return {'entry': item, 'task': self.state.task(item['task_id']), 'duplicate': True}
            require(request.get('expected') == item['revision'] and request.get('work_expected') == work['revision'], 'Stale return/work revision', 'REVISION_CONFLICT')
            require(item.get('return_event') and not item.get('conflict'), 'Actual local return required before review')
            require(request.get('return_event', item['return_event']) == item['return_event'],
                    'Review belongs to another exact native return', 'IDENTITY_MISMATCH')
            review = request.get('review', {})
            queue = self.runtime._get('runtime_review_queue', 'task_id', item['task_id'], True)
            current = (queue and queue['body'].get('native_entry_id') == item['id']
                       and queue['body'].get('return_event') == item['return_event'])
            stage_outcome = request.get('stage_outcome')
            if not current:
                require(stage_outcome is not None and request['disposition'] == 'returned',
                        'Historic return review cannot consume the current task return', 'IDENTITY_MISMATCH')
            receiver = queue['owner'] if current else item['review_owner']
            require(review.get('reviewer') == receiver, 'Review must come from the exact return receiver')
            origin = request.get('review_origin', {})
            require(origin.get('author') == receiver and origin.get('provider') and origin.get('reference')
                    and origin.get('payload_sha256') == native_hash(review) and request.get('writer_session'),
                    'Bind actual review author, writer session, exact content and native message reference')
            require(request.get('review_role') in ('coordination', 'independent'), 'Declare actual review role')
            require(request['review_role'] != 'coordination' or review.get('review_mode') != 'independent',
                    'Coordination receipt cannot label itself independent professional review')
            existing_review = request.get('existing_review')
            if existing_review is not None:
                require(isinstance(existing_review, dict) and stage_outcome == 'handled',
                        'Existing-review reconciliation only records an explicit handled stage')
                prior = [row for row in self.state.task(item['task_id'])['body'].get('reviews', [])
                    if row.get('at') == existing_review.get('at') and row.get('disposition') == 'returned'
                    and row.get('review', {}).get('native_review_origin') == origin]
                require(len(prior) == 1 and existing_review.get('payload_sha256') == native_hash(review)
                        and prior[0]['review'].get('review_role') == 'independent'
                        and prior[0]['review'].get('review_mode') == 'independent'
                        and request['review_role'] == 'independent' and review.get('review_mode') == 'independent',
                        'Reconciliation needs the exact recorded independent judgment and original payload hash', 'IDENTITY_MISMATCH')
                existing_review = {**deepcopy(existing_review), 'source': prior[0]['source']}
            require(not any(key in review and review[key] != value for key, value in
                    (('native_entry_id', item['id']), ('return_event', item['return_event']), ('stage_outcome', stage_outcome))),
                    'Review payload conflicts with its exact return or stage outcome', 'IDENTITY_MISMATCH')
            review = {**review, 'native_review_origin': origin, 'local_writer': request['writer_session'],
                'review_role': request['review_role'], 'native_entry_id': item['id'], 'return_event': item['return_event']}
            if stage_outcome is not None:
                review['stage_outcome'] = stage_outcome
            if existing_review is not None:
                review['existing_review'] = existing_review
            result = self.state.review_task(item['task_id'], request['disposition'], review, request['source'])
            item = self.get(item['id'])  # Exact low-level review also projects handling onto this entry.
            recorded_at = now()
            if existing_review is None:
                item['reviewed_at'] = recorded_at; item['review_disposition'] = request['disposition']
            else:
                item.setdefault('reviewed_at', existing_review['at'])
                item.setdefault('review_disposition', request['disposition'])
            review_hash = native_hash(request)
            preserve_stage = existing_review is not None and item.get('stage_review') and item.get('review_request_sha256')
            if existing_review is None or not item.get('review_request_sha256'):
                item['review_request_sha256'] = review_hash
            if stage_outcome is not None or request['disposition'] in ('adopted', 'abandoned'):
                item['return_review'] = {'return_event': item['return_event'],
                    'outcome': stage_outcome or 'handled', 'task_disposition': request['disposition'],
                    'reviewer': review['reviewer'], 'review_role': request['review_role'],
                    'review_mode': review.get('review_mode'), 'reviewed_at': item['reviewed_at'],
                    'recorded_at': recorded_at, 'review_request_sha256': review_hash}
                if existing_review is not None:
                    item['return_review']['existing_review'] = existing_review
            if (item['provider'] == LOCAL_PROVIDER and item.get('origin') != 'observed' and item.get('return_event')
                    and request['review_role'] == 'independent' and review.get('review_mode') == 'independent' and review.get('artifacts')
                    and not preserve_stage):
                item['stage_review'] = {'reviewer': review['reviewer'], 'mode': 'independent',
                    'artifact_evidence': self.state._file_evidence(review['artifacts'], 'runtime')}
            if stage_outcome in ('unresolved', 'revise'):
                item.pop('stage_review', None)
            saved = self._save(item, request['source'])
            decision = None
            if request.get('decision'):
                decision = self.state.decide(work['id'], work['revision'], request['decision'], request['source'], actor=request.get('decision_actor'))
            return {'entry': saved, 'task': result, 'decision': decision}

    def release(self, request):
        with self.state.transaction():
            item = self.get(request['id'])
            require(request.get('expected') == item['revision'] and item.get('native_terminal_status') in TERMINAL
                    and item.get('return_event') and not item.get('conflict') and request.get('evidence'),
                    'Release needs exact native terminal, saved result and actual shared-object release evidence', 'RESOURCE_OCCUPIED')
            item.update(resources_released=True, release_evidence=request['evidence'])
            return self._save(item, request['source'])

    def resolve(self, request):
        """Adjudicate observed conflicts, or close a truly terminal unadmitted package."""
        with self.state.transaction():
            item = self.get(request['id']); task = self.state.task(item['task_id'])
            require(request.get('expected') == item['revision'], 'Stale native conflict revision', 'REVISION_CONFLICT')
            self.runtime._check_owner(task, request.get('coordinator'), request.get('owner_revision'))
            evidence = request.get('evidence', {})
            cancel = request.get('cancel_unadmitted') is True
            require((item.get('conflict') or cancel) and evidence.get('reference') and evidence.get('basis') and evidence.get('reviewer'),
                    'Explicit actual terminal/conflict assessment and source reference required')
            selected = request.get('selected_terminal_status')
            require(selected in {ref['status'] for ref in item.get('terminal_observations', [])},
                    'Resolution must select an actually observed native terminal', 'IDENTITY_MISMATCH')
            if cancel:
                require(item.get('native_terminal_status') in TERMINAL and item['phase'] not in ('prepared', 'unconfirmed')
                        and (item.get('conflict') or selected == item['native_terminal_status'])
                        and not item.get('accepted_by') and not item.get('return_event') and not item.get('dispatch_key'),
                        'Only a confirmed terminal package that was never admitted may close without a result', 'ENTRY_NOT_READY')
                require(evidence.get('business_not_executed') is True
                        and evidence.get('thread_id') == item.get('thread_id') and evidence.get('turn_id') == item.get('turn_id')
                        and {'status': selected, 'receipt_id': evidence.get('terminal_receipt_id')} in item['terminal_observations'],
                        'Unadmitted closure needs exact thread/turn, terminal receipt and affirmative non-execution evidence', 'IDENTITY_MISMATCH')
            before = item.get('conflict'); item.update(native_terminal_status=selected, conflict=None, conflict_resolution=deepcopy(evidence))
            item.pop('review_request_sha256', None)
            if cancel:
                item.update(phase='cancelled', resources_released=True,
                    closure_coverage='Actual native terminal and never-admitted state checked; business non-execution is declared with its source; sent budget and original facts retained')
                queue = self.runtime._get('runtime_review_queue', 'task_id', task['id'], True)
                if queue and queue['body'].get('native_entry_id') == item['id']:
                    self.db.execute("UPDATE runtime_review_queue SET status='resolved',revision=revision+1,at=? WHERE task_id=?", (now(), task['id']))
            elif item.get('return_event'):
                if task['status'] == 'adopted':
                    self.state.review_task(task['id'], 'returned', {'answer': 'Native conflict requires renewed review', 'evidence': evidence}, request['source'])
                item.pop('return_review', None)
                item.pop('stage_review', None)
                self._queue(item, request['source'])
                queue = self.runtime._get('runtime_review_queue', 'task_id', task['id'], True)
                if queue and queue['body'].get('native_entry_id') == item['id'] and queue['body'].get('return_event') == item['return_event']:
                    self.db.execute("UPDATE runtime_review_queue SET status='needs_review',revision=revision+1,at=? WHERE task_id=?", (now(), task['id']))
            self.state.event(item['work_id'], 'native_entry_conflict_resolved', request['source'], {'id': item['id'], 'before': before, 'assessment': request})
            return self._save(item, request['source'])

    def recover(self, work, after=0, limit=12):
        require(type(after) is int and after >= 0 and 1 <= limit <= 100, 'Bounded recovery cursor/limit required')
        self.state.get(work)
        rows = self.db.execute("SELECT seq,kind,payload,at FROM events WHERE work_id=? AND seq>? AND kind IN ('native_entry_receipt','native_entry_result','native_entry_state') ORDER BY seq LIMIT ?", (work, after, limit + 1)).fetchall()
        entries = self.actions(work)
        projected = {row['task_id']: {**dict(row), 'body': json.loads(row['body'])}
            for row in self.db.execute("SELECT task_id,owner,status,revision,body,at FROM runtime_review_queue WHERE json_extract(body,'$.native_entry_id') IS NOT NULL")
            if self.state.task(row['task_id'])['work_id'] == work}
        pending, legacy = [], []
        for item in entries:
            row = projected.get(item['task_id'])
            current = bool(row and row['body'].get('native_entry_id') == item['id']
                           and row['body'].get('return_event') == item.get('return_event'))
            if not item.get('return_event'):
                if current and row['status'] != 'resolved':
                    pending.append({**row, 'queue_projected': True})
                continue
            if self._return_handled(item) and not item.get('conflict'):
                continue
            review = item.get('return_review', {})
            detail = {**row} if current else {'task_id': item['task_id'], 'owner': item['review_owner'],
                'revision': None, 'at': item.get('reviewed_at', item['prepared_at']),
                'body': {'native_entry_id': item['id'], 'return_event': item['return_event'], 'policy_id': item.get('policy_id')}}
            detail.update(queue_projected=current, entry_revision=item['revision'],
                status='revise' if review.get('outcome') == 'revise' else
                       row['status'] if current and row['status'] != 'resolved' else 'pending')
            if not current and not review:
                # The old task queue omitted historical stages. Keep their
                # unknown handling visible separately until explicitly reconciled;
                # timestamps and review files alone state no handling decision.
                detail.update(status='legacy_unresolved', stage_review=item.get('stage_review'),
                    review_disposition=item.get('review_disposition'), return_event=item['return_event'])
                legacy.append(detail)
            else:
                pending.append(detail)
        pending.sort(key=lambda row: (row['at'], row['body']['native_entry_id']))
        legacy.sort(key=lambda row: (row['at'], row['body']['native_entry_id']))
        receivers = {row['task_id']: row['owner'] for row in pending}
        attention = []
        for item in entries:
            missing_return = bool(item.get('native_terminal_status') and not item.get('return_event')
                                  and not item.get('entry_only') and item['phase'] != 'cancelled')
            if not (self.live(item) or item.get('conflict') or missing_return or
                    item.get('origin') == 'observed' and not item.get('return_event')):
                continue
            detail = {'id': item['id'], 'task': item['task_id'], 'phase': item['phase'], 'thread': item.get('thread_id'),
                'receiver': receivers.get(item['task_id'], item['review_owner']),
                'last_observed_at': item.get('last_observed_at', item['prepared_at']),
                'entry': {'command': 'native-entry show', 'request': {'id': item['id']}}}
            issues = []
            for key in ('native_terminal_status', 'return_event', 'conflict'):
                if item.get(key):
                    detail['native_status' if key == 'native_terminal_status' else key] = item[key]
            if item.get('conflict'):
                issues.append('native_conflict')
            if missing_return:
                detail['missing_return'] = True
                issues.append('missing_return')
            # accepted_by is also a producer fact on observed/ended entries.
            # Only a current execution claim expects an executor binding.
            if (item.get('origin') != 'observed' and not item.get('entry_only') and not item.get('return_event')
                    and not item.get('native_terminal_status') and item['phase'] in ('sent', 'accepted')
                    and (item.get('accepted_by') or item['provider'] == LOCAL_PROVIDER)):
                binding = self.state.binding(item.get('thread_id'))
                if not binding:
                    detail['binding'] = {'status': 'missing', 'expected_work': work, 'expected_role': 'executor'}
                    issues.append('missing_binding')
                elif binding['work_id'] != work or binding['role'] != 'executor':
                    detail['binding'] = {'status': 'conflict', **binding, 'expected_work': work, 'expected_role': 'executor'}
                    issues.append('binding_conflict')
            if issues:
                detail['issues'] = issues
            attention.append(detail)
        attention.sort(key=lambda item: (not bool(item.get('issues')), item['last_observed_at'], item['id']))
        return {'work': work, 'events': [{'seq': row['seq'], 'kind': row['kind'], 'at': row['at'],
            'id': json.loads(row['payload'])['id']} for row in rows[:limit]],
            'next_cursor': rows[min(len(rows), limit)-1]['seq'] if rows else after, 'has_more': len(rows) > limit,
            'attention': attention[:limit], 'attention_count': len(attention), 'omitted_attention': max(0, len(attention)-limit),
            'issue_counts': dict(Counter(issue for item in attention for issue in item.get('issues', []))),
            'pending_reviews': pending[:limit],
            'pending_review_count': len(pending), 'omitted_reviews': max(0, len(pending)-limit),
            'legacy_returns': legacy[:limit], 'legacy_return_count': len(legacy),
            'omitted_legacy_returns': max(0, len(legacy)-limit),
            'coverage': 'Read-only same-store unprocessed attention plus incremental events; native freshness/notification delivery is not implied'}

    def call(self, operation, request):
        require(isinstance(request, dict), 'Native entry request must be an object')
        if operation == 'show': return self.get(request['id'])
        if operation == 'recover': return self.recover(request['work'], request.get('after', 0), request.get('limit', 12))
        if operation == 'check':
            item = self.get(request['id'])
            require(item.get('origin') != 'observed' and not item.get('entry_only') and item['phase'] == 'accepted'
                    and not item.get('conflict') and not item.get('native_terminal_status') and not item.get('return_event'),
                    'Only a currently admitted ordinary task may continue')
            self._identity(item, request)
            binding = self.state.binding(item['thread_id'])
            require(binding and binding['work_id'] == item['work_id'] and binding['role'] == 'executor',
                    'Current executor binding required; reconcile the recorded binding before continuation', 'IDENTITY_MISMATCH')
            task = self._current(item)
            return {'entry': item, 'task': task, 'pending_inputs': self.state.pending(item['work_id']), 'allowed': True}
        if operation == 'input':
            require(request.get('raw_source') is not None, 'Explicit actual native input provenance required; unknown is allowed')
            return self.state.input(request['session'], request['turn'], request['prompt'], raw_source=request['raw_source'])
        functions = {'prepare': self.prepare, 'fact': self.fact, 'accept': self.accept, 'receive': self.receive,
                     'finish': self.finish,
                     'return': self.returned, 'review': self.review, 'release': self.release, 'resolve': self.resolve}
        require(operation in functions, 'Unsupported native-entry operation')
        try:
            result = functions[operation](request)
            if operation in ('return', 'review', 'receive', 'finish') and request.get('next_prepare'):
                # Separate transactions preserve a real return/review if an
                # already-authorized next package has meanwhile become stale.
                try:
                    result['next_preparation'] = self.prepare(request['next_prepare'])
                except HarnessError as exc:
                    result['next_preparation'] = {'send_once': False, 'error': {'code': exc.code, 'message': str(exc)}}
            return result
        except (KeyError, TypeError, AttributeError) as exc:
            raise HarnessError('Invalid ordinary native request shape: ' + str(exc), 'INVALID_INPUT') from exc
