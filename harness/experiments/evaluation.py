"""Fixed deterministic E, sourced independent review and pre-holdout lock."""
from copy import deepcopy
import json
import math
from pathlib import Path
import time
import uuid

from ..common import digest, require, now, HarnessError
from .journal import hashed, human, safe_path


def pointer(value, key):
    for part in key.strip('/').split('/') if key else []:
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def aggregate_cost(cost):
    data = {k: deepcopy(cost[k]) for k in ('runs', 'wall_seconds', 'model_calls', 'known_provider_model_calls',
        'budget_unit', 'budget_units_used', 'per_arm')}
    data['unknown_cost_count'] = len(cost['unknown_cost_records'])
    return data


def evaluation_groups(view):
    groups = {}
    for run in view['runs'].values():
        group = groups.setdefault((run['split'], run['arm']), {'split': run['split'], 'arm': run['arm'], 'runs': 0,
            'evaluated': 0, 'semantic_reviewed': 0, 'score_total': 0, 'serious_errors': 0})
        group['runs'] += 1
        if run.get('evaluation'):
            group['evaluated'] += 1; group['score_total'] += run['evaluation']['score']; group['serious_errors'] += run['evaluation']['serious_errors']
        group['semantic_reviewed'] += bool(run.get('semantic_review'))
    return list(groups.values())


class Evaluation:
    def _benefit(self, name, rows, arm, baseline):
        view = self.get(name); criterion = view['contract']['spec']['E']['improvement']
        selected = [r for r in rows if r['arm'] == arm]
        bases = {r['case']: r for r in rows if r['arm'] == baseline}
        quality = all(r['status'] == 'completed' and r['evaluation']['serious_errors'] == 0 and
            r['semantic_review']['review']['verdict'] == 'pass' and r['evaluation']['score'] >= bases[r['case']]['evaluation']['score'] and
            all(not b['passed'] or next(c['passed'] for c in r['evaluation']['checks'] if c['id'] == b['id'])
                for b in bases[r['case']]['evaluation']['checks']) for r in selected)
        kind = criterion['kind']; totals, sources = {}, []
        for side in (arm, baseline):
            items = [r for r in rows if r['arm'] == side]
            if kind == 'quality':
                totals[side] = sum(r['evaluation']['score'] for r in items)
                continue
            values = []
            for item in items:
                run = view['runs'][item['run_id']]
                value = run.get('receipt', {}).get('elapsed_seconds') if kind == 'execution_wall_seconds' else run.get('cost', {}).get('money')
                currency = run.get('receipt', {}).get('cost', {}).get('currency')
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or kind == 'money' and currency != criterion['currency']:
                    return {'quality_nonregressed': quality, 'improved': False, 'criterion': criterion,
                        'state': 'unknown_benefit', 'reason': 'Required observed cost/currency is unknown; no improvement inferred'}
                values.append(value); sources.append({'run_id': run['run_id'], 'receipt_sha256': run['receipt_sha256']})
            if kind == 'money':
                # Full-chain money requires every charged stage to have the
                # same known unit. Unknown authoring/human costs are never zero.
                return {'quality_nonregressed': quality, 'improved': False, 'criterion': criterion,
                    'state': 'unknown_benefit', 'reason': 'Full-chain monetary coverage is not verified by the current offline executor'}
            totals[side] = sum(values)
        if kind == 'quality':
            delta = totals[arm] - totals[baseline]; improved = delta > 0
        else:
            delta = totals[baseline] - totals[arm]
            improved = delta > 0 and delta >= criterion.get('min_absolute', 0) and (totals[baseline] > 0 and delta / totals[baseline] >= criterion.get('min_relative', 0))
        return {'quality_nonregressed': quality, 'improved': quality and improved, 'criterion': criterion,
            'state': 'observed', 'totals': totals, 'difference': delta, 'sources': sources,
            'scope': 'Actual executor segment only; generation, human, maintenance and whole-chain totals remain separately reported' if kind == 'execution_wall_seconds' else 'Fixed quality checks'}

    def evaluate(self, name, expected, run_id, source):
        start = time.monotonic(); view = self.get(name); run = view['runs'][run_id]
        require(run['status'] in ('completed', 'failed', 'not_run') and not run.get('generation'), 'Actual terminal task execution required', 'NOT_RUN')
        if run.get('evaluation'):
            return run['evaluation']
        spec = view['contract']['spec']; case = next(c for c in spec['cases'] if c['id'] == run['case'])
        require(self.executor.identity() == spec['environment'], 'Frozen evaluation/runtime build changed; use explicit E reassessment', 'EVALUATION_CHANGED')
        answer_path = self._dir(name) / 'cases' / case['id'] / 'answer.json'
        require(digest(answer_path) == case['answer_sha256'], 'Frozen E answer changed', 'EVALUATION_CHANGED')
        answer = json.loads(answer_path.read_text()); checks = []
        for check in spec['E']['checks']:
            passed, issue = False, None
            try:
                require(run['status'] == 'completed', 'Execution did not complete')
                artifact = next(a for a in run['receipt']['artifacts'] if a['path'] == check['artifact'])
                path = Path(run['receipt']['run_dir']) / 'result/artifacts' / check['artifact']
                require(digest(path) == artifact['sha256'], 'Actual output changed')
                actual = pointer(json.loads(path.read_text()), check.get('pointer', ''))
                target = pointer(answer, check.get('answer_pointer', check.get('pointer', '')))
                if check['kind'] == 'equals':
                    passed = type(actual) is type(target) and actual == target
                elif check['kind'] == 'contains':
                    passed = isinstance(actual, list) and isinstance(target, list) and all(v in actual for v in target)
                else:
                    passed = type(actual) in (int, float) and type(target) in (int, float) and math.isfinite(actual) and math.isfinite(target) and abs(actual - target) <= check.get('tolerance', 0)
                issue = None if passed else 'Fixed E comparison failed'
            except (KeyError, StopIteration, TypeError, ValueError, OSError, RuntimeError, HarnessError) as error:
                issue = type(error).__name__ + ': ' + str(error)
            checks.append({'id': check['id'], 'passed': bool(passed), 'serious': check['serious'], 'issue': issue})
        data = {'run_id': run_id, 'case': run['case'], 'arm': run['arm'], 'split': run['split'],
            'E_sha256': hashed(spec['E']), 'receipt_sha256': run.get('receipt_sha256'), 'checks': checks,
            'serious_errors': sum(not c['passed'] and c['serious'] for c in checks),
            'score': sum(c['passed'] for c in checks) / len(checks), 'observed_at': now(),
            'semantic_status': 'independent_review_required', 'comparison_scope': 'Frozen deterministic checks only'}
        data['sha256'] = hashed(data)
        # The file is an immutable attempt artifact, outside the SQLite
        # transaction. An aborted attempt remains as evidence; a retry writes
        # another unique file rather than overwriting it or blocking recovery.
        data['path'] = self._write_json(self._dir(name) / 'evaluation' / (run_id + '-' + uuid.uuid4().hex + '.json'), data)
        with self.state.transaction():
            self._append(name, expected, 'evaluated', data, source)
            if case['split'] == 'holdout':
                # Commit final-test feedback and its consumed source family
                # together, before any score becomes visible in the journal.
                self.state.observation('experiment-contamination', 'note', {'experiment': name,
                    'families': [case['family']], 'input_hashes': [case['input_fingerprint']],
                    'reason': 'Final test evaluated; further optimization needs a new final source'}, source)
        self._auto(name, 'cost', {'category': 'evaluation', 'arm': run['budget_arm'], 'wall_seconds': time.monotonic() - start,
            'model_calls': 0, 'tokens': None, 'money': None, 'source_ref': data['path']}, 'Actual deterministic evaluation')
        return data

    def semantic_review(self, name, expected, run_id, review, provenance, source):
        view = self.get(name); run = view['runs'][run_id]; spec = view['contract']['spec']
        require(run.get('evaluation') and review.get('evaluation_sha256') == run['evaluation']['sha256'], 'Review must bind exact actual evaluation')
        require(review.get('verdict') in ('pass', 'fail', 'unknown') and isinstance(review.get('findings'), list) and
            review.get('scope') and review.get('limitations') is not None, 'Semantic scope/verdict/findings/limitations required')
        if provenance.get('kind') == 'human_input':
            identity = human(self.state, spec['work'], provenance['approval'], hashed(review))
            session = identity['session']
        else:
            require(provenance.get('kind') == 'agent_result', 'Use an actual reviewed human input or sourced independent agent result')
            task = self.state.task(provenance['task_id'])
            require(task['work_id'] == spec['work'], 'Reviewer task belongs to another work')
            producers = {task['body'].get('executor_session'), task['body'].get('producer')} - {None, ''}
            producers.update(r[0] for r in self.state.db.execute('SELECT target FROM dispatches WHERE task_id=?', (task['id'],)))
            session = provenance['session']
            require(session in producers, 'Reviewer session must match the original recorded review-task executor', 'REVIEW_IDENTITY')
            row = self.state.db.execute('SELECT * FROM events WHERE seq=?', (provenance['event_seq'],)).fetchone()
            require(row is not None and row['kind'] == 'task_returned', 'Original agent return event required', 'REVIEW_IDENTITY')
            event = json.loads(row['payload']); original = event.get('review', {})
            path = safe_path(self.state.root, provenance['path'])
            require(event.get('task') == task['id'] and provenance['path'] in original.get('artifacts', []) and
                provenance.get('sha256') == digest(path) and provenance['sha256'] in original.get('answer', '') and
                json.loads(path.read_text()) == review, 'Review bytes must bind the original returned report/hash', 'REVIEW_IDENTITY')
            identity = {**deepcopy(provenance), 'event_source': row['source'], 'event_at': row['at'],
                'basis': 'Recorded independent task executor and exact original return event/report; existing local source trust, not account authentication'}
        require(session not in spec['producer_sessions'], 'Known producer cannot claim independent semantic review', 'REVIEW_IDENTITY')
        data = {'run_id': run_id, 'review': review, 'identity': identity, 'independence': 'different_recorded_session_with_source'}
        self._append(name, expected, 'semantic_review', data, source)
        return data

    def _matrix(self, name, arms, splits):
        view = self.get(name); rows = []
        for case in view['contract']['spec']['cases']:
            if case['split'] not in splits:
                continue
            for arm in arms:
                results = [r for r in view['runs'].values() if r['case'] == case['id'] and r['arm'] == arm and not r['generation']]
                # Retrying a failed test is not an opportunity to erase it.
                require(len(results) == 1, 'Exactly one frozen run per arm/case required, including failures', 'EVALUATION_INCOMPLETE')
                run = results[0]
                require(run.get('evaluation') and run.get('semantic_review'), 'Independent evaluation/review missing', 'EVALUATION_INCOMPLETE')
                rows.append({'run_id': run['run_id'], 'arm': arm, 'case': case['id'], 'split': case['split'],
                    'status': run['status'], 'evaluation': run['evaluation'], 'semantic_review': run['semantic_review']})
        return rows

    def lock(self, name, expected, arm, reason, source):
        view = self.get(name); self._active(view)
        require(not view['lock'] and arm in view['candidates'] and reason, 'A current candidate and selection reason required')
        self._method(name, view, arm, generated=False)
        replaced = {c['replaces'] for c in view['candidates'].values() if c.get('replaces')}
        active = [a for a in view['candidates'] if a not in replaced]
        require(arm in active, 'Replaced candidate cannot be locked', 'CANDIDATE_INACTIVE')
        rows = self._matrix(name, ['H0', *active], ['dev', 'select'])
        benefit = self._benefit(name, rows, arm, 'H0')
        if not benefit['improved']:
            self._append(name, expected, 'ended', {'reason': reason, 'outcome': 'rejected' if not benefit['quality_nonregressed'] else 'no_gain',
                'benefit': benefit, 'matrix_sha256': hashed(rows)}, source)
            return self.get(name)
        self._append(name, expected, 'locked', {'arm': arm, 'candidate_sha256': view['candidates'][arm]['sha256'],
            'matrix_sha256': hashed(rows), 'benefit': benefit, 'reason': reason, 'scope': view['contract']['spec'].get('applies', {})}, source)
        return self.get(name)

    def final_assessment(self, name):
        view = self.get(name); require(view['lock'], 'Lock a candidate before final assessment')
        arm = view['lock']['arm']; rows = self._matrix(name, ['H0', arm, 'H0_equal_budget'], ['holdout'])
        benefits = [self._benefit(name, rows, arm, baseline) for baseline in ('H0', 'H0_equal_budget')]
        passed = all(b['improved'] for b in benefits)
        return {'passed': passed, 'benefits': benefits, 'matrix': rows, 'sha256': hashed(rows),
            'outcome': 'eligible_for_scoped_proposal' if passed else 'holdout_failed_or_no_gain',
            'cost': self.budget(name), 'resource_comparison': 'Same frozen per-arm total allowance including generation/evaluation; unused old-method allowance is not burned',
            'scope': 'Construction mechanism results do not establish domain method benefit'}

    def finish(self, name, expected, outcome, reason, source):
        require(outcome in ('rejected', 'no_gain', 'holdout_failed', 'budget_exhausted', 'completed', 'observed'), 'Unknown terminal experiment outcome')
        view = self.get(name)
        require(not any(r['status'] == 'unknown' for r in view['runs'].values()), 'Reconcile unknown invocation before closing')
        self._append(name, expected, 'ended', {'outcome': outcome, 'reason': reason, 'cost': self.budget(name)}, source)
        return self.get(name)

    def release_history(self, name, expected, reason, source):
        """Explicit immutable transfer aggregate, including failed/no-gain history."""
        require(not self.state.readonly and not self._recovery_only, 'History release requires a current writable trusted host')
        require(isinstance(reason, str) and reason.strip() and isinstance(source, str) and source.strip(),
            'Historical disclosure reason and source required')
        view = self.get(name)
        require(type(expected) is int and expected == view['revision'], 'Stale history release revision', 'REVISION_CONFLICT')
        require(view['end'] and not any(r['status'] == 'unknown' for r in view['runs'].values()),
            'Only ended history without UNKNOWN can disclose aggregates', 'HISTORY_UNRELEASED')
        if view.get('history_release'):
            require(view['history_release']['reason'] == reason, 'Historical disclosure snapshot is immutable', 'IMMUTABLE_CONTEXT')
            return view['history_release']
        groups = [g for g in evaluation_groups(view) if g['split'] == 'holdout']
        for group in groups:
            group['feedback'] = 'aggregate_only'; group['disclosure'] = 'immutable_ended_history'
        data = {'schema': 'htam.history-aggregate/v1', 'experiment': name, 'contract_sha256': view['contract']['sha256'],
            'source_revision': view['revision'], 'source_journal_sha256': hashed(view['history']),
            'aggregates': groups, 'cost': aggregate_cost(self.budget(name)), 'outcome': view['end']['outcome'],
            'reason': reason, 'coverage': 'Ended experiment aggregate only; no case IDs, answers or per-case judgments'}
        data['sha256'] = hashed(data)
        self._append(name, expected, 'history_released', data, source)
        return data

    def summary(self, name):
        view = self.get(name)
        statuses = {}
        revealed = bool(view['publication_plan'])
        for run in view['runs'].values():
            statuses[run['status']] = statuses.get(run['status'], 0) + 1
        groups = evaluation_groups(view)
        for group in groups:
            if group['split'] == 'holdout' and not revealed:
                group.pop('score_total'); group.pop('serious_errors'); group['feedback'] = 'unrevealed_holdout'
            else:
                group['feedback'] = 'aggregate_only'
        if view.get('history_release'):
            groups = [g for g in groups if g['split'] != 'holdout'] + deepcopy(view['history_release']['aggregates'])
        lock, end = deepcopy(view['lock']), deepcopy(view['end'])
        for record in (lock, end):
            if record and record.get('benefit', {}).get('sources'):
                record['benefit']['source_receipt_count'] = len(record['benefit'].pop('sources'))
        return {'id': name, 'revision': view['revision'], 'stage': view['stage'], 'lock': lock, 'end': end,
            'contract_sha256': view['contract']['sha256'], 'component': view['contract']['spec']['component'],
            'run_count': len(view['runs']), 'run_status_counts': statuses, 'aggregate_evaluation': groups,
            'history_release': {k: view['history_release'][k] for k in ('sha256', 'source_revision', 'source_journal_sha256', 'outcome')} if view.get('history_release') else None,
            'budget': self.budget(name), 'publication': view['publication'], 'rollback': view['rollback'],
            'coverage': 'Actual recorded executions and sourced reviews; hidden answers and per-case selection answers are not included'}
