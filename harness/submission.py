"""Exact-content normal-review submission checks, using the existing event store.

No remote write, public acceptance, new scientific authority or general browser
interception is provided here. The host covers explicit review-submit actions.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from .common import require, local_path, digest, now, HarnessError
from .runtime import native_hash

MARKER = re.compile(r'HTAM_SUBMISSION:([A-Za-z0-9_.-]+)')
SUBMIT = re.compile(r'(?:submit\s*(?:for\s*)?review|提交审核|提交审查)', re.I)

def browser_action(event):
    tool = event.get('tool_name', '')
    if not re.search(r'(?:cua_repl|node_repl).*?(?:__|\.)js$', tool):
        return False
    raw = event.get('tool_input', {})
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except ValueError: return False
    code = raw.get('code', '') if isinstance(raw, dict) else ''
    return bool(MARKER.search(code) or (SUBMIT.search(code) and re.search(r'\.(?:click|press|evaluate)\s*\(', code)))

def _event(state, seq, work, kind):
    row = state.db.execute('SELECT * FROM events WHERE seq=? AND work_id=? AND kind=?', (seq,work,kind)).fetchone()
    require(row is not None, 'Exact ' + kind + ' evidence required', 'SUBMISSION_EVIDENCE')
    return json.loads(row['payload'])

def _objects(state, objects):
    require(isinstance(objects, list) and objects, 'Submission needs a nonempty exact object batch')
    result = []
    for obj in objects:
        require(isinstance(obj, dict) and all(obj.get(k) for k in ('id','version','root','payload_file','payload_sha256')), 'Exact object identity/content required')
        path = local_path(state.root, obj['payload_file'])
        values = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        found = [v for v in values if v.get('id') == obj['id'] and v.get('version') == obj['version'] and obj['root'] in (v.get('json') or v.get('json_ordered') or {})]
        require(len(found) == 1, 'Reviewed object must be unambiguous in its payload file')
        content = found[0].get('json') or found[0].get('json_ordered')
        require(native_hash(content) == obj['payload_sha256'], 'Reviewed payload has changed', 'SUBMISSION_CONTENT_CHANGED')
        result.append(deepcopy(obj))
    keys=[(o['root'],o['id'],o['version']) for o in result]
    require(len(keys)==len(set(keys)), 'Duplicate submission objects')
    return result

def authorize(state, req):
    work, writer = req['work'], req['writer_session']
    binding = state.binding(writer)
    require(binding and binding['work_id']==work and binding['role']=='main', 'Submission scope is recorded by the bound main reviewer')
    reviewed = _event(state, req['review_event'], work, 'native_stage_reviewed')
    review = reviewed['review']
    require(review.get('reviewer')==writer and review.get('review_mode')=='independent', 'Scope must cite actual independent review')
    require(req.get('purpose')=='normal_review' and req.get('reason'), 'Declare exact normal-review scope and reason')
    # This is a scoped main disposition, not an inference from a research-use pass.
    objects=_objects(state,req['objects'])
    value={**deepcopy(req),'objects':objects,'task':reviewed.get('task'),'at':now(),'meaning':'Main declares this exact subset eligible for normal review; remote precheck and action binding still required'}
    with state.transaction(): seq=state.event(work,'submission_scope_reviewed',req['source'],value,session=writer)
    return {'event':seq,**value}

def retract(state, req):
    scope=_event(state,req['authorization_event'],req['work'],'submission_scope_reviewed')
    binding=state.binding(req['writer_session'])
    require(binding and binding['work_id']==req['work'] and binding['role']=='main' and req.get('reason'),'Exact main reviewer and reason required to withdraw a submission scope')
    with state.transaction():seq=state.event(req['work'],'submission_scope_retracted',req['source'],deepcopy(req),session=req['writer_session'])
    return {'event':seq,'status':'retracted','authorization_event':req['authorization_event']}

def validate(state, req, *, fresh=True):
    scope=_event(state, req['authorization_event'], req['work'], 'submission_scope_reviewed')
    require(not state.db.execute("SELECT seq FROM events WHERE work_id=? AND kind='submission_scope_retracted' AND json_extract(payload,'$.authorization_event')=?",(req['work'],req['authorization_event'])).fetchone(), 'Submission review scope has been withdrawn; use current review', 'SUBMISSION_SCOPE_RETRACTED')
    require(state.get(req['work'])['status']=='active', 'Submission work is not active')
    require(not state.blocking_inputs(req['work'],task=scope.get('task')), 'Reconcile affected current user input before submission')
    objects=_objects(state,req['objects']);allowed={(x['root'],x['id'],x['version'],x['payload_sha256']) for x in _objects(state,scope['objects'])}
    require(all((x['root'],x['id'],x['version'],x['payload_sha256']) in allowed for x in objects), 'Action exceeds the exact reviewed subset', 'SUBMISSION_SCOPE_CHANGED')
    binding=state.binding(req['session'])
    require(binding and binding['work_id']==req['work'], 'Submission session must belong to the reviewed work')
    reports=req.get('verification_reports',[]);require(isinstance(reports,list) and reports,'Full current remote verification reports required')
    checked=[];evidence=[]
    for ref in reports:
        path=local_path(state.root,ref);report=json.loads(path.read_text())
        require(report.get('status')=='passed_remote_verification' and report.get('counts',{}).get('blockers')==0,'Remote anomaly revokes submission readiness','SUBMISSION_REMOTE_BLOCKED')
        stamp=datetime.fromisoformat(report['generated_at_utc'].replace('Z','+00:00'))
        age=(datetime.now(timezone.utc)-stamp).total_seconds()
        require(not fresh or 0<=age<=900,'Remote proof must be refreshed within15 minutes of the actual action','SUBMISSION_STALE_PROOF')
        cp=local_path(state.root,report['files']['checks']);checked.extend(json.loads(l) for l in cp.read_text().splitlines() if l.strip())
        evidence.extend([{'path':str(path.relative_to(state.root)),'sha256':digest(path)},{'path':str(cp.relative_to(state.root)),'sha256':digest(cp)}])
    for obj in objects:
        matches=[v for v in checked if v.get('role')=='root' and v.get('id')==obj['id'] and v.get('version')==obj['version'] and v.get('path')=='/'+obj['root']+'#readback']
        require(matches and all(v.get('status')=='ok' and v.get('remote_state_code')==0 and v.get('remote_user_id')==req.get('owner_id') and v.get('local_payload_sha256')==v.get('remote_payload_sha256')==obj['payload_sha256'] for v in matches),'Every selected object needs owner/state0/full-body equality; state-only success cannot clear a mismatch','SUBMISSION_CONTENT_UNVERIFIED')
    require(req.get('owner_id'),'Exact owner required')
    _check_known_content(state,req,objects)
    return evidence

def observe(state, work, report_paths, source, session=None):
    """Retain content checks independently of state-only checks and tool success."""
    saved=[]
    with state.transaction():
        for ref in report_paths:
            path=local_path(state.root,ref);report=json.loads(path.read_text())
            require(report.get('schema_version')==1 and report.get('status') in ('passed_remote_verification','blocked_remote_verification'), 'Expected native remote-verification report')
            cp=local_path(state.root,report['files']['checks'])
            body_checks=[v for v in (json.loads(l) for l in cp.read_text().splitlines() if l.strip()) if v.get('role')=='root' and v.get('path','').endswith('#readback') and (v.get('status')!='ok' or v.get('local_payload_sha256') or v.get('remote_payload_sha256'))]
            for v in body_checks:
                value={'check':v,'generated_at_utc':report['generated_at_utc'],'report':str(path.relative_to(state.root)),'report_sha256':digest(path),'checks_sha256':digest(cp)}
                fingerprint=native_hash(value)
                old=state.db.execute("SELECT seq FROM events WHERE work_id=? AND kind='submission_content_observed' AND json_extract(payload,'$.fingerprint')=?",(work,fingerprint)).fetchone()
                if old:saved.append(old['seq']);continue
                saved.append(state.event(work,'submission_content_observed',source,{**value,'fingerprint':fingerprint},session=session))
    return saved

def observe_hook(state,event):
    binding=state.binding(event.get('session_id'))
    if not binding or event.get('hook_event_name')!='PostToolUse':return
    def docs(value):
        if isinstance(value,str):
            try:yield from docs(json.loads(value))
            except (ValueError,RecursionError):
                for line in value.splitlines():
                    if line.strip().startswith('{'):
                        try:yield from docs(json.loads(line))
                        except (ValueError,RecursionError):pass
        elif isinstance(value,dict):
            if value.get('schema_version')==1 and 'remote_verification' in str(value.get('status','')) and isinstance(value.get('files'),dict):yield value
            else:
                for v in value.values():yield from docs(v)
        elif isinstance(value,list):
            for v in value:yield from docs(v)
    for doc in docs(event.get('tool_response',{})):
        try:observe(state,binding['work_id'],[doc['files']['report']],'actual tool remote-verification report',event['session_id'])
        except (HarnessError,OSError,KeyError,ValueError):
            # An unrelated/unreadable output never grants submission readiness.
            pass

def _check_known_content(state, req, objects):
    for obj in objects:
        row=state.db.execute("SELECT payload FROM events WHERE work_id=? AND kind='submission_content_observed' AND json_extract(payload,'$.check.id')=? AND json_extract(payload,'$.check.version')=? AND json_extract(payload,'$.check.path')=? ORDER BY json_extract(payload,'$.generated_at_utc') DESC,seq DESC LIMIT 1",(req['work'],obj['id'],obj['version'],'/'+obj['root']+'#readback')).fetchone()
        if row:
            check=json.loads(row['payload'])['check']
            require(check.get('status')=='ok' and check.get('local_payload_sha256')==check.get('remote_payload_sha256')==obj['payload_sha256'],'A known full-content anomaly or newer changed body still invalidates this object; state-only success cannot resolve it','SUBMISSION_KNOWN_ANOMALY')

def prepare(state, req):
    req = deepcopy(req)
    row=state.db.execute("SELECT work_id,payload FROM events WHERE seq=? AND kind='submission_scope_reviewed'",(req.get('authorization_event'),)).fetchone()
    require(row,'Exact reviewed submission scope required')
    scope=json.loads(row['payload'])
    req.setdefault('work',row['work_id']);req.setdefault('objects',scope['objects']);req.setdefault('owner_id',scope.get('owner_id'))
    require(isinstance(req.get('id'),str) and re.fullmatch(r'[A-Za-z0-9_.-]+',req['id']),'Stable submission action id required')
    require(req.get('tool_name') and isinstance(req.get('tool_input'),dict),'Bind the exact intended browser tool arguments')
    code=req['tool_input'].get('code','');m=MARKER.search(code)
    require(m and m.group(1)==req['id'],'Exact HTAM_SUBMISSION marker required in the prepared action')
    require(browser_action({'tool_name':req['tool_name'],'tool_input':req['tool_input']}),'Only supported explicit browser submission actions are prepared here')
    with state.transaction():
        require(not state.db.execute("SELECT seq FROM events WHERE kind='submission_prepared' AND json_extract(payload,'$.id')=?",(req['id'],)).fetchone(),'Submission action id already used; inspect its prior result before retry')
        observe(state,req['work'],req.get('verification_reports',[]),req['source'],req['session'])
        evidence=validate(state,req)
        value={**deepcopy(req),'evidence':evidence,'tool_input_sha256':native_hash(req['tool_input']),'at':now()}
        seq=state.event(req['work'],'submission_prepared',req['source'],value,session=req['session'])
    return {'event':seq,'ready':True,'id':req['id'],'objects':req['objects'],'coverage':'Exact planned call and fresh known remote bytes; target UI semantics and public acceptance are separate'}

def guard(state,event):
    binding=state.binding(event.get('session_id'))
    if not binding or not browser_action(event):return None
    phase=event.get('hook_event_name');raw=event.get('tool_input',{});raw=json.loads(raw) if isinstance(raw,str) else raw
    if phase=='PostToolUse':
        with state.transaction():state.event(binding['work_id'],'submission_tool_returned','actual host hook',{'session':event['session_id'],'tool_input_sha256':native_hash(raw),'tool_response_sha256':native_hash(event.get('tool_response')),'meaning':'Tool return only; final cloud readback required'},session=event['session_id'])
        return {}
    if phase!='PreToolUse':return None
    try:
        marker=MARKER.search(raw.get('code',''));require(marker,'Review submission needs an exact prepared batch. See harness/README.md#送审动作与已审实物绑定; use submission show for the existing scoped review and prepare the batch without asking the user again','SUBMISSION_NOT_PREPARED')
        with state.transaction():
            row=state.db.execute("SELECT seq,payload FROM events WHERE work_id=? AND kind='submission_prepared' AND json_extract(payload,'$.id')=? ORDER BY seq DESC LIMIT 1",(binding['work_id'],marker.group(1))).fetchone()
            require(row,'No prepared action for this submission','SUBMISSION_NOT_PREPARED');req=json.loads(row['payload'])
            require(req['session']==event['session_id'] and req['tool_name']==event['tool_name'] and req['tool_input_sha256']==native_hash(raw),'Actual call differs from prepared submission','SUBMISSION_ACTION_CHANGED')
            require(not state.db.execute("SELECT seq FROM events WHERE kind='submission_claimed' AND json_extract(payload,'$.prepared_event')=?",(row['seq'],)).fetchone(),'Submission already attempted; reconcile actual outcome before retry','SUBMISSION_ALREADY_ATTEMPTED')
            for ref in req['evidence']:require(digest(local_path(state.root,ref['path']))==ref['sha256'],'Submission evidence changed','SUBMISSION_EVIDENCE_CHANGED')
            validate(state,req)
            state.event(binding['work_id'],'submission_claimed','actual host PreToolUse',{'prepared_event':row['seq'],'id':req['id'],'caller':event['session_id'],'call_id':event.get('tool_use_id') or event.get('call_id')},session=event['session_id'])
        return {'hookSpecificOutput':{'hookEventName':phase,'permissionDecision':'allow','permissionDecisionReason':'Exact reviewed content, fresh full readback and prepared action match'}}
    except (HarnessError,OSError,ValueError,KeyError) as exc:
        with state.transaction():state.event(binding['work_id'],'submission_denied','actual host PreToolUse',{'caller':event['session_id'],'reason':str(exc)},session=event['session_id'])
        return {'hookSpecificOutput':{'hookEventName':phase,'permissionDecision':'deny','permissionDecisionReason':str(exc)}}
