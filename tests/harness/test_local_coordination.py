"""Targeted ordinary-path checks. All stores, native facts and files are fixtures."""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from harness.common import HarnessError, encoded, now
from harness import goals
from harness.native_entry import NativeEntry, LOCAL_PROVIDER, native_projection
from harness.runtime import Runtime, native_hash
from harness.state import State


class LocalCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', self.root)
        self.entry = NativeEntry(self.state)
        self.runtime = Runtime(self.state)
        self.state.update('w', 0, {'title': 'Existing local research', 'request': 'Use existing bounded evidence',
            'request_source': 'fixture human request', 'objective': 'Assess existing evidence', 'status': 'active'}, 'fixture')
        (self.root / 'result.md').write_text('Existing bounded result')
        self.state.bind('main', 'w', 'main', 'fixture')

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def task(self, identifier='t', **extra):
        return self.state.create_task(identifier, 'w', self.state.get('w')['revision'],
            {'expected': 'A bounded result', 'purpose': 'Use existing bounded evidence', **extra}, 'fixture')

    def receive_request(self, **extra):
        return {'id': 'existing', 'work': 'w', 'expected': self.state.get('w')['revision'], 'task': 't',
            'review_owner': 'local-main', 'source': 'fixture actual native read', 'receipt': {
                'id': 'actual-read', 'reference': 'fixture native read envelope', 'observed_at': '2026-10-23T08:00:00Z',
                'raw': {'threadId': 'native-worker', 'latestTurn': {'id': 'actual-turn', 'status': 'completed'}}}, **extra}

    @staticmethod
    def result(**extra):
        return {'answer': 'Existing evidence supports a bounded use', 'artifacts': ['result.md'], 'remaining': ['Retain original scope'], **extra}

    def prepare_local(self, identifier='local', thread='native-worker', **extra):
        return self.entry.prepare({'id': identifier, 'work': 'w', 'expected': self.state.get('w')['revision'],
            'task': 't', 'provider': LOCAL_PROVIDER, 'method': 'send_message',
            'params': {'threadId': thread, 'prompt': 'Continue the authorized bounded task'},
            'review_owner': 'local-main', 'entry_coverage': {'execution_location': 'Known fixture local environment'},
            'source': 'fixture authorized work', **extra})

    def local_ack(self, identifier='local', terminal=None):
        item = self.entry.get(identifier)
        raw = {'threadId': item['thread_id'], 'turnId': 'turn-' + identifier, 'admissionOutcome': 'started'}
        if terminal:
            raw['terminalStatus'] = terminal
        return self.entry.fact({'id': identifier, 'source': 'fixture exact native acknowledgement', 'receipt': {
            'id': 'send-' + identifier, 'kind': 'send', 'reference': 'fixture actual send',
            'observed_at': '2026-10-23T08:00:00Z', 'request_sha256': item['request_sha256'], 'raw': raw}})

    @staticmethod
    def delegation_read():
        return {'thread': {'id': 'native-worker'}, 'page': {'order': 'newest_first'},
            'turns': [{'id': 'received-turn', 'status': 'inProgress', 'items': [{'type': 'functionCallOutput',
                'id': 'received-message', 'name': 'send_message_to_thread', 'namespace': 'codex_app', 'output': {
                    'text': '<codex_delegation><source_thread_id>main</source_thread_id><input>Continue the authorized bounded task</input></codex_delegation>',
                    'truncated': False}}]}]}

    def stage_start(self):
        self.task(); self.runtime.owner('w', 'main', 0, 'fixture current owner')
        self.prepare_local(review_owner='main', coordinator='main', owner_revision=1, resources=['original-shared-object'])
        return self.local_ack()['entry']

    def stage_finish(self, identifier='local'):
        item = self.entry.get(identifier)
        return self.entry.finish({'id': identifier, 'expected': item['revision'], 'executor_session': 'native-worker',
            'turn_id': item['turn_id'], 'result': self.result(), 'source': 'fixture actual stage result'})['entry']

    def stage_review(self, mode='independent', identifier='local'):
        path = 'stage-review-' + identifier + '.json'
        (self.root/path).write_text('{"independent_review": true, "fixture": true}')
        item = self.entry.get(identifier)
        review = {**self.result(artifacts=[path]), 'reviewer': 'main', 'review_mode': mode}
        return self.entry.review({'id': identifier, 'expected': item['revision'], 'work_expected': self.state.get('w')['revision'],
            'disposition': 'returned', 'review': review, 'review_role': 'independent' if mode == 'independent' else 'coordination',
            'review_origin': {'author': 'main', 'provider': LOCAL_PROVIDER, 'reference': 'fixture existing independent stage review',
                'payload_sha256': native_hash(review)},
            'writer_session': 'main', 'source': 'fixture reuse actual stage review'})['entry']

    def prepare_feedback(self, identifier='feedback', **extra):
        return self.prepare_local(identifier, review_owner='main', coordinator='main', owner_revision=1,
            params={'threadId': 'native-worker', 'prompt': 'Continue after the reviewed real stage'}, **extra)

    def claim_feedback(self, identifier='feedback'):
        from harness.host import handle_hook
        message = self.entry.get(identifier)['params']
        event = {'hook_event_name': 'PreToolUse', 'session_id': 'main', 'tool_use_id': 'call-' + identifier,
            'tool_name': 'mcp__codex_app__send_message_to_thread', 'tool_input': message}
        self.assertEqual(handle_hook(self.state, event), {})
        return event

    def meaning(self, text, kind, **extra):
        record = self.state.input('main', kind + str(self.state.db.total_changes), text, raw_source={'kind': 'unknown_native_origin'})
        body = {'kind': kind, 'input_sha256': record['input_sha256'], 'scope_revision': 1, 'quote': text,
            'reviewer': 'local-main', 'source_ref': 'fixture original native text', 'reason': 'Semantic review of original text',
            'impact': 'Only the declared action scope', **extra}
        self.state.interpret_input(record['id'], 'w', self.state.get('w')['revision'], body, 'fixture semantic judgment')
        return record

    def origin(self, text):
        record = self.state.input('main', 'human-' + str(self.state.db.total_changes), text, raw_source={'kind': 'fixture_human'})
        (self.root / 'origin.txt').write_text('Fixture original human transcript: ' + text)
        assessment = self.state.assess_input(record['id'], 0, {'kind': 'human_direct', 'reviewer': 'local-main',
            'source_ref': 'fixture original transcript', 'quote': text, 'reason': 'Fixture reviewed source',
            'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'fixture')
        return record, assessment

    def test_receive_and_result_save_without_dispatch_plan_policy_or_owner(self):
        self.task()
        request = self.receive_request(result=self.result())
        returned = self.entry.call('receive', request)
        self.assertEqual(self.state.task('t')['status'], 'returned')
        self.assertTrue(returned['entry']['return_event'])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)
        self.assertFalse(returned['entry']['budget_claimed'])
        self.assertTrue(self.entry.receive(request)['duplicate'])
        self.assertEqual(self.entry.recover('w')['pending_reviews'][0]['owner'], 'local-main')

    def test_ordinary_result_survives_pause_and_missing_artifact(self):
        self.task()
        self.state.update('w', 1, {'status': 'paused'}, 'fixture explicit pause')
        saved = self.entry.receive(self.receive_request(result=self.result(artifacts=['missing.md'])))
        self.assertEqual(saved['entry']['phase'], 'returned')
        review = self.state.task('t')['body']['reviews'][-1]['review']
        self.assertEqual(review['artifact_issues'][0]['status'], 'unverified')

    def test_unknown_turn_stays_unknown_while_result_is_saved(self):
        self.task()
        request = self.receive_request(result=self.result())
        request['receipt']['raw'] = {'threadId': 'native-worker'}
        saved = self.entry.receive(request)['entry']
        self.assertIsNone(saved['turn_id'])
        self.assertIn('turnId', saved['missing_fields'])
        self.assertIsNone(saved['dispatch_key'])

    def test_question_is_attention_without_freezing_actions_or_granting_authority(self):
        self.task()
        record = self.meaning('How far has this task progressed?', 'question')
        self.assertEqual(len(self.state.pending('w')), 1)
        self.assertTrue(self.state.check_work('w', task='t')['allowed'])
        self.assertEqual(self.state.input_assessment(record['id'])['kind'], 'unknown')
        self.runtime.owner('w', 'old-main', 0, 'fixture')
        with self.assertRaises(HarnessError):
            self.runtime.owner('w', 'new-main', 1, 'fixture', {'mode': 'user_reassignment', 'enforce_writes': True,
                'expected_work_revision': 1, 'authorization': {'input_id': record['id']}})

    def test_pause_blocks_true_dependents_and_preserves_siblings(self):
        self.task('a'); self.task('b'); self.task('c', requires=['a'])
        self.meaning('Pause the A branch while we check its source', 'pause', scope='tasks', tasks=['a'])
        self.assertFalse(self.state.check_work('w', task='a')['allowed'])
        self.assertFalse(self.state.check_work('w', task='c')['allowed'])
        self.assertTrue(self.state.check_work('w', task='b')['allowed'])
        self.state.decide('w', 1, {'action': 'continue', 'reason': 'Independent B remains useful',
            'expected_gain': 'Continue unaffected evidence', 'tasks': ['b'], 'assessments': [{'requirement': 'overall',
                'state': 'open', 'remaining': ['Bounded source check'], 'supports': [], 'limitations': [], 'reason': 'Local source stage'}]}, 'fixture scoped continuation')

    def test_semantic_reassessment_requires_its_own_cas(self):
        self.task()
        record = self.meaning('Is there an update?', 'question')
        effect = self.state.input_effect(record['id'], 'w')
        changed = {**effect['assessment'], 'kind': 'pause', 'reason': 'Fixture alternate interpretation'}
        with self.assertRaises(HarnessError) as caught:
            self.state.interpret_input(record['id'], 'w', 1, changed, 'fixture stale semantic assessment')
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertTrue(self.state.check_work('w', task='t')['allowed'])

    def test_input_origin_change_invalidates_semantic_shortcut(self):
        self.task()
        record = self.meaning('Is there an update?', 'question')
        (self.root / 'origin.txt').write_text('Fixture source later clarified')
        self.state.assess_input(record['id'], 0, {'kind': 'human_direct', 'reviewer': 'local-main',
            'source_ref': 'fixture clarified source', 'quote': record['prompt'], 'reason': 'New source review',
            'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'fixture')
        self.assertIsNone(self.state.input_effect(record['id'], 'w'))
        self.assertFalse(self.state.check_work('w', task='t')['allowed'])

    def test_original_purpose_evidence_can_be_bound_and_adopted_without_old_dot(self):
        self.task()
        self.runtime.owner('w', 'local-main', 0, 'fixture current local coordinator')
        returned = self.entry.receive(self.receive_request(result=self.result()))['entry']
        contract = {'purpose': self.state.task('t')['body']['purpose'], 'checks': [{'id': 'current-result', 'kind': 'files', 'description': 'Current bounded result',
            'basis': 'Existing output for the original bounded use', 'inputs': [{'path': 'result.md'}]}]}
        change = {'kind': 'evidence', 'purpose_basis': contract['purpose'], 'reason': 'Bind existing result to original use',
            'impact': 'No new scientific quantity or use', 'source': 'fixture original task and existing result'}
        self.state.set_task_completion('t', self.state.get('w')['revision'], contract, 'fixture supplement', change)
        review = {**self.result(), 'reviewer': 'local-main', 'review_mode': 'self', 'intended_use': contract['purpose'],
            'native_entry_id': returned['id'], 'return_event': returned['return_event']}
        adopted = self.state.review_task('t', 'adopted', review, 'fixture local review')
        self.assertEqual(adopted['status'], 'adopted')
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 0)
        self.assertTrue(adopted['body']['reviews'][-1]['review']['completion_result']['passed'])
        (self.root / 'result.md').write_text('Changed after review')
        self.assertFalse(self.state.task('t')['body']['completion'] == contract)
        from harness.fulfillment import check_contract
        self.assertFalse(check_contract(self.root, self.state.task('t')['body']['completion'], self.state)['passed'])

    def test_new_purpose_still_requires_verified_human_change(self):
        self.task(); self.runtime.owner('w', 'old-dot', 0, 'fixture')
        contract = {'purpose': 'Publish a new public dataset', 'checks': [{'id': 'file', 'kind': 'files', 'description': 'Fixture publication output',
            'basis': 'Fixture output', 'inputs': [{'path': 'result.md'}]}]}
        with self.assertRaises(HarnessError) as caught:
            self.state.set_task_completion('t', 1, contract, 'fixture', {'kind': 'evidence',
                'purpose_basis': 'Use existing bounded evidence', 'reason': 'Claimed same use', 'impact': 'New publication', 'source': 'fixture'})
        self.assertEqual(caught.exception.code, 'AUTHORIZATION_REQUIRED')

    def test_observed_producer_cannot_label_self_review_independent(self):
        self.task(); self.entry.receive(self.receive_request(result=self.result()))
        with self.assertRaisesRegex(HarnessError, 'Known producer'):
            self.state.review_task('t', 'adopted', {**self.result(), 'reviewer': 'native-worker', 'review_mode': 'independent'}, 'fixture')

    def test_local_create_can_start_authorized_work_without_entry_only_or_decision(self):
        self.task()
        prepared = self.entry.prepare({'id': 'create', 'work': 'w', 'expected': 1, 'task': 't',
            'provider': LOCAL_PROVIDER, 'method': 'create', 'params': {'title': 'Bounded research', 'prompt': 'Read current evidence now',
                'target': {'type': 'projectless'}}, 'session_choice_reason': 'Independent useful work', 'review_owner': 'local-main',
            'entry_coverage': {'execution_location': 'Known fixture local environment'}, 'source': 'fixture authorized scope'})
        self.assertTrue(prepared['send_once'])
        self.assertFalse(prepared['entry']['entry_only'])
        received = self.entry.fact({'id': 'create', 'source': 'fixture exact first native read', 'receipt': {
            'id': 'created-read', 'kind': 'read', 'reference': 'fixture actual created thread', 'observed_at': '2026-10-23T08:00:00Z',
            'raw': {'threadId': 'created-worker', 'latestTurn': {'id': 'created-turn', 'status': 'inProgress'},
                'items': [{'id': 'created-message', 'turnId': 'created-turn', 'role': 'user',
                    'text': prepared['entry']['params']['prompt'], 'truncated': False}]}}})['entry']
        self.assertEqual(received['phase'], 'accepted')
        self.assertEqual(self.state.binding('created-worker')['role'], 'executor')

    def test_created_thread_delegation_keeps_exact_sender_prompt_and_envelope_checks(self):
        # Shape observed in the native create_thread read receipt, with fixture identities.
        self.task(); self.runtime.owner('w', 'main', 0, 'fixture current coordinator')
        self.entry.prepare({'id': 'create', 'work': 'w', 'expected': 1, 'task': 't',
            'provider': LOCAL_PROVIDER, 'method': 'create',
            'params': {'title': 'Bounded research', 'prompt': 'Continue the authorized bounded task',
                'target': {'type': 'projectless'}},
            'session_choice_reason': 'Independent useful work', 'review_owner': 'main',
            'coordinator': 'main', 'owner_revision': 1,
            'entry_coverage': {'execution_location': 'Known fixture local environment'}, 'source': 'fixture'})
        item = self.entry.get('create'); item['phase'] = 'unconfirmed'
        self.entry._save(item, 'fixture actual create sent')
        actual_shape = self.delegation_read()
        actual_shape['turns'][0]['items'][0]['name'] = 'create_thread'
        for boundary in ('source', 'prompt', 'truncated', 'malformed', 'namespace'):
            with self.subTest(boundary=boundary):
                raw = deepcopy(actual_shape); message = raw['turns'][0]['items'][0]
                if boundary == 'source': message['output']['text'] = message['output']['text'].replace('>main<', '>other<')
                elif boundary == 'prompt': message['output']['text'] = message['output']['text'].replace('bounded task', 'different task')
                elif boundary == 'truncated': message['output']['truncated'] = True
                elif boundary == 'malformed': message['output']['text'] += '<broken>'
                elif boundary == 'namespace': message['namespace'] = 'untrusted_provider'
                with self.assertRaises(HarnessError):
                    self.entry.fact({'id': 'create', 'source': 'fixture', 'receipt': {
                        'id': 'create-invalid-' + boundary, 'kind': 'read', 'reference': 'fixture native read',
                        'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})
                self.assertEqual(self.entry.get('create')['phase'], 'unconfirmed')
                self.assertIsNone(self.state.binding('native-worker'))
        received = self.entry.fact({'id': 'create', 'source': 'fixture exact native create read', 'receipt': {
            'id': 'create-agent-read', 'kind': 'read', 'reference': 'fixture native read',
            'observed_at': '2026-10-24T08:00:00Z', 'raw': actual_shape}})['entry']
        self.assertEqual(received['phase'], 'accepted')
        self.assertEqual(received['turn_id'], 'received-turn')
        self.assertEqual(self.state.binding('native-worker')['role'], 'executor')

    def test_history_consumes_bound_terminal_without_relabeling_latest_turn(self):
        self.task(); self.prepare_local(); self.local_ack()
        raw = {'thread': {'id': 'native-worker'}, 'page': {'order': 'newest_first'}, 'turns': [
            {'id': 'newer-turn', 'status': 'inProgress', 'items': []},
            {'id': 'turn-local', 'status': 'completed', 'items': []}]}
        projected = native_projection(raw, 'native-worker', 'turn-local')
        self.assertEqual(projected['latestTurn'], {'id': 'newer-turn', 'status': 'inProgress'})
        self.assertEqual(projected['turnId'], 'turn-local')
        self.assertEqual(native_projection(raw, 'native-worker')['latestTurn'], projected['latestTurn'])
        receipt = {'id': 'history-read', 'kind': 'read', 'reference': 'fixture actual multi-turn history',
                   'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}
        item = self.entry.fact({'id': 'local', 'source': 'fixture', 'receipt': receipt})['entry']
        self.assertEqual(item['turn_id'], 'turn-local')
        self.assertEqual(item['native_terminal_status'], 'completed')
        self.assertFalse(self.entry.live(item))
        saved = json.loads(self.state.db.execute("SELECT payload FROM events WHERE kind='native_entry_receipt' ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(saved['receipt']['raw'], raw)

    def test_history_keeps_bound_running_turn_live_when_newest_is_terminal(self):
        self.task(); self.prepare_local(); self.local_ack()
        raw = {'thread': {'id': 'native-worker'}, 'page': {'order': 'newest_first'}, 'turns': [
            {'id': 'newer-turn', 'status': 'completed', 'items': []},
            {'id': 'turn-local', 'status': 'inProgress', 'items': []}]}
        item = self.entry.fact({'id': 'local', 'source': 'fixture', 'receipt': {
            'id': 'history-running', 'kind': 'read', 'reference': 'fixture actual history',
            'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})['entry']
        self.assertIsNone(item['native_terminal_status'])
        self.assertTrue(self.entry.live(item))

    def test_history_rejects_missing_duplicate_and_wrong_thread_targets(self):
        self.task(); self.prepare_local(); self.local_ack()
        before = self.entry.get('local')
        for boundary in ('missing', 'duplicate', 'wrong-thread'):
            with self.subTest(boundary=boundary):
                raw = {'thread': {'id': 'native-worker'}, 'page': {'order': 'newest_first'}, 'turns': [
                    {'id': 'newer-turn', 'status': 'completed', 'items': []},
                    {'id': 'turn-local', 'status': 'completed', 'items': []}]}
                if boundary == 'missing': raw['turns'].pop()
                elif boundary == 'duplicate': raw['turns'].append(deepcopy(raw['turns'][1]))
                else: raw['thread']['id'] = 'other-worker'
                with self.assertRaises(HarnessError):
                    self.entry.fact({'id': 'local', 'source': 'fixture', 'receipt': {
                        'id': 'history-invalid-' + boundary, 'kind': 'read', 'reference': 'fixture invalid history',
                        'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})
                self.assertEqual(self.entry.get('local'), before)

    def test_explicit_zero_budget_is_enforced_and_unknown_send_is_not_retried(self):
        self.task()
        self.runtime.policy('limit', 0, {'execution_limit': 1, 'review_limit': 1, 'review_backlog_limit': 2,
            'budget': {'unit': 'turn', 'limit': 0, 'unknown_policy': 'stop'}}, 'fixture')
        request = {'id': 'limited', 'work': 'w', 'expected': 1, 'task': 't', 'provider': LOCAL_PROVIDER,
            'method': 'send_message', 'params': {'threadId': 'native-worker', 'prompt': 'Continue authorized work'},
            'review_owner': 'local-main', 'policy_id': 'limit', 'entry_coverage': {'execution_location': 'Known local environment'}, 'source': 'fixture'}
        with self.assertRaises(HarnessError): self.entry.prepare(request)
        request.pop('policy_id')
        self.entry.prepare(request)
        self.entry.fact({'id': 'limited', 'source': 'fixture timeout', 'receipt': {'id': 'timeout', 'kind': 'unknown',
            'reference': 'fixture tool timeout', 'observed_at': '2026-10-23T08:00:00Z', 'raw': {}}})
        self.assertFalse(self.entry.prepare(request)['send_once'])
        self.assertEqual(self.entry.get('limited')['phase'], 'unconfirmed')

    def test_actual_host_send_claims_ordinary_intent_once_without_legacy_dispatch(self):
        from harness.host import handle_hook
        self.task()
        message = {'threadId': 'native-worker', 'prompt': 'Continue the original bounded task'}
        self.entry.prepare({'id': 'local-send', 'work': 'w', 'expected': 1, 'task': 't', 'provider': LOCAL_PROVIDER,
            'method': 'send_message', 'params': message, 'review_owner': 'local-main',
            'entry_coverage': {'execution_location': 'Known local fixture'}, 'source': 'fixture authorized work'})
        event = {'hook_event_name': 'PreToolUse', 'session_id': 'main', 'tool_use_id': 'actual-call',
            'tool_name': 'mcp__codex_app__send_message_to_thread', 'tool_input': message}
        self.assertEqual(handle_hook(self.state, event), {})
        self.assertEqual(self.entry.get('local-send')['phase'], 'unconfirmed')
        duplicate = handle_hook(self.state, event)
        self.assertEqual(duplicate['hookSpecificOutput']['permissionDecision'], 'deny')
        handle_hook(self.state, {**event, 'hook_event_name': 'PostToolUse', 'tool_response': {
            'threadId': 'native-worker', 'turnId': 'real-turn', 'admissionOutcome': 'started'}})
        self.assertEqual(self.entry.get('local-send')['phase'], 'accepted')
        self.assertEqual(self.state.binding('native-worker')['work_id'], 'w')
        self.assertEqual(self.state.binding('native-worker')['role'], 'executor')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)
        saved = self.entry.finish({'id': 'local-send', 'expected': self.entry.get('local-send')['revision'],
            'executor_session': 'native-worker', 'turn_id': 'real-turn', 'result': self.result(), 'source': 'fixture actual return'})
        self.assertEqual(saved['entry']['phase'], 'returned')

    def test_local_dispatch_binding_is_idempotent_and_usable_for_continuation(self):
        from harness.context import ordinary_context
        self.task(); self.state.bind('native-worker', 'w', 'executor', 'fixture existing executor')
        before = self.state.binding('native-worker')
        self.prepare_local(); item = self.local_ack()['entry']
        self.assertEqual(item['phase'], 'accepted')
        self.assertEqual(self.state.binding('native-worker'), before)
        self.assertTrue(self.entry.call('check', {'id': 'local', 'executor_session': 'native-worker',
            'thread_id': 'native-worker', 'turn_id': 'turn-local'})['allowed'])
        self.assertNotIn('issue_counts', ordinary_context(self.state, 'w'))

    def test_local_binding_conflicts_preserve_actual_receipt_and_existing_role(self):
        self.task(); self.state.bind('native-worker', 'w', 'observer', 'fixture read-only observer')
        self.prepare_local(); item = self.local_ack()['entry']
        self.assertEqual(item['phase'], 'sent')
        self.assertEqual(len(item['receipts']), 1)
        self.assertEqual(self.state.binding('native-worker')['role'], 'observer')
        detail = self.entry.recover('w')['attention'][0]
        self.assertEqual(detail['binding']['status'], 'conflict')
        self.assertIn('binding_conflict', detail['issues'])
        self.assertEqual(detail['receiver'], 'local-main')
        self.state.update('other', 0, {'title': 'Other', 'request': 'Other scope', 'request_source': 'fixture',
            'objective': 'Other original work', 'status': 'active'}, 'fixture')
        self.state.bind('native-worker', 'other', 'executor', 'fixture explicit move', expected=1)
        item = self.entry.fact({'id': 'local', 'source': 'fixture later actual read', 'receipt': {
            'id': 'conflicting-read', 'kind': 'read', 'reference': 'fixture actual read with another work binding',
            'observed_at': '2026-10-23T08:01:00Z', 'raw': {'threadId': 'native-worker',
                'latestTurn': {'id': 'turn-local', 'status': 'inProgress'}}}})['entry']
        self.assertEqual(item['phase'], 'sent')
        self.assertEqual(len(item['receipts']), 2)
        self.assertEqual(self.state.binding('native-worker')['work_id'], 'other')
        self.assertEqual(self.entry.recover('w')['attention'][0]['binding']['work_id'], 'other')

    def test_observation_never_acquires_binding_or_execution_permission(self):
        self.task(); request = self.receive_request()
        request['receipt']['raw']['latestTurn']['status'] = 'inProgress'
        item = self.entry.receive(request)['entry']
        self.entry.fact({'id': 'existing', 'source': 'fixture later read', 'receipt': {
            'id': 'later-read', 'kind': 'read', 'reference': 'fixture later native read',
            'observed_at': '2026-10-23T08:01:00Z', 'raw': {'threadId': 'native-worker',
                'latestTurn': {'id': 'actual-turn', 'status': 'inProgress'}}}})
        self.assertIsNone(self.state.binding('native-worker'))
        with self.assertRaises(HarnessError):
            self.entry.call('check', {'id': 'existing', 'executor_session': 'native-worker',
                'thread_id': 'native-worker', 'turn_id': item['turn_id']})
        self.assertNotIn('binding', self.entry.recover('w')['attention'][0])

    def test_legacy_missing_binding_is_reported_without_silent_repair(self):
        self.task(); self.prepare_local()
        legacy = self.entry.get('local')
        legacy.update(phase='accepted', accepted_by='native-worker', turn_id='turn-local')
        self.entry._save(legacy, 'fixture historical faulty admission')
        self.entry.fact({'id': 'local', 'source': 'fixture current observation', 'receipt': {
            'id': 'progress', 'kind': 'read', 'reference': 'fixture exact read', 'observed_at': '2026-10-23T08:01:00Z',
            'raw': {'threadId': 'native-worker', 'latestTurn': {'id': 'turn-local', 'status': 'inProgress'}}}})
        self.assertIsNone(self.state.binding('native-worker'))
        recovery = self.entry.recover('w')
        self.assertEqual(recovery['issue_counts'], {'missing_binding': 1})
        self.assertEqual(recovery['attention'][0]['binding']['status'], 'missing')
        with self.assertRaises(HarnessError):
            self.entry.call('check', {'id': 'local', 'executor_session': 'native-worker',
                'thread_id': 'native-worker', 'turn_id': 'turn-local'})
        self.state.bind('native-worker', 'w', 'executor', 'fixture explicit reconciliation')
        self.assertEqual(self.entry.recover('w')['issue_counts'], {})

    def test_terminal_and_returned_receipts_keep_closure_and_skip_new_binding(self):
        self.task(); self.prepare_local(); terminal = self.local_ack(terminal='completed')['entry']
        self.assertEqual(terminal['phase'], 'sent')
        self.assertEqual(terminal['accepted_by'], 'native-worker')
        self.assertIsNone(self.state.binding('native-worker'))
        terminal.update(phase='accepted', accepted_by='native-worker')
        self.entry._save(terminal, 'fixture historical accepted producer')
        self.entry.fact({'id': 'local', 'source': 'fixture late timeout', 'receipt': {'id': 'late-unknown',
            'kind': 'unknown', 'reference': 'fixture late timeout', 'observed_at': '2026-10-23T08:02:00Z', 'raw': {}}})
        self.assertEqual(self.entry.get('local')['phase'], 'accepted')
        self.assertEqual(self.entry.recover('w')['issue_counts'], {'missing_return': 1})
        self.assertIsNone(self.state.binding('native-worker'))
        item = self.entry.get('local')
        self.entry.finish({'id': 'local', 'expected': item['revision'], 'executor_session': 'native-worker',
            'turn_id': item['turn_id'], 'result': self.result(), 'source': 'fixture actual return'})
        self.entry.fact({'id': 'local', 'source': 'fixture late timeout', 'receipt': {'id': 'returned-unknown',
            'kind': 'unknown', 'reference': 'fixture late timeout', 'observed_at': '2026-10-23T08:03:00Z', 'raw': {}}})
        self.assertEqual(self.entry.get('local')['phase'], 'returned')
        self.assertEqual(self.entry.recover('w')['attention'], [])
        with self.assertRaises(HarnessError):
            self.entry.call('check', {'id': 'local', 'executor_session': 'native-worker',
                'thread_id': 'native-worker', 'turn_id': item['turn_id']})

    def test_local_entry_only_receipt_remains_preparation_without_binding(self):
        self.task(); self.prepare_local(entry_only=True)
        self.local_ack()
        self.assertIsNone(self.state.binding('native-worker'))
        self.assertEqual(self.entry.get('local')['phase'], 'sent')
        self.assertNotIn('binding', self.entry.recover('w')['attention'][0])

    def test_bounded_context_retains_review_accounting_and_deep_read_obligation(self):
        from harness.context import hook_context, render_context
        for index in range(5):
            self.task('t-' + str(index))
            request = self.receive_request(id='existing-' + str(index), task='t-' + str(index),
                review_owner='fixture-receiver-' + str(index) + '-' + 'large-owner' * 60, result=self.result())
            request['receipt']['id'] = 'actual-read-' + str(index)
            request['receipt']['raw']['threadId'] = 'native-worker-' + str(index)
            self.entry.receive(request)
        page = self.entry.recover('w', limit=2)
        self.assertEqual(page['pending_review_count'], 5)
        self.assertEqual(page['omitted_reviews'], 3)
        self.assertEqual(page['issue_counts'], {})
        before = self.state.db.total_changes
        raw = hook_context(self.state, 'main', budget=3000)
        self.assertLessEqual(len(raw.encode()), 3000)
        summary = json.loads(raw.split('\n', 1)[1])['ordinary_native']
        self.assertEqual(summary['pending_review_count'], 5)
        self.assertEqual(summary['omitted_reviews'] + len(summary.get('pending_reviews', [])), 5)
        self.assertTrue(summary['details_required'])
        self.assertEqual(summary['entry']['request']['work'], 'w')
        rendered = render_context(self.state, work='w', budget=4000)
        self.assertEqual(rendered['ordinary_native']['pending_review_count'], 5)
        self.assertEqual(self.state.db.total_changes, before)

    def test_received_agent_delegation_matches_actual_caller_without_human_authority(self):
        from harness.native_entry import native_projection
        self.task(); self.prepare_local()
        item = self.entry.get('local'); item.update(phase='unconfirmed', native_guard={'caller': 'main'})
        self.entry._save(item, 'fixture actual pre-tool claim')
        raw = self.delegation_read()
        message = native_projection(raw)['items'][0]
        self.assertEqual(message['role'], 'agent')
        self.assertEqual(message['source_thread_id'], 'main')
        saved = self.entry.fact({'id': 'local', 'source': 'fixture exact native read', 'receipt': {
            'id': 'agent-read', 'kind': 'read', 'reference': 'fixture received delegation',
            'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})['entry']
        self.assertEqual(saved['phase'], 'accepted')
        self.assertEqual(saved['turn_id'], 'received-turn')
        self.assertEqual(self.state.binding('native-worker')['role'], 'executor')
        self.assertEqual(self.state.pending('w'), [])
        self.task('observed')
        request = self.receive_request(id='observed', task='observed')
        request['receipt']['raw'] = {'threadId': 'observed-worker'}
        self.entry.receive(request)
        raw['thread']['id'] = 'observed-worker'
        self.entry.fact({'id': 'observed', 'source': 'fixture observation only', 'receipt': {
            'id': 'observed-agent-read', 'kind': 'read', 'reference': 'fixture observed delegation',
            'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})
        self.assertIsNone(self.state.binding('observed-worker'))

    def test_received_delegation_rejects_inexact_evidence_then_uses_prepared_coordinator(self):
        self.task(); self.runtime.owner('w', 'main', 0, 'fixture current coordinator')
        self.prepare_local(coordinator='main', owner_revision=1)
        item = self.entry.get('local'); item['phase'] = 'unconfirmed'
        self.entry._save(item, 'fixture unknown native send')
        for boundary in ('truncated', 'source', 'prompt', 'tool', 'ambiguous', 'malformed', 'turn'):
            with self.subTest(boundary=boundary):
                raw = deepcopy(self.delegation_read()); message = raw['turns'][0]['items'][0]
                if boundary == 'truncated': message['output']['truncated'] = True
                elif boundary == 'source': message['output']['text'] = message['output']['text'].replace('>main<', '>other<')
                elif boundary == 'prompt': message['output']['text'] = message['output']['text'].replace('bounded task', 'different task')
                elif boundary == 'tool': message['name'] = 'arbitrary_tool'
                elif boundary == 'ambiguous': raw['turns'][0]['items'].append(deepcopy(message))
                elif boundary == 'malformed': message['output']['text'] += '<broken>'
                elif boundary == 'turn': message['turnId'] = 'another-turn'
                with self.assertRaises(HarnessError):
                    self.entry.fact({'id': 'local', 'source': 'fixture boundary check', 'receipt': {
                        'id': 'invalid-' + boundary, 'kind': 'read', 'reference': 'fixture received delegation',
                        'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})
                self.assertEqual(self.entry.get('local')['phase'], 'unconfirmed')
                self.assertIsNone(self.entry.get('local')['turn_id'])
                self.assertIsNone(self.state.binding('native-worker'))
        saved = self.entry.fact({'id': 'local', 'source': 'fixture exact native read', 'receipt': {
            'id': 'coordinator-agent-read', 'kind': 'read', 'reference': 'fixture received delegation',
            'observed_at': '2026-10-24T08:00:00Z', 'raw': self.delegation_read()}})['entry']
        self.assertEqual(saved['phase'], 'accepted')

    def test_stage_feedback_confirms_same_turn_delivery_and_preserves_original_claim(self):
        from harness.host import handle_hook
        self.stage_start(); self.stage_finish(); self.stage_review()
        feedback = self.prepare_feedback()['entry']
        self.assertEqual(feedback['continuation_of']['id'], 'local')
        event = self.claim_feedback()
        handle_hook(self.state, {**event, 'hook_event_name': 'PostToolUse', 'tool_response': {
            'threadId': 'native-worker', 'turnId': 'turn-local', 'admissionOutcome': 'steered'}})
        self.assertEqual(self.entry.get('feedback')['phase'], 'unconfirmed')
        raw = self.delegation_read(); raw['turns'][0]['id'] = 'turn-local'
        raw['turns'][0]['items'][0]['output']['text'] = '<codex_delegation><source_thread_id>main</source_thread_id><input>Continue after the reviewed real stage</input></codex_delegation>'
        for boundary in ('source', 'prompt', 'truncated'):
            wrong = deepcopy(raw); output = wrong['turns'][0]['items'][0]['output']
            if boundary == 'source': output['text'] = output['text'].replace('>main<', '>other<')
            elif boundary == 'prompt': output['text'] = output['text'].replace('reviewed real stage', 'another task')
            else: output['truncated'] = True
            with self.assertRaises(HarnessError):
                self.entry.fact({'id': 'feedback', 'source': 'fixture wrong receipt', 'receipt': {'id': 'bad-' + boundary,
                    'kind': 'read', 'reference': 'fixture read', 'observed_at': '2026-10-24T08:00:00Z', 'raw': wrong}})
            self.assertEqual(self.entry.get('feedback')['phase'], 'unconfirmed')
        received = self.entry.fact({'id': 'feedback', 'source': 'fixture actual receipt', 'receipt': {'id': 'received-feedback',
            'kind': 'read', 'reference': 'fixture exact received delegation', 'observed_at': '2026-10-24T08:00:00Z', 'raw': raw}})['entry']
        self.assertEqual(received['phase'], 'accepted'); self.assertFalse(received.get('conflict'))
        original = self.entry.get('local')
        self.assertEqual(original['phase'], 'returned'); self.assertIsNone(original['native_terminal_status'])
        self.assertTrue(self.entry.live(original)); self.assertIn('local', [item['id'] for item in self.entry.occupied()])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)
        self.stage_finish('feedback'); self.stage_review(identifier='feedback')
        next_stage = self.prepare_feedback('next-stage')['entry']
        self.assertEqual(next_stage['continuation_of']['id'], 'feedback')
        self.claim_feedback('next-stage')
        self.entry.fact({'id': 'next-stage', 'source': 'fixture unknown later-stage send', 'receipt': {'id': 'unknown-next-stage',
            'kind': 'unknown', 'reference': 'fixture later-stage timeout', 'observed_at': '2026-10-24T08:00:00Z', 'raw': {}}})
        with self.assertRaises(HarnessError): self.prepare_feedback('cannot-skip-unknown-stage')

    def test_stage_feedback_requires_real_return_and_independent_review(self):
        self.stage_start()
        with self.assertRaises(HarnessError): self.prepare_feedback('no-return')
        self.stage_finish()
        with self.assertRaises(HarnessError): self.prepare_feedback('no-review')
        self.stage_review(mode='self')
        with self.assertRaises(HarnessError): self.prepare_feedback('not-independent')
        self.assertEqual(self.entry.get('local')['phase'], 'returned')

    def test_stage_feedback_blocks_cross_task_and_unknown_child_send(self):
        self.stage_start(); self.stage_finish(); self.stage_review(); self.task('another-task')
        with self.assertRaises(HarnessError): self.prepare_feedback('cross-task', task='another-task')
        self.prepare_feedback(); self.claim_feedback()
        self.entry.fact({'id': 'feedback', 'source': 'fixture unknown actual send', 'receipt': {'id': 'unknown-feedback',
            'kind': 'unknown', 'reference': 'fixture timeout', 'observed_at': '2026-10-24T08:00:00Z', 'raw': {}}})
        with self.assertRaises(HarnessError): self.prepare_feedback('duplicate-after-unknown')
        self.assertEqual(self.entry.get('feedback')['phase'], 'unconfirmed')

    def test_stage_feedback_rejects_wrong_steered_thread_and_turn(self):
        self.stage_start(); self.stage_finish(); self.stage_review(); self.prepare_feedback(); self.claim_feedback()
        item = self.entry.get('feedback')
        receipt = {'id': 'wrong-thread', 'kind': 'send', 'reference': 'fixture actual send', 'observed_at': '2026-10-24T08:00:00Z',
            'request_sha256': item['request_sha256'], 'raw': {'threadId': 'other-worker', 'turnId': 'turn-local', 'admissionOutcome': 'steered'}}
        with self.assertRaises(HarnessError): self.entry.fact({'id': 'feedback', 'source': 'fixture send', 'receipt': receipt})
        receipt = {**receipt, 'id': 'wrong-turn', 'raw': {'threadId': 'native-worker', 'turnId': 'different-turn', 'admissionOutcome': 'steered'}}
        rejected = self.entry.fact({'id': 'feedback', 'source': 'fixture send', 'receipt': receipt})['entry']
        self.assertTrue(rejected['conflict']); self.assertNotEqual(rejected['phase'], 'accepted')

    def test_human_can_reassign_coordinator_with_cas_without_old_owner_ack(self):
        self.task(); self.runtime.owner('w', 'old-dot', 0, 'fixture historical owner')
        record, assessment = self.origin('Move this work coordination to the local main session; retain original scientific execution')
        statement = self.runtime.owner_statement('w', 'local-main', 1)
        authorization = {'input_id': record['id'], 'input_sha256': record['input_sha256'],
            'assessment_revision': assessment['revision'], 'scope_revision': 1, 'quote': record['prompt'],
            'mutation_sha256': goals.fingerprint(statement)}
        handoff = {'mode': 'user_reassignment', 'enforce_writes': True, 'expected_work_revision': 1,
            'execution_effect': 'coordination_only', 'reason': 'User reclaimed local coordination',
            'impact': 'Original native work retained; unknown actions need reconciliation', 'authorization': authorization}
        moved = self.runtime.owner('w', 'local-main', 1, 'fixture approved user reassignment', handoff)
        self.assertEqual(moved['owner'], 'local-main')
        self.assertEqual(moved['revision'], 2)
        self.assertEqual(self.state.get('w')['revision'], 2)
        with self.assertRaises(HarnessError):
            self.runtime.owner('w', 'other-main', 1, 'stale fixture', handoff)
        self.assertEqual(self.runtime.coordinator('w')['owner'], 'local-main')

    def test_native_question_resolution_preserves_scope_without_overall_replanning(self):
        from harness.native_inputs import NativeInputs
        self.task(); self.runtime.owner('w', 'local-main', 0, 'fixture')
        route = self.runtime.map('t', 0, {'host_id': 'fixture-host', 'runtime_id': 'fixture-runtime',
            'thread_id': 'native-worker', 'review_owner': 'local-main'}, 'fixture')
        message = 'What is the current progress?'
        facts = [
            {'kind': 'native_user_message_observed', 'hostId': 'fixture-host', 'threadId': 'native-worker',
                'turnId': 'question-turn', 'itemId': 'actual-message', 'contentSha256': goals.fingerprint(message),
                'textBlocks': [{'index': 0, 'text': message, 'originalBytes': len(message.encode()), 'truncated': False}],
                'contentTypes': ['text'], 'contentTruncated': False},
            {'kind': 'thread_timeline_observed', 'hostId': 'fixture-host', 'threadId': 'native-worker',
                'source': 'public_app_server_history', 'turns': [{'turnId': 'question-turn', 'status': 'completed'}],
                'coverage': {'complete': True, 'gap': False, 'itemsComplete': True, 'textTruncated': False}}]
        with self.state.transaction():
            for i, fact in enumerate(facts):
                self.state.db.execute('INSERT INTO runtime_inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('fixture-observation-' + str(i), goals.fingerprint(fact), fact['kind'], 'fixture-host', 'fixture-runtime',
                     'native-worker', fact.get('turnId'), None, 'accepted', encoded(fact), now(), now()))
        native = NativeInputs(self.state)
        view = native.view('fixture-host', 'native-worker')
        self.assertTrue(view['pending'])
        native.resolve('fixture-host', 'native-worker', view['epoch'], {'reviewed_set_sha256': view['epoch'],
            'reviewed_count': view['attention_count'], 'coverage_event_id': 'fixture-observation-1',
            'reviewer': 'local-main', 'owner_revisions': {'w': 1}, 'source_ref': 'fixture native timeline',
            'reason': 'A progress question retains current execution scope', 'disposition': 'original_scope_confirmed',
            'nonblocking_input': {'kind': 'question', 'quote': message, 'reason': 'Progress inquiry',
                'impact': 'Current authorized task continues', 'source_ref': 'fixture actual message'}}, 'fixture semantic read')
        self.assertEqual(self.runtime.route('t')['body']['scope_revision'], route['body']['scope_revision'])
        self.assertTrue(native.assert_ready(self.runtime.route('t'), self.state.task('t')))
        self.assertFalse(self.state.get('w').get('decision'))

    def test_user_reassignment_moves_only_old_coordinator_receipts_and_receive_retains_owner(self):
        self.task(); self.task('professional'); self.task('new-result')
        self.runtime.owner('w', 'old-dot', 0, 'fixture historical coordinator')
        original = self.entry.receive(self.receive_request(review_owner='old-dot', result=self.result()))['entry']
        request = self.receive_request(id='professional-entry', task='professional', review_owner='old-dot', result=self.result())
        request['receipt']['id'] = 'professional-receipt'
        request['receipt']['raw']['latestTurn']['id'] = 'professional-turn'
        self.entry.receive(request)
        queue = self.runtime._get('runtime_review_queue', 'task_id', 'professional')
        self.runtime.review_assign('professional', 'professional-reviewer', queue['revision'], 'fixture explicit professional assignment')
        record, assessment = self.origin('Reassign local coordination and its pending reception; keep other professional reviewers')
        handoff = {'mode': 'user_reassignment', 'enforce_writes': True, 'expected_work_revision': 1,
            'execution_effect': 'coordination_only', 'reason': 'Fixture human reclaim', 'impact': 'Retain original scientific execution',
            'authorization': {'input_id': record['id'], 'input_sha256': record['input_sha256'],
                'assessment_revision': assessment['revision'], 'scope_revision': 1, 'quote': record['prompt'],
                'mutation_sha256': goals.fingerprint(self.runtime.owner_statement('w', 'local-main', 1))}}
        self.runtime.owner('w', 'local-main', 1, 'fixture human reassignment', handoff)
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't')['owner'], 'local-main')
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 'professional')['owner'], 'professional-reviewer')
        moved = self.entry.get(original['id'])
        self.assertEqual(moved['review_owner'], 'local-main')
        for field in ('id', 'thread_id', 'turn_id', 'accepted_by', 'dispatch_key', 'receipts', 'return_event'):
            self.assertEqual(moved[field], original[field])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM events WHERE kind='return_review_reassigned_by_user'").fetchone()[0], 1)
        rejected = self.receive_request(id='irrelevant-receiver', task='new-result', expected=2, review_owner='unrelated-owner')
        rejected['receipt']['id'] = 'unrelated-new-receipt'
        rejected['receipt']['raw']['latestTurn']['id'] = 'new-turn'
        with self.assertRaises(HarnessError) as caught:
            self.entry.receive(rejected)
        self.assertEqual(caught.exception.code, 'COORDINATOR_REQUIRED')


if __name__ == '__main__':
    unittest.main()
