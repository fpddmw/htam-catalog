"""Scoped native-input attention derived from the existing inbox and resolution events.

Observed userMessage is an unverified native source, never human authorization.
This fences subsequent managed actions only; already running native turns remain native.
"""
import hashlib
import json

from .common import encoded, require
from . import goals


class NativeInputs:
    def __init__(self, state): self.state, self.db = state, state.db

    def quarantine_receipt(self, host, thread):
        row = self.db.execute("SELECT seq,payload FROM events WHERE kind='native_input_thread_quarantined' AND json_extract(payload,'$.host_id')=? AND json_extract(payload,'$.thread_id')=? ORDER BY seq DESC LIMIT 1", (host, thread)).fetchone()
        return {'event_seq': row['seq'], **json.loads(row['payload'])} if row else None

    def routes(self, host, thread, archived=False):
        rows = self.db.execute('SELECT * FROM runtime_routes WHERE host_id=? AND thread_id=? ORDER BY task_id', (host, thread)).fetchall()
        if not rows and archived:
            receipt = self.quarantine_receipt(host, thread)
            if receipt:
                return receipt['routes']
        require(rows, 'Native input targets an unmapped exact host/thread', 'IDENTITY_MISMATCH')
        return [{**dict(row), 'body': json.loads(row['body'])} for row in rows]

    def handoff_context(self, task):
        work = self.state.get(task['work_id'])
        return {'work': {key: work.get(key) for key in ('id', 'request_revision', 'request', 'objective', 'constraints',
                'execution_policy', 'representation', 'requirements')},
            'task': {key: task.get(key) for key in ('id', 'work_id', 'work_revision', 'body')},
            'coverage': 'Current actual work/task definition and recorded professional evidence; original files remain at their exact references'}

    def validate(self, fact):
        self.routes(fact['hostId'], fact.get('threadId'))
        if fact['kind'] == 'thread_timeline_observed':
            require(isinstance(fact.get('coverage'), dict) and isinstance(fact.get('turns'), list)
                    and all(isinstance(turn, dict) and isinstance(turn.get('turnId'), str) and turn['turnId'] for turn in fact['turns']),
                    'Timeline requires exact read coverage and actual turn identities')
            return
        require(all(isinstance(fact.get(key), str) and fact[key] for key in ('turnId', 'itemId', 'contentSha256'))
                and len(fact['contentSha256']) == 64 and all(char in '0123456789abcdef' for char in fact['contentSha256']) and isinstance(fact.get('textBlocks'), list)
                and isinstance(fact.get('contentTypes'), list), 'Native message requires actual item/content identity')
        for block in fact['textBlocks']:
            require(isinstance(block, dict) and type(block.get('index')) is int and isinstance(block.get('text'), str)
                    and type(block.get('originalBytes')) is int and block['originalBytes'] >= len(block['text'].encode())
                    and type(block.get('truncated')) is bool,
                    'Native text block requires explicit byte/truncation coverage')
            if not block['truncated']:
                require(len(block['text'].encode()) == block['originalBytes']
                        and hashlib.sha256(block['text'].encode()).hexdigest() == block.get('textSha256'),
                        'Native observed text/hash differs', 'INPUT_IDENTITY_CONFLICT')

    def _owned(self, fact, text=False, live_only=False):
        methods = "method IN ('turn/start','turn/steer')" if text else "method='turn/start'"
        for row in self.db.execute("SELECT * FROM runtime_actions WHERE host_id=? AND thread_id=? AND turn_id=? AND " + methods,
                                   (fact['hostId'], fact['threadId'], fact.get('turnId'))):
            body = json.loads(row['body'])
            if not body.get('ack_event') or not body.get('attempt_event'):
                continue
            if live_only and body.get('phase') not in ('guarded', 'attempted', 'acknowledged', 'unknown'):
                continue
            if not text:
                return True
            params = body['params']; original = params.get('input', [])
            if not params.get('clientUserMessageId') or fact.get('clientUserMessageId') != params['clientUserMessageId'] or not original:
                continue
            blocks = fact['textBlocks']
            if fact['contentTypes'] != ['text'] or len(blocks) != len(original) or fact.get('contentTruncated'):
                continue
            if all(not block['truncated'] and block['index'] == index and block['text'] == item.get('text')
                   and item.get('type') == 'text' for index, (block, item) in enumerate(zip(blocks, original))):
                return True
        return False

    @staticmethod
    def complete(fact):
        coverage = fact.get('coverage', {})
        anchor = fact.get('requestedAnchor', coverage.get('requestedAnchor'))
        return (coverage.get('complete') is True and coverage.get('gap') is False and coverage.get('itemsComplete') is True
                and coverage.get('textTruncated') is False and (not anchor or coverage.get('anchorFound') is True))

    def _signals(self, host, thread):
        self.routes(host, thread, archived=True)
        records, latest_scan, scans, gap_epochs, prior_gap, messages, activity = {}, None, {}, {}, {}, {}, {}
        def native_activity(fact, turn_id, status, event_id):
            if not turn_id or self._owned({**fact, 'turnId': turn_id}): return
            item = activity.setdefault(turn_id, {'turn_id': turn_id, 'terminal_statuses': set(), 'evidence_event': event_id})
            item['evidence_event'] = event_id
            if status in ('completed', 'interrupted', 'failed'): item['terminal_statuses'].add(status)
        rows = self.db.execute("SELECT rowid,* FROM runtime_inbox WHERE host_id=? AND thread_id=? ORDER BY rowid", (host, thread))
        for row in rows:
            fact = json.loads(row['body']); kind = fact['kind']
            signal = None
            if kind == 'thread_timeline_observed' and row['status'] != 'rejected' and fact.get('source') == 'public_app_server_history':
                for turn in fact.get('turns', []): native_activity(fact, turn.get('turnId'), turn.get('status'), row['event_id'])
            elif kind in ('turn_started', 'turn_terminal') and fact.get('source') == 'public_app_server_notification' and fact.get('nativeMethod') in ('turn/started', 'turn/completed'):
                native_activity(fact, fact.get('turnId'), fact.get('nativeStatus'), row['event_id'])
            if kind == 'native_user_message_observed' and row['status'] != 'rejected':
                identity = {'turn_id': fact['turnId'], 'item_id': fact['itemId'], 'content_sha256': fact['contentSha256']}
                key = goals.fingerprint(identity)
                quality = (not fact.get('contentTruncated'), sum(not block['truncated'] for block in fact['textBlocks']), sum(len(block['text'].encode()) for block in fact['textBlocks']))
                prior = messages.get(key)
                if prior is None or quality > prior['quality']:
                    messages[key] = {'kind': 'unverified_native_input', 'identity': identity, 'fact': fact,
                        'event_id': row['event_id'], 'rowid': prior['rowid'] if prior else row['rowid'], 'quality': quality}
            elif kind == 'thread_timeline_observed' and row['status'] != 'rejected':
                latest_scan = {'event_id': row['event_id'], 'rowid': row['rowid'], 'fact': fact}
                anchor_key = goals.fingerprint(fact.get('requestedAnchor', fact['coverage'].get('requestedAnchor')))
                gap = not self.complete(fact)
                if gap and anchor_key not in gap_epochs:
                    gap_epochs[anchor_key] = 1
                elif anchor_key in gap_epochs and prior_gap.get(anchor_key) != gap:
                    gap_epochs[anchor_key] += 1
                prior_gap[anchor_key] = gap
                scans[anchor_key] = latest_scan
            elif kind in ('turn_started', 'turn_terminal') and row['status'] == 'rejected' and not self._owned(fact):
                if fact.get('source') == 'public_app_server_notification' and fact.get('nativeMethod') in ('turn/started', 'turn/completed'):
                    signal = {'kind': 'unattributed_native_activity', 'identity': {'turn_id': fact.get('turnId'), 'native_status': fact.get('nativeStatus')}, 'fact': fact}
            if signal:
                key = goals.fingerprint({'kind': signal['kind'], 'identity': signal['identity']})
                records.setdefault(key, {**signal, 'key': key, 'event_id': row['event_id'], 'rowid': row['rowid']})
        for scan in scans.values():
            fact = scan['fact']
            for turn in fact.get('turns', []):
                probe = {**fact, 'turnId': turn.get('turnId')}
                if not self._owned(probe):
                    identity = {'turn_id': turn.get('turnId'), 'native_status': turn.get('status')}
                    key = goals.fingerprint({'kind': 'unattributed_native_turn', 'identity': identity})
                    records.setdefault(key, {'kind': 'unattributed_native_turn', 'identity': identity, 'fact': fact,
                        'key': key, 'event_id': scan['event_id'], 'rowid': scan['rowid']})
            if not self.complete(fact):
                identity = {'requested_anchor': fact.get('requestedAnchor'), 'coverage': fact['coverage']}
                key = goals.fingerprint({'kind': 'coverage_gap', 'identity': identity})
                records[key] = {'kind': 'coverage_gap', 'identity': identity, 'fact': fact, 'key': key,
                    'event_id': scan['event_id'], 'rowid': scan['rowid']}
        for item in messages.values():
            if not self._owned(item['fact'], text=True):
                key = goals.fingerprint({'kind': item['kind'], 'identity': item['identity']})
                records[key] = {**item, 'key': key}
        unresolved = [{**item, 'terminal_statuses': sorted(item['terminal_statuses'])} for item in activity.values() if len(item['terminal_statuses']) != 1]
        return records, latest_scan, gap_epochs, unresolved

    def view(self, host, thread, *, limit=8, offset=0, event_id=None):
        require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and offset >= 0, 'Native input page must be 1..100 with nonnegative offset')
        signals, scan, gap_epochs, unresolved = self._signals(host, thread)
        identity = {key: value['identity'] for key, value in sorted(signals.items())}
        epoch = goals.fingerprint({'inputs': identity, 'coverage_epochs': gap_epochs})
        row = self.db.execute("SELECT seq,payload FROM events WHERE kind='native_input_attention_resolved' AND json_extract(payload,'$.host_id')=? AND json_extract(payload,'$.thread_id')=? ORDER BY seq DESC LIMIT 1", (host, thread)).fetchone()
        resolution = {'event_seq': row['seq'], **json.loads(row['payload'])} if row else None
        pending = bool(signals) and (not resolution or resolution['epoch'] != epoch)
        selected = sorted(signals.values(), key=lambda item: item['rowid'])[offset:offset + limit]
        items = []
        for item in selected:
            fact = item['fact']
            display = {key: item[key] for key in ('key', 'kind', 'identity', 'event_id')}
            display['origin_status'] = 'unknown; native role/classification is not human authorization'
            if fact['kind'] == 'native_user_message_observed':
                display['text_blocks'] = [{**block, 'text': block['text'][:550], 'display_truncated': len(block['text']) > 550} for block in fact['textBlocks'][:4]]
                display['omitted_text_blocks'] = max(0, len(fact['textBlocks']) - 4)
            items.append(display)
        if event_id:
            exact = self.db.execute('SELECT body FROM runtime_inbox WHERE event_id=? AND host_id=? AND thread_id=?', (event_id, host, thread)).fetchone()
            if not exact and event_id.startswith('event:') and event_id[6:].isdigit():
                exact = self.db.execute("SELECT payload AS body FROM events WHERE seq=? AND kind='native_input_thread_quarantined' AND json_extract(payload,'$.host_id')=? AND json_extract(payload,'$.thread_id')=?", (int(event_id[6:]), host, thread)).fetchone()
            require(exact, 'Exact native input evidence is outside this thread', 'IDENTITY_MISMATCH')
            items = [{'exact_observation': json.loads(exact['body']), 'origin_status': 'unknown; explicit origin review is separate'}]
        quarantine = self.quarantine_receipt(host, thread)
        active = sorted({item['turn_id'] for item in unresolved} | ({turn.get('turnId') for turn in scan['fact'].get('turns', []) if turn.get('status') not in ('completed', 'interrupted', 'failed')
            and not self._owned({**scan['fact'], 'turnId': turn.get('turnId')}, live_only=True)} if scan else set()))
        return {'host_id': host, 'thread_id': thread, 'epoch': epoch, 'attention_count': len(signals), 'pending': pending,
            'quarantine': {'event_seq': quarantine['event_seq'], 'context_sha256': quarantine['context_sha256'],
                'entry': 'runtime-inputs --host ' + host + ' --thread ' + thread + ' --event event:' + str(quarantine['event_seq']),
                'effect': 'Retired thread remains fenced; exact old evidence is preserved'} if quarantine else None,
            'active_coverage_gaps': sum(item['kind'] == 'coverage_gap' for item in signals.values()),
            'items': items, 'omitted': max(0, len(signals) - offset - len(selected)), 'resolution': resolution,
            'latest_scan': {'event_id': scan['event_id'], 'coverage': scan['fact']['coverage'], 'complete': self.complete(scan['fact']),
                'newer_observations': any(item['rowid'] > scan['rowid'] for item in signals.values())} if scan else None,
            'foreign_active_turns': active[:limit], 'foreign_active_turn_count': len(active), 'omitted_foreign_active_turns': max(0, len(active) - limit),
            'unresolved_native_activity': unresolved[:limit], 'omitted_unresolved_activity': max(0, len(unresolved) - limit),
            'input_coverage': 'Persisted native userMessage and turn metadata only. Arbitrary function outputs/delegation metadata are not parsed as human input.',
            'native_effect': 'Attention constrains subsequent managed actions; already running native work is not stopped by this receipt'}

    def assert_ready(self, route, task):
        if not route.get('thread_id'):
            return None
        view = self.view(route['host_id'], route['thread_id'], limit=1)
        require(not view['quarantine'], 'This native thread was explicitly retired; recover on a different thread', 'NATIVE_THREAD_QUARANTINED')
        require(not view['pending'], 'Native input/coverage attention needs exact thread coordination: ' + view['epoch'], 'NATIVE_INPUT_ATTENTION')
        require(not view['foreign_active_turns'], 'Exact thread has active/unknown activity outside a current owned action', 'NATIVE_THREAD_ACTIVE')
        if view['attention_count'] and not (view['resolution'] or {}).get('review', {}).get('nonblocking_input'):
            decision = self.db.execute("SELECT MAX(seq) FROM events WHERE work_id=? AND kind='overall_decision'", (task['work_id'],)).fetchone()[0]
            require(decision and decision > view['resolution']['event_seq'], 'Refresh the work decision after exact native-input coordination', 'NATIVE_INPUT_DECISION_REQUIRED')
        return {'epoch': view['epoch'], 'resolution_event': view['resolution']['event_seq'] if view['resolution'] else None}

    def resolve(self, host, thread, epoch, body, source):
        require(isinstance(body, dict) and all(isinstance(body.get(key), str) and body[key].strip() for key in ('source_ref', 'reason'))
                and body.get('disposition') in ('original_scope_confirmed', 'correction_recorded', 'not_applicable'),
                'Native input coordination needs disposition and semantic source/reason')
        from .runtime import Runtime
        runtime = Runtime(self.state)
        with self.state.transaction():
            view = self.view(host, thread)
            require(not view['quarantine'], 'Retired thread cannot be released by an ordinary input resolution', 'NATIVE_THREAD_QUARANTINED')
            require(epoch == view['epoch'] and body.get('reviewed_set_sha256') == epoch
                    and body.get('reviewed_count') == view['attention_count'], 'Native input epoch/set changed', 'REVISION_CONFLICT')
            require(view['latest_scan'] and view['latest_scan']['complete']
                    and not view['active_coverage_gaps'] and not view['latest_scan']['newer_observations']
                    and body.get('coverage_event_id') == view['latest_scan']['event_id'], 'Exact complete timeline readback is required; coverage gap remains', 'NATIVE_INPUT_COVERAGE')
            routes = self.routes(host, thread); works = set()
            nonblocking = body.get('nonblocking_input')
            if nonblocking is not None:
                require(isinstance(nonblocking, dict) and nonblocking.get('kind') in ('question', 'suggestion')
                        and all(nonblocking.get(key) for key in ('quote', 'reason', 'impact', 'source_ref')),
                        'Nonblocking native input needs exact quote, source and impact judgment')
                signals, _, _, _ = self._signals(host, thread)
                quotes = ['\n'.join(block['text'] for block in signal['fact'].get('textBlocks', []))
                          for signal in signals.values() if signal['kind'] == 'unverified_native_input']
                require(quotes and all(text and nonblocking['quote'] in text for text in quotes)
                        and all(signal['kind'] in ('unverified_native_input', 'unattributed_native_turn') for signal in signals.values()),
                        'A question assessment cannot dismiss different messages or unknown activity', 'NATIVE_INPUT_ATTENTION')
            for route in routes:
                task = self.state.task(route['task_id']); works.add(task['work_id'])
                runtime._check_owner(task, body.get('reviewer'), body.get('owner_revisions', {}).get(task['work_id']))
            receipt = {'host_id': host, 'thread_id': thread, 'epoch': epoch, 'reviewed_count': view['attention_count'],
                'review': body, 'authority': 'Existing authorized scope only; seeing native input does not grant new permission'}
            seq = self.state.event(next(iter(works)) if len(works) == 1 else None, 'native_input_attention_resolved', source, receipt)
            for route in routes:
                if nonblocking:
                    continue  # Attention is recorded; a question does not invalidate authorized work.
                route['body']['scope_revision'] += 1
                route['body']['native_input_resolution_event'] = seq
                self.db.execute('UPDATE runtime_routes SET revision=revision+1,body=? WHERE task_id=?', (encoded(route['body']), route['task_id']))
                for action in runtime._actions(route['task_id']):
                    if action['body']['phase'] == 'prepared' and action['method'] != 'turn/interrupt':
                        action['body'].update(phase='invalidated', invalidated_by='native-input:' + str(seq)); runtime._save(action)
                        if action['dispatch_key']:
                            self.db.execute("UPDATE dispatches SET status='invalidated' WHERE key=? AND status='prepared'", (action['dispatch_key'],))
                            if self.state.task(route['task_id'])['status'] == 'reserved':
                                self.db.execute("UPDATE tasks SET status='proposed' WHERE id=?", (route['task_id'],))
        return self.view(host, thread)

    def quarantine(self, host, thread, epoch, body, source):
        """Retire an irrecoverable anchor, preserving claims and requiring a new route/context."""
        from .runtime import Runtime, LIVE, TERMINAL
        require(isinstance(body, dict) and all(isinstance(body.get(key), str) and body[key].strip() for key in ('source_ref', 'reason', 'handoff_note')),
                'Thread quarantine needs actual boundary evidence, reason and handoff context note')
        runtime = Runtime(self.state)
        with self.state.transaction():
            view = self.view(host, thread)
            require(not view['quarantine'] or body.get('previous_quarantine_event') == view['quarantine']['event_seq'],
                    'Refresh must bind the exact previous quarantine receipt', 'REVISION_CONFLICT')
            require(epoch == view['epoch'] and body.get('reviewed_set_sha256') == epoch and body.get('reviewed_count') == view['attention_count'],
                    'Native quarantine epoch/set changed', 'REVISION_CONFLICT')
            scan = view['latest_scan']
            require(scan and body.get('coverage_event_id') == scan['event_id'] and not scan['newer_observations'],
                    'Quarantine requires exact latest retained-history observation', 'NATIVE_INPUT_COVERAGE')
            fact = json.loads(self.db.execute('SELECT body FROM runtime_inbox WHERE event_id=?', (scan['event_id'],)).fetchone()[0])
            coverage = fact['coverage']; gaps = coverage.get('gaps', [])
            require(view['active_coverage_gaps'] and gaps and all(gap.get('code') in ('anchor_turn_not_found', 'anchor_item_not_found') for gap in gaps)
                    and coverage.get('exhausted') is True and coverage.get('itemsComplete') is True and not coverage.get('bounded')
                    and not any(coverage.get(key) for key in ('textTruncated', 'contentTruncated', 'sourceTruncated', 'identityTruncated')),
                    'Only an exhausted retained-history read with an irrecoverable anchor may be quarantined', 'NATIVE_INPUT_COVERAGE')
            require(not view['foreign_active_turns'] and all(turn.get('status') in TERMINAL for turn in fact['turns']),
                    'Actual retained native activity is active/unknown', 'NATIVE_THREAD_ACTIVE')
            routes = self.routes(host, thread); contexts = {}; works = set()
            for route in routes:
                task = self.state.task(route['task_id']); works.add(task['work_id'])
                runtime._check_owner(task, body.get('reviewer'), body.get('owner_revisions', {}).get(task['work_id']))
                require(not any(action['body']['phase'] in LIVE for action in runtime._actions(task['id'])),
                        'Owned live/UNKNOWN actions must be reconciled before quarantine', 'ACTION_UNKNOWN')
                contexts[task['id']] = self.handoff_context(task)
            receipt = {'host_id': host, 'thread_id': thread, 'epoch': epoch, 'reviewed_count': view['attention_count'],
                'review': body, 'routes': routes, 'handoff_contexts': contexts, 'context_sha256': goals.fingerprint(contexts),
                'authority': 'Retire this thread only; no input authorized, native stop claimed, resource released or cost cleared'}
            seq = self.state.event(next(iter(works)) if len(works) == 1 else None, 'native_input_thread_quarantined', source, receipt)
            for route in routes:
                route['body']['scope_revision'] += 1
                route['body']['native_input_quarantine'] = seq
                self.db.execute('UPDATE runtime_routes SET revision=revision+1,body=? WHERE task_id=?', (encoded(route['body']), route['task_id']))
                for action in runtime._actions(route['task_id']):
                    if action['body']['phase'] == 'prepared':
                        action['body'].update(phase='invalidated', invalidated_by='native-quarantine:' + str(seq)); runtime._save(action)
                        if action['dispatch_key']:
                            self.db.execute("UPDATE dispatches SET status='invalidated' WHERE key=? AND status='prepared'", (action['dispatch_key'],))
                            if self.state.task(route['task_id'])['status'] == 'reserved':
                                self.db.execute("UPDATE tasks SET status='proposed' WHERE id=?", (route['task_id'],))
        return self.view(host, thread)
