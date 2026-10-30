"""Goal relationships and current decisions; semantic judgments stay explicit."""
from copy import deepcopy
import hashlib

from .common import cached, encoded, require, HarnessError
from .fulfillment import bind_contract, check_contract


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def check_change(change):
    require(isinstance(change, dict), 'Changed completion criteria need an explicit change record')
    require(change.get('kind') in ('evidence', 'correction', 'scope'), 'Specify evidence, correction or scope change')
    for key in ('reason', 'impact', 'source'):
        require(isinstance(change.get(key), str) and change[key].strip(), 'Change missing ' + key)
    if change['kind'] == 'scope':
        require(change.get('approval_source'), 'Scope change needs the user approval source')


def criteria(contract):
    """Evidence locations/hashes can refresh; predicates and selected identities cannot silently change."""
    if not contract:
        return None
    return {'purpose': contract.get('purpose'), 'checks': [
        {k: v for k, v in c.items() if k not in ('inputs', 'report')}
        for c in contract.get('checks', [])]}


def reduced_criteria(old, new):
    """Recognize removed/replaced declared predicates; semantic equivalence is reviewed."""
    before = {c['id']: c for c in (criteria(old) or {}).get('checks', [])}
    after = {c['id']: c for c in (criteria(new) or {}).get('checks', [])}
    for key, check in before.items():
        if key not in after:
            return True
        current = after[key]
        for field, value in check.items():
            if field in ('description', 'basis'):
                continue
            if isinstance(value, list):
                if not isinstance(current.get(field), list) or any(v not in current[field] for v in value):
                    return True
            elif current.get(field) != value:
                return True
    return False


def bind_changed(root, old, new, change=None):
    if old and old != new:
        # Rebinding hashes alone is allowed, and still revokes adoption in State.
        def unhashed(value):
            if isinstance(value, dict):
                return {k: unhashed(v) for k, v in value.items() if k != 'sha256'}
            return [unhashed(v) for v in value] if isinstance(value, list) else value
        if unhashed(old) != unhashed(new):
            check_change(change)
            if criteria(old) != criteria(new):
                require(change['kind'] != 'evidence', 'Changed predicates are not an evidence-only refresh')
                if reduced_criteria(old, new):
                    require(change.get('approval_source'), 'Removing or replacing completion predicates needs approval provenance; correction alone is insufficient')
    return bind_contract(root, new)


def bind_requirements(root, incoming, previous, change=None):
    require(isinstance(incoming, list) and incoming, 'Work needs at least one overall requirement')
    old = {r['id']: r for r in previous or []}
    result, seen = [], set()
    for supplied in incoming:
        require(isinstance(supplied, dict), 'Requirement must be an object')
        require(set(supplied) <= {'id', 'description', 'source', 'required', 'revision', 'completion', 'claims'}, 'Unknown requirement fields')
        r = deepcopy(supplied)
        for key in ('id', 'description', 'source'):
            require(isinstance(r.get(key), str) and r[key].strip(), 'Requirement missing ' + key)
        require(r['id'] not in seen, 'Duplicate requirement id')
        seen.add(r['id']); before = old.get(r['id'])
        r.setdefault('required', True)
        require(type(r['required']) is bool, 'required must be boolean')
        if before and before.get('required', True) and not r['required']:
            require(change and change.get('approval_source'), 'Making a required obligation optional needs approval provenance')
        if 'claims' in r or (before or {}).get('claims'):
            r['claims'] = bind_claims(root, r.get('claims', []), (before or {}).get('claims', []), change)
        changed = before and any(r.get(k) != before.get(k) for k in ('description', 'source', 'required'))
        changed = changed or bool(before and claim_criteria(before.get('claims')) != claim_criteria(r.get('claims')))
        if changed:
            check_change(change)
            require(change['kind'] != 'evidence', 'Requirement definition changed')
        if 'completion' in r:
            r['completion'] = bind_changed(root, (before or {}).get('completion'), r['completion'], change)
        if before and before.get('completion') and not r.get('completion'):
            check_change(change)
            require(change['kind'] != 'evidence', 'Removing proof criteria changes the requirement')
            require(change.get('approval_source'), 'Removing completion criteria needs approval provenance')
        shape_changed = before and criteria(before.get('completion')) != criteria(r.get('completion'))
        r['revision'] = (before or {}).get('revision', 0) + (1 if not before or changed or shape_changed else 0)
        result.append(r)
    if old and set(old) != seen:
        check_change(change)
        require(change['kind'] != 'evidence', 'Adding/removing requirements changes the scope or criteria')
        if set(old) - seen:
            require(change.get('approval_source'), 'Removing original obligations needs approval provenance; a correction label is not approval')
    return result


CLAIM_LEVELS = ('technical', 'conditional_use', 'user_acceptance', 'public_submission', 'public_acceptance')


def claim_criteria(claims):
    return [{**{k: v for k, v in c.items() if k != 'completion'}, 'completion': criteria(c.get('completion'))}
            for c in claims or []]


def bind_claims(root, incoming, previous, change=None):
    require(isinstance(incoming, list), 'Use claims must be a list')
    old = {c['id']: c for c in previous}; result = []; seen = set()
    for supplied in incoming:
        require(isinstance(supplied, dict) and set(supplied) <= {'id', 'description', 'source', 'required', 'level', 'applicability', 'completion'}, 'Invalid use claim')
        c = deepcopy(supplied)
        for field in ('id', 'description', 'source'):
            require(isinstance(c.get(field), str) and c[field].strip(), 'Use claim missing ' + field)
        require(c['id'] not in seen, 'Duplicate use claim'); seen.add(c['id'])
        c.setdefault('required', True); c.setdefault('applicability', 'required')
        require(type(c['required']) is bool and c.get('level') in CLAIM_LEVELS, 'Specify required and actual use-claim level')
        require(c['applicability'] in ('required', 'conditional'), 'Invalid use-claim applicability')
        if old.get(c['id'], {}).get('required') and not c['required']:
            require(change and change.get('approval_source'), 'Making a use claim optional needs approval provenance')
        before = old.get(c['id'], {})
        if before.get('applicability', 'required') == 'required' and c['applicability'] == 'conditional' and before:
            require(change and change.get('approval_source'), 'Making unconditional applicability conditional needs approval provenance')
        if before.get('completion') and not c.get('completion'):
            require(change and change.get('approval_source'), 'Removing use-claim completion needs approval provenance')
        if 'completion' in c:
            c['completion'] = bind_changed(root, old.get(c['id'], {}).get('completion'), c['completion'], change)
        result.append(c)
    if set(old) - seen:
        require(change and change.get('approval_source'), 'Removing use claims needs approval provenance')
    return result


def claim_view(state, requirement, assessment, verify=True):
    declared = {x['claim']: x for x in assessment.get('claims', [])}
    result = []
    for c in requirement.get('claims', []):
        a = declared.get(c['id'], {}); status = a.get('state', 'unassessed')
        contract = a.get('applicability_evidence') if status == 'not_applicable' else c.get('completion')
        proof = check_contract(state.root, contract, state) if verify and status in ('satisfied', 'not_applicable') else None
        if status == 'not_applicable' and (c['applicability'] != 'conditional' or not a.get('reason') or not contract):
            status = 'blocked'
        if proof and not proof['passed']:
            status = 'blocked'
        result.append({'id': c['id'], 'description': c['description'], 'source': c['source'],
                       'level': c['level'], 'required': c['required'], 'state': status,
                       'reason': a.get('reason'), 'remaining': a.get('remaining', []), 'evidence': proof,
                       'verification': 'checked' if proof else 'not_rechecked'})
    return result


def assessment_references(work):
    """Exact pointers to the one maintained copy of each current assessment."""
    return [{'requirement': rid, 'path': ['assessments', rid], 'sha256': fingerprint(value)}
            for rid, value in work.get('assessments', {}).items()]


def stored_decision(work, decision):
    """Remove a duplicate payload only when all of its fields still agree."""
    result = deepcopy(decision)
    for supplied in result.pop('assessments', []):
        current = work.get('assessments', {}).get(supplied.get('requirement'))
        require(current is not None and all(key in current and current[key] == value
                for key, value in supplied.items()),
                'Recorded decision assessment differs from the maintained assessment; reconcile explicitly')
    result['assessment_refs'] = assessment_references(work)
    return result


def decision_view(state, work, *, full=False):
    d = deepcopy(work.get('decision', {})); reasons = []
    if not d:
        return {'action': 'unreconciled', 'validity': 'absent', 'reasons': ['No current decision']}
    if not full and 'assessments' in d:
        # Legacy records stay readable without repeating their full payload in
        # ordinary recovery. The full record remains an exact historical read.
        recorded = d.pop('assessments')
        d['assessment_refs'] = assessment_references(work)
        d['recorded_assessments'] = {'count': len(recorded), 'sha256': fingerprint(recorded),
            'entry': {'command': 'context', 'work': work['id'], 'full': True},
            'coverage': 'Original legacy decision payload; current assessments are maintained on the work'}
    if d.get('request_revision') != work['request_revision']:
        reasons.append('Request changed')
    if d.get('validity') == 'needs_reconciliation':
        reasons.extend(d.get('invalidated_by', ['Current facts changed']))
    if 'next_action' in d and d['next_action'] != work.get('next_action'):
        reasons.append('Next action differs from the current decision')
    if d.get('work_status') and d['work_status'] != work['status']:
        reasons.append('Work status changed')
    if d.get('action') == 'deliver':
        view = overview(state, work, verify=False)
        if any(r['required'] and r['state'] != 'satisfied' for r in view['requirements']):
            reasons.append('Delivery obligations changed or remain open')
    if reasons:
        d.update(recorded_action=d.get('action'), action='unreconciled', validity='needs_reconciliation', reasons=reasons)
    else:
        d['validity'] = 'current' if 'next_action' in d else 'legacy_action_binding_unspecified'
    return d


def links(state, work, identifier, supplied, bind_versions=True):
    body = deepcopy(supplied)
    requirements = {r['id']: r for r in work.get('requirements', [])}
    require(requirements, 'Legacy work needs explicit requirements migration before new tasks or dispatch')
    contribution = body.get('contributes_to')
    if contribution is None and len(requirements) == 1:
        contribution = list(requirements)
    require(isinstance(contribution, list) and contribution and all(isinstance(x, str) for x in contribution), 'Task needs contributes_to requirement ids')
    require(len(contribution) == len(set(contribution)) and set(contribution) <= set(requirements), 'Unknown/duplicate contribution requirement')
    body['contributes_to'] = contribution
    body['requirement_versions'] = {k: requirements[k]['revision'] for k in contribution}
    if 'use_claims' in body:
        require(isinstance(body['use_claims'], list), 'Task use_claims must be exact references')
        bound_uses, used = [], set()
        for supplied_use in body['use_claims']:
            require(isinstance(supplied_use, dict) and set(supplied_use) <= {'requirement', 'claim', 'role'}, 'Invalid task use-claim reference')
            ref = {**supplied_use, 'role': supplied_use.get('role', 'supports')}
            rid, cid = ref.get('requirement'), ref.get('claim')
            require(isinstance(rid, str) and isinstance(cid, str) and rid in contribution
                    and cid in {claim['id'] for claim in requirements[rid].get('claims', [])}, 'Task use claim is outside its declared contribution')
            require(ref['role'] in ('supports', 'satisfies'), 'Use claim role must be supports or satisfies')
            require((rid, cid) not in used, 'Duplicate task use claim'); used.add((rid, cid)); bound_uses.append(ref)
        body['use_claims'] = bound_uses
    body.setdefault('kind', 'unit')
    require(body['kind'] in ('unit', 'aggregate'), 'Task kind must be unit or aggregate')
    deps = body.get('requires', body.get('depends_on', []))
    require(isinstance(deps, list) and all(isinstance(x, str) for x in deps), 'Task requires must be task ids')
    if 'requires' in body and 'depends_on' in body:
        require(body['requires'] == body['depends_on'], 'requires and depends_on disagree')
    body['requires'] = deps
    bind_assets(state, body)
    if bind_versions:
        body['judgments'] = bind_judgments(state, work['id'], body.get('judgments', []))
    related = deps + ([body['parent_task']] if body.get('parent_task') else [])
    for dep in related:
        require(dep != identifier, 'Task cannot reference itself')
        ancestor = state.task(dep)
        require(ancestor['work_id'] == work['id'], 'Task relationship crosses work boundaries')
        require(ancestor['work_revision'] == work['request_revision'], 'Task relationship refers to an old request')
        # Check both organizing and execution edges; cycles are never meaningful.
        pending, seen = [dep], set()
        while pending:
            item = pending.pop()
            require(item != identifier, 'Cyclic task relationship')
            if item in seen:
                continue
            seen.add(item); b = state.task(item)['body']
            pending.extend(b.get('requires', b.get('depends_on', [])))
            if b.get('parent_task'):
                pending.append(b['parent_task'])
    return body


def bind_assets(state, body):
    refs = body.get('assets', [])
    require(isinstance(refs, list), 'Task assets must be a list')
    if not refs:
        return
    from .assets import AssetRegistry
    registry = AssetRegistry(state); seen = set()
    for ref in refs:
        require(isinstance(ref, dict) and set(ref) <= {'id', 'version', 'purpose', 'required'}, 'Invalid task asset reference')
        require(all(isinstance(ref.get(k), str) and ref[k].strip() for k in ('id', 'version', 'purpose')), 'Task asset needs exact id/version and purpose')
        key = (ref['id'], ref['version']); require(key not in seen, 'Duplicate task asset'); seen.add(key)
        require(type(ref.get('required', True)) is bool, 'Asset required must be boolean')
        registry.get(*key)


def asset_dependencies(state, task):
    from .assets import AssetRegistry
    registry = AssetRegistry(state)
    def blocking(issue):
        if issue['kind'] in ('asset_has_new_version', 'dependency_has_new_version'):
            return False  # A pinned old version remains usable; review newer scope separately.
        if issue['kind'] == 'dependency_needs_review':
            return any(blocking(i) for i in issue.get('issues', []))
        return True
    result = []
    for ref in task['body'].get('assets', []):
        asset = registry.get(ref['id'], ref['version'])
        if ref.get('required', True):
            require(not any(blocking(i) for i in asset['validity']['issues']), 'Required asset evidence needs review: ' + ref['id'])
        result.append({'id': ref['id'], 'version': ref['version'], 'sha256': asset['sha256'],
                       'version_revision': asset['version_revision'], 'purpose': ref['purpose']})
    return result


def bind_judgments(state, work_id, references):
    """New shorthand references bind now; never infer a past persisted binding."""
    require(isinstance(references, list), 'Task judgment dependencies must be a list')
    bound = []
    for supplied in references:
        ref = {'id': supplied} if isinstance(supplied, str) else deepcopy(supplied)
        require(isinstance(ref, dict) and set(ref) <= {'id', 'revision', 'purpose'}, 'Invalid judgment reference')
        require(isinstance(ref.get('id'), str) and ref['id'], 'Judgment reference needs id')
        current = state.judgment(ref['id'])
        require(current['work_id'] == work_id, 'Judgment reference crosses work boundaries')
        if 'revision' not in ref:
            ref['revision'] = current['revision']
        require(type(ref['revision']) is int and ref['revision'] == current['revision'], 'Bind the current exact judgment revision: ' + ref['id'])
        require(ref['id'] not in [x['id'] for x in bound], 'Duplicate judgment dependency')
        bound.append(ref)
    return bound


def judgment_dependencies(state, task, strict=False):
    observations = []
    for ref in task['body'].get('judgments', []):
        if isinstance(ref, str):
            current = state.judgment(ref)
            require(not strict, 'Legacy judgment has no bound revision; reconcile this task: ' + ref)
            require(current['status'] == 'adopted', 'Judgment needs review: ' + ref)
            observations.append({'id': ref, 'binding': 'legacy_unversioned', 'current_revision': current['revision'], 'status': current['status']})
            continue
        require(isinstance(ref, dict) and ref.get('id') and type(ref.get('revision')) is int, 'Invalid bound judgment reference')
        current = state.judgment(ref['id'])
        require(current['work_id'] == task['work_id'], 'Judgment belongs to another work')
        require(current['revision'] == ref['revision'], 'Judgment revision changed: ' + ref['id'])
        require(current['status'] == 'adopted', 'Judgment needs review: ' + ref['id'])
        observations.append({**ref, 'binding': 'exact', 'status': current['status']})
    return observations


def task_frontier(state, work, task):
    """Execution depends on declared task/judgment edges, not sibling reviews."""
    supports = [sid for rid in task['body']['contributes_to']
                for sid in work.get('assessments', {}).get(rid, {}).get('supports', [])]
    visited, pending, records = set(), [task['id'], *supports], []
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        value = state.task(identifier)
        body = value['body']
        judgments = []
        for ref in body.get('judgments', []):
            jid = ref if isinstance(ref, str) else ref['id']
            current = state.judgment(jid)
            judgments.append({'binding': ref, 'current_revision': current['revision'], 'current_status': current['status']})
        record = {'id': identifier, 'body': {k: v for k, v in body.items() if k != 'reviews'}, 'judgments': judgments}
        if body.get('assets'):
            record['assets'] = asset_dependencies(state, value)
        if identifier != task['id']:
            record.update(status=value['status'], reviews=body.get('reviews', []))
        records.append(record)
        pending.extend(body.get('requires', body.get('depends_on', [])))
    requirements = [r for r in work['requirements'] if r['id'] in task['body']['contributes_to']]
    return fingerprint({'request_revision': work['request_revision'], 'requirements': requirements, 'dependencies': sorted(records, key=lambda x: x['id'])})


def current_task(state, task):
    work = state.get(task['work_id'])
    require(task['work_revision'] == work['request_revision'], 'Task belongs to an old request')
    body = task['body']; reqs = {r['id']: r for r in work.get('requirements', [])}
    require(body.get('contributes_to') and body.get('requirement_versions'), 'Task needs goal-link migration')
    for rid in body['contributes_to']:
        require(rid in reqs and body['requirement_versions'].get(rid) == reqs[rid]['revision'], 'Task requirement changed: ' + rid)
    return work


def ready(state, task, seen=None):
    seen = set(seen or [])
    require(task['id'] not in seen, 'Cyclic task evidence dependency')
    seen.add(task['id'])
    current_task(state, task)
    judgment_dependencies(state, task)
    asset_dependencies(state, task)
    for dep in task['body'].get('requires', task['body'].get('depends_on', [])):
        other = state.task(dep); current_task(state, other)
        require(other['status'] == 'adopted', 'Required task is not adopted: ' + dep)
        ready(state, other, seen)
        proof = check_contract(state.root, other['body'].get('completion'), state, seen | {dep})
        require(proof['passed'], 'Required task evidence changed: ' + dep + ': ' + '; '.join(proof['blockers']))


def _frontier(state, work, ids):
    reqs = [r for r in work.get('requirements', []) if r['id'] in ids]
    tasks = []
    for t in state.tasks(work['id']):
        b = t['body']
        if t['work_revision'] == work['request_revision'] and set(b.get('contributes_to', [])) & set(ids):
            if b.get('reviews') or t['status'] in ('returned', 'revise', 'adopted', 'abandoned'):
                judgments = []
                for ref in b.get('judgments', []):
                    jid = ref if isinstance(ref, str) else ref['id']
                    j = state.judgment(jid)
                    judgments.append({'binding': ref, 'revision': j['revision'], 'status': j['status']})
                item = {'id': t['id'], 'status': t['status'], 'completion': b.get('completion'),
                        'reviews': b.get('reviews', []), 'links': b.get('contributes_to')}
                if judgments:
                    item['judgments'] = judgments
                tasks.append(item)
    return fingerprint({'request': work['request_revision'], 'requirements': reqs, 'results': tasks})


def frontier(state, work, ids):
    key = ('frontier', id(state.db), state.db.total_changes, work['id'], work['request_revision'],
           fingerprint(work.get('requirements', [])), tuple(sorted(ids)))
    return cached(key, lambda: _frontier(state, work, ids))


def overview(state, work, verify=True):
    tasks = [t for t in state.tasks(work['id']) if t['work_revision'] == work['request_revision']]
    rows = []
    for r in work.get('requirements', []):
        assessment = work.get('assessments', {}).get(r['id'], {})
        contributors = [t for t in tasks if r['id'] in t['body'].get('contributes_to', []) and t['status'] == 'adopted']
        fresh = assessment.get('frontier') == frontier(state, work, [r['id']])
        proof = check_contract(state.root, r.get('completion'), state) if verify else None
        status = assessment.get('state', 'open') if fresh else 'needs_reconciliation'
        if status == 'satisfied' and proof and not proof['passed']:
            status = 'blocked'
        if verify:
            for support in assessment.get('supports', []):
                try:
                    t = state.task(support); ready(state, t)
                    checked = check_contract(state.root, t['body'].get('completion'), state, {support})
                    if t['status'] != 'adopted' or not checked['passed']:
                        status = 'needs_reconciliation'
                except HarnessError:
                    status = 'needs_reconciliation'
        uses = claim_view(state, r, assessment, verify)
        if status == 'satisfied' and any(c['required'] and c['state'] not in ('satisfied', 'not_applicable') for c in uses):
            status = 'partial'
        rows.append({'id': r['id'], 'description': r['description'], 'revision': r['revision'],
            'state': status, 'required': r['required'], 'adopted_contributions': [t['id'] for t in contributors],
            'remaining': assessment.get('remaining', ['Overall contribution has not been reviewed']),
            'limitations': assessment.get('limitations', []), 'evidence': proof, 'claims': uses,
            'use_coverage': 'explicit_claims' if r.get('claims') else 'legacy_unspecified'})
    return {'requirements': rows, 'unlinked_results': [t['id'] for t in tasks if t['status'] == 'adopted' and not t['body'].get('contributes_to')],
            'migration_required': not bool(work.get('requirements'))}


def completion(state, work):
    # Existing professional checks remain in force alongside the overall requirements.
    if not work.get('requirements'):
        return check_contract(state.root, work.get('completion'), state)
    view = overview(state, work)
    blockers = [r['id'] + ': ' + r['state'] for r in view['requirements'] if r['required'] and r['state'] != 'satisfied']
    if work.get('completion'):
        blockers.extend(check_contract(state.root, work['completion'], state)['blockers'])
    return {'status': 'blocked' if blockers else 'passed', 'passed': not blockers,
            'checks': view['requirements'], 'blockers': blockers}
