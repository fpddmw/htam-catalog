import json
from pathlib import Path
import tempfile
import unittest

from harness.state import State
from harness.common import HarnessError
from harness.context import hook_context, render_context
from harness.host import handle_hook
from harness import goals


class GoalContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', self.root)
        self.state.update('w', 0, {'title': 'Material reuse', 'request': 'Assess options and deliver a usable model',
            'request_source': 'test user', 'objective': 'A usable, bounded research result', 'status': 'active',
            'constraints': ['Preserve conditional assumptions'], 'requirements': [
                {'id':'science','description':'A reproducible conditional model','source':'test user'},
                {'id':'save','description':'A recoverable native artifact','source':'test user'}]}, 'test fixture')
        self.state.bind('main','w','main','test fixture')
        self.state.bind('worker','w','executor','test fixture')
        (self.root/'result.txt').write_text('actual synthetic data')

    def tearDown(self):
        self.state.close(); self.temp.cleanup()

    def work(self):
        return self.state.get('w')

    def contract(self, path='result.txt'):
        return {'purpose':'A scoped result','checks':[{'id':'content','kind':'files',
            'description':'Requested research artifact', 'basis':'Exact synthetic result', 'inputs':[{'path':path}]}]}

    def task(self, identifier, rid='science', **extra):
        return self.state.create_task(identifier,'w',self.work()['revision'], {'purpose':'Answer the requested material question',
            'expected':'Usable evidence with assumptions', 'contributes_to':[rid], **extra}, 'test task')

    def assess(self, rid, **extra):
        return {'requirement':rid,'state':'open','remaining':['Complete the requested result'],
            'limitations':['Research assumptions, no supplier-specific claim'], 'supports':[], 'reason':'Task still necessary', **extra}

    def decide(self, ids, assessments=None):
        return self.state.decide('w',self.work()['revision'], {'action':'continue',
            'reason':'Address remaining requirements using existing evidence','expected_gain':'Usable whole result',
            'tasks':ids,'assessments':assessments or [self.assess('science'),self.assess('save')]}, 'overall review')

    def message(self, **extra):
        return {'threadId':'worker','prompt':'Finish the bounded material model',**extra}

    def reserve(self, task, key='send', **extra):
        return self.state.reserve(key,task,'worker','test dispatch',self.message(),**extra)

    def hook(self, message=None):
        return handle_hook(self.state,{'hook_event_name':'PreToolUse','session_id':'main',
            'tool_name':'mcp__codex_app__send_message_to_thread','tool_input':message or self.message()}, self.root)

    def adopt(self, identifier):
        review={'artifacts':['result.txt'],'answer':'Checked this component','remaining':['Other requirements remain'],
            'reviewer':'main','review_mode':'independent'}
        self.state.review_task(identifier,'returned',review,'returned')
        return self.state.review_task(identifier,'adopted',review,'reviewed')

    def test_missing_parent_or_dependency_rejected(self):
        for link in ({'parent_task':'missing'},{'depends_on':['missing']},{'requires':['missing']}):
            with self.subTest(link=link),self.assertRaises(HarnessError): self.task('bad',**link)

    def test_aggregate_parent_does_not_block_child_execution(self):
        self.task('stage',kind='aggregate')
        self.task('child',parent_task='stage');self.decide(['child'])
        self.assertEqual(self.reserve('child')['status'],'prepared')
        self.assertEqual(self.hook(),{})
        self.assertEqual(self.state.task('stage')['status'],'proposed')

    def test_changed_real_dependency_blocks_even_with_valid_decision(self):
        self.task('basis',completion=self.contract());self.adopt('basis')
        self.task('dependent',depends_on=['basis']);self.decide(['dependent'])
        (self.root/'result.txt').write_text('changed')
        with self.assertRaisesRegex(HarnessError,'evidence changed'):self.reserve('dependent')

    def test_dependency_change_blocks_adoption_and_transitive_use(self):
        self.task('basis',completion=self.contract());self.adopt('basis')
        (self.root/'downstream.txt').write_text('unchanged downstream')
        self.task('middle',requires=['basis'],completion=self.contract('downstream.txt'));self.adopt('middle')
        self.task('last',requires=['middle'],completion=self.contract('downstream.txt'));self.decide(['last'])
        (self.root/'result.txt').write_text('revised basis')
        with self.assertRaisesRegex(HarnessError,'evidence changed'):self.reserve('last')
        self.state.review_task('last','returned',{'answer':'candidate'},'return')
        with self.assertRaisesRegex(HarnessError,'evidence changed'):self.adopt('last')

    def test_empty_repair_is_rejected(self):
        self.task('repair')
        with self.assertRaisesRegex(HarnessError,'Repair review missing'):
            self.state.review_task('repair','revise',{},'vague repair')

    def test_valid_material_error_can_reopen_adopted_result(self):
        self.task('model',completion=self.contract());self.adopt('model')
        review={'artifacts':['result.txt'],'answer':'The unit changes the conclusion','reviewer':'main','review_mode':'independent',
            'findings':[{'requirement':'science','issue':'Wrong functional-unit scale','impact':'Changes all results',
                         'finish_when':'Correct scale and affected calculations match'}]}
        with self.assertRaisesRegex(HarnessError,'Reopening'):self.state.review_task('model','revise',review,'error')
        review['reopen_evidence']='Original source table identifies the unit'
        self.assertEqual(self.state.review_task('model','revise',review,'source comparison')['status'],'revise')

    def test_same_requirement_without_dependency_keeps_selected_task_available(self):
        self.task('result',completion=self.contract());self.task('next');self.decide(['next'])
        self.adopt('result')
        # M87: the overall evaluation needs reconciliation, while this exact
        # independent execution selection retains its unchanged dependencies.
        self.assertEqual(self.reserve('next')['status'],'prepared')
        self.assertEqual(goals.overview(self.state,self.work())['requirements'][0]['state'],'needs_reconciliation')
        self.decide(['next'],[self.assess('science',state='partial',supports=['result'])])
        self.state.check_decision(self.state.task('next'))
        self.assertFalse(goals.completion(self.state,self.work())['passed'])
        self.assertEqual(goals.overview(self.state,self.work())['requirements'][0]['adopted_contributions'],['result'])

    def test_unrelated_result_does_not_stop_selected_independent_work(self):
        self.task('result',completion=self.contract());self.task('native',rid='save');self.decide(['native'])
        self.adopt('result')
        self.assertEqual(self.reserve('native')['status'],'prepared')

    def test_initial_decision_must_account_for_whole_work(self):
        self.task('part')
        with self.assertRaisesRegex(HarnessError,'every overall requirement'):
            self.decide(['part'],[self.assess('science')])

    def test_unexplained_completion_change_rejected(self):
        self.state.update('w',self.work()['revision'],{'completion':self.contract('missing.txt')},'original')
        with self.assertRaisesRegex(HarnessError,'change record'):
            self.state.update('w',self.work()['revision'],{'completion':self.contract()},'shortcut')

    def test_criteria_cannot_be_relabelled_as_evidence_refresh(self):
        self.task('part',completion=self.contract())
        changed=self.contract();changed['checks'][0]['kind']='json';changed['checks'][0]['report']={'path':'report.json'}
        changed['checks'][0]['assertions']=[{'pointer':'/ok','equals':True}]
        with self.assertRaisesRegex(HarnessError,'predicates'):
            self.state.set_task_completion('part',self.work()['revision'],changed,'refresh',
                {'kind':'evidence','reason':'New observation','impact':'Same inputs','source':'fixture'})

    def test_requirement_change_invalidates_only_affected_task(self):
        self.task('science-work');self.task('native',rid='save')
        reqs=self.work()['requirements'];reqs[0]['description']='Corrected unit for the same intended model'
        self.state.update('w',self.work()['revision'],{'requirements':reqs,'change':{'kind':'correction',
            'reason':'Source unit error','impact':'Affected results need rescaling','source':'original table'}},'source correction')
        with self.assertRaisesRegex(HarnessError,'requirement changed'):self.decide(['science-work'])
        self.decide(['native']);self.assertEqual(self.reserve('native')['status'],'prepared')

    def test_actual_prompt_and_model_must_match_preparation(self):
        self.task('work');self.decide(['work']);self.reserve('work')
        for msg in (self.message(prompt='Do unrelated work'),self.message(model='other',thinking='low')):
            with self.subTest(msg=msg):
                self.assertEqual(self.hook(msg)['hookSpecificOutput']['permissionDecision'],'deny')
                self.assertEqual(self.state.dispatch('send')['status'],'prepared')
        self.assertEqual(self.hook(),{})

    def test_required_execution_model_is_checked_before_reservation(self):
        self.state.update('w',self.work()['revision'],{'execution_policy':{'model':'gpt-6.1-sol','thinking':'xhigh','target':'worker'}},'user request')
        self.task('work');self.decide(['work'])
        with self.assertRaisesRegex(HarnessError,'required model'):self.reserve('work')

    def test_overlapping_new_work_blocked_and_explicit_handoff_preserves_history(self):
        self.task('first');self.decide(['first']);self.reserve('first','first-send')
        self.state.settle('first-send','sent',{'observed':'tool returned'},'receipt')
        self.state.settle('first-send','started',{'observed':'recipient active'},'snapshot')
        self.task('second');self.decide(['second'])
        with self.assertRaisesRegex(HarnessError,'active/unknown'):self.reserve('second','second-send')
        self.reserve('second','second-send',intent='replace',relates_to='first-send');self.assertEqual(self.hook(),{})
        self.state.settle('second-send','sent',{'observed':'tool returned'},'receipt')
        self.assertEqual(self.state.dispatch('first-send')['status'],'superseded')
        self.assertEqual(self.state.task('first')['status'],'superseded')
        self.assertNotEqual(self.state.task('first')['status'],'adopted')

    def test_unknown_send_cannot_be_replaced(self):
        self.task('first');self.decide(['first']);self.reserve('first','first-send')
        self.state.settle('first-send','unknown',{'error':'timeout'},'receipt')
        self.task('second');self.decide(['second'])
        with self.assertRaisesRegex(HarnessError,'unknown/prepared'):
            self.reserve('second','second-send',intent='amend',relates_to='first-send')

    def test_unknown_is_not_erased_by_abandoning_the_task(self):
        self.task('first');self.decide(['first']);self.reserve('first','first-send')
        self.state.settle('first-send','unknown',{'error':'timeout'},'receipt')
        self.state.review_task('first','abandoned',{'reason':'keep result for later'},'task bookkeeping')
        self.task('second');self.decide(['second'])
        with self.assertRaisesRegex(HarnessError,'active/unknown'):self.reserve('second','second-send')
        self.state.settle('first-send','cancelled',{'observed':'Actual host confirmed no send'},'verified recovery')
        self.assertEqual(self.reserve('second','second-send')['status'],'prepared')

    def test_changed_assessment_support_blocks_continuation_without_task_dependency(self):
        self.task('component',completion=self.contract());self.adopt('component');self.task('next')
        self.decide(['next'],[self.assess('science',state='partial',supports=['component']),self.assess('save')])
        (self.root/'result.txt').write_text('changed after review')
        with self.assertRaisesRegex(HarnessError,'support evidence changed'):self.reserve('next')
        self.assertEqual(goals.overview(self.state,self.work())['requirements'][0]['state'],'needs_reconciliation')

    def test_relinking_adopted_task_to_changed_requirement_needs_fresh_review(self):
        self.task('component',completion=self.contract());self.adopt('component')
        task=self.state.relink_task('component',self.work()['revision'],{'contributes_to':['save']},'changed use')
        self.assertEqual(task['status'],'returned')
        self.assertEqual(task['body']['reviews'][-1]['disposition'],'adopted')

    def test_execution_policy_change_keeps_user_approval_boundary(self):
        original={'model':'gpt-6.1-sol','thinking':'xhigh','target':'worker'}
        self.state.update('w',self.work()['revision'],{'execution_policy':original},'user setting')
        change={'kind':'correction','reason':'Different preference','impact':'Changes executor','source':'guess'}
        with self.assertRaisesRegex(HarnessError,'user approval'):
            self.state.update('w',self.work()['revision'],{'execution_policy':{**original,'thinking':'low'},'change':change},'guess')
        self.assertEqual(self.work()['execution_policy'],original)

    def test_migration_transaction_rolls_back_all_nested_steps(self):
        revision=self.work()['revision']
        events=self.state.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with self.assertRaisesRegex(RuntimeError,'abort rehearsal'):
            with self.state.transaction():
                self.state.update('w',revision,{'next_action':'temporary migration'},'test')
                self.task('temporary')
                raise RuntimeError('abort rehearsal')
        self.assertEqual(self.work()['revision'],revision)
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],events)
        with self.assertRaises(HarnessError):self.state.task('temporary')

    def test_conditional_research_delivery_can_finish_without_optional_enhancement(self):
        reqs=self.work()['requirements'];reqs[0]['completion']=self.contract();reqs[1]['required']=False
        self.state.update('w',self.work()['revision'],{'requirements':reqs,'change':{'kind':'scope',
            'reason':'User requests internal research only','impact':'Native publication deferred', 'source':'test user',
            'approval_source':'test user approval'}},'explicit scoped research request')
        self.decide([], [self.assess('science',state='satisfied',remaining=[]),self.assess('save',state='blocked')])
        result=self.state.update('w',self.work()['revision'],{'status':'completed','outcome':'Conditional result delivered'},'delivery')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['assessments']['science']['limitations'],['Research assumptions, no supplier-specific claim'])

    def test_exploration_without_numeric_novelty_can_continue(self):
        self.task('explore',purpose='Compare possible receiving functions before selecting a case')
        self.decide(['explore'])
        self.assertEqual(self.reserve('explore')['status'],'prepared')

    def test_context_bounded_with_large_history_and_contains_actual_decision(self):
        self.state.update('w',self.work()['revision'],{'accepted':['historic fact '*1000]*100,'references':['long-path'*200]*100},'history')
        self.task('explore');self.decide(['explore'])
        text=hook_context(self.state,'main');self.assertLessEqual(len(text.encode('utf-8')),6500)
        data=json.loads(text.split('\n',1)[1])
        self.assertEqual(data['objective'],self.work()['objective'])
        self.assertEqual(len(data['overall']['requirements']),2)
        self.assertIn('remaining',data['overall']['requirements'][0])
        self.assertIn('constraints',data);self.assertIn('reminder',data)

    def test_context_preserves_text_and_structured_review_findings(self):
        self.task('reviewed')
        # Text form occurred in the actual Apple input899 copper main review.
        findings = ['20项scaled_unit均需修为总量单位，另保留per_kg单位。量纲标签关系影响后续引用。',
                    {'issue': 'A supported structured finding', 'impact': 'Affects selected use',
                     'finish_when': 'Original source basis verified', 'source': 'kept in original review'}]
        self.state.review_task('reviewed', 'returned', {'answer': 'Scoped main review',
            'artifacts': ['result.txt'], 'remaining': [], 'findings': findings}, 'review record')
        before = self.state.task('reviewed')['body']['reviews']
        text = hook_context(self.state, 'main')
        self.assertLessEqual(len(text.encode('utf-8')), 6500)
        data = json.loads(text.split('\n', 1)[1])
        rendered = next(t for t in data['tasks'] if t['id'] == 'reviewed')['findings']
        self.assertTrue(rendered[0]['issue'].startswith('20项scaled_unit'))
        self.assertEqual(rendered[0]['impact'], '')
        self.assertEqual(rendered[0]['finish_when'], '')
        self.assertEqual(rendered[1], {k: findings[1][k] for k in ('issue', 'impact', 'finish_when')})
        self.assertEqual(self.state.task('reviewed')['body']['reviews'], before)

    def test_sync_proposal_requires_recorded_approval_and_unchanged_scope(self):
        item={'work':'w','proposal':'Use declared dependencies at dispatch','locations':['harness/state.py'],
            'reason':'Observed recurrence','impact':'Prevents stale dependent work','status':'proposed'}
        self.state.observation('sync-test','sync',item,'user-visible proposal')
        with self.assertRaisesRegex(HarnessError,'approval'):
            self.state.observation('sync-test','sync',{**item,'status':'approved'},'assumed approval')
        approved={**item,'status':'approved','approval_source':'test explicit user approval'}
        self.state.observation('sync-test','sync',approved,'user reply')
        with self.assertRaisesRegex(HarnessError,'scope'):
            self.state.observation('sync-test','sync',{**approved,'status':'implemented','proposal':'Also change another project','evidence':['patch']},'expansion')
        data=render_context(self.state,work='w')
        self.assertEqual(data['sync_items'][0]['status'],'approved')

    def test_sync_implementation_preserves_approval_and_reason(self):
        item={'work':'w','proposal':'Scoped change','locations':['harness/state.py'],
            'reason':'Observed defect','impact':'Bounded fix','status':'proposed'}
        self.state.observation('sync','sync',item,'proposal')
        approved={**item,'status':'approved','approval_source':'explicit user turn'}
        self.state.observation('sync','sync',approved,'approval')
        for change in ({'impact':'Also changes another project'},{'approval_source':'assumed elsewhere'}):
            with self.subTest(change=change),self.assertRaises(HarnessError):
                self.state.observation('sync','sync',{**approved,'status':'implemented','evidence':['patch'],**change},'invalid')
        self.assertEqual(len(self.state.observations('sync')),2)

    def test_context_remains_valid_and_bounded_with_oversized_identities(self):
        huge='超长标识'*3000
        self.state.update(huge,0,{'title':huge,'request':'Synthetic stress case','request_source':'test',
            'objective':huge,'status':'active'},'stress test')
        unbound=hook_context(self.state,'unknown');self.assertLessEqual(len(unbound.encode('utf-8')),6500)
        self.assertGreater(json.loads(unbound.split('\n',1)[1])['omitted_works'],0)
        self.state.bind('huge',huge,'main','test')
        bound=hook_context(self.state,'huge');self.assertLessEqual(len(bound.encode('utf-8')),6500)
        self.assertIn('work_identity_sha256',json.loads(bound.split('\n',1)[1]))


if __name__ == '__main__':
    unittest.main()
