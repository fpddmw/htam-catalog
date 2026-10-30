import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from harness.common import HarnessError
from harness.fulfillment import bind_contract, check_contract
from harness.state import State


class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', self.root)
        self.state.update('work', 0, {'title': 'LCA', 'request': 'Save a model', 'request_source': 'user',
                                    'objective': 'Account reuse', 'status': 'active'}, 'user')

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_current_cli_queue_receipt_consumes_bound_evidence_and_scope(self):
        from harness.fulfillment import _curation
        from harness.common import digest
        self.write('rows.json', [{'id': 'one', 'version': 'v1'}])
        self.write('evidence.json', {'scope': 'actual fixture'})
        checkpoint = {'completion_scope': 'Research draft', 'publication_ready': False,
                      'input_rows_sha256': digest(self.root / 'rows.json'),
                      'evidence': [{'path': 'evidence.json', 'sha256': digest(self.root / 'evidence.json')}]}
        self.write('checkpoint.json', checkpoint)
        row = {'entity_id': 'one', 'version': 'v1', 'entity_type': 'process', 'status': 'complete',
               'input_rows_file': 'rows.json', 'input_rows_sha256': digest(self.root / 'rows.json'),
               'checkpoint_file': 'checkpoint.json', 'checkpoint_sha256': digest(self.root / 'checkpoint.json')}
        check = {'entities': [{'id': 'one', 'version': 'v1', 'entity_type': 'process'}]}
        report = {'task_states': [row]}
        self.assertEqual(_curation(self.root, check, report)['entities'], 1)
        with self.assertRaises(HarnessError):
            _curation(self.root, {**check, 'publication_ready': True}, report)
        self.write('evidence.json', {'scope': 'changed'})
        with self.assertRaises(HarnessError):
            _curation(self.root, check, report)

    def remote(self, exists=True, owner='owner', state=0, bad_reference=False):
        payload = {'lifeCycleModelDataSet': {'amount': '1'}}
        self.write('models.json', [{'id': 'model', 'version': 'v1', 'json_ordered': payload}])
        h = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        identity = {'table': 'lifecyclemodels', 'id': 'model', 'version': 'v1'}
        row = {**identity, 'role': 'root', 'row_index': 0, 'path': '/lifeCycleModelDataSet', 'status': 'ok' if exists else 'missing_dataset'}
        rows = [row]
        if exists:
            rows.append({**row, 'path': row['path'] + '#readback', 'remote_user_id': owner,
                         'remote_state_code': state, 'local_payload_sha256': h, 'remote_payload_sha256': h})
        if bad_reference:
            rows.append({'role': 'reference', 'row_index': 0, 'status': 'missing_dataset'})
        # Unrelated failures must not prevent adoption of this exact model.
        rows.append({'role': 'reference', 'row_index': 1, 'status': 'missing_dataset'})
        p = self.root / 'remote-verification.jsonl'
        p.write_text('\n'.join(json.dumps(r) for r in rows))
        self.write('report.json', {'input_path': str(self.root / 'models.json'), 'counts': {'checked': len(rows)}, 'generated_at_utc': '2026-10-07T00:00:00Z'})
        return {'purpose': 'Saved model only', 'checks': [{'id': 'save', 'kind': 'tiangong-remote', 'description': 'Exact saved model',
                 'basis': 'Public verification of this payload', 'inputs': [{'path': 'models.json'}, {'path': 'remote-verification.jsonl'}],
                 'report': {'path': 'report.json'}, 'owner': 'owner', 'state': 0, 'roots': [identity]}]}

    def test_explanation_alone_cannot_complete_work(self):
        with self.assertRaises(HarnessError):
            self.state.update('work', 1, {'status': 'completed', 'outcome': 'All done'}, 'claim')
        self.assertEqual(self.state.get('work')['revision'], 1)

    def test_existing_local_model_does_not_establish_persistence(self):
        c = bind_contract(self.root, self.remote(exists=False))
        result = check_contract(self.root, c)
        self.assertFalse(result['passed'])
        self.assertIn('missing', result['blockers'][0])

    def test_exact_success_ignores_unrelated_blocked_root(self):
        c = bind_contract(self.root, self.remote())
        self.assertTrue(check_contract(self.root, c)['passed'])
        requirements = self.state.get('work')['requirements']
        requirements[0]['completion'] = c
        self.state.update('work', 1, {'requirements': requirements, 'completion': c}, 'bind whole requested result')
        self.state.decide('work', 2, {'action': 'deliver', 'reason': 'Exact requested model persisted',
            'expected_gain': 'Hand over the saved model', 'tasks': [], 'assessments': [{'requirement': 'overall',
                'state': 'satisfied', 'remaining': [], 'limitations': ['Unrelated root remains out of scope'],
                'supports': [], 'reason': 'Exact model root and its references passed'}]}, 'semantic review')
        self.state.update('work', 3, {'status': 'completed', 'outcome': 'Saved scoped model'}, 'exact readback')

    def test_wrong_account_state_or_selected_dependency_blocks(self):
        for kwargs in ({'owner': 'other'}, {'state': 20}, {'bad_reference': True}):
            with self.subTest(kwargs=kwargs):
                c = bind_contract(self.root, self.remote(**kwargs))
                self.assertFalse(check_contract(self.root, c)['passed'])

    def test_changed_input_invalidates_old_check_even_if_rebound(self):
        raw = self.remote()
        bound = bind_contract(self.root, raw)
        self.write('models.json', [{'id': 'model', 'version': 'v1', 'json_ordered': {'lifeCycleModelDataSet': {'amount': '2'}}}])
        self.assertFalse(check_contract(self.root, bound)['passed'])
        # Merely refreshing the file hashes cannot turn an old remote receipt current.
        refreshed = bind_contract(self.root, raw)
        result = check_contract(self.root, refreshed)
        self.assertFalse(result['passed'])
        self.assertIn('older/different payload', result['blockers'][0])

    def test_post_submit_payload_match_does_not_hide_field_loss(self):
        raw = self.remote()
        self.write('curation.json', {'entities': [{'entity_type': 'flow', 'id': 'flow', 'version': 'v1', 'status': 'blocked',
                                                 'blockers': [{'code': 'sharing_scope_omitted'}]}]})
        raw['checks'].append({'id': 'scope', 'kind': 'tiangong-curation', 'description': 'Sharing scope retained',
            'basis': 'Before/after editor comparison', 'inputs': [{'path': 'models.json'}], 'report': {'path': 'curation.json'},
            'entities': [{'entity_type': 'flow', 'id': 'flow', 'version': 'v1'}]})
        checked = check_contract(self.root, bind_contract(self.root, raw))
        self.assertEqual([c['status'] for c in checked['checks']], ['passed', 'blocked'])

    def test_support_only_report_cannot_close_process_curation(self):
        self.write('rows.json', {})
        self.write('curation.json', {'entities': [{'entity_type': 'support', 'id': 'same', 'version': 'v1', 'status': 'passed', 'blockers': []}]})
        c = {'purpose': 'Process', 'checks': [{'id': 'curation', 'description': 'Current process', 'kind': 'tiangong-curation',
             'basis': 'Native process input', 'inputs': [{'path': 'rows.json'}], 'report': {'path': 'curation.json'},
             'entities': [{'entity_type': 'process', 'id': 'same', 'version': 'v1'}]}]}
        self.assertFalse(check_contract(self.root, bind_contract(self.root, c))['passed'])

    def test_exit_success_is_not_ruleset_success(self):
        self.write('rows.json', {})
        self.write('qa.json', {'status': 'completed_local_flow_qa', 'ruleset_gate': {'passed': False}})
        c = {'purpose': 'QA', 'checks': [{'id': 'qa', 'kind': 'json', 'description': 'Ruleset passed', 'basis': 'Native QA',
             'inputs': [{'path': 'rows.json'}], 'report': {'path': 'qa.json'}, 'assertions': [{'pointer': '/ruleset_gate/passed', 'equals': True}]}]}
        self.assertFalse(check_contract(self.root, bind_contract(self.root, c))['passed'])

    def test_question_does_not_change_request_on_acknowledgment(self):
        self.state.bind('chat', 'work', 'main', 'bind')
        note = self.state.input('chat', 'question', 'Why is this useful? I am only asking.')
        self.state.ack(note['id'], 'work', 1, 'Explanation only; no plan change requested')
        self.assertEqual(self.state.get('work')['request'], 'Save a model')
        self.assertEqual(self.state.get('work')['request_revision'], 1)

    def test_adoption_requires_evidence_and_refresh_revokes_old_adoption(self):
        raw = self.remote()
        self.state.create_task('t', 'work', 1, {'expected': 'Saved', 'purpose': 'Reuse'}, 'plan')
        review = {'artifacts': ['models.json'], 'answer': 'Saved', 'remaining': [], 'reviewer': 'main', 'review_mode': 'self'}
        self.state.review_task('t', 'returned', review, 'returned')
        with self.assertRaises(HarnessError):
            self.state.review_task('t', 'adopted', review, 'no evidence')
        self.state.set_task_completion('t', 1, raw, 'bind exact proof')
        self.state.review_task('t', 'adopted', review, 'current readback')
        self.state.set_task_completion('t', 2, raw, 'new observation needs re-adoption')
        self.assertEqual(self.state.task('t')['status'], 'returned')

    def test_report_mutation_does_not_silently_change_adopted_evidence(self):
        raw = self.remote()
        c = bind_contract(self.root, raw)
        self.write('report.json', {'counts': {'checked': 0}})
        self.assertFalse(check_contract(self.root, c)['passed'])

    def test_parent_rechecks_adopted_task_and_its_new_user_input(self):
        raw = self.remote()
        self.state.create_task('child', 'work', 1, {'expected': 'Saved', 'purpose': 'Reuse', 'completion': raw}, 'plan')
        review = {'artifacts': ['models.json'], 'answer': 'Saved', 'remaining': [], 'reviewer': 'main', 'review_mode': 'self'}
        self.state.review_task('child', 'returned', review, 'returned')
        self.state.review_task('child', 'adopted', review, 'readback')
        contract = {'purpose': 'Overall delivery', 'checks': [{'id': 'child', 'description': 'Adopted current result', 'kind': 'task', 'task': 'child'}]}
        self.assertTrue(check_contract(self.root, contract, self.state)['passed'])
        self.state.bind('child-chat', 'work', 'main', 'bind')
        note = self.state.input('child-chat', 'new-question', 'Please explain the result')
        self.assertFalse(check_contract(self.root, contract, self.state)['passed'])
        self.state.ack(note['id'], 'work', 1, 'Question answered, original scope retained')
        self.assertTrue(check_contract(self.root, contract, self.state)['passed'])
        self.write('models.json', [{'id': 'model', 'version': 'v1', 'json_ordered': {'changed': True}}])
        self.assertFalse(check_contract(self.root, contract, self.state)['passed'])


if __name__ == '__main__':
    unittest.main()
