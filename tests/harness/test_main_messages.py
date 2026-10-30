"""No native transport: temporary stores and synthetic hook envelopes only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.common import HarnessError
from harness.context import hook_context
from harness.host import (claim_coordination_message, coordination_message_show,
                          handle_hook, prepare_coordination_message)
from harness.main_messages import check_main_message, main_message_policy
from harness.native_entry import LOCAL_PROVIDER, NativeEntry
from harness.state import State


class MainMessagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root/'store', self.root)
        self.entry = NativeEntry(self.state)
        (self.root/'result.md').write_text('Synthetic bounded result')
        self.completion = {'purpose': 'Fixture bounded result', 'checks': [
            {'id': 'result', 'kind': 'files', 'description': 'Result exists',
             'basis': 'Fixture output', 'inputs': [{'path': 'result.md'}]}]}
        for work in ('w', 'maintenance'):
            self.state.update(work, 0, {'title': work, 'request': 'Fixture bounded work',
                'request_source': 'synthetic fixture', 'objective': 'Fixture result', 'status': 'active',
                'requirements': [{'id': 'overall', 'description': 'Fixture result',
                    'source': 'synthetic fixture', 'completion': self.completion}]}, 'fixture')
        for session, work, role in (('main', 'w', 'main'), ('worker', 'w', 'executor'),
                ('maintenance-main', 'maintenance', 'main'), ('maintenance-worker', 'maintenance', 'executor')):
            self.state.bind(session, work, role, 'fixture')

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def activate(self):
        with self.state.transaction():
            return self.state.event(None, 'main_message_policy', 'synthetic test policy', {'mode': 'user_initiated'})

    def event(self, caller='worker', target='main', prompt='Fixture result', **extra):
        return {'hook_event_name': 'PreToolUse', 'session_id': caller,
                'tool_name': 'mcp__codex_app__send_message_to_thread', 'tool_use_id': 'fixture-call',
                'tool_input': {'threadId': target, 'prompt': prompt}, **extra}

    def denied(self, event):
        result = handle_hook(self.state, event, self.root)['hookSpecificOutput']
        self.assertEqual(result['permissionDecision'], 'deny')
        self.assertIn('user initiated', result['permissionDecisionReason'])

    def task(self, identifier):
        self.state.create_task(identifier, 'w', self.state.get('w')['revision'],
            {'purpose': 'Fixture bounded use', 'expected': 'Fixture result', 'completion': self.completion}, 'fixture')

    def native_request(self, identifier='native', target='main'):
        return {'id': identifier, 'provider': LOCAL_PROVIDER, 'method': 'send_message',
            'work': 'w', 'expected': self.state.get('w')['revision'], 'task': 't',
            'params': {'threadId': target, 'prompt': 'Fixture result'}, 'review_owner': 'main',
            'entry_coverage': {'execution_location': 'synthetic local fixture'}, 'source': 'fixture'}

    def coordination_request(self):
        return {'id': 'coord', 'caller': 'maintenance-main',
            'caller_work': {'id': 'maintenance', 'revision': 1, 'binding_revision': 1},
            'target_work': {'id': 'w', 'revision': 1, 'binding_revision': 1},
            'message': {'threadId': 'main', 'prompt': 'Fixture coordination'},
            'purposes': ['responsibility_handoff'], 'target_scope': 'Synthetic management scope',
            'authorization_reference': 'Synthetic historical preparation fixture', 'source': 'fixture'}

    def test_absent_policy_preserves_legacy_report_and_new_policy_blocks_all_callers(self):
        self.assertIsNone(main_message_policy(self.state))
        self.assertEqual(handle_hook(self.state, self.event(), self.root), {})
        self.activate()
        for caller in ('worker', 'maintenance-worker', 'maintenance-main', 'unbound-caller'):
            for prompt in ('Fixture result', 'urgent approval required'):
                with self.subTest(caller=caller, prompt=prompt):
                    self.denied(self.event(caller=caller, prompt=prompt))
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)
        # The store-wide policy also covers the main of a different work.
        self.denied(self.event(target='maintenance-main'))

    def test_existing_coordination_cannot_prepare_replay_or_claim_after_policy(self):
        request = self.coordination_request()
        original = prepare_coordination_message(self.state, request)['message']
        works = self.state.works()
        self.activate()
        for candidate in (request, {**request, 'id': 'new-coord'}):
            with self.assertRaisesRegex(HarnessError, 'user initiated'):
                prepare_coordination_message(self.state, candidate)
        with self.assertRaisesRegex(HarnessError, 'user initiated'):
            claim_coordination_message(self.state, original, request['caller'], 'main', request['message'], 'fixture', 'fixture')
        self.denied(self.event(caller=request['caller'], **{'tool_input': request['message']}))
        self.assertEqual(coordination_message_show(self.state, 'coord')['status'], 'prepared')
        self.assertEqual(self.state.works(), works)

    def test_native_prepare_and_existing_preparation_cannot_bypass_policy(self):
        self.task('t')
        request = self.native_request()
        old = self.entry.prepare(request)['entry']
        self.activate()
        for candidate in (request, {**request, 'id': 'new-native'}):
            with self.assertRaisesRegex(HarnessError, 'user initiated'):
                self.entry.prepare(candidate)
        with self.assertRaisesRegex(HarnessError, 'user initiated'):
            self.entry._current(old)
        with patch('harness.host.claim_dispatch', side_effect=AssertionError('must not claim')):
            self.denied(self.event(caller='maintenance-main'))
        self.assertEqual(self.entry.get('native')['phase'], 'prepared')
        self.assertEqual(self.state.task('t')['status'], 'proposed')

    def test_policy_is_rechecked_in_claim_transaction(self):
        self.task('t')
        self.entry.prepare(self.native_request())
        # Simulate a policy written after route selection but before action claim.
        with patch('harness.host.check_evidence', side_effect=lambda *args: self.activate()):
            self.denied(self.event(caller='maintenance-main'))
        self.assertEqual(self.entry.get('native')['phase'], 'prepared')

    def test_main_to_worker_keeps_native_and_dispatch_rules(self):
        self.activate()
        self.task('t')
        request = self.native_request(target='worker')
        self.assertTrue(self.entry.prepare(request)['send_once'])
        self.assertEqual(handle_hook(self.state, self.event(caller='main', target='worker'), self.root), {})
        self.assertEqual(self.entry.get('native')['phase'], 'unconfirmed')
        # No preparation still grants no permission for a different managed worker.
        self.state.bind('other-worker', 'w', 'executor', 'fixture')
        denied = handle_hook(self.state, self.event(caller='main', target='other-worker'), self.root)
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_authorized_prepared_dispatch_to_worker_still_claims(self):
        self.activate()
        self.task('dispatch')
        self.state.decide('w', self.state.get('w')['revision'], {'action': 'continue',
            'reason': 'Fixture task remains useful', 'expected_gain': 'Fixture result', 'tasks': ['dispatch'],
            'assessments': [{'requirement': 'overall', 'state': 'open', 'supports': [],
                'remaining': ['Fixture result pending'], 'limitations': [], 'reason': 'Fixture current state'}]}, 'fixture')
        message = self.event(caller='main', target='worker')['tool_input']
        self.state.reserve('dispatch-key', 'dispatch', 'worker', 'fixture', message)
        self.assertEqual(handle_hook(self.state, self.event(caller='main', target='worker'), self.root), {})
        self.assertEqual(self.state.dispatch('dispatch-key')['status'], 'claimed')

    def test_direct_user_input_and_bound_or_unbound_context_show_policy(self):
        self.activate()
        for session in ('main', 'worker', 'unbound-caller'):
            for phase in ('SessionStart', 'UserPromptSubmit'):
                result = handle_hook(self.state, {'hook_event_name': phase, 'session_id': session,
                    'turn_id': 'fixture-user-turn', 'prompt': 'Continue here', 'source': {'kind': 'unknown'}}, self.root)
                context = result['hookSpecificOutput']['additionalContext']
                self.assertLessEqual(len(context.encode()), 6500)
                policy = json.loads(context.split('\n', 1)[1])['main_message_policy']
                self.assertEqual(policy['mode'], 'user_initiated')
                self.assertIn('finish/return', policy['instruction'])
        self.assertEqual(len(self.state.pending('w')), 2)

    def test_historical_send_receipts_and_local_finish_return_remain_available(self):
        request = self.coordination_request()
        prepare_coordination_message(self.state, request)
        event = self.event(caller=request['caller'], tool_input=request['message'])
        self.assertEqual(handle_hook(self.state, event, self.root), {})
        self.activate()
        handle_hook(self.state, {**event, 'hook_event_name': 'PostToolUse', 'tool_response': {'isError': False}}, self.root)
        self.assertEqual(coordination_message_show(self.state, 'coord')['status'], 'sent')
        for operation in ('finish', 'return'):
            self.task(operation)
            item = self.entry.receive({'id': operation, 'work': 'w', 'expected': self.state.get('w')['revision'],
                'task': operation, 'review_owner': 'main', 'source': 'fixture existing native read', 'receipt': {
                    'id': 'read-'+operation, 'reference': 'synthetic existing native read',
                    'observed_at': '2026-10-24T08:00:00Z', 'raw': {'threadId': 'worker',
                        'latestTurn': {'id': operation, 'status': 'completed'}}}})['entry']
            saved = self.entry.call(operation, {'id': operation, 'expected': item['revision'],
                'executor_session': 'worker', 'thread_id': 'worker', 'turn_id': operation,
                'result': {'answer': 'Fixture result', 'artifacts': ['result.md'], 'remaining': []}, 'source': 'fixture result'})
            self.assertTrue(saved['entry']['return_event'])
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 2)

    def test_latest_unsupported_or_malformed_policy_fails_closed(self):
        self.activate()
        for value in ({'mode': 'unsupported'}, None):
            with self.state.transaction():
                self.state.event(None, 'main_message_policy', 'synthetic invalid fixture', value)
            with self.assertRaises(HarnessError):
                check_main_message(self.state, 'main')
            result = handle_hook(self.state, self.event(), self.root)
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        # Unmanaged destinations retain the existing coverage boundary.
        self.assertEqual(handle_hook(self.state, self.event(target='unmanaged'), self.root), {})


if __name__ == '__main__':
    unittest.main()
