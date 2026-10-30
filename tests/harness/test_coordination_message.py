import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness.common import HarnessError
from harness import goals
from harness.context import render_context
from harness.host import prepare_coordination_message, coordination_message_show, coordination_message_fact, handle_hook
from harness.state import State


class CoordinationMessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.state = State(self.root/'store', self.root)
        for work in ('harness-daily-coordination', 'apple-materials'):
            self.state.update(work, 0, {'title': work, 'request': 'Existing exact authorized work', 'request_source': 'synthetic original user',
                'objective': 'Current bounded work', 'status': 'active'}, 'fixture')
        self.state.bind('root-main', 'harness-daily-coordination', 'main', 'fixture')
        self.state.bind('apt-main', 'apple-materials', 'main', 'fixture')
        self.message = {'threadId': 'apt-main', 'hostId': 'local', 'prompt': 'Coordinate only the natural deployment window and approved responsibility boundary; preserve ongoing scientific work.'}
        self.request = {'id': 'coord-1', 'caller': 'root-main',
            'caller_work': {'id': 'harness-daily-coordination', 'revision': 1, 'binding_revision': 1},
            'target_work': {'id': 'apple-materials', 'revision': 1, 'binding_revision': 1},
            'message': self.message, 'purposes': ['deployment_window', 'responsibility_handoff'],
            'target_scope': 'Natural deployment window and approved coordination boundary only',
            'authorization_reference': 'Exact synthetic original user approval; reference only, not a target human input', 'source': 'fixture original approval'}

    def tearDown(self):
        self.state.close(); self.temp.cleanup()

    def event(self, phase='PreToolUse', **patch):
        return {'hook_event_name': phase, 'session_id': 'root-main', 'tool_name': 'mcp__codex_app__send_message_to_thread',
                'tool_use_id': 'actual-host-call-1', 'tool_input': self.message, **patch}

    def deny(self, event):
        value = handle_hook(self.state, event, self.root)
        self.assertEqual(value['hookSpecificOutput']['permissionDecision'], 'deny')

    def show(self): return coordination_message_show(self.state, 'coord-1')

    def test_exact_cross_work_main_message_claims_once_without_scientific_mutation(self):
        works = self.state.works(); bindings = [self.state.binding(s) for s in ('root-main', 'apt-main')]
        result = prepare_coordination_message(self.state, self.request); self.assertTrue(result['send_once'])
        self.assertEqual(handle_hook(self.state, self.event(), self.root), {})
        self.assertEqual(self.show()['status'], 'claimed'); self.deny(self.event())
        self.assertFalse(prepare_coordination_message(self.state, self.request)['send_once'])
        self.assertEqual(works, self.state.works()); self.assertEqual(bindings, [self.state.binding(s) for s in ('root-main', 'apt-main')])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM inputs').fetchone()[0], 0)
        self.assertIn('no human origin', self.show()['authorization_coverage'])

    def test_unprepared_or_changed_message_and_wrong_caller_still_denied(self):
        self.deny(self.event())
        prepare_coordination_message(self.state, self.request)
        self.deny(self.event(tool_input={**self.message, 'prompt': 'Do a scientific task instead'}))
        self.deny(self.event(session_id='other-main'))
        self.assertEqual(self.show()['status'], 'prepared')

    def test_both_current_work_revisions_required_at_prepare_and_claim(self):
        for field in ('caller_work', 'target_work'):
            with self.subTest(field=field):
                with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**self.request, field: {**self.request[field], 'revision': 0}})
                with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**self.request, field: {**self.request[field], 'binding_revision': 0}})
        prepare_coordination_message(self.state, self.request)
        self.state.update('apple-materials', 1, {'next_action': 'Original scientific progress'}, 'original main')
        self.deny(self.event()); self.assertEqual(self.show()['status'], 'invalidated')
        refreshed = {**self.request, 'id': 'coord-2', 'target_work': {**self.request['target_work'], 'revision': 2}}
        self.assertTrue(prepare_coordination_message(self.state, refreshed)['send_once'])
        self.assertEqual(handle_hook(self.state, self.event(tool_use_id='actual-host-call-2'), self.root), {})

    def test_caller_revision_change_blocks_claim(self):
        prepare_coordination_message(self.state, self.request)
        self.state.update('harness-daily-coordination', 1, {'next_action': 'New authorized phase'}, 'main')
        self.deny(self.event()); self.assertEqual(self.show()['status'], 'invalidated')

    def test_binding_change_and_new_input_block_stale_preparation(self):
        prepare_coordination_message(self.state, self.request)
        self.state.bind('apt-main', 'apple-materials', 'observer', 'changed role', 1)
        self.deny(self.event())
        self.state.bind('apt-main', 'apple-materials', 'main', 'explicit restore', 2)
        self.deny(self.event())  # Restore is a new binding version, not the prepared one.
        self.assertEqual(self.show()['status'], 'invalidated')

    def test_new_input_blocks_claim_without_relabeling_origin(self):
        prepare_coordination_message(self.state, self.request)
        self.state.input('apt-main', 'new-turn', 'Original user changed the deployment boundary', raw_source={'kind': 'unknown'})
        self.deny(self.event()); self.assertEqual(self.show()['status'], 'invalidated')

    def test_model_overrides_non_main_and_unapproved_purpose_are_rejected(self):
        for patch in ({'message': {**self.message, 'model': 'override'}}, {'purposes': ['research_dispatch']}, {'authorization_reference': ''}):
            with self.subTest(patch=patch):
                with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**self.request, **patch})
        self.state.bind('root-main', 'harness-daily-coordination', 'executor', 'changed role', 1)
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, self.request)

    def test_actual_post_unknown_replay_and_late_receipt_preserve_separate_ack(self):
        prepare_coordination_message(self.state, self.request); handle_hook(self.state, self.event(), self.root)
        handle_hook(self.state, self.event('PostToolUse', tool_response=None), self.root)
        self.assertEqual(self.show()['status'], 'unknown')
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**self.request, 'id': 'coord-2'})
        response = {'isError': False, 'content': [{'type': 'text', 'text': 'Native send returned; recipient ACK still separate'}]}
        post = self.event('PostToolUse', tool_response=response)
        handle_hook(self.state, post, self.root); self.assertEqual(self.show()['status'], 'sent')
        count = self.state.db.execute("SELECT COUNT(*) FROM events WHERE kind='coordination_message_state'").fetchone()[0]
        handle_hook(self.state, post, self.root)
        self.assertEqual(count, self.state.db.execute("SELECT COUNT(*) FROM events WHERE kind='coordination_message_state'").fetchone()[0])
        self.assertIn('handoff ACK remain separate', self.show()['send_coverage'])
        self.assertNotIn('handoff_ack', self.show())

    def test_missing_or_wrong_actual_host_call_cannot_claim_or_settle(self):
        prepare_coordination_message(self.state, self.request); self.deny(self.event(tool_use_id=None))
        handle_hook(self.state, self.event(), self.root)
        handle_hook(self.state, self.event('PostToolUse', tool_use_id='wrong-call', tool_response={'isError': False}), self.root)
        self.assertEqual(self.show()['status'], 'claimed')

    def test_post_without_observed_claim_is_quarantined_not_passed(self):
        prepare_coordination_message(self.state, self.request)
        handle_hook(self.state, self.event('PostToolUse', tool_response={'isError': False}), self.root)
        self.assertEqual(self.show()['status'], 'unknown'); self.assertNotIn('guard_evaluation_id', self.show())
        self.deny(self.event())

    def test_cli_prepare_and_readonly_show_use_same_store(self):
        script = Path(__import__('harness').__file__).parents[1]/'scripts/harness.py'
        request_file = self.root/'request.json'; request_file.write_text(json.dumps(self.request))
        argv = [sys.executable, str(script), '--root', str(self.root), '--store', str(self.state.directory)]
        result = subprocess.run([*argv, 'coordination-message', 'prepare', '--file', str(request_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertTrue(json.loads(result.stdout)['send_once'])
        request_file.write_text(json.dumps({'id': 'coord-1'}))
        result = subprocess.run([*argv, '--read-only', 'coordination-message', 'show', '--file', str(request_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(json.loads(result.stdout)['status'], 'prepared')
        result = subprocess.run([*argv, '--read-only', 'coordination-message', 'prepare', '--file', str(request_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def delegated_request(self):
        self.state.bind('dot-author', 'harness-daily-coordination', 'main', 'approved management authorship')
        self.state.bind('ordinary-writer', 'harness-daily-coordination', 'executor', 'approved local management execution')
        source = {'claim': 'Exact unsent administrative intent', 'scope': 'Approved natural-boundary handoff',
            'evidence': [{'reference': 'Actual user approval and Dot agent message; declared, not human authentication'}],
            'no_coordination_trigger': True,
            'source_provenance': {'kind': 'agent_message', 'author_thread': 'dot-author', 'local_writer_session': 'ordinary-writer'},
            'intended_message': {'provider': 'mcp__codex_app__send_message_to_thread', 'params': self.message, 'sent': False},
            'budget': {'trial_total_cap': 10, 'already_used_or_reserved': 3, 'planned_handoff_additional_reserve': 1}}
        record = self.state.judge('saved-dot-intent', 'apple-materials', 0, 'proposed', source, 'approved agent intent')
        return {**self.request, 'caller': 'ordinary-writer', 'delegation': {'author': 'dot-author',
            'author_work': dict(self.request['caller_work']),
            'origin': {'judgment_id': record['id'], 'revision': record['revision'], 'body_sha256': goals.fingerprint(record['body'])}}}

    def test_delegated_author_executor_actual_pre_post_once_preserves_roles_owner_and_budget_source(self):
        request = self.delegated_request(); works = self.state.works()
        prepare_coordination_message(self.state, request)
        actual = self.event(session_id='ordinary-writer')
        self.assertEqual(handle_hook(self.state, actual, self.root), {})
        self.assertEqual(self.show()['status'], 'claimed')
        self.assertEqual(self.show()['caller_snapshot']['binding']['role'], 'executor')
        self.assertEqual(self.show()['author_snapshot']['binding']['session'], 'dot-author')
        self.assertEqual(handle_hook(self.state, {**actual, 'hook_event_name': 'PostToolUse', 'tool_response': {'isError': False}}, self.root), {})
        self.assertEqual(self.show()['status'], 'sent'); self.deny(actual)
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**request, 'id': 'reuse-same-delegation'})
        self.assertEqual(self.state.works(), works)
        self.assertEqual(self.state.binding('apt-main')['work_id'], 'apple-materials')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM runtime_coordinators').fetchone()[0], 0)
        self.assertEqual(self.state.judgment('saved-dot-intent')['body']['budget']['already_used_or_reserved'], 3)
        self.assertIn('No cloud/local identity mapping', self.show()['delegation_coverage'])
        readonly = State(self.state.directory, self.root, readonly=True)
        try:
            observed = render_context(readonly, session='ordinary-writer')['actual_caller_observation']
            self.assertEqual(observed['status'], 'observed')
            self.assertEqual(observed['receipt']['caller'], 'ordinary-writer')
            self.assertIsNone(observed['receipt']['route'])  # Duplicate rejection precedes route assignment; do not backfill it.
            self.assertEqual(observed['receipt']['status'], 'rejected')  # The later duplicate Pre was really rejected.
            self.assertEqual(readonly.db.total_changes, 0)
        finally: readonly.close()

    def test_delegated_unknown_author_foreign_executor_and_source_rewrite_rejected(self):
        request = self.delegated_request()
        for patch in ({'author': 'stranger'}, {'origin': {**request['delegation']['origin'], 'body_sha256': 'wrong'}}):
            with self.subTest(patch=patch), self.assertRaises(HarnessError):
                prepare_coordination_message(self.state, {**request, 'delegation': {**request['delegation'], **patch}})
        with self.assertRaises(HarnessError):
            prepare_coordination_message(self.state, {**request, 'message': {**self.message, 'prompt': 'Different message'}})
        self.state.bind('ordinary-writer', 'apple-materials', 'executor', 'different work', 1)
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, request)

    def test_delegated_actual_caller_and_message_must_match_not_author_string(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        self.deny(self.event(session_id='dot-author'))
        self.deny(self.event(session_id='unknown-helper'))
        self.deny(self.event(session_id='ordinary-writer', tool_input={**self.message, 'prompt': 'Tampered actual send'}))
        self.assertEqual(self.show()['status'], 'prepared')
        self.assertEqual(handle_hook(self.state, self.event(session_id='ordinary-writer'), self.root), {})
        guard = json.loads(self.state.db.execute("SELECT payload FROM events WHERE kind='guard_evaluated' ORDER BY seq DESC LIMIT 1").fetchone()[0])
        self.assertEqual(guard['caller'], 'ordinary-writer'); self.assertEqual(guard['route'], 'prepared_delegated_coordination')

    def test_delegated_author_binding_change_and_restore_requires_fresh_preparation(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        self.state.bind('dot-author', 'harness-daily-coordination', 'observer', 'changed author role', 1)
        self.deny(self.event(session_id='ordinary-writer'))
        self.state.bind('dot-author', 'harness-daily-coordination', 'main', 'restore is new revision', 2)
        self.deny(self.event(session_id='ordinary-writer')); self.assertEqual(self.show()['status'], 'invalidated')

    def test_delegated_executor_binding_change_rejected_at_actual_pre(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        self.state.bind('ordinary-writer', 'apple-materials', 'executor', 'changed executor work', 1)
        self.deny(self.event(session_id='ordinary-writer')); self.assertEqual(self.show()['status'], 'invalidated')

    def test_delegated_author_work_and_pending_input_change_rejected(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        self.state.input('dot-author', 'new-author-input', 'Changed management scope', raw_source={'kind': 'unknown'})
        self.deny(self.event(session_id='ordinary-writer')); self.assertEqual(self.show()['status'], 'invalidated')
        # Refresh preparation explicitly, then a work revision change still invalidates it.
        refreshed = {**request, 'id': 'coord-2'}; prepare_coordination_message(self.state, refreshed)
        self.state.update('harness-daily-coordination', 1, {'next_action': 'New management phase'}, 'actual current management author')
        self.deny(self.event(session_id='ordinary-writer'))
        self.assertEqual(coordination_message_show(self.state, 'coord-2')['status'], 'invalidated')

    def test_delegated_saved_source_revision_change_blocks_actual_claim(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        old = self.state.judgment('saved-dot-intent')
        self.state.judge(old['id'], old['work_id'], old['revision'], 'proposed', {**old['body'], 'claim': 'Revised original source'}, 'actual source revision')
        self.deny(self.event(session_id='ordinary-writer')); self.assertEqual(self.show()['status'], 'invalidated')

    def test_delegated_intent_status_or_revision_cannot_reissue_consumed_message(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        actual = self.event(session_id='ordinary-writer'); handle_hook(self.state, actual, self.root)
        handle_hook(self.state, {**actual, 'hook_event_name': 'PostToolUse', 'tool_response': {'isError': False}}, self.root)
        old = self.state.judgment('saved-dot-intent')
        adopted = self.state.judge(old['id'], old['work_id'], old['revision'], 'adopted', old['body'], 'Same source adopted without a new send intent')
        self.assertEqual(goals.fingerprint(adopted['body']), request['delegation']['origin']['body_sha256'])
        current = {**request, 'id': 'same-intent-adopted', 'delegation': {**request['delegation'],
            'origin': {**request['delegation']['origin'], 'revision': adopted['revision']}}}
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, current)
        message = {**self.message, 'prompt': 'A genuinely new approved administrative message'}
        body = {**adopted['body'], 'intended_message': {**adopted['body']['intended_message'], 'params': message}}
        changed = self.state.judge(old['id'], old['work_id'], adopted['revision'], 'proposed', body, 'Changed content still uses the consumed intent identity')
        revised = {**current, 'id': 'same-intent-revised', 'message': message, 'delegation': {**current['delegation'],
            'origin': {'judgment_id': changed['id'], 'revision': changed['revision'], 'body_sha256': goals.fingerprint(changed['body'])}}}
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, revised)
        new = self.state.judge('new-approved-intent', old['work_id'], 0, 'proposed', body, 'New exact administrative intent')
        fresh = {**revised, 'id': 'new-message-new-intent', 'delegation': {**revised['delegation'],
            'origin': {'judgment_id': new['id'], 'revision': new['revision'], 'body_sha256': goals.fingerprint(new['body'])}}}
        self.assertTrue(prepare_coordination_message(self.state, fresh)['send_once'])
        self.assertEqual(self.show()['status'], 'sent')

    def test_context_readonly_exact_binding_owner_and_actual_pre_are_distinct(self):
        request = self.delegated_request()
        from harness.runtime import Runtime
        Runtime(self.state).owner('harness-daily-coordination', 'review-deployment-owner', 0, 'fixture exact runtime owner')
        self.state.update('harness-daily-coordination', 1, {'owner': 'descriptive-work-owner'}, 'fixture distinct label')
        readonly = State(self.state.directory, self.root, readonly=True)
        try:
            for full in (False, True):
                result = render_context(readonly, session='ordinary-writer', full=full)
                self.assertEqual(result['session_binding']['revision'], 1)
                self.assertEqual(result['session_binding']['role'], 'executor')
                self.assertEqual(result['coordinator_identity']['owner'], 'review-deployment-owner')
                self.assertEqual(result['coordinator_identity']['revision'], 1)
                self.assertFalse(result['coordinator_identity']['writes_enforced'])
                self.assertEqual(result['actual_caller_observation']['status'], 'not_observed')
            unknown = render_context(readonly, session='unbound-native-session')
            self.assertEqual(unknown['session_binding']['status'], 'unbound'); self.assertEqual(unknown['session_binding']['revision'], 0)
            self.assertEqual(readonly.db.total_changes, 0)
        finally: readonly.close()

    def send_fact(self, identifier='native-send', turn=None):
        return {'id': identifier, 'kind': 'send', 'provider': 'mcp__codex_app__send_message_to_thread',
            'reference': 'Actual send tool result', 'observed_at': '2026-10-22T14:03:13+00:00',
            'request_sha256': goals.fingerprint(self.message), 'params': self.message,
            'raw': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps({'threadId': 'apt-main', **({'turnId': turn} if turn else {})})}]}}

    def wait_fact(self, identifier='native-wait', turn='management-turn', status='completed', assessed=True):
        fact = {'id': identifier, 'kind': 'notification', 'provider': 'mcp__codex_app__wait_threads',
            'reference': 'Actual native terminal and ACK artifact; not a guard receipt', 'observed_at': '2026-10-22T14:15:02+00:00',
            'raw': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps({'polls': [
                {'thread': {'id': 'unrelated'}, 'latestTurn': {'id': 'other', 'status': 'failed'}},
                {'thread': {'id': 'apt-main'}, 'latestTurn': {'id': turn, 'status': status},
                 'latestAssistantMessage': {'text': 'safe_boundary=true; this text does not automatically transfer owner'}}]})}]}}
        if assessed:
            fact['correlation'] = {'reference': 'Exact saved native-return judgment and ACK artifact', 'basis': 'Declared message/turn association, not a mechanically observed prompt',
                'reviewer': 'Dot', 'request_sha256': goals.fingerprint(self.message), 'thread_id': 'apt-main', 'turn_id': turn}
        return fact

    def native_fact(self, *receipts):
        return coordination_message_fact(self.state, {'id': 'coord-1', 'source': 'Actual native observations by local executor', 'receipts': list(receipts)})

    def test_native_fact_batch_after_legitimate_target_rebind_keeps_send_and_terminal_without_hook(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        original = self.show()['target_snapshot']
        self.state.update('apple-lca', 0, {'title': 'Original scientific stage', 'request': 'Existing stage', 'request_source': 'Existing stage user request', 'objective': 'Retained science', 'status': 'active'}, 'fixture')
        self.state.input('apt-main', 'pending-old-parent', 'Parent correction', raw_source={'kind': 'unknown'})
        self.state.bind('apt-main', 'apple-lca', 'main', 'Actual safe-boundary stage rebind', 1)
        result = self.native_fact(self.send_fact(), self.wait_fact()); item = result['message']
        self.assertEqual(item['status'], 'sent'); self.assertEqual(item['native_turn_id'], 'management-turn')
        self.assertEqual(item['native_terminal_status'], 'completed'); self.assertEqual(item['target_snapshot'], original)
        self.assertEqual(item['native_observation_context']['snapshots_at_observation']['target']['binding']['revision'], 2)
        self.assertEqual(len(item['native_observation_context']['prepared_work_states_at_observation']['target']['pending_inputs']), 1)
        self.assertTrue(item['handoff_review_required']); self.assertEqual(item['native_coverage']['guard'], 'not_observed')
        self.assertIn('Declared correlation', item['turn_binding_coverage'])
        for key in ('host_call_id', 'guard_evaluation_id', 'handoff_ack'): self.assertNotIn(key, item)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM runtime_coordinators').fetchone()[0], 0)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**request, 'id': 'do-not-reissue'})
        self.deny(self.event(session_id='ordinary-writer'))

    def test_native_fact_missing_correlation_unknown_then_exact_public_prompt_recovers_idempotently(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        send = self.send_fact(); wait = self.wait_fact(assessed=False)
        self.assertEqual(self.native_fact(send, wait)['message']['status'], 'unknown')
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, {**request, 'id': 'unknown-do-not-reissue'})
        read = {'id': 'actual-full-read', 'kind': 'read', 'provider': 'mcp__codex_app__read_thread', 'reference': 'Exact public native read',
            'observed_at': '2026-10-22T14:16:00+00:00', 'raw': {'structuredContent': {'thread': {'id': 'apt-main'},
                'page': {'order': 'newest_first'}, 'turns': [{'id': 'management-turn', 'status': 'completed', 'items': [
                    {'type': 'userMessage', 'codexDelegation': {'input': self.message['prompt']}, 'truncated': False}]}]}}}
        item = self.native_fact(read)['message']; self.assertEqual(item['status'], 'sent')
        self.assertIn('Exact received prompt', item['turn_binding_coverage'])
        seq = item['event_seq']; self.assertEqual(self.native_fact(read)['message']['event_seq'], seq)
        with self.assertRaises(HarnessError): self.native_fact({**read, 'reference': 'Different content under same observation id'})

    def test_native_fact_conflicting_turn_and_late_real_post_remain_unknown(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        actual = self.event(session_id='ordinary-writer'); handle_hook(self.state, actual, self.root)
        self.native_fact(self.send_fact(turn='management-turn'), self.wait_fact(assessed=False))
        self.native_fact(self.wait_fact('wrong-turn', turn='different-turn', assessed=False))
        self.assertEqual(self.show()['status'], 'unknown')
        handle_hook(self.state, {**actual, 'hook_event_name': 'PostToolUse', 'tool_response': {'isError': False}}, self.root)
        self.assertEqual(self.show()['status'], 'unknown'); self.assertEqual(self.show()['post_outcome'], 'sent')
        self.assertTrue(self.show()['native_conflicts'])

    def test_native_fact_actual_pre_rejection_cannot_become_admission_or_be_washed_by_post(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        self.state.update('apple-materials', 1, {'next_action': 'Changed original target state'}, 'actual target progress')
        actual = self.event(session_id='ordinary-writer'); self.deny(actual)
        self.assertEqual(self.show()['status'], 'invalidated')
        item = self.native_fact(self.send_fact(), self.wait_fact())['message']
        self.assertEqual(item['status'], 'unknown'); self.assertEqual(item['native_terminal_status'], 'completed')
        self.assertEqual(item['native_coverage']['guard'], 'actual_rejection_observed')
        self.assertNotIn('guard_evaluation_id', item); self.assertNotIn('host_call_id', item)
        handle_hook(self.state, {**actual, 'hook_event_name': 'PostToolUse', 'tool_response': {'isError': False}}, self.root)
        self.assertEqual(self.show()['status'], 'unknown')

    def test_native_fact_late_consumption_blocks_already_prepared_new_id(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        self.state.update('harness-daily-coordination', 1, {'next_action': 'Fresh phase'}, 'management progress')
        self.deny(self.event(session_id='ordinary-writer'))
        refreshed = {**request, 'id': 'new-id-before-late-native-fact', 'caller_work': {**request['caller_work'], 'revision': 2},
            'delegation': {**request['delegation'], 'author_work': {**request['delegation']['author_work'], 'revision': 2}}}
        prepare_coordination_message(self.state, refreshed)
        self.native_fact(self.send_fact(), self.wait_fact())
        self.deny(self.event(session_id='ordinary-writer', tool_use_id='new-actual-call'))
        self.assertEqual(coordination_message_show(self.state, refreshed['id'])['status'], 'prepared')

    def test_native_fact_message_or_terminal_contradiction_stays_unknown(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        bad = self.send_fact(); bad['params'] = {**self.message, 'prompt': 'Different actual message'}
        self.assertEqual(self.native_fact(bad, self.wait_fact())['message']['status'], 'unknown')
        self.assertTrue(self.show()['native_conflicts'])
        # Later actual observations preserve both terminal facts without clearing the conflict.
        self.native_fact(self.send_fact('actual-matching-send', turn='management-turn'), self.wait_fact('actual-terminal', assessed=False))
        self.native_fact(self.wait_fact('contradictory-terminal', status='failed', assessed=False))
        self.assertEqual(self.show()['status'], 'unknown')
        self.assertEqual(self.show()['native_terminal_status'], 'completed')
        self.assertTrue(any('terminal' in value['reason'] for value in self.show()['native_conflicts']))

    def test_native_fact_terminal_before_send_preserves_observation_until_actual_ack(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        item = self.native_fact(self.wait_fact())['message']
        self.assertEqual(item['status'], 'unknown'); self.assertEqual(item['native_terminal_status'], 'completed')
        self.assertFalse(item.get('native_send_observed', False))
        item = self.native_fact(self.send_fact())['message']
        self.assertEqual(item['status'], 'sent'); self.assertEqual(item['native_terminal_status'], 'completed')
        self.assertFalse(item['native_conflicts'])

    def test_native_fact_source_once_survives_status_revision_change_and_cli_refuses_readonly_write(self):
        request = self.delegated_request(); prepare_coordination_message(self.state, request)
        path = self.root/'fact.json'; path.write_text(json.dumps({'id': 'coord-1', 'source': 'Actual receipt', 'receipts': [self.send_fact(), self.wait_fact()]}))
        script = Path(__import__('harness').__file__).parents[1]/'scripts/harness.py'
        argv = [sys.executable, str(script), '--root', str(self.root), '--store', str(self.state.directory)]
        denied = subprocess.run([*argv, '--read-only', 'coordination-message', 'fact', '--file', str(path)], capture_output=True, text=True)
        self.assertEqual(denied.returncode, 2)
        result = subprocess.run([*argv, 'coordination-message', 'fact', '--file', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(json.loads(result.stdout)['message']['status'], 'sent')
        source = self.state.judgment('saved-dot-intent')
        updated = self.state.judge(source['id'], source['work_id'], 1, 'adopted', source['body'], 'Status change is not a new intent')
        replay = {**request, 'id': 'do-not-reissue-adopted', 'delegation': {**request['delegation'],
            'origin': {**request['delegation']['origin'], 'revision': updated['revision']}}}
        with self.assertRaises(HarnessError): prepare_coordination_message(self.state, replay)


if __name__ == '__main__': unittest.main()
