"""Actual offline invocation, durable reservation/reconciliation and all costs."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import time

from ..common import digest, require, HarnessError, now
from ..experiences import ExperienceRegistry
from .contracts import environment, tree
from .isolation import launch, Limits, IsolationError
from .isolation.launcher import _snapshot
from .journal import identifier, hashed, safe_path


class OfflineExecutor:
    def identity(self):
        return environment()

    def execute(self, candidate, inputs, directory, *, entrypoint, args, limits):
        return launch(candidate, inputs, directory, entrypoint=entrypoint, args=args, limits=limits)


class Execution:
    def _candidate_admission(self, name, view, arm, replaces):
        """One admission rule for direct registration and pre-send P2 reserve."""
        identifier(arm); require(arm not in ('H0', 'H0_equal_budget'), 'Reserved baseline arm')
        require(not view['lock'] and not any(r['split'] == 'holdout' for r in view['runs'].values()),
            'Candidate is locked; a new final test/experiment is required', 'HOLDOUT_CONTAMINATED')
        old = view['candidates']; ceiling = view['contract']['spec']['budget']
        require(arm not in old, 'Candidate arm/version is immutable')
        revisions = sum(bool(c.get('replaces')) for c in old.values())
        initial = sum(not c.get('replaces') for c in old.values())
        require((replaces in old and revisions < ceiling['max_revisions']) if replaces else initial < ceiling['max_candidates'],
            'Candidate/revision budget exhausted', 'BUDGET_EXHAUSTED')
        if replaces:
            self._method(name, view, replaces, generated=False)

    def _native_scope_usage(self, scope):
        """Same-store reservations, including unknown and not-run attempts."""
        runs = {}
        for row in self.state.db.execute("SELECT asset,body FROM observations WHERE kind='note' AND asset LIKE 'experiment:%' ORDER BY id"):
            record = json.loads(row['body']).get('experiment', {})
            data = record.get('data', {}); key = (row['asset'], data.get('run_id'))
            if record.get('kind') == 'run_intent' and data.get('native_authorization_scope') == scope:
                runs[key] = {**data, 'status': 'unknown'}
            elif record.get('kind') in ('run_result', 'run_reconciled') and key in runs:
                runs[key].update(data)
        return {'scope': scope, 'reserved': sum(r.get('reserved_units', 1) for r in runs.values()),
            'unknown': [{'experiment': key[0].removeprefix('experiment:'), 'run_id': key[1]}
                for key, run in runs.items() if run['status'] == 'unknown'],
            'blocking': [{'experiment': key[0].removeprefix('experiment:'), 'run_id': key[1], 'failure': run['receipt']['blocking_failure']}
                for key, run in runs.items() if run.get('receipt', {}).get('blocking_failure')]}

    def add_candidate(self, name, expected, arm, candidate_dir, mechanism, source, *, replaces=None, producer_run=None):
        require(not self.state.readonly, 'Read-only experiment cannot freeze a candidate')
        identifier(arm); require(arm not in ('H0', 'H0_equal_budget'), 'Reserved baseline arm')
        view = self.get(name); self._active(view)
        require(view['revision'] == expected, 'Stale candidate revision', 'REVISION_CONFLICT')
        self._candidate_admission(name, view, arm, replaces)
        spec = view['contract']['spec']; old = view['candidates']
        candidate = safe_path(self.state.root, str(candidate_dir))
        approval = view['contract']['approval']
        if approval.get('mode') == 'delegated_construction':
            prefix = safe_path(self.state.root, approval['scope']['permitted_local_prefix'])
            require(candidate.is_relative_to(prefix), 'Construction candidate lies outside approved synthetic prefix', 'CONSTRUCTION_SCOPE')
        files = tree(candidate, Limits(**spec['limits']))
        producer = None
        if producer_run is not None:
            producer = view['runs'].get(producer_run)
            require(producer and producer['status'] == 'completed' and producer['generation'] and
                producer.get('invocation', {}).get('role') == 'p2' and producer['arm'] == arm,
                'Candidate requires its exact actual P2 producer', 'GENERATION_PROVENANCE')
            require(producer['invocation'].get('candidate_mechanism') == mechanism and
                producer['invocation'].get('candidate_replaces') == replaces, 'Candidate metadata differs from its frozen P2 intent', 'GENERATION_PROVENANCE')
            accepted = {a['path'].removeprefix('generated/'): {'sha256': a['sha256'], 'bytes': a['bytes']}
                for a in producer['receipt']['artifacts'] if a['path'].startswith('generated/')}
            require(files == accepted, 'Candidate differs from actual accepted P2 output', 'CONTENT_CHANGED')
        if spec.get('evaluation_reassessment_of'):
            parent = self.get(spec['evaluation_reassessment_of'])
            require(hashed(files) in {c['sha256'] for c in parent['candidates'].values()}, 'E reassessment cannot optimize a new H/M on exposed data', 'HOLDOUT_CONTAMINATED')
        baseline = view['contract']['baseline_manifest']
        changed = {p for p in set(baseline) | set(files) if baseline.get(p) != files.get(p)}
        require(changed and changed <= set(spec['targets']) and set(files) == set(baseline), 'Candidate changes outside approved files or no concrete change', 'CHANGE_SCOPE')
        frozen = self._dir(name) / 'candidates' / arm; frozen.parent.mkdir(exist_ok=True)
        if frozen.exists():
            require(tree(frozen, Limits(**spec['limits'])) == files, 'Existing immutable candidate bytes differ', 'CONTENT_CHANGED')
        else:
            require(_snapshot(candidate, frozen, Limits(**spec['limits'])) == files, 'Candidate changed while freezing', 'CONTENT_CHANGED')
        payload = {'files': files, 'changed': sorted(changed), 'mechanism': mechanism}
        manifest = self._dir(name) / 'candidates' / (arm + '.json')
        if manifest.exists():
            require(json.loads(manifest.read_text()) == payload, 'Existing candidate proposal differs', 'CONTENT_CHANGED')
            manifest_path = str(manifest.relative_to(self.state.root))
        else:
            manifest_path = self._write_json(manifest, payload)
        experience_id = name + '.' + arm
        with self.state.transaction():
            registry = ExperienceRegistry(self.state)
            exp = registry.propose(experience_id, 0, {'work': spec['work'], 'title': mechanism,
                'claim': mechanism, 'applies': spec.get('applies', {}), 'evidence': [spec['trigger'], {'path': manifest_path}] +
                    ([{'observation_id': producer['intent_observation']}] if producer else []),
                'method_targets': [{'path': t['path']} for t in view['contract']['targets'].values()],
                'constraints': spec.get('limitations', []), 'counterexamples': []}, source)
            if replaces:
                old_experience = registry.get(old[replaces]['experience_id'])
                registry.relate(old_experience['id'], old_experience['revision'], 'superseded_by', [experience_id],
                    'Explicit bounded replacement in experiment ' + name, source)
            self._append(name, expected, 'candidate', {'arm': arm, 'files': files, 'sha256': hashed(files),
                'changed': sorted(changed), 'mechanism': mechanism, 'experience_id': experience_id,
                'experience_sha256': exp['candidate_sha256'], 'replaces': replaces,
                **({'producer_run': producer_run, 'producer_receipt_sha256': producer['receipt_sha256']} if producer else {})}, source)
        return self.get(name)

    def _method(self, name, view, arm, *, generated=True):
        if generated and view['contract']['spec']['component'] == 'M':
            if arm not in ('H0', 'H0_equal_budget'):
                self._method(name, view, arm, generated=False)
            generated_arm = 'H0' if arm == 'H0_equal_budget' else arm
            require(generated_arm in view['generations'], 'Meta arm must actually generate its H first', 'NOT_RUN')
            data = view['generations'][generated_arm]
            return safe_path(self.state.root, data['path']), data['files']
        if arm in ('H0', 'H0_equal_budget'):
            return self._dir(name) / 'baseline', view['contract']['baseline_manifest']
        require(arm in view['candidates'], 'Unknown candidate arm')
        require(not any(c.get('replaces') == arm for c in view['candidates'].values()), 'Candidate was explicitly replaced', 'CANDIDATE_INACTIVE')
        current = view['candidates'][arm]
        exp = ExperienceRegistry(self.state).get(current['experience_id'])
        require(exp.get('status') == 'active' and not exp.get('relations') and exp.get('visibility') != 'restricted', 'Candidate is inactive/replaced/restricted', 'CANDIDATE_INACTIVE')
        return self._dir(name) / 'candidates' / arm, current['files']

    def _recorded_method_manifest(self, view, arm, *, generation=False):
        """Immutable original facts, independent of later experience status."""
        if view['contract']['spec']['component'] == 'M' and not generation:
            key = 'H0' if arm == 'H0_equal_budget' else arm
            require(key in view['generations'], 'Original generated H is unavailable', 'RUN_UNKNOWN')
            return view['generations'][key]['files']
        if arm in ('H0', 'H0_equal_budget'):
            return view['contract']['baseline_manifest']
        require(arm in view['candidates'], 'Original candidate record is unavailable', 'RUN_UNKNOWN')
        return view['candidates'][arm]['files']

    def budget(self, name):
        view = self.get(name); spec = view['contract']['spec']; runs = list(view['runs'].values())
        wall = calls = units = native_turns = 0; per_arm = {}; unknown = []; unknown_calls = []
        unit = spec['environment'].get('budget_unit', 'provider_model_call')
        for run in runs:
            cost = run.get('cost', {'wall_seconds': run['reserved_seconds'], 'model_calls': run.get('reserved_calls', 0), 'tokens': None, 'money': None})
            observed_calls = cost.get('model_calls')
            if run.get('budget_unit') == 'native_turn_attempt' and 'cost' not in run:
                observed_calls = None
            if observed_calls is None:
                unknown_calls.append(run['run_id'])
            else:
                calls += observed_calls
            used = run.get('reserved_units', 1) if run.get('budget_unit') == 'native_turn_attempt' else cost.get('budget_units', observed_calls or 0)
            wall += cost['wall_seconds']; units += used
            native_turns += used if run.get('budget_unit') == 'native_turn_attempt' else 0
            arm = per_arm.setdefault(run['budget_arm'], {'runs': 0, 'wall_seconds': 0, 'model_calls': 0, 'budget_units': 0})
            arm['runs'] += 1; arm['wall_seconds'] += cost['wall_seconds']; arm['budget_units'] += used
            arm['model_calls'] = None if observed_calls is None or arm['model_calls'] is None else arm['model_calls'] + observed_calls
            if cost.get('tokens') is None or cost.get('money') is None:
                unknown.append(run['run_id'])
        for row in view['history']:
            if row['kind'] != 'cost':
                continue
            data = row['data']; wall += data['wall_seconds']; calls += data['model_calls']
            charged_units = data['model_calls'] if unit == 'provider_model_call' else 0
            units += charged_units
            arm = per_arm.setdefault(data['arm'], {'runs': 0, 'wall_seconds': 0, 'model_calls': 0, 'budget_units': 0})
            arm['wall_seconds'] += data['wall_seconds']; arm['budget_units'] += charged_units
            if arm['model_calls'] is not None:
                arm['model_calls'] += data['model_calls']
            if data.get('tokens') is None or data.get('money') is None:
                unknown.append(data['category'])
        result = {'runs': len(runs), 'wall_seconds': wall, 'model_calls': None if unknown_calls else calls,
            'known_provider_model_calls': calls, 'unknown_provider_model_call_runs': unknown_calls,
            'budget_unit': unit, 'budget_units_used': units, 'native_turn_attempts_reserved': native_turns, 'per_arm': per_arm,
            'limits': spec['budget'], 'unknown_cost_records': unknown,
            'cost_coverage': 'Recorded actual executions plus explicitly sourced generation/evaluation/review/maintenance; unobserved human/model costs remain unknown'}
        if unit == 'native_turn_attempt':
            result['native_scope'] = self._native_scope_usage(spec['environment']['native_authorization_scope'])
        return result

    def charge(self, name, expected, body, source):
        require(body.get('category') in ('generation', 'evaluation', 'semantic_review', 'maintenance', 'recovery') and body.get('source_ref'), 'Cost category and actual source required')
        require(type(body.get('wall_seconds')) in (int, float) and body['wall_seconds'] >= 0 and type(body.get('model_calls')) is int and body['model_calls'] >= 0, 'Nonnegative observed costs required')
        require(body.get('arm'), 'Cost allocation arm required')
        self._append(name, expected, 'cost', body, source)
        return self.budget(name)

    def _reserve(self, name, expected, run_id, arm, split, case, inputs, manifest, *, generation=False, invocation=None):
        # Scope admission and intent commit share BEGIN IMMEDIATE, including
        # competing labs in the same store; no parallel native budget database.
        try:
            with self.state.transaction():
                return self._reserve_transaction(name, expected, run_id, arm, split, case, inputs, manifest, generation=generation, invocation=invocation)
        except HarnessError as error:
            if error.code == 'BUDGET_EXHAUSTED':
                # Persist the stop after the rejected admission transaction has
                # rolled back. No run intent/process was committed or launched.
                self._append(name, expected, 'budget_stop', {'run_id': run_id, 'reason': str(error), 'cost': self.budget(name)}, 'Frozen budget admission')
            raise

    def _reserve_transaction(self, name, expected, run_id, arm, split, case, inputs, manifest, *, generation=False, invocation=None):
        view = self.get(name)
        spec = view['contract']['spec']; identifier(run_id)
        request = {'run_id': run_id, 'arm': arm, 'split': split, 'case': case, 'input_manifest': manifest,
            'generation': generation, 'executor_identity': self.executor.identity(),
            'method_manifest': invocation['method_manifest'] if invocation else self._recorded_method_manifest(view, arm, generation=generation)}
        if invocation is not None:
            request['invocation'] = deepcopy(invocation)
        elif spec.get('native_output') is not None:
            request['output_contract'] = deepcopy(spec['native_output'])
        if run_id in view['runs']:
            prior = view['runs'][run_id]
            prior_request = request if 'method_manifest' in prior else {k: v for k, v in request.items() if k != 'method_manifest'}
            require(prior['request_sha256'] == hashed(prior_request), 'Run ID reused for a different request', 'RUN_ID_CONFLICT')
            return prior, False
        require(view['revision'] == expected, 'Stale new-run revision', 'REVISION_CONFLICT')
        self._active(view)
        require(not any(r['arm'] == arm and r['case'] == case and r['generation'] == generation for r in view['runs'].values()),
            'Frozen arm/case already ran; retain failure and use the same run ID for reconciliation', 'CASE_ALREADY_RUN')
        require(not any(r['status'] == 'unknown' for r in view['runs'].values()), 'Reconcile the existing unknown execution first', 'RUN_UNKNOWN')
        require(request['executor_identity'] == spec['environment'], 'Frozen execution environment changed', 'ENVIRONMENT_CHANGED')
        identity = request['executor_identity']; unit = identity.get('budget_unit', 'provider_model_call')
        limits = Limits(**spec['limits'])
        reservation = limits.wall_seconds + (identity['maximum_wall_overhead_seconds'] if unit == 'native_turn_attempt' else 4)
        reserved_units = identity.get('maximum_budget_units_per_run', identity.get('maximum_model_calls_per_run', 0))
        reserved_calls = None if unit == 'native_turn_attempt' else reserved_units
        cost = self.budget(name); ceiling = spec['budget']; budget_arm = 'H0_equal_budget' if arm == 'H0_equal_budget' else arm
        previous = cost['per_arm'].get(budget_arm, {'runs': 0, 'wall_seconds': 0, 'model_calls': 0, 'budget_units': 0})
        if unit == 'native_turn_attempt':
            native_scope = self._native_scope_usage(identity['native_authorization_scope'])
            require(not native_scope['unknown'], 'Native scope has an unknown invocation; reconcile before any new turn', 'RUN_UNKNOWN')
            require(not native_scope['blocking'], 'Native scope has an explicit boundary failure; no new sends', 'NATIVE_BOUNDARY_UNVERIFIED')
            require(native_scope['reserved'] + reserved_units <= identity['native_scope_cap'], 'Native construction scope turn budget exhausted', 'BUDGET_EXHAUSTED')
        if not (cost['runs'] < ceiling['max_runs'] and cost['wall_seconds'] + reservation <= ceiling['max_wall_seconds'] and
            cost['budget_units_used'] + reserved_units <= ceiling['max_calls'] and previous['budget_units'] + reserved_units <= ceiling['per_arm_calls'] and previous['runs'] < ceiling['per_arm_runs'] and
            previous['wall_seconds'] + reservation <= ceiling['per_arm_wall_seconds']):
            raise HarnessError('Experiment budget exhausted; no process launched', 'BUDGET_EXHAUSTED')
        if invocation and invocation['role'] == 'p2':
            self._candidate_admission(name, view, arm, invocation.get('candidate_replaces'))
            proposals = [r for r in view['runs'].values() if r.get('invocation', {}).get('role') == 'p2']
            require(len(proposals) < ceiling['max_candidates'] + ceiling['max_revisions'], 'P2 proposal/revision budget exhausted', 'BUDGET_EXHAUSTED')
        if invocation and invocation['role'] == 'meta':
            common = view.get('generation_context')
            require(not common or common['context_sha256'] == invocation['context_sha256'], 'M0/M1 common context changed', 'IMMUTABLE_CONTEXT')
            if common is None:
                self._append(name, expected, 'generation_context', {'context_sha256': invocation['context_sha256'],
                    'input_manifest_sha256': hashed(manifest), 'input_route': invocation['input_route'],
                    'context': deepcopy(invocation['context'])}, 'Common M0/M1 input frozen before first send')
                expected += 1
        data = {**request, 'request_sha256': hashed(request), 'budget_arm': budget_arm,
            'reserved_seconds': reservation, 'reserved_calls': reserved_calls, 'reserved_units': reserved_units, 'budget_unit': unit,
            **({'native_authorization_scope': identity['native_authorization_scope'], 'native_scope_cap': identity['native_scope_cap']} if unit == 'native_turn_attempt' else {}),
            'run_root': str((self._dir(name) / 'runs' / run_id).relative_to(self.state.root)),
            'prepared_at': now()}
        from .construction import guard_construction_action
        guard_construction_action(self, name, view, data, stage='reserve')
        record = self._append(name, expected, 'run_intent', data, 'Actual executor invocation reserved')
        data['intent_observation'] = record['observation_id']
        return data, True

    def _invoke(self, name, intent, method, inputs, expected_files):
        spec = self.get(name)['contract']['spec']; started = time.monotonic(); invoked = False
        try:
            require(tree(method, Limits(**spec['limits'])) == expected_files, 'Frozen method content changed', 'CONTENT_CHANGED')
            require(tree(inputs, Limits(**spec['limits'])) == intent['input_manifest'], 'Frozen case content changed', 'CONTENT_CHANGED')
            invoked = True
            extra = {'execution_context': {**intent, 'experiment': name, 'candidate_manifest': expected_files},
                'guard': lambda action: self._native_send_guard(name, intent, expected_files, action)} if intent.get('budget_unit') == 'native_turn_attempt' else {}
            invocation = intent.get('invocation', {})
            if extra:
                extra['execution_context']['output_contract'] = invocation.get('output_contract', spec.get('native_output'))
            receipt = self.executor.execute(method, inputs, safe_path(self.state.root, intent['run_root']),
                entrypoint=invocation.get('entrypoint', spec['entrypoint'] if intent['generation'] else spec.get('execution_entrypoint', spec['entrypoint'])),
                args=[intent['case']], limits=Limits(**spec['limits']), **extra)
            if type(self.executor) is not OfflineExecutor:
                require(self.executor.verify_receipt(receipt, intent, expected_files), 'Verified adapter did not confirm actual matching execution', 'EXECUTOR_UNVERIFIED')
            else:
                require(receipt['source_manifest'] == {'candidate': expected_files, 'input': intent['input_manifest']}, 'Actual executed source/input differs from the frozen request', 'CONTENT_CHANGED')
            usage = receipt.get('cost', {}) if type(self.executor) is not OfflineExecutor else {'model_calls': 0, 'tokens': None, 'money': None}
            if intent.get('budget_unit') == 'native_turn_attempt':
                require(usage.get('model_calls') is None and usage.get('native_turn_attempts') == 1,
                    'Native usage must report one bounded turn attempt and unknown internal provider calls', 'EXECUTOR_UNVERIFIED')
            else:
                require(type(usage.get('model_calls', intent['reserved_calls'])) is int and 0 <= usage.get('model_calls', intent['reserved_calls']) <= intent['reserved_calls'], 'Executor exceeded or misreported its call reservation', 'EXECUTOR_UNVERIFIED')
            status = 'completed' if receipt['execution_succeeded'] else 'failed'
            data = {'run_id': intent['run_id'], 'status': status, 'receipt': receipt, 'receipt_sha256': hashed(receipt),
                'method_manifest': expected_files, 'method_sha256': hashed(expected_files),
                'cost': {'wall_seconds': time.monotonic() - started, 'model_calls': usage.get('model_calls', intent['reserved_calls']),
                    'tokens': usage.get('tokens'), 'money': usage.get('money'),
                    'basis': 'Actual default offline invocation or verified adapter usage; missing provider usage stays unknown/conservatively reserved'}}
        except (IsolationError, HarnessError, OSError) as error:
            data = {'run_id': intent['run_id'], 'status': 'unknown' if invoked else 'not_run', 'error': {'type': type(error).__name__, 'message': str(error)},
                'cost': {'wall_seconds': intent['reserved_seconds'] if invoked else time.monotonic() - started,
                    'model_calls': intent['reserved_calls'] if invoked else 0, 'tokens': None, 'money': None}}
        self._auto(name, 'run_result', data, 'Actual executor returned')
        return self.get(name)['runs'][intent['run_id']]

    def _native_send_guard(self, name, intent, files, action):
        """Trusted parent check at adapter beforeSend; no authority from labels."""
        require(not self._recovery_only, 'Recovery-only object owns no live native runtime', 'GUARD_DENIED')
        require(isinstance(action, dict) and action.get('method') in ('thread/start', 'turn/start', 'turn/interrupt') and
            isinstance(action.get('intentId'), str) and isinstance(action.get('requestSha256'), str), 'Invalid native action', 'GUARD_DENIED')
        with self.state.transaction():
            view = self.get(name); run = view['runs'].get(intent['run_id'])
            require(run and run['request_sha256'] == intent['request_sha256'] and run['intent_observation'] == intent['intent_observation'] and
                run['status'] == 'unknown', 'Native reservation no longer current', 'GUARD_DENIED')
            if action['method'] != 'turn/interrupt':
                self._active(view)
                from .construction import guard_construction_action
                guard_construction_action(self, name, view, run, stage='send', action=action)
                require(not self._native_scope_usage(intent['native_authorization_scope'])['blocking'],
                    'Native scope has an explicit boundary failure; no new sends', 'NATIVE_BOUNDARY_UNVERIFIED')
                require(self.executor.identity() == intent['executor_identity'], 'Native executor changed before send', 'ENVIRONMENT_CHANGED')
                invocation = run.get('invocation')
                if invocation:
                    method, current_files, inputs, manifest, actual = self._generation_invocation(name, view, run['arm'], invocation['context'])
                    actual.update({k: invocation[k] for k in ('candidate_replaces', 'candidate_mechanism') if k in invocation})
                    require(actual == invocation and manifest == run['input_manifest'], 'Frozen generation invocation changed', 'CONTENT_CHANGED')
                else:
                    method, current_files = self._method(name, view, run['arm'], generated=not run['generation'])
                    inputs = self._dir(name) / ('meta-input' if run['generation'] else 'cases/' + run['case'] + '/input')
                require(current_files == files and tree(method, Limits(**view['contract']['spec']['limits'])) == files,
                    'Frozen native method changed before send', 'CONTENT_CHANGED')
                require(tree(inputs, Limits(**view['contract']['spec']['limits'])) == run['input_manifest'],
                    'Frozen native case changed before send', 'CONTENT_CHANGED')
            records = [json.loads(r['body']) for r in self.state.db.execute("SELECT body FROM observations WHERE asset='experiment-native-send' AND kind='note'")]
            records = [r for r in records if r.get('experiment') == name and r.get('run_id') == run['run_id']]
            require(not any(r['action']['intentId'] == action['intentId'] for r in records), 'Native action already attempted; no resend', 'ALREADY_ATTEMPTED')
            prior_starts = [r['action'] for r in records if r['action']['method'] == 'turn/start']
            require(action['method'] != 'thread/start' or not any(r['action']['method'] == 'thread/start' for r in records),
                'Only one new native thread per reservation', 'GUARD_DENIED')
            require(action['method'] != 'turn/start' or not prior_starts, 'Only one native turn per reservation', 'BUDGET_EXHAUSTED')
            if action['method'] in ('turn/start', 'turn/interrupt'):
                paths = list(safe_path(self.state.root, run['run_root']).glob('*/facts.jsonl'))
                require(len(paths) == 1 and paths[0].stat().st_size <= 2 * 1024 * 1024, 'Original native ownership facts unavailable', 'GUARD_DENIED')
                facts = [json.loads(line) for line in paths[0].read_text().splitlines()]
                if action['method'] == 'turn/start':
                    require(any(f.get('kind') == 'acknowledged' and f.get('method') == 'thread/start' and
                        f.get('nativeResult', {}).get('threadId') == action.get('threadId') and
                        f.get('runtimeId') == action.get('runtimeId') for f in facts), 'Turn is outside this original newly created native thread', 'GUARD_DENIED')
                else:
                    require(any(f.get('kind') == 'acknowledged' and f.get('method') == 'turn/start' and
                        f.get('threadId') == action.get('threadId') and f.get('nativeResult', {}).get('turnId') == action.get('turnId') and
                        f.get('runtimeId') == action.get('runtimeId') for f in facts), 'Interrupt is outside the exact original native turn', 'GUARD_DENIED')
            observation = self.state.observation('experiment-native-send', 'note', {'experiment': name, 'run_id': run['run_id'],
                'reservation_observation_id': run['intent_observation'], 'request_sha256': run['request_sha256'], 'action': action}, 'Current original experiment authority checked at native send boundary')
        return {'allowed': True, 'intentId': action['intentId'], 'requestSha256': action['requestSha256'], 'guardRef': 'observation:' + str(observation)}

    def run(self, name, expected, run_id, arm, case_id):
        require(not self._recovery_only, 'Recovery-only host cannot run or send new executor work', 'EXECUTOR_UNVERIFIED')
        view = self.get(name); spec = view['contract']['spec']
        case = next((c for c in spec['cases'] if c['id'] == case_id), None)
        require(case is not None, 'Unknown frozen case')
        if case['split'] == 'holdout':
            require(view['lock'] and arm in ('H0', 'H0_equal_budget', view['lock']['arm']), 'Only H0, locked candidate and equal-budget control may run holdout', 'HOLDOUT_LOCK_REQUIRED')
        else:
            require(arm != 'H0_equal_budget', 'Equal-budget control is reserved for final test')
        inputs = self._dir(name) / 'cases' / case_id / 'input'
        if run_id in view['runs']:
            prior, _ = self._reserve(name, expected, run_id, arm, case['split'], case_id, inputs, case['input_manifest'])
            return prior
        method, files = self._method(name, view, arm)
        intent, new = self._reserve(name, expected, run_id, arm, case['split'], case_id, inputs, case['input_manifest'])
        return self._invoke(name, intent, method, inputs, files) if new else intent

    def reconcile(self, name, expected, run_id, source):
        view = self.get(name); run = view['runs'][run_id]
        require(run['status'] == 'unknown', 'Only unknown invocation needs reconciliation')
        root = safe_path(self.state.root, run['run_root'])
        files = run.get('method_manifest') or self._recorded_method_manifest(view, run['arm'], generation=run['generation'])
        if type(self.executor) is not OfflineExecutor:
            require(callable(getattr(self.executor, 'recover_receipt', None)), 'Executor has no verified recovery path; keep unknown', 'RUN_UNKNOWN')
            require(self.executor.identity() == view['contract']['spec']['environment'], 'Frozen executor identity changed', 'ENVIRONMENT_CHANGED')
            receipt = self.executor.recover_receipt({**run, 'experiment': name, 'candidate_manifest': files, 'absolute_run_root': str(root)}, files)
            require(receipt and self.executor.verify_receipt(receipt, run, files), 'No matching original native receipt; keep unknown', 'RUN_UNKNOWN')
            data = {'run_id': run_id, 'status': 'completed' if receipt['execution_succeeded'] else 'failed',
                'receipt': receipt, 'receipt_sha256': hashed(receipt), 'method_manifest': files, 'method_sha256': hashed(files),
                'cost': {'wall_seconds': run['reserved_seconds'], 'model_calls': None, 'tokens': receipt.get('cost', {}).get('tokens'), 'money': None,
                    'basis': 'Full native turn/wall reservation retained after interruption; no resend'}}
            self._append(name, expected, 'run_reconciled', data, source)
            return self.get(name)['runs'][run_id]
        receipts = list(root.glob('*/run.json')) if root.is_dir() else []
        require(len(receipts) == 1, 'No unique actual local executor receipt; keep unknown and do not rerun', 'RUN_UNKNOWN')
        path = receipts[0]; receipt = json.loads(path.read_text())
        require(receipt.get('protocol') == 'htam.isolation-probe.v1' and receipt.get('run_id') == path.parent.name and
            receipt.get('source_manifest', {}).get('input') == run['input_manifest'] and
            receipt.get('runner_sha256') == digest(Path(__file__).parent / 'isolation/inside.py'), 'Receipt does not match this actual reserved invocation', 'RUN_UNKNOWN')
        require(receipt['source_manifest']['candidate'] == files, 'Actual receipt method differs', 'RUN_UNKNOWN')
        data = {'run_id': run_id, 'status': 'completed' if receipt['execution_succeeded'] else 'failed',
            'receipt': receipt, 'receipt_sha256': hashed(receipt), 'method_manifest': files, 'method_sha256': hashed(files),
            'cost': {'wall_seconds': run['reserved_seconds'], 'model_calls': 0, 'tokens': None, 'money': None,
                'basis': 'Conservative full reservation retained after parent interruption; receipt time available separately'}}
        self._append(name, expected, 'run_reconciled', data, source)
        return self.get(name)['runs'][run_id]
