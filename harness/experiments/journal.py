"""Append-only projections in the existing business observations journal."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from ..common import encoded, require, local_path, now, digest

SCHEMA = 'htam.experiment/v1'


def hashed(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', value), 'Invalid experiment/arm/run identity')
    return value


def safe_path(root, value):
    raw = Path(value) if Path(value).is_absolute() else root / value
    require(not any(p.is_symlink() for p in [raw, *raw.parents]), 'Symlink in experiment path', 'PATH_ESCAPE')
    return local_path(root, str(raw))


def human(state, work, supplied, expected_hash):
    require(isinstance(supplied, dict) and supplied.get('bound_sha256') == expected_hash, 'Approval must bind exact current hash', 'APPROVAL_HASH')
    record = state._input_scope(supplied['input_id'], work)
    assessment = state.input_assessment(record['id'])
    require(assessment['kind'] == 'human_direct' and assessment['status'] == 'reviewed', 'Actually reviewed human input required', 'PROVENANCE_UNVERIFIED')
    require(supplied.get('input_sha256') == record['input_sha256'] == assessment['input_sha256'] and
        supplied.get('assessment_revision') == assessment['revision'] and supplied.get('scope_revision') == record['scope_revision'],
        'Approval source/assessment/scope changed', 'REVISION_CONFLICT')
    quote = supplied.get('quote')
    require(isinstance(quote, str) and quote.strip() and quote in record['prompt'], 'Quote must occur in the actual original human response')
    require(isinstance(supplied.get('reviewer'), str) and supplied['reviewer'].strip() and
        isinstance(supplied.get('review_reason'), str) and supplied['review_reason'].strip(),
        'Approval needs an explicit semantic reviewer and proposal/response correspondence reason', 'PROPOSAL_UNRESOLVED')
    ref = supplied.get('proposal_ref')
    require(isinstance(ref, dict), 'Exact presented proposal reference required', 'PROPOSAL_UNRESOLVED')
    if 'event_seq' in ref:
        row = state.db.execute('SELECT * FROM events WHERE seq=?', (ref['event_seq'],)).fetchone()
        require(row is not None and row['kind'] == 'proposal_presented' and row['work_id'] == work,
            'Original proposal display event unavailable', 'PROPOSAL_UNRESOLVED')
        displayed = json.loads(row['payload'])
        require(displayed.get('session') == record['session'] and displayed.get('context_ref') and displayed.get('bound_sha256') == expected_hash,
            'Displayed proposal version/session differs from this reviewed response', 'APPROVAL_HASH')
        proposal_identity = {'event_seq': row['seq'], 'source': row['source'], 'at': row['at'], 'bound_sha256': expected_hash}
    else:
        path = safe_path(state.root, ref.get('path', ''))
        require(path.is_file() and ref.get('sha256') == digest(path) and ref.get('context_ref') and ref.get('session') == record['session'],
            'Exact displayed proposal path/hash/session/context is unresolved', 'PROPOSAL_UNRESOLVED')
        displayed = json.loads(path.read_text())
        require(displayed.get('sha256', displayed.get('bound_sha256')) == expected_hash,
            'Presented proposal was revised after the response', 'APPROVAL_HASH')
        proposal_identity = {**ref, 'bound_sha256': expected_hash}
    return {**deepcopy(supplied), 'proposal_identity': proposal_identity, 'session': record['session'],
        'source_basis': 'Actual B02 human input + exact displayed proposal + explicit contextual semantic review; no additional human hash-copy step'}


class Journal:
    def _records(self, name):
        values = []
        for row in self.state.db.execute("SELECT id,body,source,at FROM observations WHERE asset=? AND kind='note' ORDER BY id", ('experiment:' + identifier(name),)):
            payload = json.loads(row['body']).get('experiment', {})
            if payload.get('schema') == SCHEMA:
                values.append({**payload, 'observation_id': row['id'], 'source': row['source'], 'at': row['at']})
        return values

    def get(self, name):
        rows = self._records(name)
        require(rows, 'Unknown experiment: ' + name, 'EXPERIMENT_UNKNOWN')
        contract = next(r['data'] for r in rows if r['kind'] == 'frozen')
        candidates, runs, generations = {}, {}, {}
        for row in rows:
            data, kind = row['data'], row['kind']
            if kind == 'candidate':
                candidates[data['arm']] = data
            elif kind == 'run_intent':
                runs[data['run_id']] = {**data, 'status': 'unknown', 'intent_observation': row['observation_id']}
            elif kind in ('run_result', 'run_reconciled'):
                runs[data['run_id']].update(data)
            elif kind == 'evaluated':
                runs[data['run_id']]['evaluation'] = data
            elif kind == 'semantic_review':
                runs[data['run_id']]['semantic_review'] = data
            elif kind == 'generated':
                generations[data['arm']] = data
        latest = lambda kind: next((r['data'] for r in reversed(rows) if r['kind'] == kind), None)
        end = latest('ended')
        return {'id': name, 'revision': rows[-1]['revision'], 'contract': contract,
            'candidates': candidates, 'runs': runs, 'generations': generations,
            'generation_context': latest('generation_context'),
            'history_release': latest('history_released'),
            'lock': latest('locked'), 'end': end, 'publication_plan': latest('publication_plan'),
            'publication_approval': latest('publication_approved'), 'publication': latest('publication_complete'),
            'rollback': latest('rollback_complete'), 'history': rows,
            'stage': 'rolled_back' if latest('rollback_complete') else 'ended' if end else 'published' if latest('publication_complete') else 'locked' if latest('locked') else 'frozen'}

    def _append(self, name, expected, kind, data, source):
        require(not self._recovery_only or kind == 'run_reconciled', 'Recovery-only host can only collect an exact original receipt', 'EXECUTOR_UNVERIFIED')
        require(not self.state.readonly, 'Read-only experiment cannot mutate state')
        require(isinstance(source, str) and source.strip(), 'Experiment event source required')
        with self.state.transaction():
            rows = self._records(name)
            actual = rows[-1]['revision'] if rows else 0
            require(type(expected) is int and expected == actual, 'Stale experiment revision', 'REVISION_CONFLICT')
            payload = {'schema': SCHEMA, 'id': name, 'revision': actual + 1, 'kind': kind, 'data': deepcopy(data)}
            observation = self.state.observation('experiment:' + name, 'note', {'experiment': payload}, source)
            return {**payload, 'observation_id': observation}

    def _auto(self, name, kind, data, source):
        with self.state.transaction():
            return self._append(name, self.get(name)['revision'], kind, data, source)

    def _write_json(self, path, data):
        require(not self._recovery_only, 'Recovery-only host cannot write new experiment proposals/evaluations', 'EXECUTOR_UNVERIFIED')
        require(not self.state.readonly, 'Read-only experiment cannot write files')
        path = safe_path(self.state.root, str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        return str(path.relative_to(self.state.root))

    def _active(self, view):
        self._verify_executor()
        require(not view['end'] and not view['publication'], 'Experiment no longer accepts candidate runs', 'EXPERIMENT_CLOSED')
        require(self.state.get(view['contract']['spec']['work'])['request_revision'] == view['contract']['spec']['work_request_revision'],
            'User work request changed after frozen U/A', 'IMMUTABLE_CONTRACT')
        if view['contract']['approval'].get('mode') == 'delegated_construction':
            from .construction import construction_authority
            construction_authority(self.state, view['contract']['approval'], view['contract']['sha256'], self.executor.identity(),
                subject_kind='frozen_contract', lab_root=self.directory, contract=view['contract'])
        else:
            human(self.state, view['contract']['spec']['work'], view['contract']['approval'], view['contract']['sha256'])

    def _dir(self, name):
        rows = self._records(name)
        if rows:
            recorded = next(r['data']['lab_root'] for r in rows if r['kind'] == 'frozen')
            require(recorded == str(self.directory.relative_to(self.state.root)), 'Use the recorded exact experiment lab root', 'LAB_ROOT_MISMATCH')
        return self.directory / identifier(name)
