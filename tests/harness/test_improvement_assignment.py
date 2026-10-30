"""Maintenance responsibility is an event relation, not another task or review."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from harness.cli import main
from harness.common import HarnessError, digest, validation_scope
from harness.context import hook_context, improvement_items, render_context
from harness.improvements import assign_maintenance, maintenance_assignment, query_improvements, record_findings
from harness.native_entry import NativeEntry
from harness.state import State


class ImprovementAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self.state = State(self.store, self.root)
        for work in ('research', 'maintenance', 'replacement', 'unrelated'):
            self.state.update(work, 0, {'title': work, 'request': 'Existing authorized work',
                'request_source': 'fixture human request', 'objective': 'Preserve each existing purpose',
                'status': 'active'}, 'fixture')
        for work in ('research', 'maintenance', 'replacement'):
            self.state.bind(work + '-main', work, 'main', 'fixture')
        (self.root / 'result.md').write_text('Actual unchanged scientific package')
        self.state.create_task('science-task', 'research', self.state.get('research')['revision'],
            {'purpose': 'Scientific analysis', 'expected': 'The original return'}, 'fixture')
        self.entry = NativeEntry(self.state)
        self.entry.receive({'id': 'native', 'work': 'research', 'task': 'science-task',
            'expected': self.state.get('research')['revision'], 'review_owner': 'research-main',
            'source': 'fixture actual return read', 'receipt': {'id': 'native-read',
                'reference': 'fixture native receipt', 'observed_at': '2026-10-26T08:00:00Z',
                'raw': {'threadId': 'research-worker', 'latestTurn': {'id': 'actual-turn', 'status': 'completed'}}}})
        item = self.entry.get('native')
        self.entry.finish({'id': 'native', 'expected': item['revision'], 'executor_session': 'research-worker',
            'turn_id': 'actual-turn', 'result': {'answer': 'Actual bounded scientific result', 'artifacts': ['result.md'], 'remaining': []},
            'source': 'fixture actual return'})
        self.candidate = self.finding('first', severity='action_blocking')

    def tearDown(self):
        if self.state:
            self.state.close()
        self.temp.cleanup()

    def finding(self, identifier, **extra):
        return record_findings(self.state, self.entry.get('native'), [{'id': identifier,
            'problem': 'Shared tooling defect ' + identifier, 'impact': 'Scientific result requires the documented boundary',
            'evidence': ['result.md'], **extra}], 'fixture declared finding', 'research-worker')[0]['event']

    def assign(self, target='maintenance', expected=0, candidate=None, reason='Explicit technical maintenance handoff'):
        return assign_maintenance(self.state, 'research', candidate or self.candidate, expected,
            {'maintenance_work': target, 'reason': reason}, 'fixture approved handoff')

    def triage(self, candidate, disposition):
        return self.state.triage('research', candidate, {'disposition': disposition,
            'reason': 'Actual existing disposition', 'next_action': 'Use the existing scoped response',
            'evidence': ['result.md']}, 'fixture existing triage')

    def business_records(self):
        # All non-event state, including tasks, bindings, native records, review
        # queues, observations, dispatches and policies, must stay byte-for-byte.
        tables = [row[0] for row in self.state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in ('events', 'event_metadata')]
        return {table: sorted(tuple(row) for row in self.state.db.execute('SELECT * FROM "' + table + '"'))
            for table in tables}

    def test_assignment_transfer_withdrawal_and_current_exact_replay(self):
        before = self.business_records()
        original = deepcopy(improvement_items(self.state, 'research')[0])
        candidate_count = self.state.db.execute("SELECT count(*) FROM events WHERE kind='improvement_candidate'").fetchone()[0]
        first = self.assign()
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['origin_work'], 'research')
        self.assertEqual(first['main_bindings'][0]['session'], 'maintenance-main')
        changes = self.state.db.total_changes
        self.assertTrue(self.assign()['duplicate'])
        self.assertEqual(self.state.db.total_changes, changes)
        for work in ('research', 'maintenance'):
            current = improvement_items(self.state, work)[0]
            self.assertEqual(current['event'], self.candidate)
            self.assertEqual(current['work'], 'research')
            for key in ('task', 'native_entry', 'return_event', 'return_sha256', 'review_owner', 'original_origin', 'status'):
                self.assertEqual(current[key], original[key])
        self.assertEqual(improvement_items(self.state, 'unrelated'), [])
        self.assign('replacement', 1)
        self.assertEqual(improvement_items(self.state, 'maintenance'), [])
        self.assertEqual(improvement_items(self.state, 'replacement')[0]['event'], self.candidate)
        with self.assertRaisesRegex(HarnessError, 'Stale'):
            self.assign()
        withdrawn = self.assign(None, 2)
        self.assertEqual(withdrawn['revision'], 3)
        self.assertEqual(improvement_items(self.state, 'replacement'), [])
        self.assertEqual(improvement_items(self.state, 'research')[0]['status'], 'awaiting_triage')
        with self.assertRaisesRegex(HarnessError, 'Stale'):
            self.assign('replacement', 1)
        self.assertEqual(self.business_records(), before)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE kind='improvement_candidate'").fetchone()[0], candidate_count)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE kind='improvement_supplement'").fetchone()[0], 0)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE kind='improvement_assignment'").fetchone()[0], 3)

    def test_handoff_keeps_every_existing_disposition_and_original_review(self):
        candidates = {'awaiting_triage': self.candidate}
        for disposition in ('addressed', 'deferred', 'no_change', 'monitoring'):
            candidates[disposition] = self.finding(disposition)
            self.triage(candidates[disposition], disposition)
        before = self.business_records()
        for event in candidates.values():
            self.assign(candidate=event)
        for work in ('research', 'maintenance'):
            defaults = improvement_items(self.state, work)
            self.assertEqual({item['status'] for item in defaults}, {'awaiting_triage', 'monitoring'})
            explicit = query_improvements(self.state, work, include_resolved=True)
            self.assertEqual({item['status'] for item in explicit['items']}, set(candidates))
            self.assertTrue(all(item['work'] == 'research' and item['review_owner'] == 'research-main'
                for item in explicit['items']))
        self.assertEqual(self.business_records(), before)
        # The authorized maintainer still uses the original work and event.
        self.triage(candidates['monitoring'], 'addressed')
        self.assertEqual(len(improvement_items(self.state, 'maintenance')), 1)
        with self.assertRaisesRegex(HarnessError, 'not in this work'):
            self.state.triage('maintenance', self.candidate, {'disposition': 'addressed',
                'reason': 'wrong work', 'next_action': 'wrong work'}, 'fixture')

    def test_later_evidence_retains_assignment_and_only_reopens_existing_attention(self):
        self.triage(self.candidate, 'addressed')
        assignment = self.assign()
        self.assertEqual(improvement_items(self.state, 'maintenance'), [])
        before = self.business_records()
        (self.root / 'new.md').write_text('New actual finding evidence')
        added = record_findings(self.state, self.entry.get('native'), [{'candidate_event': self.candidate,
            'evidence': ['new.md'], 'impact': 'New evidence changes the affected result interpretation'}],
            'fixture new evidence', 'actual-writer')[0]
        for work in ('research', 'maintenance'):
            current = improvement_items(self.state, work)[0]
            self.assertEqual(current['status'], 'awaiting_triage')
            self.assertEqual(current['previous_disposition'], 'addressed')
            self.assertEqual(current['attention'], 'new_finding_evidence')
            self.assertEqual(current['maintenance_assignment']['event'], assignment['event'])
            self.assertEqual(current['latest_event'], added['observation_event'])
            self.assertEqual(current['review_owner'], 'research-main')
            self.assertEqual(current['original_origin']['writer_session'], 'research-worker')
        self.assertEqual(self.business_records(), before)
        self.assertEqual(maintenance_assignment(self.state, self.candidate)['revision'], 1)

    def test_invalid_assignment_and_stale_versions_do_not_write(self):
        before = self.state.db.total_changes
        for work, event, expected, body in (
            ('maintenance', self.candidate, 0, {'maintenance_work': 'maintenance', 'reason': 'wrong origin'}),
            ('research', self.candidate, 0, {'maintenance_work': 'missing', 'reason': 'unknown destination'}),
            ('research', self.candidate, 0, {'maintenance_work': '', 'reason': 'empty destination'}),
            ('research', self.candidate, 0, {'maintenance_work': 'maintenance', 'reason': 'not a triage', 'disposition': 'monitoring'}),
            ('research', self.candidate, 1, {'maintenance_work': 'maintenance', 'reason': 'stale'}),
        ):
            with self.subTest(body=body), self.assertRaises(HarnessError):
                assign_maintenance(self.state, work, event, expected, body, 'fixture')
            self.assertEqual(self.state.db.total_changes, before)
        self.assertTrue(self.assign(None)['duplicate'])
        self.assertEqual(self.state.db.total_changes, before)
        self.assign()
        current = self.state.db.total_changes
        self.assertTrue(self.assign(expected=1)['duplicate'])
        self.assertEqual(self.state.db.total_changes, current)

    def test_bounded_explicit_query_reads_resolved_and_prior_assignments_without_duplicates(self):
        for identifier in range(5):
            self.assign(candidate=self.finding(str(identifier)))
        self.assign()
        first = query_improvements(self.state, 'maintenance', relation='maintenance', limit=2)
        second = query_improvements(self.state, 'maintenance', relation='maintenance', limit=2, offset=first['next_offset'])
        third = query_improvements(self.state, 'maintenance', relation='maintenance', limit=2, offset=second['next_offset'])
        found = [item['event'] for page in (first, second, third) for item in page['items']]
        self.assertEqual(len(found), 6)
        self.assertEqual(len(set(found)), 6)
        self.assertEqual(found[0], self.candidate)  # Declared severity outranks recency.
        self.assertIsNone(third['next_offset'])
        self.triage(self.candidate, 'deferred')
        self.assign('replacement', 1)
        self.assign(None, 2)
        self.assertEqual(query_improvements(self.state, 'maintenance', event=self.candidate)['items'], [])
        previous = query_improvements(self.state, 'maintenance', relation='maintenance',
            include_resolved=True, event=self.candidate, history_limit=1)
        self.assertEqual(previous['items'][0]['work'], 'research')
        self.assertEqual(previous['items'][0]['context_relation'], 'previous_maintenance')
        self.assertEqual(previous['items'][0]['status'], 'deferred')
        self.assertEqual(previous['assignment_history']['items'][0]['revision'], 3)
        revisions = []
        for offset in range(3):
            history = query_improvements(self.state, 'research', event=self.candidate, include_resolved=True,
                history_limit=1, history_offset=offset)['assignment_history']
            revisions.extend(item['revision'] for item in history['items'])
        self.assertEqual(revisions, [3, 2, 1])
        self.assertEqual(query_improvements(self.state, 'unrelated', include_resolved=True)['items'], [])
        for options in ({'limit': 51}, {'offset': -1}, {'history_limit': 0}, {'history_offset': -1}):
            with self.subTest(options=options), self.assertRaises(HarnessError):
                query_improvements(self.state, 'research', **options)

    def test_context_uses_live_main_binding_and_reads_without_writing_within_budget(self):
        self.assign()
        self.state.bind('maintenance-main', 'unrelated', 'observer', 'fixture actual rebind', expected=1)
        self.state.bind('current-maintainer', 'maintenance', 'main', 'fixture current main')
        for identifier in range(4):
            self.assign(candidate=self.finding(str(identifier)))
        before = self.state.db.total_changes
        started = time.monotonic()
        with validation_scope(timeout=5):
            for work in ('research', 'maintenance'):
                raw = hook_context(self.state, work + '-main', work=work)
                self.assertLessEqual(len(raw.encode()), 6500)
                compact = json.loads(raw.split('\n', 1)[1])
                item = compact['improvement_candidates'][0]
                self.assertEqual(item['event'], self.candidate)
                self.assertEqual(item['work'], 'research')
                self.assertIn('Scientific result', item['impact'])
                self.assertEqual(item['maintenance_assignment']['main_bindings'][0]['session'], 'current-maintainer')
                self.assertEqual(len(compact['improvement_candidates']) + compact['omitted_improvement_candidates'], 5)
                view = render_context(self.state, work=work)
                self.assertLessEqual(len(json.dumps(view, ensure_ascii=False).encode()), 24000)
                self.assertEqual(view['improvement_candidates'][0]['event'], self.candidate)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.state.db.total_changes, before)
        # Actual CLI observational mode uses a query-only snapshot and changes no
        # fixture file, even when recovering disposed findings and full context.
        self.state.close()
        self.state = None
        files_before = {str(path.relative_to(self.root)): digest(path) for path in self.root.rglob('*') if path.is_file()}
        base = ['--root', str(self.root), '--store', str(self.store), '--read-only']
        for command in (['improvement-query', '--work', 'maintenance', '--include-resolved', '--event', str(self.candidate)],
                        ['context', '--work', 'maintenance'], ['context', '--work', 'research', '--full']):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                result = main(base + command)
            self.assertEqual(result, 0, err.getvalue())
            json.loads(out.getvalue())
        self.assertEqual({str(path.relative_to(self.root)): digest(path) for path in self.root.rglob('*') if path.is_file()}, files_before)

    def test_assigned_priority_issue_survives_oversize_fallback_in_both_contexts(self):
        self.assign()
        self.assign(candidate=self.finding('later-normal'))
        packet = {'mandatory': [], 'selected': [{'text': 'x' * 24000}],
            'context_complete': True, 'required_load': None}
        with patch('harness.context.knowledge_context', return_value=packet):
            for work in ('research', 'maintenance'):
                raw = hook_context(self.state, work + '-main', work=work)
                self.assertLessEqual(len(raw.encode()), 6500)
                data = json.loads(raw.split('\n', 1)[1])
                issue = data['improvement_candidates'][0]
                self.assertEqual(issue['event'], self.candidate)
                self.assertEqual(issue['severity'], 'action_blocking')
                self.assertEqual(issue['work'], 'research')
                self.assertEqual(issue['maintenance_assignment']['maintenance_work'], 'maintenance')
                self.assertIn('Scientific result', issue['impact'])
                self.assertEqual(data['omitted_improvement_candidates'], 1)

    def test_crowded_state_keeps_essential_assigned_issue_when_action_card_does_not_fit(self):
        # Equivalent to the actual 6397-byte recovery: retained native review
        # metadata fits, the action card is popped, and no global fallback runs.
        # All business records and the payload below are isolated fixtures.
        self.triage(self.candidate, 'monitoring')
        assignment = self.assign()
        for identifier in range(7):
            self.assign(candidate=self.finding('later-' + str(identifier)))
        ordinary = {'attention': [], 'attention_count': 14, 'omitted_attention': 14,
            'pending_review_count': 1, 'omitted_reviews': 0, 'pending_reviews': [{
                'task_id': 'science-task', 'owner': 'research-main', 'status': 'revise', 'revision': None,
                'at': '2026-10-26T08:00:00Z'}],
            'entry': {'command': 'native-entry recover', 'request': {'work': 'research'}},
            'coverage': 'R' * 3900, 'details_required': True}
        knowledge = {'mandatory': [], 'selected': [], 'context_complete': True, 'required_load': None}
        before = self.state.db.total_changes
        records = self.business_records()
        with patch('harness.context.ordinary_context', side_effect=lambda *a, **kw: deepcopy(ordinary)), \
             patch('harness.context.knowledge_context', return_value=knowledge):
            raw = hook_context(self.state, 'research-main', task_limit=0)
        self.assertLessEqual(len(raw.encode()), 6500)
        data = json.loads(raw.split('\n', 1)[1])
        self.assertEqual(data['id'], 'research')  # The surrounding state remains.
        self.assertEqual(len(data['improvement_candidates']), 1)
        self.assertEqual(data['omitted_improvement_candidates'], 7)
        self.assertTrue(data['details_required'])
        issue = data['improvement_candidates'][0]
        self.assertEqual(issue['event'], self.candidate)
        self.assertEqual(issue['work'], 'research')
        self.assertEqual(issue['status'], 'monitoring')
        self.assertEqual(issue['severity'], 'action_blocking')
        self.assertEqual(issue['maintenance_assignment'], {'event': assignment['event'], 'revision': 1,
            'maintenance_work': 'maintenance'})
        self.assertIn('Scientific result', issue['impact'])
        self.assertEqual(issue['entry'], {'command': 'improvement-query', 'work': 'research',
            'event': self.candidate, 'include_resolved': True})
        self.assertEqual(query_improvements(self.state, 'research', event=issue['event'])['items'][0]['event'], self.candidate)
        self.assertEqual(self.state.db.total_changes, before)
        self.assertEqual(self.business_records(), records)

    def test_large_assignment_identity_does_not_evict_the_priority_issue(self):
        target = 'maintenance-' + 'x' * 8000
        self.state.update(target, 0, {'title': 'Existing work with a large identifier',
            'request': 'Existing purpose', 'request_source': 'fixture', 'objective': 'Existing purpose',
            'status': 'active'}, 'fixture')
        self.state.bind('main-' + 'x' * 8000, target, 'main', 'fixture')
        self.assign(target)
        for work in ('research', target):
            raw = hook_context(self.state, None, work=work)
            self.assertLessEqual(len(raw.encode()), 6500)
            issue = json.loads(raw.split('\n', 1)[1])['improvement_candidates'][0]
            self.assertEqual(issue['event'], self.candidate)
            self.assertEqual(issue['maintenance_assignment']['revision'], 1)
            self.assertTrue(issue['maintenance_assignment']['details_omitted'])
        explicit = query_improvements(self.state, 'research', event=self.candidate)
        self.assertEqual(explicit['assignment_history']['items'][0]['maintenance_work'], target)

    def test_cli_mutation_only_appends_assignment_and_rejects_read_only(self):
        request = self.root / 'assignment.json'
        request.write_text(json.dumps({'maintenance_work': 'maintenance', 'reason': 'Approved technical handoff'}))
        arguments = ['--root', str(self.root), '--store', str(self.store), 'improvement-assign',
            '--work', 'research', '--event', str(self.candidate), '--expected', '0', '--file', str(request),
            '--source', 'fixture actual authorization']
        before = self.business_records()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(['--read-only'] + arguments), 2)
            self.assertEqual(main(arguments), 0)
            changes = self.state.db.total_changes
            self.assertEqual(main(arguments), 0)
        self.assertEqual(self.state.db.total_changes, changes)
        self.assertEqual(self.business_records(), before)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM events WHERE kind='improvement_assignment'").fetchone()[0], 1)
        self.assertFalse((self.store / 'state-snapshot.json').exists())
        self.assertFalse((self.store / 'current.md').exists())


if __name__ == '__main__':
    unittest.main()
