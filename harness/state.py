from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .common import HarnessError, cached, connect, digest, encoded, local_path, now, require
from .fulfillment import bind_contract, check_contract
from . import goals


SCHEMA = """
CREATE TABLE IF NOT EXISTS works(id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
 body TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, work_id TEXT,
 kind TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings(session TEXT PRIMARY KEY, work_id TEXT NOT NULL
 REFERENCES works(id), role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inputs(id INTEGER PRIMARY KEY, session TEXT NOT NULL,
 turn_id TEXT NOT NULL, prompt TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
 UNIQUE(session,turn_id));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
 work_revision INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatches(key TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 target TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, asset TEXT NOT NULL,
 kind TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS judgments(id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
 revision INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_plans(key TEXT PRIMARY KEY REFERENCES dispatches(key), body TEXT NOT NULL);
"""


class State:
    def __init__(self, directory, root=None, readonly=False, *, readonly_snapshot=False):
        from .migrations import inspect_schema, NEW_SCHEMA, VERSION
        from .runtime import SCHEMA as RUNTIME_SCHEMA
        directory = Path(directory)
        self.directory = directory
        self.root = (root or directory.parent).resolve()
        self.readonly = readonly
        path = directory / 'state.sqlite'
        fresh = not path.exists()
        require(fresh or path.stat().st_size > 0, 'Existing empty state file is corrupt; restore explicitly', 'CORRUPT_STORE')
        self.db = connect(path, readonly=readonly, create=fresh)
        try:
            if readonly_snapshot:
                self._readonly_snapshot()
            if fresh and not readonly:
                self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\n' + ';\n'.join((*NEW_SCHEMA, *RUNTIME_SCHEMA))
                                      + ';\nPRAGMA user_version=' + str(VERSION) + ';\nCOMMIT;')
            self.schema_version = inspect_schema(self.db)
            require(readonly or self.schema_version == VERSION,
                    'Existing legacy store is read-only; run migration-plan then migration-apply explicitly', 'MIGRATION_REQUIRED')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _readonly_snapshot(self, timeout=5):
        """Release the live source before a potentially long observational call."""
        require(self.readonly, 'Only read-only state may use an isolated read snapshot')
        source = self.db
        destination = sqlite3.connect(':memory:')
        deadline = time.monotonic() + timeout
        def progress(status, remaining, total):
            require(time.monotonic() < deadline, 'Read snapshot timed out; no live read transaction retained', 'READ_SNAPSHOT_TIMEOUT')
        try:
            # No explicit source BEGIN: backup steps release their source read
            # locks between chunks and retry consistently when the source changes.
            source.execute('PRAGMA busy_timeout=100')
            source.backup(destination, pages=256, progress=progress, sleep=0.01)
            destination.row_factory = sqlite3.Row
            destination.execute('PRAGMA query_only=ON')
            destination.execute('PRAGMA foreign_keys=ON')
        except BaseException:
            destination.close()
            raise
        finally:
            source.close()
        self.db = destination

    @contextmanager
    def transaction(self):
        require(not self.readonly, 'Read-only state connection cannot modify business state')
        if self.db.in_transaction:
            depth = getattr(self, '_transaction_depth', 0) + 1
            self._transaction_depth = depth
            name = 'harness_nested_' + str(depth)
            self.db.execute('SAVEPOINT ' + name)
            try:
                yield
                self.db.execute('RELEASE SAVEPOINT ' + name)
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT ' + name)
                self.db.execute('RELEASE SAVEPOINT ' + name)
                raise
            finally:
                self._transaction_depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, work, kind, source, payload, **metadata):
        require(isinstance(source, str) and bool(source.strip()), "A source or reason is required")
        cursor = self.db.execute("INSERT INTO events(work_id,kind,source,payload,at) VALUES(?,?,?,?,?)",
                                 (work, kind, source, encoded(payload), now()))
        self.db.execute('INSERT INTO event_metadata VALUES(?,?,?,?,?,?,?)',
                        (cursor.lastrowid, 'htam.event/v2', metadata.get('request_id'),
                         metadata.get('external_event_id'), metadata.get('session'),
                         metadata.get('occurred_at'), encoded(metadata['origin']) if 'origin' in metadata else None))
        return cursor.lastrowid

    def get(self, work):
        row = self.db.execute("SELECT * FROM works WHERE id=?", (work,)).fetchone()
        require(row is not None, "Unknown work: " + work)
        body = json.loads(row["body"])
        body.setdefault("request_revision", row["revision"])
        return {"id": work, "revision": row["revision"], **body,
                "updated_at": row["updated_at"]}

    def works(self):
        return [self.get(r[0]) for r in self.db.execute("SELECT id FROM works ORDER BY id")]

    def update(self, work, expected, patch, source, input_id=None, input_expected=None, actor=None):
        allowed = {"title", "request", "request_source", "objective", "status", "owner",
                   "next_action", "constraints", "references", "accepted", "unknowns", "outcome", "completion",
                   "requirements", "change", "execution_policy", "request_interpretation", "representation", "user_acceptance", "parent"}
        require(isinstance(patch, dict) and set(patch) <= allowed, "Unknown work fields")
        with self.transaction():
            from .coordination import check_coordinator_write
            owner_receipt = check_coordinator_write(self, work, actor, patch)
            row = self.db.execute("SELECT * FROM works WHERE id=?", (work,)).fetchone()
            revision = row["revision"] if row else 0
            require(revision == expected, f"Stale work revision: expected {expected}, actual {revision}")
            body = json.loads(row["body"]) if row else {}
            patch = dict(patch)
            change = patch.pop('change', None)
            if isinstance(change, dict):
                change = {key: value for key, value in change.items() if key != 'authorization_receipt'}
            if row:
                from .coordination import authorize_change
                authorization = authorize_change(self, work, patch, change)
                if authorization:
                    change = {**(change or {}), 'authorization_receipt': authorization}
                    bound = dict(body.get('change_authorizations', {}))
                    for key in authorization['statement']['changes']:
                        bound[key] = authorization
                    body['change_authorizations'] = bound
            if "completion" in patch:
                patch['completion'] = goals.bind_changed(self.root, body.get('completion'), patch['completion'], change)
            if not row and 'requirements' not in patch:
                patch['requirements'] = [{'id': 'overall', 'description': patch.get('objective'), 'source': patch.get('request_source')}]
            if 'requirements' in patch:
                patch['requirements'] = goals.bind_requirements(self.root, patch['requirements'], body.get('requirements'), change)
                body['requirement_revision'] = body.get('requirement_revision', 0) + (patch['requirements'] != body.get('requirements'))
            if 'execution_policy' in patch:
                require(isinstance(patch['execution_policy'], dict) and set(patch['execution_policy']) <= {'model', 'thinking', 'target'}, 'Invalid execution policy')
                if body.get('execution_policy') and patch['execution_policy'] != body['execution_policy']:
                    goals.check_change(change)
                    require(change['kind'] == 'scope', 'Changing an explicit execution policy requires user approval provenance')
            if 'parent' in patch:
                from .portfolio import validate_parent
                patch['parent'] = validate_parent(self, work, patch['parent'])
            if 'requirements' in patch:
                from .portfolio import validate_children
                validate_children(self, work, patch['requirements'])
            request_revision = body.get("request_revision", revision)
            request_changed = not row or any(k in patch and patch[k] != body.get(k) for k in ("request", "objective", "constraints"))
            decision_changed = [k for k in ('next_action', 'status', 'objective', 'request', 'constraints', 'requirements', 'representation', 'parent')
                                if k in patch and patch[k] != body.get(k)]
            for field in ('request_interpretation', 'representation', 'user_acceptance'):
                if field in patch:
                    require(isinstance(patch[field], dict) and patch[field].get('source'), field + ' needs a separate explicit source')
            if patch.get('representation', {}).get('changes_use'):
                require(change and change.get('approval_source'), 'A representation changing intended use needs approval provenance')
            if patch.get('user_acceptance', {}).get('status') == 'accepted' and (patch['user_acceptance'] != body.get('user_acceptance')
                    or 'origin_assessment' in body.get('user_acceptance', {})):
                acceptance = patch['user_acceptance'] = dict(patch['user_acceptance'])
                require(type(acceptance.get('input_id')) is int, 'User acceptance needs the actual recorded input id')
                record = self._input_scope(acceptance['input_id'], work)
                if record['scope_relation'] != 'received':
                    require(acceptance.get('scope_revision') == record['scope_revision'],
                            'Acceptance needs the exact explicit input scope revision', 'REVISION_CONFLICT')
                assessment = self.input_assessment(record['id'])
                require(assessment['kind'] == 'human_direct' and assessment['status'] == 'reviewed',
                        'User acceptance needs explicit human-origin review of the exact input', 'PROVENANCE_UNVERIFIED')
                acceptance['origin_assessment'] = {'revision': assessment['revision'],
                    'input_sha256': assessment['input_sha256'], 'reviewer': assessment['reviewer'],
                    'source': assessment['source']}
                acceptance['input_scope'] = {'work_id': work, 'revision': record['scope_revision'],
                                             'relation': record['scope_relation']}
            body.update(patch)
            if decision_changed and body.get('decision'):
                body['decision'] = {**body['decision'], 'validity': 'needs_reconciliation',
                                    'invalidated_by': decision_changed, 'facts_changed_at': now()}
            body["request_revision"] = request_revision + 1 if request_changed else request_revision
            require(body.get("status") in ("active", "paused", "waiting", "completed"), "Invalid work status")
            for key in ("title", "request", "request_source", "objective"):
                require(isinstance(body.get(key), str) and bool(body[key].strip()), "Missing " + key)
            require(body["status"] != "completed" or bool(body.get("outcome")), "Completion needs an outcome reference and explanation")
            if body["status"] == "completed":
                require(body.get('requirements'), 'Migrate overall requirements before work completion')
                remaining_inputs = [i for i in self.blocking_inputs(work, operation='adopt') if i["id"] != input_id]
                require(not remaining_inputs, "Reconcile new user input before completion")
                closure = goals.completion(self, {'id': work, **body})
                require(closure["passed"], "Completion evidence: " + "; ".join(closure["blockers"]))
                body["completion_result"] = closure
            for key in ("constraints", "references", "accepted", "unknowns"):
                require(isinstance(body.get(key, []), list), key + " must be a list")
            self.db.execute("INSERT INTO works VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body,updated_at=excluded.updated_at",
                            (work, revision + 1, encoded(body), now()))
            self.event(work, "work_updated", source, {"before_revision": revision, "after": body, "change": change,
                       **({'coordinator_write': owner_receipt} if owner_receipt else {})})
            if input_id is not None:
                self._ack(input_id, work, source, input_expected)
        return self.get(work)

    def bind(self, session, work, role, source, expected=None):
        require(role in ("main", "executor", "observer"), "Invalid role")
        with self.transaction():
            self.get(work)
            previous = self.binding(session)
            revision = previous['revision'] if previous else 0
            changed = previous and (previous['work_id'] != work or previous['role'] != role)
            require(expected is not None or not changed,
                    'Changing a session binding needs --expected-binding revision', 'BINDING_REVISION_REQUIRED')
            require(expected is None or expected == revision, 'Stale binding revision', 'REVISION_CONFLICT')
            if previous and not changed:
                return previous
            self.db.execute("INSERT INTO bindings VALUES(?,?,?) ON CONFLICT(session) DO UPDATE SET work_id=excluded.work_id,role=excluded.role", (session, work, role))
            self.db.execute('INSERT INTO binding_versions VALUES(?,?) ON CONFLICT(session) DO UPDATE SET revision=excluded.revision', (session, revision + 1))
            self.event(work, "session_bound", source, {"session": session, "role": role,
                'before': previous, 'revision': revision + 1}, session=session)
            return self.binding(session)

    def binding(self, session):
        row = self.db.execute("SELECT * FROM bindings WHERE session=?", (session,)).fetchone()
        if row is None:
            return None
        version = self.db.execute('SELECT revision FROM binding_versions WHERE session=?', (session,)).fetchone() if self.schema_version >= 2 else None
        return {**dict(row), 'revision': version[0] if version else 0}

    def input(self, session, turn, prompt, *, raw_source=None, receipt_event_seq=None, observed_at=None, occurred_at=None):
        from .migrations import input_hash
        require(bool(session) and bool(turn), "Session and turn identity required")
        require(isinstance(prompt, str), 'Input prompt must be text')
        content_hash = input_hash(session, turn, prompt, raw_source)
        with self.transaction():
            existing = self.db.execute('SELECT id FROM inputs WHERE session=? AND turn_id=?', (session, turn)).fetchone()
            if existing:
                record = self.input_record(existing[0])
                require(record['input_sha256'] == content_hash, 'Input identity replay has different original content/source', 'INPUT_IDENTITY_CONFLICT')
                return record
            binding = self.binding(session)
            work = binding['work_id'] if binding else None
            received_at = observed_at or now()
            if receipt_event_seq is not None:
                receipt_event = self.db.execute('SELECT work_id,kind,payload,at FROM events WHERE seq=?', (receipt_event_seq,)).fetchone()
                require(receipt_event and receipt_event['kind'] == 'hook_observed', 'Input needs its actual hook receipt event')
                data = json.loads(receipt_event['payload'])
                require(receipt_event['work_id'] == work and data.get('session') == session
                        and data.get('turn') == turn and data.get('event') == 'UserPromptSubmit', 'Input receipt event differs')
                received_at = receipt_event['at']
            cursor = self.db.execute('INSERT INTO inputs(session,turn_id,prompt) VALUES(?,?,?)', (session, turn, prompt))
            identifier = cursor.lastrowid
            self.db.execute('INSERT INTO input_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier, work, 'recorded' if work else 'unbound', encoded(raw_source), content_hash,
                 received_at, occurred_at, receipt_event_seq, encoded({'capture': 'first reception', 'source_status': 'unknown'})))
            if work:
                self.db.execute('INSERT INTO input_scopes VALUES(?,?,?,?,?,?,?,?)',
                    (identifier, work, 'received', 1, 0, 'first receipt', encoded({'receipt_event_seq': receipt_event_seq}), received_at))
            self.event(work, 'input_received', 'input capture', {'input_id': identifier, 'input_sha256': content_hash,
                       'receipt_event_seq': receipt_event_seq, 'received_work_id': work}, session=session, occurred_at=occurred_at, origin=raw_source)
            return self.input_record(identifier)

    def _legacy_inputs(self):
        from .migrations import legacy_inputs
        version = self.db.execute('PRAGMA data_version').fetchone()[0]
        if getattr(self, '_legacy_cache_version', None) != version:
            self._legacy_cache = legacy_inputs(self.db)
            self._legacy_cache_version = version
        return self._legacy_cache

    def input_record(self, identifier):
        row = self.db.execute('SELECT * FROM inputs WHERE id=?', (identifier,)).fetchone()
        require(row is not None, 'Unknown input: ' + str(identifier), 'INPUT_NOT_FOUND')
        if self.schema_version < 2:
            receipt = next(item['receipt'] for item in self._legacy_inputs() if item['input']['id'] == identifier)
        else:
            receipt = self.db.execute('SELECT * FROM input_receipts WHERE input_id=?', (identifier,)).fetchone()
            require(receipt is not None, 'Input is missing its receipt record', 'CORRUPT_STORE')
        return {**dict(row), **dict(receipt)}

    def _input_scope(self, identifier, work):
        record = self.input_record(identifier)
        if self.schema_version < 2:
            entry = next(item for item in self._legacy_inputs() if item['input']['id'] == identifier)
            scope = next((item for item in entry['scopes'] if item['work_id'] == work), None)
            scope = {**scope, 'revision': 0} if scope else None
        else:
            scope = self.db.execute('SELECT * FROM input_scopes WHERE input_id=? AND work_id=?', (identifier, work)).fetchone()
        require(scope is not None, 'Input is not explicitly applicable to this work', 'INPUT_SCOPE_UNRESOLVED')
        scope = dict(scope)
        scope_body = scope.get('body', {})
        if isinstance(scope_body, str):
            scope_body = json.loads(scope_body)
        require(scope_body.get('status', 'active') != 'retracted', 'Input scope was explicitly retracted', 'INPUT_SCOPE_RETRACTED')
        return {**record, 'scope_work_id': work, 'scope_relation': scope['relation'],
                'scope_revision': scope['revision'], 'scope_acknowledged': scope['acknowledged']}

    def input_assessment(self, identifier):
        record = self.input_record(identifier)
        row = self.db.execute('SELECT * FROM input_assessments WHERE input_id=? ORDER BY revision DESC LIMIT 1', (identifier,)).fetchone() if self.schema_version >= 2 else None
        if not row:
            return {'revision': 0, 'kind': 'unknown', 'status': 'unreviewed', 'input_sha256': record['input_sha256']}
        result = dict(row)
        result['body'] = json.loads(result['body'])
        current = result['input_sha256'] == record['input_sha256']
        for evidence in result['body']['evidence']:
            try:
                path = local_path(self.root, evidence['path'])
                current = current and path.is_file() and digest(path) == evidence['sha256']
            except (HarnessError, OSError):
                current = False
        result['status'] = 'reviewed' if current else 'needs_review'
        return result

    def input_link(self, identifier, work, expected, scope_expected, body, source):
        require(isinstance(body, dict) and all(isinstance(body.get(k), str) and body[k].strip()
                for k in ('reviewer', 'source_ref', 'reason')), 'Input link needs reviewer/source_ref/reason')
        status = body.get('status', 'active')
        require(status in ('active', 'retracted'), 'Input scope status must be active or retracted')
        require(status == 'retracted' or isinstance(body.get('purpose'), str) and body['purpose'].strip(), 'Active input link needs a purpose')
        with self.transaction():
            require(self.get(work)['revision'] == expected, 'Stale work revision', 'REVISION_CONFLICT')
            record = self.input_record(identifier)
            previous = self.db.execute('SELECT * FROM input_scopes WHERE input_id=? AND work_id=?', (identifier, work)).fetchone()
            revision = previous['revision'] if previous else 0
            require(revision == scope_expected, 'Stale input scope revision', 'REVISION_CONFLICT')
            require(not previous or previous['relation'] != 'received', 'The original received scope is immutable')
            require(status != 'retracted' or previous is not None, 'Cannot retract a nonexistent input scope', 'INPUT_SCOPE_UNRESOLVED')
            body = {**body, 'status': status}
            acknowledged = previous['acknowledged'] if previous and status == 'retracted' else 0
            self.db.execute('INSERT INTO input_scopes VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(input_id,work_id) DO UPDATE SET relation=excluded.relation,revision=excluded.revision,acknowledged=excluded.acknowledged,source=excluded.source,body=excluded.body,at=excluded.at',
                (identifier, work, 'explicitly_linked', revision + 1, acknowledged, source, encoded(body), now()))
            self.event(work, 'input_scope_retracted' if status == 'retracted' else 'input_linked', source,
                       {'input_id': identifier, 'input_sha256': record['input_sha256'],
                        'revision': revision + 1, 'link': body, 'previous': dict(previous) if previous else None})
            return {'input_id': identifier, 'work_id': work, 'scope_revision': revision + 1,
                    'status': status, 'applicable': status == 'active'}

    def assess_input(self, identifier, expected, body, source):
        kinds = {'human_direct', 'app_suggestion', 'agent_message', 'tool_or_quote', 'unknown'}
        require(isinstance(body, dict) and body.get('kind') in kinds, 'Unknown input source assessment kind')
        require(all(isinstance(body.get(k), str) and body[k].strip() for k in ('reviewer', 'source_ref', 'quote', 'reason', 'input_sha256')),
                'Input assessment needs reviewer/source_ref/quote/reason/input_sha256')
        require(isinstance(body.get('evidence'), list) and bool(body['evidence']), 'Input assessment needs local evidence pointers')
        with self.transaction():
            record = self.input_record(identifier)
            require(body['input_sha256'] == record['input_sha256'], 'Assessment input content changed', 'INPUT_IDENTITY_CONFLICT')
            require(body['quote'] in record['prompt'], 'Assessment quote is not in the captured original input')
            previous = self.db.execute('SELECT MAX(revision) FROM input_assessments WHERE input_id=?', (identifier,)).fetchone()[0] or 0
            require(previous == expected, 'Stale input assessment revision', 'REVISION_CONFLICT')
            body = {**body, 'evidence': [dict(item) for item in body['evidence']]}
            for item in body['evidence']:
                path = local_path(self.root, item.get('path', ''))
                require(path.is_file(), 'Input origin evidence file is missing')
                actual = digest(path)
                require(not item.get('sha256') or item['sha256'] == actual, 'Input origin evidence changed')
                item['sha256'] = actual
            self.db.execute('INSERT INTO input_assessments VALUES(?,?,?,?,?,?,?,?)',
                (identifier, previous + 1, body['kind'], body['reviewer'], source, record['input_sha256'], encoded(body), now()))
            self.event(record['received_work_id'], 'input_origin_assessed', source,
                       {'input_id': identifier, 'revision': previous + 1, 'assessment': body})
            return self.input_assessment(identifier)

    def input_query(self, identifier=None, work=None, session=None, limit=8):
        require(1 <= limit <= 100, 'Input query limit must be 1..100')
        clauses, values = [], []
        if identifier is not None:
            clauses.append('inputs.id=?'); values.append(identifier)
        if session is not None:
            clauses.append('inputs.session=?'); values.append(session)
        if work is not None and self.schema_version >= 2:
            clauses.append("EXISTS(SELECT 1 FROM input_scopes WHERE input_id=inputs.id AND work_id=? AND COALESCE(json_extract(body,'$.status'),'active')='active')"); values.append(work)
        if work is not None and self.schema_version < 2:
            selected = [item['input']['id'] for item in self._legacy_inputs()
                        if any(scope['work_id'] == work for scope in item['scopes'])]
            clauses.append('inputs.id IN (' + ','.join('?' for _ in selected) + ')'); values.extend(selected)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        total = self.db.execute('SELECT COUNT(*) FROM inputs' + where, values).fetchone()[0]
        identifiers = [row[0] for row in self.db.execute('SELECT inputs.id FROM inputs' + where + ' ORDER BY id DESC LIMIT ?', [*values, limit])]
        result = []
        for item_id in identifiers:
            record = self.input_record(item_id)
            if work is not None:
                record = self._input_scope(item_id, work)
            assessment = self.input_assessment(item_id)
            if identifier is None:
                record = {k: v for k, v in record.items() if k not in ('provenance',)}
                record['prompt'] = record['prompt'][:550]
                assessment = {k: v for k, v in assessment.items() if k != 'body'}
            record['origin_assessment'] = assessment
            if identifier is not None and self.schema_version >= 2:
                record['scopes'] = [{**dict(row), 'applicable': json.loads(row['body']).get('status', 'active') == 'active'}
                                    for row in self.db.execute('SELECT * FROM input_scopes WHERE input_id=? ORDER BY work_id', (item_id,))]
            result.append(record)
        return {'items': result, 'total': total, 'omitted': total - len(result), 'schema_version': self.schema_version,
                'coverage': 'Captured receipt and explicit scope only; source kind is reviewed separately; list prompts bounded to 550 characters'}

    def pending(self, work):
        if self.schema_version < 2:
            identifiers = [entry['input']['id'] for entry in self._legacy_inputs() if any(
                scope['work_id'] == work and not scope['acknowledged'] for scope in entry['scopes'])]
        else:
            identifiers = [row[0] for row in self.db.execute("SELECT input_id FROM input_scopes WHERE work_id=? AND acknowledged=0 AND COALESCE(json_extract(body,'$.status'),'active')='active' ORDER BY input_id", (work,))]
        result = []
        for identifier in identifiers:
            assessment = self.input_assessment(identifier)
            if assessment['status'] == 'reviewed' and assessment['kind'] in {'app_suggestion', 'agent_message', 'tool_or_quote'}:
                continue
            result.append({**self._input_scope(identifier, work), 'source_kind': assessment['kind'],
                           'source_status': assessment['status']})
        return result

    def input_effect(self, identifier, work):
        """Latest exact semantic assessment; origin authority remains separate."""
        record = self._input_scope(identifier, work)
        row = self.db.execute("SELECT seq,payload FROM events WHERE work_id=? AND kind='input_effect_assessed' AND json_extract(payload,'$.input_id')=? ORDER BY seq DESC LIMIT 1", (work, identifier)).fetchone()
        if not row:
            return None
        effect = json.loads(row['payload'])
        origin = self.input_assessment(identifier)
        if (effect['input_sha256'] != record['input_sha256'] or effect['scope_revision'] != record['scope_revision']
                or effect['origin_revision'] != origin['revision'] or effect['origin_status'] != origin['status']):
            return None
        return {**effect, 'event_seq': row['seq']}

    def interpret_input(self, identifier, work, expected, body, source):
        """Save a sourced semantic judgment, never infer human authorization."""
        require(isinstance(body, dict) and body.get('kind') in
                ('question', 'suggestion', 'correction', 'pause', 'resume', 'authorization_change'), 'Unknown input meaning')
        require(all(isinstance(body.get(k), str) and body[k].strip() for k in
                    ('quote', 'reviewer', 'source_ref', 'reason', 'impact')), 'Input meaning needs original quote, reviewer, source, reason and impact')
        with self.transaction():
            require(self.get(work)['revision'] == expected, 'Stale work revision', 'REVISION_CONFLICT')
            record = self._input_scope(identifier, work)
            previous = self.db.execute("SELECT MAX(seq) FROM events WHERE work_id=? AND kind='input_effect_assessed' AND json_extract(payload,'$.input_id')=?", (work, identifier)).fetchone()[0] or 0
            require(body.get('effect_expected', 0) == previous, 'Stale input-effect assessment', 'REVISION_CONFLICT')
            require(body['quote'] in record['prompt'] and body.get('input_sha256') == record['input_sha256'],
                    'Input meaning must bind the original input', 'INPUT_IDENTITY_CONFLICT')
            require(body.get('scope_revision') == record['scope_revision'], 'Stale input scope revision', 'REVISION_CONFLICT')
            scope = body.get('scope', 'work'); tasks = body.get('tasks', [])
            require(scope in ('work', 'tasks') and isinstance(tasks, list) and len(tasks) == len(set(tasks)), 'Specify work or exact task scope')
            require((scope == 'tasks' and tasks) or (scope == 'work' and not tasks), 'Task scope needs exact task roots')
            for task_id in tasks:
                task_record = self.task(task_id)
                require(task_record['work_id'] == work, 'Input effect crosses work boundaries')
                goals.current_task(self, task_record)
            operations = body.get('operations', ['dispatch', 'adopt'])
            require(isinstance(operations, list) and operations and set(operations) <= {'dispatch', 'adopt'}, 'Unknown input effect operation')
            origin = self.input_assessment(identifier)
            value = {'input_id': identifier, 'input_sha256': record['input_sha256'], 'scope_revision': record['scope_revision'],
                'origin_revision': origin['revision'], 'origin_status': origin['status'], 'assessment': {**body, 'scope': scope, 'tasks': tasks, 'operations': operations},
                'authority': 'Semantic impact only; source review and any authorization remain independently required'}
            self.event(work, 'input_effect_assessed', source, value)
            return self.input_effect(identifier, work)

    def blocking_inputs(self, work, task=None, operation='dispatch'):
        """Pending attention is visible; only affected actions are held."""
        from .coordination import _affected
        result = []
        for record in self.pending(work):
            effect = self.input_effect(record['id'], work)
            if effect:
                meaning = effect['assessment']
                if meaning['kind'] in ('question', 'suggestion', 'resume') or operation not in meaning['operations']:
                    continue
                if task is not None and meaning['scope'] == 'tasks':
                    affected = _affected({t['id']: t for t in self.tasks(work)}, meaning['tasks'])
                    if task not in affected:
                        continue
            result.append({**record, 'effect': effect})
        return result

    def _ack(self, identifier, work, note, scope_expected=None):
        row = self._input_scope(identifier, work)
        require(row['scope_relation'] == 'received' or scope_expected is not None,
                'Acknowledging an explicit input scope needs its revision', 'INPUT_SCOPE_REVISION_REQUIRED')
        require(scope_expected is None or row['scope_revision'] == scope_expected,
                'Stale input scope revision', 'REVISION_CONFLICT')
        if row['scope_acknowledged']:
            return
        self.db.execute('UPDATE input_scopes SET acknowledged=1,revision=revision+1 WHERE input_id=? AND work_id=? AND revision=?',
                        (identifier, work, row['scope_revision']))
        if row['scope_relation'] == 'received':
            self.db.execute('UPDATE inputs SET acknowledged=1 WHERE id=?', (identifier,))
        self.event(work, "input_reconciled", note, {"input_id": identifier, 'input_sha256': row['input_sha256'],
                   'scope_revision': row['scope_revision'] + 1})

    def ack(self, identifier, work, expected, note, scope_expected=None):
        with self.transaction():
            current = self.get(work)
            require(current["revision"] == expected, "Stale work revision", 'REVISION_CONFLICT')
            self._ack(identifier, work, note, scope_expected)

    def task(self, identifier):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
        require(row is not None, "Unknown task: " + identifier)
        return {**dict(row), "body": json.loads(row["body"])}

    def tasks(self, work):
        def load():
            return [self.task(r[0]) for r in self.db.execute("SELECT id FROM tasks WHERE work_id=? ORDER BY rowid", (work,))]
        return cached(('tasks', id(self.db), self.db.total_changes, work), load)

    def create_task(self, identifier, work, expected, body, source):
        require(isinstance(body, dict) and body.get("expected") and body.get("purpose"), "Task needs expected result and purpose")
        require(isinstance(body.get("judgments", []), list), "Task judgment dependencies must be a list")
        if "completion" in body:
            body = {**body, "completion": bind_contract(self.root, body["completion"])}
        with self.transaction():
            require(self.get(work)["revision"] == expected, "Stale work revision")
            require(not self.db.execute("SELECT 1 FROM tasks WHERE id=?", (identifier,)).fetchone(), "Task already exists")
            body = goals.links(self, self.get(work), identifier, body)
            self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (identifier, work, self.get(work)["request_revision"], encoded(body), "proposed"))
            self.event(work, "task_proposed", source, {"id": identifier, "body": body})
        return self.task(identifier)

    def set_task_completion(self, identifier, expected, contract, source, change=None):
        with self.transaction():
            task = self.task(identifier)
            work = self.get(task["work_id"])
            require(work["revision"] == expected, "Stale work revision")
            require(task["work_revision"] == work["request_revision"], "Task belongs to an old request")
            body = task["body"]
            before = body.get("completion")
            if isinstance(change, dict):
                change = {key: value for key, value in change.items() if key != 'authorization_receipt'}
            from .coordination import authorize_change
            authorization = authorize_change(self, work['id'], {'completion': contract}, change, task_id=identifier)
            if authorization:
                change = {**(change or {}), 'authorization_receipt': authorization}
                bound = dict(body.get('change_authorizations', {}))
                for key in authorization['statement']['changes']:
                    bound[key] = authorization
                body['change_authorizations'] = bound
            body["completion"] = goals.bind_changed(self.root, before, contract, change)
            status = "returned" if task["status"] == "adopted" else task["status"]
            self.db.execute("UPDATE tasks SET body=?,status=? WHERE id=?", (encoded(body), status, identifier))
            # Use the existing work revision for optimistic concurrency on evidence refresh.
            self.db.execute("UPDATE works SET revision=revision+1,updated_at=? WHERE id=?", (now(), task["work_id"]))
            self.event(task["work_id"], "completion_evidence_bound", source,
                       {"task": identifier, "before": before, "after": body["completion"], "status": status, "change": change})
        return self.task(identifier)

    def review_task(self, identifier, disposition, review, source):
        require(disposition in ("returned", "adopted", "revise", "abandoned"), "Invalid task disposition")
        require(isinstance(review, dict), "Review must be an object")
        with self.transaction():
            task = self.task(identifier)
            stage_outcome = review.get('stage_outcome')
            if stage_outcome is not None:
                require(disposition == 'returned' and stage_outcome in ('handled', 'unresolved', 'revise'),
                        'Stage review records handled, unresolved or revise while preserving task disposition')
                require(review.get('native_entry_id') and type(review.get('return_event')) is int,
                        'Stage review needs the exact native return', 'IDENTITY_MISMATCH')
                if stage_outcome == 'handled':
                    require(review.get('review_role') == 'independent' and review.get('review_mode') == 'independent',
                            'Handling an independent stage review requires actual independent review')
                    for key in ('artifacts', 'answer', 'remaining', 'reviewer'):
                        require(key in review and (review[key] or key == 'remaining'), 'Stage review missing ' + key)
                    review = {**review, 'artifact_evidence': self._file_evidence(review['artifacts'], 'runtime')}
            if review.get('review_mode'):
                producers = {task['body'].get('executor_session'), task['body'].get('producer')} - {None, ''}
                producers.update(r[0] for r in self.db.execute(
                    "SELECT target FROM dispatches WHERE task_id=? AND status IN ('sent','started','returned','unknown')", (identifier,)))
                producers.update(r[0] for r in self.db.execute("SELECT DISTINCT json_extract(payload,'$.accepted_by') FROM events WHERE kind='native_entry_state' AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.accepted_by') IS NOT NULL", (identifier,)))
                reviewer = review.get('reviewer')
                require(not (review['review_mode'] == 'independent' and reviewer in producers),
                        'Known producer cannot label its own review independent')
                review = {**review, 'review_identity': {
                    'known_producers': sorted(producers),
                    'status': 'unverified' if not producers or not reviewer else
                              'same_recorded_identity' if reviewer in producers else 'different_recorded_identities',
                    'basis': 'Recorded task executor/observed dispatch targets versus declared reviewer; not an authentication claim'}}
            if disposition in ('adopted', 'revise', 'abandoned') or stage_outcome is not None:
                if disposition in ('adopted', 'revise'):
                    goals.current_task(self, task)
                if self.schema_version >= 3:
                    from .runtime import Runtime
                    runtime_review = Runtime(self).before_review(identifier, review)
                    if runtime_review:
                        review = {**review, 'runtime_review_receipt': runtime_review}
                        if disposition == 'abandoned' and runtime_review['body'].get('native_entry_id'):
                            for key in ('artifacts', 'answer', 'remaining', 'reviewer', 'review_mode', 'review_role'):
                                require(key in review and (review[key] or key == 'remaining'), 'Native return closure missing ' + key)
                            require(review['review_role'] in ('coordination', 'independent')
                                    and review['review_mode'] in ('self', 'independent'), 'Declare actual return closure review role and mode')
                            review = {**review, 'artifact_evidence': self._file_evidence(review['artifacts'], 'runtime')}
            if disposition == 'revise':
                for key in ('artifacts', 'answer', 'reviewer', 'review_mode', 'findings'):
                    require(review.get(key), 'Repair review missing ' + key)
                require(review['review_mode'] in ('self', 'independent'), 'Specify actual review mode')
                require(isinstance(review['findings'], list), 'Repair findings must be a list')
                for finding in review['findings']:
                    require(isinstance(finding, dict) and finding.get('requirement') in task['body']['contributes_to'], 'Repair must identify an affected requirement')
                    for key in ('issue', 'impact', 'finish_when'):
                        require(isinstance(finding.get(key), str) and finding[key].strip(), 'Repair finding missing ' + key)
                if task['status'] == 'adopted':
                    require(review.get('reopen_evidence'), 'Reopening adopted work needs new evidence or a concrete error')
            if disposition == "adopted":
                goals.ready(self, task)
                from .coordination import check_admission
                review = {**review, 'coordination_result': check_admission(self, task, operation='adopt', review=review)}
                require(task["status"] in ("returned", "revise"), "Return the result before adopting it")
                require(task["work_revision"] == self.get(task["work_id"])["request_revision"], "Task is based on an old request; propose a current task referencing its result")
                for key in ("artifacts", "answer", "remaining", "reviewer", "review_mode"):
                    require(key in review and (review[key] or key == "remaining"), "Review missing " + key)
                require(review["review_mode"] in ("self", "independent"), "Specify actual review mode")
                require(not self.blocking_inputs(task["work_id"], task=identifier, operation='adopt'), "Reconcile affected user input before adoption")
                closure = check_contract(self.root, task["body"].get("completion"), self, {identifier})
                require(closure["passed"], "Adoption evidence: " + "; ".join(closure["blockers"]))
                review = {**review, "completion_result": closure}
                for ref in review['artifacts']:
                    from .common import local_path
                    require(isinstance(ref, str) and local_path(self.root, ref).is_file(), 'Adoption artifact must exist')
            body = task["body"]
            body.setdefault("reviews", []).append({"disposition": disposition, "review": review, "source": source, "at": now()})
            status = task['status'] if stage_outcome is not None else disposition
            self.db.execute("UPDATE tasks SET status=?,body=? WHERE id=?", (status, encoded(body), identifier))
            self.event(task["work_id"], 'native_stage_reviewed' if stage_outcome is not None else "task_" + disposition,
                       source, {"task": identifier, "review": review})
            if self.schema_version >= 3:
                from .runtime import Runtime
                Runtime(self).on_review(identifier, disposition, review, source)
        return self.task(identifier)

    def relink_task(self, identifier, expected, patch, source):
        """Explicit migration/reconciliation of relationships, never rewrites old reviews."""
        require(isinstance(patch, dict) and set(patch) <= {'contributes_to', 'parent_task', 'requires', 'kind', 'judgments'}, 'Only task relationships may be reconciled')
        with self.transaction():
            task = self.task(identifier); work = self.get(task['work_id'])
            require(work['revision'] == expected, 'Stale work revision')
            require(task['work_revision'] == work['request_revision'], 'Cannot migrate old-request task as current')
            body = {**task['body'], **patch}
            if 'requires' in patch:
                body.pop('depends_on', None)
            body = goals.links(self, work, identifier, body, bind_versions='judgments' in patch)
            status = task['status']
            if status == 'adopted' and task['body'].get('requirement_versions'):
                if any(body.get(k) != task['body'].get(k) for k in ('requirement_versions', 'contributes_to', 'requires', 'judgments')):
                    status = 'returned'
            self.db.execute('UPDATE tasks SET body=?,status=? WHERE id=?', (encoded(body), status, identifier))
            self.db.execute('UPDATE works SET revision=revision+1,updated_at=? WHERE id=?', (now(), work['id']))
            self.event(work['id'], 'task_reconciled', source, {'task': identifier, 'before': task['body'], 'after': body})
        return self.task(identifier)

    def decide(self, work_id, expected, decision, source, actor=None):
        require(isinstance(decision, dict), 'Decision must be an object')
        require(decision.get('action') in ('continue', 'change', 'deliver', 'ask', 'wait'), 'Invalid overall action')
        for key in ('reason', 'expected_gain'):
            require(isinstance(decision.get(key), str) and decision[key].strip(), 'Decision missing ' + key)
        with self.transaction():
            work = self.get(work_id)
            require(work['revision'] == expected, 'Stale work revision')
            from .coordination import prepare_decision, check_coordinator_write
            owner_receipt = check_coordinator_write(self, work_id, actor, decision)
            if owner_receipt:
                require(decision.get('responsible', owner_receipt['coordinator']) == owner_receipt['coordinator'],
                        'Overall responsible must be the current coordinator', 'COORDINATOR_REQUIRED')
                decision = {**decision, 'responsible': owner_receipt['coordinator'], 'coordinator_write': owner_receipt}
            decision = prepare_decision(self, work, decision)
            selected_ids = decision.get('tasks', [])
            require(not any(self.blocking_inputs(work_id, task=t) for t in selected_ids)
                    if selected_ids else not self.blocking_inputs(work_id), 'Reconcile affected user input before deciding')
            reqs = {r['id']: r for r in work.get('requirements', [])}
            require(reqs, 'Migrate overall requirements first')
            task_ids = decision.get('tasks', [])
            require(isinstance(task_ids, list) and len(task_ids) == len(set(task_ids)), 'Decision tasks must be unique ids')
            selected = [self.task(i) for i in task_ids]
            for task in selected:
                require(task['work_id'] == work_id, 'Decision task belongs to another work')
                goals.current_task(self, task)
            assessments = dict(work.get('assessments', {})); reviewed = set()
            for supplied in decision.get('assessments', []):
                a = dict(supplied); rid = a.get('requirement')
                require(rid in reqs and rid not in reviewed, 'Unknown/duplicate requirement assessment')
                reviewed.add(rid)
                require(a.get('state') in ('open', 'partial', 'blocked', 'satisfied'), 'Invalid requirement state')
                for field in ('remaining', 'limitations', 'supports'):
                    require(isinstance(a.get(field), list), 'Assessment needs ' + field)
                require(a.get('reason'), 'Assessment needs its semantic reason')
                for support in a['supports']:
                    t = self.task(support); goals.ready(self, t)
                    require(t['work_id'] == work_id and rid in t['body']['contributes_to'] and t['status'] == 'adopted', 'Assessment support is not a current adopted contribution')
                    checked = check_contract(self.root, t['body'].get('completion'), self, {support})
                    require(checked['passed'], 'Assessment support evidence changed: ' + support)
                if a['state'] == 'satisfied':
                    require(not a['remaining'], 'Satisfied requirement cannot have required work remaining')
                    proof = check_contract(self.root, reqs[rid].get('completion'), self)
                    require(proof['passed'], 'Overall requirement proof is incomplete: ' + rid)
                if 'claims' in a:
                    require(isinstance(a['claims'], list), 'Claim assessments must be a list')
                    known = {c['id']: c for c in reqs[rid].get('claims', [])}; used = set()
                    for claim in a['claims']:
                        require(isinstance(claim, dict) and claim.get('claim') in known and claim['claim'] not in used, 'Unknown/duplicate claim assessment')
                        used.add(claim['claim'])
                        require(claim.get('state') in ('open', 'partial', 'blocked', 'satisfied', 'not_applicable') and claim.get('reason'), 'Use claim needs state and reason')
                        if claim['state'] == 'not_applicable':
                            require(known[claim['claim']]['applicability'] == 'conditional', 'Required unconditional claim cannot be waived')
                            claim['applicability_evidence'] = bind_contract(self.root, claim.get('applicability_evidence'))
                if a['state'] == 'satisfied':
                    uses = goals.claim_view(self, reqs[rid], a)
                    require(all(not c['required'] or c['state'] in ('satisfied', 'not_applicable') for c in uses), 'Required use claim remains open or unsupported: ' + rid)
                a['frontier'] = goals.frontier(self, work, [rid]); a['source'] = source
                assessments[rid] = a
            require(set(assessments) >= set(reqs), 'Assess every overall requirement at least once')
            for t in selected:
                for rid in t['body']['contributes_to']:
                    require(assessments[rid]['frontier'] == goals.frontier(self, work, [rid]), 'Reconcile the latest result before continuation: ' + rid)
            decision_work = {**work, 'assessments': assessments}
            next_action = decision.get('next_action', work.get('next_action', decision['reason']))
            previous_results = []
            for tid in work.get('decision', {}).get('tasks', []):
                prior_task = self.task(tid)
                reviews = prior_task['body'].get('reviews', [])
                latest = reviews[-1] if reviews else {}
                previous_results.append({'task': tid, 'status': prior_task['status'],
                    'review_index': len(reviews) - 1 if reviews else None,
                    'answer': str(latest.get('review', {}).get('answer', ''))[:1000], 'returned_at': latest.get('at')})
            last_return = self.db.execute("SELECT at FROM events WHERE work_id=? AND kind='task_returned' ORDER BY seq DESC LIMIT 1", (work_id,)).fetchone()
            observed_at = now()
            def seconds_since(stamp):
                try: return max(0, (datetime.fromisoformat(observed_at)-datetime.fromisoformat(stamp)).total_seconds()) if stamp else None
                except (TypeError,ValueError): return None
            value = {**decision, 'source': source, 'request_revision': work['request_revision'], 'at': now(),
                'revision': work.get('decision', {}).get('revision', 0) + 1,
                'next_action': next_action, 'work_status': work['status'], 'responsible': decision.get('responsible', 'main'), 'validity': 'current',
                'previous_outcome_comparison': {'expected_gain': work.get('decision', {}).get('expected_gain'),
                    'actual_returns': previous_results, 'elapsed_since_previous_decision_seconds':seconds_since(work.get('decision',{}).get('at')),
                    'seconds_since_latest_recorded_return':seconds_since(last_return[0] if last_return else None), 'meaning': 'Observed returns, not an automatic judgment of scientific gain or effectiveness'},
                'task_stamps': {t['id']: {'frontier_kind': 'task-dependencies-v1', 'frontier': goals.task_frontier(self, decision_work, t),
                    'task': goals.fingerprint({k:v for k,v in t['body'].items() if k != 'reviews'})} for t in selected}}
            body = {k:v for k,v in work.items() if k not in ('id', 'revision', 'updated_at')}
            # History keeps the original decision, while current state points
            # at the single maintained assessment copy.
            current_decision = goals.stored_decision(decision_work,
                {key: item for key, item in value.items() if key != 'assessments'})
            sequence = self.event(work_id, 'overall_decision', source, value)
            current_decision['history_event'] = sequence
            body.update(assessments=assessments, decision=current_decision, next_action=next_action)
            self.db.execute('UPDATE works SET body=?,revision=revision+1,updated_at=? WHERE id=?', (encoded(body), now(), work_id))
        return value

    def compact_current_state(self, work_id, expected, source, *, text_moves=(), deduplicate_paths=(), actor=None, apply=False):
        """Preview/apply explicitly reviewed representation maintenance only.

        Text segments must reconstruct the exact original. Callers label the
        historical text; this helper never infers scientific meaning or splits
        a reason automatically. Original decision/text remains in the event.
        """
        require(isinstance(source, str) and source.strip(), 'Maintenance needs its approval/source reference')
        require(type(apply) is bool, 'apply must be boolean')
        with self.transaction() if apply else nullcontext():
            work = self.get(work_id)
            require(work['revision'] == expected, 'Stale work revision', 'REVISION_CONFLICT')
            body = deepcopy({k: v for k, v in work.items() if k not in ('id', 'revision', 'updated_at')})
            original_decision = deepcopy(body.get('decision'))
            if original_decision:
                body['decision'] = goals.stored_decision(work, original_decision)
            changes = []; seen = set()
            def target(path, fields):
                require(isinstance(path, list) and all(isinstance(part, str) for part in path), 'Maintenance path must be explicit strings')
                valid = ((len(path) == 3 and path[0] == 'assessments' and path[1] in body.get('assessments', {}) and path[2] in (fields - {'expected_gain'})) or
                         (fields == {'reason', 'expected_gain'} and len(path) == 2 and path[0] == 'decision' and path[1] in fields))
                require(valid, 'Maintenance path is outside the approved current-text/list representation')
                key = tuple(path); require(key not in seen, 'Duplicate maintenance path'); seen.add(key)
                parent = body
                for part in path[:-1]:
                    require(isinstance(parent, dict) and part in parent, 'Unknown maintenance path')
                    parent = parent[part]
                require(path[-1] in parent, 'Unknown maintenance field')
                return parent, path[-1]
            for move in text_moves:
                require(isinstance(move, dict) and set(move) == {'path', 'before', 'segments'}, 'Text move needs path/before/segments')
                parent, key = target(move['path'], {'reason', 'expected_gain'})
                require(isinstance(parent[key], str) and parent[key] == move['before'], 'Text before-value changed', 'REVISION_CONFLICT')
                segments = move['segments']
                require(isinstance(segments, list) and segments and all(isinstance(s, dict) and len(s) == 1 and
                    next(iter(s)) in ('current', 'historical') and isinstance(next(iter(s.values())), str) for s in segments),
                    'Segments must explicitly label current or historical text')
                require(''.join(next(iter(s.values())) for s in segments) == move['before'], 'Segments must preserve the exact original text')
                current = ''.join(s.get('current', '') for s in segments)
                require(current.strip() and any(s.get('historical') for s in segments), 'Retain current text and explicitly identify historical text')
                parent[key] = current; changes.append(deepcopy(move))
            for path in deduplicate_paths:
                parent, key = target(path, {'limitations'})
                before = parent[key]; require(isinstance(before, list), 'Deduplication target must be a list')
                after = []
                for item in before:
                    if item not in after:
                        after.append(item)
                if before != after:
                    parent[key] = after
                    changes.append({'path': path, 'before': before, 'after': after, 'operation': 'exact_duplicates_only'})
            if body.get('decision'):
                body['decision']['assessment_refs'] = goals.assessment_references(body)
            before_body = {k: v for k, v in work.items() if k not in ('id', 'revision', 'updated_at')}
            changed = body != before_body
            result = {'work': work_id, 'expected_revision': expected, 'changed': changed,
                'before_bytes': len(encoded(before_body).encode()), 'after_bytes': len(encoded(body).encode()),
                'changes': changes, 'body': body, 'applied': False}
            if apply and changed:
                sequence = self.event(work_id, 'current_state_compacted', source,
                    {'before_revision': expected, 'actor': actor, 'before_decision': original_decision, 'changes': changes,
                     'coverage': 'Explicit historical text and exact duplicates only; request, requirements, research fields and task selection unchanged'})
                body['current_state_history_event'] = sequence
                self.db.execute('UPDATE works SET body=?,revision=revision+1,updated_at=? WHERE id=?', (encoded(body), now(), work_id))
                result.update(applied=True, revision=expected + 1, history_event=sequence,
                    after_bytes=len(encoded(body).encode()))
            return result

    def check_decision(self, task):
        work = goals.current_task(self, task)
        d = goals.decision_view(self, work)
        require(d.get('action') in ('continue', 'change', 'deliver'), 'A current overall continuation decision is required')
        require(d.get('request_revision') == work['request_revision'], 'Overall decision belongs to an old request')
        stamp = d.get('task_stamps', {}).get(task['id'])
        require(stamp, 'Task has not been selected by the overall decision')
        current_frontier = goals.task_frontier(self, work, task) if stamp.get('frontier_kind') == 'task-dependencies-v1' else goals.frontier(self, work, task['body']['contributes_to'])
        require(stamp['frontier'] == current_frontier, 'Affected result, judgment or criteria require reconciliation')
        require(stamp['task'] == goals.fingerprint({k:v for k,v in task['body'].items() if k != 'reviews'}), 'Task scope changed after decision')
        # Explicit supports of this selected decision are evidence dependencies;
        # incidental sibling reviews in the same coarse requirement are not.
        for rid in task['body']['contributes_to']:
            for support in work.get('assessments', {}).get(rid, {}).get('supports', []):
                other = self.task(support); goals.ready(self, other)
                proof = check_contract(self.root, other['body'].get('completion'), self, {support})
                require(other['status'] == 'adopted' and proof['passed'], 'Overall decision support evidence changed: ' + support)

    def check_work(self, work, task=None, operation='dispatch'):
        w = self.get(work)
        reasons = []
        if w["status"] != "active":
            reasons.append("Work is " + w["status"])
        if self.blocking_inputs(work, task=task, operation=operation):
            reasons.append("Affected user input has not been reconciled")
        return {"work": work, "revision": w["revision"], "request_revision": w["request_revision"], "allowed": not reasons, "reasons": reasons}

    def reserve(self, key, task_id, target, source, message=None, intent='new', relates_to=None, native_entry_id=None):
        with self.transaction():
            task = self.task(task_id)
            check = self.check_work(task["work_id"], task=task_id)
            require(check["allowed"], "; ".join(check["reasons"]))
            require(task["work_revision"] == check["request_revision"], "Task belongs to an old request revision")
            require(task["status"] == "proposed", "Task is not available for dispatch")
            require(task['body'].get('kind') == 'unit', 'Aggregate stages cannot be dispatched as execution units')
            if self.schema_version >= 3:
                from .native_entry import NativeEntry
                ordinary = NativeEntry(self)
                require(not any((ordinary.live(item) or item.get('conflict')) and item['id'] != native_entry_id and
                        (item['task_id'] == task_id or item.get('thread_id') == target) for item in ordinary.actions()),
                        'Ordinary native preparation already owns this task/thread; reconcile before another send', 'ACTION_UNKNOWN')
            goals.ready(self, task)
            goals.judgment_dependencies(self, task, strict=True)
            self.check_decision(task)
            from .coordination import check_admission
            check_admission(self, task, operation='dispatch', target=target)
            require(isinstance(message, dict) and isinstance(message.get('prompt'), str) and message['prompt'].strip(), 'Bind the exact dispatch message before reserve')
            require(set(message) <= {'threadId', 'hostId', 'prompt', 'model', 'thinking'}, 'Unknown dispatch message fields')
            require(message.get('threadId', target) == target, 'Message target differs from reservation target')
            message = {**message, 'threadId': target}
            policy = self.get(task['work_id']).get('execution_policy', {})
            for field in ('model', 'thinking'):
                require(not policy.get(field) or message.get(field) == policy[field], 'Dispatch violates required ' + field)
            require(not policy.get('target') or target == policy['target'], 'Dispatch violates required target')
            require(intent in ('new', 'amend', 'replace'), 'Unknown dispatch intent')
            active = self.active_dispatches(target)
            if intent == 'new':
                require(not active, 'Target has active/unknown execution; reconcile its receipt or use explicit amend/replace')
            else:
                require(len(active) == 1 and relates_to == active[0]['key'], 'Amend/replace must identify the one active dispatch')
                require(active[0]['status'] in ('sent', 'started'), 'Resolve unknown/prepared dispatch before amend/replace')
            binding = self.binding(target)
            require(binding and binding['role'] == 'executor', 'Bind the target executor before dispatch')
            require(binding["work_id"] == task["work_id"], "Target is bound to another work")
            require(not self.db.execute("SELECT 1 FROM dispatches WHERE key=?", (key,)).fetchone(), "Dispatch identity already used; inspect its receipt")
            self.db.execute("INSERT INTO dispatches VALUES(?,?,?,?,?)", (key, task_id, target, "prepared", "{}"))
            plan = {'message': message, 'message_sha256': goals.fingerprint(message), 'intent': intent, 'relates_to': relates_to}
            self.db.execute('INSERT INTO dispatch_plans VALUES(?,?)', (key, encoded(plan)))
            self.db.execute("UPDATE tasks SET status='reserved' WHERE id=?", (task_id,))
            self.event(task["work_id"], "dispatch_prepared", source, {"key": key, "task": task_id, "target": target})
        return self.dispatch(key)

    def active_dispatches(self, target, excluding=None):
        rows = self.db.execute("SELECT d.* FROM dispatches d JOIN tasks t ON d.task_id=t.id WHERE d.target=? AND d.status IN ('prepared','claimed','sent','started','unknown')", (target,)).fetchall()
        return [dict(r) for r in rows if r['key'] != excluding and self.task(r['task_id'])['body'].get('kind', 'unit') == 'unit'
                and (r['status'] in ('prepared', 'claimed', 'unknown') or self.task(r['task_id'])['status'] in ('reserved', 'dispatched', 'running'))]

    def dispatch_plan(self, key):
        r = self.db.execute('SELECT body FROM dispatch_plans WHERE key=?', (key,)).fetchone()
        require(r, 'Legacy dispatch has no bound payload; reconcile/reprepare without replaying an unknown send')
        return json.loads(r[0])

    def dispatch(self, key):
        row = self.db.execute("SELECT * FROM dispatches WHERE key=?", (key,)).fetchone()
        require(row is not None, "Unknown dispatch")
        p = self.db.execute('SELECT body FROM dispatch_plans WHERE key=?', (key,)).fetchone()
        plan = json.loads(p[0]) if p else {}
        guard = plan.get('guard_evaluation')
        return {**dict(row), "receipt": json.loads(row["receipt"]),
                **({'native_guard': plan['native_guard']} if plan.get('native_guard') else {}),
                'guard_evaluation': guard or {'status': 'unverified', 'basis': 'No actual guard evaluation receipt recorded; send state is separate'}}

    def settle(self, key, status, receipt, source):
        transitions = {"prepared": {"sent", "unknown", "cancelled"}, "claimed": {"sent", "unknown", "cancelled"}, "sent": {"started", "returned", "unknown"},
                       "started": {"returned", "unknown"}, "unknown": {"sent", "started", "returned", "cancelled"}}
        require(bool(receipt), "An observed receipt or recovery evidence is required")
        with self.transaction():
            d = self.dispatch(key)
            require(status in transitions.get(d["status"], set()), "Invalid dispatch transition")
            self.db.execute("UPDATE dispatches SET status=?,receipt=? WHERE key=?", (status, encoded(receipt), key))
            task = self.task(d["task_id"])
            task_status = {"sent": "dispatched", "started": "running", "returned": "returned", "unknown": "reserved", "cancelled": "abandoned"}[status]
            self.db.execute("UPDATE tasks SET status=? WHERE id=?", (task_status, d["task_id"]))
            self.event(task["work_id"], "dispatch_" + status, source, {"key": key, "receipt": receipt})
            if status == 'sent':
                p = self.db.execute('SELECT body FROM dispatch_plans WHERE key=?', (key,)).fetchone()
                plan = json.loads(p[0]) if p else {}
                if plan.get('intent') in ('amend', 'replace'):
                    previous = self.dispatch(plan['relates_to'])
                    require(previous['status'] in ('sent', 'started'), 'Related dispatch changed; reconcile the observed send')
                    self.db.execute("UPDATE dispatches SET status='superseded' WHERE key=?", (previous['key'],))
                    self.db.execute("UPDATE tasks SET status='superseded' WHERE id=?", (previous['task_id'],))
                    self.event(task['work_id'], 'dispatch_superseded', source,
                        {'previous': previous['key'], 'current': key, 'intent': plan['intent'], 'meaning': 'Explicit scope handoff, not result completion'})
        return self.dispatch(key)

    def observation(self, asset, kind, body, source):
        require(kind in ("reading", "access", "adoption", "note", "sync"), "Invalid observation kind")
        with self.transaction():
            require(isinstance(body, dict), 'Observation body must be an object')
            body = dict(body)
            if kind == 'note' and body.get('improvement_trigger') == 'task_finding':
                from .improvements import check_origin, record_findings
                from .native_entry import NativeEntry
                require(body.get('native_entry') and body.get('return_event') and body.get('writer_session'),
                        'Task finding supplement needs native_entry, exact return_event and actual writer_session')
                entry = NativeEntry(self).get(body['native_entry'])
                check_origin(entry, body)
                findings = record_findings(self, entry, body.get('findings'), source, body['writer_session'], asset=asset)
                return {'findings': findings, 'duplicate': all(item['duplicate'] for item in findings),
                    'requested_asset': asset,
                    'asset_role': 'Topic on new observations; finding identity controls deduplication and duplicate observations keep their recorded topic'}
            if kind in ('reading', 'adoption', 'access'):
                if body.get('path'):
                    path = local_path(self.root, body['path'])
                    if kind == 'access' and not path.is_file():
                        body['content_identity'] = {'path':str(path.relative_to(self.root)), 'sha256':None, 'presence':'unavailable'}
                    else:
                        require(path.is_file(), 'Observed file must exist at capture time')
                        sha = digest(path)
                        require(not body.get('sha256') or body['sha256'] == sha, 'Observed content hash differs')
                        body['content_identity'] = {'path': str(path.relative_to(self.root)), 'sha256': sha}
                elif asset.startswith('sha256:'):
                    body['content_identity'] = {'sha256': asset[7:], 'basis': 'Declared exact content identity'}
                if kind == 'adoption' and body.get('replacement'):
                    ref = body['replacement']
                    ref = {'path': ref} if isinstance(ref, str) else dict(ref)
                    body['replacement_identity'] = self._file_evidence([ref], 'implementation')[0]
            if kind == 'note' and body.get('improvement_trigger'):
                require(body['improvement_trigger'] in ('user_reported_recurrence','recovery_route_changed','deployed_fix_failure'), 'Unknown improvement trigger')
                require(body.get('work') and body.get('scope') and body.get('evidence'), 'Improvement candidate needs work, scope and actual evidence pointer')
                self.get(body['work'])
                self.event(body['work'], 'improvement_candidate', source, {'asset':asset, **body,
                    'meaning':'Explicitly classified observation for triage; not a new approved rule'})
            self._validate_sync(asset, kind, body)
            row = self.db.execute("INSERT INTO observations(asset,kind,body,source,at) VALUES(?,?,?,?,?)", (asset, kind, encoded(body), source, now()))
            require(bool(source), "Observation requires source")
        return row.lastrowid

    def _file_evidence(self, items, default_layer):
        require(isinstance(items, list) and bool(items), 'Evidence must be a nonempty file-reference list')
        result = []
        for supplied in items:
            ref = {'path': supplied} if isinstance(supplied, str) else dict(supplied)
            require(isinstance(ref.get('path'), str), 'Evidence needs a workspace file path')
            path = local_path(self.root, ref['path'])
            require(path.is_file(), 'Evidence file does not exist: ' + ref['path'])
            sha = digest(path)
            require(not ref.get('sha256') or ref['sha256'] == sha, 'Evidence content changed: ' + ref['path'])
            ref.update(path=str(path.relative_to(self.root)), sha256=sha, layer=ref.get('layer', default_layer))
            require(ref['layer'] in ('implementation','test','runtime','effect'), 'Unknown evidence layer')
            if ref.get('valid_until'):
                try:
                    valid = isinstance(ref['valid_until'], str) and datetime.fromisoformat(ref['valid_until']) > datetime.fromisoformat(now())
                except (TypeError,ValueError):
                    valid = False
                require(valid, 'Evidence validity expired or is not an aware ISO timestamp')
            result.append(ref)
        return result

    def _validate_sync(self, asset, kind, body):
        if kind != 'sync':
            return
        statuses = ('pending','proposed','approved','implemented','verified','effect_failed','recurrence','needs_review','rejected','superseded')
        require(body.get('status') in statuses, 'Invalid sync status')
        for field in ('work', 'proposal', 'locations', 'reason', 'impact'):
            require(body.get(field), 'Sync item missing ' + field)
        require(isinstance(body['locations'], list) and all(isinstance(x, str) and x.strip() for x in body['locations']), 'Sync locations must be a list of paths')
        self.get(body['work'])
        previous = next((x['body'] for x in reversed(self.observations(asset)) if x['kind'] == 'sync'), None)
        if previous:
            transitions = {'pending': {'proposed','rejected','superseded'}, 'proposed': {'approved','rejected','superseded'},
                'approved': {'implemented','superseded'}, 'implemented': {'verified','effect_failed','recurrence','needs_review','superseded'},
                'verified': {'effect_failed','recurrence','needs_review','superseded'},
                'effect_failed': {'implemented','verified','needs_review','recurrence','superseded'},
                'recurrence': {'implemented','verified','needs_review','superseded'},
                'needs_review': {'implemented','verified','effect_failed','recurrence','superseded'},
                'rejected': set(), 'superseded': set()}
            require(body['status'] in transitions[previous['status']], 'Invalid sync transition; changed proposal needs its own item')
            for field in ('proposal','locations','work','reason','impact'):
                require(body[field] == previous[field], 'Changed sync scope requires a new proposal')
            if previous.get('approval_source'):
                require(body.get('approval_source') == previous['approval_source'], 'Preserve the actual approval source')
        else:
            require(body['status'] in ('pending','proposed'), 'Record the proposal before approval')
        if body['status'] not in ('pending','proposed','rejected','superseded'):
            require(body.get('approval_source'), 'Sync writeback needs explicit user approval provenance')
        if body['status'] in ('implemented','verified','effect_failed','recurrence','needs_review'):
            body['evidence'] = self._file_evidence(body.get('evidence'), 'implementation')
            body['evidence_bound_at'] = now()
            body['effect_status'] = 'pending_actual_effect_review'
        if body['status'] == 'verified':
            require(any(e['layer'] == 'effect' for e in body['evidence']), 'Verified effect needs effect-layer evidence; tests alone are insufficient')
            effect = body.get('effect_review', {})
            for field in ('reviewer','scope','observation','coverage','limitations'):
                require(effect.get(field) or (field == 'limitations' and effect.get(field) == []), 'Effect review missing ' + field)
            body['effect_status'] = 'reviewed_for_declared_scope'
        if body['status'] in ('effect_failed','recurrence','needs_review'):
            require(body.get('failure') and body.get('next_action'), 'Effect failure/recurrence needs actual observation and next action')
            body['effect_status'] = body['status']
            self.event(body['work'], 'improvement_candidate', 'sync effect observation', {'asset': asset,
                'kind': body['status'], 'failure': body['failure'], 'next_action': body['next_action'],
                'meaning': 'Triage candidate; no Wiki write or new authorization inferred'})

    def triage(self, work, event_id, body, source):
        """Append an evidence-bound response without erasing the original failure."""
        require(isinstance(body, dict), 'Triage response must be an object')
        require(body.get('disposition') in ('addressed', 'monitoring', 'deferred', 'no_change'), 'Invalid triage disposition')
        if 'severity' in body:
            from .improvements import SEVERITIES
            require(body['severity'] in SEVERITIES, 'Unknown explicit finding severity')
        for field in ('reason', 'next_action'):
            require(isinstance(body.get(field), str) and body[field].strip(), 'Triage needs ' + field)
        with self.transaction():
            self.get(work)
            event = self.db.execute("SELECT * FROM events WHERE seq=? AND work_id=? AND kind='improvement_candidate'", (event_id, work)).fetchone()
            require(event is not None, 'Candidate event is not in this work')
            value = {**body, 'candidate_seq': event_id, 'evidence': self._file_evidence(body.get('evidence', []), 'effect')}
            from .improvements import latest_finding
            current_event = latest_finding(self, event)['seq']
            supplemented = json.loads(event['payload']).get('kind') == 'task_finding' and current_event != event_id
            require((not supplemented and 'finding_event' not in body) or body.get('finding_event') == current_event,
                    'Finding changed before triage; run context --work ' + work +
                    ' and pass its latest_event as finding_event for the supplemented finding', 'REVISION_CONFLICT')
            value['finding_event'] = current_event
            self.event(work, 'improvement_triaged', source, value)
        return value

    def observations(self, asset):
        return [{**dict(r), "body": json.loads(r["body"])} for r in self.db.execute("SELECT * FROM observations WHERE asset=? ORDER BY id", (asset,))]

    def judgment(self, identifier):
        r = self.db.execute("SELECT * FROM judgments WHERE id=?", (identifier,)).fetchone()
        require(r is not None, "Unknown judgment: " + identifier)
        return {**dict(r), "body": json.loads(r["body"])}

    def judge(self, identifier, work, expected, status, body, source):
        require(status in ("proposed", "adopted", "needs_review", "retracted"), "Invalid judgment status")
        require(isinstance(body, dict) and body.get("claim") and body.get("scope") and body.get("evidence"), "Judgment needs claim, scope and evidence")
        require(isinstance(body["evidence"], list) and all(isinstance(x, dict) for x in body["evidence"]), "Evidence must be a list of references")
        body = json.loads(encoded(body))
        if body.get('kind') == 'blocker' or body.get('only_external_wait'):
            for key in ('coverage', 'affected', 'independent_actions', 'checked_recovery_routes', 'next_discriminator'):
                require(key in body and (body[key] or key == 'independent_actions'), 'Blocking judgment missing ' + key)
            require(isinstance(body['affected'], list) and isinstance(body['independent_actions'], list) and
                    isinstance(body['checked_recovery_routes'], list), 'Blocker scope/recovery routes must be explicit lists')
        if status == 'adopted':
            from .common import local_path, digest
            for ref in body['evidence']:
                if 'path' in ref:
                    p = local_path(self.root, ref['path'])
                    require(p.is_file(), 'Judgment evidence missing')
                    actual = digest(p)
                    require(not ref.get('sha256') or ref['sha256'] == actual, 'Judgment evidence changed before adoption')
                    ref['sha256'] = actual
        with self.transaction():
            self.get(work)
            from .coordination import validate_trigger
            validate_trigger(self, work, body)
            r = self.db.execute("SELECT * FROM judgments WHERE id=?", (identifier,)).fetchone()
            require((r["revision"] if r else 0) == expected, "Stale judgment revision")
            require(not r or r["work_id"] == work, "Judgment belongs to another work")
            self.db.execute("INSERT INTO judgments VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,status=excluded.status,body=excluded.body", (identifier, work, expected + 1, status, encoded(body)))
            self.event(work, "judgment_" + status, source, {"id": identifier, "revision": expected + 1,
                "previous_revision": expected, "previous_status": r['status'] if r else None, "body": body})
            if r and r['status'] == 'adopted' and (status != 'adopted' or json.loads(r['body']).get('claim') != body.get('claim')):
                self.event(work, 'improvement_candidate', source, {'kind': 'important_judgment_changed', 'judgment': identifier,
                    'previous_revision': expected, 'revision': expected+1, 'scope': body['scope'],
                    'meaning': 'Candidate for triage; no new lesson or Wiki write inferred'})
        return self.judgment(identifier)

    def history(self, work):
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.db.execute("SELECT * FROM events WHERE work_id=? ORDER BY seq", (work,))]

    def backup(self, path):
        from .migrations import checked_backup
        return checked_backup(self.db, path)
