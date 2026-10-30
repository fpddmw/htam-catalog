"""Durable native facts and explicit controls; semantic/scientific adoption stays in State."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math

from .common import HarnessError, digest, encoded, local_path, now, require
from . import goals

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS runtime_coordinators(work_id TEXT PRIMARY KEY REFERENCES works(id),
       owner TEXT NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS runtime_policies(id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
       body TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS runtime_routes(task_id TEXT PRIMARY KEY REFERENCES tasks(id),
       revision INTEGER NOT NULL, host_id TEXT NOT NULL, thread_id TEXT, runtime_id TEXT NOT NULL,
       body TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS runtime_actions(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
       dispatch_key TEXT UNIQUE REFERENCES dispatches(key), revision INTEGER NOT NULL,
       host_id TEXT NOT NULL, thread_id TEXT, runtime_id TEXT NOT NULL, turn_id TEXT,
       method TEXT NOT NULL, request_sha256 TEXT NOT NULL, body TEXT NOT NULL, observed_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS runtime_inbox(event_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
       kind TEXT NOT NULL, host_id TEXT NOT NULL, runtime_id TEXT NOT NULL, thread_id TEXT, turn_id TEXT,
       action_id TEXT REFERENCES runtime_actions(id), status TEXT NOT NULL,
       body TEXT NOT NULL, observed_at TEXT NOT NULL, received_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS runtime_controls(id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
       revision INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
       body TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(id,task_id))''',
    '''CREATE TABLE IF NOT EXISTS runtime_review_queue(task_id TEXT PRIMARY KEY REFERENCES tasks(id),
       action_id TEXT REFERENCES runtime_actions(id), owner TEXT NOT NULL, revision INTEGER NOT NULL,
       status TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS runtime_resource_claims(resource TEXT PRIMARY KEY,
       action_id TEXT NOT NULL REFERENCES runtime_actions(id), status TEXT NOT NULL,
       body TEXT NOT NULL, at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS runtime_usage(id TEXT PRIMARY KEY, action_id TEXT REFERENCES runtime_actions(id),
       unit TEXT NOT NULL, amount REAL, body TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS runtime_inbox_target ON runtime_inbox(host_id,thread_id,turn_id,status)',
)
TABLES = {'runtime_coordinators', 'runtime_policies', 'runtime_routes', 'runtime_actions',
          'runtime_inbox', 'runtime_controls', 'runtime_review_queue', 'runtime_resource_claims', 'runtime_usage'}
TERMINAL = {'completed', 'interrupted', 'failed'}
LIVE = {'guarded', 'attempted', 'acknowledged', 'unknown'}


def native_hash(value):
    """JSON-native-v1: compact sorted JSON; no invented numeric normalization."""
    def check(item):
        if isinstance(item, float):
            require(math.isfinite(item) and item.is_integer(), 'Native canonical parameters require integral numbers')
            return int(item)
        if isinstance(item, dict):
            return {key: check(val) for key, val in item.items()}
        if isinstance(item, list):
            return [check(val) for val in item]
        return item
    return hashlib.sha256(json.dumps(check(value), ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def latest_observed(first, second):
    return second if not first or datetime.fromisoformat(second.replace('Z', '+00:00')) > datetime.fromisoformat(first.replace('Z', '+00:00')) else first


class Runtime:
    def __init__(self, state):
        self.state, self.db = state, state.db

    def _write(self):
        require(self.state.schema_version >= 3, 'Runtime writes need explicit migration to schema 3', 'MIGRATION_REQUIRED')

    def _get(self, table, key, value, optional=False):
        row = self.db.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (value,)).fetchone()
        require(optional or row is not None, 'Missing ' + table + ': ' + str(value), 'RUNTIME_NOT_CONFIGURED')
        if row is None:
            return None
        result = dict(row)
        if 'body' in result:
            result['body'] = json.loads(result['body'])
        return result

    def action(self, identifier):
        return self._get('runtime_actions', 'id', identifier)

    def route(self, task, optional=False):
        return self._get('runtime_routes', 'task_id', task, optional)

    def coordinator(self, work):
        return self._get('runtime_coordinators', 'work_id', work)

    def owner(self, work, owner, expected, source, handoff=None):
        self._write()
        require(isinstance(owner, str) and owner.strip(), 'Coordinator identity required')
        with self.state.transaction():
            current_work = self.state.get(work)
            old = self._get('runtime_coordinators', 'work_id', work, True)
            require((old['revision'] if old else 0) == expected, 'Stale coordinator revision', 'REVISION_CONFLICT')
            from .coordination import coordinator_write_scope
            protected = coordinator_write_scope(self.state, work)
            if protected or handoff is not None:
                require(isinstance(handoff, dict) and handoff.get('enforce_writes') is True,
                        'Protected coordinator handoff cannot silently disable overall-write checks', 'COORDINATOR_REQUIRED')
                require(handoff.get('expected_work_revision') == current_work['revision'], 'Stale work at handoff', 'REVISION_CONFLICT')
                if handoff.get('mode') == 'user_reassignment':
                    # The human can reclaim coordination even when the former
                    # coordinator is unavailable. This never asserts its native
                    # work stopped or its unknown sends were reconciled.
                    from .coordination import _verify_authorization
                    authorization = handoff.get('authorization')
                    _verify_authorization(self.state, work, authorization)
                    statement = self.owner_statement(work, owner, expected)
                    require(authorization['mutation_sha256'] == goals.fingerprint(statement),
                            'Human reassignment is bound to another work/owner/revision', 'AUTHORIZATION_MUTATION_MISMATCH')
                    require(handoff.get('reason') and handoff.get('impact') and handoff.get('execution_effect') == 'coordination_only',
                            'Describe coordination-only reassignment and its real external limits', 'HANDOFF_REQUIRED')
                    handoff = {**handoff, 'statement': statement,
                        'unresolved_dispatches': [dict(row) for row in self.db.execute(
                            "SELECT d.key,d.target,d.status FROM dispatches d JOIN tasks t ON t.id=d.task_id WHERE t.work_id=? AND d.status IN ('prepared','claimed','sent','started','unknown')", (work,))],
                        'native_effect': 'Same-store coordinator writes/actions change owner; external execution and unknown effects remain as observed'}
                    transferred = []
                    if old and old['owner'] != owner:
                        # Move the former coordinator's unconsumed reception
                        # responsibility only. Other professional reviewers keep
                        # their exact assignment and every producer stays intact.
                        rows = self.db.execute("SELECT q.* FROM runtime_review_queue q JOIN tasks t ON t.id=q.task_id WHERE t.work_id=? AND q.owner=? AND q.status!='resolved'", (work, old['owner'])).fetchall()
                        for row in rows:
                            queue = {**dict(row), 'body': json.loads(row['body'])}
                            transfer = self.state.event(work, 'return_review_reassigned_by_user', source,
                                {'task': queue['task_id'], 'before_owner': old['owner'], 'owner': owner,
                                 'before_status': queue['status'], 'before_revision': queue['revision'],
                                 'input_id': authorization['input_id'], 'mutation_sha256': authorization['mutation_sha256'],
                                 'coverage': 'Reception responsibility transferred; no native stop or scientific review inferred'})
                            status = 'pending' if queue['status'] == 'claimed' else queue['status']
                            self.db.execute('UPDATE runtime_review_queue SET owner=?,status=?,revision=revision+1,body=?,at=? WHERE task_id=? AND revision=?',
                                (owner, status, encoded({**queue['body'], 'owner_transfer_event': transfer}), now(), queue['task_id'], queue['revision']))
                            native_id = queue['body'].get('native_entry_id')
                            if native_id:
                                from .native_entry import NativeEntry
                                entries = NativeEntry(self.state)
                                item = entries.get(native_id)
                                if item['review_owner'] == old['owner']:
                                    entries._save({**item, 'review_owner': owner, 'owner_transfer_event': transfer}, source)
                            transferred.append({'task': queue['task_id'], 'event_seq': transfer})
                    handoff['transferred_return_reviews'] = transferred
                else:
                    receipt = handoff.get('receipt', {})
                    require(receipt.get('from_owner') and (not old or receipt['from_owner'] == old['owner'])
                            and receipt.get('reference') and receipt.get('provider') and receipt.get('safe_boundary') is True,
                            'Explicit prior-owner acknowledgement or verified human reassignment required', 'HANDOFF_REQUIRED')
            self.db.execute('INSERT INTO runtime_coordinators VALUES(?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET owner=excluded.owner,revision=excluded.revision,source=excluded.source,at=excluded.at',
                            (work, owner, expected + 1, source, now()))
            self.state.event(work, 'runtime_coordinator', source, {'before': old, 'owner': owner, 'revision': expected + 1})
            if protected or handoff is not None:
                self.state.event(work, 'coordinator_write_scope', source, {'owner': owner, 'revision': expected + 1,
                    'handoff': handoff, 'coverage': 'Explicit handoff declaration; source identity is not independently authenticated'})
                body = {k: v for k, v in current_work.items() if k not in ('id', 'revision', 'updated_at')}
                if body.get('decision'):
                    body['decision'] = {**body['decision'], 'validity': 'needs_reconciliation',
                        'invalidated_by': ['coordinator_handoff'], 'facts_changed_at': now()}
                self.db.execute('UPDATE works SET body=?,revision=revision+1,updated_at=? WHERE id=?', (encoded(body), now(), work))
        return self.coordinator(work)

    def owner_statement(self, work, owner, expected):
        """Exact read-only binding for a human-directed coordination reassignment."""
        current_work = self.state.get(work)
        old = self._get('runtime_coordinators', 'work_id', work, True)
        require((old['revision'] if old else 0) == expected, 'Stale coordinator revision', 'REVISION_CONFLICT')
        return {'operation': 'coordinator_reassignment', 'work_id': work, 'work_revision': current_work['revision'],
            'from_owner': old['owner'] if old else None, 'owner_revision': expected, 'to_owner': owner,
            'execution_effect': 'coordination_only'}

    def policy(self, identifier, expected, body, source):
        self._write()
        require(isinstance(body, dict) and type(body.get('execution_limit')) is int and 1 <= body['execution_limit'] <= 30,
                'Execution limit must be 1..30')
        require(type(body.get('review_limit')) is int and 0 <= body['review_limit'] <= 30
                and type(body.get('review_backlog_limit')) is int and 1 <= body['review_backlog_limit'] <= 1000,
                'Explicit review capacity/backlog bounds required')
        budget = body.get('budget')
        require(isinstance(budget, dict) and isinstance(budget.get('unit'), str) and budget['unit']
                and isinstance(budget.get('limit'), (int, float)) and math.isfinite(budget['limit']) and budget['limit'] >= 0,
                'Finite budget limit/unit required; zero explicitly stops new actions', 'BUDGET_UNCONFIGURED')
        require(budget.get('unknown_policy') in ('stop', 'reserve'), 'Declare unknown-cost policy')
        if budget['unknown_policy'] == 'reserve':
            require(isinstance(budget.get('unknown_reserve'), (int, float)) and math.isfinite(budget['unknown_reserve'])
                    and budget['unknown_reserve'] > 0, 'Positive unknown-cost budget reserve required')
        with self.state.transaction():
            old = self._get('runtime_policies', 'id', identifier, True)
            require((old['revision'] if old else 0) == expected, 'Stale runtime policy revision', 'REVISION_CONFLICT')
            self.db.execute('INSERT INTO runtime_policies VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body,source=excluded.source,at=excluded.at',
                            (identifier, expected + 1, encoded(body), source, now()))
            self.state.event(None, 'runtime_policy', source, {'id': identifier, 'revision': expected + 1, 'body': body})
        return self._get('runtime_policies', 'id', identifier)

    def map(self, task_id, expected, body, source):
        self._write()
        require(isinstance(body, dict) and all(isinstance(body.get(key), str) and body[key] for key in ('host_id', 'runtime_id', 'review_owner')),
                'Exact host/runtime and return-review owner required')
        require(body.get('thread_id') is None or isinstance(body['thread_id'], str) and body['thread_id'], 'Thread identity must be exact or unknown')
        with self.state.transaction():
            task = self.state.task(task_id)
            old = self.route(task_id, True)
            require((old['revision'] if old else 0) == expected, 'Stale runtime route revision', 'REVISION_CONFLICT')
            from .native_inputs import NativeInputs
            native = NativeInputs(self.state)
            require(not body.get('thread_id') or not native.quarantine_receipt(body['host_id'], body['thread_id']),
                    'A retired thread cannot become a new execution route', 'NATIVE_THREAD_QUARANTINED')
            recovery = None
            if old:
                retired = native.quarantine_receipt(old['host_id'], old['thread_id']) if old['thread_id'] else None
                if retired:
                    runtime_context = native.handoff_context(task)
                    require((body['host_id'], body.get('thread_id')) != (old['host_id'], old['thread_id'])
                            and native.view(old['host_id'], old['thread_id'], limit=1)['epoch'] == retired['epoch']
                            and retired['event_seq'] == body.get('quarantine_event')
                            and body.get('handoff_context_sha256') == retired['context_sha256']
                            and retired['handoff_contexts'].get(task_id) == runtime_context,
                            'New route must consume the exact retired-thread/current-work handoff context', 'REVISION_CONFLICT')
                    self._check_owner(task, body.get('coordinator'), body.get('owner_revision'))
                    recovery = {'quarantine_event': retired['event_seq'], 'context_sha256': goals.fingerprint(runtime_context),
                        'source_host': old['host_id'], 'source_thread': old['thread_id'], 'handoff_note': retired['review']['handoff_note']}
                else:
                    native.assert_ready(old, task)
                live = [a for a in self._actions(task_id) if a['body']['phase'] in LIVE]
                require(not live, 'Resolve existing live/unknown action before changing its route', 'ACTION_UNKNOWN')
            route_body = {key: value for key, value in body.items() if key not in ('host_id', 'thread_id', 'runtime_id')}
            for protected in ('paused', 'scope_revision', 'control_plan', 'control_id', 'native_input_quarantine', 'recovery_context'):
                require(protected not in body, 'Route mapping cannot rewrite scope control: ' + protected)
            if old:
                route_body.update({key: value for key, value in old['body'].items() if key in ('paused', 'scope_revision', 'control_plan', 'control_id')})
            else:
                latest = self.db.execute('SELECT id,kind,body FROM runtime_controls WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
                route_body.update(paused=latest['kind'] != 'resume' if latest else False, scope_revision=1)
                if latest:
                    route_body.update(control_plan=json.loads(latest['body'])['plan'], control_id=latest['id'])
            if recovery:
                route_body['recovery_context'] = recovery
            self.db.execute('INSERT INTO runtime_routes VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,host_id=excluded.host_id,thread_id=excluded.thread_id,runtime_id=excluded.runtime_id,body=excluded.body,source=excluded.source,at=excluded.at',
                (task_id, expected + 1, body['host_id'], body.get('thread_id'), body['runtime_id'], encoded(route_body), source, now()))
            self.state.event(task['work_id'], 'runtime_route', source, {'task': task_id, 'before': old, 'route': body, 'revision': expected + 1})
        return self.route(task_id)

    def _actions(self, task=None):
        rows = self.db.execute('SELECT id FROM runtime_actions' + (' WHERE task_id=?' if task else '') + ' ORDER BY rowid', (task,) if task else ())
        return [self.action(row[0]) for row in rows]

    def _save(self, action, body=None):
        self.db.execute('UPDATE runtime_actions SET thread_id=?,turn_id=?,body=?,observed_at=? WHERE id=?',
            (action['thread_id'], action['turn_id'], encoded(body or action['body']), action['observed_at'], action['id']))

    def _check_owner(self, task, owner, revision):
        current = self.coordinator(task['work_id'])
        require(current['owner'] == owner and current['revision'] == revision, 'Coordinator changed; explicit CAS handoff required', 'REVISION_CONFLICT')

    def _scope(self, task_id, control=False):
        route = self.route(task_id)
        require(control or not route['body'].get('paused'), 'This task scope is locally paused', 'SCOPE_PAUSED')
        snapshot = route['body'].get('control_plan')
        if snapshot:
            require(self.scope_plan(snapshot['roots'], snapshot['include_dependents'])['graph_sha256'] == snapshot['graph_sha256'],
                    'Real dependencies changed after scope control; review a fresh scope plan', 'SCOPE_GRAPH_CHANGED')
        if control:
            return route
        for dependency in self.state.task(task_id)['body'].get('requires', []):
            other = self.route(dependency, True)
            require(not other or not other['body'].get('paused'), 'A real prerequisite is paused', 'DEPENDENCY_PAUSED')
        return route

    def prepare(self, identifier, task_id, expected, owner, owner_expected, body, source):
        self._write()
        require(isinstance(body, dict), 'Native intent body must be an object')
        require(body.get('method') in ('thread/start', 'thread/resume', 'turn/start', 'turn/interrupt', 'turn/steer'), 'Unsupported native method')
        params = body.get('params')
        require(isinstance(params, dict), 'Bind exact native parameters')
        with self.state.transaction():
            task = self.state.task(task_id)
            self._check_owner(task, owner, owner_expected)
            route = self._scope(task_id, body['method'] in ('turn/interrupt', 'turn/steer'))
            require(route['revision'] == expected, 'Stale runtime route revision', 'REVISION_CONFLICT')
            require(not self._get('runtime_actions', 'id', identifier, True), 'Native intent identity already used; inspect its facts', 'INTENT_ALREADY_USED')
            method = body['method']
            require(method == 'thread/start' and route['thread_id'] is None or params.get('threadId') == route['thread_id'], 'Native request thread differs from task route', 'IDENTITY_MISMATCH')
            require(method == 'turn/interrupt' or self.state.check_work(task['work_id'], task=task_id)['allowed'], 'Reconcile affected work status/new input before preparing an action')
            if method not in ('turn/interrupt', 'turn/steer'):
                require(not any(item['body']['phase'] in LIVE for item in self._actions(task_id)),
                        'An existing action is live/unknown; reconcile first', 'ACTION_UNKNOWN')
            if method in ('thread/start', 'thread/resume', 'turn/start'):
                goals.ready(self.state, task); self.state.check_decision(task)
            from .native_inputs import NativeInputs
            native_input_receipt = NativeInputs(self.state).assert_ready(route, task) if method != 'turn/interrupt' else None
            bootstrap = body.get('bootstrap_proof')
            if bootstrap is not None:
                require(method == 'turn/start', 'First-turn bootstrap is only for turn/start', 'BOOTSTRAP_UNAVAILABLE')
                self._bootstrap_proof(route, task, bootstrap)
            self._check_recovery_context(route, task, body, method)
            dispatch_key = None
            if method == 'turn/start':
                message = body.get('message')
                require(isinstance(message, dict) and message.get('threadId') == route['thread_id'], 'Bind the original exact dispatch message')
                text = params.get('input')
                require(isinstance(text, list) and text and all(isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str) for item in text), 'Native text input required')
                require('\n'.join(item['text'] for item in text) == message.get('prompt'), 'Native input differs from prepared dispatch prompt', 'IDENTITY_MISMATCH')
                require(not message.get('model') or message['model'] == params.get('model'), 'Native model differs from dispatch')
                require(not message.get('thinking') or message['thinking'] == params.get('effort'), 'Native reasoning differs from dispatch')
                require(isinstance(params.get('clientUserMessageId'), str) and params['clientUserMessageId'], 'Exact client correlation ID required')
                if task['status'] == 'revise':
                    require(body.get('repair_review_sha256') == goals.fingerprint(task['body']['reviews'][-1]), 'Bind the current repair review before another execution')
                    self.db.execute("UPDATE tasks SET status='proposed' WHERE id=?", (task_id,))
                dispatch_key = body.get('dispatch_key', identifier)
                self.state.reserve(dispatch_key, task_id, route['thread_id'], source, message, body.get('intent', 'new'), body.get('relates_to'))
            if method == 'thread/start':
                require(body.get('session_choice_reason'), 'New native session needs its concrete purpose/reuse reason')
            if method in ('turn/interrupt', 'turn/steer'):
                require(body.get('control_id'), 'Native control must reference an explicit scope-control request')
                self._control_target(body['control_id'], task_id, route, params.get('turnId', params.get('expectedTurnId')))
            data = {**deepcopy(body), 'owner': owner, 'owner_revision': owner_expected, 'route_revision': expected,
                    'scope_revision': route['body']['scope_revision'], 'request_revision': self.state.get(task['work_id'])['request_revision'],
                    'phase': 'prepared', 'policy_id': body.get('policy_id', 'workspace'), 'resources': body.get('resources', []),
                    'native_input_receipt': native_input_receipt}
            if bootstrap is not None:
                data['bootstrap_prepared_inbox_rowid'] = self.db.execute('SELECT COALESCE(MAX(rowid),0) FROM runtime_inbox').fetchone()[0]
            if method == 'turn/start':
                data['binding_snapshot'] = self.state.binding(route['thread_id'])
            require(isinstance(data['resources'], list) and all(isinstance(item, str) and item for item in data['resources']), 'Explicit resource IDs required')
            self.db.execute('INSERT INTO runtime_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, task_id, dispatch_key, 1, route['host_id'], route['thread_id'], route['runtime_id'],
                 params.get('turnId', params.get('expectedTurnId')), method, native_hash(params), encoded(data), None))
            self.state.event(task['work_id'], 'native_intent_prepared', source, {'id': identifier, 'dispatch_key': dispatch_key,
                'task': task_id, 'method': method, 'request_sha256': native_hash(params)})
        return self.intent(identifier)

    def intent(self, identifier):
        action = self.action(identifier)
        task = self.state.task(action['task_id'])
        return {'intentId': identifier, 'revision': action['revision'], 'dispatchKey': action['dispatch_key'] or identifier,
                'workId': task['work_id'], 'coordinator': action['body']['owner'],
                'ownerRevision': action['body']['owner_revision'], 'requestSha256': action['request_sha256']}

    def budget(self, policy_id):
        policy = self._get('runtime_policies', 'id', policy_id)['body']
        budget = policy['budget']; unit = budget['unit']
        known, charged, unknown, measures = 0.0, 0.0, 0, 0
        for action in self._actions():
            if action['method'] != 'turn/start' or action['body'].get('policy_id') != policy_id or not action['body'].get('budget_claimed'):
                continue
            rows = [dict(row) for row in self.db.execute('SELECT * FROM runtime_usage WHERE action_id=? AND unit=?', (action['id'], unit))]
            exact = [row for row in rows if row['amount'] is not None]
            counters, amount = {}, 0.0
            for row in exact:
                data = json.loads(row['body'])
                if data.get('measurement') == 'cumulative':
                    counter = data['counter_id']; counters[counter] = max(counters.get(counter, 0), row['amount'])
                else:
                    amount += row['amount']
            amount += sum(counters.values()); known += amount; measures += len(exact)
            complete = any(json.loads(row['body']).get('complete') for row in exact)
            if not complete:
                unknown += 1
            charged += amount if complete else max(amount, budget.get('unknown_reserve', 0))
        from .native_entry import NativeEntry
        ordinary = [item for item in NativeEntry(self.state).actions() if item.get('budget_claimed') and item['policy_id'] == policy_id]
        unknown += len(ordinary)
        charged += len(ordinary) * budget.get('unknown_reserve', 0)
        return {'unit': unit, 'known_reported': known, 'measurements': measures, 'unknown_actions': unknown,
                'ordinary_reserved_actions': len(ordinary),
                'budget_committed': charged, 'limit': budget['limit'],
                'coverage': 'Known scoped measurements plus explicit unknown reserves; reserves are not measured cost'}

    def _capacity(self, action):
        policy = self._get('runtime_policies', 'id', action['body']['policy_id'])['body']
        require(not policy.get('stop_new') and not policy.get('account_exhausted'), 'New actions stopped by policy/account limit', 'BUDGET_STOP')
        executions = [item for item in self._actions() if item['method'] == 'turn/start' and item['body']['phase'] in LIVE
                      and item['body'].get('policy_id') == action['body']['policy_id']]
        from .native_entry import NativeEntry
        ordinary = NativeEntry(self.state)
        active = [item for item in ordinary.actions() if ordinary.live(item) and item['policy_id'] == action['body']['policy_id']]
        require(len(executions) + len(active) < policy['execution_limit'], 'Execution capacity is occupied, including UNKNOWN actions', 'EXECUTION_CAPACITY')
        backlog = self.db.execute("SELECT COUNT(*) FROM runtime_review_queue WHERE status IN ('pending','claimed','needs_review') AND COALESCE(json_extract(body,'$.policy_id'),'workspace')=?", (action['body']['policy_id'],)).fetchone()[0]
        require(backlog < policy['review_backlog_limit'], 'Return-review backlog has reached its bound', 'REVIEW_BACKLOG')
        spend = self.budget(action['body']['policy_id']); budget = policy['budget']
        require(budget['unknown_policy'] != 'stop', 'Unmeasured next-action cost requires review before execution', 'BUDGET_UNKNOWN')
        require(spend['budget_committed'] + budget['unknown_reserve'] <= budget['limit'], 'Finite budget is exhausted', 'BUDGET_STOP')

    def _check_recovery_context(self, route, task, body, method):
        recovery = route['body'].get('recovery_context')
        if recovery and not recovery.get('consumed_by') and method != 'turn/interrupt':
            from .native_inputs import NativeInputs
            require(body.get('recovery_context_sha256') == recovery['context_sha256'] == goals.fingerprint(NativeInputs(self.state).handoff_context(task)),
                    'Recovery intent must consume current checked handoff context', 'REVISION_CONFLICT')

    def _bootstrap_proof(self, route, task, proof, action=None):
        """A live owned creation receipt permits one pre-first-turn metadata bootstrap.

        This proves neither complete retained history nor atomic exclusion of native input.
        Any recorded turn intent consumes the path permanently, even if it was not sent.
        """
        code = 'BOOTSTRAP_UNAVAILABLE'
        require(isinstance(proof, dict) and proof.get('mode') == 'new_owned_thread_pre_first_turn'
                and all(isinstance(proof.get(key), str) and proof[key] for key in
                    ('session_action_id', 'session_ack_event_id', 'metadata_event_id', 'host_id', 'runtime_id', 'thread_id')),
                'Exact first-turn creation/metadata proof required', code)
        require(all(proof[key] == route[key] for key in ('host_id', 'runtime_id', 'thread_id'))
                and route['body'].get('control_mode') == 'owned_stdio', 'Bootstrap route/runtime differs', code)
        session = self._get('runtime_actions', 'id', proof['session_action_id'], True)
        require(session and session['method'] == 'thread/start' and session['task_id'] == task['id']
                and all(session[key] == route[key] for key in ('host_id', 'runtime_id', 'thread_id'))
                and session['body']['phase'] == 'identity_acknowledged' and not session['body'].get('conflict')
                and session['body'].get('ack_event') == proof['session_ack_event_id']
                and route['body'].get('ownership_fact') == proof['session_ack_event_id'],
                'Bootstrap needs the actual same-runtime thread/start acknowledgement', code)
        def inbox(identifier):
            row = self.db.execute('SELECT rowid,* FROM runtime_inbox WHERE event_id=?', (identifier,)).fetchone()
            require(row, 'Bootstrap evidence is not persisted in this store', code)
            return {**dict(row), 'body': json.loads(row['body'])}
        ack = inbox(proof['session_ack_event_id'])
        attempt = inbox(session['body'].get('attempt_event'))
        require(ack['action_id'] == attempt['action_id'] == session['id'] and ack['status'] == attempt['status'] == 'applied'
                and ack['kind'] in ('acknowledged', 'late_acknowledgement') and attempt['kind'] == 'send_attempted'
                and ack['rowid'] > attempt['rowid']
                and ack['body'].get('nativeResult', {}).get('threadId') == route['thread_id'],
                'Bootstrap creation attempt/ack chain is incomplete', code)
        connections = self.db.execute("SELECT kind FROM runtime_inbox WHERE host_id=? AND runtime_id=? AND kind IN ('runtime_initialized','runtime_disconnected','runtime_closed') ORDER BY rowid",
            (route['host_id'], route['runtime_id'])).fetchall()
        require(connections and all(row['kind'] == 'runtime_initialized' for row in connections),
                'Bootstrap cannot reopen a disconnected or closed runtime', code)
        metadata = inbox(proof['metadata_event_id'])
        thread = metadata['body'].get('thread')
        require(metadata['kind'] == 'thread_read' and metadata['status'] == 'observed'
                and all(metadata[key] == route[key] for key in ('host_id', 'runtime_id', 'thread_id'))
                and isinstance(thread, dict) and thread.get('id') == route['thread_id']
                and isinstance(thread.get('status'), dict) and thread['status'].get('type') == 'idle'
                and metadata['rowid'] > ack['rowid'], 'Bootstrap needs exact loaded-idle metadata after creation', code)
        latest = self.db.execute("SELECT rowid FROM runtime_inbox WHERE host_id=? AND runtime_id=? AND thread_id=? AND kind='thread_read' ORDER BY rowid DESC LIMIT 1",
            (route['host_id'], route['runtime_id'], route['thread_id'])).fetchone()
        require(latest and latest['rowid'] == metadata['rowid'], 'Bootstrap metadata was superseded', code)
        current_time = datetime.fromisoformat(now().replace('Z', '+00:00'))
        require(all(-5 <= (current_time - datetime.fromisoformat(metadata[key].replace('Z', '+00:00'))).total_seconds() <= 60
                    for key in ('observed_at', 'received_at')), 'Bootstrap metadata is stale', code)
        if action:
            original = action['body'].get('bootstrap_proof')
            require(isinstance(original, dict) and all(proof.get(key) == original.get(key) for key in original if key != 'metadata_event_id')
                    and metadata['rowid'] > action['body'].get('bootstrap_prepared_inbox_rowid', metadata['rowid']),
                    'Actual bootstrap guard requires a new metadata read after preparation', code)
        require(not self.db.execute("SELECT 1 FROM runtime_actions WHERE host_id=? AND thread_id=? AND (method LIKE 'turn/%' OR method='thread/resume') AND id!=? LIMIT 1",
            (route['host_id'], route['thread_id'], action['id'] if action else '')).fetchone(),
            'A prior turn/resume intent permanently closed first-turn bootstrap', code)
        for row in self.db.execute('SELECT kind,body FROM runtime_inbox WHERE host_id=? AND thread_id=?', (route['host_id'], route['thread_id'])):
            fact = json.loads(row['body']); kind = row['kind']
            require(kind not in ('turn_started', 'turn_terminal', 'turn_reconciled', 'native_user_message_observed', 'item_completed', 'token_usage', 'reconcile_unknown', 'action_unknown')
                    and not (kind == 'thread_status' and (not isinstance(fact.get('nativeStatus'), dict) or fact['nativeStatus'].get('type') != 'idle'))
                    and not (kind == 'thread_read' and isinstance(fact.get('thread'), dict)
                             and isinstance(fact['thread'].get('status'), dict)
                             and fact['thread']['status'].get('type') not in ('idle', 'notLoaded'))
                    and not (kind == 'thread_timeline_observed' and (fact.get('turns') or fact.get('messageEventIds'))),
                    'Native activity/unknown evidence closes first-turn bootstrap', code)
        from .native_inputs import NativeInputs
        view = NativeInputs(self.state).view(route['host_id'], route['thread_id'], limit=1)
        require(not view['attention_count'] and not view['foreign_active_turn_count'] and not view['quarantine'],
                'Native attention/history cannot be bypassed by first-turn bootstrap', code)
        return deepcopy(proof)

    def guard(self, request):
        self._write()
        require(isinstance(request, dict), 'Native guard request must be an object')
        with self.state.transaction():
            action = self.action(request.get('intentId'))
            body = action['body']; task = self.state.task(action['task_id'])
            route = self._scope(action['task_id'], action['method'] in ('turn/interrupt', 'turn/steer'))
            self._check_owner(task, body['owner'], body['owner_revision'])
            self._check_recovery_context(route, task, body, action['method'])
            from .native_inputs import NativeInputs
            native_input_receipt = NativeInputs(self.state).assert_ready(route, task) if action['method'] != 'turn/interrupt' else None
            require(not body.get('native_input_receipt') or body['native_input_receipt'] == native_input_receipt,
                    'Native input set/coordination changed after preparation', 'REVISION_CONFLICT')
            bootstrap = body.get('bootstrap_proof')
            require(request.get('bootstrapProof') is None or bootstrap is not None,
                    'Bootstrap was not bound by preparation', 'BOOTSTRAP_UNAVAILABLE')
            bootstrap_verified = self._bootstrap_proof(route, task, request.get('bootstrapProof'), action) if bootstrap is not None else None
            declared = request.get('intent', {})
            require(isinstance(declared, dict) and declared.get('coordinator') == body['owner'] and declared.get('ownerRevision') == body['owner_revision'],
                    'Actual initiator must bind the current coordinator version', 'REVISION_CONFLICT')
            connection = self.db.execute("SELECT kind FROM runtime_inbox WHERE host_id=? AND runtime_id=? AND kind IN ('runtime_initialized','runtime_disconnected','runtime_closed') ORDER BY rowid DESC LIMIT 1",
                                         (action['host_id'], action['runtime_id'])).fetchone()
            require(connection and connection['kind'] == 'runtime_initialized', 'Owned runtime connection is unknown/disconnected/closed', 'RUNTIME_UNAVAILABLE')
            require(body['phase'] == 'prepared', 'Native intent was already claimed/attempted; reconcile rather than resend', 'INTENT_ALREADY_USED')
            require(request.get('intentRevision') == action['revision'] and request.get('method') == action['method']
                    and request.get('requestSha256') == action['request_sha256'], 'Native request/intent differs from prepared version', 'IDENTITY_MISMATCH')
            for field, column in (('hostId', 'host_id'), ('runtimeId', 'runtime_id'), ('threadId', 'thread_id')):
                require(request.get(field) == action[column], 'Native ' + field + ' differs', 'IDENTITY_MISMATCH')
            require(request.get('turnId') == action['turn_id'], 'Native turn identity differs', 'IDENTITY_MISMATCH')
            require(request.get('clientUserMessageId') == body['params'].get('clientUserMessageId'), 'Native client correlation differs', 'IDENTITY_MISMATCH')
            require(request.get('dispatchKey') == (action['dispatch_key'] or action['id']) and request.get('workId') == task['work_id'], 'Native dispatch/work identity differs', 'IDENTITY_MISMATCH')
            require(route['revision'] == body['route_revision'] and route['body']['scope_revision'] == body['scope_revision'], 'Task route/control changed after prepare', 'REVISION_CONFLICT')
            require(self.state.get(task['work_id'])['request_revision'] == body['request_revision'], 'Work correction invalidated this intent', 'REVISION_CONFLICT')
            require(action['method'] == 'turn/interrupt' or self.state.check_work(task['work_id'], task=task['id'])['allowed'], 'Reconcile affected work status/new input before native action')
            if action['method'] not in ('turn/interrupt', 'turn/steer'):
                from .native_entry import NativeEntry
                ordinary = NativeEntry(self.state)
                require(not any((ordinary.live(item) or item.get('conflict')) and (item['task_id'] == action['task_id'] or
                        action['thread_id'] is not None and item.get('thread_id') == action['thread_id']) for item in ordinary.actions()),
                        'Ordinary native entry already owns this task/thread', 'ACTION_UNKNOWN')
                require(not any(item['id'] != action['id'] and item['body']['phase'] in LIVE for item in self._actions(action['task_id'])),
                        'Another native intent already owns this task', 'ACTION_UNKNOWN')
                require(not any(item['task_id'] != action['task_id'] and item['host_id'] == action['host_id']
                                and action['thread_id'] is not None and item['thread_id'] == action['thread_id']
                                and item['body']['phase'] in LIVE for item in self._actions()),
                        'The exact native thread has another active/unknown task', 'ACTION_UNKNOWN')
            if action['method'] in ('thread/start', 'thread/resume', 'turn/start'):
                goals.ready(self.state, task); self.state.check_decision(task)
                from .coordination import check_admission
                coordination_result = check_admission(self.state, task, operation='dispatch', target=action['thread_id'])
            else:
                coordination_result = None
            if action['method'] == 'turn/start':
                dispatch = self.state.dispatch(action['dispatch_key'])
                require(dispatch['status'] == 'prepared' and task['status'] == 'reserved',
                        'Core dispatch changed after native preparation', 'INTENT_ALREADY_USED')
                require(self.state.binding(action['thread_id']) == body['binding_snapshot'],
                        'Executor binding changed after native preparation', 'REVISION_CONFLICT')
                plan = self.state.dispatch_plan(action['dispatch_key'])
                require(plan['message'] == body['message'] and plan['message_sha256'] == goals.fingerprint(body['message']),
                        'Core dispatch message differs from exact native intent', 'IDENTITY_MISMATCH')
                self._capacity(action)
                for resource in body['resources']:
                    from .native_entry import NativeEntry
                    require(not any(resource in item['resources'] for item in NativeEntry(self.state).occupied()),
                            'Ordinary native entry holds shared object: ' + resource, 'RESOURCE_OCCUPIED')
                    claim = self._get('runtime_resource_claims', 'resource', resource, True)
                    require(not claim or claim['status'] == 'released', 'Shared object remains occupied: ' + resource, 'RESOURCE_OCCUPIED')
                for resource in body['resources']:
                    self.db.execute('INSERT INTO runtime_resource_claims VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET action_id=excluded.action_id,status=excluded.status,body=excluded.body,at=excluded.at',
                        (resource, action['id'], 'held', encoded({'release': 'explicit evidence; no timeout release'}), now()))
                body['budget_claimed'] = True
                self.db.execute("UPDATE dispatches SET status='claimed' WHERE key=? AND status='prepared'", (action['dispatch_key'],))
            if action['method'] in ('turn/interrupt', 'turn/steer'):
                self._control_target(body['control_id'], action['task_id'], route, action['turn_id'])
            resume_verified = self._resume_proof(action, request) if action['method'] == 'thread/resume' else False
            body['phase'] = 'guarded'
            if bootstrap_verified:
                body['bootstrap_guard_proof'] = bootstrap_verified
            seq = self.state.event(task['work_id'], 'native_guard_claimed', 'actual runtime guard',
                {'id': action['id'], 'request_sha256': action['request_sha256'], 'resources': body['resources'],
                 'coordination_result': coordination_result, 'bootstrap_proof': bootstrap_verified,
                 'history_complete': False if bootstrap_verified else None})
            body['guard_ref'] = 'event:' + str(seq); self._save(action)
            if action['dispatch_key']:
                plan = self.state.dispatch_plan(action['dispatch_key'])
                plan['native_guard'] = {'guard_ref': body['guard_ref'], 'intent_id': action['id'],
                    'request_sha256': action['request_sha256'], 'coverage': 'Exact native State guard committed before send boundary'}
                self.db.execute('UPDATE dispatch_plans SET body=? WHERE key=?', (encoded(plan), action['dispatch_key']))
        return {'allowed': True, 'intentId': action['id'], 'requestSha256': action['request_sha256'],
                'guardRef': body['guard_ref'], 'closedOwnedRuntimeVerified': resume_verified,
                'newOwnedThreadBootstrapVerified': bootstrap_verified is not None}

    def _project(self, action, status, fact):
        """Reuse the core dispatch/task projection; no scientific adoption here."""
        if not action['dispatch_key']:
            return
        dispatch = self.state.dispatch(action['dispatch_key'])
        task = self.state.task(action['task_id'])
        current = dispatch['status']
        if current == 'returned' and status in ('sent', 'started'):
            return
        if current == status:
            return
        self.db.execute('UPDATE dispatches SET status=?,receipt=? WHERE key=?',
                        (status, encoded({'native_event_id': fact['eventId'], 'kind': fact['kind'],
                                         'observed_at': fact['observedAt']}), dispatch['key']))
        latest = self.db.execute("SELECT id FROM runtime_actions WHERE task_id=? AND method='turn/start' ORDER BY rowid DESC LIMIT 1", (task['id'],)).fetchone()
        if latest and latest['id'] != action['id']:
            return  # Historical receipt updates never replace the current execution's task projection.
        if task['status'] not in ('adopted', 'abandoned'):
            projected = {'sent': 'dispatched', 'started': 'running', 'returned': 'returned',
                         'unknown': 'reserved', 'cancelled': 'proposed', 'invalidated': 'proposed'}[status]
            self.db.execute('UPDATE tasks SET status=? WHERE id=?', (projected, task['id']))

    def _queue(self, action, fact, status='pending'):
        route = self.route(action['task_id'])
        old = self._get('runtime_review_queue', 'task_id', action['task_id'], True)
        revision = old['revision'] + 1 if old else 1
        if old:
            if old['action_id'] != action['id']:
                positions = {row['id']: row['rowid'] for row in self.db.execute('SELECT rowid,id FROM runtime_actions WHERE id IN (?,?)', (old['action_id'], action['id']))}
                if positions.get(old['action_id'], 0) > positions.get(action['id'], 0):
                    return
            elif status != 'needs_review':
                return  # Same return/replay retains its owner, claim, disposition and revision.
        owner = old['owner'] if old and old['action_id'] == action['id'] else route['body']['review_owner']
        self.db.execute('INSERT INTO runtime_review_queue VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET action_id=excluded.action_id,owner=excluded.owner,revision=excluded.revision,status=excluded.status,body=excluded.body,at=excluded.at',
            (action['task_id'], action['id'], owner, revision, status,
             encoded({'native_event_id': fact['eventId'], 'native_status': fact.get('nativeStatus'),
                      'policy_id': action['body'].get('policy_id', 'workspace'),
                      'meaning': 'Return requires review; native completion is not scientific adoption'}), now()))

    def _native_review_target(self, task_id, review):
        """Validate an exact ordinary return without treating the task queue as its history."""
        identifier, returned = review.get('native_entry_id'), review.get('return_event')
        if identifier is None and returned is None:
            return None
        require(identifier and type(returned) is int, 'Bind review to an exact native entry and return event', 'IDENTITY_MISMATCH')
        row = self.db.execute("SELECT payload FROM events WHERE kind='native_entry_state' AND json_extract(payload,'$.id')=? ORDER BY seq DESC LIMIT 1", (identifier,)).fetchone()
        item = json.loads(row['payload']) if row else None
        require(item and item['task_id'] == task_id and item.get('return_event') == returned
                and not item.get('conflict'), 'Review belongs to another native return or unresolved conflict', 'IDENTITY_MISMATCH')
        return item

    def _record_native_review(self, task_id, disposition, review, source):
        outcome = review.get('stage_outcome')
        if outcome is None and disposition not in ('adopted', 'abandoned', 'revise'):
            return
        item = self._native_review_target(task_id, review)
        if item is None:
            return
        outcome = outcome or ('revise' if disposition == 'revise' else 'handled')
        item['return_review'] = {'return_event': item['return_event'], 'outcome': outcome,
            'task_disposition': disposition, 'reviewer': review.get('reviewer'),
            'review_role': review.get('review_role'), 'review_mode': review.get('review_mode'),
            'recorded_at': now(), 'source': source}
        if outcome in ('unresolved', 'revise'):
            item.pop('stage_review', None)
        from .native_entry import NativeEntry
        NativeEntry(self.state)._save(item, source)

    def on_review(self, task_id, disposition, review=None, source='Exact task return review'):
        if self.state.schema_version < 3:
            return
        old = self._get('runtime_review_queue', 'task_id', task_id, True)
        if old is None:
            self._record_native_review(task_id, disposition, review or {}, source)
            return
        if old:
            review = review or {}
            if (review.get('native_entry_id') or old['body'].get('native_entry_id')) and (
                    review.get('native_entry_id') != old['body'].get('native_entry_id')
                    or review.get('return_event') != old['body'].get('return_event')):
                self._record_native_review(task_id, disposition, review, source)
                return
            if old['body'].get('native_entry_id'):
                # A task-level self-check or an older stage cannot consume the
                # current return's responsibility. NativeEntry supplies identity.
                outcome = review.get('stage_outcome')
                status = {'handled': 'resolved', 'unresolved': 'pending', 'revise': 'revise'}.get(outcome)
                if status is None and disposition == 'returned' and old['status'] == 'resolved':
                    return
            else:
                status = None
            status = status or ('resolved' if disposition in ('adopted', 'abandoned') else 'revise' if disposition == 'revise' else 'pending')
            self.db.execute('UPDATE runtime_review_queue SET status=?,revision=revision+1,at=? WHERE task_id=?', (status, now(), task_id))
            if old['body'].get('native_entry_id'):
                self._record_native_review(task_id, disposition, review, source)

    def before_review(self, task_id, review):
        """Mapped native returns consume the same durable responsibility/capacity on every review path."""
        item = self._native_review_target(task_id, review)
        row = self._get('runtime_review_queue', 'task_id', task_id, True)
        if row and row['body'].get('native_entry_id'):
            require(item is not None, 'Mapped native return needs native_entry_id and return_event; use native-entry review for '
                    + row['body']['native_entry_id'] + ' return ' + str(row['body'].get('return_event')), 'IDENTITY_MISMATCH')
        if item and (row is None or row['body'].get('native_entry_id') != item['id']
                     or row['body'].get('return_event') != item['return_event']):
            require(review.get('stage_outcome') is not None,
                    'Historic return review cannot adopt or revise a newer task return', 'IDENTITY_MISMATCH')
            require(review.get('reviewer') == item['review_owner'], 'Historic return review needs its recorded receiver', 'REVISION_CONFLICT')
            return None  # Exact historical handling leaves the current queue untouched.
        if row is None:
            return None
        require(row['owner'] == review.get('reviewer'), 'Return review must consume its assigned exact owner', 'REVISION_CONFLICT')
        if item and row['status'] == 'resolved' and item.get('return_review', {}).get('outcome') == 'handled':
            return row  # Scientific adoption can follow an already handled stage.
        if item and review.get('stage_outcome') is not None and row['status'] == 'revise':
            return row  # An explicit follow-up stage judgment can address its findings.
        if row['body'].get('ordinary_observation') and not row['body'].get('policy_id'):
            require(row['status'] in ('pending', 'needs_review', 'claimed'), 'Return was already reviewed', 'REVIEW_ALREADY_RESOLVED')
            return row  # No synthetic runtime budget/capacity is needed to read a saved ordinary result.
        if row['status'] in ('pending', 'needs_review'):
            self.review_claim(task_id, row['owner'], row['revision'], row['body'].get('policy_id', 'workspace'))
        else:
            require(row['status'] == 'claimed', 'Return was already reviewed; prepare/reconcile the current return', 'REVIEW_ALREADY_RESOLVED')
        return self._get('runtime_review_queue', 'task_id', task_id)

    def review_claim(self, task_id, owner, expected, policy_id='workspace'):
        self._write()
        with self.state.transaction():
            row = self._get('runtime_review_queue', 'task_id', task_id)
            require(row['revision'] == expected and row['owner'] == owner, 'Stale/wrong return-review owner', 'REVISION_CONFLICT')
            require(row['body'].get('policy_id', 'workspace') == policy_id, 'Review capacity policy must match its own queue', 'IDENTITY_MISMATCH')
            policy = self._get('runtime_policies', 'id', policy_id)['body']
            active = self.db.execute("SELECT COUNT(*) FROM runtime_review_queue WHERE status='claimed' AND COALESCE(json_extract(body,'$.policy_id'),'workspace')=?", (policy_id,)).fetchone()[0]
            require(row['status'] in ('pending', 'needs_review') and active < policy['review_limit'], 'Review capacity is occupied', 'REVIEW_CAPACITY')
            self.db.execute("UPDATE runtime_review_queue SET status='claimed',revision=revision+1,at=? WHERE task_id=?", (now(), task_id))
        return self._get('runtime_review_queue', 'task_id', task_id)

    def review_assign(self, task_id, owner, expected, source):
        self._write()
        require(isinstance(owner, str) and owner, 'Exact return-review owner required')
        with self.state.transaction():
            row = self._get('runtime_review_queue', 'task_id', task_id)
            require(row['revision'] == expected, 'Stale review responsibility revision', 'REVISION_CONFLICT')
            self.db.execute("UPDATE runtime_review_queue SET owner=?,status='pending',revision=revision+1,at=? WHERE task_id=?", (owner, now(), task_id))
            self.state.event(self.state.task(task_id)['work_id'], 'return_review_assigned', source, {'task': task_id, 'before': row, 'owner': owner})
        return self._get('runtime_review_queue', 'task_id', task_id)

    def scope_plan(self, roots, include_dependents=True):
        require(isinstance(roots, list) and roots and len(roots) == len(set(roots)), 'Declare unique task roots')
        tasks = {row[0]: self.state.task(row[0]) for row in self.db.execute('SELECT id FROM tasks')}
        require(set(roots) <= set(tasks), 'Unknown task in scope plan')
        selected = set(roots)
        if include_dependents:
            while True:
                updated = selected | {identifier for identifier, task in tasks.items() if selected.intersection(task['body'].get('requires', []))}
                if updated == selected:
                    break
                selected = updated
        graph = [{'task': identifier, 'work_id': tasks[identifier]['work_id'],
                  'request_revision': tasks[identifier]['work_revision'],
                  'requires': sorted(tasks[identifier]['body'].get('requires', [])),
                  'requirement_versions': tasks[identifier]['body'].get('requirement_versions')}
                 for identifier in sorted(selected)]
        return {'roots': sorted(roots), 'include_dependents': bool(include_dependents),
                'tasks': sorted(selected), 'graph': graph, 'graph_sha256': goals.fingerprint(graph)}

    def control(self, identifier, kind, body, source):
        self._write()
        require(isinstance(body, dict) and isinstance(body.get('plan'), dict)
                and all(key in body['plan'] for key in ('roots', 'include_dependents'))
                and isinstance(body.get('owner_revisions'), dict) and body.get('owner'), 'Exact scope plan and coordinator versions required')
        require(kind in ('pause', 'correct', 'resume') and body.get('reason'), 'Declare pause/correct/resume reason')
        require(kind != 'correct' or type(body.get('input_id')) is int, 'Correction requires its exact input ID')
        with self.state.transaction():
            plan = body['plan']; current = self.scope_plan(plan['roots'], plan['include_dependents'])
            require(current == plan, 'Dependencies/scope changed after planning; review fresh snapshot', 'SCOPE_GRAPH_CHANGED')
            target = body.get('expected_native_target')
            if target is not None:
                require(isinstance(target, dict) and all(isinstance(target.get(key), str) and target[key] for key in ('task_id', 'host_id', 'runtime_id', 'thread_id', 'turn_id'))
                        and plan['tasks'] == [target.get('task_id')],
                        'Exact native runner control must select only its own task', 'IDENTITY_MISMATCH')
                route = self.route(target['task_id'])
                require(all(target.get(key) == route[key] for key in ('host_id', 'runtime_id', 'thread_id')),
                        'Native control route changed before commit', 'REVISION_CONFLICT')
                active = [action for action in self._actions(target['task_id']) if action['method'] == 'turn/start'
                    and action['body']['phase'] in LIVE and action['body'].get('started_event')]
                require(len(active) == 1 and active[0]['turn_id'] == target.get('turn_id')
                        and all(active[0][key] == route[key] for key in ('host_id', 'runtime_id', 'thread_id')),
                        'Exact running native turn changed before control commit', 'REVISION_CONFLICT')
            require(not self.db.execute('SELECT 1 FROM runtime_controls WHERE id=?', (identifier,)).fetchone(), 'Control identity already used')
            for task_id in plan['tasks']:
                task = self.state.task(task_id)
                self._check_owner(task, body['owner'], body['owner_revisions'].get(task['work_id']))
                if kind == 'correct':
                    scoped = self.state._input_scope(body['input_id'], task['work_id'])
                    require(scoped['scope_relation'] == 'received' or body.get('input_scope_revisions', {}).get(task['work_id']) == scoped['scope_revision'],
                            'Correction needs current explicit input scope revision', 'REVISION_CONFLICT')
                route = self.route(task_id, True)
                rows = [item for item in self._actions(task_id) if item['body']['phase'] in LIVE]
                prepared = [item for item in self._actions(task_id) if item['body']['phase'] == 'prepared']
                for action in prepared:
                    action['body'].update(phase='invalidated', invalidated_by=identifier)
                    self._save(action)
                    if action['dispatch_key']:
                        self.db.execute("UPDATE dispatches SET status='invalidated' WHERE key=?", (action['dispatch_key'],))
                        if self.state.task(task_id)['status'] == 'reserved':
                            self.db.execute("UPDATE tasks SET status='proposed' WHERE id=?", (task_id,))
                if route:
                    route['body']['paused'] = kind != 'resume'
                    route['body']['scope_revision'] += 1
                    route['body']['control_plan'] = plan
                    route['body']['control_id'] = identifier
                    self.db.execute('UPDATE runtime_routes SET revision=revision+1,body=?,at=? WHERE task_id=?', (encoded(route['body']), now(), task_id))
                capability = 'interrupt' if kind == 'pause' else 'steer'
                supported = route and route['body'].get('control_mode') == 'owned_stdio' and route['body'].get('capabilities', {}).get(capability) is True
                status = 'requested' if rows and supported and kind != 'resume' else 'unsupported' if rows or not route else 'effective'
                data = {**deepcopy(body), 'affected_actions': [item['id'] for item in rows],
                        'local_paused': kind != 'resume', 'invalidated_prepared': [item['id'] for item in prepared],
                        'native_coverage': 'Own exact runtime capability' if supported else 'External/unknown runtime control not connected',
                        'effective_scope': 'Local admission only; no active mapped action' if status == 'effective' else None}
                self.db.execute('INSERT INTO runtime_controls VALUES(?,?,?,?,?,?,?,?)',
                    (identifier, task_id, 1, kind, status, encoded(data), source, now()))
                self.state.event(task['work_id'], 'runtime_scope_' + kind, source, {'id': identifier, 'task': task_id,
                    'graph_sha256': plan['graph_sha256'], 'native_status': status, 'affected_actions': data['affected_actions']})
        return self.control_view(identifier)

    def control_view(self, identifier):
        return [{**dict(row), 'body': json.loads(row['body'])} for row in self.db.execute('SELECT * FROM runtime_controls WHERE id=? ORDER BY task_id', (identifier,))]

    def _control_target(self, identifier, task_id, route, turn_id):
        row = self.db.execute('SELECT * FROM runtime_controls WHERE id=? AND task_id=?', (identifier, task_id)).fetchone()
        require(row is not None and row['status'] in ('requested', 'delivered'), 'No applicable native-control request', 'CONTROL_UNSUPPORTED')
        data = json.loads(row['body'])
        targets = [self.action(item) for item in data['affected_actions']]
        matches = [item for item in targets if item['turn_id'] == turn_id and item['runtime_id'] == route['runtime_id']
                   and item['body'].get('started_event') and item['body']['phase'] in LIVE]
        require(route['body'].get('control_mode') == 'owned_stdio' and len(matches) == 1,
                'Control requires exact owned started turn; external runtime control unsupported', 'CONTROL_UNSUPPORTED')
        return matches[0]

    def _resume_proof(self, action, request):
        prior = request.get('priorOwnership') or action['body'].get('prior_ownership')
        require(isinstance(prior, dict) and prior.get('hostId') == action['host_id']
                and prior.get('threadId') == action['thread_id'] and prior.get('runtimeId') != action['runtime_id'], 'Exact prior ownership chain required', 'CONTROL_UNSUPPORTED')
        ref = prior.get('closedFactId', '')
        event_id = ref[len('inbox:'):] if ref.startswith('inbox:') else ref
        closed = self._get('runtime_inbox', 'event_id', event_id)
        fact = closed['body']
        require(fact['kind'] == 'runtime_closed' and fact['hostId'] == action['host_id']
                and fact['runtimeId'] == prior['runtimeId'] and fact.get('transport', {}).get('closed') is True,
                'Prior runtime closure is not proven', 'CONTROL_UNSUPPORTED')
        old = [item for item in self._actions() if item['host_id'] == action['host_id'] and item['thread_id'] == action['thread_id'] and item['runtime_id'] == prior['runtimeId']]
        require(old and all(item['body']['phase'] not in LIVE for item in old), 'Prior runtime still has uncoordinated actions', 'ACTION_UNKNOWN')
        observed = [json.loads(row[0]) for row in self.db.execute("SELECT body FROM runtime_inbox WHERE kind='turn_reconciled' AND host_id=? AND thread_id=? AND turn_id=? AND status='applied' ORDER BY rowid DESC", (action['host_id'], action['thread_id'], prior.get('terminalTurnId')))]
        require(observed and observed[0].get('nativeStatus') in TERMINAL and observed[0].get('identityResolvedWithinCoverage') is True
                and observed[0].get('coverage', {}).get('latestObservedTurnId', observed[0].get('latestObservedTurnId')) == prior.get('terminalTurnId'),
                'Latest prior terminal turn needs exact readback evidence', 'ACTION_UNKNOWN')
        return True

    def usage(self, identifier, action_id, body, source):
        self._write()
        require(isinstance(body, dict), 'Usage must be an object with explicit measurement scope')
        amount = body.get('amount')
        require(isinstance(body.get('unit'), str) and body['unit'] and (amount is None or isinstance(amount, (int, float)) and math.isfinite(amount) and amount >= 0), 'Scoped nonnegative usage or explicit unknown required')
        require(body.get('measurement') in ('delta', 'cumulative', 'unknown') and body.get('source_ref'), 'Usage needs measurement scope and evidence/source reference')
        require(body['measurement'] != 'cumulative' or body.get('counter_id'), 'Cumulative usage requires counter identity')
        with self.state.transaction():
            self.action(action_id)
            old = self._get('runtime_usage', 'id', identifier, True)
            if old:
                require(old['action_id'] == action_id and old['body'] == body and old['source'] == source, 'Usage identity conflict', 'INPUT_IDENTITY_CONFLICT')
                return old
            self.db.execute('INSERT INTO runtime_usage VALUES(?,?,?,?,?,?,?)', (identifier, action_id, body['unit'], amount, encoded(body), source, now()))
            self.state.event(self.state.task(self.action(action_id)['task_id'])['work_id'], 'runtime_usage', source, {'id': identifier, 'action': action_id, 'body': body})
        return self._get('runtime_usage', 'id', identifier)

    def release(self, resource, action_id, evidence_event, source, review=None):
        self._write()
        with self.state.transaction():
            claim = self._get('runtime_resource_claims', 'resource', resource)
            require(claim['action_id'] == action_id, 'Shared object owner changed', 'REVISION_CONFLICT')
            receipt = self._get('runtime_inbox', 'event_id', evidence_event)
            require(receipt['action_id'] == action_id and receipt['status'] == 'applied', 'Object release needs exact persisted action evidence', 'RESOURCE_UNKNOWN')
            action = self.action(action_id)
            require(action['body']['phase'] in ('terminal', 'not_sent', 'rejected') and not action['body'].get('conflict')
                    and receipt['kind'] in ('action_not_sent', 'native_rejected', 'turn_terminal', 'turn_reconciled'),
                    'Active/unknown execution can still affect the shared object', 'RESOURCE_UNKNOWN')
            if receipt['kind'] != 'action_not_sent':
                require(isinstance(review, dict) and review.get('source_ref') and review.get('reason')
                        and isinstance(review.get('evidence'), list) and review['evidence']
                        and all(isinstance(item, dict) and isinstance(item.get('path'), str) for item in review['evidence']),
                        'Object release requires explicit side-effect reconciliation evidence review', 'RESOURCE_UNKNOWN')
                task = self.state.task(action['task_id'])
                self._check_owner(task, review.get('reviewer'), review.get('owner_revision'))
                review = deepcopy(review)
                for item in review['evidence']:
                    path = local_path(self.state.root, item['path'])
                    require(path.is_file() and (not item.get('sha256') or digest(path) == item['sha256']), 'Side-effect evidence missing/changed', 'RESOURCE_UNKNOWN')
                    item['sha256'] = digest(path)
            self.db.execute("UPDATE runtime_resource_claims SET status='released',body=?,at=? WHERE resource=?", (encoded({'evidence_event': evidence_event, 'source': source, 'review': review}), now(), resource))
            self.state.event(self.state.task(self.action(action_id)['task_id'])['work_id'], 'shared_object_released', source,
                             {'resource': resource, 'action': action_id, 'evidence_event': evidence_event, 'review': review})
        return self._get('runtime_resource_claims', 'resource', resource)

    def _fact_action(self, fact):
        identifier = fact.get('intentId')
        if identifier:
            return self.action(identifier)
        matches = [item for item in self._actions() if item['host_id'] == fact['hostId']
                   and item['thread_id'] == fact.get('threadId') and item['method'] == 'turn/start']
        if fact['kind'] == 'turn_reconciled':
            require(fact.get('identityResolvedWithinCoverage') is True, 'Readback identity is unresolved', 'IDENTITY_MISMATCH')
            requested_turn, client = fact.get('requestedTurnId'), fact.get('requestedClientUserMessageId')
            matches = [item for item in matches if
                (requested_turn and item['turn_id'] == requested_turn == fact.get('turnId')) or
                (client and item['body']['params'].get('clientUserMessageId') == client
                 and client in fact.get('matchedClientUserMessageIds', []))]
        elif fact.get('turnId'):
            exact = [item for item in matches if item['turn_id'] == fact['turnId']]
            if not exact and any(item['turn_id'] is None and item['body']['phase'] in LIVE for item in matches):
                return None  # Early event is durable, not yet associated to an acknowledged turn.
            matches = exact
            require(matches, 'Native turn is not a known action target', 'IDENTITY_MISMATCH')
        else:
            matches = [item for item in matches if item['body']['phase'] in LIVE]
        require(len(matches) <= 1, 'Native fact has ambiguous action identity', 'IDENTITY_MISMATCH')
        return matches[0] if matches else None

    def _validate_fact(self, fact, action):
        if action is None:
            if fact.get('threadId'):
                require(self.db.execute('SELECT 1 FROM runtime_routes WHERE host_id=? AND thread_id=?',
                                       (fact['hostId'], fact['threadId'])).fetchone(), 'Fact targets an unmapped host/thread', 'IDENTITY_MISMATCH')
            return
        require(fact['hostId'] == action['host_id'], 'Fact host differs from prepared target', 'IDENTITY_MISMATCH')
        if action['method'] != 'thread/start':
            require(fact.get('threadId') == action['thread_id'], 'Fact thread differs from prepared target', 'IDENTITY_MISMATCH')
        if fact['kind'] != 'turn_reconciled':
            require(fact['runtimeId'] == action['runtime_id'], 'Fact runtime differs from prepared executor', 'IDENTITY_MISMATCH')
        for field, expected in (('requestSha256', action['request_sha256']), ('intentRevision', action['revision'])):
            require(field not in fact or fact[field] == expected, 'Fact request version differs', 'IDENTITY_MISMATCH')
        if fact.get('turnId') and action['turn_id']:
            require(fact['turnId'] == action['turn_id'], 'Fact turn differs', 'IDENTITY_MISMATCH')
        if fact['kind'] == 'turn_reconciled':
            require(fact.get('source') == 'public_app_server_readback' and fact.get('identityResolvedWithinCoverage') is True,
                    'Exact public readback evidence required', 'IDENTITY_MISMATCH')
            requested_turn, client = fact.get('requestedTurnId'), fact.get('requestedClientUserMessageId')
            require(requested_turn and requested_turn == action['turn_id'] == fact.get('turnId') or
                    client and client == action['body']['params'].get('clientUserMessageId') and client in fact.get('matchedClientUserMessageIds', []),
                    'Readback does not match this unknown request', 'IDENTITY_MISMATCH')

    def _apply_fact(self, fact, action):
        kind = fact['kind']; data = action['body']; task = self.state.task(action['task_id'])
        data['projection_revision'] = data.get('projection_revision', 0) + 1
        action['observed_at'] = latest_observed(action['observed_at'], fact['observedAt'])
        if kind == 'send_attempted':
            require(data['phase'] == 'guarded' and fact.get('guardRef') == data.get('guard_ref'), 'Send attempt lacks exact committed guard', 'GUARD_REQUIRED')
            route = self._scope(task['id'], action['method'] in ('turn/interrupt', 'turn/steer'))
            self._check_recovery_context(route, task, data, action['method'])
            if data.get('bootstrap_proof') is not None:
                self._bootstrap_proof(route, task, data.get('bootstrap_guard_proof'), action)
            if action['method'] != 'turn/interrupt':
                from .native_inputs import NativeInputs
                current_input_receipt = NativeInputs(self.state).assert_ready(route, task)
                require(not data.get('native_input_receipt') or data['native_input_receipt'] == current_input_receipt,
                        'Native input set/coordination changed before the send boundary', 'REVISION_CONFLICT')
            require(route['revision'] == data['route_revision'] and self.state.get(task['work_id'])['request_revision'] == data['request_revision'],
                    'Scope changed between guard and send boundary', 'REVISION_CONFLICT')
            data.update(phase='attempted', attempt_event=fact['eventId'], native_request_id=fact.get('requestId'))
        elif kind in ('acknowledged', 'late_acknowledgement'):
            require(data.get('attempt_event'), 'Acknowledgement lacks persisted send attempt', 'GUARD_REQUIRED')
            if action['method'] in ('thread/start', 'thread/resume'):
                native = fact.get('nativeResult', {})
                thread = native.get('threadId')
                require(isinstance(thread, str) and thread and (action['thread_id'] is None or thread == action['thread_id']), 'Thread creation/resume acknowledgement differs', 'IDENTITY_MISMATCH')
                action['thread_id'] = thread
                route = self.route(task['id']); route['thread_id'] = thread
                route['body']['identity_status'] = 'acknowledged_persistence_unconfirmed'
                route['body']['ownership_fact'] = fact['eventId']
                self.db.execute('UPDATE runtime_routes SET thread_id=?,revision=revision+1,body=?,at=? WHERE task_id=?',
                    (thread, encoded(route['body']), now(), task['id']))
                previous = self.state.binding(thread)
                self.state.bind(thread, task['work_id'], 'executor', 'native thread acknowledgement', previous['revision'] if previous else 0)
                data['phase'] = 'identity_acknowledged'
            elif action['method'] == 'turn/start':
                turn = fact.get('nativeTurnId') or fact.get('nativeResult', {}).get('turnId')
                require(isinstance(turn, str) and turn and (action['turn_id'] is None or action['turn_id'] == turn), 'Acknowledged turn differs', 'IDENTITY_MISMATCH')
                action['turn_id'] = turn
                if data['phase'] != 'terminal' and not data.get('conflict'):
                    data['phase'] = 'acknowledged'; self._project(action, 'sent', fact)
            else:
                data['phase'] = 'control_delivered'
                self.db.execute("UPDATE runtime_controls SET status='delivered',revision=revision+1,at=? WHERE id=? AND task_id=? AND status='requested'",
                                (now(), data['control_id'], task['id']))
            data['ack_event'] = fact['eventId']
            recovery_route = self.route(task['id'])
            recovery = recovery_route['body'].get('recovery_context')
            if recovery and not recovery.get('consumed_by') and data.get('recovery_context_sha256') == recovery['context_sha256']:
                recovery.update(consumed_by=action['id'], acknowledgement_event=fact['eventId'])
                self.db.execute('UPDATE runtime_routes SET body=? WHERE task_id=?', (encoded(recovery_route['body']), task['id']))
        elif kind in ('action_unknown', 'native_rejected', 'action_not_sent'):
            if data['phase'] != 'terminal':
                data['phase'] = 'unknown' if kind == 'action_unknown' else 'not_sent' if kind == 'action_not_sent' else 'rejected'
                self._project(action, 'unknown' if kind == 'action_unknown' else 'cancelled', fact)
                self.db.execute('UPDATE runtime_resource_claims SET status=?,body=?,at=? WHERE action_id=? AND status!=?',
                    ('unknown' if kind == 'action_unknown' else 'held', encoded({'native_event_id': fact['eventId'], 'no_automatic_release': True}), now(), action['id'], 'released'))
                if kind == 'action_not_sent':
                    data['budget_claimed'] = False
                if data.get('control_id'):
                    self.db.execute('UPDATE runtime_controls SET status=?,revision=revision+1,at=? WHERE id=? AND task_id=?',
                        ('unknown' if kind == 'action_unknown' else 'unsupported', now(), data['control_id'], task['id']))
        elif kind in ('turn_started', 'turn_terminal', 'turn_reconciled'):
            if action['turn_id'] is None:
                require(kind == 'turn_reconciled', 'Native turn is not yet acknowledged', 'IDENTITY_MISMATCH')
                action['turn_id'] = fact['turnId']
            status = fact.get('nativeStatus')
            if kind == 'turn_started':
                data['started_event'] = fact['eventId']
                if data['phase'] != 'terminal' and not data.get('conflict'):
                    data.update(phase='acknowledged', native_status='inProgress')
                    self._project(action, 'started', fact)
            elif status in TERMINAL:
                old = data.get('native_terminal_status')
                if old and old != status or data.get('conflict'):
                    data['conflict'] = sorted(set(data.get('conflict', []) + [data.get('terminal_event'), fact['eventId']]) - {None})
                    data['phase'] = 'unknown'; self._project(action, 'unknown', fact); self._queue(action, fact, 'needs_review')
                else:
                    data.update(phase='terminal', native_terminal_status=status, terminal_event=fact['eventId'])
                    self._project(action, 'returned', fact); self._queue(action, fact)
                    for control_action in self._actions(task['id']):
                        if control_action['method'] in ('turn/interrupt', 'turn/steer') and control_action['turn_id'] == action['turn_id'] and control_action['body']['phase'] in LIVE | {'control_delivered'}:
                            control_action['body'].update(phase='control_terminal', terminal_event=fact['eventId'],
                                                          effect_status='Only target-turn termination proven; correction applicability separately reviewed')
                            self._save(control_action)
                    for control in self.db.execute("SELECT id,body FROM runtime_controls WHERE task_id=? AND kind='pause' AND status IN ('requested','delivered','unknown')", (task['id'],)).fetchall():
                        scope = json.loads(control['body'])
                        if action['id'] in scope['affected_actions']:
                            scope['effective_evidence'] = {'action': action['id'], 'turn_id': action['turn_id'], 'event_id': fact['eventId']}
                            self.db.execute("UPDATE runtime_controls SET status='effective',body=?,revision=revision+1,at=? WHERE id=? AND task_id=?",
                                            (encoded(scope), now(), control['id'], task['id']))
            elif kind == 'turn_reconciled' and status == 'inProgress' and data['phase'] != 'terminal' and not data.get('conflict'):
                data.update(phase='acknowledged', readback_in_progress_event=fact['eventId'])
                self._project(action, 'sent', fact)
            elif kind == 'turn_terminal':
                data.update(phase='unknown', invalid_terminal_event=fact['eventId']); self._project(action, 'unknown', fact)
        elif kind == 'token_usage':
            self.usage(fact['eventId'], action['id'], {'unit': 'tokens', 'amount': None, 'measurement': 'unknown',
                'source_ref': fact['eventId'], 'reported_native_counters': fact.get('tokenUsage'),
                'coverage': 'Native thread cumulative/last counters; exact action attribution unresolved'}, 'native observation')
        elif kind == 'item_completed':
            if fact.get('itemType') == 'agentMessage':
                data['result_fact'] = fact['eventId']
        self._save(action)

    def ingest(self, fact):
        """Commit before returning persisted:true. Rejected identity evidence remains auditable."""
        self._write()
        require(isinstance(fact, dict) and fact.get('formatVersion') == '1.0'
                and all(isinstance(fact.get(k), str) and fact[k] for k in ('eventId', 'hostId', 'runtimeId', 'source', 'kind', 'observedAt')),
                'Versioned native fact identity/source required')
        try:
            observed = datetime.fromisoformat(fact['observedAt'].replace('Z', '+00:00'))
            require(observed.tzinfo is not None, 'Native observedAt requires explicit time-zone coverage')
        except ValueError as exc:
            raise HarnessError('Native observedAt must be an actual ISO timestamp') from exc
        identity = {key: value for key, value in fact.items() if key not in ('observedAt', 'runtimeId')} if fact['eventId'].startswith('native:') else fact
        hashed = goals.fingerprint(identity)
        with self.state.transaction():
            old = self._get('runtime_inbox', 'event_id', fact['eventId'], True)
            if old:
                require(old['payload_sha256'] == hashed, 'Native event ID conflicts with another payload', 'EVENT_IDENTITY_CONFLICT')
                return {'persisted': old['status'] != 'rejected', 'eventId': 'inbox:' + fact['eventId'], 'status': old['status'], 'duplicate': True}
            action, status, error, observation_work = None, 'observed', None, None
            # A savepoint isolates semantic projection failures without dropping raw evidence.
            try:
                with self.state.transaction():
                    global_fact = fact['kind'] in ('runtime_initialized', 'runtime_disconnected', 'runtime_closed', 'client_request_unsupported')
                    observation = fact['kind'] in ('thread_read', 'reconcile_unknown', 'thread_status', 'native_user_message_observed', 'thread_timeline_observed')
                    if observation:
                        require(fact.get('threadId'), 'Exact thread is required for read-only observation', 'IDENTITY_MISMATCH')
                        self._validate_fact(fact, None)
                        if fact['kind'] in ('native_user_message_observed', 'thread_timeline_observed'):
                            from .native_inputs import NativeInputs
                            native = NativeInputs(self.state); native.validate(fact)
                            scopes = {self.state.task(route['task_id'])['work_id'] for route in native.routes(fact['hostId'], fact['threadId'])}
                            observation_work = next(iter(scopes)) if len(scopes) == 1 else None
                    elif not global_fact:
                        action = self._fact_action(fact)
                        self._validate_fact(fact, action)
                    if fact['kind'] in ('runtime_disconnected', 'runtime_closed'):
                        for item in self._actions():
                            if item['host_id'] == fact['hostId'] and item['runtime_id'] == fact['runtimeId'] and item['body']['phase'] in LIVE:
                                item['body'].update(phase='unknown', disconnect_event=fact['eventId'])
                                item['observed_at'] = latest_observed(item['observed_at'], fact['observedAt']); self._project(item, 'unknown', fact); self._save(item)
                                self.db.execute("UPDATE runtime_resource_claims SET status='unknown' WHERE action_id=? AND status!='released'", (item['id'],))
                        status = 'applied'
                    elif action:
                        self._apply_fact(fact, action); status = 'applied'
                    elif not global_fact and not observation:
                        status = 'pending'
            except HarnessError as exc:
                status, error = 'rejected', {'code': exc.code, 'message': str(exc)}
            self.db.execute('INSERT INTO runtime_inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (fact['eventId'], hashed, fact['kind'], fact['hostId'], fact['runtimeId'], fact.get('threadId'),
                 fact.get('turnId'), action['id'] if action else None, status, encoded(fact), fact['observedAt'], now()))
            seq = self.state.event(self.state.task(action['task_id'])['work_id'] if action else observation_work,
                'native_fact_' + status, 'native durable inbox', {'native_event_id': fact['eventId'], 'kind': fact['kind'], 'error': error},
                external_event_id=fact['eventId'], origin=fact['source'])
            if action and status == 'applied' and fact['kind'] in ('acknowledged', 'late_acknowledgement') and action['turn_id']:
                for row in self.db.execute("SELECT event_id,body FROM runtime_inbox WHERE status='pending' AND host_id=? AND thread_id=? AND turn_id=? ORDER BY rowid", (action['host_id'], action['thread_id'], action['turn_id'])).fetchall():
                    pending = json.loads(row['body'])
                    try:
                        with self.state.transaction():
                            self._validate_fact(pending, action); self._apply_fact(pending, action)
                        replay_status = 'applied'
                    except HarnessError as exc:
                        replay_status = 'rejected'
                        self.state.event(self.state.task(action['task_id'])['work_id'], 'native_pending_rejected', 'native durable inbox',
                                         {'native_event_id': row['event_id'], 'code': exc.code, 'reason': str(exc)})
                    self.db.execute('UPDATE runtime_inbox SET action_id=?,status=? WHERE event_id=?', (action['id'], replay_status, row['event_id']))
        return {'persisted': status != 'rejected', 'eventId': 'inbox:' + fact['eventId'], 'status': status, 'eventSeq': seq, 'error': error}

    def resolve(self, identifier, expected, body, source):
        self._write()
        require(isinstance(body, dict) and all(key in body for key in ('reviewer', 'owner_revision', 'evidence_event', 'reason', 'source_ref')),
                'Conflict adjudication requires exact owner, readback and reason/source')
        with self.state.transaction():
            action = self.action(identifier); task = self.state.task(action['task_id'])
            self._check_owner(task, body['reviewer'], body['owner_revision'])
            require(action['body'].get('projection_revision', 0) == expected, 'Stale native projection review', 'REVISION_CONFLICT')
            require(body.get('reason') and body.get('source_ref'), 'Explicit conflict adjudication needs reason/source')
            evidence = self._get('runtime_inbox', 'event_id', body['evidence_event'])
            fact = evidence['body']
            require(evidence['action_id'] == identifier and evidence['status'] == 'applied'
                    and fact['kind'] == 'turn_reconciled' and fact.get('nativeStatus') in TERMINAL,
                    'Conflict resolution needs exact persisted terminal readback', 'ACTION_UNKNOWN')
            self._validate_fact(fact, action)
            previous = deepcopy(action['body'])
            action['body'].update(phase='terminal', native_terminal_status=fact['nativeStatus'], terminal_event=fact['eventId'],
                                  conflict=None, resolution=body, projection_revision=expected + 1)
            self._project(action, 'returned', fact); self._save(action); self._queue(action, fact, 'needs_review')
            self.state.event(task['work_id'], 'native_conflict_adjudicated', source, {'action': identifier,
                'before': previous, 'assessment': body, 'projection_revision': expected + 1})
        return self.action(identifier)

    def overview(self, work=None, limit=30):
        require(1 <= limit <= 100, 'Runtime overview limit must be 1..100')
        selected = [self.state.task(row[0]) for row in self.db.execute('SELECT id FROM tasks' + (' WHERE work_id=?' if work else '') + ' ORDER BY rowid DESC', (work,) if work else ())]
        if self.state.schema_version < 3:
            return {'schema_version': self.state.schema_version, 'tasks': [{'id': item['id'], 'status': item['status'], 'runtime': 'legacy_unknown'} for item in selected[:limit]],
                    'coverage': 'Legacy records readable; native runtime/control not mapped'}
        values = []
        for task in selected[:limit]:
            route = self.route(task['id'], True)
            actions = self._actions(task['id'])
            review = self._get('runtime_review_queue', 'task_id', task['id'], True)
            attention = None
            if route and route['thread_id']:
                from .native_inputs import NativeInputs
                native = NativeInputs(self.state).view(route['host_id'], route['thread_id'], limit=1)
                attention = {key: native[key] for key in ('epoch', 'pending', 'attention_count', 'foreign_active_turns', 'foreign_active_turn_count', 'quarantine', 'native_effect')}
            values.append({'id': task['id'], 'work_id': task['work_id'], 'scientific_status': task['status'],
                'entry': route['body'].get('entry') if route else None, 'entry_coverage': route['body'].get('entry_coverage', 'No verified native UI entry') if route else 'legacy_unknown',
                'route': route, 'actions': [{'id': item['id'], 'dispatch_key': item['dispatch_key'], 'method': item['method'],
                    'phase': item['body']['phase'], 'native_status': item['body'].get('native_terminal_status'),
                    'last_observed_at': item['observed_at'], 'turn_id': item['turn_id'], 'conflict': item['body'].get('conflict'),
                    'recovery': 'Exact readonly reconcile; no resend' if item['body']['phase'] == 'unknown' else None} for item in actions[-4:]],
                'review': review, 'native_input_attention': attention,
                'usable_artifacts': task['body'].get('reviews', [])[-1:][0]['review'].get('artifacts', []) if task['status'] == 'adopted' and task['body'].get('reviews') else []})
        policies = [{**self._get('runtime_policies', 'id', row[0]), 'usage': self.budget(row[0])} for row in self.db.execute('SELECT id FROM runtime_policies')]
        controls = [{**dict(row), 'body': json.loads(row['body'])} for row in self.db.execute('SELECT * FROM runtime_controls ORDER BY rowid DESC LIMIT ?', (limit,))]
        claims = [{**dict(row), 'body': json.loads(row['body'])} for row in self.db.execute("SELECT * FROM runtime_resource_claims WHERE status!='released' ORDER BY resource")]
        from .native_entry import NativeEntry
        return {'schema_version': 3, 'tasks': values, 'omitted_tasks': max(0, len(selected) - limit),
                'ordinary_native': NativeEntry(self.state).recover(work, limit=min(limit, 12)) if work else None,
                'policies': policies, 'controls': controls, 'occupied_resources': claims,
                'coverage': 'Persisted native facts on mapped paths; local pause differs from native effective receipt; scientific review remains separate'}
