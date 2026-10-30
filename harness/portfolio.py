"""Bounded goal ancestry and portfolio views; grouping does not create dependencies."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from .common import require, now
from . import goals


def validate_parent(state, identifier, parent):
    if parent is None:
        return None
    require(isinstance(parent, dict) and set(parent) <= {'work', 'requirements', 'scope', 'source'}, 'Invalid parent relation')
    for key in ('work', 'scope', 'source'):
        require(isinstance(parent.get(key), str) and parent[key].strip(), 'Parent relation needs ' + key)
    require(parent['work'] != identifier, 'A work cannot parent itself')
    target = state.get(parent['work'])
    ids = parent.get('requirements', [])
    require(isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids), 'Parent relation needs unique requirement ids')
    require(set(ids) <= {r['id'] for r in target.get('requirements', [])}, 'Unknown parent requirement')
    visited = {identifier}
    while target:
        require(target['id'] not in visited, 'Work ancestry cycle')
        visited.add(target['id'])
        ref = target.get('parent')
        target = state.get(ref['work']) if ref else None
    return deepcopy(parent)


def validate_children(state, identifier, requirements):
    ids = {r['id'] for r in requirements or []}
    for child in state.works():
        parent = child.get('parent')
        if parent and parent['work'] == identifier:
            require(set(parent['requirements']) <= ids, 'Requirement is still referenced by child: ' + child['id'])


def ancestry(state, work, limit=6):
    w = state.get(work) if isinstance(work, str) else work
    rows = []; seen = {w['id']}
    while w.get('parent'):
        ref = w['parent']; target = state.get(ref['work'])
        require(target['id'] not in seen, 'Work ancestry cycle')
        seen.add(target['id'])
        rows.append({'id': target['id'], 'title': target['title'], 'objective': target['objective'],
                     'status': target['status'], 'revision': target['revision'], 'contributes_to': ref['requirements'],
                     'scope': ref['scope'], 'source': ref['source'],
                     'requirements': [{k: r.get(k) for k in ('id', 'description')} for r in target.get('requirements', []) if r['id'] in ref['requirements']]})
        w = target
    return {'items': rows[:limit], 'omitted': max(0, len(rows)-limit),
            'meaning': 'Organizational scope only; no automatic prerequisite, permission, completion or blocker inheritance'}


def children(state, work):
    return [w for w in state.works() if (w.get('parent') or {}).get('work') == work]


def planning(state, work, limit=12):
    """Explicit macro decision query; ordinary portfolio metadata stays cheap."""
    from .coordination import decision_context
    return decision_context(state, work, limit)


def attention(state, w):
    rows = []
    for t in state.tasks(w['id']):
        if t['work_revision'] != w['request_revision'] or t['status'] not in ('reserved', 'dispatched', 'running', 'returned', 'revise', 'proposed'):
            continue
        if t['status'] == 'proposed' and t['id'] not in w.get('decision', {}).get('tasks', []):
            continue
        event = state.db.execute("SELECT at FROM events WHERE work_id=? AND (json_extract(payload,'$.task')=? OR json_extract(payload,'$.id')=?) ORDER BY seq DESC LIMIT 1", (w['id'], t['id'], t['id'])).fetchone()
        age = None
        if event:
            try: age = max(0, (datetime.fromisoformat(now())-datetime.fromisoformat(event[0])).total_seconds())
            except ValueError: pass
        dispatch = state.db.execute("SELECT status FROM dispatches WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (t['id'],)).fetchone()
        unknown = dispatch and dispatch[0] == 'unknown'
        rows.append({'id': t['id'], 'status': t['status'], 'purpose': t['body'].get('purpose'),
                     'responsible': 'main' if unknown or t['status'] in ('returned', 'proposed') else 'executor',
                     'recovery': 'UNKNOWN: inspect actual receipt before resending' if unknown else None,
                     'last_changed_at': event[0] if event else None, 'age_seconds': age})
    return rows


def view(state, work, verify=False):
    from .assets import AssetRegistry
    w = state.get(work); overview = goals.overview(state, w, verify=False)
    registry = AssetRegistry(state); registered = registry.list(work=work) if verify else registry.metadata(work=work)
    # AssetRegistry returns versioned records; uses are exact project/requirement relations.
    records = registered if isinstance(registered, list) else registered.get('assets', registered.get('items', []))
    assets = []
    for record in records:
        versions = {u['version'] for u in record.get('work_usage', []) if u.get('current', True)}
        if not versions:
            versions = {record['version']}
        if verify:
            assets.extend(registry.get(record['id'], version) for version in sorted(versions))
        else:
            assets.append(record)
    related = children(state, work); rows = []
    for r in overview['requirements']:
        linked = []
        for a in assets:
            uses = [u for u in a.get('usage', []) if u.get('work') == work and u.get('requirement') == r['id'] and u.get('current', True)]
            if uses:
                linked.append({'id': a['id'], 'version': a['version'], 'title': a['body']['title'],
                               'validity': a['validity'], 'uses': uses, 'uses_claims': a.get('annotations', {}).get('uses', a['body'].get('uses', [])),
                               'lifecycle': a.get('annotations', {}).get('status', a['body'].get('status', 'draft')),
                               'limitations': a.get('annotations', {}).get('limitations', a['body'].get('limitations', []))})
        assessment = w.get('assessments', {}).get(r['id'], {})
        rows.append({**r, 'source': next(x['source'] for x in w['requirements'] if x['id'] == r['id']),
                     'next_action': assessment.get('next_action'), 'family': assessment.get('family'), 'assets': linked,
                     'stages': [{'id': c['id'], 'title': c['title'], 'status': c['status'], 'scope': c['parent']['scope'], 'next_action': c.get('next_action')} for c in related if r['id'] in c['parent']['requirements']]})
    return {'work': {k: w.get(k) for k in ('id', 'title', 'objective', 'request_source', 'revision', 'status', 'next_action')},
            'ancestry': ancestry(state, w), 'requirements': rows,
            'state_counts': dict(Counter(r['state'] for r in rows)),
            'children': [{'id': c['id'], 'title': c['title'], 'status': c['status'], 'parent': c['parent'], 'next_action': c.get('next_action')} for c in related],
            'attention': attention(state, w), 'decision': goals.decision_view(state, w),
            'feedback': w.get('decision', {}).get('previous_outcome_comparison'),
            'coverage': 'Current registered requirements and exact asset uses. ' + ('Captured local asset evidence checked; professional receipts and remote state not rechecked.' if verify else 'Metadata only; local asset files, professional receipts and remote state not rechecked.') + ' No completion percentage is inferred.'}


def markdown(data):
    w = data['work']; lines = ['# ' + w['title'], '', '由管理核心生成；当前事实通过管理入口更新。专业实物仍在原位置。', '',
        '工作：`' + w['id'] + '`；状态：' + w['status'] + '；修订：' + str(w['revision']), '', w['objective'], '',
        '下一步：' + (w.get('next_action') or '未指定'), '',
        '| 原要求 | 当前判断 | 可复用资产及用途 | 阶段 | 剩余 |', '|---|---|---|---|---|']
    def cell(s): return str(s).replace('|', r'\|').replace('\n', '<br>')
    for r in data['requirements']:
        asset_text = '; '.join(a['title'] + '@' + a['version'] + ' [' + a['validity']['state'] + ']' for a in r['assets']) or '尚未关联可复用资产'
        lines.append('| ' + ' | '.join(cell(s) for s in [r['id'] + ' ' + r['description'], r['state'], asset_text, '; '.join(c['title'] for c in r['stages']), '; '.join(r['remaining'])]) + ' |')
    lines += ['', '资产关联与文件存在不等于原要求满足；实际采用、用途条件和用户审阅分别保留。', '', data['coverage']]
    return '\n'.join(lines) + '\n'
