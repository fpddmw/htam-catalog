"""Task finding handoff only; every store and evidence file is an isolated fixture."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.common import HarnessError
from harness.context import hook_context, improvement_items, render_context
from harness.native_entry import LOCAL_PROVIDER, NativeEntry
from harness.runtime import Runtime, native_hash
from harness.state import State


class TaskFindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', self.root)
        self.entry = NativeEntry(self.state)
        self.runtime = Runtime(self.state)
        self.state.update('w', 0, {'title': 'Existing copper research', 'request': 'Assess native reuse',
            'request_source': 'fixture human request', 'objective': 'Bounded research use', 'status': 'active'}, 'fixture')
        self.state.bind('main', 'w', 'main', 'fixture')
        (self.root / 'result.md').write_text('Unchanged scientific package')
        self.ref = 'runs/delivery/review-notes/routing/review-item.json'
        (self.root / self.ref).parent.mkdir(parents=True)
        (self.root / 'runs/delivery/tooling').mkdir()
        (self.root / 'runs/delivery/tooling/check.json').write_text('{"local_check": true}')
        self.document = {'schema': 'htam.execution-review-item/v1', 'work': 'w', 'task': 't',
            'native_entry': 'existing', 'review_owner': 'main', 'status': 'pending_main_review',
            'problem': 'Public process list omitted rule_verification, model_id, model_version and json_ordered.',
            'current_workaround': 'Isolated task-local candidate; shared CLI remains original.',
            'disposition': 'Direct user approval request withdrawn; main reviewer handles shared tooling.',
            'evidence': ['../../tooling/check.json']}
        self.write_document()
        self.receive()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def write_document(self, **extra):
        (self.root / self.ref).write_text(json.dumps({**self.document, **extra}), encoding='utf-8')

    def receive(self, identifier='existing', task='t', worker='native-worker'):
        self.state.create_task(task, 'w', self.state.get('w')['revision'],
            {'expected': 'Bounded existing result', 'purpose': 'Assess existing evidence'}, 'fixture')
        return self.entry.receive({'id': identifier, 'work': 'w', 'task': task,
            'expected': self.state.get('w')['revision'], 'review_owner': 'main', 'source': 'fixture actual read',
            'receipt': {'id': 'read-' + identifier, 'reference': 'fixture native receipt',
                'observed_at': '2026-10-25T08:00:00Z',
                'raw': {'threadId': worker, 'latestTurn': {'id': 'turn-' + identifier, 'status': 'completed'}}}})

    def finish_request(self, **extra):
        item = self.entry.get('existing')
        return {'id': item['id'], 'expected': item['revision'], 'executor_session': item['thread_id'],
            'turn_id': item['turn_id'], 'result': {'answer': 'Existing bounded copper delivery',
                'artifacts': ['result.md'], 'remaining': ['Shared CLI approval pending at delivery time']},
            'source': 'fixture actual executor return', **extra}

    def supplement(self, findings=None, **extra):
        item = self.entry.get('existing')
        return self.state.observation('copper-tooling', 'note', {'improvement_trigger': 'task_finding',
            'native_entry': item['id'], 'return_event': item['return_event'], 'writer_session': 'maintenance-writer',
            'findings': [{'ref': self.ref}] if findings is None else findings, **extra}, 'fixture recorded executor material')

    def counts(self):
        return {kind: self.state.db.execute('SELECT count(*) FROM events WHERE kind=?', (kind,)).fetchone()[0]
            for kind in ('native_entry_result', 'improvement_candidate', 'improvement_supplement', 'improvement_triaged')}

    def handle_scientific_return(self):
        item = self.entry.get('existing')
        review = {'answer': 'Handle exact bounded scientific return', 'artifacts': ['result.md'], 'remaining': [],
            'reviewer': 'main', 'review_mode': 'independent'}
        return self.entry.review({'id': item['id'], 'expected': item['revision'],
            'work_expected': self.state.get('w')['revision'], 'disposition': 'returned', 'stage_outcome': 'handled',
            'review': review, 'review_role': 'independent', 'writer_session': 'main',
            'review_origin': {'author': 'main', 'provider': LOCAL_PROVIDER, 'reference': 'fixture exact review',
                'payload_sha256': native_hash(review)}, 'source': 'fixture scientific handling'})

    def triage(self, candidate, disposition='no_change', **extra):
        return self.state.triage('w', candidate, {'disposition': disposition,
            'reason': 'Keep task-local workaround; shared change remains a separate decision',
            'next_action': 'Revisit when new evidence warrants a shared implementation',
            'evidence': ['result.md'], **extra}, 'fixture actual main triage')

    def test_reference_first_finding_has_context_content_origin_and_independent_triage(self):
        saved = self.entry.finish(self.finish_request(findings=[{'ref': self.ref}]))
        candidate = saved['findings'][0]['event']
        self.assertTrue(saved['findings'][0]['asset'].startswith('task-finding:'))
        view = render_context(self.state, session='main')['improvement_candidates'][0]
        self.assertEqual(view['event'], candidate)
        self.assertEqual(view['problem'], self.document['problem'])
        self.assertEqual(view['task'], 't')
        self.assertEqual(view['native_entry'], 'existing')
        self.assertEqual(view['reference']['path'], self.ref)
        self.assertEqual(view['producer_session'], 'native-worker')
        self.assertEqual(view['writer_session'], 'native-worker')
        self.assertEqual(view['review_owner'], 'main')
        self.assertIn('withdrawn', view['routing'])
        paths = [ref['path'] for ref in improvement_items(self.state, 'w')[0]['evidence']]
        self.assertIn('runs/delivery/tooling/check.json', paths)
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't')['status'], 'pending')
        self.triage(candidate)
        self.assertEqual(improvement_items(self.state, 'w'), [])
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't')['status'], 'pending')
        self.assertEqual(self.state.task('t')['status'], 'returned')
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM observations WHERE kind='sync'").fetchone()[0], 0)

    def test_return_replays_and_same_reference_id_are_idempotent(self):
        request = self.finish_request(findings=[{'id': 'readmeta', 'ref': self.ref}])
        result = self.entry.finish(request)
        before = self.state.db.total_changes
        replay = self.entry.finish({**request, 'findings': [{'ref': self.ref}]})
        self.assertTrue(replay['duplicate'])
        self.assertTrue(replay['findings'][0]['duplicate'])
        self.assertEqual(result['findings'][0]['event'], replay['findings'][0]['event'])
        self.assertTrue(self.supplement()['duplicate'])
        self.assertEqual(self.state.db.total_changes, before)
        self.assertEqual(self.counts()['improvement_candidate'], 1)

    def test_new_evidence_supplements_and_reopens_only_improvement_attention(self):
        request = self.finish_request(findings=[{'ref': self.ref}])
        candidate = self.entry.finish(request)['findings'][0]['event']
        self.handle_scientific_return()
        self.assertEqual(len(improvement_items(self.state, 'w')), 1)  # Science review did not triage it.
        self.triage(candidate, 'addressed')
        scientific = deepcopy(self.entry.get('existing'))
        task = deepcopy(self.state.task('t'))
        queue = deepcopy(self.runtime._get('runtime_review_queue', 'task_id', 't'))
        (self.root / 'new-evidence.json').write_text('{"new_actual_evidence": true}')
        findings = [{'candidate_event': candidate, 'evidence': ['new-evidence.json'],
            'routing': 'User correction routes shared changes to main; original return stays historical.'}]
        added = self.supplement(findings)
        self.assertFalse(added['duplicate'])
        current = improvement_items(self.state, 'w')[0]
        self.assertEqual(current['event'], candidate)
        self.assertEqual(current['attention'], 'new_finding_evidence')
        self.assertEqual(current['previous_disposition'], 'addressed')
        self.assertEqual(current['writer_session'], 'maintenance-writer')
        self.assertEqual(current['producer_session'], 'native-worker')
        self.assertEqual(current['original_origin']['writer_session'], 'native-worker')
        self.assertEqual(self.entry.get('existing'), scientific)
        self.assertEqual(self.state.task('t'), task)
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't'), queue)
        self.assertEqual(queue['status'], 'resolved')
        before = self.state.db.total_changes
        self.assertTrue(self.supplement(findings)['duplicate'])
        self.assertTrue(self.entry.finish(request)['findings'][0]['duplicate'])
        self.assertEqual(self.state.db.total_changes, before)
        self.assertEqual(improvement_items(self.state, 'w')[0]['routing'], findings[0]['routing'])
        self.assertEqual(self.counts()['improvement_supplement'], 1)
        before = self.counts()
        for version in ({'finding_event': candidate}, {}):
            with self.subTest(version=version), self.assertRaisesRegex(HarnessError, 'latest_event as finding_event'):
                self.triage(candidate, 'deferred', **version)
            self.assertEqual(self.counts(), before)
        self.triage(candidate, 'deferred', finding_event=current['latest_event'])
        self.assertEqual(improvement_items(self.state, 'w'), [])
        (self.root / 'result.md').write_text('Changed triage evidence')
        self.assertEqual(improvement_items(self.state, 'w')[0]['attention'], 'triage_evidence_changed')

    def test_post_delivery_maintenance_import_preserves_old_return_and_handled_state(self):
        self.entry.finish(self.finish_request())
        self.handle_scientific_return()
        item = deepcopy(self.entry.get('existing'))
        task = deepcopy(self.state.task('t'))
        queue = deepcopy(self.runtime._get('runtime_review_queue', 'task_id', 't'))
        self.write_document(prior_return_event=item['return_event'])
        saved = self.supplement()
        self.assertFalse(saved['duplicate'])
        self.assertEqual(saved['requested_asset'], 'copper-tooling')
        self.assertEqual(saved['findings'][0]['asset'], 'copper-tooling')
        view = render_context(self.state, session='main')['improvement_candidates'][0]
        self.assertEqual(view['asset'], 'copper-tooling')
        self.assertEqual(view['return_event'], item['return_event'])
        self.assertEqual(view['return_sha256'], item['return_sha256'])
        self.assertIn('withdrawn', view['routing'])
        self.assertEqual(self.entry.get('existing'), item)
        self.assertEqual(self.state.task('t'), task)
        self.assertEqual(self.runtime._get('runtime_review_queue', 'task_id', 't'), queue)

    def test_wrong_reference_scope_or_missing_evidence_rolls_back_entire_first_return(self):
        original = deepcopy(self.entry.get('existing'))
        before = self.counts()
        for conflict in ({'work': 'other'}, {'task': 'other'}, {'native_entry': 'other'},
                         {'prior_return_event': 999999}, {'evidence': ['../../../../../../outside.txt']}):
            with self.subTest(conflict=conflict):
                self.write_document(**conflict)
                with self.assertRaises(HarnessError):
                    self.entry.finish(self.finish_request(findings=[{'problem': 'First valid selected finding',
                        'evidence': ['result.md']}, {'ref': self.ref}]))
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.entry.get('existing'), original)
                self.assertEqual(self.state.task('t')['body'].get('reviews', []), [])
                self.assertIsNone(self.runtime._get('runtime_review_queue', 'task_id', 't', True))

    def test_supplement_scope_mismatch_is_rejected_without_scientific_or_improvement_mutation(self):
        self.entry.finish(self.finish_request())
        before = self.counts()
        item = deepcopy(self.entry.get('existing'))
        for conflict in ({'work': 'other'}, {'task': 'other'}, {'return_event': 999999},
                         {'producer_session': 'maintenance-writer'}, {'review_owner': 'maintenance-writer'}):
            with self.subTest(conflict=conflict), self.assertRaises(HarnessError):
                self.supplement(**conflict)
            self.assertEqual(self.counts(), before)
            self.assertEqual(self.entry.get('existing'), item)

    def test_legacy_no_findings_return_and_replay_keep_existing_contract(self):
        request = self.finish_request()
        first = self.entry.finish(request)
        before = self.state.db.total_changes
        second = self.entry.finish(request)
        self.assertNotIn('findings', first)
        self.assertNotIn('findings', second)
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.state.db.total_changes, before)
        self.assertEqual(self.counts()['improvement_candidate'], 0)

    def test_same_problem_on_another_task_stays_separate_and_cross_task_link_is_rejected(self):
        first = self.entry.finish(self.finish_request(findings=[{'id': 'one', 'problem': 'Same exact defect',
            'evidence': ['result.md']}]))['findings'][0]['event']
        self.receive('another', 'other-task', 'other-worker')
        item = self.entry.get('another')
        request = {'id': item['id'], 'expected': item['revision'], 'executor_session': item['thread_id'],
            'turn_id': item['turn_id'], 'result': self.finish_request()['result'], 'source': 'fixture another return',
            'findings': [{'id': 'two', 'problem': 'Same exact defect', 'evidence': ['result.md']}]}
        second = self.entry.finish(request)['findings'][0]['event']
        self.assertNotEqual(first, second)
        self.assertEqual(self.counts()['improvement_candidate'], 2)
        before = self.counts()
        scientific = deepcopy(self.entry.get('another'))
        with self.assertRaisesRegex(HarnessError, 'same task'):
            self.entry.finish({**request, 'findings': [{'candidate_event': first, 'problem': 'Same exact defect',
                'evidence': ['result.md']}]})
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.entry.get('another'), scientific)

    def test_task_finding_cannot_supplement_other_candidate_kinds(self):
        self.state.observation('legacy-topic', 'note', {'improvement_trigger': 'user_reported_recurrence',
            'work': 'w', 'scope': 'Legacy explicit recurrence', 'evidence': 'fixture actual native turn'}, 'fixture')
        legacy = self.state.db.execute("SELECT max(seq) FROM events WHERE kind='improvement_candidate'").fetchone()[0]
        self.entry.finish(self.finish_request())
        before = self.counts()
        scientific = deepcopy(self.entry.get('existing'))
        with self.assertRaisesRegex(HarnessError, 'original task_finding'):
            self.supplement([{'candidate_event': legacy, 'problem': 'New task finding', 'evidence': ['result.md']}])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.entry.get('existing'), scientific)

    def test_bounded_context_keeps_problem_identity_and_omission_count(self):
        findings = [{'id': str(i), 'problem': 'Explicit defect ' + str(i), 'evidence': ['result.md']} for i in range(5)]
        self.entry.finish(self.finish_request(findings=findings))
        raw = hook_context(self.state, 'main', budget=6500)
        compact = json.loads(raw.split('\n', 1)[1])
        self.assertLessEqual(len(raw.encode()), 6500)
        self.assertGreater(len(compact['improvement_candidates']), 0)
        self.assertEqual(len(compact['improvement_candidates']) + compact['omitted_improvement_candidates'], 5)
        for item in compact['improvement_candidates']:
            self.assertEqual(item['task'], 't')
            self.assertEqual(item['native_entry'], 'existing')
            self.assertIn('Explicit defect', item['problem'])

    def test_full_hook_reclaims_optional_detail_for_untriaged_finding(self):
        self.state.update('w', self.state.get('w')['revision'], {'constraints': [
            'Keep the original copper boundary and actual scientific return unchanged.']}, 'fixture')
        self.state.input('main', 'pending-clarification', 'Continue within the original bounded research scope.')
        self.entry.finish(self.finish_request())
        packet = {'mandatory': [{'constraint': 'Preserve the approved research scope.'}],
            'selected': [{'optional_excerpt': ''}], 'context_complete': True, 'required_load': None}
        with patch('harness.context.knowledge_context', side_effect=lambda *args, **kwargs: deepcopy(packet)):
            base = hook_context(self.state, 'main')
            packet['selected'][0]['optional_excerpt'] = 'x' * max(0, 6250 - len(base.encode()))
            before_raw = hook_context(self.state, 'main')
            before = json.loads(before_raw.split('\n', 1)[1])
            self.assertGreater(len(before_raw.encode()), 6100)
            self.assertTrue(before['ordinary_native']['pending_reviews'])
            finding = self.supplement()['findings'][0]
            after_raw = hook_context(self.state, 'main')
            after = json.loads(after_raw.split('\n', 1)[1])
        self.assertLessEqual(len(after_raw.encode()), 6500)
        item = after['improvement_candidates'][0]
        self.assertEqual(item['event'], finding['event'])
        self.assertEqual(item['latest_event'], finding['observation_event'])
        self.assertEqual(item['task'], 't')
        self.assertEqual(item['native_entry'], 'existing')
        self.assertEqual(item['reference']['path'], self.ref)
        self.assertEqual(item['entry'], {'command': 'context', 'task': 't'})
        self.assertIn('Public process list omitted', item['problem'])
        self.assertEqual(after['omitted_improvement_candidates'], 0)
        self.assertEqual(after['constraints'], before['constraints'])
        self.assertEqual(after['pending_inputs'], before['pending_inputs'])
        self.assertEqual(after['ordinary_native'], before['ordinary_native'])
        self.assertEqual(after['knowledge']['mandatory'], before['knowledge']['mandatory'])
        self.assertEqual(after['knowledge']['selected'], [])


    def test_declared_legacy_method_attachment_is_routed_without_top_level_findings(self):
        self.write_document(remaining_method_issues_for_main_triage=['Targeted Source get unavailable; full owner snapshot required.'])
        request=self.finish_request();request['result']['artifacts'].append(self.ref)
        answer=self.entry.finish(request)
        self.assertEqual(len(answer['findings']),1)
        self.assertEqual(improvement_items(self.state,'w')[0]['problem'],'Targeted Source get unavailable; full owner snapshot required.')
        self.assertEqual(self.entry.finish(request)['findings'][0]['event'],answer['findings'][0]['event'])
        self.assertEqual(self.counts()['native_entry_result'],1)

    def test_impact_priority_survives_early_oversize_fallback(self):
        request=self.finish_request(findings=[{'id':'risk','problem':'Known body mismatch followed by submission', 'severity':'action_blocking','evidence':['result.md']},
            {'id':'later','problem':'Later ordinary tooling inconvenience','evidence':['result.md']}])
        self.entry.finish(request)
        self.assertEqual(improvement_items(self.state,'w')[0]['severity'],'action_blocking')
        with patch('harness.context.knowledge_context',return_value={'mandatory':[],'selected':[{'text':'x'*24000}], 'context_complete':True,'required_load':None}):
            raw=hook_context(self.state,'main',budget=6500);data=json.loads(raw.split('\n',1)[1])
        self.assertLessEqual(len(raw.encode()),6500)
        self.assertEqual(data['improvement_candidates'][0]['severity'],'action_blocking')
        self.assertEqual(data['omitted_improvement_candidates'],1)


if __name__ == '__main__':
    unittest.main()
