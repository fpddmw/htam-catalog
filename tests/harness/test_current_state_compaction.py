import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from harness.common import HarnessError, encoded
from harness.context import hook_context, improvement_items, knowledge_context, render_context
from harness import goals
from harness.state import State


class CurrentStateCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', self.root)
        self.state.update('w', 0, {'title': 'Conditional material model', 'request': 'Assess two uses',
            'request_source': 'fixture human request', 'objective': 'A usable model', 'status': 'active',
            'constraints': ['5 kg basis; proxy background; internal use only'], 'requirements': [
                {'id': 'model', 'description': 'Model the 5 kg functional unit', 'source': 'fixture request'},
                {'id': 'save', 'description': 'Save a recoverable artifact', 'source': 'fixture request'}]}, 'fixture')
        self.state.bind('main', 'w', 'main', 'fixture')
        self.state.create_task('t', 'w', self.work()['revision'], {'purpose': 'Model the requested basis',
            'expected': 'Conditional 5 kg result', 'contributes_to': ['model'],
            'scientific_basis': {'quantity': 5, 'unit': 'kg', 'use': 'internal only'}}, 'fixture')

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def work(self):
        return self.state.get('w')

    def assessment(self, rid, reason='Current evidence supports the next step'):
        return {'requirement': rid, 'state': 'partial', 'remaining': ['Complete the requested use'],
            'limitations': ['Proxy background', 'Proxy background', '5 kg internal use only'],
            'supports': [], 'reason': reason}

    def decide(self, assessments=None):
        return self.state.decide('w', self.work()['revision'], {'action': 'continue',
            'reason': 'Resolve current model uncertainty', 'expected_gain': 'A usable conditional model',
            'tasks': ['t'], 'assessments': assessments or [self.assessment('model'), self.assessment('save')]}, 'fixture review')

    def legacy_decision(self, reason):
        decision = self.decide([self.assessment('model', reason), self.assessment('save')])
        work = self.work()
        body = {k: v for k, v in work.items() if k not in ('id', 'revision', 'updated_at')}
        body['decision'] = deepcopy(decision)
        with self.state.transaction():
            self.state.db.execute('UPDATE works SET body=? WHERE id=?', (encoded(body), 'w'))
        return self.work()

    def test_decision_stores_one_current_copy_and_original_event(self):
        supplied = [self.assessment('model', 'Earlier reviewed reason'), self.assessment('save')]
        result = self.decide(supplied)
        current = self.work()
        self.assertEqual(result['assessments'], supplied)  # existing write-response API
        self.assertNotIn('assessments', current['decision'])
        self.assertEqual(current['decision']['assessment_refs'], goals.assessment_references(current))
        event = self.state.history('w')[-1]
        self.assertEqual(event['seq'], current['decision']['history_event'])
        self.assertEqual(event['payload'], result)
        save = deepcopy(current['assessments']['save'])
        self.decide([self.assessment('model', 'Only the new current reason')])
        self.assertEqual(self.work()['assessments']['model']['reason'], 'Only the new current reason')
        self.assertEqual(self.work()['assessments']['save'], save)
        self.assertEqual(event['payload']['assessments'][0]['reason'], 'Earlier reviewed reason')
        self.state.check_decision(self.state.task('t'))

    def test_legacy_default_view_is_compact_and_full_remains_exact(self):
        work = self.legacy_decision('Current quantity basis\n此前记录：historic detail')
        compact = goals.decision_view(self.state, work)
        self.assertNotIn('assessments', compact)
        self.assertEqual(compact['recorded_assessments']['count'], 2)
        self.assertEqual(goals.decision_view(self.state, work, full=True)['assessments'], work['decision']['assessments'])
        full = render_context(self.state, work='w', full=True)
        self.assertEqual(full['current_decision']['assessments'], work['decision']['assessments'])
        self.assertEqual(self.work()['decision'], work['decision'])

    def test_resupplying_current_assessments_keeps_existing_normalization_compatible(self):
        self.decide()
        supplied = list(self.work()['assessments'].values())
        result = self.state.decide('w', self.work()['revision'], {'action': 'continue',
            'reason': 'Use the same research judgment', 'expected_gain': 'Finish the bounded model',
            'tasks': ['t'], 'assessments': supplied}, 'second reviewer source')
        self.assertEqual(result['assessments'], supplied)
        self.assertEqual(self.work()['assessments']['model']['source'], 'second reviewer source')
        self.assertNotIn('assessments', self.work()['decision'])

    def test_preview_and_apply_preserve_scope_and_archive_exact_history(self):
        reason = 'Current 5 kg proxy judgment\n此前记录：prior case details'
        before = self.legacy_decision(reason)
        moves = [{'path': ['assessments', 'model', 'reason'], 'before': reason, 'segments': [
            {'current': 'Current 5 kg proxy judgment'}, {'historical': '\n此前记录：prior case details'}]}]
        args = {'text_moves': moves, 'deduplicate_paths': [['assessments', 'model', 'limitations']],
                'actor': 'actual-maintainer'}
        events = len(self.state.history('w'))
        preview = self.state.compact_current_state('w', before['revision'], 'approved input898 repair', **args)
        self.assertFalse(preview['applied'])
        self.assertLess(preview['after_bytes'], preview['before_bytes'])
        self.assertEqual(self.work(), before)
        self.assertEqual(len(self.state.history('w')), events)
        applied = self.state.compact_current_state('w', before['revision'], 'approved input898 repair', apply=True, **args)
        self.assertTrue(applied['applied'])
        after = self.work()
        for field in ('request_revision', 'requirements', 'constraints', 'request', 'objective', 'next_action'):
            self.assertEqual(after.get(field), before.get(field))
        for field in ('task_stamps', 'coordination', 'revision', 'validity', 'action', 'tasks'):
            self.assertEqual(after['decision'].get(field), before['decision'].get(field))
        self.assertEqual(after['assessments']['model']['reason'], 'Current 5 kg proxy judgment')
        self.assertEqual(after['assessments']['model']['limitations'], ['Proxy background', '5 kg internal use only'])
        self.assertEqual(after['decision']['assessment_refs'], goals.assessment_references(after))
        self.state.check_decision(self.state.task('t'))
        archive = self.state.history('w')[-1]
        self.assertEqual(archive['seq'], after['current_state_history_event'])
        self.assertEqual(archive['payload']['actor'], 'actual-maintainer')
        self.assertEqual(archive['payload']['before_decision'], before['decision'])
        self.assertEqual(archive['payload']['changes'][0], moves[0])
        compact = render_context(self.state, task='t')
        self.assertEqual(compact['work']['current_state_history']['event'], archive['seq'])
        full = render_context(self.state, task='t', full=True)
        self.assertEqual(full['work']['current_state_history']['record']['payload']['changes'][0], moves[0])
        full_work = render_context(self.state, work='w', full=True)
        self.assertEqual(full_work['current_state_history']['record']['payload']['before_decision'], before['decision'])
        compact_work = render_context(self.state, work='w')
        self.assertEqual(compact_work['work']['current_state_history']['event'], archive['seq'])
        self.assertFalse(self.state.compact_current_state('w', after['revision'], 'no change', apply=True)['applied'])

    def test_maintenance_rejects_stale_inexact_and_semantic_paths(self):
        self.decide()
        work = self.work()
        with self.assertRaisesRegex(HarnessError, 'Stale work revision'):
            self.state.compact_current_state('w', work['revision'] - 1, 'approved repair', apply=True)
        before = work['assessments']['model']['reason']
        cases = [(['assessments', 'model', 'reason'], [{'current': 'New interpretation'}, {'historical': before}]),
                 (['constraints'], [{'current': before}, {'historical': 'extra'}])]
        for path, segments in cases:
            with self.subTest(path=path), self.assertRaises(HarnessError):
                self.state.compact_current_state('w', work['revision'], 'approved repair', apply=True,
                    text_moves=[{'path': path, 'before': before, 'segments': segments}])
        self.assertEqual(self.work(), work)

    def test_differing_legacy_assessment_requires_explicit_reconciliation(self):
        work = self.legacy_decision('Original current reason')
        body = {k: v for k, v in work.items() if k not in ('id', 'revision', 'updated_at')}
        body['decision']['assessments'][0]['state'] = 'blocked'
        with self.state.transaction():
            self.state.db.execute('UPDATE works SET body=? WHERE id=?', (encoded(body), 'w'))
        with self.assertRaisesRegex(HarnessError, 'differs from the maintained assessment'):
            self.state.compact_current_state('w', work['revision'], 'approved repair', apply=True)
        self.assertEqual(goals.decision_view(self.state, self.work())['action'], 'continue')

    def test_task_recovery_selects_latest_review_and_preserves_exact_current_scope(self):
        self.decide()
        for answer in ('First result', 'Latest result with 5 kg conditional basis'):
            self.state.review_task('t', 'returned', {'answer': answer}, 'fixture return')
        original = self.state.task('t')
        compact = render_context(self.state, task='t')
        full = render_context(self.state, task='t', full=True)
        self.assertEqual(compact['task']['body']['reviews'], original['body']['reviews'][-1:])
        self.assertEqual(compact['task']['review_history']['omitted'], 1)
        self.assertEqual(full['task'], original)
        self.assertEqual(compact['task']['body']['scientific_basis'], original['body']['scientific_basis'])
        self.assertEqual(compact['work']['constraints'], self.work()['constraints'])
        self.assertEqual(compact['work']['requirements'], [self.work()['requirements'][0]])
        self.assertEqual(compact['work']['assessments']['model'], self.work()['assessments']['model'])

    def test_recovery_selects_unresolved_before_limit_and_retains_explicit_history(self):
        self.decide()
        (self.root / 'triage.txt').write_text('Evidence for this exact recorded disposition')
        waiting = self.state.event('w', 'improvement_candidate', 'fixture observation', {'asset': 'older-unresolved', 'kind': 'route_failure'})
        for disposition in ('addressed', 'deferred', 'no_change', 'addressed'):
            event = self.state.event('w', 'improvement_candidate', 'fixture observation', {'asset': disposition, 'kind': 'route_failure'})
            self.state.triage('w', event, {'disposition': disposition, 'reason': 'Reviewed this exact observation',
                'next_action': 'Use its recorded disposition', 'evidence': ['triage.txt']}, 'fixture triage')
        recurring = self.state.event('w', 'improvement_candidate', 'new recurrence', {'asset': 'addressed', 'kind': 'route_failure'})
        items = improvement_items(self.state, 'w')
        self.assertEqual([item['event'] for item in items], [recurring, waiting])
        data = json.loads(hook_context(self.state, 'main').split('\n', 1)[1])
        self.assertEqual([item['event'] for item in data['improvement_candidates']], [recurring, waiting])
        full = render_context(self.state, work='w', full=True)
        self.assertEqual(len(full['improvement_candidates']), 6)
        self.assertEqual(self.state.history('w')[-1]['seq'], recurring)

    def test_knowledge_budget_compacts_wrappers_before_selecting_entry(self):
        metadata = {'work_revision': self.work()['revision'], 'source': 'Original approved use and provenance',
                    'meaning': 'Existing recorded work constraint; no new authorization inferred ' * 4}
        constraints = [{'kind': 'work_constraint', 'id': 'w:constraint:' + str(i),
            'constraint': 'Exact quantity, proxy and permitted-use condition ' + str(i), **metadata} for i in range(5)]
        negative = {'kind': 'counterexample', 'id': 'counterexample-1',
                    'constraint': 'Supplier-specific public use remains outside the evidence scope'}
        class DiscoveryFixture:
            def retrieve(inner, query, context, *, limit, budget):
                inner.budget = budget
                return {'mandatory': deepcopy(constraints + [negative]), 'selected': [{'kind': 'original_entry',
                    'id': 'method', 'path': 'wiki/method.md', 'entry': 'Use its original method', 'status': 'active',
                    'knowledge_status': 'capability', 'applicability': {'state': 'declared_match'},
                    'current_file': True, 'sha': 'a' * 64, 'default_recommendation': False, 'display_history': 'Repeated catalog metadata ' * 100}],
                    'context_complete': True, 'required_load': None, 'omitted': {'mandatory': 0, 'optional': 0},
                    'coverage': {'history': 'Display-only inventory ' * 100}}
        discovery = DiscoveryFixture()
        changes = self.state.db.total_changes
        result = knowledge_context(self.state, discovery, work='w', query='method', limit=2, budget=1800)
        self.assertGreaterEqual(discovery.budget, 16000)
        self.assertLessEqual(len(encoded(result).encode()), 1800)
        self.assertTrue(result['context_complete'])
        self.assertEqual([item['constraint'] for item in result['mandatory'][:5]], [item['constraint'] for item in constraints])
        self.assertEqual(result['mandatory'][-1], negative)
        self.assertEqual(result['work_constraint_provenance'], metadata)
        self.assertEqual(result['selected'][0]['path'], 'wiki/method.md')
        self.assertEqual(result['selected'][0]['sha'], 'a' * 64)
        self.assertFalse(result['selected'][0]['default_recommendation'])
        self.assertEqual(self.state.db.total_changes, changes)

    def test_changed_triage_evidence_returns_deferred_item_without_changing_disposition(self):
        evidence = self.root / 'triage.txt'
        evidence.write_text('Original bounded observation')
        event = self.state.event('w', 'improvement_candidate', 'observation', {'asset': 'method', 'kind': 'route_failure'})
        self.state.triage('w', event, {'disposition': 'deferred', 'reason': 'Adequate for current use',
            'next_action': 'Reassess after evidence changes', 'evidence': ['triage.txt']}, 'reviewed response')
        self.assertEqual(improvement_items(self.state, 'w'), [])
        evidence.write_text('A changed result with a concrete contrary observation')
        changes = self.state.db.total_changes
        item = improvement_items(self.state, 'w')[0]
        self.assertEqual(item['status'], 'deferred')
        self.assertEqual(item['attention'], 'triage_evidence_changed')
        self.assertEqual(item['evidence_issues'], ['missing_or_changed:triage.txt'])
        self.assertEqual(self.state.db.total_changes, changes)


    def test_unselected_run_basis_retires_while_explicit_stop_is_kept(self):
        for tid in ['u','v']:
            self.state.create_task(tid,'w',self.work()['revision'],{'purpose':'fixture','expected':'fixture','contributes_to':['model']},'fixture')
        self.state.decide('w',self.work()['revision'],{'action':'continue','reason':'Earlier routes','expected_gain':'fixture','tasks':['t','u'],
            'assessments':[self.assessment('model'),self.assessment('save')],
            'coordination':{'choices':[{'task':'t','action':'run','basis':'current'}, {'task':'u','action':'run','basis':'old uncompleted delivery'}, {'task':'v','action':'stop','basis':'explicit stop'}]}},'fixture')
        self.decide()
        choices=self.work()['decision']['coordination']['choices']
        self.assertEqual({x['task'] for x in choices},{'t','v'})
        self.assertEqual(next(x for x in choices if x['task']=='v')['action'],'stop')
        history=self.state.history('w')
        self.assertTrue(any('old uncompleted delivery' in json.dumps(x) for x in history))

if __name__ == '__main__':
    unittest.main()
