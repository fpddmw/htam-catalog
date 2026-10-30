"""Read existing professional receipts; never author data or perform remote writes."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess

from .common import HarnessError, ValidationTimeout, cached, digest, encoded, local_path, remaining_timeout, require


def bind_contract(root, contract):
    """Freeze explicit evidence, preserving supplied hashes and missing-file states."""
    require(isinstance(contract, dict) and contract.get("purpose"), "Completion needs its intended use")
    require(isinstance(contract.get("checks"), list) and contract["checks"], "Completion needs evidence checks")
    value = deepcopy(contract)
    ids = set()
    for check in value["checks"]:
        require(isinstance(check, dict) and check.get("id") and check.get("description"), "Check needs id and description")
        require(check["id"] not in ids, "Duplicate completion check")
        ids.add(check["id"])
        require(check.get("kind") in ("files", "json", "tiangong-remote", "tiangong-curation", "task"), "Unknown completion checker")
        if check["kind"] == "task":
            require(bool(check.get("task")), "Task evidence needs an exact task id")
            continue
        require(check.get("basis"), "Explain the evidence-to-input association; binding is not scientific review")
        require(isinstance(check.get("inputs"), list) and check["inputs"], "Check needs its actual inputs")
        refs = check["inputs"] + ([check["report"]] if check.get("report") else [])
        if check["kind"] != "files":
            require(bool(check.get("report")), "Professional check needs its report")
        for ref in refs:
            require(isinstance(ref, dict) and isinstance(ref.get("path"), str), "Evidence needs a local path")
            path = local_path(root, ref["path"])
            if "sha256" not in ref:
                ref["sha256"] = digest(path) if path.is_file() else None
    return value


def _load(path):
    return cached(('json', str(path.resolve()), digest(path)), lambda: json.loads(path.read_text(encoding='utf-8-sig')))


def _payload_hashes(input_path, node):
    def load():
        text = input_path.read_text(encoding='utf-8-sig')
        try:
            rows = json.loads(text)
            rows = rows if isinstance(rows, list) else [rows]
        except ValueError:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        payloads = [r.get('json_ordered', r.get('json', r)) for r in rows]
        hashed = subprocess.run([node, str(Path(__file__).with_name('tiangong_payload.mjs'))],
            input=json.dumps(payloads, ensure_ascii=False), text=True, capture_output=True, timeout=remaining_timeout(15))
        require(hashed.returncode == 0, 'Exact payload hashing failed')
        return json.loads(hashed.stdout)
    return cached(('payload_hashes', str(input_path.resolve()), digest(input_path), node,
                   digest(Path(__file__).with_name('tiangong_payload.mjs'))), load)


def _pointer(data, pointer):
    require(pointer == "" or pointer.startswith("/"), "Use an exact JSON pointer")
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def _identity(row):
    return (row.get("table"), row.get("id"), row.get("version"))


def _remote(root, check, report):
    expected = check.get("roots")
    require(isinstance(expected, list) and expected, "Remote check needs exact root identities")
    require(check.get("owner") and type(check.get("state")) is int, "Remote check needs owner and exact intended state")
    report_path = local_path(root, check["report"]["path"])
    detail_ref = next((r for r in check["inputs"] if Path(r["path"]).name == "remote-verification.jsonl"), None)
    require(detail_ref is not None, "Bind remote-verification.jsonl alongside its summary")
    detail_path = local_path(root, detail_ref["path"])
    require(detail_path.parent == report_path.parent, "Summary and detail must belong to the same run")
    rows = [json.loads(line) for line in detail_path.read_text().splitlines() if line.strip()]
    require(report.get("counts", {}).get("checked") == len(rows), "Remote report/detail count differs")
    input_path = local_path(root, report["input_path"])
    require(any(local_path(root, r["path"]) == input_path for r in check["inputs"]), "Bind the actual verified payload input")
    require(report.get("generated_at_utc"), "Remote report has no observation time")
    node = os.environ.get("HTAM_NODE") or shutil.which("node")
    require(node, "Exact TianGong payload hashing requires the existing Node runtime")
    hashes = _payload_hashes(input_path, node)
    selected = set()
    for target in expected:
        require(all(isinstance(target.get(k), str) and target[k] for k in ("table", "id", "version")), "Incomplete root identity")
        matches = [r for r in rows if r.get("role") == "root" and _identity(r) == _identity(target)]
        require(matches, "Requested root was not checked: " + str(_identity(target)))
        selected.update(r["row_index"] for r in matches)
        require(all(r.get("status") == "ok" for r in matches), "Remote root is missing or mismatched: " + str(_identity(target)))
        reads = [r for r in matches if r.get("path", "").endswith("#readback")]
        require(len(reads) == 1, "Exact payload readback missing: " + str(_identity(target)))
        row = reads[0]
        require(row.get("remote_user_id") == check["owner"] and row.get("remote_state_code") == check["state"], "Remote owner/state differs")
        require(row.get("local_payload_sha256") and row["local_payload_sha256"] == row.get("remote_payload_sha256"), "Root payload was not compared or differs")
        require(0 <= row["row_index"] < len(hashes) and hashes[row["row_index"]] == row["local_payload_sha256"], "Report describes an older/different payload; run current remote verification")
    failures = [r for r in rows if r.get("row_index") in selected and r.get("status") != "ok"]
    require(not failures, "Selected roots have unresolved references: " + str(len(failures)))
    return {"roots": len(expected), "observed_at": report["generated_at_utc"], "state": check["state"], "scope": "Exact persisted payload and references; scientific adoption and public acceptance remain separate"}


def _curation(root, check, report):
    wanted = check.get("entities")
    require(isinstance(wanted, list) and wanted, "Curation check needs exact entity ids and versions")
    entities = report.get("entities")
    if "task_states" in report:
        # Consume the CLI's existing queue receipt, checking current files again.
        states = report["task_states"]
        require(isinstance(states, list), "Curation task_states must be a list")
        for target in wanted:
            require(all(target.get(k) for k in ("id", "version", "entity_type")), "Curation identity needs type/id/version")
            rows = [r for r in states if r.get("entity_id") == target["id"] and r.get("version") == target["version"] and r.get("entity_type") == target["entity_type"]]
            require(len(rows) == 1 and rows[0].get("status") == "complete", "Exact current curation is incomplete: " + target["id"])
            row = rows[0]
            for path_key, hash_key in (("input_rows_file", "input_rows_sha256"), ("checkpoint_file", "checkpoint_sha256")):
                require(row.get(path_key) and row.get(hash_key), "Curation receipt has no current content binding")
                file = local_path(root, row[path_key])
                require(file.is_file() and digest(file) == row[hash_key], "Curation input/checkpoint changed: " + str(file))
            checkpoint_path = local_path(root, row["checkpoint_file"])
            checkpoint = _load(checkpoint_path)
            require(checkpoint.get("input_rows_sha256") == row["input_rows_sha256"], "Checkpoint describes other input rows")
            require(checkpoint.get("completion_scope"), "Curation scope missing")
            require(checkpoint.get("evidence"), "Curation bound reports missing")
            for evidence in checkpoint["evidence"]:
                file = Path(evidence["path"])
                file = file if file.is_absolute() else checkpoint_path.parent / file
                require(evidence.get("sha256") and file.is_file() and digest(file) == evidence["sha256"], "Curation evidence changed: " + str(file))
            if check.get("publication_ready") is True:
                require(checkpoint.get("publication_ready") is True, "Current curation only admits a draft")
        return {"entities": len(wanted), "scope": "Exact current CLI curation inputs and evidence; semantic adoption remains with reviewer"}
    require(isinstance(entities, list), "Unsupported curation report; use its existing entity review output")
    for target in wanted:
        require(all(target.get(k) for k in ("id", "version", "entity_type")), "Curation identity needs type/id/version")
        rows = [e for e in entities if all(e.get(k) == target[k] for k in ("id", "version", "entity_type"))]
        require(len(rows) == 1, "Exact entity was not reviewed")
        require(rows[0].get("status") == "passed" and rows[0].get("blockers") == [], "Current entity curation remains blocked: " + target["id"])
    return {"entities": len(wanted)}


def _check_contract(root, contract, state=None, seen=None):
    """Check only declared requirements. No global stop for unrelated partial work."""
    if not contract:
        return {"status": "unconfigured", "passed": False, "checks": [], "blockers": ["Completion requirements have not been established"]}
    results = []
    try:
        require(contract.get("purpose") and contract.get("checks"), "Empty completion contract")
        ids = [c.get("id") for c in contract["checks"]]
        require(all(ids) and len(set(ids)) == len(ids), "Completion check identities missing or duplicated")
    except (HarnessError, AttributeError, TypeError) as exc:
        return {"status": "blocked", "passed": False, "checks": [], "blockers": [str(exc)]}
    for check in contract["checks"]:
        result = {"id": check["id"], "description": check.get("description"), "status": "blocked"}
        try:
            require(check.get("kind") in ("files", "json", "tiangong-remote", "tiangong-curation", "task"), "Unknown check kind")
            if check["kind"] == "task":
                require(state is not None, "Task proof requires the work state")
                task_id = check["task"]
                require(task_id not in (seen or set()), "Cyclic task completion dependency")
                task = state.task(task_id)
                if state.get(task['work_id']).get('requirements'):
                    from .goals import ready
                    ready(state, task, seen)
                require(task["status"] == "adopted", "Required task has not been adopted: " + task_id)
                require(task["work_revision"] == state.get(task["work_id"])["request_revision"], "Required task belongs to an old request")
                require(not state.blocking_inputs(task["work_id"], task=task_id, operation='adopt'), "Required task has affected unreconciled user input: " + task_id)
                nested = check_contract(root, task["body"].get("completion"), state, (seen or set()) | {task_id})
                require(nested["passed"], "Adopted task evidence is no longer current: " + task_id)
                result["details"] = {"task": task_id}
            else:
                require(check.get("basis") and check.get("inputs"), "Evidence scope and basis are required")
                refs = check["inputs"] + ([check["report"]] if check.get("report") else [])
                for ref in refs:
                    path = local_path(root, ref["path"])
                    require(path.is_file(), "Missing evidence: " + ref["path"])
                    require(ref.get("sha256"), "Evidence was not bound when the requirement was recorded: " + ref["path"])
                    require(digest(path) == ref["sha256"], "Evidence changed; recheck the affected result: " + ref["path"])
                if check["kind"] != "files":
                    report = _load(local_path(root, check["report"]["path"]))
                    if check["kind"] == "json":
                        require(check.get("assertions"), "Specify the relevant report findings")
                        for assertion in check["assertions"]:
                            require("equals" in assertion, "Assertion must have an explicit expected value")
                            actual = _pointer(report, assertion["pointer"])
                            require(type(actual) == type(assertion["equals"]) and actual == assertion["equals"], "Report finding differs at " + assertion["pointer"])
                    elif check["kind"] == "tiangong-remote":
                        result["details"] = _remote(root, check, report)
                    elif check["kind"] == "tiangong-curation":
                        result["details"] = _curation(root, check, report)
            result["status"] = "passed"
        except ValidationTimeout:
            raise
        except (HarnessError, OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                remaining_timeout(0)
            result["reason"] = str(exc)
        results.append(result)
    blockers = [r["id"] + ": " + r["reason"] for r in results if r["status"] != "passed"]
    return {"purpose": contract["purpose"], "status": "blocked" if blockers else "passed", "passed": not blockers, "checks": results, "blockers": blockers}


def check_contract(root, contract, state=None, seen=None):
    # Cache leaf professional checks only, with current content identities.
    # Task recursion retains its path-specific cycle checks and current state.
    if not isinstance(contract, dict) or not isinstance(contract.get('checks'), list) or any(not isinstance(c, dict) or c.get('kind') == 'task' for c in contract.get('checks', [])):
        return _check_contract(root, contract, state, seen)
    try:
        current = []
        for c in contract.get('checks', []):
            for ref in c.get('inputs', []) + ([c['report']] if c.get('report') else []):
                p = local_path(root, ref['path'])
                current.append((str(p), digest(p) if p.is_file() else None))
        key = ('completion_leaf', str(root), encoded(contract), tuple(current),
               id(state.db) if state else None, state.db.total_changes if state else None, tuple(sorted(seen or [])))
        return deepcopy(cached(key, lambda: _check_contract(root, contract, state, seen)))
    except ValidationTimeout:
        raise
    except (HarnessError, OSError, KeyError, TypeError, AttributeError):
        return _check_contract(root, contract, state, seen)


def adoption_scope(state, task):
    """Expose only the professional/use checks actually declared for this task.

    Source/quantity/identity/boundary/allocation dimensions belong to the selected
    professional contract. This function does not add a universal LCA checklist.
    """
    body = task['body']; contract = body.get('completion') or {}
    work = state.get(task['work_id']); requirements = {item['id']: item for item in work.get('requirements', [])}
    uses = []
    for ref in body.get('use_claims', []):
        requirement = requirements.get(ref['requirement'])
        require(requirement is not None and ref['requirement'] in body['contributes_to'], 'Use claim requirement changed', 'USE_SCOPE_CHANGED')
        claim = next((item for item in requirement.get('claims', []) if item['id'] == ref['claim']), None)
        require(claim is not None, 'Declared use claim changed', 'USE_SCOPE_CHANGED')
        uses.append({**ref, 'level': claim['level'], 'description': claim['description'],
                     'purpose': (claim.get('completion') or {}).get('purpose'),
                     'checks': [item['id'] for item in (claim.get('completion') or {}).get('checks', [])]})
    return {'task': task['id'], 'task_purpose': body['purpose'], 'completion_purpose': contract.get('purpose'),
            'checks': [{'id': item['id'], 'kind': item['kind'], 'description': item.get('description'),
                        'basis': item.get('basis')} for item in contract.get('checks', [])],
            'use_claims': uses,
            'meaning': 'Declared intended use and professional evidence scope; successful file/remote checks do not themselves perform scientific semantic review'}


def validate_adoption_use(state, task, review):
    """Adoption hook for explicit intended-use claims, preserving legacy API.

    A task can support a larger use without claiming it fulfilled. An explicit
    satisfied use must consume that exact use's existing completion contract.
    """
    scope = adoption_scope(state, task)
    if 'intended_use' in review:
        require(review['intended_use'] == scope['completion_purpose'],
                'Review intended use differs from the frozen completion purpose', 'USE_SCOPE_CHANGED')
    mapped = {(item['requirement'], item['claim']): item for item in scope['use_claims']}
    findings = review.get('use_assessments', [])
    require(isinstance(findings, list), 'Use assessments must be a list', 'USE_ASSESSMENT_REQUIRED')
    seen, result = set(), []
    work = state.get(task['work_id']); reqs = {item['id']: item for item in work['requirements']}
    for finding in findings:
        require(isinstance(finding, dict), 'Use assessment must be an object', 'USE_ASSESSMENT_REQUIRED')
        require(isinstance(finding.get('requirement'), str) and isinstance(finding.get('claim'), str),
                'Use assessment needs exact requirement/claim identities', 'USE_SCOPE_CHANGED')
        key = (finding.get('requirement'), finding.get('claim'))
        require(key in mapped and key not in seen, 'Use assessment must match one exact declared use claim', 'USE_SCOPE_CHANGED'); seen.add(key)
        require(finding.get('state') in ('supports', 'conditional', 'satisfied', 'not_supported')
                and isinstance(finding.get('basis'), str) and finding['basis'].strip()
                and isinstance(finding.get('limitations'), list) and all(isinstance(item, str) for item in finding['limitations']),
                'Use assessment needs actual state, basis and limitations', 'USE_ASSESSMENT_REQUIRED')
        if finding['state'] == 'conditional':
            require(finding['limitations'], 'Conditional use must retain its conditions', 'USE_ASSESSMENT_REQUIRED')
        if mapped[key]['role'] == 'satisfies':
            require(finding['state'] == 'satisfied', 'Task promised full use fulfillment; review has only a partial contribution', 'USE_NOT_FULFILLED')
        proof = None
        if finding['state'] == 'satisfied':
            claim = next(item for item in reqs[key[0]].get('claims', []) if item['id'] == key[1])
            proof = check_contract(state.root, claim.get('completion'), state, {task['id']})
            require(proof['passed'], 'The exact intended-use contract remains incomplete: ' + key[1], 'USE_NOT_FULFILLED')
        result.append({**deepcopy(finding), 'role': mapped[key]['role'], 'level': mapped[key]['level'], 'completion_result': proof})
    require(seen == set(mapped), 'Review every explicitly mapped task use claim', 'USE_ASSESSMENT_REQUIRED')
    return {**scope, 'assessments': result, 'status': 'checked_declared_uses',
            'coverage': 'Only declared task/use contracts. User acceptance, public acceptance and scientific applicability retain their own evidence.'}
