"""Intent-first original-entry publication, readback, recovery and scope rollback."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from ..assets import AssetRegistry
from ..common import digest, require
from ..experiences import ExperienceRegistry, context_match
from .journal import hashed, human, safe_path
from .contracts import tree


class Publishing:
    @contextmanager
    def _publication_writer(self):
        # One existing business store is the publication coordination scope.
        # flock releases on crash; the durable intent survives in its journal.
        require(not self.state.readonly, 'Read-only experiment cannot publish or create locks')
        path = self.state.directory / 'experiments.publish.lock'
        require(not path.is_symlink(), 'Publication lock symlink rejected')
        with path.open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _candidate_current(self, view):
        arm = view['lock']['arm']; candidate = view['candidates'][arm]
        path, files = self._method(view['id'], view, arm, generated=False)
        require(tree(path) == files and candidate['sha256'] == view['lock']['candidate_sha256'], 'Locked patch content changed', 'CONTENT_CHANGED')
        replaced = {c['replaces'] for c in view['candidates'].values() if c.get('replaces')}
        active = [a for a in view['candidates'] if a not in replaced]
        require(hashed(self._matrix(view['id'], ['H0', *active], ['dev', 'select'])) == view['lock']['matrix_sha256'],
            'Selection evidence changed after lock', 'EVALUATION_CHANGED')
        return candidate, path

    def prepare_publication(self, name, expected, asset_id, version, scope, source, *, allow_rollback=False):
        view = self.get(name); self._active(view)
        final = self.final_assessment(name)
        require(final['passed'], 'Final test/no-gain result does not support publication', 'HOLDOUT_FAILED')
        require(isinstance(scope, dict) and set(scope) == {'work', 'purpose'} and scope.get('work') == view['contract']['spec']['work'] and scope.get('purpose'), 'Publication requires exact work/purpose scope; process/tool conditions remain in applies')
        require(context_match(view['contract']['spec'].get('applies', {}), scope)['state'] != 'inapplicable', 'Publication purpose conflicts with candidate applicability')
        candidate, path = self._candidate_current(view); spec = view['contract']['spec']
        require(not spec.get('evaluation_reassessment_of'), 'Paired E reassessment does not authorize a new H/M release on exposed cases', 'HOLDOUT_CONTAMINATED')
        registry = AssetRegistry(self.state); head, before, _ = registry._row(asset_id, spec[spec['component']]['version'])
        if head['current_version'] != before['version']:
            route = next((r for r in registry.method_selections(asset_id) if r['scope'] == scope), None)
            require(route and route['version'] == before['version'] and route['authority'] == 'current' and route['state'] == 'active',
                'Formal baseline is neither global head nor exact current scoped selection', 'REVISION_CONFLICT')
        require(not self.state.db.execute('SELECT 1 FROM asset_versions WHERE asset_id=? AND version=?', (asset_id, version)).fetchone(), 'Method version is immutable/already exists')
        proposal_path = self._dir(name) / 'adoption' / (candidate['sha256'] + '.evaluation.json')
        if not proposal_path.exists():
            self._write_json(proposal_path, {'experiment': name, 'component': spec['component'],
                'development': self._matrix(name, ['H0', candidate['arm']], ['dev', 'select']), 'final': final,
                'meaning': 'Construction mechanics only; no domain improvement claimed'})
        experiences = ExperienceRegistry(self.state)
        experience = experiences.get(candidate['experience_id'])
        body = deepcopy(experience['candidate'])
        reference = {'path': str(proposal_path.relative_to(self.state.root))}
        if not any(ref.get('path') == reference['path'] for ref in body['evidence']):
            body['evidence'].append(reference)
            experience = experiences.propose(candidate['experience_id'], experience['revision'], body, source)
        targets = []
        for name_in_bundle in candidate['changed']:
            original = view['contract']['targets'][name_in_bundle]
            target = safe_path(self.state.root, original['path'])
            require(digest(target) == original['before_sha256'], 'Original method was changed concurrently', 'PUBLICATION_CONFLICT')
            targets.append({'name': name_in_bundle, **original, 'after_sha256': candidate['files'][name_in_bundle]['sha256'],
                'new_file': str((path / name_in_bundle).relative_to(self.state.root)),
                'before_file': str((self._dir(view['id']) / 'baseline' / name_in_bundle).relative_to(self.state.root))})
        plan = {'experiment': view['id'], 'contract_sha256': view['contract']['sha256'], 'component': spec['component'],
            'arm': candidate['arm'], 'candidate_sha256': candidate['sha256'], 'experience_id': candidate['experience_id'],
            'experience_sha256': experience['candidate_sha256'], 'experience_revision': experience['revision'],
            'asset_id': asset_id, 'version': version, 'previous_version': before['version'],
            'asset_global_head': head['current_version'], 'asset_expected': head['revision'],
            'scope': scope, 'targets': targets, 'final_sha256': final['sha256'],
            'rollback': {'authorized': bool(allow_rollback), 'scope': scope, 'version': before['version'], 'partial_restore': bool(allow_rollback)},
            'limitations': spec.get('limitations', [])}
        plan['sha256'] = hashed(plan)
        plan['path'] = self._write_json(self._dir(view['id']) / 'adoption' / (plan['sha256'] + '.plan.json'), plan)
        self._append(view['id'], expected, 'publication_plan', plan, source)
        return plan

    def approve_publication(self, name, expected, approval, source):
        view = self.get(name); plan = view['publication_plan']; require(plan, 'Concrete publication plan required')
        self._candidate_current(view)
        bound = human(self.state, view['contract']['spec']['work'], approval, plan['sha256'])
        experiences = ExperienceRegistry(self.state); exp = experiences.get(plan['experience_id'])
        require(exp['status'] == 'active' and not exp['relations'] and exp['candidate_sha256'] == plan['experience_sha256'] and
            exp['revision'] == plan['experience_revision'], 'Experience candidate state/content changed', 'CANDIDATE_INACTIVE')
        exp_approval = {**bound, 'candidate_sha256': exp['candidate_sha256'], 'reviewer': source,
            'reason': 'Exact B06 plan, method target and allowed scope approved; publication still pending'}
        experiences.approve(exp['id'], exp['revision'], exp_approval, source)
        data = {'plan_sha256': plan['sha256'], 'approval': bound}
        self._append(name, expected, 'publication_approved', data, source)
        return data

    def _approved_plan(self, name):
        view = self.get(name); plan = view['publication_plan']; approval = view['publication_approval']
        if not view['publication']:
            self._active(view)
        require(plan and approval and approval['plan_sha256'] == plan['sha256'], 'Current plan has no exact approval', 'APPROVAL_HASH')
        self._check_plan_bytes(plan)
        human(self.state, view['contract']['spec']['work'], approval['approval'], plan['sha256'])
        candidate, path = self._candidate_current(view)
        experience = ExperienceRegistry(self.state).get(plan['experience_id'])
        require(experience['status'] == 'active' and not experience['relations'] and experience['candidate_sha256'] == plan['experience_sha256'] and
            experience['stage'] in ('approved_pending_publication', 'published'), 'Pending approval/current active experience no longer valid', 'CANDIDATE_INACTIVE')
        require(self.final_assessment(name)['sha256'] == plan['final_sha256'], 'Evaluation/review changed after adoption plan', 'EVALUATION_CHANGED')
        if not view['publication']:
            head, snapshot, _ = AssetRegistry(self.state)._row(plan['asset_id'])
            require(head['revision'] == plan['asset_expected'] and snapshot['version'] == plan['asset_global_head'],
                'Formal asset changed while publication was pending', 'REVISION_CONFLICT')
        return view, plan

    def _check_plan_bytes(self, plan):
        payload = {k: v for k, v in plan.items() if k not in ('path', 'sha256')}
        require(hashed(payload) == plan['sha256'] and json.loads(safe_path(self.state.root, plan['path']).read_text()) ==
            {k: v for k, v in plan.items() if k != 'path'}, 'Concrete adoption proposal content changed', 'APPROVAL_HASH')

    def _restore_authority(self, name):
        view = self.get(name); plan = view['publication_plan']; approval = view['publication_approval']
        require(plan and approval and approval['plan_sha256'] == plan['sha256'], 'Exact restore authority missing')
        self._check_plan_bytes(plan)
        human(self.state, view['contract']['spec']['work'], approval['approval'], plan['sha256'])
        return view, plan

    def _replace_target(self, target, data, expected):
        target = safe_path(self.state.root, str(target))
        # Recheck at the actual replace boundary while holding the shared
        # writer lock. Do not overwrite a peer's unrecognized bytes.
        require(target.is_file() and digest(target) == expected, 'Concurrent target change at replace boundary', 'PUBLICATION_CONFLICT')
        mode = stat.S_IMODE(target.stat().st_mode)
        fd, temporary = tempfile.mkstemp(prefix='.htam-publish-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            require(digest(target) == expected and not target.is_symlink(), 'Target changed before atomic replace', 'PUBLICATION_CONFLICT')
            os.replace(temporary, target)
            parent = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _apply_files(self, name, plan, restore=False):
        for item in plan['targets']:
            with self.state.transaction():
                # B02 scope/source cannot change during this exact write
                # boundary. A crash still leaves the earlier durable intent.
                self._restore_authority(name) if restore else self._approved_plan(name)
                target = safe_path(self.state.root, item['path'])
                actual = digest(target)
                before, after = item['before_sha256'], item['after_sha256']
                require(actual in (before, after), 'Target contains an unrelated concurrent change', 'PUBLICATION_CONFLICT')
                desired = before if restore else after
                source = safe_path(self.state.root, item['before_file'] if restore else item['new_file'])
                require(digest(source) == desired, 'Approved immutable publication/rollback content changed', 'CONTENT_CHANGED')
                if actual != desired:
                    self._replace_target(target, source.read_bytes(), actual)
                require(digest(target) == desired, 'Publication readback failed', 'PUBLICATION_READBACK')
                self._auto(name, 'file_readback', {'plan_sha256': plan['sha256'], 'path': item['path'],
                    'sha256': desired, 'direction': 'restore' if restore else 'publish'}, 'Actual original-entry readback')

    def _register_version(self, name, view, plan):
        registry = AssetRegistry(self.state)
        provenance = [{'path': plan['path']}]
        provenance += [{'path': p['path'], 'sha256': p['after_sha256']} for p in plan['targets']]
        provenance += [{'path': p['new_file'], 'sha256': p['after_sha256']} for p in plan['targets']]
        body = {'version': plan['version'], 'type': 'method', 'title': plan['component'] + ' scoped method ' + name,
            'description': 'Exact approved original-entry patch with immutable release files', 'provenance': provenance,
            'uses': [{'purpose': plan['scope']['purpose'], 'state': 'conditional', 'scope': plan['scope'],
                'evidence': [{'path': plan['path']}]}], 'limitations': plan['limitations'] + ['Limited to exact approved scope; subsequent field effectiveness remains to be observed'],
            'depends_on': [], 'status': 'conditional', 'applies': view['contract']['spec'].get('applies', {}), 'publication': {'experiment': name, 'plan_sha256': plan['sha256'],
                'experience_id': plan['experience_id'], 'experience_sha256': plan['experience_sha256'],
                'previous_version': plan['previous_version'], 'immutable_bundle': str((self._dir(name) / 'candidates' / plan['arm']).relative_to(self.state.root)),
                'files': view['candidates'][plan['arm']]['files'], 'bundle_sha256': view['candidates'][plan['arm']]['sha256']}}
        registry.put(plan['asset_id'], plan['asset_expected'], body, 'Actual B06 approved original-entry publication')

    def _selection(self, name, view, plan, restore, reason, state='active'):
        files = view['contract']['baseline_manifest'] if restore else view['candidates'][plan['arm']]['files']
        bundle = self._dir(name) / 'baseline' if restore else self._dir(name) / 'candidates' / plan['arm']
        data = {'schema': 'htam.method-selection/v1', 'scope': plan['scope'], 'state': state,
            'version': plan['previous_version'] if restore else plan['version'],
            'previous_version': plan['version'] if restore else plan['previous_version'],
            'bundle': str(bundle.relative_to(self.state.root)), 'files': files, 'sha256': hashed(files),
            'experiment': name, 'experience_id': plan['experience_id'], 'experience_sha256': plan['experience_sha256'],
            'plan_sha256': plan['sha256'], 'approval': view['publication_approval']['approval'], 'reason': reason}
        self.state.observation('asset-selection:' + plan['asset_id'], 'note', {'method_selection': data}, reason)

    def publish(self, name, expected, source):
        with self._publication_writer():
            view, plan = self._approved_plan(name)
            require(not view['publication'], 'Method already published; no duplicate write')
            intents = [r for r in view['history'] if r['kind'] == 'publication_intent' and r['data']['plan_sha256'] == plan['sha256']]
            if not intents:
                self._append(name, expected, 'publication_intent', {'plan_sha256': plan['sha256'], 'plan': plan,
                    'approval': view['publication_approval'], 'direction': 'publish'}, source)
            else:
                require(view['revision'] == expected, 'Stale recovery revision', 'REVISION_CONFLICT')
            self._apply_files(name, plan)
            with self.state.transaction():
                self._approved_plan(name)
                self._register_version(name, self.get(name), plan)
                self._selection(name, self.get(name), plan, False, 'Exact approved publication selected for this scope')
                self._auto(name, 'publication_complete', {'plan_sha256': plan['sha256'], 'asset_id': plan['asset_id'],
                    'version': plan['version'], 'scope': plan['scope'], 'readback': 'all_exact',
                    'bundle': str((self._dir(name) / 'candidates' / plan['arm']).relative_to(self.state.root))}, source)
            return self.get(name)['publication']

    def recover(self, name, expected, direction, source):
        require(direction in ('resume', 'restore'), 'Recovery is resume or exact authorized restore')
        if direction == 'resume':
            return self.publish(name, expected, source)
        with self._publication_writer():
            view, plan = self._restore_authority(name)
            require(not view['publication'] and plan['rollback']['partial_restore'], 'Partial restore requires its explicit preauthorization')
            require(any(r['kind'] == 'publication_intent' for r in view['history']), 'No interrupted publication intent')
            self._append(name, expected, 'restore_intent', {'plan_sha256': plan['sha256']}, source)
            self._apply_files(name, plan, restore=True)
            self._auto(name, 'ended', {'outcome': 'publication_aborted_restored', 'plan_sha256': plan['sha256'],
                'reason': 'Exact approved preimages restored; no asset version registered'}, source)
            return self.get(name)

    def rollback(self, name, expected, scope, reason, source):
        with self._publication_writer():
            view, plan = self._restore_authority(name)
            require(view['publication'] and not view['rollback'] and plan['rollback']['authorized'] and scope == plan['rollback']['scope'],
                'Rollback needs published version and exact preauthorized scope', 'ROLLBACK_SCOPE')
            with self.state.transaction():
                self._append(name, expected, 'rollback_intent', {'plan_sha256': plan['sha256'], 'scope': scope, 'reason': reason}, source)
                self._selection(name, view, plan, False, 'Scope paused during exact rollback', state='paused_pending_rollback')
            self._apply_files(name, plan, restore=True)
            impact = AssetRegistry(self.state).impact(plan['asset_id'], plan['version'])
            data = {'plan_sha256': plan['sha256'], 'scope': scope, 'asset_id': plan['asset_id'],
                'from_version': plan['version'], 'to_version': plan['previous_version'], 'reason': reason,
                'affected_registered_uses': impact['uses'], 'coverage': impact['coverage'],
                'inflight': 'Existing bindings keep immutable bytes/version; scoped future selection returns the prior version; scientific artifacts are retained'}
            with self.state.transaction():
                self._restore_authority(name)
                self._selection(name, self.get(name), plan, True, reason)
                self._auto(name, 'rollback_complete', data, source)
            return data

    def resolve_method(self, name, scope, context=None):
        view, plan = self._restore_authority(name)
        require(view['publication'] and plan['scope'] == scope, 'No published method for exact scope')
        require(context_match(view['contract']['spec'].get('applies', {}), {**(context or {}), **scope})['state'] == 'declared_match',
            'Exact method process/tool/condition context remains unresolved', 'METHOD_SCOPE_UNRESOLVED')
        require(view['rollback'] or not any(r['kind'] == 'rollback_intent' for r in view['history']), 'Scope paused while rollback is unresolved', 'ROLLBACK_UNKNOWN')
        rollback = view['rollback'] and view['rollback']['scope'] == scope
        version = plan['previous_version'] if rollback else plan['version']
        path = self._dir(name) / 'baseline' if rollback else self._dir(name) / 'candidates' / plan['arm']
        files = view['contract']['baseline_manifest'] if rollback else view['candidates'][plan['arm']]['files']
        require(tree(path) == files, 'Pinned method version changed', 'CONTENT_CHANGED')
        return {'asset_id': plan['asset_id'], 'version': version, 'scope': scope, 'bundle': str(path.relative_to(self.state.root)),
            'files': files, 'sha256': hashed(files), 'selection': 'scoped_rollback' if rollback else 'approved_conditional'}

    def bind_method(self, name, task_id, requirement, purpose, source, context=None):
        view = self.get(name); scope = view['publication_plan']['scope']; task = self.state.task(task_id)
        require(task['work_id'] == scope['work'] and purpose == scope['purpose'], 'Method use differs from approved work/purpose scope')
        binding_key = 'method-use:' + task_id + ':' + view['publication_plan']['asset_id']
        rows = self.state.observations(binding_key)
        if rows:
            # Resuming a task never follows an installation/rollback implicitly.
            pinned = rows[0]['body']['method_binding']
            require(pinned['scope'] == scope and tree(safe_path(self.state.root, pinned['bundle'])) == pinned['files'],
                'Existing task method pin scope/content differs; explicit checkpoint review required', 'METHOD_PINNED')
            return pinned
        binding = self.resolve_method(name, scope, context=context)
        with self.state.transaction():
            use = AssetRegistry(self.state).use(task['work_id'], requirement, binding['asset_id'], binding['version'],
                {'description': purpose, 'state': 'reused', 'task': task_id}, source)
            binding = {**binding, 'task': task_id, 'asset_use_id': use['id'], 'meaning': 'Use these immutable files; current original entry may change independently'}
            self.state.observation(binding_key, 'note', {'method_binding': binding}, source)
        return binding
