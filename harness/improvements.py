"""Explicit task findings in the existing improvement event stream.

References are evidence, never instructions or approval. Native result identity
and scientific review state are owned by native_entry and remain untouched here.
"""
from copy import deepcopy
import json

from .common import digest, local_path, require
from .runtime import native_hash


CONTENT = ('problem', 'scope', 'impact', 'current_workaround', 'routing', 'severity')
SEVERITIES = ('action_blocking', 'high', 'normal')


def maintenance_assignment(state, candidate_event):
    """Read the current technical responsibility; main bindings stay live."""
    row = state.db.execute("SELECT seq,source,payload,at FROM events WHERE kind='improvement_assignment' "
        "AND json_extract(payload,'$.candidate_seq')=? ORDER BY seq DESC LIMIT 1", (candidate_event,)).fetchone()
    if not row:
        return {'revision': 0, 'maintenance_work': None}
    value = {**json.loads(row['payload']), 'event': row['seq'], 'source': row['source'], 'at': row['at']}
    sessions = [state.binding(row['session']) for row in state.db.execute(
        "SELECT session FROM bindings WHERE work_id=? AND role='main' ORDER BY session", (value['maintenance_work'],))]
    value['main_bindings'] = sessions
    value['main_resolution'] = ('single_recorded_main_binding' if len(sessions) == 1 else
        'multiple_recorded_main_bindings' if sessions else 'no_recorded_main_binding')
    return value


def assign_maintenance(state, work, candidate_event, expected, body, source):
    """Append only an explicit responsibility change; never triage or dispatch.

    The original candidate is the identity. A retry of the current exact change
    is idempotent; an older revision cannot overwrite a subsequent assignment.
    Responsibility is descriptive and grants no modification/deployment or
    scientific authority.
    """
    require(isinstance(body, dict) and set(body) == {'maintenance_work', 'reason'},
        'Assignment needs only maintenance_work (existing work or null) and reason')
    target = body['maintenance_work']
    require(target is None or isinstance(target, str) and target.strip(),
        'Maintenance work must be an existing work id or null to withdraw')
    require(isinstance(body['reason'], str) and body['reason'].strip(), 'Assignment needs a reason')
    require(type(expected) is int and expected >= 0, 'Assignment expected revision must be a nonnegative integer')
    require(isinstance(source, str) and source.strip(), 'Assignment needs an actual source')
    request_hash = native_hash({'work': work, 'candidate_event': candidate_event, 'expected': expected,
        'body': body, 'source': source})
    with state.transaction():
        state.get(work)
        candidate = state.db.execute("SELECT seq FROM events WHERE seq=? AND work_id=? "
            "AND kind='improvement_candidate'", (candidate_event, work)).fetchone()
        require(candidate is not None, 'Candidate event is not in this work', 'IDENTITY_MISMATCH')
        current = maintenance_assignment(state, candidate_event)
        if current.get('request_sha256') == request_hash:
            return {**current, 'duplicate': True}
        require(current['revision'] == expected, 'Stale maintenance assignment revision', 'REVISION_CONFLICT')
        if target is not None:
            state.get(target)
        if current['maintenance_work'] == target and current.get('reason') == body['reason']:
            return {**current, 'duplicate': True}
        if target is None and current['revision'] == 0:
            return {**current, 'duplicate': True}
        state.event(work, 'improvement_assignment', source, {'candidate_seq': candidate_event,
            'origin_work': work, **deepcopy(body), 'before_revision': expected, 'revision': expected + 1,
            'previous_maintenance_work': current['maintenance_work'], 'request_sha256': request_hash,
            'meaning': 'Technical maintenance responsibility only; original disposition and scientific ownership unchanged; '
                'no modification, deployment, scientific or messaging authorization'})
        return {**maintenance_assignment(state, candidate_event), 'duplicate': False}


def candidate_rows(state, work, *, relation='all', include_previous=False, event=None):
    """Select original identities once, including only explicitly linked works."""
    require(relation in ('all', 'origin', 'maintenance'), 'Unknown improvement relation')
    clauses, parameters = [], []
    if relation in ('all', 'origin'):
        clauses.append('e.work_id=?')
        parameters.append(work)
    if relation in ('all', 'maintenance'):
        clauses.append("e.seq IN (SELECT json_extract(a.payload,'$.candidate_seq') FROM events a "
            "WHERE a.kind='improvement_assignment' AND json_extract(a.payload,'$.maintenance_work')=?" +
            ('' if include_previous else " AND NOT EXISTS (SELECT 1 FROM events newer WHERE "
                "newer.kind='improvement_assignment' AND newer.seq>a.seq AND "
                "json_extract(newer.payload,'$.candidate_seq')=json_extract(a.payload,'$.candidate_seq'))") + ')')
        parameters.append(work)
    sql = "SELECT e.seq,e.work_id,e.payload FROM events e WHERE e.kind='improvement_candidate' AND (" + ' OR '.join(clauses) + ')'
    if event is not None:
        sql += ' AND e.seq=?'
        parameters.append(event)
    return state.db.execute(sql + ' ORDER BY e.seq DESC', parameters)


def query_improvements(state, work, *, relation='all', include_resolved=False, event=None,
                       limit=8, offset=0, history_limit=8, history_offset=0):
    """Bounded read recovery, including resolved and previously assigned items."""
    from .context import improvement_items, _improvement_summary
    require(type(limit) is int and 1 <= limit <= 50 and type(offset) is int and offset >= 0,
        'Improvement query limit/offset outside supported bounds')
    require(type(history_limit) is int and 1 <= history_limit <= 50 and
        type(history_offset) is int and history_offset >= 0, 'Assignment history limit/offset outside supported bounds')
    require(event is None or type(event) is int and event > 0, 'Improvement event must be a positive integer')
    state.get(work)
    items = improvement_items(state, work, include_resolved=include_resolved, relation=relation,
        include_previous=include_resolved, event=event)
    result = {'work': work, 'relation': relation, 'include_resolved': include_resolved,
        'items': [_improvement_summary(item) for item in items[offset:offset + limit]],
        'total': len(items), 'offset': offset, 'limit': limit,
        'next_offset': offset + limit if len(items) > offset + limit else None,
        'coverage': 'Same original events; explicit maintenance assignments only. Include resolved also recovers '
            'previous assignments. Responsibility does not grant authorization.'}
    if event is not None and items:
        rows = state.db.execute("SELECT seq,work_id,source,payload,at FROM events WHERE kind='improvement_assignment' "
            "AND json_extract(payload,'$.candidate_seq')=? ORDER BY seq DESC LIMIT ? OFFSET ?",
            (event, history_limit + 1, history_offset)).fetchall()
        result['assignment_history'] = {'items': [{**json.loads(row['payload']), 'event': row['seq'],
            'source': row['source'], 'at': row['at']} for row in rows[:history_limit]],
            'offset': history_offset, 'limit': history_limit,
            'next_offset': history_offset + history_limit if len(rows) > history_limit else None,
            'coverage': 'Recorded assignment history only; session bindings are resolved live on the current assignment'}
    return result


def declared_findings(state, request):
    """Collect explicitly labelled findings, including existing return attachments.

    Never classify arbitrary prose, filenames or scientific review findings.
    Legacy method-triage fields are declarations, not inferred instructions.
    """
    result = request.get('result', {})
    require(isinstance(request.get('findings', []), list), 'Findings must be an optional list')
    values = list(request.get('findings', []))
    for field in ('findings', 'finding_refs'):
        if field in result:
            require(isinstance(result[field], list), 'Result ' + field + ' must be a list')
            values.extend(result[field])
    for ref in result.get('artifacts', []):
        path = local_path(state.root, ref)
        if path.suffix.lower() != '.json' or not path.is_file() or path.stat().st_size > 1024 * 1024:
            continue
        try:
            doc = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(doc, dict):
            continue
        declared = doc.get('remaining_method_issues_for_main_triage')
        if declared is not None:
            require(isinstance(declared, list) and all(isinstance(x, str) and x.strip() for x in declared),
                    'Declared method issues must be nonempty text entries')
            if declared:
                values.append({'ref': ref, 'id': 'declared-methods:' + native_hash(ref), 'problem': '\n'.join(declared)})
    return values



def check_origin(entry, supplied):
    """Reject conflicting optional identities rather than silently dropping them."""
    actual = {'work': entry['work_id'], 'work_id': entry['work_id'],
        'task': entry['task_id'], 'task_id': entry['task_id'],
        'native_entry': entry['id'], 'native_entry_id': entry['id'],
        'return_event': entry.get('return_event'), 'prior_return_event': entry.get('return_event'),
        'return_sha256': entry.get('return_sha256'),
        'producer_session': entry.get('accepted_by') or entry.get('thread_id'),
        'review_owner': entry.get('review_owner')}
    for key, value in actual.items():
        require(key not in supplied or supplied[key] == value,
                'Finding ' + key + ' differs from its exact native return', 'IDENTITY_MISMATCH')


def latest_finding(state, candidate):
    row = state.db.execute("SELECT seq,payload FROM events WHERE kind='improvement_supplement' "
        "AND json_extract(payload,'$.candidate_seq')=? ORDER BY seq DESC LIMIT 1", (candidate['seq'],)).fetchone()
    return row or candidate


def _capture(state, supplied, entry):
    require(isinstance(supplied, dict), 'Each finding must be an object or a reference path')
    check_origin(entry, supplied)
    document, evidence, reference = {}, [], None
    if supplied.get('ref'):
        require(isinstance(supplied['ref'], str), 'Finding ref must be a workspace path')
        reference = state._file_evidence([supplied['ref']], 'runtime')[0]
        path = local_path(state.root, reference['path'])
        if path.suffix.lower() == '.json':
            try:
                document = json.loads(path.read_text(encoding='utf-8-sig'))
            except (ValueError, UnicodeError) as exc:
                require(False, 'Finding reference must contain valid JSON: ' + str(exc))
            require(isinstance(document, dict), 'Finding reference JSON must be an object')
            require(digest(path) == reference['sha256'], 'Finding reference changed while loading')
            check_origin(entry, document)
        evidence.append(reference)
        # Evidence embedded in a referenced item is relative to that item, not cwd.
        embedded = document.get('evidence', [])
        require(isinstance(embedded, list), 'Referenced finding evidence must be a list')
        for item in embedded:
            require(isinstance(item, (str, dict)), 'Finding evidence must be a file reference')
            ref = {'path': item} if isinstance(item, str) else dict(item)
            require(isinstance(ref.get('path'), str), 'Finding evidence needs a workspace path')
            ref['path'] = str(local_path(state.root, path.parent / ref['path']).relative_to(state.root))
            evidence.extend(state._file_evidence([ref], 'runtime'))
    incoming = {key: deepcopy(document[key]) for key in CONTENT if key in document}
    if document.get('disposition') and 'routing' not in incoming:
        incoming['routing'] = document['disposition']  # Declared routing, not triage or permission.
    incoming.update({key: deepcopy(supplied[key]) for key in CONTENT if key in supplied})
    for key, value in incoming.items():
        require(isinstance(value, str) and value.strip(), 'Finding ' + key + ' must be nonempty text')
    if 'severity' in incoming:
        require(incoming['severity'] in SEVERITIES, 'Unknown explicit finding severity')
    if supplied.get('evidence'):
        evidence.extend(state._file_evidence(supplied['evidence'], 'runtime'))
    elif 'evidence' in supplied:
        require(supplied['evidence'] == [], 'Finding evidence must be a file-reference list')
    if reference:
        incoming['reference'] = reference
    if evidence:
        incoming['evidence'] = list({ref['path']: ref for ref in evidence}.values())
    return incoming


def record_findings(state, entry, findings, source, writer_session, asset=None):
    """Append candidates/supplements atomically with the caller's existing action."""
    require(isinstance(findings, list), 'Findings must be an optional list')
    if not findings:
        return []
    require(isinstance(source, str) and source.strip() and isinstance(writer_session, str)
            and writer_session.strip(), 'Finding needs its actual source and writer session')
    require(asset is None or isinstance(asset, str) and asset.strip(), 'Finding asset topic must be nonempty text')
    returned = state.db.execute("SELECT * FROM events WHERE seq=? AND work_id=? AND kind='native_entry_result'",
        (entry.get('return_event'), entry['work_id'])).fetchone()
    require(returned and json.loads(returned['payload']).get('id') == entry['id'],
            'Finding needs its exact saved native return', 'IDENTITY_MISMATCH')
    task = state.task(entry['task_id'])
    require(task['work_id'] == entry['work_id'], 'Finding task belongs to another work', 'IDENTITY_MISMATCH')
    origin = {'work': entry['work_id'], 'task': entry['task_id'], 'native_entry': entry['id'],
        'return_event': entry['return_event'], 'return_sha256': entry['return_sha256'],
        'producer_session': entry.get('accepted_by') or entry.get('thread_id'),
        'review_owner': entry.get('review_owner')}
    results = []
    with state.transaction():
        for supplied in findings:
            supplied = {'ref': supplied} if isinstance(supplied, str) else supplied
            incoming = _capture(state, supplied, entry)
            identifier = supplied.get('id')
            require(identifier is None or isinstance(identifier, str) and identifier.strip(), 'Finding id must be nonempty text')
            identity = ('id', identifier) if identifier else ('ref', incoming['reference']['path']) if incoming.get('reference') else (
                'problem', incoming['problem']) if incoming.get('problem') else ('candidate', supplied.get('candidate_event'))
            key = native_hash({'native_entry': entry['id'], 'identity': identity})
            candidate = None
            if 'candidate_event' in supplied:
                candidate = state.db.execute("SELECT * FROM events WHERE seq=? AND work_id=? AND kind='improvement_candidate'",
                    (supplied['candidate_event'], entry['work_id'])).fetchone()
                require(candidate is not None, 'Finding candidate event is not in this work', 'IDENTITY_MISMATCH')
            matched = state.db.execute("SELECT seq,kind,payload FROM events WHERE work_id=? "
                "AND kind IN ('improvement_candidate','improvement_supplement') AND (json_extract(payload,'$.finding_key')=? "
                "OR json_extract(payload,'$.native_entry')=? AND json_extract(payload,'$.reference.path')=?) "
                "ORDER BY seq LIMIT 1", (entry['work_id'], key, entry['id'], incoming.get('reference', {}).get('path'))).fetchone()
            if matched:
                number = matched['seq'] if matched['kind'] == 'improvement_candidate' else json.loads(matched['payload'])['candidate_seq']
                require(candidate is None or candidate['seq'] == number, 'Finding identity already belongs to another candidate', 'IDENTITY_MISMATCH')
                candidate = state.db.execute('SELECT * FROM events WHERE seq=?', (number,)).fetchone()
            if candidate:
                original = json.loads(candidate['payload'])
                require(original.get('kind') == 'task_finding' and original.get('task') == entry['task_id'],
                    'Finding supplements require an original task_finding from the same task; keep other findings separate for main triage',
                    'IDENTITY_MISMATCH')
            previous = json.loads(latest_finding(state, candidate)['payload']) if candidate else {}
            submission_hash = native_hash({'origin': origin, 'content': incoming})
            if candidate:
                prior = state.db.execute("SELECT seq,payload FROM events WHERE (seq=? OR kind='improvement_supplement' "
                    "AND json_extract(payload,'$.candidate_seq')=?) AND json_extract(payload,'$.submission_sha256')=? LIMIT 1",
                    (candidate['seq'], candidate['seq'], submission_hash)).fetchone()
                if prior:
                    results.append({'event': candidate['seq'], 'observation_event': prior['seq'], 'duplicate': True,
                        'asset': json.loads(prior['payload']).get('asset', 'task-finding:' + key)})
                    continue
            content = {key: previous[key] for key in CONTENT if key in previous}
            content.update(incoming)
            require(content.get('problem'), 'Finding needs a problem or a reference containing one')
            evidence = {ref['path']: ref for ref in previous.get('evidence', [])}
            evidence.update({ref['path']: ref for ref in incoming.get('evidence', [])})
            require(evidence, 'Finding needs actual evidence file references')
            content['evidence'] = list(evidence.values())
            if 'reference' not in content and previous.get('reference'):
                content['reference'] = previous['reference']
            payload = {**content, 'kind': 'task_finding', 'improvement_trigger': 'task_finding',
                'finding_key': key, 'submission_sha256': submission_hash, **origin,
                'asset': asset or previous.get('asset') or 'task-finding:' + key,
                'writer_session': writer_session,
                'meaning': 'Explicit task finding awaiting main triage; no scientific adoption or shared-tool approval'}
            if identifier:
                payload['finding_id'] = identifier
            if candidate:
                payload['candidate_seq'] = candidate['seq']
            seq = state.event(entry['work_id'], 'improvement_supplement' if candidate else 'improvement_candidate', source, payload,
                session=writer_session, origin={'kind': 'declared_task_finding', 'producer_session': origin['producer_session'],
                    'reference': incoming.get('reference'), 'coverage': 'Selected source statement; writer identity does not authenticate authorship'})
            results.append({'event': candidate['seq'] if candidate else seq, 'observation_event': seq, 'duplicate': False,
                'asset': payload['asset']})
    return results
