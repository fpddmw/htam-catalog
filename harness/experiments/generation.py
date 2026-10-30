"""Bounded P2/M visible projections and actual candidate generation in the journal."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import shutil

from ..common import require, digest
from .contracts import tree
from .isolation import Limits
from .isolation.launcher import _snapshot
from .journal import hashed, safe_path
from .evaluation import aggregate_cost


def context_digest(body):
    return hashed({k: v for k, v in body.items() if k != 'sha256'})


class Generation:
    def _feedback_projection(self, view, ref):
        spec = view['contract']['spec']; policy = spec['generation']['feedback_policy']
        require(isinstance(ref, dict) and set(ref) <= {'kind', 'experiment', 'run_id'}, 'Typed feedback reference required')
        kind, name = ref.get('kind'), ref.get('experiment')
        require(kind in ('dev_run', 'select_aggregate', 'historical_transfer') and isinstance(name, str), 'Unsupported visible feedback kind')
        require(name == view['id'] or name in policy['experiments'], 'Feedback experiment outside approved visible scope', 'GENERATION_VISIBILITY')
        prior = self.get(name)
        require(prior['contract']['spec']['work'] == spec['work'], 'Current limited feedback reader supports the same trusted local work only', 'GENERATION_VISIBILITY')
        if kind == 'dev_run':
            require(policy['dev'] and ref.get('run_id') in prior['runs'], 'Visible development run required')
            run = prior['runs'][ref['run_id']]
            require(run['split'] == 'dev' and not run['generation'] and run['status'] != 'unknown', 'Only actual terminal development task records are visible', 'GENERATION_VISIBILITY')
            data = {k: deepcopy(run.get(k)) for k in ('run_id', 'case', 'arm', 'status', 'receipt_sha256', 'evaluation', 'semantic_review', 'cost')}
            if data['semantic_review']:
                data['semantic_review'] = {k: data['semantic_review'][k] for k in ('review', 'independence')}
            data['artifacts'] = {}
            receipt = run.get('receipt', {})
            if receipt:
                root = safe_path(self.state.root, receipt['run_dir'])
                recorded_lab = safe_path(self.state.root, prior['contract']['lab_root'])
                require(root.is_relative_to(recorded_lab / name / 'runs' / run['run_id']), 'Feedback receipt root differs')
                artifacts = root / 'result/artifacts'
                for item in receipt.get('artifacts', []):
                    file = safe_path(self.state.root, str(artifacts / item['path']))
                    require(file.is_relative_to(artifacts) and digest(file) == item['sha256'], 'Feedback artifact changed')
                    data['artifacts'][item['path']] = file.read_text(encoding='utf-8')
                facts = root / 'facts.jsonl'
                if facts.is_file():
                    require(facts.stat().st_size <= 2 * 1024 * 1024 and digest(facts) == receipt['facts_sha256'], 'Feedback facts changed')
                    data['public_tools'] = [{k: deepcopy(f.get(k)) for k in
                        ('itemId', 'command', 'cwd', 'output', 'status', 'exitCode')} for f in
                        (json.loads(line) for line in facts.read_text().splitlines()) if f.get('kind') == 'f09_command_completed']
        else:
            require('run_id' not in ref, 'Aggregates never accept per-case references')
            summary = self.summary(name)
            if kind == 'select_aggregate':
                require(policy['select_aggregate'], 'Selection feedback is not enabled')
                groups = [g for g in summary['aggregate_evaluation'] if g['split'] == 'select']
            else:
                require(policy['historical_transfer'] and name != view['id'] and (prior.get('history_release') or prior['publication_plan']) and prior['end'],
                    'Only previously released historical transfer aggregates are visible', 'GENERATION_VISIBILITY')
                groups = [g for g in summary['aggregate_evaluation'] if g['split'] == 'holdout' and g.get('feedback') == 'aggregate_only']
            cost = aggregate_cost(summary['budget'])
            release = prior.get('history_release') if kind == 'historical_transfer' else None
            if release:
                groups, cost = deepcopy(release['aggregates']), deepcopy(release['cost'])
            data = {'groups': deepcopy(groups), 'contract_sha256': prior['contract']['sha256'],
                'cost': cost, 'scope': 'Aggregates only; no case/answer/judge records',
                **({'release_sha256': release['sha256'], 'source_revision': release['source_revision'],
                    'source_journal_sha256': release['source_journal_sha256']} if release else {})}
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        require(len(encoded.encode()) <= policy['max_bytes'], 'Visible feedback exceeds frozen byte budget')
        return {'reference': deepcopy(ref), 'source_sha256': hashed(data), 'projection': data}

    def prepare_generation(self, name, purpose, feedback_refs=None):
        view = self.get(name); self._active(view); spec = view['contract']['spec']
        require('generation' in spec and purpose in ('p2', 'meta'), 'Frozen generation policy required')
        require((purpose == 'p2' and spec['component'] == 'H') or (purpose == 'meta' and spec['component'] == 'M'), 'Generation role differs from frozen component')
        refs = [] if feedback_refs is None else feedback_refs
        require(isinstance(refs, list) and len(refs) <= 32, 'Bounded feedback reference list required')
        if purpose == 'meta' and view.get('generation_context'):
            selected = view['generation_context']['context']
            require(feedback_refs is None or refs == selected['references'], 'M0/M1 common feedback selection changed', 'IMMUTABLE_CONTEXT')
            return deepcopy(selected)
        projections = [self._feedback_projection(view, ref) for ref in refs]
        require(len({hashed(p['reference']) for p in projections}) == len(projections), 'Duplicate feedback reference')
        base = spec['starting_H_manifest'] if purpose == 'meta' else view['contract']['baseline_manifest']
        body = {'schema': 'htam.generation-context/v1', 'experiment': name, 'purpose': purpose,
            'contract_sha256': view['contract']['sha256'], 'policy_sha256': hashed(spec['generation']['feedback_policy']),
            'starting_H_sha256': hashed(base), 'references': deepcopy(refs), 'feedback': projections}
        require(len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) <= spec['generation']['feedback_policy']['max_bytes'], 'Generation context exceeds frozen budget')
        body['sha256'] = context_digest(body)
        if purpose == 'meta' and view.get('generation_context'):
            require(view['generation_context']['context_sha256'] == body['sha256'], 'M0/M1 common context was already fixed', 'IMMUTABLE_CONTEXT')
        return body

    def _generation_inputs(self, name, view, context):
        require(not self.state.readonly and not self._recovery_only, 'Generation view publication requires a current writable execution host')
        require(context.get('sha256') == context_digest(context) and self.prepare_generation(name, context['purpose'], context['references']) == context,
            'Frozen generation context/source projection changed', 'CONTENT_CHANGED')
        spec = view['contract']['spec']; limits = Limits(**spec['limits'])
        root = self._dir(name) / 'generation-input' / context['sha256']
        base = self._dir(name) / ('starting-H' if context['purpose'] == 'meta' else 'baseline')
        expected_base = spec['starting_H_manifest'] if context['purpose'] == 'meta' else view['contract']['baseline_manifest']
        require(tree(base, limits) == expected_base, 'Frozen starting H changed')
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='pending-', dir=root.parent))
            try:
                require(_snapshot(base, stage / 'H0', limits) == expected_base, 'Starting H changed during generation snapshot')
                (stage / 'dev').mkdir()
                for case in spec['cases']:
                    if case['split'] == 'dev' and spec['generation']['feedback_policy']['dev']:
                        require(_snapshot(self._dir(name) / 'cases' / case['id'] / 'input', stage / 'dev' / case['id'], limits) == case['input_manifest'], 'Frozen dev input changed')
                (stage / 'context.json').write_text(json.dumps(context, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')
                tree(stage, limits)  # Validate the combined view before publication.
                try:
                    stage.rename(root)
                except OSError:
                    require(root.exists(), 'Generation view publication failed')
            finally:
                if stage.exists(): shutil.rmtree(stage)
        require(json.loads((root / 'context.json').read_text()) == context and tree(root / 'H0', limits) == expected_base, 'Immutable generation input differs')
        expected_names = {'context.json'} | {'H0/' + p for p in expected_base}
        expected_names.update('dev/' + c['id'] + '/' + p for c in spec['cases'] if c['split'] == 'dev' and spec['generation']['feedback_policy']['dev'] for p in c['input_manifest'])
        manifest = tree(root, limits)
        require(set(manifest) == expected_names, 'Unexpected generation input file')
        for case in spec['cases']:
            if case['split'] == 'dev' and spec['generation']['feedback_policy']['dev']:
                require(tree(root / 'dev' / case['id'], limits) == case['input_manifest'], 'Generation dev input changed')
        return root, manifest

    def _generation_invocation(self, name, view, arm, context):
        spec = view['contract']['spec']; config = spec['generation']; purpose = context['purpose']
        if purpose == 'meta':
            method, files = self._method(name, view, arm, generated=False)
        else:
            method, files = self._dir(name) / 'p2-method', config['producer_manifest']
            require(tree(method, Limits(**spec['limits'])) == files, 'Frozen P2 method changed')
        inputs, manifest = self._generation_inputs(name, view, context)
        invocation = {'role': purpose, 'method_manifest': files, 'entrypoint': config['entrypoint'],
            'input_route': str(inputs.relative_to(self.state.root)), 'input_manifest_sha256': hashed(manifest),
            'context_sha256': context['sha256'], 'context': context,
            'output_contract': config['output'], 'output_contract_sha256': hashed(config['output'])}
        return method, files, inputs, manifest, invocation

    def propose_candidate(self, name, expected, run_id, arm, generation_context, mechanism, source, replaces=None):
        require(not self.state.readonly, 'Read-only experiment cannot propose or write generated candidates')
        require(isinstance(mechanism, str) and mechanism.strip(), 'Exact proposed candidate mechanism required')
        view = self.get(name); self._active(view)
        require(view['contract']['spec']['component'] == 'H' and generation_context.get('purpose') == 'p2', 'H-level P2 proposal required')
        require(arm not in ('H0', 'H0_equal_budget') and not view['lock'], 'Candidate proposal is locked/reserved')
        if run_id in view['runs']:
            result = view['runs'][run_id]; invocation = result.get('invocation', {})
            require(invocation.get('role') == 'p2' and result['arm'] == arm and invocation.get('context') == generation_context and
                invocation.get('candidate_replaces') == replaces and invocation.get('candidate_mechanism') == mechanism,
                'P2 run ID reused with different request', 'RUN_ID_CONFLICT')
        else:
            method, files, inputs, manifest, invocation = self._generation_invocation(name, view, arm, generation_context)
            invocation['candidate_replaces'] = replaces
            invocation['candidate_mechanism'] = mechanism
            intent, new = self._reserve(name, expected, run_id, arm, 'proposal_generate', '__proposal__', inputs, manifest,
                generation=True, invocation=invocation)
            result = self._invoke(name, intent, method, inputs, files) if new else intent
        require(result['status'] == 'completed', 'P2 did not produce valid actual artifacts', 'NOT_RUN')
        produced = Path(result['receipt']['run_dir']) / 'result/artifacts/generated'
        manifest = tree(produced, Limits(**view['contract']['spec']['limits']))
        accepted = {a['path'].removeprefix('generated/'): {'sha256': a['sha256'], 'bytes': a['bytes']}
            for a in result['receipt']['artifacts'] if a['path'].startswith('generated/')}
        require(manifest == accepted, 'P2 output differs from accepted actual receipt', 'CONTENT_CHANGED')
        if manifest == view['contract']['baseline_manifest']:
            if not any(r['kind'] == 'proposal_no_change' and r['data']['run_id'] == run_id for r in self.get(name)['history']):
                self._auto(name, 'proposal_no_change', {'run_id': run_id, 'arm': arm, 'receipt_sha256': result['receipt_sha256']}, source)
            return {'outcome': 'no_change', 'run': result}
        current = self.get(name)
        if arm in current['candidates']:
            require(current['candidates'][arm].get('producer_run') == run_id and current['candidates'][arm]['files'] == manifest,
                'Candidate already has a different producer', 'RUN_ID_CONFLICT')
            return current
        return self.add_candidate(name, current['revision'], arm, str(produced.relative_to(self.state.root)), mechanism,
            source, replaces=replaces, producer_run=run_id)
