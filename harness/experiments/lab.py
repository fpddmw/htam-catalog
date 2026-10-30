"""Composable experiment entry; no background scheduler or parallel state store."""
from copy import deepcopy
import inspect
import json
from pathlib import Path

from ..common import require, digest, HarnessError
from ..experiences import ExperienceRegistry
from .journal import Journal, hashed, human, safe_path
from .contracts import Contracts
from .execution import Execution, OfflineExecutor
from .evaluation import Evaluation
from .publishing import Publishing
from .generation import Generation


class ExperimentLab(Journal, Contracts, Execution, Evaluation, Publishing, Generation):
    def __init__(self, state, directory, *, executor=None, executor_verification=None, recovery_only=False):
        self.state = state
        self.directory = safe_path(state.root, str(directory))
        self.executor = executor or OfflineExecutor()
        require(type(recovery_only) is bool, 'Recovery mode is a trusted host boolean')
        self._recovery_only = recovery_only
        self._executor_verification = deepcopy(executor_verification)
        if type(self.executor) is not OfflineExecutor:
            # Host code supplies an adapter, not a user-provided receipt or an
            # import path. Its exact reviewed implementation and isolation
            # evidence must be approved before even preparing a contract.
            require(executor_verification and all(callable(getattr(self.executor, key, None)) for key in ('identity', 'execute', 'verify_receipt')),
                'External executor needs verified host adapter', 'EXECUTOR_UNVERIFIED')
            verification = executor_verification
            if self.executor.identity().get('budget_unit') == 'native_turn_attempt':
                require(callable(getattr(self.executor, 'recover_receipt', None)), 'Native executor needs no-resend recovery', 'EXECUTOR_UNVERIFIED')
                require(verification.get('executor_identity_sha256') == hashed(self.executor.identity()),
                    'Native verification must bind the exact reviewed host configuration and bounded authorization scope', 'EXECUTOR_UNVERIFIED')
            implementation = Path(inspect.getfile(type(self.executor))).resolve()
            require(verification.get('implementation_sha256') == digest(implementation) and verification.get('isolation_evidence'),
                'Executor implementation/isolation evidence missing or changed', 'EXECUTOR_UNVERIFIED')
            if not recovery_only:
                self._verify_executor()

    def _verify_executor(self):
        """Fresh authorization for new work; recovery/owned cancellation omit it."""
        require(not self._recovery_only, 'Recovery-only host cannot start new experiment work', 'EXECUTOR_UNVERIFIED')
        if type(self.executor) is OfflineExecutor:
            return
        verification = self._executor_verification
        implementation = Path(inspect.getfile(type(self.executor))).resolve()
        require(verification['implementation_sha256'] == digest(implementation), 'Executor implementation changed', 'EXECUTOR_UNVERIFIED')
        if self.executor.identity().get('budget_unit') == 'native_turn_attempt':
            require(verification.get('executor_identity_sha256') == hashed(self.executor.identity()), 'Native host identity changed', 'EXECUTOR_UNVERIFIED')
        for ref in verification['isolation_evidence']:
            require(digest(safe_path(self.state.root, ref['path'])) == ref['sha256'], 'Executor verification evidence changed', 'EXECUTOR_UNVERIFIED')
        if verification.get('construction_admission'):
            from .construction import construction_authority
            subject = hashed({k: v for k, v in verification.items() if k not in ('approval', 'construction_admission')})
            construction_authority(self.state, verification['construction_admission'], subject, self.executor.identity(),
                subject_kind='executor_verification', lab_root=self.directory)
        else:
            human(self.state, verification['work'], verification['approval'], hashed({k: v for k, v in verification.items() if k != 'approval'}))

    def prepare_evaluation_revision(self, parent, supplied):
        old = self.get(parent)['contract']; supplied = deepcopy(supplied)
        supplied['evaluation_reassessment_of'] = parent
        self._allow_e_reassessment = parent
        try:
            prepared = self.prepare(supplied)
        finally:
            self._allow_e_reassessment = None
        spec = prepared['spec']
        require(spec['E'] != old['spec']['E'], 'E fork must change a distinct exact E version')
        for key in ('U', 'A', 'H', 'M', 'baseline_dir', 'targets'):
            require(spec[key] == old['spec'][key], 'E revision cannot silently change ' + key, 'IMMUTABLE_CONTRACT')
        require(spec['cases'] == old['spec']['cases'], 'E revision must pair reevaluate the same cases; old results are retained')
        return prepared

    def fork_evaluation(self, name, expected, parent, prepared, approval, source):
        old = self.get(parent)['contract']
        require(self.prepare_evaluation_revision(parent, prepared['spec']) == prepared, 'Prepared E revision changed')
        spec = prepared['spec']
        # Previously evaluated final families are deliberately reused only for
        # paired E reassessment. They stay contaminated for method optimization.
        self._allow_e_reassessment = parent
        try:
            result = self.freeze(name, expected, prepared, approval, source)
        finally:
            self._allow_e_reassessment = None
        self._auto(name, 'evaluation_revision', {'parent': parent, 'previous_E_sha256': hashed(old['spec']['E']),
            'new_E_sha256': hashed(spec['E']), 'old_results_retained': True,
            'requirement': 'Rerun H0 and candidate under new E; previous scores are never copied'}, source)
        return self.get(name)

    def lesson(self, name, expected, mechanism, body, source):
        view = self.get(name)
        require(view['end'] or view['publication'], 'Useful outcome required before recording transferable lesson')
        require(body.get('claim') and body.get('applies') is not None and body.get('evidence'), 'Lesson needs mechanism, applicability and original evidence')
        registry = ExperienceRegistry(self.state); prior = registry.get(mechanism, required=False)
        candidate = registry.propose(mechanism, prior.get('revision', 0), {**body, 'work': view['contract']['spec']['work']}, source)
        self._append(name, expected, 'lesson', {'mechanism': mechanism, 'candidate_sha256': candidate['candidate_sha256'],
            'stage': 'candidate_only', 'publication': 'Original method requires exact separate adoption scope'}, source)
        return candidate

    def call(self, operation, request):
        """Closed CLI dispatch; file fields never become code/module imports."""
        require(not self._recovery_only or operation in ('get', 'summary', 'budget', 'reconcile'),
            'Recovery-only host exposes read projections and original receipt reconciliation only', 'EXECUTOR_UNVERIFIED')
        methods = {'prepare', 'freeze', 'get', 'summary', 'budget', 'add_candidate', 'run', 'reconcile', 'charge',
            'evaluate', 'semantic_review', 'lock', 'final_assessment', 'finish', 'prepare_publication',
            'approve_publication', 'publish', 'recover', 'rollback', 'resolve_method', 'bind_method',
            'lesson', 'prepare_evaluation_revision', 'fork_evaluation', 'prepare_generation', 'propose_candidate', 'release_history'}
        if operation in ('meta_generate', 'meta_compare'):
            from .meta import MetaTrial
            body = dict(request); meta = MetaTrial(self, body.pop('name'))
            method = meta.generate if operation == 'meta_generate' else meta.compare
        else:
            require(operation in methods and isinstance(request, dict), 'Unknown experiment operation')
            method, body = getattr(self, operation), request
        try:
            inspect.signature(method).bind(**body)
        except TypeError as error:
            raise HarnessError('Invalid experiment request fields: ' + str(error)) from error
        return method(**body)
