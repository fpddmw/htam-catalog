"""Exact return handling uses fixture stores; no native sessions or live state."""
from copy import deepcopy
import unittest

from harness.common import HarnessError, encoded
from harness.native_entry import LOCAL_PROVIDER
from harness.runtime import native_hash
import test_local_coordination as fixtures


class NativeReviewIdentityTests(unittest.TestCase):
    setUp = fixtures.LocalCoordinationTests.setUp
    tearDown = fixtures.LocalCoordinationTests.tearDown
    task = fixtures.LocalCoordinationTests.task
    result = staticmethod(fixtures.LocalCoordinationTests.result)
    receive_request = fixtures.LocalCoordinationTests.receive_request
    prepare_local = fixtures.LocalCoordinationTests.prepare_local
    local_ack = fixtures.LocalCoordinationTests.local_ack
    stage_start = fixtures.LocalCoordinationTests.stage_start
    stage_finish = fixtures.LocalCoordinationTests.stage_finish
    prepare_feedback = fixtures.LocalCoordinationTests.prepare_feedback

    def receive(self, identifier, turn=None):
        request = self.receive_request(id=identifier, result=self.result())
        request['receipt']['id'] = 'read-' + identifier
        request['receipt']['raw']['latestTurn']['id'] = turn or 'turn-' + identifier
        return self.entry.receive(request)['entry']

    def review_request(self, identifier, outcome='handled', mode='independent', **extra):
        item = self.entry.get(identifier)
        artifact = 'review-' + identifier + '.json'
        (self.root / artifact).write_text('{"fixture": "actual bounded judgment"}')
        review = {**self.result(artifacts=[artifact]), 'reviewer': item['review_owner'], 'review_mode': mode}
        request = {'id': identifier, 'expected': item['revision'], 'return_event': item['return_event'],
            'work_expected': self.state.get('w')['revision'], 'disposition': 'returned', 'stage_outcome': outcome,
            'review': review, 'review_role': 'independent' if mode == 'independent' else 'coordination',
            'review_origin': {'author': review['reviewer'], 'provider': LOCAL_PROVIDER,
                'reference': 'fixture original independent judgment', 'payload_sha256': native_hash(review)},
            'writer_session': 'fixture-writer', 'source': 'fixture exact stage judgment', **extra}
        if outcome is None:
            request.pop('stage_outcome')
        return request

    def queue(self):
        return self.runtime._get('runtime_review_queue', 'task_id', 't')

    def test_handled_stage_preserves_task_and_terminal_replays_do_not_reopen(self):
        self.task(); item = self.receive('first')
        request = self.review_request('first')
        result = self.entry.review(request)
        self.assertEqual(result['task']['status'], 'returned')
        self.assertNotIn('completion_result', result['task']['body']['reviews'][-1]['review'])
        self.assertEqual(result['entry']['return_review']['return_event'], item['return_event'])
        self.assertEqual(self.queue()['status'], 'resolved')
        before = self.state.db.total_changes
        self.assertTrue(self.entry.review(request)['duplicate'])
        self.assertEqual(before, self.state.db.total_changes)
        self.entry.fact({'id': 'first', 'source': 'fixture later native read', 'receipt': {
            'id': 'later-terminal', 'kind': 'read', 'reference': 'fixture exact terminal',
            'observed_at': '2026-10-24T08:00:00Z', 'raw': {'threadId': 'native-worker',
                'latestTurn': {'id': item['turn_id'], 'status': 'completed'}}}})
        self.state.review_task('t', 'returned', self.result(reviewer='native-worker', review_mode='self'), 'fixture repeated selfcheck')
        self.assertEqual(self.queue()['status'], 'resolved')
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 0)

    def test_old_return_handling_never_consumes_newer_queue(self):
        self.task(); first = self.receive('first'); second = self.receive('second')
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 2)
        before = deepcopy(self.queue())
        legacy = self.review_request('first', outcome=None)
        with self.assertRaisesRegex(HarnessError, 'Historic'):
            self.entry.review(legacy)
        wrong = self.review_request('first', return_event=second['return_event'])
        with self.assertRaisesRegex(HarnessError, 'exact native return'):
            self.entry.review(wrong)
        self.entry.review(self.review_request('first'))
        self.assertEqual(before, self.queue())
        page = self.entry.recover('w')
        self.assertEqual(page['pending_review_count'], 1)
        self.assertEqual(page['pending_reviews'][0]['body']['return_event'], second['return_event'])
        self.assertEqual(self.state.task('t')['status'], 'returned')
        self.assertEqual(self.entry.get('first')['return_review']['return_event'], first['return_event'])

    def test_selfcheck_and_read_do_not_supply_an_independent_stage_decision(self):
        self.task(); self.receive('first')
        with self.assertRaisesRegex(HarnessError, 'independent'):
            self.entry.review(self.review_request('first', mode='self'))
        self.entry.review(self.review_request('first', outcome=None, mode='self'))
        self.assertTrue(self.entry.get('first')['reviewed_at'])
        self.assertEqual(self.queue()['status'], 'pending')
        self.runtime.on_review('t', 'adopted')
        self.assertEqual(self.queue()['status'], 'pending')
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 1)

    def test_historical_native_stage_does_not_change_a_later_owned_runtime_queue(self):
        self.task(); self.receive('first')
        self.state.db.execute("UPDATE runtime_review_queue SET body=?,status='claimed' WHERE task_id='t'",
            (encoded({'native_event_id': 'fixture newer owned runtime return', 'policy_id': 'workspace'}),))
        before = deepcopy(self.queue())
        self.entry.review(self.review_request('first'))
        self.assertEqual(self.queue(), before)

    def test_exact_low_level_stage_review_records_handling_without_queue_projection(self):
        self.task(); item = self.receive('first')
        self.state.db.execute("DELETE FROM runtime_review_queue WHERE task_id='t'")
        review = {**self.result(), 'reviewer': item['review_owner'], 'review_mode': 'independent',
            'review_role': 'independent', 'native_entry_id': item['id'],
            'return_event': item['return_event'], 'stage_outcome': 'handled'}
        task = self.state.review_task('t', 'returned', review, 'fixture exact review with missing projection')
        saved = self.entry.get('first')
        self.assertEqual(saved['return_review']['outcome'], 'handled')
        self.assertEqual(saved['return_review']['return_event'], item['return_event'])
        self.assertEqual(task['status'], 'returned')
        self.assertEqual(task['body']['reviews'][-1]['review']['stage_outcome'], 'handled')
        self.assertIsNone(self.runtime._get('runtime_review_queue', 'task_id', 't', True))
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 0)

    def test_explicit_unresolved_and_revise_keep_the_stage_open(self):
        self.task(); self.receive('first')
        for outcome, expected in [('unresolved', 'pending'), ('revise', 'revise'), ('handled', 'resolved')]:
            self.entry.review(self.review_request('first', outcome=outcome))
            self.assertEqual(self.queue()['status'], expected)
            self.assertEqual(self.state.task('t')['status'], 'returned')
        self.assertEqual(self.entry.recover('w')['pending_review_count'], 0)

    def test_stage_handling_does_not_bypass_task_adoption_evidence(self):
        self.task(); self.receive('first'); self.entry.review(self.review_request('first'))
        request = self.review_request('first', outcome=None, disposition='adopted')
        with self.assertRaisesRegex(HarnessError, 'Adoption evidence'):
            self.entry.review(request)
        self.assertEqual(self.state.task('t')['status'], 'returned')
        self.assertEqual(self.queue()['status'], 'resolved')

    def test_mapped_task_consumers_require_exact_return_and_abandonment_evidence(self):
        self.task(); self.receive('first')
        for disposition in ('adopted', 'revise', 'abandoned'):
            with self.subTest(disposition=disposition), self.assertRaisesRegex(HarnessError, 'native-entry review for first'):
                self.state.review_task('t', disposition, self.result(reviewer='local-main', review_mode='self'), 'fixture ambiguous task review')
        request = self.review_request('first', outcome=None, disposition='abandoned')
        request['review'].pop('artifacts')
        request['review_origin']['payload_sha256'] = native_hash(request['review'])
        with self.assertRaisesRegex(HarnessError, 'closure missing artifacts'):
            self.entry.review(request)
        self.assertEqual(self.queue()['status'], 'pending')
        self.entry.review(self.review_request('first', outcome=None, disposition='abandoned'))
        self.assertEqual(self.queue()['status'], 'resolved')
        self.assertEqual(self.state.task('t')['status'], 'abandoned')

    def test_historic_explicit_review_preserves_running_long_lived_task_status(self):
        self.task(); self.receive('first'); second = self.receive('second')
        self.state.db.execute("UPDATE tasks SET status='running' WHERE id='t'")
        self.entry.review(self.review_request('first'))
        self.assertEqual(self.state.task('t')['status'], 'running')
        self.assertEqual(self.queue()['body']['return_event'], second['return_event'])
        self.assertEqual(self.queue()['status'], 'pending')

    def test_legacy_history_is_visible_separately_and_recovery_is_read_only(self):
        self.task(); self.receive('first'); self.receive('second')
        item = self.entry.get('first'); item.pop('return_review')
        item.update(reviewed_at='2026-10-24T08:00:00Z', review_disposition='returned')
        self.entry._save(item, 'fixture legacy timestamp without outcome')
        before = self.state.db.total_changes
        page = self.entry.recover('w', limit=1)
        self.assertEqual(before, self.state.db.total_changes)
        self.assertEqual(page['pending_review_count'], 1)
        self.assertEqual(page['legacy_return_count'], 1)
        self.assertEqual(page['legacy_returns'][0]['status'], 'legacy_unresolved')
        self.assertEqual(page['legacy_returns'][0]['body']['native_entry_id'], 'first')

    def test_stage_migration_preserves_existing_continuation_proof_and_history(self):
        self.stage_start(); self.stage_finish()
        original = self.entry.review(self.review_request('local', outcome=None))['entry']
        feedback = self.prepare_feedback()['entry']
        old_hash = original['review_request_sha256']
        request = self.review_request('local')
        prior = self.state.task('t')['body']['reviews'][-1]
        request['existing_review'] = {'at': prior['at'],
            'payload_sha256': prior['review']['native_review_origin']['payload_sha256']}
        migrated = self.entry.review(request)['entry']
        self.assertEqual(migrated['review_request_sha256'], old_hash)
        self.assertEqual(migrated['reviewed_at'], original['reviewed_at'])
        self.assertEqual(migrated['stage_review'], original['stage_review'])
        self.assertEqual(feedback['continuation_of']['review_request_sha256'], old_hash)
        self.assertIn('local', self.entry._continuation_ancestors(feedback))
        self.assertTrue(self.entry.review(request)['duplicate'])
        self.assertEqual(len(self.state.task('t')['body']['reviews']), 3)


if __name__ == '__main__':
    unittest.main()
