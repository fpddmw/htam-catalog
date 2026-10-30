from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.assets import AssetRegistry, text_match
from harness.common import digest, encoded, validation_scope
from harness.discovery import Discovery
from harness.experiences import ExperienceRegistry
from harness.state import State


class CapabilityDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / 'store', self.root)
        self.state.update('w', 0, {'title': 'Curation', 'request': 'Find existing route',
            'request_source': 'fixture', 'objective': 'Use an applicable original entry',
            'status': 'active', 'constraints': ['Preserve source identity and scope']}, 'fixture')
        self.index = Discovery(self.root, self.root / 'index', self.state)

    def tearDown(self):
        self.index.close()
        self.state.close()
        self.temp.cleanup()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def scan(self, entries=()):
        self.index.scan({'roots': ['wiki', 'runs', '.agents/skills'], 'entrypoints': list(entries)})

    def entry(self, name, **extra):
        path = 'wiki/' + name + '.md'
        self.write(path, 'curation route ' + name)
        return {'id': name, 'kind': 'project_entry', 'path': path, **extra}

    def asset(self, identifier, **extra):
        AssetRegistry(self.state).put(identifier, 0, {'version': '1', 'type': 'evidence',
            'title': 'curation historical object', 'description': 'Large old run ' * 250,
            'provenance': [{'path': 'runs/old.md'}], 'depends_on': [],
            'uses': [{'purpose': 'curation', 'state': 'unreviewed', 'scope': 'One object', 'evidence': []}],
            'limitations': ['Preserve original object scope'], **extra}, 'fixture')

    def test_current_capability_and_skill_precede_history_without_reindex(self):
        route = self.write('wiki/tiangong-lca-tools.md', 'Previous tools route')
        self.write('wiki/capabilities-and-needs.md', 'Old evidence [record](../runs/curation-debug.md)')
        skill = self.write('.agents/skills/curation/SKILL.md', 'Previous skill body')
        for i in range(65):
            self.write(f'runs/{i:02}-curation-debug.md', 'curation-gate is unavailable in this historical run')
        self.scan()
        indexed = self.index.db.execute('SELECT sha,text FROM files WHERE path=?', ('wiki/tiangong-lca-tools.md',)).fetchone()
        route.write_text('Current standalone entity review: dataset curation-queue build/next/verify. Kelly verify receipt.')
        skill.write_text('Use dataset curation-queue for standalone entity review; inspect its current objects.')
        self.index.close()
        self.index = Discovery(self.root, self.root / 'index', self.state, readonly=True)
        with validation_scope():
            found = self.index.find('curation', limit=2)
            result = self.index.retrieve('curation', {'work': 'w', 'purpose': 'curation'}, limit=2, budget=8000)
        self.assertEqual(found['results'][0]['path'], '.agents/skills/curation/SKILL.md')
        self.assertEqual({i['kind'] for i in result['selected']}, {'original_entry'})
        self.assertIn('wiki/tiangong-lca-tools.md', {i['path'] for i in result['selected']})
        entry = next(i for i in result['selected'] if i['path'] == 'wiki/tiangong-lca-tools.md')
        self.assertEqual(entry['current_file'], 'read_current_original')
        self.assertEqual(entry['sha'], digest(route))
        self.assertFalse(entry['default_recommendation'])
        self.assertIn('curation-queue', entry['snippet'])
        inspected = self.index.inspect_entry(entry['id'])
        self.assertIn('build/next/verify', inspected['text'])
        self.assertEqual(inspected['scientific_adoption'], 'unchanged')
        self.assertEqual(self.index.db.total_changes, 0)
        after = self.index.db.execute('SELECT sha,text FROM files WHERE path=?', ('wiki/tiangong-lca-tools.md',)).fetchone()
        self.assertEqual(tuple(indexed), tuple(after))
        self.assertIn('not proof of absence', found['not_found_means'])
        self.assertEqual(self.index.retrieve('', {'work': 'w'})['selected'], [])

    def test_scope_status_and_conflict_rank_below_current_matching_entry(self):
        entries = [
            self.entry('00-wrong-scope', applies={'work': 'other'}),
            self.entry('01-wrong-version', applies={'tool_versions': {'cli': ['0.1.22']}}),
            self.entry('02-retired', status='retired'),
            self.entry('03-conflict'),
            self.entry('04-unavailable'),
            self.entry('99-current', status='active', applies={'work': 'w', 'tool_versions': {'cli': ['0.1.28']}}),
        ]
        self.scan(entries)
        (self.root / 'wiki/04-unavailable.md').unlink()
        for identifier, status in [('a', 'adopted'), ('b', 'rejected')]:
            self.state.observation(identifier, 'adoption', {'path': 'wiki/03-conflict.md', 'status': status,
                'scope': 'same use'}, 'fixture')
        with validation_scope():
            result = self.index.retrieve('curation', {'work': 'w', 'tool_versions': {'cli': '0.1.28'}}, limit=8, budget=20000)
        self.assertEqual(result['selected'][0]['id'], '99-current')
        originals = {i['id']: i for i in result['selected'] if i['kind'] == 'original_entry'}
        self.assertEqual(originals['00-wrong-scope']['applicability']['state'], 'inapplicable')
        self.assertEqual(originals['01-wrong-version']['applicability']['state'], 'inapplicable')
        self.assertEqual(originals['03-conflict']['knowledge_status'], 'conflict')
        self.assertEqual(originals['04-unavailable']['current_file'], 'unavailable')
        self.assertNotIn('02-retired', originals)
        self.assertTrue(all(not i['default_recommendation'] for i in originals.values()))

    def test_budget_retains_original_route_and_required_counterexample(self):
        current = self.entry('route', applies={'purposes': ['curation']})
        self.write('runs/old.md', 'curation old run')
        self.scan([current])
        for i in range(3):
            self.asset('old-' + str(i))
        experience = ExperienceRegistry(self.state)
        experience.propose('scope-check', 0, {'work': 'w', 'title': 'curation scope',
            'claim': 'Read the exact entry', 'applies': {'work': 'w'},
            'evidence': [{'path': 'runs/old.md'}], 'counterexamples': [
                {'id': 'other-object', 'description': 'Other objects need their own professional judgment for composition, product function and boundary.',
                 'evidence': [{'path': 'runs/old.md'}]}]}, 'fixture')
        with validation_scope():
            result = self.index.retrieve('curation', {'work': 'w', 'purpose': 'curation', 'mechanisms': ['scope-check']}, limit=1, budget=1100)
        self.assertTrue(result['context_complete'])
        self.assertIn('Preserve source identity', encoded(result['mandatory']))
        self.assertIn('Other objects need their own professional judgment', encoded(result['mandatory']))
        self.assertEqual(result['selected'][0]['id'], 'route')
        self.assertEqual(result['selected'][0]['kind'], 'original_entry')
        self.assertTrue(result['selected'][0]['details_omitted'])
        self.assertEqual(result['selected'][0]['sha'], digest(self.root / current['path']))
        self.assertFalse(result['selected'][0]['default_recommendation'])
        self.assertLessEqual(result['loaded_payload_bytes'], 1100)
        with validation_scope():
            tiny = self.index.retrieve('curation', {'work': 'w', 'mechanisms': ['scope-check']}, limit=1, budget=512)
        self.assertFalse(tiny['context_complete'])
        self.assertEqual(tiny['selected'], [])
        self.assertIn('Other objects need their own professional judgment', encoded(tiny['mandatory']))

    def test_explicit_version_pin_keeps_its_priority_and_version(self):
        entry = self.entry('route')
        self.write('runs/old.md', 'curation old run')
        self.scan([entry])
        self.asset('exact-version', description='curation selected version')
        with validation_scope():
            result = self.index.retrieve('curation', {'work': 'w', 'asset_versions': {'exact-version': '1'}}, limit=2, budget=8000)
        self.assertEqual(result['selected'][0]['kind'], 'registered_asset')
        self.assertEqual(result['selected'][0]['version'], '1')
        self.assertTrue(result['selected'][0]['explicit_version_pin'])
        budget = len(encoded(result['mandatory']).encode()) + len(encoded(result['selected'][0]).encode()) + 500
        with validation_scope():
            bounded = self.index.retrieve('curation', {'work': 'w', 'asset_versions': {'exact-version': '1'}}, limit=2, budget=budget)
        self.assertEqual([i['kind'] for i in bounded['selected']], ['registered_asset', 'original_entry'])
        self.assertTrue(bounded['selected'][1]['details_omitted'])

    def test_current_removed_terms_and_read_boundaries_are_respected(self):
        route = self.write('wiki/tiangong-lca-tools.md', 'curation withdrawn route')
        secret = self.entry('private', access='restricted')
        self.write('wiki/.env', 'curation CREDENTIAL_SECRET')
        skill = self.write('.agents/skills/private/SKILL.md', 'curation before symlink')
        self.scan([secret, {'id': 'credential', 'path': 'wiki/.env', 'kind': 'project_entry'}])
        route.write_text('Current route without the withdrawn keyword')
        (self.root / secret['path']).write_text('curation PRIVATE_SECRET')
        outside = self.root.parent / (self.root.name + '-outside.md')
        try:
            outside.write_text('curation OUTSIDE_SECRET')
            skill.unlink()
            skill.symlink_to(outside)
            with validation_scope():
                result = self.index.find('curation')
            self.assertNotIn('SECRET', encoded(result))
            self.assertNotIn('wiki/tiangong-lca-tools.md', {i['path'] for i in result['results']})
            self.assertEqual(result['results'], [])
            with self.assertRaisesRegex(Exception, 'Unknown discovery entrypoint'):
                self.index.inspect_entry('file:.agents/skills/private/SKILL.md')
        finally:
            outside.unlink(missing_ok=True)


    def test_sql_candidates_preserve_unicode_terms_paths_identifiers_and_escapes(self):
        for path, text in [
            ('runs/german.md', 'Straße copper'),
            ('runs/greek.md', 'ΟΣ ς σ copper'),
            ('runs/ligature.md', 'oﬃce ﬀ ﬂ ﬁ ﬅ ﬆ'),
            ('runs/turkish.md', 'İZMİR ı copper'),
            ('runs/kelvin.md', 'KELVIN copper'),
            ('runs/accent.md', 'ÉCOLE cuivre'),
            ('runs/铜路径.md', '机制与原始 模型条件'),
            ('runs/spacing.md', 'COPPER first section\nseparate curation section'),
            ('runs/control.md', 'line\nnext\tvalue "quoted" \\literal %_ [brackets]'),
            ('runs/10.1234-PATH-ID.md', 'Different words'),
        ]:
            self.write(path, text.replace('\\n', '\n').replace('\\t', '\t'))
        self.scan()
        self.index.db.execute('UPDATE files SET identifiers=? WHERE path=?',
            (encoded(['10.9876/IDENTIFIER']), 'runs/accent.md'))
        self.index.db.commit()
        rows = [dict(row) for row in self.index.db.execute('SELECT * FROM files')]
        for query in ['STRASSE', 'σ', 'OFFICE', 'İZMİR', 'IZMIR', 'KELVIN', 'école', '铜', '机制 模型',
                'copper curation', '10.1234-PATH-ID', '10.9876/identifier', r'\n', r'\"quoted\"', '%_', '"',
                '  COPPER    curation  ']:
            with self.subTest(query=query):
                expected = {row['path'] for row in rows if text_match(query,
                    (row['path'] + '\n' + row['identifiers'] + '\n' + row['text']).casefold())}
                candidates = {row['path'] for row in self.index._index_candidates(query)}
                self.assertLessEqual(expected, candidates)
                with validation_scope():
                    found = self.index.find(query, limit=50)
                actual = {path for item in found['results'] for path in [item['path'], *item['other_paths']]}
                self.assertEqual(expected, actual)

    def test_prefilter_checks_only_candidates_and_keeps_dynamic_read_protection(self):
        self.write('runs/good.md', 'curation current evidence')
        private = self.write('runs/private.md', 'curation PRIVATE_SECRET')
        link = self.write('runs/link.md', 'curation before replacement')
        for i in range(250):
            self.write(f'runs/irrelevant-{i:03}.md', 'Unrelated temperature evidence')
        self.scan([{'id': 'private', 'path': 'runs/private.md', 'kind': 'project_entry', 'access': 'restricted'}])
        outside = self.root.parent / (self.root.name + '-outside.md')
        try:
            outside.write_text('curation OUTSIDE_SECRET')
            link.unlink()
            link.symlink_to(outside)
            statements = []
            self.index.db.set_trace_callback(statements.append)
            with patch.object(self.index, '_permitted', wraps=self.index._permitted) as permission, validation_scope():
                found = self.index.find('curation')
            self.assertEqual([item['path'] for item in found['results']], ['runs/good.md'])
            self.assertEqual(found['search_coverage']['candidate_paths_checked'], 3)
            self.assertFalse(any('irrelevant-' in str(call.args[0]) for call in permission.call_args_list))
            self.assertNotIn('SECRET', encoded(found))
            body_reads = [sql for sql in statements if sql.startswith('SELECT * FROM files WHERE')]
            self.assertTrue(body_reads)
            self.assertFalse(any('private.md' in sql or 'link.md' in sql for sql in body_reads))
            self.assertLess(len(permission.call_args_list), 20)
        finally:
            self.index.db.set_trace_callback(None)
            outside.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
