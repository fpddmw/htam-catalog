import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from harness.common import HarnessError
from harness.state import State
from harness.assets import AssetRegistry
from harness.context import hook_context, render_context
from harness import goals, portfolio


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = self.root / 'store'; self.s = State(self.store, self.root)
        self.parent = self.s.update('materials', 0, self.patch('Original materials', [
            {'id': 'B23', 'description': 'APT and tungsten powder', 'source': 'original list'},
            {'id': 'A03', 'description': 'Recycled copper', 'source': 'original list'}]), 'user')
        self.s.update('apt', 0, {**self.patch('APT stage'), 'parent': self.relation()}, 'user-approved organization')
        (self.root/'source.json').write_text('quantity: 1 kg')
        self.reg = AssetRegistry(self.s)

    def tearDown(self): self.s.close(); self.temp.cleanup()

    def patch(self, title, requirements=None):
        b={'title': title, 'request': 'Continue research', 'request_source': 'user source',
           'objective': title, 'status': 'active'}
        if requirements is not None: b['requirements'] = requirements
        return b

    def relation(self):
        return {'work':'materials','requirements':['B23'],'scope':'APT component only; powder remains', 'source':'user-approved portfolio'}

    def body(self, version='1', dependencies=None, path='source.json'):
        return {'version':version,'type':'lci','title':'Scoped inventory','description':'Source-limited research',
                'provenance':[{'path':path}], 'uses':[{'purpose':'conditional research','state':'conditional',
                'scope':'source-specific','evidence':[{'path':path}]}], 'limitations':['No supplier-specific claim'],
                'depends_on':dependencies or []}

    def test_organization_preserves_task_and_does_not_complete_parent(self):
        t=self.s.create_task('t','apt',1,{'expected':'Inspect source','purpose':'Source check'},'plan')
        old=self.s.get('apt')['request_revision']
        self.s.update('apt',1,{'parent':self.relation()},'organization')
        self.assertEqual(self.s.get('apt')['request_revision'],old)
        goals.current_task(self.s,t)
        self.assertFalse(goals.completion(self.s,self.s.get('materials'))['passed'])
        self.assertEqual(portfolio.view(self.s,'materials')['requirements'][0]['stages'][0]['id'],'apt')

    def test_cycles_missing_parent_and_dangling_requirement_are_rejected(self):
        with self.assertRaises(HarnessError):
            self.s.update('materials',1,{'parent':{'work':'apt','requirements':['overall'],'scope':'cycle','source':'test'}},'test')
        with self.assertRaises(HarnessError):
            self.s.update('apt',1,{'parent':{**self.relation(),'requirements':['missing']}},'test')
        with self.assertRaises(HarnessError):
            self.s.update('materials',1,{'requirements':[{'id':'A03','description':'Copper','source':'original'}],
                'change':{'kind':'scope','reason':'test','impact':'test','source':'test','approval_source':'user test'}},'test')

    def test_parent_visibility_and_asset_summary_are_bounded(self):
        self.reg.put('inventory',0,self.body(),'existing source review')
        self.reg.use('apt','overall','inventory','1','Inspect conditional use','reuse')
        data=render_context(self.s,work='apt')
        self.assertEqual(data['ancestry']['items'][0]['id'],'materials')
        raw=hook_context(self.s,None,work='apt')
        self.assertLessEqual(len(raw.encode()),6500)
        d=json.loads(raw.split('\n',1)[1])
        self.assertEqual(d['assets']['count'],1)
        self.assertEqual(d['assets']['items'][0]['recorded_validity'],'not_rechecked')
        self.assertEqual(d['ancestry']['items'][0]['contributes_to'],['B23'])

    def test_changed_asset_blocks_only_declared_dependent_task(self):
        self.reg.put('inventory',0,self.body(),'review')
        (self.root/'other.json').write_text('different source')
        self.reg.put('independent',0,self.body(path='other.json'),'review')
        def task(identifier,asset):
            return self.s.create_task(identifier,'apt',1,{'expected':'Use existing asset','purpose':'Answer scope',
                'assets':[{'id':asset,'version':'1','purpose':'exact quantity evidence'}]},'plan')
        affected=task('affected','inventory'); independent=task('independent','independent')
        goals.ready(self.s,affected); goals.ready(self.s,independent)
        (self.root/'source.json').write_text('quantity corrected')
        with self.assertRaises(HarnessError): goals.ready(self.s,affected)
        goals.ready(self.s,independent)

    def test_new_version_does_not_force_unrelated_pinned_content_upgrade(self):
        self.reg.put('inventory',0,self.body(),'review')
        t=self.s.create_task('pinned','apt',1,{'expected':'Reuse old exact source','purpose':'reproduction',
            'assets':[{'id':'inventory','version':'1','purpose':'old reproduction'}]},'plan')
        self.reg.put('inventory',1,self.body(version='2'),'new release')
        goals.ready(self.s,t)
        self.assertEqual(self.reg.get('inventory','1')['body']['version'],'1')

    def test_reuse_is_visible_without_adopting_requirement(self):
        self.reg.put('inventory',0,self.body(),'review')
        before=self.s.get('materials')
        self.reg.use('materials','A03','inventory','1','Compare applicability to copper','reuse')
        self.assertEqual(before,self.s.get('materials'))
        row=portfolio.view(self.s,'materials')['requirements'][1]
        self.assertEqual(row['assets'][0]['uses'][0]['state'],'reused')
        self.assertEqual(row['assets'][0]['uses'][0]['applicability'],'not_rechecked')
        checked=portfolio.view(self.s,'materials',verify=True)['requirements'][1]
        self.assertEqual(checked['assets'][0]['uses'][0]['applicability'],'unreviewed')
        self.assertNotEqual(row['state'],'satisfied')

    def test_generated_views_use_effective_lifecycle_and_never_hash_sources(self):
        from harness.cli import render
        from unittest.mock import patch
        self.reg.put('inventory',0,self.body(),'review')
        self.reg.use('apt','overall','inventory','1','Observe current scope','reuse')
        self.reg.revise('inventory',1,{'uses':[{'purpose':'conditional research','state':'unavailable','scope':'source-specific','evidence':[]}], 'status':'retired'},'withdraw use')
        with patch('harness.assets.digest',side_effect=AssertionError('automatic view read source')):
            data=portfolio.view(self.s,'apt')
            rendered=render(self.s)
        asset=data['requirements'][0]['assets'][0]
        self.assertEqual(asset['uses_claims'][0]['state'],'unavailable')
        self.assertEqual(asset['lifecycle'],'retired')
        text=(self.store/'current.md').read_text()
        self.assertIn('APT component only; powder remains',text)
        self.assertIn('portfolios/materials.md',text)
        self.assertIn('not_rechecked',(self.store/'portfolios/apt.md').read_text())

    def test_portfolio_retains_older_exact_use_version(self):
        self.reg.put('inventory',0,self.body(),'review')
        self.reg.use('apt','overall','inventory','1','Reproduce pinned data','reuse')
        self.reg.put('inventory',1,self.body(version='2'),'new version')
        for verify in [False,True]:
            data=portfolio.view(self.s,'apt',verify=verify)
            self.assertEqual(data['requirements'][0]['assets'][0]['version'],'1')

    def test_recurrence_response_keeps_original_event_and_updates_attention(self):
        self.s.observation('goal','note',{'work':'apt','scope':'parent goal','improvement_trigger':'user_reported_recurrence','evidence':['source.json']},'real reported failure')
        event=self.s.db.execute("SELECT seq FROM events WHERE kind='improvement_candidate'").fetchone()[0]
        self.s.triage('apt',event,{'disposition':'monitoring','reason':'parent goal visible in actual recovery',
            'next_action':'observe subsequent material work','evidence':[{'path':'source.json'}]},'approved repair')
        d=json.loads(hook_context(self.s,None,work='apt').split('\n',1)[1])
        self.assertEqual(d['improvement_candidates'][0]['status'],'monitoring')
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM events WHERE kind='improvement_candidate'").fetchone()[0],1)
        with self.assertRaises(HarnessError):
            self.s.triage('materials',event,{'disposition':'addressed','reason':'wrong work','next_action':'none','evidence':[{'path':'source.json'}]},'invalid scope')

    def test_default_status_discovers_work_without_dumping_history(self):
        self.s.update('apt',1,{'accepted':['large historical result '*200 for _ in range(100)]},'retained history')
        repo=Path(__file__).resolve().parents[2]
        command=[sys.executable,str(repo/'scripts/harness.py'),'--root',str(self.root),'--store',str(self.store),'status']
        compact=subprocess.run(command,capture_output=True,text=True)
        full=subprocess.run(command+['--full'],capture_output=True,text=True)
        self.assertEqual(compact.returncode,0,compact.stderr)
        self.assertLess(len(compact.stdout.encode()),4000)
        self.assertNotIn('accepted',json.loads(compact.stdout)[0])
        self.assertTrue(any('accepted' in x for x in json.loads(full.stdout)))

    def test_cli_readonly_portfolio_and_assets_leave_store_unchanged(self):
        self.reg.put('inventory',0,self.body(),'review')
        repo=Path(__file__).resolve().parents[2]
        command=[sys.executable,str(repo/'scripts/harness.py'),'--root',str(self.root),'--store',str(self.store),'--read-only']
        for tail in [['portfolio','--work','materials'],['assets'],['asset-show','--id','inventory']]:
            out=subprocess.run(command+tail,capture_output=True,text=True)
            self.assertEqual(out.returncode,0,out.stderr);json.loads(out.stdout)
        out=subprocess.run(command+['asset-put','--id','bad','--expected','0','--file','missing','--source','test'],capture_output=True,text=True)
        self.assertEqual(out.returncode,2)
        self.assertEqual(len(self.reg.list()),1)


if __name__ == '__main__': unittest.main()
