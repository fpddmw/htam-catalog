import json
import multiprocessing
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from harness.common import HarnessError
from harness.state import State
from harness.runtime import Runtime, native_hash
from harness.native_entry import NativeEntry, PROVIDER
from harness import goals


def race_accept(directory, root, request, barrier, queue):
    state = State(Path(directory), Path(root))
    barrier.wait()
    try:
        NativeEntry(state).accept(request); queue.put('ok')
    except HarnessError as exc:
        queue.put(exc.code)
    finally:
        state.close()


class EntryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.state = State(self.root/'store', self.root); self.runtime = Runtime(self.state); self.entry = NativeEntry(self.state)
        self.state.update('w', 0, {'title': 'Local evidence', 'request': 'Read existing evidence', 'request_source': 'user request',
            'objective': 'Resolve an existing boundary', 'status': 'active'}, 'fixture')
        (self.root/'result.md').write_text('Existing scoped evidence')
        self.runtime.owner('w', 'dot', 0, 'fixture')
        self.runtime.policy('daily', 0, {'execution_limit': 2, 'review_limit': 1, 'review_backlog_limit': 5,
            'budget': {'unit': 'model_turn', 'limit': 10, 'unknown_policy': 'reserve', 'unknown_reserve': 1}}, 'fixture')

    def tearDown(self):
        self.state.close(); self.temp.cleanup()

    def decision(self, tasks):
        return {'action': 'continue', 'reason': 'Current local question is useful', 'expected_gain': 'A usable bounded decision', 'tasks': tasks,
            'assessments': [{'requirement': 'overall', 'state': 'open', 'remaining': ['Boundary remains'], 'supports': [], 'limitations': [], 'reason': 'Local evidence stage'}]}

    def prepare(self, name='a', thread='worker', **patch):
        task = 't-'+name
        completion = {'purpose': 'Bounded local decision', 'checks': [{'id': 'result', 'kind': 'files', 'description': 'Actual local output',
            'basis': 'This is the output used for this task', 'inputs': [{'path': 'result.md'}]}]}
        request = {'id': name, 'provider': PROVIDER, 'method': 'send_message', 'work': 'w', 'expected': self.state.get('w')['revision'],
            'task': task, 'task_body': {'expected': 'Local boundary finding', 'purpose': 'Use current local evidence', 'completion': completion},
            'decision': self.decision([task]), 'params': {'threadId': thread, 'prompt': 'Work '+name+'; admit exact local entry first'},
            'coordinator': 'dot', 'owner_revision': self.runtime.coordinator('w')['revision'], 'review_owner': 'dot', 'policy_id': 'daily',
            'entry_coverage': {'execution_location': 'Windows -> WSL; permissions not implied'}, 'source': 'authorized scope', **patch}
        return self.entry.prepare(request), request

    def sent(self, name='a', **patch):
        item = self.entry.get(name)
        receipt = {'id': 'send-'+name, 'kind': 'send', 'reference': 'native send '+name, 'observed_at': '2026-10-22T10:00:00Z',
            'request_sha256': item['request_sha256'], 'raw': {'structuredContent': {'threadId': item.get('thread_id') or 'new-worker',
                'turnId': 'turn-'+name, 'admissionOutcome': 'started'}}, **patch}
        return self.entry.fact({'id': name, 'receipt': receipt, 'source': 'actual native send'}), receipt

    def accept(self, name='a'):
        item = self.entry.get(name)
        return self.entry.accept({'id': name, 'expected': item['revision'], 'executor_session': item['thread_id'],
            'thread_id': item['thread_id'], 'turn_id': item['turn_id'], 'execution_location': {'distro': 'Ubuntu-24.04'}, 'source': 'actual receiver'})

    def returned(self, name='a'):
        item = self.entry.get(name)
        request = {'id': name, 'expected': item['revision'], 'executor_session': item['thread_id'], 'thread_id': item['thread_id'],
            'turn_id': item['turn_id'], 'result': {'answer': 'Existing candidate has a bounded endpoint', 'artifacts': ['result.md'], 'remaining': ['Retain scope']}, 'source': 'actual executor'}
        return self.entry.returned(request), request

    def terminal(self, name='a', status='completed', receipt_id=None):
        item = self.entry.get(name)
        return self.entry.fact({'id': name, 'source': 'native read', 'receipt': {'id': receipt_id or 'terminal-'+name, 'kind': 'read',
            'reference': 'native exact read', 'observed_at': '2026-10-22T10:05:00Z',
            'raw': {'threadId': item['thread_id'], 'latestTurn': {'id': item['turn_id'], 'status': status}}}})

    def review(self, name='a'):
        item = self.entry.get(name)
        review = {'artifacts': ['result.md'], 'answer': 'Accept this local bounded result', 'remaining': ['Retain scope'], 'reviewer': 'dot', 'review_mode': 'self'}
        request = {'id': name, 'expected': item['revision'], 'work_expected': self.state.get('w')['revision'], 'disposition': 'adopted',
            'review': review, 'review_origin': {'author': 'dot', 'provider': PROVIDER, 'reference': 'native actual Dot judgment', 'payload_sha256': native_hash(review)},
            'review_role': 'coordination', 'writer_session': 'worker', 'source': 'actual Dot judgment'}
        return self.entry.review(request), request

    def ready_return(self):
        self.prepare(); self.sent(); self.accept(); self.returned()

    def correct(self, text):
        from harness.coordination import change_preview
        self.state.bind('main', 'w', 'main', 'fixture')
        (self.root/'origin.txt').write_text('Synthetic exact human correction transcript')
        record = self.state.input('main', 'correction', text, raw_source={'kind': 'synthetic_human_test'})
        assessment = self.state.assess_input(record['id'], 0, {'kind': 'human_direct', 'reviewer': 'main', 'source_ref': 'synthetic transcript',
            'quote': text, 'reason': 'Fixture reviewed original source', 'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'fixture')
        patch = {'request': text}
        change = {'kind': 'correction', 'reason': 'Exact fixture correction', 'impact': 'Current scope', 'source': 'fixture', 'authorization': {
            'input_id': record['id'], 'input_sha256': record['input_sha256'], 'assessment_revision': assessment['revision'],
            'scope_revision': self.state._input_scope(record['id'], 'w')['scope_revision'], 'quote': text,
            'mutation_sha256': change_preview(self.state, 'w', patch)['mutation_sha256']}}
        return self.state.update('w', self.state.get('w')['revision'], {**patch, 'change': change}, 'fixture exact correction')


class OrdinaryEntryTests(EntryFixture):
    def test_result_before_terminal_and_duplicate_review_do_not_reopen(self):
        self.ready_return()
        self.assertIsNone(self.entry.get('a')['native_terminal_status'])
        self.assertEqual(self.state.task('t-a')['status'], 'returned')
        _, review_request = self.review()
        self.assertTrue(self.entry.review(review_request)['duplicate'])
        self.terminal()
        self.assertEqual(self.state.task('t-a')['status'], 'adopted')
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't-a')['status'], 'resolved')
        self.assertEqual(self.entry.get('a')['native_terminal_status'], 'completed')

    def test_terminal_before_result_retains_result_and_receiver(self):
        self.prepare(); self.sent(); self.accept(); self.terminal(); self.returned()
        queue = self.runtime._get('runtime_review_queue', 'task_id', 't-a')
        self.assertIsNotNone(queue['body']['return_event']); self.assertEqual(queue['owner'], 'dot')
        self.review()

    def test_return_replay_is_idempotent_and_changed_return_is_rejected(self):
        self.prepare(); self.sent(); self.accept(); _, req = self.returned()
        before = self.state.db.total_changes
        self.assertTrue(self.entry.returned(req)['duplicate']); self.assertEqual(before, self.state.db.total_changes)
        with self.assertRaises(HarnessError): self.entry.returned({**req, 'result': {**req['result'], 'answer': 'Different'}})

    def test_unknown_send_reconciles_exact_receipt_without_resending(self):
        prepared, req = self.prepare()
        self.entry.fact({'id': 'a', 'source': 'timeout', 'receipt': {'id': 'unknown-a', 'kind': 'unknown', 'reference': 'tool timeout',
            'observed_at': '2026-10-22T10:00:00Z', 'raw': {}}})
        self.assertFalse(self.entry.prepare(req)['send_once'])
        self.sent(); self.accept()
        self.assertEqual(self.state.task('t-a')['status'], 'running')

    def test_exact_receipt_replay_and_conflicting_identity(self):
        self.prepare(); _, receipt = self.sent()
        self.assertTrue(self.entry.fact({'id': 'a', 'source': 'replay', 'receipt': receipt})['duplicate'])
        with self.assertRaises(HarnessError): self.entry.fact({'id': 'a', 'source': 'conflict', 'receipt': {**receipt, 'reference': 'different'}})

    def test_new_correction_blocks_admission_but_keeps_send_evidence(self):
        self.prepare(); self.sent()
        self.correct('Correct scope')
        with self.assertRaises(HarnessError): self.accept()
        self.assertEqual(len(self.entry.get('a')['receipts']), 1)
        self.assertEqual(self.state.task('t-a')['status'], 'proposed')

    def test_direct_input_remains_unknown_and_blocks_next_business(self):
        self.prepare(); self.sent()
        self.state.bind('worker', 'w', 'executor', 'actual receiver')
        record = self.entry.call('input', {'session': 'worker', 'turn': 'direct', 'prompt': 'Use corrected local product boundary',
            'raw_source': {'provider': 'actual native source', 'kind': 'unknown'}})
        self.assertNotEqual(self.state.input_assessment(record['id'])['kind'], 'human_direct')
        with self.assertRaises(HarnessError): self.accept()

    def test_local_return_survives_changed_request(self):
        self.prepare(); self.sent(); self.accept()
        self.correct('New scope')
        self.returned()
        self.assertEqual(self.state.task('t-a')['status'], 'returned')
        with self.assertRaises(HarnessError): self.review()

    def test_wrong_turn_cannot_complete_or_claim(self):
        self.prepare(); self.sent()
        item = self.entry.get('a')
        with self.assertRaises(HarnessError): self.entry.accept({'id': 'a', 'expected': item['revision'], 'executor_session': 'worker', 'thread_id': 'worker', 'turn_id': 'wrong', 'source': 'claim'})
        with self.assertRaises(HarnessError): self.entry.fact({'id': 'a', 'source': 'wrong read', 'receipt': {'id': 'wrong', 'kind': 'read',
            'reference': 'read', 'observed_at': '2026-10-22T10:00:00Z', 'raw': {'threadId': 'worker', 'latestTurn': {'id': 'wrong', 'status': 'completed'}}}})

    def test_claim_is_exactly_once_and_existing_binding_is_preserved(self):
        self.prepare(); self.sent(); self.state.bind('worker', 'w', 'main', 'existing responsibility')
        with self.assertRaises(HarnessError): self.accept()
        self.assertEqual(self.state.binding('worker')['role'], 'main')

    def test_duplicate_claim_rejected(self):
        self.prepare(); self.sent(); self.accept()
        with self.assertRaises(HarnessError): self.accept()
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 1)

    def test_shared_resource_and_legacy_dispatch_are_blocked(self):
        self.prepare(resources=['shared-file']); self.sent(); self.accept()
        with self.assertRaisesRegex(HarnessError, 'shared object'): self.prepare('b', 'other-worker', resources=['shared-file'])
        self.assertFalse(self.state.db.execute("SELECT 1 FROM tasks WHERE id='t-b'").fetchone())
        self.state.create_task('legacy', 'w', self.state.get('w')['revision'], {'expected': 'Local query', 'purpose': 'Local query'}, 'fixture')
        self.state.decide('w', self.state.get('w')['revision'], self.decision(['legacy']), 'fixture')
        with self.assertRaisesRegex(HarnessError, 'Ordinary native'): self.state.reserve('legacy-send', 'legacy', 'worker', 'legacy caller', {'threadId': 'worker', 'prompt': 'query'})

    def test_unknown_cost_and_capacity_remain_until_native_terminal(self):
        self.runtime.policy('daily', 1, {'execution_limit': 1, 'review_limit': 1, 'review_backlog_limit': 5,
            'budget': {'unit': 'model_turn', 'limit': 10, 'unknown_policy': 'reserve', 'unknown_reserve': 1}}, 'fixture')
        self.ready_return()
        with self.assertRaisesRegex(HarnessError, 'capacity'): self.prepare('b', 'worker-b')
        self.terminal(); self.prepare('b', 'worker-b')
        self.assertEqual(self.runtime.budget('daily')['ordinary_reserved_actions'], 2)

    def test_create_is_entry_only_with_unknown_native_fields(self):
        _, req = self.prepare(method='create', params={'title': 'Normal task', 'prompt': 'Read only project entry'}, entry_only=True, session_choice_reason='Useful separate work')
        self.assertIsNone(self.entry.get('a')['thread_id'])
        self.assertNotIn('runtime_id', self.entry.get('a'))
        self.sent(); self.terminal()
        self.assertEqual(self.entry.get('a')['phase'], 'entry_ready')
        self.assertFalse(self.entry.recover('w')['pending_reviews'])
        self.assertFalse(self.state.db.execute('SELECT 1 FROM dispatches').fetchone())
        with self.assertRaises(HarnessError): self.accept()

    def test_read_can_bind_first_ack_only_with_exact_received_prompt(self):
        _, req = self.prepare()
        fact = {'id': 'read-first', 'kind': 'read', 'reference': 'receiver native read', 'observed_at': '2026-10-22T10:00:00Z',
            'raw': {'threadId': 'worker', 'latestTurn': {'id': 'turn-a', 'status': 'inProgress'}, 'items': []}}
        with self.assertRaises(HarnessError): self.entry.fact({'id': 'a', 'source': 'read', 'receipt': fact})
        fact['raw']['items'] = [{'id': 'actual-user-message', 'turnId': 'turn-a', 'role': 'user', 'text': req['params']['prompt'], 'truncated': False}]
        self.entry.fact({'id': 'a', 'source': 'read', 'receipt': fact}); self.accept()

    def test_steered_new_work_is_not_admitted(self):
        self.prepare(); self.sent(raw={'threadId': 'worker', 'turnId': 'turn-a', 'admissionOutcome': 'steered'})
        with self.assertRaises(HarnessError): self.accept()

    def test_release_requires_terminal_result_and_exact_evidence(self):
        self.ready_return()
        with self.assertRaises(HarnessError): self.entry.release({'id': 'a', 'expected': self.entry.get('a')['revision'], 'source': 'release', 'evidence': 'claim'})
        self.terminal()
        self.entry.release({'id': 'a', 'expected': self.entry.get('a')['revision'], 'source': 'actual release', 'evidence': 'Exact file writer closed, observed after native terminal'})

    def test_unconfirmed_ack_requires_read_reconciliation(self):
        self.prepare(); self.sent(raw={'threadId': 'worker', 'turnId': 'turn-a', 'admissionOutcome': 'unconfirmed'})
        with self.assertRaises(HarnessError): self.accept()
        self.entry.fact({'id': 'a', 'source': 'actual later read', 'receipt': {'id': 'progress-a', 'kind': 'read', 'reference': 'exact read',
            'observed_at': '2026-10-22T10:01:00Z', 'raw': {'threadId': 'worker', 'latestTurn': {'id': 'turn-a', 'status': 'inProgress'}}}})
        self.accept()

    def test_local_pause_fences_followup_and_independent_work_continues(self):
        self.prepare(); self.sent(); self.accept()
        plan = self.runtime.scope_plan(['t-a'])
        result = self.runtime.control('pause-a', 'pause', {'plan': plan, 'owner': 'dot', 'owner_revisions': {'w': 1}, 'reason': 'Actual scoped pause'}, 'fixture')
        self.assertEqual(result[0]['status'], 'unsupported')  # No native stop is fabricated.
        item = self.entry.get('a')
        with self.assertRaises(HarnessError): self.entry.call('check', {'id': 'a', 'executor_session': 'worker', 'thread_id': 'worker', 'turn_id': 'turn-a'})
        self.prepare('b', 'worker-b')
        self.returned()  # Already produced artifacts survive the local hold.

    def test_recovery_keeps_unprocessed_return_after_cursor_and_restart(self):
        self.ready_return(); page = self.entry.recover('w', limit=100)
        self.state.close(); self.state = State(self.root/'store', self.root); self.entry = NativeEntry(self.state); self.runtime = Runtime(self.state)
        resumed = self.entry.recover('w', after=page['next_cursor'])
        self.assertEqual(resumed['events'], []); self.assertEqual(resumed['pending_reviews'][0]['owner'], 'dot')
        self.review(); self.terminal()
        self.assertEqual(self.entry.recover('w')['pending_reviews'], [])

    def test_daily_capacity_does_not_consume_apt_review_policy(self):
        self.state.create_task('apt-other', 'w', self.state.get('w')['revision'], {'expected': 'Own existing result', 'purpose': 'Existing APT result'}, 'fixture')
        self.state.db.execute('INSERT INTO runtime_review_queue VALUES(?,?,?,?,?,?,?)', ('apt-other', None, 'apt', 1, 'claimed', '{}', '2026-10-22T09:00:00Z'))
        self.ready_return(); self.review()
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 'apt-other')['status'], 'claimed')

    def test_conflicting_terminal_needs_explicit_observed_resolution(self):
        self.ready_return(); self.terminal(); self.review()
        self.terminal(status='interrupted', receipt_id='contradiction')
        self.assertIsNotNone(self.entry.get('a')['conflict'])
        req = {'id': 'a', 'expected': self.entry.get('a')['revision'], 'coordinator': 'dot', 'owner_revision': 1,
            'selected_terminal_status': 'completed', 'evidence': {'reference': 'exact final native read', 'basis': 'Reviewer reconciled both observations', 'reviewer': 'dot'}, 'source': 'actual adjudication'}
        self.entry.resolve(req)
        self.assertEqual(self.state.task('t-a')['status'], 'returned')
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't-a')['status'], 'needs_review')
        self.review()

    def test_terminal_unadmitted_closure_requires_current_owner_and_exact_non_execution_evidence(self):
        self.prepare(resources=['shared-object']); self.sent()

        def close_request(name='a'):
            item = self.entry.get(name)
            return {'id': name, 'expected': item['revision'], 'coordinator': 'dot', 'owner_revision': 1,
                'cancel_unadmitted': True, 'selected_terminal_status': 'completed', 'source': 'actual boundary review',
                'evidence': {'reference': 'exact receiver refusal and native final read', 'basis': 'Receiver declined before business admission; no business actions occurred',
                    'reviewer': 'dot', 'business_not_executed': True, 'thread_id': item['thread_id'], 'turn_id': item['turn_id'],
                    'terminal_receipt_id': 'terminal-'+name}}

        with self.subTest('active cannot close'):
            with self.assertRaises(HarnessError): self.entry.resolve(close_request())
        self.entry.fact({'id': 'a', 'source': 'unknown observation', 'receipt': {'id': 'uncertain-a', 'kind': 'unknown',
            'reference': 'native observation unavailable', 'observed_at': '2026-10-22T10:01:00Z', 'raw': {}}})
        with self.subTest('unknown cannot close'):
            with self.assertRaises(HarnessError): self.entry.resolve(close_request())
        self.terminal()
        before = self.entry.get('a'); spend = self.runtime.budget('daily')['budget_committed']
        self.assertEqual(len(self.entry.recover('w')['pending_reviews']), 1)
        self.assertTrue(self.entry.recover('w')['attention'][0]['missing_return'])
        with self.subTest('current owner and exact refusal evidence required'):
            request = close_request()
            with self.assertRaises(HarnessError): self.entry.resolve({**request, 'owner_revision': 0})
            with self.assertRaises(HarnessError): self.entry.resolve({**request, 'evidence': {**request['evidence'], 'business_not_executed': False}})
            with self.assertRaises(HarnessError): self.entry.resolve({**request, 'evidence': {**request['evidence'], 'turn_id': 'wrong'}})
        closed = self.entry.resolve(close_request())
        self.assertEqual(closed['phase'], 'cancelled'); self.assertTrue(closed['resources_released'])
        self.assertEqual(closed['native_terminal_status'], 'completed'); self.assertEqual(closed['receipts'], before['receipts'])
        self.assertIsNone(closed['return_event']); self.assertTrue(closed['budget_claimed'])
        self.assertEqual(self.runtime.budget('daily')['budget_committed'], spend)
        self.assertEqual(self.state.task('t-a')['status'], 'proposed')
        self.assertEqual(self.entry.recover('w')['pending_reviews'], []); self.assertEqual(self.entry.recover('w')['attention'], [])
        self.terminal(receipt_id='later-same-terminal')
        self.assertEqual(self.entry.get('a')['phase'], 'cancelled')
        self.assertEqual(self.entry.recover('w')['pending_reviews'], []); self.assertEqual(self.entry.recover('w')['attention'], [])
        self.prepare('b', 'worker-b'); self.sent('b'); self.accept('b'); self.returned('b'); self.terminal('b'); self.review('b')
        self.assertEqual(self.entry.recover('w')['pending_reviews'], []); self.assertEqual(self.entry.recover('w')['attention'], [])
        with self.subTest('admitted item cannot close without a result'):
            with self.assertRaises(HarnessError): self.entry.resolve(close_request('b'))

    def test_ordinary_budget_is_finite_and_abort_is_not_a_send(self):
        prepared, request = self.prepare()
        self.assertEqual(self.runtime.budget('daily')['budget_committed'], 1)
        self.entry.fact({'id': 'a', 'source': 'positive no-send evidence', 'receipt': {'id': 'aborted-a', 'kind': 'not_sent',
            'observed_at': '2026-10-22T10:00:00Z', 'reference': 'preparation abandoned before invocation', 'reason': 'No external call made', 'raw': {}}})
        self.assertEqual(self.runtime.budget('daily')['budget_committed'], 0)
        self.assertFalse(self.entry.prepare(request)['send_once'])

    def test_two_processes_claim_only_one_execution(self):
        self.prepare(); self.sent(); item = self.entry.get('a')
        req = {'id': 'a', 'expected': item['revision'], 'executor_session': 'worker', 'thread_id': 'worker', 'turn_id': 'turn-a', 'source': 'receiver'}
        ctx = multiprocessing.get_context('fork'); barrier = ctx.Barrier(2); queue = ctx.Queue()
        workers = [ctx.Process(target=race_accept, args=(str(self.state.directory), str(self.root), req, barrier, queue)) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(10); self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(queue.get(timeout=1) for _ in workers), ['REVISION_CONFLICT', 'ok'])
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 1)

    def test_combined_return_preserves_result_when_next_package_is_stale(self):
        self.prepare(); self.sent(); self.accept(); item = self.entry.get('a')
        req = {'id': 'a', 'expected': item['revision'], 'executor_session': 'worker', 'thread_id': 'worker', 'turn_id': 'turn-a',
            'result': {'answer': 'Actual local result', 'artifacts': ['result.md'], 'remaining': []}, 'source': 'executor',
            'next_prepare': {'id': 'stale-next', 'provider': PROVIDER, 'method': 'send_message', 'work': 'w', 'expected': 0,
                'params': {'threadId': 'worker-b', 'prompt': 'Do existing independent work'}, 'entry_coverage': {'execution_location': 'known'}, 'source': 'original approved next package'}}
        result = self.entry.call('return', req)
        self.assertFalse(result['next_preparation']['send_once'])
        self.assertEqual(self.state.task('t-a')['status'], 'returned')
        self.assertEqual(len(self.entry.recover('w')['pending_reviews']), 1)

    def test_cli_recovery_is_readonly_and_mutations_are_refused(self):
        self.ready_return()
        script = Path(__import__('harness').__file__).parents[1]/'scripts/harness.py'
        req_file = self.root/'request.json'; req_file.write_text(json.dumps({'work': 'w'}))
        before = self.state.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        argv = [sys.executable, str(script), '--root', str(self.root), '--store', str(self.state.directory), '--read-only', 'native-entry']
        result = subprocess.run([*argv, 'recover', '--file', str(req_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['pending_reviews'][0]['owner'], 'dot')
        result = subprocess.run([*argv, 'input', '--file', str(req_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, self.state.db.execute('SELECT COUNT(*) FROM events').fetchone()[0])


class CoordinatorWriteTests(EntryFixture):
    def guard(self):
        current = self.state.get('w')
        return self.runtime.owner('w', 'dot', 1, 'explicit approved handoff', {'enforce_writes': True, 'expected_work_revision': current['revision'],
            'receipt': {'from_owner': 'dot', 'reference': 'native exact boundary ACK', 'provider': 'actual native thread', 'safe_boundary': True}})

    def actor(self, payload, owner='dot', revision=2):
        return {'coordinator': owner, 'owner_revision': revision, 'writer_session': 'ordinary-writer',
            'origin': {'author': owner, 'provider': PROVIDER, 'reference': 'actual message reference', 'payload_sha256': goals.fingerprint(payload)}}

    def test_legacy_owner_does_not_implicitly_enable_write_protection(self):
        self.state.update('w', self.state.get('w')['revision'], {'next_action': 'Legacy work continues'}, 'legacy caller')

    def test_decision_and_direct_update_use_same_owner_revision(self):
        self.guard(); decision = self.decision([])
        with self.assertRaises(HarnessError): self.state.decide('w', self.state.get('w')['revision'], decision, 'old main')
        with self.assertRaises(HarnessError): self.state.update('w', self.state.get('w')['revision'], {'next_action': 'APT overwrites parent'}, 'old main')
        self.state.decide('w', self.state.get('w')['revision'], decision, 'delegated Dot', actor=self.actor(decision))
        self.assertEqual(self.state.get('w')['decision']['responsible'], 'dot')
        patch = {'next_action': 'Non-APT proceeds'}
        self.state.update('w', self.state.get('w')['revision'], patch, 'delegated Dot', actor=self.actor(patch))
        with self.assertRaises(HarnessError): self.state.update('w', self.state.get('w')['revision'], patch, 'old coordinator', actor=self.actor(patch, revision=1))

    def test_delegated_payload_and_parent_child_scope_are_exact(self):
        self.guard(); patch = {'next_action': 'Specific Dot choice'}
        with self.assertRaises(HarnessError): self.state.update('w', self.state.get('w')['revision'], {'next_action': 'Altered'}, 'wrong delegation', actor=self.actor(patch))
        self.state.update('child', 0, {'title': 'APT', 'request': 'Own scientific work', 'request_source': 'user', 'objective': 'APT scope', 'status': 'active'}, 'unchanged child')
        self.state.update('child', 1, {'next_action': 'APT continues'}, 'original APT session')

    def test_owner_transfer_requires_ack_and_invalidates_prior_decision(self):
        self.guard(); d = self.decision([])
        self.state.decide('w', self.state.get('w')['revision'], d, 'Dot', actor=self.actor(d))
        with self.assertRaises(HarnessError): self.runtime.owner('w', 'other', 2, 'silent transfer')
        self.runtime.owner('w', 'other', 2, 'actual natural boundary', {'enforce_writes': True, 'expected_work_revision': self.state.get('w')['revision'],
            'receipt': {'from_owner': 'dot', 'reference': 'actual Dot ACK', 'provider': PROVIDER, 'safe_boundary': True}})
        self.assertEqual(self.state.get('w')['decision']['validity'], 'needs_reconciliation')
        with self.assertRaises(HarnessError): self.state.decide('w', self.state.get('w')['revision'], d, 'old Dot', actor=self.actor(d))


if __name__ == '__main__': unittest.main()
