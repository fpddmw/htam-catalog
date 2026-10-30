"""Second layer: isolated M0/M1 actually generate H, then common E tests H."""
from pathlib import Path
import shutil
import uuid

from ..common import digest, require
from .contracts import tree
from .journal import hashed, safe_path
from .isolation import Limits
from .isolation.launcher import _snapshot


class MetaTrial:
    def __init__(self, lab, name):
        self.lab, self.name = lab, name
        require(lab.get(name)['contract']['spec']['component'] == 'M', 'MetaTrial needs an M contract')

    def generate(self, expected, run_id, arm, generation_context=None):
        lab, name = self.lab, self.name; view = lab.get(name); lab._active(view)
        require(not lab.state.readonly, 'Read-only experiment cannot generate H')
        if arm in view['generations']:
            prior = view['generations'][arm]
            require(prior['generation_run'] == run_id and (generation_context is None or
                view['runs'][run_id].get('invocation', {}).get('context') == generation_context),
                'Meta arm already has a different immutable generation', 'RUN_ID_CONFLICT')
            return prior
        require(arm not in view['generations'] and not view['lock'], 'Meta arm already generated/locked; recursion depth stops at M', 'RECURSION_BOUND')
        spec = view['contract']['spec']; invocation = None
        if spec.get('generation'):
            context = generation_context or lab.prepare_generation(name, 'meta')
            require(context.get('purpose') == 'meta', 'Meta context required')
            method, files, inputs, manifest, invocation = lab._generation_invocation(name, view, arm, context)
        else:
            require(generation_context is None, 'Legacy offline generator has no dynamic context policy')
            inputs = lab._dir(name) / 'meta-input'
        if invocation is None and not inputs.exists():
            inputs.mkdir()
            _snapshot(lab._dir(name) / 'starting-H', inputs / 'H0', Limits(**spec['limits']))
            for case in spec['cases']:
                if case['split'] == 'dev':
                    _snapshot(lab._dir(name) / 'cases' / case['id'] / 'input', inputs / case['id'], Limits(**spec['limits']))
        if invocation is None:
            manifest = tree(inputs, Limits(**spec['limits']))
            method, files = lab._method(name, view, arm, generated=False)
        intent, new = lab._reserve(name, expected, run_id, arm, 'meta_generate', '__meta__', inputs, manifest, generation=True, invocation=invocation)
        result = lab._invoke(name, intent, method, inputs, files) if new else intent
        require(result['status'] == 'completed', 'Meta generator did not actually complete', 'NOT_RUN')
        source = Path(result['receipt']['run_dir']) / 'result/artifacts/generated'
        produced = tree(source, Limits(**spec['limits']))
        expected = {a['path'][len('generated/'):]: {'sha256': a['sha256'], 'bytes': a['bytes']}
            for a in result['receipt']['artifacts'] if a['path'].startswith('generated/')}
        require(produced == expected, 'Generated H differs from the actual accepted generator output', 'CONTENT_CHANGED')
        initial = spec['starting_H_manifest']
        require(set(produced) == set(initial) and spec.get('execution_entrypoint', spec['entrypoint']) in produced, 'Generated H must preserve starting-H file boundary', 'CHANGE_SCOPE')
        # H and M may have different file names; explicit generated-H editable
        # names are frozen in the meta contract, never inferred from outputs.
        changed = {p for p in initial if initial[p] != produced[p]}
        require(changed <= set(spec.get('generated_H_editable', [])), 'Generated H changed outside frozen H surface', 'CHANGE_SCOPE')
        destination = lab._dir(name) / 'generated' / arm
        destination.parent.mkdir(exist_ok=True)
        if destination.exists():
            require(tree(destination, Limits(**spec['limits'])) == produced, 'Existing immutable generated H differs', 'CONTENT_CHANGED')
        else:
            stage = destination.with_name(arm + '.pending-' + uuid.uuid4().hex)
            _snapshot(source, stage, Limits(**spec['limits']))
            try:
                stage.rename(destination)
            except OSError:
                require(destination.exists() and tree(destination, Limits(**spec['limits'])) == produced,
                    'Generated H publication failed or conflicts', 'CONTENT_CHANGED')
            finally:
                if stage.exists(): shutil.rmtree(stage)
        data = {'arm': arm, 'path': str(destination.relative_to(lab.state.root)), 'files': produced, 'sha256': hashed(produced),
            'generation_run': run_id, 'generator_sha256': hashed(files), 'starting_H_sha256': hashed(initial),
            'common_input_sha256': hashed(manifest), 'depth': 2, 'recursive_generation_allowed': False}
        lab._auto(name, 'generated', data, 'Actual isolated meta generator produced H')
        return data

    def execute_generated(self, expected, run_id, arm, case_id):
        return self.lab.run(self.name, expected, run_id, arm, case_id)

    def compare(self):
        view = self.lab.get(self.name)
        require('H0' in view['generations'] and view['candidates'], 'Both meta arms must generate independently')
        values = list(view['generations'].values())
        require(len({v['starting_H_sha256'] for v in values}) == 1 and len({v['common_input_sha256'] for v in values}) == 1,
            'M0/M1 starting H and visible cases differ')
        return {'generations': values, 'evaluated_H': self.lab.summary(self.name),
            'executor': view['contract']['spec']['environment']['executor'],
            'scope': 'Actual independent M generation and H execution under one frozen executor, common E and allowances; no domain meta-improvement inferred'}
