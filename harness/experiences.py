"""Versioned experience projections over the existing observations journal.

Authoritative payload: observations(kind='note').body.experience, schema
htam.experience/v1. Views are rebuilt; no knowledge database or method copy is
created. Explicit mechanism IDs/relations, never lexical similarity, merge work.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import fnmatch
import hashlib
import json

from .common import HarnessError, digest, encoded, local_path, now, require

SCHEMA = 'htam.experience/v1'
CONTEXT_KEYS = {'work', 'task', 'purpose', 'process', 'tool_versions', 'conditions', 'mechanisms', 'asset_versions'}


def context_match(applies, context):
    """Declared exact applicability; missing context remains explicitly unknown."""
    require(isinstance(context, dict) and set(context) <= CONTEXT_KEYS, 'Unknown retrieval context field')
    applies = {} if applies is None else applies
    require(isinstance(applies, dict) and set(applies) <= {'work', 'purposes', 'processes', 'tool_versions', 'conditions'}, 'Unknown applicability field')
    for field in ('purposes', 'processes', 'conditions'):
        require(isinstance(applies.get(field, []), list) and all(isinstance(v, str) and v for v in applies.get(field, [])), 'Applicability ' + field + ' must be a text list')
    require(isinstance(applies.get('tool_versions', {}), dict), 'Applicability tool_versions must be an object')
    require(isinstance(context.get('tool_versions', {}), dict) and isinstance(context.get('asset_versions', {}), dict), 'Context versions must be objects')
    for field in ('work', 'task', 'purpose', 'process'):
        require(field not in context or isinstance(context[field], str) and bool(context[field]), 'Context ' + field + ' must be text')
    require('work' not in applies or isinstance(applies['work'], str) and bool(applies['work']), 'Applicability work must be text')
    for field in ('conditions', 'mechanisms'):
        require(isinstance(context.get(field, []), list) and all(isinstance(v, str) and v for v in context.get(field, [])), 'Context ' + field + ' must be a text list')
    for tool, versions in applies.get('tool_versions', {}).items():
        require(isinstance(tool, str) and bool(tool) and isinstance(versions, list) and bool(versions) and all(isinstance(v, str) and v for v in versions), 'Applicability tool versions must be nonempty text lists')
    for field in ('tool_versions', 'asset_versions'):
        require(all(isinstance(k, str) and bool(k) and isinstance(v, str) and bool(v) for k, v in context.get(field, {}).items()), 'Context ' + field + ' must map names to exact text versions')
    mismatches, missing = [], []
    for field, key in [('purposes', 'purpose'), ('processes', 'process')]:
        values = applies.get(field, [])
        if values and not context.get(key):
            missing.append(key)
        elif values and context[key] not in values:
            mismatches.append(key)
    for tool, versions in applies.get('tool_versions', {}).items():
        actual = context.get('tool_versions', {}).get(tool)
        if actual is None:
            missing.append('tool_versions.' + tool)
        elif actual not in versions:
            mismatches.append('tool_versions.' + tool)
    conditions = set(applies.get('conditions', []))
    if conditions and 'conditions' not in context:
        missing.append('conditions')
    elif not conditions <= set(context.get('conditions', [])):
        mismatches.append('conditions')
    if applies.get('work') and context.get('work') != applies['work']:
        (missing if not context.get('work') else mismatches).append('work')
    return {'state': 'inapplicable' if mismatches else 'needs_context' if missing else 'declared_match',
            'mismatches': mismatches, 'missing': missing}


def bounded_selection(mandatory, optional, *, limit=8, budget=16000):
    require(type(limit) is int and 0 <= limit <= 50, 'Selection limit must be 0..50')
    require(type(budget) is int and 512 <= budget <= 1000000, 'Selection budget must be 512..1000000 bytes')
    required_bytes = len(encoded(mandatory).encode())
    if required_bytes > budget:
        # Keep actual constraint/counterexample content ahead of journal and
        # explanation wrappers. Any reduction remains explicitly incomplete.
        semantic = ('kind', 'id', 'mechanism', 'task', 'constraint', 'requires',
            'limitations', 'scope', 'description', 'missing', 'required_load')
        compact = [{key: item[key] for key in semantic if key in item} for item in mandatory]
        identity = {'count': len(mandatory), 'bytes': required_bytes,
            'sha256': hashlib.sha256(encoded(mandatory).encode()).hexdigest(),
            'action': 'Repeat retrieve with the same work/task/context and a larger budget'}
        retained = []
        for item in compact:
            if len(encoded(retained + [item]).encode()) <= budget:
                retained.append(item)
                continue
            # A long first constraint still carries useful semantics. Clipped
            # text is labelled; it never claims the whole condition was loaded.
            excerpt = {'kind': item.get('kind'), 'content_excerpt': '', 'content_truncated': True}
            allowance = budget - len(encoded(retained + [excerpt]).encode()) - 24
            if allowance > 40:
                excerpt['content_excerpt'] = encoded(item).encode()[:allowance].decode('utf-8', errors='ignore')
                while len(encoded(retained + [excerpt]).encode()) > budget and excerpt['content_excerpt']:
                    excerpt['content_excerpt'] = excerpt['content_excerpt'][:-16]
                retained.append(excerpt)
        return {'mandatory': retained, 'selected': [], 'context_complete': False,
            'required_load': identity, 'mandatory_projection': 'semantic_excerpt; full set requires explicit load',
            'omitted': {'mandatory': len(mandatory) - len(retained), 'optional': len(optional)},
            'budget_bytes': budget, 'loaded_payload_bytes': len(encoded(retained).encode())}
    selected, used = [], required_bytes
    for item in optional:
        cost = len(encoded(item).encode())
        if len(selected) >= limit or used + cost > budget:
            continue
        selected.append(item); used += cost
    return {'mandatory': mandatory, 'selected': selected, 'context_complete': True, 'required_load': None,
            'omitted': {'mandatory': 0, 'optional': len(optional) - len(selected)},
            'budget_bytes': budget, 'loaded_payload_bytes': used}


class ExperienceRegistry:
    def __init__(self, state, can_read=None):
        self.state, self.db, self.root = state, state.db, state.root
        self.can_read = can_read or self._local_read

    def _local_read(self, path):
        try:
            from .discovery import DEFAULT_EXCLUDES
            value = local_path(self.root, path)
            hard = [pattern for pattern in DEFAULT_EXCLUDES if pattern != '.harness-local']
            return not any(fnmatch.fnmatchcase(part.lower(), pattern.lower())
                for part in value.relative_to(self.root).parts for pattern in hard)
        except (HarnessError, OSError):
            return False

    def _records(self, identifier=None):
        sql = "SELECT * FROM observations WHERE kind='note'"
        args = ()
        if identifier is not None:
            sql += ' AND asset=?'; args = ('experience:' + identifier,)
        records = []
        for row in self.db.execute(sql + ' ORDER BY id', args):
            value = json.loads(row['body']).get('experience')
            if isinstance(value, dict) and value.get('schema') == SCHEMA:
                records.append({**dict(row), 'experience': value})
        return records

    def resolve_context(self, context):
        context_match({}, context)
        value = deepcopy(context)
        if value.get('task'):
            task = self.state.task(value['task'])
            require(not value.get('work') or value['work'] == task['work_id'], 'Retrieval task belongs to another work', 'INPUT_SCOPE_UNRESOLVED')
            value['work'] = task['work_id']
        return value

    def _revision(self, identifier):
        records = self._records(identifier)
        return records[-1]['experience']['revision'] if records else 0

    def _active_candidate(self, identifier, sha):
        """Raw identity/lifecycle check for existing method routes, without recursive get()."""
        history = self._records(identifier)
        current = next((r['experience'] for r in reversed(history) if r['experience']['record_type'] == 'candidate'), None)
        require(current and current['candidate_sha256'] == sha, 'Experience candidate changed')
        lifecycle = next((r['experience']['status'] for r in reversed(history) if r['experience']['record_type'] == 'lifecycle'), 'active')
        retracted = {r['experience']['relation_observation_id'] for r in history if r['experience']['record_type'] == 'relation_retracted'}
        require(lifecycle == 'active' and not any(r['experience']['record_type'] == 'relation' and r['id'] not in retracted for r in history), 'Experience retired or related to a replacement')
        candidate = current['candidate']
        if candidate.get('valid_until'):
            require(datetime.fromisoformat(candidate['valid_until']) > datetime.fromisoformat(now()), 'Experience validity expired')
        return candidate

    def _append(self, identifier, expected, kind, body, source):
        require(isinstance(identifier, str) and identifier.strip() and len(identifier) <= 160, 'Mechanism ID required')
        require(type(expected) is int and expected >= 0, 'Experience expected revision required')
        actual = self._revision(identifier)
        require(actual == expected, 'Stale experience revision', 'REVISION_CONFLICT')
        value = {'schema': SCHEMA, 'mechanism': identifier, 'revision': actual + 1,
                 'record_type': kind, **deepcopy(body)}
        observation_id = self.state.observation('experience:' + identifier, 'note', {'experience': value}, source)
        return {'observation_id': observation_id, **value}

    def _ref(self, supplied):
        require(isinstance(supplied, dict), 'Experience evidence reference must be an object')
        ref = deepcopy(supplied)
        if 'path' in ref:
            require(self.can_read(ref['path']), 'Evidence is outside the trusted local read scope', 'ACCESS_SCOPE')
            path = local_path(self.root, ref['path'])
            require(path.is_file(), 'Experience evidence file unavailable')
            actual = digest(path)
            require(not ref.get('sha256') or ref['sha256'] == actual, 'Experience evidence changed')
            ref.update(path=str(path.relative_to(self.root)), sha256=actual)
        elif 'event_seq' in ref:
            row = self.db.execute('SELECT seq,work_id,kind,source,at FROM events WHERE seq=?', (ref['event_seq'],)).fetchone()
            require(row is not None, 'Original experience event unavailable')
            ref['original_event'] = dict(row)
        elif 'observation_id' in ref:
            row = self.db.execute('SELECT id,asset,kind,source,at FROM observations WHERE id=?', (ref['observation_id'],)).fetchone()
            require(row is not None, 'Original experience observation unavailable')
            ref['original_observation'] = dict(row)
        else:
            raise HarnessError('Evidence needs path, event_seq or observation_id')
        return ref

    def propose(self, identifier, expected, body, source):
        require(isinstance(body, dict) and all(body.get(k) for k in ('work', 'title', 'claim', 'evidence')), 'Candidate needs work/title/claim/evidence')
        self.state.get(body['work'])
        require(isinstance(body['evidence'], list), 'Candidate evidence must be a list')
        context_match(body.get('applies', {}), {})
        with self.state.transaction():
            previous = self.get(identifier, required=False)
            require(not previous or previous.get('candidate') is not None,
                'Cannot revise an experience through a restricted history projection', 'ACCESS_SCOPE')
            for historical in previous.get('history', []):
                parts = historical.get('candidate', {})
                require(historical.get('visibility') != 'restricted' and not any(
                    part.get('visibility') == 'restricted' for part in parts.get('counterexamples', []) + parts.get('method_targets', [])),
                    'Cannot revise an experience through a restricted history projection', 'ACCESS_SCOPE')
            content = deepcopy(body)
            content.setdefault('access', 'local_workspace')
            require(content['access'] in ('local_workspace', 'restricted'), 'Experience access is local_workspace or restricted')
            prior_candidates = [item['candidate'] for item in previous.get('history', []) if item['record_type'] == 'candidate']
            known = [ref for item in prior_candidates for ref in item.get('evidence', []) + item.get('method_targets', [])]
            known += [ref for item in prior_candidates for counter in item.get('counterexamples', []) for ref in counter['evidence']]
            def bind(ref):
                return ref if ref in known else self._ref(ref)
            # Existing bound historical references remain intact even if files
            # changed; only genuinely new references are captured afresh.
            content['evidence'] = [bind(ref) for ref in content['evidence']]
            for field in ('counterexamples', 'constraints'):
                require(isinstance(content.get(field, []), list), field + ' must be a list')
            for counter in content.get('counterexamples', []):
                require(isinstance(counter, dict) and counter.get('id') and counter.get('description') and isinstance(counter.get('evidence'), list) and counter['evidence'], 'Counterexample needs id/description/evidence')
                context_match(counter.get('applies', {}), {})
                counter['evidence'] = [bind(ref) for ref in counter['evidence']]
            if previous.get('candidate'):
                prior = previous['candidate']
                require(all(ref in content['evidence'] for ref in prior['evidence']), 'Retain original evidence in the candidate chain')
                old_ids = {c['id'] for c in prior.get('counterexamples', [])}
                new_ids = {c['id'] for c in content.get('counterexamples', [])}
                require(old_ids <= new_ids, 'Retain counterexamples; revise explicit applicability instead of erasing history')
            require(isinstance(content.get('method_targets', []), list), 'Method targets must be a list')
            for target in content.get('method_targets', []):
                bound = bind(target)
                require('path' in bound, 'Method target needs its original file path')
                target.update(bound)
            if 'method_release' in content:
                self._validate_release(content, content['method_release'])
            sha = hashlib.sha256(encoded(content).encode()).hexdigest()
            if previous.get('candidate_sha256') == sha and previous['revision'] == expected:
                return previous
            self._append(identifier, expected, 'candidate', {'candidate': content, 'candidate_sha256': sha}, source)
        return self.get(identifier)

    def _validate_release(self, candidate, release):
        """An ordinary method change has an exact approved diff, not an experiment gate."""
        require(isinstance(release, dict) and set(release) == {'asset_id', 'asset_expected', 'version', 'scope', 'changes', 'limitations'}, 'Method release needs asset_id/asset_expected/version/scope/changes/limitations')
        require(isinstance(release['asset_id'], str) and release['asset_id'] and isinstance(release['version'], str) and release['version'], 'Exact method asset/version required')
        require(type(release['asset_expected']) is int and release['asset_expected'] >= 0, 'Method asset expected revision required')
        scope = release['scope']
        require(isinstance(scope, dict) and set(scope) == {'work', 'purpose'} and scope['work'] == candidate['work'] and scope['purpose'], 'Method release needs exact candidate work/purpose')
        require(context_match(candidate.get('applies', {}), scope)['state'] != 'inapplicable', 'Method release purpose conflicts with candidate applicability')
        require(isinstance(release['limitations'], list), 'Method release limitations must be a list')
        changes = release['changes']
        require(isinstance(changes, list) and changes, 'Method release needs exact original-file changes')
        targets = {target['path']: target['sha256'] for target in candidate.get('method_targets', [])}
        paths = []
        for change in changes:
            require(isinstance(change, dict) and set(change) == {'path', 'before_sha256', 'after_sha256'}, 'Method change needs original path and before/after hashes')
            require(change['path'] in targets and change['before_sha256'] == targets[change['path']], 'Method change differs from its captured original target')
            require(isinstance(change['after_sha256'], str) and len(change['after_sha256']) == 64 and all(c in '0123456789abcdef' for c in change['after_sha256']), 'Method change needs exact proposed after hash')
            paths.append(change['path'])
        require(len(paths) == len(set(paths)) and set(paths) == set(targets), 'Method release must account for each approved original target exactly once')

    def publish_method(self, identifier, expected, release, source):
        """Record an already implemented ordinary approved change at its original entry.

        This operation does not edit a method or fabricate field-effect evidence.
        It has no experiment requirement; exact approval, bytes and CAS still apply.
        """
        from .assets import AssetRegistry
        with self.state.transaction():
            current = self.get(identifier)
            require(current.get('status') == 'active' and not current.get('relations') and current.get('approval') and not current.get('approval_issue'), 'Current active approved experience required', 'CANDIDATE_INACTIVE')
            require(current['revision'] == expected, 'Stale experience revision', 'REVISION_CONFLICT')
            candidate = current['candidate']
            self._validate_release(candidate, release)
            require(candidate.get('method_release') == release, 'Publication differs from exact approved method release', 'APPROVAL_HASH')
            files = {}
            for change in release['changes']:
                require(self.can_read(change['path']), 'Original method is outside trusted read scope', 'ACCESS_SCOPE')
                path = local_path(self.root, change['path'])
                require(path.is_file() and digest(path) == change['after_sha256'], 'Approved original method change is not implemented/current', 'CONTENT_CHANGED')
                files[change['path']] = {'sha256': change['after_sha256'], 'bytes': path.stat().st_size}
            registry = AssetRegistry(self.state)
            existing = registry.method_selections(release['asset_id'])
            require(not any(r.get('experience_id') == identifier and r.get('experience_sha256') == current['candidate_sha256'] and r['version'] == release['version'] for r in existing), 'Method release is already registered')
            previous = None
            if registry._present():
                row = self.db.execute('SELECT current_version FROM assets WHERE id=?', (release['asset_id'],)).fetchone()
                previous = row[0] if row else None
            body = {'version': release['version'], 'type': 'method', 'title': candidate['title'],
                'description': candidate['claim'], 'provenance': [{'path': name, 'sha256': info['sha256']} for name, info in files.items()],
                'uses': [{'purpose': release['scope']['purpose'], 'state': 'conditional', 'scope': release['scope'],
                    'evidence': [{'path': name, 'sha256': info['sha256']} for name, info in files.items()]}],
                'limitations': release['limitations'], 'depends_on': [], 'status': 'conditional',
                'applies': candidate.get('applies', {}), 'publication': {'kind': 'ordinary_approved_change',
                    'experience_id': identifier, 'experience_sha256': current['candidate_sha256'], 'previous_version': previous,
                    'original_files': files}}
            registry.put(release['asset_id'], release['asset_expected'], body, source)
            selection = {'schema': 'htam.method-selection/v1', 'kind': 'ordinary_approved_change', 'scope': release['scope'],
                'version': release['version'], 'previous_version': previous, 'bundle': '.', 'files': files,
                'sha256': hashlib.sha256(encoded(files).encode()).hexdigest(), 'experiment': None,
                'plan_sha256': current['candidate_sha256'], 'experience_id': identifier,
                'experience_sha256': current['candidate_sha256'], 'approval': current['approval']['approval'],
                'state': 'active', 'reason': 'Exact approved change verified at the original method entry'}
            selection_id = self.state.observation('asset-selection:' + release['asset_id'], 'note', {'method_selection': selection}, source)
            self._append(identifier, expected, 'method_publication', {'asset_id': release['asset_id'], 'version': release['version'],
                'candidate_sha256': current['candidate_sha256'], 'scope': release['scope'], 'selection_observation_id': selection_id}, source)
        return self.get(identifier)

    def relate(self, identifier, expected, relation, targets, reason, source):
        require(relation in ('merge_into', 'split_into', 'duplicate_of', 'superseded_by'), 'Unknown mechanism relation')
        require(isinstance(targets, list) and targets and all(isinstance(t, str) and t for t in targets) and identifier not in targets and len(set(targets)) == len(targets), 'Explicit distinct relation targets required')
        require(relation != 'split_into' or len(targets) >= 2, 'Split needs at least two explicit mechanisms')
        require(bool(reason), 'Relation needs its semantic reason')
        with self.state.transaction():
            self.get(identifier)
            for target in targets:
                self.get(target)
            value = self._append(identifier, expected, 'relation', {'relation': relation, 'targets': targets, 'reason': reason}, source)
        return value

    def retract_relation(self, identifier, expected, relation_observation_id, reason, source):
        require(reason, 'Relation retraction needs a reason')
        with self.state.transaction():
            record = next((r for r in self._records(identifier) if r['id'] == relation_observation_id and r['experience']['record_type'] == 'relation'), None)
            require(record is not None, 'Relation is not in this mechanism')
            return self._append(identifier, expected, 'relation_retracted', {'relation_observation_id': relation_observation_id, 'reason': reason}, source)

    def mark(self, identifier, expected, status, reason, source):
        require(status in ('active', 'rejected', 'withdrawn', 'expired'), 'Unknown experience lifecycle')
        require(reason, 'Experience lifecycle needs reason')
        with self.state.transaction():
            self.get(identifier)
            return self._append(identifier, expected, 'lifecycle', {'status': status, 'reason': reason}, source)

    def _approval(self, candidate, sha, approval, *, receipt=False):
        require(isinstance(approval, dict) and approval.get('candidate_sha256') == sha, 'Approval must bind the exact candidate hash')
        for field in ('input_id', 'assessment_revision', 'input_sha256', 'scope_revision', 'quote', 'reviewer', 'reason'):
            require(field in approval and approval[field] is not None, 'Approval missing ' + field)
        record = self.state._input_scope(approval['input_id'], candidate['work'])
        assessment = self.state.input_assessment(approval['input_id'])
        require(assessment['kind'] == 'human_direct' and assessment['status'] == 'reviewed', 'Approval needs an actually reviewed human input', 'PROVENANCE_UNVERIFIED')
        require(record['input_sha256'] == approval['input_sha256'] == assessment['input_sha256'] and
            assessment['revision'] == approval['assessment_revision'], 'Approval input/assessment version changed', 'REVISION_CONFLICT')
        # Match the existing protected-change receipt contract: acknowledging
        # an unchanged scope is bookkeeping, while link/retraction revisions
        # retain their authority consequences. Legacy receipts without captured
        # scope identity cannot retrospectively prove an acknowledgement.
        from .coordination import _scope_identity
        scope_sha = _scope_identity(self.state, record)
        acknowledged_only = (receipt and approval.get('scope_acknowledged') is False
            and bool(record['scope_acknowledged'])
            and record['scope_revision'] == approval['scope_revision'] + 1
            and approval.get('scope_sha256') == scope_sha)
        require(record['scope_revision'] == approval['scope_revision'] or acknowledged_only,
            'Approval scope revision changed', 'REVISION_CONFLICT')
        if receipt and 'scope_sha256' in approval:
            require(approval['scope_sha256'] == scope_sha, 'Approval applicability changed', 'REVISION_CONFLICT')
        require(isinstance(approval['quote'], str) and approval['quote'].strip() and approval['quote'] in record['prompt'], 'Approval quote must occur in the actual original input')
        require(bool(approval['reviewer']) and bool(approval['reason']), 'Approval requires explicit semantic review')
        require(bool(candidate.get('method_targets')), 'Approved experience needs original method target(s)')
        return {'input_id': record['id'], 'input_sha256': record['input_sha256'], 'assessment_revision': assessment['revision'],
            'assessment_source': assessment['source'], 'scope_revision': record['scope_revision'],
            'scope_sha256': scope_sha, 'scope_acknowledged': bool(record['scope_acknowledged']),
            'candidate_sha256': sha, 'quote': approval['quote'], 'reviewer': approval['reviewer'], 'reason': approval['reason']}

    def approve(self, identifier, expected, approval, source):
        with self.state.transaction():
            current = self.get(identifier)
            require(current.get('candidate') is not None, 'No accessible candidate')
            require(not any(target.get('visibility') == 'restricted' for target in current['candidate'].get('method_targets', [])),
                'Original method target is outside the trusted local read scope', 'ACCESS_SCOPE')
            bound = self._approval(current['candidate'], current['candidate_sha256'], approval)
            self._append(identifier, expected, 'approval', {'approval': bound,
                'publication': 'pending_original_method_publication'}, source)
        return self.get(identifier)

    def get(self, identifier, required=True):
        records = self._records(identifier)
        require(records or not required, 'Unknown experience mechanism: ' + identifier)
        if not records:
            return {}
        history = [dict(r['experience'], observation_id=r['id'], source=r['source'], at=r['at']) for r in records]
        current = next((h for h in reversed(history) if h['record_type'] == 'candidate'), None)
        if not current:
            return {'id': identifier, 'revision': history[-1]['revision'], 'history': history, 'stage': 'no_candidate'}
        candidate = current['candidate']
        visible = self._candidate_visible(candidate)
        if not visible:
            return {'id': identifier, 'revision': history[-1]['revision'], 'visibility': 'restricted', 'history_count': len(history)}
        lifecycle_status = next((h['status'] for h in reversed(history) if h['record_type'] == 'lifecycle'), 'active')
        status = lifecycle_status
        validity = 'not_time_limited'
        if candidate.get('valid_until'):
            try:
                if datetime.fromisoformat(candidate['valid_until']) <= datetime.fromisoformat(now()):
                    validity = 'expired'
                    if status == 'active':
                        status = 'expired'
                else:
                    validity = 'within_declared_time'
            except (ValueError, TypeError):
                validity = 'unknown'
                if status == 'active':
                    status = 'validity_unknown'
        retracted = {h['relation_observation_id'] for h in history if h['record_type'] == 'relation_retracted'}
        relations = [h for h in history if h['record_type'] == 'relation' and h['observation_id'] not in retracted]
        approval = next((h for h in reversed(history) if h['record_type'] == 'approval'), None)
        stage = 'candidate'
        approval_issue = None
        if approval:
            try:
                self._approval(candidate, current['candidate_sha256'], approval['approval'], receipt=True)
                stage = 'approved_pending_publication'
            except HarnessError as error:
                approval_issue = {'code': error.code, 'reason': str(error)}
        methods = self._formal_methods(identifier, current['candidate_sha256'])
        if methods['current_publication_count'] and stage == 'approved_pending_publication':
            stage = 'published'
        projected_history = []
        for record in history:
            if record['record_type'] == 'candidate':
                record = deepcopy(record)
                if self._candidate_visible(record['candidate']):
                    record['candidate'] = self._project_candidate(record['candidate'])
                else:
                    record = {k: record[k] for k in ('schema', 'mechanism', 'revision', 'record_type', 'observation_id', 'at', 'candidate_sha256')}
                    record['visibility'] = 'restricted'
            projected_history.append(record)
        return {'id': identifier, 'revision': history[-1]['revision'], 'candidate': self._project_candidate(candidate),
            'candidate_sha256': current['candidate_sha256'], 'stage': stage, 'status': status,
            'lifecycle_status': lifecycle_status, 'time_validity': validity,
            'relations': relations, 'approval': approval, 'approval_issue': approval_issue,
            'history': projected_history, 'formal_methods': methods,
            'formal_method_effect': 'published' if methods['current_publication_count'] else
                'rolled_back' if any(p.get('rolled_back') for p in methods['publications']) else
                'registered_needs_review' if methods['publications'] else 'not_published'}

    def _formal_methods(self, identifier, candidate_sha):
        """Join existing publication routes and exact asset uses; no synthetic success."""
        from .assets import AssetRegistry
        registry = AssetRegistry(self.state)
        experiments = {}
        for row in self.db.execute("SELECT body FROM observations WHERE kind='note' AND json_extract(body,'$.experiment.kind')='publication_plan' AND json_extract(body,'$.experiment.data.experience_id')=?", (identifier,)):
            plan = json.loads(row['body'])['experiment']['data']
            experiments[plan['experiment']] = plan
        asset_ids = set()
        for row in self.db.execute("SELECT asset,body FROM observations WHERE kind='note' AND (json_extract(body,'$.method_selection.experience_id')=? OR json_extract(body,'$.method_selection.experiment') IN (SELECT json_extract(body,'$.experiment.id') FROM observations WHERE kind='note' AND json_extract(body,'$.experiment.kind')='publication_plan' AND json_extract(body,'$.experiment.data.experience_id')=?))", (identifier, identifier)):
            asset_ids.add(row['asset'].removeprefix('asset-selection:'))
        publications, uses, effects = [], [], []
        restricted = 0
        for asset_id in sorted(asset_ids):
            for selection in registry.method_selections(asset_id):
                plan = experiments.get(selection.get('experiment'), {})
                if (selection.get('experience_id') or plan.get('experience_id')) != identifier:
                    continue
                sha = selection.get('experience_sha256') or plan.get('experience_sha256')
                paths = [str(__import__('pathlib').Path(selection['bundle']) / name) for name in selection['files']]
                if not all(self.can_read(path) for path in paths):
                    restricted += 1
                    continue
                publications.append({'asset_id': asset_id, 'version': selection['version'], 'scope': selection['scope'],
                    'state': selection['state'], 'authority': selection['authority'], 'candidate_current': sha == candidate_sha,
                    'published_version': plan.get('version', selection['version']),
                    'rolled_back': bool(plan and selection['version'] == plan.get('previous_version')),
                    'kind': selection.get('kind') or 'experiment', 'experiment': selection.get('experiment'),
                    'selection_observation_id': selection['observation_id'], 'content_currentness': 'not_rechecked',
                    'entry': 'asset-handoff --id ' + asset_id + ' --version ' + selection['version'] + ' --purpose ' + selection['scope']['purpose']})
            if not registry._present():
                continue
            published_versions = {p.get('published_version', p['version']) for p in publications if p['asset_id'] == asset_id}
            for row in self.db.execute('SELECT version,body FROM asset_versions WHERE asset_id=?', (asset_id,)):
                publication = json.loads(row['body']).get('publication', {})
                if publication.get('experience_id') == identifier or publication.get('experiment') in experiments:
                    published_versions.add(row['version'])
            for row in self.db.execute('SELECT id,work_id,requirement,version,body,source,at FROM asset_uses WHERE asset_id=? ORDER BY id', (asset_id,)):
                if row['version'] not in published_versions:
                    continue
                use = json.loads(row['body'])
                if not self._refs_visible(list(registry._path_references(use))):
                    restricted += 1
                    continue
                record = {key: row[key] for key in ('id', 'work_id', 'requirement', 'version', 'source', 'at')}
                record.update(asset_id=asset_id, purpose=use['description'], state=use['state'], task=use.get('task'), evidence=use.get('evidence', []))
                uses.append(record)
                if use.get('outcome'):
                    effects.append({'asset_use_id': row['id'], 'asset_id': asset_id, 'version': row['version'], **use['outcome']})
        return {'publications': publications, 'current_publication_count': sum(p['candidate_current'] and not p['rolled_back'] and p['authority'] == 'current' and p['state'] == 'active' for p in publications),
            'recorded_uses': uses, 'reported_effects': effects, 'restricted_record_count': restricted,
            'meaning': 'Approved publication, recorded use and reported benefit/failure are separate evidence layers; no long-term effectiveness inferred'}

    def _refs_visible(self, refs):
        return all(self.can_read(ref['path']) for ref in refs if 'path' in ref)

    def _candidate_visible(self, candidate):
        return candidate.get('access', 'local_workspace') == 'local_workspace' and self._refs_visible(candidate['evidence'])

    def _project_candidate(self, candidate):
        """Do not reveal counterexample/target text via accessible parent/history."""
        result = deepcopy(candidate)
        result['counterexamples'] = [counter if self._refs_visible(counter['evidence']) else
            {'id': counter['id'], 'applies': counter.get('applies', {}), 'visibility': 'restricted', 'required_load': True}
            for counter in result.get('counterexamples', [])]
        result['method_targets'] = [target if self._refs_visible([target]) else
            {'visibility': 'restricted', 'required_load': True} for target in result.get('method_targets', [])]
        return result

    def required(self, context):
        context = self.resolve_context(context)
        mandatory = []
        if context.get('task'):
            task = self.state.task(context['task'])
            for field in ('requires', 'limitations', 'scope'):
                if task['body'].get(field):
                    mandatory.append({'kind': 'recorded_task_condition', 'id': context['task'] + ':' + field,
                        'task': context['task'], 'work_revision': task['work_revision'], field: task['body'][field],
                        'meaning': 'Actual task condition/dependency; not a relation inferred from parent grouping'})
        if context.get('work'):
            work = self.state.get(context['work'])
            constraints = work.get('constraints', [])
            constraints = [constraints] if isinstance(constraints, str) else constraints
            for index, constraint in enumerate(constraints):
                mandatory.append({'kind': 'work_constraint', 'id': f"{work['id']}:constraint:{index}",
                    'work_revision': work['revision'], 'constraint': constraint, 'source': work.get('request_source'),
                    'meaning': 'Existing recorded work constraint; no new authorization inferred'})
        selected = set(context.get('mechanisms', []))
        records = self._records()
        # Relationship IDs/targets belong to the trusted journal's metadata.
        # Build the closure even when an origin's evidence body is restricted;
        # the caller must see a required-load gap instead of a false complete.
        retractions = {(r['experience']['mechanism'], r['experience']['relation_observation_id'])
            for r in records if r['experience']['record_type'] == 'relation_retracted'}
        relation_metadata = [(r['experience']['mechanism'], r['experience']['targets']) for r in records
            if r['experience']['record_type'] == 'relation' and (r['experience']['mechanism'], r['id']) not in retractions]
        # Explicit current relation graph only. Retraction/split history stays in
        # get(), while a merge target retains relevant origin counterexamples.
        changed = True
        while changed:
            changed = False
            for identifier, targets in relation_metadata:
                if selected.intersection(targets) and identifier not in selected:
                    selected.add(identifier); changed = True
        seen = set()
        for identifier in sorted(selected):
            item = self.get(identifier, required=False)
            if item.get('visibility') == 'restricted':
                mandatory.append({'kind': 'restricted_required_mechanism', 'mechanism': identifier, 'required_load': True})
                continue
            candidate = item.get('candidate', {})
            if not candidate:
                mandatory.append({'kind': 'missing_required_mechanism', 'mechanism': identifier, 'required_load': True})
                continue
            applicability = context_match(candidate.get('applies', {}), context)
            if applicability['state'] == 'needs_context':
                mandatory.append({'kind': 'mechanism_applicability_unknown', 'mechanism': identifier,
                    'missing': applicability['missing'], 'required_load': True})
                continue
            if applicability['state'] != 'declared_match':
                continue
            for index, constraint in enumerate(candidate.get('constraints', [])):
                mandatory.append({'kind': 'mechanism_constraint', 'mechanism': identifier,
                    'id': identifier + ':constraint:' + str(index), 'constraint': constraint,
                    'candidate_sha256': item['candidate_sha256']})
            for counter in candidate.get('counterexamples', []):
                counter_scope = context_match(counter.get('applies', {}), context)
                if counter_scope['state'] == 'needs_context':
                    mandatory.append({'kind': 'counterexample_applicability_unknown', 'mechanism': identifier,
                        'id': counter['id'], 'missing': counter_scope['missing'], 'required_load': True})
                    continue
                if counter_scope['state'] != 'declared_match':
                    continue
                if counter.get('visibility') == 'restricted':
                    mandatory.append({'kind': 'restricted_required_counterexample', 'mechanism': identifier,
                        'id': counter['id'], 'required_load': True, 'meaning': 'Use original authorized evidence route'})
                    continue
                identity = encoded([identifier, counter['id']])
                if identity not in seen:
                    mandatory.append({'kind': 'counterexample', 'mechanism': identifier, **counter}); seen.add(identity)
        return mandatory

    def select(self, context, query='', *, limit=8, budget=16000):
        context = self.resolve_context(context)
        selected_ids = set(context.get('mechanisms', []))
        optional, inactive = [], []
        for identifier in sorted({r['experience']['mechanism'] for r in self._records()}):
            item = self.get(identifier)
            candidate = item.get('candidate')
            if not candidate:
                continue
            if selected_ids and identifier not in selected_ids:
                continue
            if query and query.casefold() not in encoded(candidate).casefold():
                continue
            applicability = context_match(candidate.get('applies', {}), context)
            if applicability['state'] != 'declared_match':
                continue
            if item['status'] != 'active' or item.get('relations'):
                inactive.append({'id': identifier, 'revision': item['revision'], 'status': item['status'],
                    'relations': item['relations'], 'entry': 'ExperienceRegistry.get(' + repr(identifier) + ')'})
                continue
            optional.append({'kind': 'experience_candidate', 'id': identifier, 'revision': item['revision'],
                'title': candidate['title'], 'claim': candidate['claim'], 'applies': candidate.get('applies', {}),
                'evidence': candidate['evidence'], 'constraints': candidate.get('constraints', []),
                'method_targets': candidate.get('method_targets', []),
                'candidate_sha256': item['candidate_sha256'], 'stage': item['stage'],
                'approval_issue': item['approval_issue'], 'formal_method_effect': item['formal_method_effect']})
        # Fix the top-k mechanism plan before loading its required closure. Do
        # not backfill a later candidate after a budget omission: that candidate
        # would introduce an unaccounted counterexample/relationship closure.
        optional = optional[:limit]
        required_context = {**context, 'mechanisms': sorted(selected_ids or {item['id'] for item in optional})}
        mandatory = self.required(required_context)
        result = bounded_selection(mandatory, optional, limit=limit, budget=budget)
        if any(item.get('required_load') for item in mandatory):
            result['context_complete'] = False
            result['unresolved_required'] = [item for item in mandatory if item.get('required_load')]
        return {**result, 'inactive': inactive[:50], 'inactive_count': len(inactive), 'context': context,
            'required_context': required_context,
            'coverage': 'Explicit v1 mechanism records and existing work constraints only; no lexical merge or automatic rule adoption'}
