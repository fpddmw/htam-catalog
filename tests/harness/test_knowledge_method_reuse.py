import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from harness.assets import AssetRegistry
from harness.common import HarnessError, digest, encoded
from harness.context import knowledge_context
from harness.discovery import Discovery
from harness.experiences import ExperienceRegistry, bounded_selection
from harness.state import State


class KnowledgeMethodReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', root=self.root)
        self.assets = AssetRegistry(self.state)
        self.experiences = ExperienceRegistry(self.state)
        self.state.update('w', 0, {'title': 'Copper work', 'request': 'Review existing copper sources',
            'request_source': 'User request', 'objective': 'Useful copper evidence', 'status': 'active',
            'constraints': ['Keep Cu92% scope and source-table limitations']}, 'fixture')
        for name, text in [('source.txt', 'Copper evidence at table 2'), ('finding.txt', 'Recovered copper yield'),
            ('method.md', 'Read the original table before quantity adoption.'), ('origin.txt', 'Human approval fixture')]:
            (self.root / name).write_text(text)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def body(self, path='source.txt', version='1', **extra):
        return {'version': version, 'type': 'evidence', 'title': 'Copper evidence', 'description': 'Table-backed quantities',
            'provenance': [{'path': path}], 'uses': [{'purpose': 'source review', 'state': 'unreviewed', 'scope': 'Original table only', 'evidence': []}],
            'limitations': ['Original source scope'], 'depends_on': [], **extra}

    def test_alias_terms_and_relations_find_original_objects_without_adoption(self):
        self.assets.put('source', 0, self.body(aliases=['Cu', '铜']), 'fixture')
        self.assets.put('finding', 0, self.body('finding.txt', title='Recovered quantity', relations=[
            {'kind': 'derived_from', 'target': {'id': 'source', 'version': '1'}, 'reason': 'Uses the named source table'},
            {'kind': 'supports', 'target': {'path': 'source.txt', 'locator': 'table 2, row Cu92%'},
                'reason': 'Original table location', 'state': 'reviewed', 'evidence': [{'path': 'finding.txt'}]}]), 'fixture')
        found = self.assets.search('铜', {'work': 'w', 'purpose': 'new purpose'})
        self.assertEqual({item['id'] for item in found['items']}, {'source', 'finding'})
        item = next(item for item in found['items'] if item['id'] == 'finding')
        self.assertEqual(item['match_basis'], 'declared_relation')
        self.assertFalse(item['default_recommendation'])
        self.assertEqual(item['relations'][0]['state'], 'candidate')
        self.assertEqual(item['relations'][1]['target']['locator'], 'table 2, row Cu92%')
        self.assertTrue(item['relations'][1]['target']['sha256'])
        self.assertEqual(len(self.assets.impact('source')['semantic_relations']), 1)
        self.assertNotIn('assessments', self.state.get('w'))
        self.assertIn('source', {item['id'] for item in self.assets.search('Copper quantities')['items']})

    def test_unclassified_accessible_text_keeps_excerpt_and_access_stays_bounded(self):
        (self.root / 'unknown.md').write_text('Copper recovery has a Cu92% source limitation.')
        (self.root / 'private.md').write_text('Copper PRIVATE_SECRET')
        index = Discovery(self.root, self.root / 'index', self.state)
        try:
            index.scan({'roots': ['unknown.md', 'private.md'], 'entrypoints': [
                {'id': 'private', 'path': 'private.md', 'kind': 'project_entry', 'access': 'restricted'}]})
            result = index.retrieve('Copper limitation', {'work': 'w', 'purpose': 'review'}, budget=12000)
            self.assertEqual(result['unclassified_file_count'], 1)
            self.assertIn('Cu92%', result['unclassified_files'][0]['snippet'])
            selected = next(item for item in result['selected'] if item.get('path') == 'unknown.md')
            self.assertFalse(selected['default_recommendation'])
            self.assertNotIn('PRIVATE_SECRET', encoded(result))
            self.assertNotIn('private.md', encoded(result['selected']))
        finally:
            index.close()

    def test_wrapper_budget_keeps_required_semantics(self):
        class LargeCoverage:
            def retrieve(inner, query, scope, **kwargs):
                return {'mandatory': [{'kind': 'work_constraint', 'constraint': 'Keep Cu92% scope'}],
                    'selected': [], 'context_complete': True, 'required_load': None,
                    'omitted': {'mandatory': 0, 'optional': 0}, 'coverage': {'noise': 'x' * 30000}}
        packet = knowledge_context(self.state, LargeCoverage(), work='w', budget=900)
        self.assertIn('Keep Cu92% scope', encoded(packet))
        self.assertTrue(packet['context_complete'])
        self.assertLessEqual(len(encoded(packet).encode()), 900)
        oversized = [{'kind': 'counterexample', 'id': 'counter', 'description': 'Counterexample boundary ' * 300}]
        packet = bounded_selection(oversized, [], budget=512)
        self.assertTrue(packet['mandatory'])
        self.assertIn('Counterexample boundary', encoded(packet['mandatory']))
        self.assertFalse(packet['context_complete'])
        self.assertTrue(packet['required_load']['sha256'])
        packet = bounded_selection([{'kind': 'work_constraint', 'constraint': 'Keep Cu92% scope'}, *oversized], [], budget=512)
        self.assertIn('Keep Cu92% scope', encoded(packet['mandatory']))
        self.assertIn('Counterexample boundary', encoded(packet['mandatory']))
        self.assertFalse(packet['context_complete'])

    def test_retired_history_stays_out_of_default_context_and_candidates_are_not_methods(self):
        self.assets.put('draft-method', 0, self.body('method.md', type='method'), 'Unapproved draft fixture')
        candidate = self.assets.search('draft-method', {'work': 'w', 'purpose': 'source review'})['items'][0]
        self.assertFalse(candidate['default_recommendation'])
        self.assets.put('old-source', 0, self.body(), 'Historical fixture')
        self.assets.revise('old-source', 1, {'status': 'retired'}, 'Historical disposition')
        index = Discovery(self.root, self.root / 'index', self.state)
        try:
            index.scan({'roots': ['source.txt', 'method.md']})
            result = index.retrieve('', {'work': 'w'}, budget=12000)
            self.assertNotIn('old-source', {item.get('id') for item in result['selected']})
            historical = self.assets.search('old-source')['items'][0]
            self.assertEqual(historical['status'], 'retired')
        finally:
            index.close()

    def ordinary_proposal(self, *, linked=False):
        before = digest(self.root / 'method.md')
        after_text = 'Read original table, preserve Cu92% boundary, and cite the table row.'
        after = hashlib.sha256(after_text.encode()).hexdigest()
        release = {'asset_id': 'table-method', 'asset_expected': 0, 'version': 'approved-1',
            'scope': {'work': 'w', 'purpose': 'source review'}, 'changes': [
                {'path': 'method.md', 'before_sha256': before, 'after_sha256': after}], 'limitations': ['Only source review']}
        candidate = self.experiences.propose('table-method-fix', 0, {'work': 'w', 'title': 'Read original source table',
            'claim': 'Preserve source row and Cu92% scope', 'evidence': [{'path': 'source.txt'}],
            'applies': {'work': 'w', 'purposes': ['source review']}, 'method_targets': [{'path': 'method.md'}],
            'method_release': release, 'counterexamples': [{'id': 'other-feed', 'description': 'Other feed composition requires a separate scope',
                'evidence': [{'path': 'source.txt'}]}]}, 'proposal fixture')
        if not linked:
            self.state.bind('human', 'w', 'main', 'fixture')
        record = self.state.input('unbound-human' if linked else 'human', 'approval', 'Approve this exact original-table method change for source review.', raw_source={'kind': 'human'})
        assessment = self.state.assess_input(record['id'], 0, {'kind': 'human_direct', 'reviewer': 'fixture',
            'source_ref': 'direct user input', 'quote': 'Approve this exact original-table method change', 'reason': 'Fixture actual approval',
            'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'fixture')
        if linked:
            self.state.input_link(record['id'], 'w', self.state.get('w')['revision'], 0,
                {'reviewer': 'fixture', 'source_ref': 'original approval', 'reason': 'Exact original method scope',
                    'purpose': 'source review'}, 'fixture link')
        approval = {'input_id': record['id'], 'input_sha256': record['input_sha256'], 'assessment_revision': assessment['revision'],
            'scope_revision': 1, 'candidate_sha256': candidate['candidate_sha256'], 'quote': 'Approve this exact original-table method change',
            'reviewer': 'fixture', 'reason': 'Exact proposed method change and scope approved'}
        approved = self.experiences.approve('table-method-fix', candidate['revision'], approval, 'fixture')
        return release, after_text, approved

    def test_scope_ack_preserves_published_method_but_source_reassessment_invalidates_it(self):
        release, after_text, approved = self.ordinary_proposal()
        (self.root / 'method.md').write_text(after_text)
        published = self.experiences.publish_method('table-method-fix', approved['revision'], release, 'fixture')
        approval = approved['approval']['approval']
        self.assertTrue(approval['scope_sha256'])
        self.assertFalse(approval['scope_acknowledged'])
        self.state.ack(approval['input_id'], 'w', self.state.get('w')['revision'], 'Original approval acknowledged', scope_expected=1)
        acknowledged = self.experiences.get('table-method-fix')
        self.assertEqual(acknowledged['stage'], 'published')
        self.assertEqual(acknowledged['formal_methods']['current_publication_count'], 1)
        self.assertIsNone(acknowledged['approval_issue'])
        record = self.state.input_record(approval['input_id'])
        self.state.assess_input(record['id'], 1, {'kind': 'human_direct', 'reviewer': 'main',
            'source_ref': 'renewed origin review', 'quote': record['prompt'], 'reason': 'Origin reassessed',
            'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'origin reassessment')
        reassessed = self.experiences.get('table-method-fix')
        self.assertEqual(reassessed['approval_issue']['code'], 'REVISION_CONFLICT')
        self.assertEqual(reassessed['formal_methods']['current_publication_count'], 0)
        self.state.assess_input(record['id'], 2, {'kind': 'app_suggestion', 'reviewer': 'main',
            'source_ref': 'corrected actual origin', 'quote': record['prompt'], 'reason': 'Source rechecked',
            'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'origin correction')
        invalidated = self.experiences.get('table-method-fix')
        self.assertEqual(invalidated['approval_issue']['code'], 'PROVENANCE_UNVERIFIED')
        self.assertEqual(invalidated['formal_methods']['current_publication_count'], 0)
        self.assertEqual(self.assets.method_selections('table-method')[0]['authority'], 'needs_reapproval')
        self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM asset_versions').fetchone()[0], 1)

    def test_changed_link_reactivation_and_legacy_ack_need_current_same_candidate_approval(self):
        release, after_text, approved = self.ordinary_proposal(linked=True)
        (self.root / 'method.md').write_text(after_text)
        self.experiences.publish_method('table-method-fix', approved['revision'], release, 'fixture')
        # Emulate the existing pre-fix approval format, which did not capture a
        # stable scope receipt. No later observation can invent that evidence.
        with self.state.transaction():
            rows = self.state.db.execute("SELECT id,body FROM observations WHERE asset IN ('experience:table-method-fix','asset-selection:table-method')").fetchall()
            for row in rows:
                body = json.loads(row['body'])
                receipt = body.get('experience', {}).get('approval') or body.get('method_selection', {}).get('approval')
                if receipt:
                    receipt.pop('scope_sha256', None)
                    receipt.pop('scope_acknowledged', None)
                    self.state.db.execute('UPDATE observations SET body=? WHERE id=?', (encoded(body), row['id']))
        approval = approved['approval']['approval']
        input_id = approval['input_id']
        self.state.ack(input_id, 'w', self.state.get('w')['revision'], 'Original approval acknowledged', scope_expected=1)
        legacy = self.experiences.get('table-method-fix')
        self.assertEqual(legacy['approval_issue']['code'], 'REVISION_CONFLICT')
        self.assertEqual(legacy['formal_methods']['current_publication_count'], 0)

        def renew():
            current = self.experiences.get('table-method-fix')
            supplied = {**approval, 'scope_revision': self.state._input_scope(input_id, 'w')['scope_revision']}
            renewed = self.experiences.approve('table-method-fix', current['revision'], supplied, 'Current exact scope reviewed')
            self.assertEqual(renewed['stage'], 'published')
            self.assertEqual(renewed['formal_methods']['current_publication_count'], 1)
            selection = self.assets.method_selections('table-method')[0]
            self.assertEqual(selection['authority_approval_observation_id'], renewed['approval']['observation_id'])
            self.assertEqual(self.state.db.execute('SELECT COUNT(*) FROM asset_versions').fetchone()[0], 1)
            return renewed

        renew()
        original_link = {'reviewer': 'fixture', 'source_ref': 'original approval', 'reason': 'Exact original method scope', 'purpose': 'source review'}
        self.state.input_link(input_id, 'w', self.state.get('w')['revision'], 2,
            {**original_link, 'purpose': 'different purpose'}, 'fixture link')
        changed = self.experiences.get('table-method-fix')
        self.assertEqual(changed['formal_methods']['current_publication_count'], 0)
        self.assertEqual(changed['approval_issue']['code'], 'REVISION_CONFLICT')
        self.state.input_link(input_id, 'w', self.state.get('w')['revision'], 3, original_link, 'fixture link')
        self.assertEqual(self.experiences.get('table-method-fix')['formal_methods']['current_publication_count'], 0)
        renew()
        self.state.input_link(input_id, 'w', self.state.get('w')['revision'], 4,
            {**original_link, 'status': 'retracted'}, 'fixture link')
        self.assertEqual(self.experiences.get('table-method-fix')['approval_issue']['code'], 'INPUT_SCOPE_RETRACTED')
        self.state.input_link(input_id, 'w', self.state.get('w')['revision'], 5, original_link, 'fixture link')
        reactivated = self.experiences.get('table-method-fix')
        self.assertEqual(reactivated['approval_issue']['code'], 'REVISION_CONFLICT')
        self.assertEqual(reactivated['formal_methods']['current_publication_count'], 0)

    def test_ordinary_approval_publication_use_and_effect_are_separate(self):
        release, after_text, approved = self.ordinary_proposal()
        self.assertEqual(approved['stage'], 'approved_pending_publication')
        self.assertEqual(approved['formal_method_effect'], 'not_published')
        with self.assertRaisesRegex(HarnessError, 'not implemented'):
            self.experiences.publish_method('table-method-fix', approved['revision'], release, 'fixture')
        self.assertFalse(self.assets._present())
        (self.root / 'method.md').write_text(after_text)
        published = self.experiences.publish_method('table-method-fix', approved['revision'], release, 'fixture')
        self.assertEqual(published['stage'], 'published')
        self.assertEqual(published['formal_methods']['recorded_uses'], [])
        selected = self.assets.search('', {'work': 'w', 'purpose': 'source review'})['items'][0]
        self.assertTrue(selected['default_recommendation'])
        handoff = self.assets.handoff('table-method', 'approved-1', 'source review', context={'work': 'w'}, verify=True)
        self.assertEqual(handoff['validity']['state'], 'original_method_matches')
        self.assets.use('w', 'overall', 'table-method', 'approved-1', {'description': 'source review',
            'state': 'reused', 'evidence': [{'path': 'finding.txt'}]}, 'Actual scoped return fixture')
        used = self.experiences.get('table-method-fix')['formal_methods']
        self.assertEqual(len(used['recorded_uses']), 1)
        self.assertEqual(used['reported_effects'], [])
        self.assets.use('w', 'overall', 'table-method', 'approved-1', {'description': 'source review', 'state': 'reused',
            'outcome': {'assessment': 'no_gain', 'explanation': 'Original boundary already present in this case',
                'evidence': [{'path': 'finding.txt'}]}}, 'Follow-up fixture')
        self.assertEqual(self.experiences.get('table-method-fix')['formal_methods']['reported_effects'][0]['assessment'], 'no_gain')
        self.experiences.mark('table-method-fix', published['revision'], 'withdrawn', 'Scope no longer useful', 'fixture')
        retired = self.assets.search('', {'work': 'w', 'purpose': 'source review'})['items'][0]
        self.assertFalse(retired['default_recommendation'])
        self.assertEqual(retired['scope_selection']['authority'], 'needs_reapproval')
        self.assertEqual(len(self.experiences.get('table-method-fix')['formal_methods']['recorded_uses']), 2)

    def test_new_bytes_outside_approved_diff_cannot_register(self):
        release, _, approved = self.ordinary_proposal()
        (self.root / 'method.md').write_text('An unrelated unapproved method')
        with self.assertRaises(HarnessError):
            self.experiences.publish_method('table-method-fix', approved['revision'], release, 'fixture')
        self.assertEqual(self.experiences.get('table-method-fix')['formal_method_effect'], 'not_published')

    def test_real_origin_approval_is_pending_publication_and_revalidates(self):
        # Current counterpart of the original B05 case: approval is neither
        # publication nor a file edit, and corrected source provenance prevails.
        original = digest(self.root / 'method.md')
        _, _, approved = self.ordinary_proposal()
        self.assertEqual(approved['stage'], 'approved_pending_publication')
        self.assertEqual(approved['formal_method_effect'], 'not_published')
        self.assertEqual(approved['formal_methods']['current_publication_count'], 0)
        self.assertEqual(approved['formal_methods']['publications'], [])
        self.assertEqual(digest(self.root / 'method.md'), original)
        approval = approved['approval']['approval']
        record = self.state.input_record(approval['input_id'])
        self.state.assess_input(record['id'], 1, {'kind': 'app_suggestion', 'reviewer': 'main',
            'source_ref': 'corrected actual origin', 'quote': record['prompt'], 'reason': 'Source rechecked',
            'input_sha256': record['input_sha256'], 'evidence': [{'path': 'origin.txt'}]}, 'origin correction')
        rechecked = self.experiences.get('table-method-fix')
        self.assertEqual(rechecked['stage'], 'candidate')
        self.assertEqual(rechecked['approval_issue']['code'], 'PROVENANCE_UNVERIFIED')
        self.assertEqual(rechecked['formal_method_effect'], 'not_published')


if __name__ == '__main__':
    unittest.main()
