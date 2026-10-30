"""Focused package boundary, integrity and freshness checks; no live State or native host."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import cli, dot_package as dp
from harness.common import HarnessError


class DotPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.output = Path(self.temp.name) / 'package'
        self.arguments = {'source_thread': '01a15520-3a8c-7292-b9b1-9bacb52060f5',
                          'authorization_reference': 'Human approved fixed framework excerpts in source thread',
                          'cloud_directory': '/workspace/scratch/d077817aa117/htam-coordination'}
        for name, _, selectors in dp.SOURCES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            text = '# Source\n\n## Excluded before\n\nPRIVATE BEFORE\n\n'
            for selector in selectors:
                suffix = '（待审阅、未迁移）' if selector == '10 Dot云端接入安排' else ''
                text += '## ' + selector + suffix + '\n\nSelected body: ' + selector + '\n\n'
                if selector == '经验同步与RSI':
                    text += '### 合并、退役与恢复\n\nSelected operations.\n\n'
                    text += '### 订阅实验的受信Python入口\n\nPRIVATE NATIVE CODE\n\n'
            text += '## Excluded after\n\nPRIVATE AFTER\n'
            path.write_text(text, encoding='utf-8')

    def export(self):
        return dp.export(self.root, self.output, **self.arguments)

    def manifest(self):
        return json.loads((self.output / 'manifest.json').read_text(encoding='utf-8'))

    def write_manifest(self, value):
        (self.output / 'manifest.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_exact_boundary_roundtrip_and_external_identity(self):
        result = self.export()
        actual = sorted(p.relative_to(self.output).as_posix() for p in self.output.rglob('*') if p.is_file())
        self.assertEqual(list(dp.ALLOWLIST), actual)
        for guide in ('architecture-guide.md', 'operations-guide.md'):
            text = (self.output / 'references' / guide).read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE', text)
        self.assertNotIn('订阅实验的受信Python入口', (self.output / 'references/operations-guide.md').read_text(encoding='utf-8'))
        self.assertEqual([], self.manifest()['research_work_scopes'])
        checked = dp.check(self.output, manifest_sha256=result['manifest_sha256'])
        self.assertEqual('valid', checked['integrity'])
        self.assertTrue(checked['external_manifest_matched'])
        self.assertEqual('unknown', checked['source_freshness']['status'])
        self.assertEqual('not_checked', checked['delivery_status'])
        self.assertEqual('current', dp.check(self.output, source_root=self.root)['source_freshness']['status'])
        with self.assertRaises(HarnessError):
            dp.check(self.output, manifest_sha256='0' * 64)
        self.assertFalse((self.root / '.harness-local').exists())

    def test_source_selected_drift_vs_full_file_drift_and_unavailable(self):
        self.export()
        source = self.root / dp.SOURCES[0][0]
        source.write_text(source.read_text(encoding='utf-8') + '\nOutside selection changed.\n', encoding='utf-8')
        freshness = dp.check(self.output, source_root=self.root)['source_freshness']
        self.assertEqual('stale', freshness['status'])
        self.assertEqual('current', freshness['selected_sections'])
        self.assertEqual('stale', freshness['full_source_files'])
        source.write_text(source.read_text(encoding='utf-8').replace('Selected body:', 'Changed selected body:', 1), encoding='utf-8')
        self.assertEqual('stale', dp.check(self.output, source_root=self.root)['source_freshness']['selected_sections'])
        unknown = dp.check(self.output, source_root=self.root / 'missing')['source_freshness']
        self.assertEqual('unknown', unknown['status'])
        self.assertEqual('unknown', unknown['selected_sections'])

    def test_missing_ambiguous_titles_and_stop_refuse_without_output(self):
        source = self.root / 'harness/README.md'
        original = source.read_text(encoding='utf-8')
        variants = (
            original.replace('## 日常使用', '## 日常使用与其他未批准内容'),
            original + '\n## 日常使用（历史）\n\nDuplicate\n',
            original.replace('### 订阅实验的受信Python入口', '### Other stop'),
            original.replace('### 订阅实验的受信Python入口', '### 订阅实验的受信Python入口\n\n### 订阅实验的受信Python入口'),
        )
        for variant in variants:
            with self.subTest(variant=variant[-60:]):
                source.write_text(variant, encoding='utf-8')
                with self.assertRaises(HarnessError):
                    self.export()
                self.assertFalse(self.output.exists())
        source.write_text(original + '\n```markdown\n## 日常使用\n```\n', encoding='utf-8')
        self.export()  # A fenced example heading is not an ambiguous real section.

    def test_payload_tampering_extra_members_and_malformed_manifests(self):
        self.export()
        current = self.output / 'current.md'
        before = current.read_bytes()
        current.write_bytes(before + b'changed')
        with self.assertRaises(HarnessError):
            dp.check(self.output)
        current.write_bytes(before)
        extra = self.output / 'outside.txt'
        extra.write_text('extra', encoding='utf-8')
        with self.assertRaises(HarnessError):
            dp.check(self.output)
        extra.unlink()
        extra.mkdir()
        with self.assertRaises(HarnessError):
            dp.check(self.output)
        extra.rmdir()
        original = self.manifest()
        variants = [None, [], {}, {**original, 'config': []}, {**original, 'sources': [None, None]},
                    {**original, 'files': {'../outside.txt': {'bytes': 1, 'sha256': '0' * 64}}},
                    {**original, 'generated_at': 'invalid timestamp'}, {**original, 'research_work_scopes': ['unapproved']}]
        for value in variants:
            with self.subTest(value=type(value).__name__):
                self.write_manifest(value)
                with self.assertRaises(HarnessError):
                    dp.check(self.output)
        (self.output / 'manifest.json').write_text('{"format":"a","format":"b"}', encoding='utf-8')
        with self.assertRaises(HarnessError):
            dp.check(self.output)
        self.write_manifest(original)
        self.assertEqual('valid', dp.check(self.output)['integrity'])

    def test_existing_output_and_raced_output_are_never_overwritten(self):
        self.output.mkdir()
        with self.assertRaises(HarnessError):
            self.export()
        self.assertEqual([], list(self.output.iterdir()))
        self.output.rmdir()
        publish = dp._publish_new
        def race(staging, output):
            output.mkdir()
            return publish(staging, output)
        with patch.object(dp, '_publish_new', side_effect=race):
            with self.assertRaises((HarnessError, FileExistsError)):
                self.export()
        self.assertEqual([], list(self.output.iterdir()))
        self.assertFalse(any(p.name.startswith('.dot-package-pending-') for p in self.output.parent.iterdir()))

    def test_source_change_during_generation_does_not_publish_partial_package(self):
        source = self.root / 'harness/README.md'
        render = dp._current
        def change(config):
            source.write_text(source.read_text(encoding='utf-8') + '\nConcurrent change\n', encoding='utf-8')
            return render(config)
        with patch.object(dp, '_current', side_effect=change):
            with self.assertRaises(HarnessError) as raised:
                self.export()
        self.assertEqual('DOT_PACKAGE_SOURCE_CHANGED', raised.exception.code)
        self.assertFalse(self.output.exists())
        self.assertFalse(any(p.name.startswith('.dot-package-pending-') for p in self.output.parent.iterdir()))

    @unittest.skipIf(os.name == 'nt', 'Symlink fixture needs the normal WSL filesystem')
    def test_symlink_sources_output_parents_and_package_members_refused(self):
        source = self.root / 'harness/README.md'
        saved = source.with_suffix('.saved')
        source.rename(saved)
        source.symlink_to(saved)
        with self.assertRaises(HarnessError):
            self.export()
        source.unlink()
        saved.rename(source)
        linked_parent = self.root / 'linked-output-parent'
        linked_parent.symlink_to(self.output.parent, target_is_directory=True)
        with self.assertRaises(HarnessError):
            dp.export(self.root, linked_parent / 'new', **self.arguments)
        self.export()
        member = self.output / 'references/operations-guide.md'
        member.unlink()
        member.symlink_to(source)
        with self.assertRaises(HarnessError):
            dp.check(self.output)

    def test_cli_check_never_opens_state_and_read_only_export_refuses(self):
        self.export()
        store = self.root / 'nonexistent-store'
        prefix = ['--root', str(self.root), '--store', str(store), '--read-only']
        with patch.object(cli, 'State', side_effect=AssertionError('State must not open')):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(prefix + ['dot-package', 'check', '--package', str(self.output), '--check-sources'])
            self.assertEqual(0, code, err.getvalue())
            self.assertEqual('current', json.loads(out.getvalue())['source_freshness']['status'])
            target = self.output.parent / 'read-only-output'
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.main(prefix + ['dot-package', 'export', '--output', str(target),
                    '--source-thread', self.arguments['source_thread'], '--authorization-reference', self.arguments['authorization_reference'],
                    '--cloud-directory', self.arguments['cloud_directory']])
            self.assertEqual(2, code)
            self.assertIn('Read-only mode refuses Dot package export', err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())
            self.assertFalse(target.exists())
        self.assertFalse(store.exists())


if __name__ == '__main__':
    unittest.main()
