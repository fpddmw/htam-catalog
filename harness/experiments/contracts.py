"""Frozen U/A/H/M/E, changes, source families, budgets and actual environment."""
from copy import deepcopy
from dataclasses import asdict
import math
import os
from pathlib import Path, PurePosixPath
import platform
import sys

from ..common import digest, require, now
from .journal import hashed, human, safe_path, identifier
from .isolation import Limits
from .isolation.launcher import _snapshot


def tree(path, limits=None):
    limits = limits or Limits()
    path = Path(path).absolute()
    require(path.is_dir() and not any(p.is_symlink() for p in [path, *path.parents]), 'Expected nonsymlink source directory')
    result, total = {}, 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            item = Path(base) / name
            require(not item.is_symlink() and (item.is_file() or item.is_dir()), 'Source link/special file rejected', 'PATH_ESCAPE')
        for name in files:
            item = Path(base) / name
            total += item.stat().st_size
            require(len(result) < limits.source_files and total <= limits.source_bytes, 'Source limits exceeded')
            result[str(item.relative_to(path))] = {'sha256': digest(item), 'bytes': item.stat().st_size}
    return result


def environment():
    here = Path(__file__).resolve().parent
    files = sorted(here.rglob('*.py')) + [Path(sys.executable).resolve(), Path('/usr/bin/bwrap')]
    return {'executor': 'offline-python-bwrap/v1', 'network': False, 'python': platform.python_version(),
        'kernel': platform.release(), 'files': {str(p): digest(p) for p in files},
        'resource_scope': 'wall/namespace/output hard boundaries; CPU/AS/NPROC are not aggregate cgroup budgets'}


class Contracts:
    def prepare(self, supplied):
        self._verify_executor()
        spec = deepcopy(supplied)
        require(isinstance(spec, dict), 'Experiment contract must be an object')
        for key in ('work', 'task', 'component', 'U', 'A', 'H', 'M', 'E', 'baseline_dir', 'targets', 'cases', 'budget', 'triage', 'trigger'):
            require(key in spec, 'Contract missing ' + key)
        require(spec['component'] in ('H', 'M'), 'H/M optimization only; E changes require separately approved fork_evaluation', 'IMMUTABLE_CONTRACT')
        task = self.state.task(spec['task'])
        require(task['work_id'] == spec['work'], 'Experiment task/work mismatch')
        spec['work_request_revision'] = self.state.get(spec['work'])['request_revision']
        producers = {task['body'].get('executor_session'), task['body'].get('producer')} - {None, ''}
        producers.update(r[0] for r in self.state.db.execute('SELECT target FROM dispatches WHERE task_id=?', (spec['task'],)))
        require(producers, 'Actual recorded producer identity is required for independent review')
        for key in ('U', 'A', 'H', 'M'):
            require(isinstance(spec[key], dict) and spec[key].get('version') and spec[key].get('value'), 'Frozen component needs version and content: ' + key)
        for key in ('U', 'A', 'H', 'M', 'E'):
            if spec[key].get('path'):
                path = safe_path(self.state.root, spec[key]['path'])
                require(path.is_file(), 'Frozen component reference unavailable')
                spec[key]['ref_sha256'] = digest(path)
        require(isinstance(spec['E'], dict) and spec['E'].get('version') and isinstance(spec['E'].get('checks'), list) and spec['E']['checks'], 'Frozen E checks/version required')
        improvement = spec['E'].setdefault('improvement', {'kind': 'quality'})
        require(isinstance(improvement, dict) and improvement.get('kind') in ('quality', 'execution_wall_seconds', 'money'), 'Use a finite frozen improvement criterion')
        if improvement['kind'] != 'quality':
            require(type(improvement.get('min_absolute', 0)) in (int, float) and improvement.get('min_absolute', 0) >= 0 and
                type(improvement.get('min_relative', 0)) in (int, float) and 0 <= improvement.get('min_relative', 0) < 1 and
                (improvement.get('min_absolute', 0) > 0 or improvement.get('min_relative', 0) > 0), 'Positive frozen benefit threshold required')
        if improvement['kind'] == 'money':
            require(isinstance(improvement.get('currency'), str) and improvement['currency'], 'Money metric requires one explicit currency')
        for check in spec['E']['checks']:
            require(set(check) <= {'id', 'artifact', 'pointer', 'answer_pointer', 'kind', 'serious', 'tolerance'} and check.get('id') and
                check.get('kind') in ('equals', 'number_close', 'contains') and type(check.get('serious')) is bool, 'Invalid fixed evaluation check')
            relative_file(check['artifact'])
        require(all(spec['triage'].get(k) for k in ('hypothesis', 'expected_gain', 'affected_types', 'precedents', 'reason')), 'Triage must explain value, prior examples and falsifiable mechanism')
        require(isinstance(spec['trigger'], dict) and ('observation_id' in spec['trigger'] or 'event_seq' in spec['trigger']), 'Original result/feedback reference required')
        table, key, value = ('observations', 'id', spec['trigger']['observation_id']) if 'observation_id' in spec['trigger'] else ('events', 'seq', spec['trigger']['event_seq'])
        require(self.state.db.execute('SELECT 1 FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone(), 'Original trigger unavailable')
        limits = Limits(**spec.get('limits', {})); limits.validate(); spec['limits'] = asdict(limits)
        budget = spec['budget']
        for key in ('max_runs', 'max_calls', 'max_candidates', 'max_revisions', 'per_arm_runs'):
            require(type(budget.get(key)) is int and budget[key] >= 0, 'Nonnegative integer budget required: ' + key)
        require(1 <= budget['max_candidates'] <= 2 and 0 <= budget['max_revisions'] <= 1 and budget['max_runs'] > 0, 'Candidate/revision bounds outside contract')
        for key in ('max_wall_seconds', 'per_arm_wall_seconds'):
            require(type(budget.get(key)) in (int, float) and budget[key] > 0, 'Positive wall budget required')
        require(spec['A']['value'].get('network') == self.executor.identity().get('network') and spec['A']['value'].get('production_write') is False,
            'Executor network boundary must match A; candidate production writes remain disabled')
        identity = self.executor.identity(); unit = identity.get('budget_unit', 'provider_model_call')
        require(unit in ('provider_model_call', 'native_turn_attempt'), 'Unsupported frozen budget unit')
        if unit == 'native_turn_attempt':
            require(identity.get('provider_model_calls_observable') is False and identity.get('maximum_budget_units_per_run') == 1 and
                isinstance(identity.get('native_authorization_scope'), str) and bool(identity['native_authorization_scope']) and
                type(identity.get('native_scope_cap')) is int and 1 <= identity['native_scope_cap'] <= 2147483647,
                'Native executor needs the exact bounded construction scope and honest call accounting', 'EXECUTOR_UNVERIFIED')
            overhead = identity.get('maximum_wall_overhead_seconds')
            require(type(overhead) in (int, float) and math.isfinite(overhead) and overhead >= 0,
                'Native executor must declare finite nonnegative wall overhead', 'EXECUTOR_UNVERIFIED')
            require(budget.get('budget_unit') == unit, 'Native contract must explicitly name native_turn_attempt budget units')
        else:
            require(budget.get('budget_unit', unit) == unit, 'Budget unit differs from execution environment')
        max_calls = identity.get('maximum_budget_units_per_run', identity.get('maximum_model_calls_per_run', 0))
        require(type(max_calls) is int and max_calls >= 0, 'Executor must expose a bounded per-run model call ceiling')
        budget.setdefault('per_arm_calls', budget['max_calls'])
        require(type(budget['per_arm_calls']) is int and budget['per_arm_calls'] >= max_calls, 'Per-arm model call budget required')
        baseline = safe_path(self.state.root, spec['baseline_dir']); spec['baseline_dir'] = str(baseline.relative_to(self.state.root))
        baseline_files = tree(baseline, limits)
        relative_file(spec.setdefault('entrypoint', 'main.py'))
        require(spec['entrypoint'] in baseline_files, 'Baseline entrypoint missing')
        require(isinstance(spec['targets'], dict) and spec['targets'], 'Explicit editable files and original targets required')
        targets = {}
        protected = {safe_path(self.state.root, spec[k]['path']) for k in ('U', 'A', 'E') if spec[k].get('path')}
        for name, value in spec['targets'].items():
            relative_file(name)
            target = safe_path(self.state.root, value)
            require(name in baseline_files and target.is_file() and target not in protected, 'Editable target must be an existing method file outside U/A/E')
            require(digest(target) == baseline_files[name]['sha256'], 'Baseline and original target differ', 'CONTENT_CHANGED')
            targets[name] = {'path': str(target.relative_to(self.state.root)), 'before_sha256': digest(target)}
        formal = spec[spec['component']]['value'].get('asset_id')
        if formal:
            from ..assets import AssetRegistry
            _, snapshot, _ = AssetRegistry(self.state)._row(formal, spec[spec['component']]['version'])
            metadata = __import__('json').loads(snapshot['body'])
            matching = [r for r in metadata['provenance'] if r['path'] in {t['path'] for t in targets.values()}]
            require(matching and all(digest(safe_path(self.state.root, r['path'])) == r['sha256'] for r in matching),
                'Formal baseline version does not match its original-entry bytes', 'CONTENT_CHANGED')
        require(isinstance(spec['cases'], list) and spec['cases'], 'Source-family cases required')
        cases, families, duplicate_groups, content_splits, case_ids = [], {}, {}, {}, set()
        for item in spec['cases']:
            case = deepcopy(item); identifier(case['id'])
            require(case['id'] not in case_ids and case.get('split') in ('dev', 'select', 'holdout') and case.get('family') and case.get('source_ref'), 'Case identity/split/family/source required')
            case_ids.add(case['id'])
            for mapping, label in ((families, case['family']), (duplicate_groups, case.get('near_duplicate_group', case['family']))):
                require(label not in mapping or mapping[label] == case['split'], 'Source/near-duplicate family crosses data splits', 'DATA_LEAKAGE')
                mapping[label] = case['split']
            inputs = safe_path(self.state.root, case['input_dir']); answer = safe_path(self.state.root, case['answer'])
            require(answer.is_file() and not answer.is_relative_to(inputs) and not answer.is_relative_to(baseline), 'Hidden answers must be outside candidate/input mounts', 'DATA_LEAKAGE')
            case['input_dir'], case['answer'] = str(inputs.relative_to(self.state.root)), str(answer.relative_to(self.state.root))
            case['input_manifest'], case['answer_sha256'] = tree(inputs, limits), digest(answer)
            fingerprint = hashed(sorted(f['sha256'] for f in case['input_manifest'].values()))
            case['input_fingerprint'] = fingerprint
            require(fingerprint not in content_splits or content_splits[fingerprint] == case['split'], 'Identical input content crosses splits', 'DATA_LEAKAGE')
            content_splits[fingerprint] = case['split']
            if case['split'] == 'holdout':
                for row in self.state.db.execute("SELECT body FROM observations WHERE asset='experiment-contamination' AND kind='note'"):
                    prior = __import__('json').loads(row[0])
                    require(getattr(self, '_allow_e_reassessment', None) or case['family'] not in prior.get('families', []) and fingerprint not in prior.get('input_hashes', []),
                        'Final-test family was exposed in an earlier experiment; new final test required', 'HOLDOUT_CONTAMINATED')
            cases.append(case)
        require({c['split'] for c in cases} == {'dev', 'select', 'holdout'}, 'All three data splits required')
        answer_hashes = {c['answer_sha256'] for c in cases}
        exposed_hashes = {f['sha256'] for f in baseline_files.values()}
        exposed_hashes.update(f['sha256'] for c in cases for f in c['input_manifest'].values())
        require(not answer_hashes.intersection(exposed_hashes), 'Hidden answer bytes were copied into visible candidate/input files', 'DATA_LEAKAGE')
        spec['cases'] = cases
        depth = 2 if spec['component'] == 'M' else 1
        require('depth' not in spec or spec['depth'] == depth, 'Recursion depth is fixed at H/M; no third layer', 'RECURSION_BOUND')
        spec['depth'] = depth
        if spec['component'] == 'M':
            require(spec.get('starting_H_dir') and spec.get('meta_reason') and isinstance(spec.get('first_layer_experiments'), list) and len(spec['first_layer_experiments']) >= 2, 'Meta experiment needs starting H and multiple prior improvement records')
            spec['starting_H_manifest'] = tree(safe_path(self.state.root, spec['starting_H_dir']), limits)
            for prior in spec['first_layer_experiments']:
                old = self.get(prior)
                require(old['contract']['spec']['depth'] == 1 and old['end'] and any(r['status'] == 'completed' for r in old['runs'].values()), 'Meta evidence must be real completed first-layer records', 'RECURSION_BOUND')
        if unit == 'native_turn_attempt':
            from .native_artifacts import freeze_output_contract, _names, _config_fail
            for files in [baseline_files, *[case['input_manifest'] for case in cases]]:
                _names(list(files), _config_fail, max_files=limits.source_files)
            spec['native_output'] = freeze_output_contract(spec.get('native_output'))
            if spec['component'] == 'M':
                require('generation' in spec, 'Native M requires an explicit generation contract and distinct H execution entrypoint')
        if 'generation' in spec:
            from .native_artifacts import freeze_output_contract
            config = spec['generation']
            require(isinstance(config, dict) and set(config) <= {'entrypoint', 'producer_dir', 'producer_manifest', 'output', 'feedback_policy'}, 'Closed generation configuration required')
            config['entrypoint'] = relative_file(config.get('entrypoint', spec['entrypoint']))
            initial = spec['starting_H_manifest'] if spec['component'] == 'M' else baseline_files
            editable = spec.get('generated_H_editable', []) if spec['component'] == 'M' else list(targets)
            config['output'] = freeze_output_contract(config.get('output'), generation=True, baseline_manifest=initial, editable=editable)
            if spec['component'] == 'H':
                producer = safe_path(self.state.root, config['producer_dir'])
                config['producer_dir'] = str(producer.relative_to(self.state.root)); config['producer_manifest'] = tree(producer, limits)
                require(config['entrypoint'] in config['producer_manifest'], 'P2 entrypoint absent from frozen method')
            else:
                require('producer_dir' not in config and config['entrypoint'] in baseline_files, 'M uses its actual frozen arm as producer')
            visible_generation = list(initial.values()) + list(config.get('producer_manifest', {}).values())
            require(not answer_hashes.intersection(item['sha256'] for item in visible_generation),
                'Hidden answer bytes were copied into generation method/starting H', 'DATA_LEAKAGE')
            if unit == 'native_turn_attempt':
                for files in (initial, config.get('producer_manifest', {})):
                    _names(list(files), _config_fail, max_files=limits.source_files)
            spec['execution_entrypoint'] = relative_file(spec.get('execution_entrypoint', spec['entrypoint']))
            require(spec['execution_entrypoint'] in initial, 'Generated H execution entrypoint absent')
            policy = config.setdefault('feedback_policy', {})
            require(isinstance(policy, dict) and set(policy) <= {'dev', 'select_aggregate', 'historical_transfer', 'experiments', 'max_bytes'}, 'Closed feedback visibility policy required')
            for key, default in [('dev', True), ('select_aggregate', False), ('historical_transfer', False)]:
                policy.setdefault(key, default); require(type(policy[key]) is bool, 'Feedback switches must be booleans')
            policy.setdefault('experiments', [])
            require(isinstance(policy['experiments'], list) and len(policy['experiments']) <= 32 and all(isinstance(p, str) and p for p in policy['experiments']), 'Bounded exact visible experiment list required')
            policy.setdefault('max_bytes', 65536)
            require(type(policy['max_bytes']) is int and 1 <= policy['max_bytes'] <= 65536, 'Bounded visible feedback byte budget required')
        spec['producer_sessions'] = sorted(producers)
        spec['environment'] = self.executor.identity()
        return {'schema': 'htam.experiment-contract/v1', 'spec': spec, 'baseline_manifest': baseline_files,
            'targets': targets, 'sha256': hashed({'spec': spec, 'baseline_manifest': baseline_files, 'targets': targets})}

    def freeze(self, name, expected, prepared, approval, source):
        require(not self.state.readonly, 'Read-only experiment cannot freeze files')
        identifier(name); require(expected == 0 and not self._records(name), 'Experiment identity already exists')
        actual = self.prepare(prepared['spec'])
        require(actual == prepared, 'Contract/source changed after preparation', 'CONTENT_CHANGED')
        if isinstance(approval, dict) and approval.get('mode') == 'delegated_construction':
            from .construction import construction_authority
            bound = construction_authority(self.state, approval, prepared['sha256'], self.executor.identity(),
                subject_kind='frozen_contract', lab_root=self.directory, contract=prepared)
        else:
            bound = human(self.state, prepared['spec']['work'], approval, prepared['sha256'])
        directory = self._dir(name); directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        limits = Limits(**prepared['spec']['limits'])
        require(_snapshot(safe_path(self.state.root, prepared['spec']['baseline_dir']), directory / 'baseline', limits) == prepared['baseline_manifest'], 'Baseline changed during freeze', 'CONTENT_CHANGED')
        for case in prepared['spec']['cases']:
            path = directory / 'cases' / case['id']; path.mkdir(parents=True, mode=0o700)
            require(_snapshot(safe_path(self.state.root, case['input_dir']), path / 'input', limits) == case['input_manifest'], 'Case changed during freeze', 'CONTENT_CHANGED')
            (path / 'answer.json').write_bytes(safe_path(self.state.root, case['answer']).read_bytes())
            require(digest(path / 'answer.json') == case['answer_sha256'], 'Answer changed during freeze', 'EVALUATION_CHANGED')
            (path / 'answer.json').chmod(0o600)
        if prepared['spec']['component'] == 'M':
            _snapshot(safe_path(self.state.root, prepared['spec']['starting_H_dir']), directory / 'starting-H', limits)
        elif prepared['spec'].get('generation'):
            config = prepared['spec']['generation']
            require(_snapshot(safe_path(self.state.root, config['producer_dir']), directory / 'p2-method', limits) == config['producer_manifest'], 'P2 method changed during freeze')
        self._write_json(directory / 'manifest.json', prepared)
        self._append(name, 0, 'frozen', {**prepared, 'approval': bound, 'lab_root': str(self.directory.relative_to(self.state.root))}, source)
        return self.get(name)


def relative_file(value):
    require(isinstance(value, str) and value and not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts,
        'Relative regular file path required', 'PATH_ESCAPE')
    return value
