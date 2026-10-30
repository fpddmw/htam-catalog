import json
import io
import sqlite3
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from harness.host import handle_hook
from harness.context import hook_context, render_context, sync_items
from harness.discovery import Discovery
from harness.common import digest, validation_scope, ValidationTimeout
from harness.state import State
from harness import goals
from harness.common import HarnessError, encoded

class SystemicContractsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.st=State(self.root/'store',self.root)
        (self.root/'e.txt').write_text('actual fixture evidence')
        self.st.update('w',0,{'title':'Fixture','request':'Original requested use','request_source':'original user/plan04','objective':'Deliver declared use','status':'active',
            'requirements':[{'id':'overall','description':'Deliver declared use','source':'original user/plan04','completion':self.contract()}]},'fixture')
        self.st.bind('main','w','main','fixture');self.st.bind('worker','w','executor','fixture')
    def tearDown(self):self.st.close();self.tmp.cleanup()
    def contract(self,path='e.txt'):
        return {'purpose':'Exact fixture use','checks':[{'id':'file','kind':'files','description':'Artifact exists','basis':'Fixture result','inputs':[{'path':path}]}]}
    def task(self,i,**kw):return self.st.create_task(i,'w',self.st.get('w')['revision'],{'purpose':'Do bounded use','expected':'Real result','completion':self.contract(),**kw},'fixture')
    def decide(self,ids=(),state='open',claims=None,action='continue'):
        a={'requirement':'overall','state':state,'supports':[],'remaining':[] if state=='satisfied' else ['remaining use'],'limitations':[],'reason':'Fixture assessment'}
        if claims is not None:a['claims']=claims
        return self.st.decide('w',self.st.get('w')['revision'],{'action':action,'reason':'Use current facts','expected_gain':'Requested result','tasks':list(ids),'assessments':[a]},'fixture')
    def reserve(self,i,key):return self.st.reserve(key,i,'worker','fixture',{'threadId':'worker','prompt':'Do the exact bounded task'})
    def judge(self,expected,claim):return self.st.judge('j','w',expected,'adopted',{'claim':claim,'scope':'only task a','evidence':[{'path':'e.txt'}]},'fixture')
    def add_claims(self):
        (self.root/'saved.json').write_text('{"saved":true}')
        def jc(path,pointer,value):return {'purpose':'Exact claim','checks':[{'id':'report','kind':'json','description':'Claim result','basis':'Fixture factual receipt','inputs':[{'path':path}],'report':{'path':path},'assertions':[{'pointer':pointer,'equals':value}]}]}
        req={'id':'overall','description':'Deliver declared use','source':'original user/plan04','completion':self.contract(),'claims':[
            {'id':'save','description':'Account draft saved','source':'original plan04 save clause','level':'technical','completion':jc('saved.json','/saved',True)},
            {'id':'contribute','description':'Applicable contribution disposition','source':'original plan04 contribution clause','level':'public_submission','applicability':'conditional','completion':jc('submitted.json','/submitted',True)}]}
        self.st.update('w',self.st.get('w')['revision'],{'requirements':[req],'change':{'kind':'correction','reason':'Restore separate original uses','impact':'Original contribution obligation stays visible','source':'original plan04'}},'fixture')
        return jc
    def test_new_judgment_binding_is_exact_and_only_affected_execution_stops(self):
        self.judge(0,'route A');a=self.task('a',judgments=['j']);self.task('b');self.decide(['a','b'])
        self.assertEqual(a['body']['judgments'],[{'id':'j','revision':1}]);self.judge(1,'route B')
        with self.assertRaisesRegex(HarnessError,'revision changed'):self.reserve('a','ka')
        self.assertEqual(self.reserve('b','kb')['status'],'prepared')
    def test_legacy_judgment_is_not_guessed_and_explicit_relink_binds_it(self):
        self.judge(0,'route');t=self.task('a');body=t['body'];body['judgments']=['j']
        with self.st.transaction():self.st.db.execute('UPDATE tasks SET body=? WHERE id=?',(encoded(body),'a'))
        self.decide(['a'])
        with self.assertRaisesRegex(HarnessError,'Legacy judgment'):self.reserve('a','ka')
        self.st.relink_task('a',self.st.get('w')['revision'],{'judgments':[{'id':'j','revision':1}]},'explicit current use review');self.decide(['a'])
        self.assertEqual(self.reserve('a','ka')['status'],'prepared')
    def test_saved_receipt_cannot_close_missing_original_contribution_use(self):
        self.add_claims()
        with self.assertRaisesRegex(HarnessError,'use claim'):
            self.decide(state='satisfied',action='deliver',claims=[{'claim':'save','state':'satisfied','reason':'actual saved receipt'},{'claim':'contribute','state':'open','reason':'not yet disposed'}])
        self.assertEqual(self.st.get('w')['request'],'Original requested use')
    def test_evidenced_not_applicable_contribution_allows_conditional_delivery(self):
        jc=self.add_claims();(self.root/'applicability.json').write_text('{"applicable":false,"reason":"fixture scope"}')
        self.decide(state='satisfied',action='deliver',claims=[{'claim':'save','state':'satisfied','reason':'actual saved receipt'},{'claim':'contribute','state':'not_applicable','reason':'exact applicable-scope evidence','applicability_evidence':jc('applicability.json','/applicable',False)}])
        self.assertTrue(goals.completion(self.st,self.st.get('w'))['passed'])
        self.assertEqual(goals.decision_view(self.st,self.st.get('w'))['action'],'deliver')
    def test_correction_label_does_not_authorize_obligation_removal(self):
        self.add_claims();req=self.st.get('w')['requirements'][0];req['claims']=[]
        with self.assertRaisesRegex(HarnessError,'approval provenance'):
            self.st.update('w',self.st.get('w')['revision'],{'requirements':[req],'change':{'kind':'correction','reason':'shorten','impact':'remove','source':'agent interpretation'}},'fixture')
    def test_changed_next_action_cannot_leave_deliver_as_current(self):
        self.decide(state='satisfied',action='deliver');self.st.update('w',self.st.get('w')['revision'],{'next_action':'Fix a newly identified obligation'},'new factual direction')
        d=goals.decision_view(self.st,self.st.get('w'));self.assertEqual(d['action'],'unreconciled');self.assertEqual(d['recorded_action'],'deliver')
    def test_known_producer_cannot_self_label_independent(self):
        self.task('a',executor_session='worker');self.st.review_task('a','returned',{'answer':'done'},'fixture')
        with self.assertRaisesRegex(HarnessError,'Known producer'):
            self.st.review_task('a','adopted',{'artifacts':['e.txt'],'answer':'checked','remaining':[],'reviewer':'worker','review_mode':'independent'},'fixture')
    def test_unknown_producer_independence_is_explicitly_unverified(self):
        self.task('a');self.st.review_task('a','returned',{'answer':'done'},'fixture')
        t=self.st.review_task('a','adopted',{'artifacts':['e.txt'],'answer':'checked','remaining':[],'reviewer':'main','review_mode':'independent'},'fixture')
        self.assertEqual(t['body']['reviews'][-1]['review']['review_identity']['status'],'unverified')
    def test_external_only_blocker_needs_recovery_scope(self):
        with self.assertRaisesRegex(HarnessError,'Blocking judgment'):
            self.st.judge('b','w',0,'adopted',{'claim':'Only external waiting','scope':'one object','evidence':[{'path':'e.txt'}],'only_external_wait':True},'fixture')
    def test_legacy_requirements_without_use_claims_are_not_retroactively_revoked(self):
        self.decide(state='satisfied',action='deliver');self.assertTrue(goals.completion(self.st,self.st.get('w'))['passed']);self.assertEqual(goals.overview(self.st,self.st.get('w'))['requirements'][0]['use_coverage'],'legacy_unspecified')

    def event(self, shape=None, name='PreToolUse', caller='main', target='worker'):
        return {'hook_event_name':name,'session_id':caller,'tool_name':'mcp__codex_app__send_message_to_thread',
            'tool_use_id':'actual-fixture-call','tool_input': shape if shape is not None else {'threadId':target,'prompt':'Do the exact bounded task'},'tool_response':{}}
    def test_producer_field_is_known_for_independence(self):
        self.task('a',producer='worker');self.st.review_task('a','returned',{'answer':'done'},'fixture')
        with self.assertRaisesRegex(HarnessError,'Known producer'):
            self.st.review_task('a','adopted',{'artifacts':['e.txt'],'answer':'checked','remaining':[],'reviewer':'worker','review_mode':'independent'},'fixture')
    def test_unconditional_to_conditional_and_removed_predicate_need_approval(self):
        self.add_claims();req=self.st.get('w')['requirements'][0]
        for change_kind in ('applicability','predicate'):
            changed=json.loads(json.dumps(req))
            if change_kind=='applicability':changed['claims'][0]['applicability']='conditional'
            else:changed['claims'][0]['completion']['checks'][0]['assertions']=[]
            with self.subTest(change_kind=change_kind), self.assertRaisesRegex(HarnessError,'approval provenance'):
                self.st.update('w',self.st.get('w')['revision'],{'requirements':[changed],'change':{'kind':'correction','reason':'shorten','impact':'weaker','source':'agent'}},'fixture')
    def test_string_arguments_guard_and_sent_are_distinct(self):
        self.task('a');self.decide(['a']);self.reserve('a','ka')
        e=self.event(json.dumps({'threadId':'worker','prompt':'Do the exact bounded task'}))
        self.assertEqual(handle_hook(self.st,e,self.root),{})
        self.assertEqual(self.st.dispatch('ka')['guard_evaluation']['status'],'passed')
        self.assertEqual(self.st.dispatch('ka')['status'],'claimed')
        e['hook_event_name']='PostToolUse';e['tool_response']='{}';handle_hook(self.st,e,self.root)
        self.assertEqual(self.st.dispatch('ka')['status'],'sent')
    def test_post_receipt_does_not_backfill_missing_guard(self):
        self.task('a');self.decide(['a']);self.reserve('a','ka');handle_hook(self.st,self.event(name='PostToolUse'),self.root)
        d=self.st.dispatch('ka');self.assertEqual(d['status'],'sent');self.assertEqual(d['guard_evaluation']['status'],'unverified')
    def test_malformed_guard_has_explicit_diagnostic_no_prompt_leak(self):
        e=self.event('{malformed SECRET_VALUE')
        self.assertEqual(handle_hook(self.st,e,self.root)['hookSpecificOutput']['permissionDecision'],'deny')
        rows=list(self.st.db.execute("SELECT payload FROM events WHERE kind='guard_evaluated'"))
        self.assertEqual(json.loads(rows[-1][0])['status'],'rejected');self.assertNotIn('SECRET_VALUE',str(rows))
    def test_timeout_rolls_back_claim_and_records_failed(self):
        self.task('a');self.decide(['a']);self.reserve('a','ka')
        with patch('harness.host.remaining_timeout',side_effect=ValidationTimeout('budget expired')):
            self.assertEqual(handle_hook(self.st,self.event(),self.root)['hookSpecificOutput']['permissionDecision'],'deny')
        d=self.st.dispatch('ka');self.assertEqual(d['status'],'prepared');self.assertEqual(d['guard_evaluation']['status'],'failed')
    def test_bound_executor_report_does_not_create_dispatch(self):
        e=self.event(caller='worker',target='main');self.assertEqual(handle_hook(self.st,e,self.root),{})
        e['hook_event_name']='PostToolUse';handle_hook(self.st,e,self.root)
        self.assertEqual(self.st.db.execute('SELECT count(*) FROM dispatches').fetchone()[0],0)
        self.assertEqual(self.st.db.execute("SELECT count(*) FROM events WHERE kind='executor_report_sent'").fetchone()[0],1)
    def test_returned_and_revise_visible_with_owner_time_and_budget(self):
        self.task('a');self.st.review_task('a','returned',{'answer':'done'},'fixture')
        data=json.loads(hook_context(self.st,'main').split('\n',1)[1]);self.assertEqual(data['tasks'][0]['responsible_role'],'main');self.assertTrue(data['tasks'][0]['last_changed_at'])
        self.assertLessEqual(len(hook_context(self.st,'main').encode()),6500)
    def test_invocation_cache_reuses_then_invalidates_changed_content(self):
        f=self.root/'e.txt'
        with validation_scope() as stats:
            a=digest(f);self.assertEqual(a,digest(f));self.assertEqual(stats['hits'],1)
            f.write_text('a changed body');self.assertNotEqual(a,digest(f))
        self.assertNotEqual(a,digest(f))
    def test_readonly_connection_and_default_context(self):
        st=State(self.root/'store',self.root,readonly=True)
        try:
            d=render_context(st,work='w');self.assertEqual(st.db.total_changes,0);self.assertLess(len(json.dumps(d).encode()),24000)
            with self.assertRaises(HarnessError):st.update('w',1,{'next_action':'mutation'},'invalid')
        finally:st.close()
    def test_readonly_cli_snapshot_releases_source_during_concurrent_write(self):
        from harness import cli
        from harness import state as state_module
        ready, release = threading.Event(), threading.Event()
        observed = {}; original_connect = state_module.connect
        def remember(*args, **kwargs):
            observed['source'] = original_connect(*args, **kwargs)
            return observed['source']
        def slow_read(args, root, directory, state):
            with self.assertRaises(sqlite3.ProgrammingError): observed['source'].execute('SELECT 1')
            self.assertEqual(state.db.execute('PRAGMA query_only').fetchone()[0], 1)
            state.db.execute('BEGIN')
            before = state.get('w')['revision']; ready.set()
            self.assertTrue(release.wait(3))
            self.assertEqual(state.get('w')['revision'], before)
            with self.assertRaises(sqlite3.OperationalError): state.db.execute("UPDATE works SET revision=99")
            return {'snapshot_revision': before}
        def run():
            observed['exit'] = cli.main(['--root', str(self.root), '--store', str(self.root/'store'), '--read-only', 'status'])
        self.st.db.execute('PRAGMA busy_timeout=200')
        with patch('harness.state.connect', side_effect=remember), patch('harness.cli.execute', side_effect=slow_read), patch('sys.stdout', new=io.StringIO()):
            reader = threading.Thread(target=run); reader.start()
            try:
                self.assertTrue(ready.wait(3))
                self.st.update('w', self.st.get('w')['revision'], {'next_action': 'Concurrent original work continues'}, 'fixture live writer')
            finally:
                release.set(); reader.join(3)
            self.assertFalse(reader.is_alive()); self.assertEqual(observed.get('exit'), 0)
        class BrokenSource(sqlite3.Connection):
            def backup(self, *args, **kwargs): raise sqlite3.OperationalError('fixture interrupted backup')
        source = sqlite3.connect(self.root/'store/state.sqlite', factory=BrokenSource)
        with patch('harness.state.connect', return_value=source), self.assertRaises(sqlite3.OperationalError):
            State(self.root/'store', self.root, readonly=True, readonly_snapshot=True)
        with self.assertRaises(sqlite3.ProgrammingError): source.execute('SELECT 1')
    def test_discovery_scope_reuses_configuration_and_rechecks_candidate_protection(self):
        from harness.common import local_path
        (self.root/'allowed').mkdir(); (self.root/'allowed/plain.txt').write_text('Visible fixture')
        (self.root/'allowed/private.txt').write_text('Private fixture'); (self.root/'.env').write_text('Fixture secret')
        (self.root/'outside.txt').write_text('Explicit original entry')
        (self.root/'allowed/link.txt').symlink_to(self.root/'outside.txt')
        coverage = {'roots': ['allowed'], 'entrypoints': [{'path': 'outside.txt'},
            {'path': 'allowed/private.txt', 'access': 'restricted'}, {'path': '.env'}]}
        discovery = Discovery(self.root, self.root/'search', readonly=True)
        try:
            with patch('harness.discovery.local_path', wraps=local_path) as resolve_config, discovery._read_scope(coverage):
                self.assertTrue(discovery._permitted('allowed/plain.txt'))
                self.assertTrue(discovery._permitted('allowed/plain.txt'))
                self.assertTrue(discovery._permitted('outside.txt'))
                for path in ('allowed/private.txt', '.env', 'allowed/link.txt', '../outside.txt'):
                    self.assertFalse(discovery._permitted(path))
                (self.root/'allowed/plain.txt').unlink(); (self.root/'allowed/plain.txt').symlink_to(self.root/'outside.txt')
                self.assertFalse(discovery._permitted('allowed/plain.txt'))
                self.assertEqual(resolve_config.call_count, 4)
        finally: discovery.close()
    def test_retrieve_runs_one_contextual_asset_search_and_one_scope_compile(self):
        discovery = Discovery(self.root, self.root/'search', state=self.st, readonly=True)
        try:
            with patch('harness.assets.AssetRegistry.search', autospec=True, return_value={'items': [], 'count': 0}) as search, patch.object(discovery, '_compile_policy', wraps=discovery._compile_policy) as compile_policy:
                discovery.retrieve('Exact fixture method', {'work': 'w'})
                self.assertEqual(search.call_count, 1); self.assertEqual(compile_policy.call_count, 1)
                self.assertEqual(search.call_args.args[2]['work'], 'w')
        finally: discovery.close()
    def test_exact_content_adoption_survives_rename_and_reindex(self):
        (self.root/'wiki').mkdir();old=self.root/'wiki/00-old.md';new=self.root/'wiki/99-current.md'
        old.write_text('USLCI old route');new.write_text('USLCI current route')
        self.st.observation('old','adoption',{'path':'wiki/00-old.md','status':'superseded','replacement':'wiki/99-current.md','scope':'same use'},'review')
        self.st.observation('new','adoption',{'path':'wiki/99-current.md','status':'adopted','scope':'same use','conditions':['current owner']},'review')
        d=Discovery(self.root,self.root/'search',state=self.st)
        try:
            d.scan({'roots':['wiki']});self.assertEqual(d.find('USLCI')['results'][0]['knowledge']['status'],'adopted')
            new.rename(new.with_name('renamed.md'));d.scan({'roots':['wiki']})
            self.assertEqual(d.find('USLCI')['results'][0]['path'],'wiki/renamed.md')
            new=old;old.write_text('USLCI different old content');d.scan({'roots':['wiki']})
            item=next(x for x in d.find('USLCI')['results'] if x['path']=='wiki/00-old.md');self.assertEqual(item['knowledge']['status'],'unknown')
        finally:d.close()
    def test_conflicting_adoption_scopes_are_visible(self):
        for asset,status in [('a','adopted'),('b','superseded')]:self.st.observation(asset,'adoption',{'path':'e.txt','status':status,'scope':'same use'},'review')
        d=Discovery(self.root,self.root/'search',state=self.st)
        try:self.assertEqual(d.knowledge(digest(self.root/'e.txt'))['status'],'conflict')
        finally:d.close()
    def sync(self):
        b={'work':'w','proposal':'Bounded fix','locations':['e.txt'],'reason':'Actual defect','impact':'Bounded scope'}
        self.st.observation('sync','sync',{**b,'status':'proposed'},'proposal')
        b.update(approval_source='actual fixture approval');self.st.observation('sync','sync',{**b,'status':'approved'},'approval');return b
    def test_sync_missing_evidence_and_test_only_effect_are_rejected(self):
        b=self.sync()
        with self.assertRaisesRegex(HarnessError,'does not exist'):self.st.observation('sync','sync',{**b,'status':'implemented','evidence':['missing.json']},'invalid')
        self.st.observation('sync','sync',{**b,'status':'implemented','evidence':['e.txt']},'implemented')
        with self.assertRaisesRegex(HarnessError,'effect-layer'):self.st.observation('sync','sync',{**b,'status':'verified','evidence':[{'path':'e.txt','layer':'test'}]},'test only')
    def test_effect_review_and_recurrence_preserve_approval_and_stale_evidence(self):
        b=self.sync();self.st.observation('sync','sync',{**b,'status':'implemented','evidence':['e.txt']},'implemented')
        v={**b,'status':'verified','evidence':[{'path':'e.txt','layer':'effect'}],'effect_review':{'reviewer':'main','scope':'one real use','observation':'actual expected behavior','coverage':'one invocation','limitations':['long term unknown']}}
        self.st.observation('sync','sync',v,'real use review');self.assertEqual(sync_items(self.st,'w'),[])
        (self.root/'e.txt').write_text('changed actual version');self.assertEqual(sync_items(self.st,'w')[0]['evidence_currentness'],'needs_review')
        self.st.observation('sync','sync',{**b,'status':'recurrence','evidence':[{'path':'e.txt','layer':'runtime'}],'failure':'observed repeat','next_action':'repair exact branch'},'real recurrence')
        self.assertEqual(self.st.observations('sync')[-1]['body']['approval_source'],b['approval_source'])

    def test_cached_search_marks_changed_current_file_without_overwriting_adoption(self):
        self.st.observation('a','adoption',{'path':'e.txt','status':'adopted','scope':'one use'},'review')
        d=Discovery(self.root,self.root/'search',state=self.st)
        try:
            d.scan({'roots':['e.txt']});(self.root/'e.txt').write_text('changed current body')
            item=d.find('actual fixture')['results'][0];self.assertEqual(item['knowledge']['status'],'needs_reindex');self.assertEqual(item['knowledge']['indexed_content']['status'],'adopted')
        finally:d.close()
    def test_distinct_adoption_scopes_are_not_reported_as_same_scope_conflict(self):
        self.st.observation('a','adoption',{'path':'e.txt','status':'adopted','scope':'research'},'review')
        self.st.observation('b','adoption',{'path':'e.txt','status':'superseded','scope':'public submission'},'review')
        d=Discovery(self.root,self.root/'search',state=self.st)
        try:self.assertEqual(d.knowledge(digest(self.root/'e.txt'))['status'],'multiple_scopes')
        finally:d.close()
    def test_judgment_change_and_user_recurrence_are_triage_candidates_only(self):
        self.judge(0,'old route');self.judge(1,'new route')
        self.st.observation('repeat','note',{'improvement_trigger':'user_reported_recurrence','work':'w','scope':'one recurring defect','evidence':'actual recorded user turn'},'actual user feedback')
        self.assertEqual(self.st.db.execute("SELECT count(*) FROM events WHERE kind='improvement_candidate'").fetchone()[0],2)
        data=json.loads(hook_context(self.st,'main').split('\n',1)[1]);self.assertEqual(len(data['improvement_candidates']),2)
    def test_return_timing_is_observed_duration_without_scientific_gain_claim(self):
        self.task('a');self.decide(['a']);self.st.review_task('a','returned',{'answer':'bounded result'},'fixture');d=self.decide()
        c=d['decision']['previous_outcome_comparison'] if 'decision' in d else self.st.get('w')['decision']['previous_outcome_comparison']
        self.assertIsNotNone(c['elapsed_since_previous_decision_seconds']);self.assertIsNotNone(c['seconds_since_latest_recorded_return'])

    def test_invalid_or_expired_sync_evidence_validity_is_rejected(self):
        b=self.sync()
        for stamp in ('not-a-date','2000-01-01T00:00:00+00:00','2099-01-01'):
            with self.subTest(stamp=stamp),self.assertRaisesRegex(HarnessError,'Evidence validity'):
                self.st.observation('sync','sync',{**b,'status':'implemented','evidence':[{'path':'e.txt','valid_until':stamp}]},'invalid')

    def test_failed_access_can_be_recorded_without_inventing_content_identity(self):
        self.st.observation('missing','access',{'path':'not-present.pdf','status':'unavailable'},'actual failed access')
        ref=self.st.observations('missing')[0]['body']['content_identity'];self.assertIsNone(ref['sha256']);self.assertEqual(ref['presence'],'unavailable')
