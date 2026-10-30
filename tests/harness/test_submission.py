import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
from harness.state import State
from harness.common import HarnessError
from harness.runtime import native_hash
from harness import submission

class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.s=State(self.root/'store',root=self.root)
        self.s.update('w',0,{'title':'research','request':'deliver','request_source':'fixture','objective':'exact content','status':'active'},'fixture')
        self.s.bind('main','w','main','fixture');self.s.bind('worker','w','executor','fixture')
        self.body={'sourceDataSet':{'description':'reviewed original and limitations'}}
        self.row={'id':'a','version':'002','json':self.body};self.write('rows.jsonl',self.row)
        self.obj={'id':'a','version':'002','root':'sourceDataSet','payload_file':'rows.jsonl','payload_sha256':native_hash(self.body)}
        with self.s.transaction():ev=self.s.event('w','native_stage_reviewed','fixture',{'review':{'reviewer':'main','review_mode':'independent'}})
        self.auth=submission.authorize(self.s,{'work':'w','writer_session':'main','review_event':ev,'purpose':'normal_review','reason':'Reviewed final source','objects':[self.obj],'source':'fixture'})
        self.stamp=datetime.now(timezone.utc)-timedelta(seconds=30);self.report('full',self.obj['payload_sha256'])
        self.call={'code':"// HTAM_SUBMISSION:one\nawait tab.playwright.getByRole('button', {name:'Submit Review'}).click();"}
        self.req={'id':'one','work':'w','session':'worker','owner_id':'owner','authorization_event':self.auth['event'],'objects':[self.obj],'verification_reports':['full.json'],'tool_name':'mcp__cua_repl__js','tool_input':self.call,'source':'fixture'}
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def write(self,name,value): (self.root/name).write_text(json.dumps(value))
    def report(self,name,remote,*,local=None,status='ok',seconds=0):
        c={'role':'root','id':'a','version':'002','path':'/sourceDataSet#readback','status':status,'remote_user_id':'owner','remote_state_code':0,'local_payload_sha256':self.obj['payload_sha256'] if local is None else local,'remote_payload_sha256':remote}
        self.write(name+'.jsonl',c)
        r={'schema_version':1,'generated_at_utc':(self.stamp+timedelta(seconds=seconds)).isoformat(),'status':'passed_remote_verification' if status=='ok' else 'blocked_remote_verification','counts':{'blockers':0 if status=='ok' else 1},'files':{'checks':name+'.jsonl','report':name+'.json'}}
        self.write(name+'.json',r);return r
    def event(self,raw=None):return {'hook_event_name':'PreToolUse','session_id':'worker','tool_name':'mcp__cua_repl__js','tool_input':raw or self.call,'tool_use_id':'actual-call'}
    def decision(self,e=None):return submission.guard(self.s,e or self.event())['hookSpecificOutput']['permissionDecision']
    def test_exact_batch_checked_at_actual_call_once(self):
        submission.prepare(self.s,self.req);self.assertEqual(self.decision(),'allow');self.assertEqual(self.decision(),'deny')
    def test_edited_payload_or_actual_call_revokes_readiness(self):
        submission.prepare(self.s,self.req)
        self.assertEqual(self.decision(self.event({'code':self.call['code']+' // changed action'})),'deny')
        self.row['json']['sourceDataSet']['description']='edited';self.write('rows.jsonl',self.row)
        self.assertEqual(self.decision(),'deny')
    def test_mismatch_then_state_only_success_cannot_clear_content_failure(self):
        self.report('bad','changed',status='payload_mismatch',seconds=5)
        submission.observe(self.s,'w',['bad.json'],'fixture')
        r=self.report('state',None,local='',seconds=10)
        submission.observe_hook(self.s,{'hook_event_name':'PostToolUse','session_id':'worker','tool_response':{'output':json.dumps(r)}})
        with self.assertRaisesRegex(HarnessError,'known full-content'):submission.prepare(self.s,self.req)
        self.report('resolved',self.obj['payload_sha256'],seconds=15);self.req['verification_reports']=['resolved.json']
        submission.prepare(self.s,self.req);self.assertEqual(self.decision(),'allow')
    def test_newer_failure_after_prepare_denies_same_call(self):
        submission.prepare(self.s,self.req)
        r=self.report('bad','changed',status='payload_mismatch',seconds=5)
        submission.observe_hook(self.s,{'hook_event_name':'PostToolUse','session_id':'worker','tool_response':{'output':json.dumps(r)}})
        self.assertEqual(self.decision(),'deny')
    def test_readonly_browser_and_unrelated_object_are_not_blocked(self):
        self.assertIsNone(submission.guard(self.s,self.event({'code':'await tab.playwright.domSnapshot();'})))
        self.report('other','changed',status='payload_mismatch',seconds=5)
        c=json.loads((self.root/'other.jsonl').read_text());c['id']='different';self.write('other.jsonl',c)
        submission.observe(self.s,'w',['other.json'],'fixture');submission.prepare(self.s,self.req);self.assertEqual(self.decision(),'allow')
    def test_state_only_or_stale_proof_cannot_prepare(self):
        self.report('state',None,local='');self.req['verification_reports']=['state.json']
        with self.assertRaisesRegex(HarnessError,'full-body'):submission.prepare(self.s,self.req)
        self.report('old',self.obj['payload_sha256'],seconds=-1000);self.req['verification_reports']=['old.json']
        with self.assertRaisesRegex(HarnessError,'15 minutes'):submission.prepare(self.s,self.req)
    def test_unprepared_named_submit_is_denied(self):
        self.assertEqual(self.decision(self.event({'code':"await tab.playwright.getByRole('button', {name:'提交审核'}).click();"})),'deny')
    def test_executor_cannot_convert_research_review_into_submission_scope(self):
        req={**self.auth,'writer_session':'worker'}
        with self.assertRaisesRegex(HarnessError,'main reviewer'):submission.authorize(self.s,req)

    def test_withdrawn_scope_revokes_already_prepared_action(self):
        submission.prepare(self.s,self.req)
        submission.retract(self.s,{'work':'w','authorization_event':self.auth['event'],'writer_session':'main','reason':'New final material supersedes earlier draft','source':'fixture'})
        self.assertEqual(self.decision(),'deny')

if __name__=='__main__':unittest.main()
