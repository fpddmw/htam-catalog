"""Explicit schema upgrades and recovery. Opening an existing State never migrates it."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

from ..common import HarnessError, connect, digest, encoded, now, require

VERSION = 3
MIGRATION = 'runtime-foundation-to-3'
BASE_TABLES = {'works', 'events', 'bindings', 'inputs', 'tasks', 'dispatches',
               'observations', 'judgments', 'dispatch_plans'}
ASSET_TABLES = {'assets', 'asset_versions', 'asset_revisions', 'asset_uses'}
NEW_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS binding_versions(session TEXT PRIMARY KEY REFERENCES bindings(session),
       revision INTEGER NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS input_receipts(input_id INTEGER PRIMARY KEY REFERENCES inputs(id),
       received_work_id TEXT REFERENCES works(id), ownership_status TEXT NOT NULL,
       raw_source TEXT NOT NULL, input_sha256 TEXT NOT NULL, observed_at TEXT, occurred_at TEXT,
       receipt_event_seq INTEGER REFERENCES events(seq), provenance TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS input_scopes(input_id INTEGER NOT NULL REFERENCES inputs(id),
       work_id TEXT NOT NULL REFERENCES works(id), relation TEXT NOT NULL,
       revision INTEGER NOT NULL, acknowledged INTEGER NOT NULL, source TEXT NOT NULL,
       body TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(input_id,work_id))''',
    '''CREATE TABLE IF NOT EXISTS input_assessments(input_id INTEGER NOT NULL REFERENCES inputs(id),
       revision INTEGER NOT NULL, kind TEXT NOT NULL, reviewer TEXT NOT NULL, source TEXT NOT NULL,
       input_sha256 TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL,
       PRIMARY KEY(input_id,revision))''',
    '''CREATE TABLE IF NOT EXISTS event_metadata(event_seq INTEGER PRIMARY KEY REFERENCES events(seq),
       envelope_version TEXT NOT NULL, request_id TEXT, external_event_id TEXT,
       session TEXT, occurred_at TEXT, origin TEXT)''',
    '''CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,
       migration_id TEXT NOT NULL, checksum TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS input_scopes_work ON input_scopes(work_id,acknowledged,input_id)',
)
NEW_TABLES = {'binding_versions', 'input_receipts', 'input_scopes', 'input_assessments',
              'event_metadata', 'schema_migrations'}


def checksum():
    from ..state import SCHEMA
    from ..assets import SCHEMA as ASSET_SCHEMA
    from ..runtime import SCHEMA as RUNTIME_SCHEMA
    return hashlib.sha256(encoded({'migration_source': digest(Path(__file__)), 'base_schema': SCHEMA,
        'assets_schema': ASSET_SCHEMA, 'input_schema': NEW_SCHEMA, 'runtime_schema': RUNTIME_SCHEMA}).encode()).hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _shape(db, name):
    columns = [tuple(row) for row in db.execute('PRAGMA table_info(' + quote(name) + ')')]
    foreign = sorted(tuple(row) for row in db.execute('PRAGMA foreign_key_list(' + quote(name) + ')'))
    unique = []
    for row in db.execute('PRAGMA index_list(' + quote(name) + ')'):
        if row[2]:
            unique.append((tuple(x[2] for x in db.execute('PRAGMA index_info(' + quote(row[1]) + ')')), row[4]))
    return columns, foreign, sorted(unique)


def inspect_schema(db):
    """Recognize the actual v0/v1 layout and additive v2/v3 layouts, including constraints."""
    from ..state import SCHEMA
    from ..assets import SCHEMA as ASSET_SCHEMA
    from ..runtime import SCHEMA as RUNTIME_SCHEMA, TABLES as RUNTIME_TABLES
    version = db.execute('PRAGMA user_version').fetchone()[0]
    require(version in (0, 1, 2, VERSION), 'Unsupported state database version: ' + str(version), 'UNSUPPORTED_SCHEMA')
    names = tables(db)
    expected = BASE_TABLES | (NEW_TABLES if version >= 2 else set()) | (RUNTIME_TABLES if version >= 3 else set())
    if names & ASSET_TABLES:
        expected |= ASSET_TABLES
    require(names == expected, 'Unrecognized or incomplete state schema; use migration preflight/repair', 'CORRUPT_STORE')
    reference = sqlite3.connect(':memory:')
    try:
        reference.executescript(SCHEMA)
        for statement in (*ASSET_SCHEMA, *NEW_SCHEMA, *RUNTIME_SCHEMA):
            reference.execute(statement)
        require(all(_shape(db, name) == _shape(reference, name) for name in names),
                'State schema constraints differ from the supported layout', 'CORRUPT_STORE')
        require(not db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone(),
                'Unrecognized state triggers/views require explicit review', 'UNSUPPORTED_SCHEMA')
    finally:
        reference.close()
    return version


def input_hash(session, turn, prompt, raw_source):
    return hashlib.sha256(encoded({'session': session, 'turn_id': turn, 'prompt': prompt,
                                  'raw_source': raw_source}).encode('utf-8')).hexdigest()


def legacy_inputs(db):
    """Read evidence of receipt, never infer ownership from today's binding."""
    hooks, reconciled = {}, {}
    for row in db.execute("SELECT seq,work_id,kind,payload,at FROM events WHERE kind IN ('hook_observed','input_reconciled') ORDER BY seq"):
        seq, work, kind, payload, at = tuple(row)
        payload = json.loads(payload)
        if kind == 'hook_observed' and payload.get('event') == 'UserPromptSubmit':
            hooks.setdefault((payload.get('session'), payload.get('turn')), []).append(
                {'seq': seq, 'work_id': work, 'at': at, 'raw_source': payload.get('source')})
        elif kind == 'input_reconciled':
            reconciled.setdefault(payload.get('input_id'), []).append({'seq': seq, 'work_id': work, 'at': at})
    works = {r[0] for r in db.execute('SELECT id FROM works')}
    result = []
    for values in db.execute('SELECT id,session,turn_id,prompt,acknowledged FROM inputs ORDER BY id'):
        identifier, session, turn, prompt, ack = tuple(values)
        matches = hooks.get((session, turn), [])
        owners = {event['work_id'] for event in matches}
        owner = next(iter(owners)) if len(owners) == 1 else None
        status = ('legacy_missing_receipt' if not matches else 'legacy_conflict' if len(owners) != 1
                  else 'unbound' if owner is None else 'recorded')
        if owner is not None and owner not in works:
            owner, status = None, 'legacy_conflict'
        raw_sources = {encoded(event['raw_source']) for event in matches}
        raw = json.loads(next(iter(raw_sources))) if len(raw_sources) == 1 else None
        receipt = {'input_id': identifier, 'received_work_id': owner, 'ownership_status': status,
                   'raw_source': encoded(raw), 'input_sha256': input_hash(session, turn, prompt, raw),
                   'observed_at': matches[0]['at'] if matches else None, 'occurred_at': None,
                   'receipt_event_seq': matches[0]['seq'] if matches and status != 'legacy_conflict' else None,
                   'provenance': encoded({'legacy': True, 'receipt_events': matches,
                                          'reconciliation_events': reconciled.get(identifier, []),
                                          'source_status': 'unknown; receipt metadata does not establish human origin'})}
        scopes = []
        if owner:
            scopes.append({'work_id': owner, 'relation': 'received', 'acknowledged': ack,
                           'source': 'legacy receipt event', 'at': receipt['observed_at'],
                           'body': {'receipt_event_seq': receipt['receipt_event_seq']}})
        for event in reconciled.get(identifier, []):
            if event['work_id'] in works and all(scope['work_id'] != event['work_id'] for scope in scopes):
                scopes.append({'work_id': event['work_id'], 'relation': 'legacy_reviewed', 'acknowledged': 1,
                               'source': 'legacy input_reconciled event', 'at': event['at'],
                               'body': {'reconciliation_event_seq': event['seq'], 'human_origin': 'unknown'}})
        result.append({'input': {'id': identifier, 'session': session, 'turn_id': turn,
                                 'prompt': prompt, 'acknowledged': ack}, 'receipt': receipt, 'scopes': scopes})
    return result


def table_hashes(db, names=None, columns=None):
    result = {}
    for name in sorted(names or tables(db)):
        selected = (columns or {}).get(name)
        expression = ','.join(quote(c) for c in selected) if selected else '*'
        hashed, count = hashlib.sha256(), 0
        for row in db.execute('SELECT ' + expression + ' FROM ' + quote(name) + ' ORDER BY rowid'):
            hashed.update((encoded(list(row)) + '\n').encode('utf-8'))
            count += 1
        result[name] = {'rows': count, 'sha256': hashed.hexdigest()}
    return result


def fingerprint(db):
    schema = [list(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    return hashlib.sha256(encoded({'schema_version': db.execute('PRAGMA user_version').fetchone()[0],
                                  'schema': schema, 'tables': table_hashes(db)}).encode('utf-8')).hexdigest()


def checked_backup(db, output):
    """Exclusive destination creation; caller supplies a stable read transaction."""
    output = Path(output).resolve()
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    destination = None
    try:
        destination = sqlite3.connect(output)
        db.backup(destination)
        destination.commit()
        require(destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Backup integrity check failed', 'CORRUPT_STORE')
        require(not destination.execute('PRAGMA foreign_key_check').fetchall(), 'Backup foreign key check failed', 'CORRUPT_STORE')
        result = {'path': str(output), 'fingerprint': fingerprint(destination)}
    except BaseException:
        if destination:
            destination.close()
        output.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        result['sha256'] = digest(output)
        return result


def preflight(path):
    db = connect(Path(path), readonly=True)
    try:
        db.execute('BEGIN')
        version = inspect_schema(db)
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'State integrity check failed', 'CORRUPT_STORE')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'State foreign key check failed', 'CORRUPT_STORE')
        receipts = legacy_inputs(db) if version < 2 else []
        ownership = {}
        for entry in receipts:
            status = entry['receipt']['ownership_status']
            ownership[status] = ownership.get(status, 0) + 1
        return {'format': 'htam.migration-plan/v1', 'migration_id': MIGRATION, 'migration_checksum': checksum(),
                'source_version': version, 'target_version': VERSION, 'fingerprint': fingerprint(db),
                'tables': table_hashes(db), 'ownership': ownership,
                'source_policy': 'No historical human-origin attestation inferred', 'observed_at': now(),
                'needed': version != VERSION}
    finally:
        db.close()


def _upgrade_inputs(db, plan, fault=None):
    legacy = legacy_inputs(db)
    for statement in NEW_SCHEMA:
        db.execute(statement)
    db.execute('INSERT INTO binding_versions SELECT session,1 FROM bindings')
    for entry in legacy:
        receipt = entry['receipt']
        db.execute('INSERT INTO input_receipts VALUES(?,?,?,?,?,?,?,?,?)', tuple(receipt.values()))
        for scope in entry['scopes']:
            db.execute('INSERT INTO input_scopes VALUES(?,?,?,?,?,?,?,?)',
                       (receipt['input_id'], scope['work_id'], scope['relation'], 1, scope['acknowledged'],
                        scope['source'], encoded(scope['body']), scope['at'] or now()))
    if fault:
        fault('after_backfill')
    require(table_hashes(db, BASE_TABLES | (tables(db) & ASSET_TABLES)) == plan['tables'],
            'Migration changed protected legacy rows', 'MIGRATION_VERIFY_FAILED')
    db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?,?)',
               (2, 'input-foundation-1-to-2', checksum(), encoded({'before_fingerprint': plan['fingerprint'],
                'legacy_tables': plan['tables'], 'ownership': plan['ownership']}), now()))
    db.execute('PRAGMA user_version=2')


def _upgrade(db, plan, fault=None):
    from ..runtime import SCHEMA as RUNTIME_SCHEMA
    if plan['source_version'] < 2:
        _upgrade_inputs(db, plan, fault)
    protected = table_hashes(db)
    for statement in RUNTIME_SCHEMA:
        db.execute(statement)
    if fault:
        fault('after_runtime_schema')
    require(table_hashes(db, set(protected)) == protected, 'Runtime migration changed existing facts', 'MIGRATION_VERIFY_FAILED')
    db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?,?)',
               (VERSION, MIGRATION, checksum(), encoded({'before_fingerprint': plan['fingerprint'],
                'legacy_tables': plan['tables'], 'protected_before_runtime': protected}), now()))
    db.execute('PRAGMA user_version=3')


def apply(path, plan, backup_path, *, fault=None):
    path = Path(path).resolve()
    require(plan.get('format') == 'htam.migration-plan/v1' and plan.get('migration_id') == MIGRATION
            and plan.get('migration_checksum') == checksum() and plan.get('target_version') == VERSION,
            'Migration plan/version differs from this build; run migration-plan again', 'MIGRATION_PLAN_STALE')
    require(path.is_file(), 'State store does not exist', 'NO_STORE')
    db = connect(path, create=False)
    backup = None
    try:
        db.execute('BEGIN IMMEDIATE')
        version = inspect_schema(db)
        require(version == plan['source_version'] and fingerprint(db) == plan['fingerprint'],
                'State changed after migration plan; run migration-plan again', 'MIGRATION_PLAN_STALE')
        require(version < VERSION, 'State already uses the target schema', 'MIGRATION_NOT_NEEDED')
        reader = connect(path, readonly=True)
        try:
            reader.execute('BEGIN')
            backup = checked_backup(reader, backup_path)
        finally:
            reader.close()
        require(backup['fingerprint'] == plan['fingerprint'], 'Backup differs from locked source', 'MIGRATION_VERIFY_FAILED')
        _upgrade(db, plan, fault)
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Migrated foreign keys failed', 'MIGRATION_VERIFY_FAILED')
        inspect_schema(db)
        db.commit()
        return {'schema_version': VERSION, 'backup': backup, 'verification': verify(path)}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def verify(path):
    db = connect(Path(path), readonly=True)
    try:
        db.execute('BEGIN')
        version = inspect_schema(db)
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        foreign = [list(r) for r in db.execute('PRAGMA foreign_key_check')]
        protected = None
        if version >= 2:
            row = db.execute('SELECT body FROM schema_migrations WHERE version=?', (version,)).fetchone()
            if row:
                original = json.loads(row[0])['legacy_tables']
                current = table_hashes(db, set(original))
                if 'schema_migrations' in original:
                    hashed, count = hashlib.sha256(), 0
                    for record in db.execute('SELECT * FROM schema_migrations WHERE version<? ORDER BY rowid', (version,)):
                        hashed.update((encoded(list(record)) + '\n').encode('utf-8')); count += 1
                    current['schema_migrations'] = {'rows': count, 'sha256': hashed.hexdigest()}
                protected = current == original
        return {'schema_version': version, 'integrity': integrity, 'foreign_key_errors': foreign,
                'fingerprint': fingerprint(db), 'tables': table_hashes(db),
                'legacy_rows_unchanged_since_migration': protected,
                'note': 'Later authorized updates may change legacy rows; compare recovery deltas before rollback.'}
    finally:
        db.close()


def _rows(db, name):
    return {row[0]: list(row[1:]) for row in db.execute('SELECT rowid,* FROM ' + quote(name) + ' ORDER BY rowid')}


def recovery_export(baseline, current):
    before, after = connect(Path(baseline), readonly=True), connect(Path(current), readonly=True)
    try:
        before.execute('BEGIN'); after.execute('BEGIN')
        inspect_schema(before)
        target_version = inspect_schema(after)
        require(target_version in (2, VERSION), 'Recovery delta requires supported schema 2/3', 'UNSUPPORTED_SCHEMA')
        changes = {}
        for name in sorted(tables(before) | tables(after)):
            old = _rows(before, name) if name in tables(before) else {}
            new = _rows(after, name) if name in tables(after) else {}
            changes[name] = {'columns': [r[1] for r in after.execute('PRAGMA table_info(' + quote(name) + ')')],
                             'delete_rowids': [rowid for rowid in old if rowid not in new or old[rowid] != new[rowid]],
                             'rows': [[rowid, *values] for rowid, values in new.items() if rowid not in old or old[rowid] != values]}
        return {'format': 'htam.recovery-delta/v1', 'migration_checksum': checksum(), 'observed_at': now(),
                'baseline_fingerprint': fingerprint(before), 'target_fingerprint': fingerprint(after),
                'target_schema': target_version, 'target_tables': sorted(tables(after)), 'changes': changes,
                'scope': 'All table differences, including new instructions, approvals and receipts; no schema downgrade.'}
    finally:
        before.close(); after.close()


def restore(baseline, output, delta=None):
    """Restore to a NEW path; supported v1 delta reconstructs exact forward schema 2/3."""
    baseline, output = Path(baseline).resolve(), Path(output).resolve()
    reader = connect(baseline, readonly=True)
    try:
        reader.execute('BEGIN')
        inspect_schema(reader)
        if delta:
            require(delta.get('format') == 'htam.recovery-delta/v1' and delta.get('target_schema') in (2, VERSION)
                    and isinstance(delta.get('migration_checksum'), str)
                    and delta.get('baseline_fingerprint') == fingerprint(reader),
                    'Recovery delta does not match this backup/build', 'RECOVERY_MISMATCH')
        checked_backup(reader, output)
    finally:
        reader.close()
    if delta:
        from ..assets import SCHEMA as ASSET_SCHEMA
        from ..runtime import SCHEMA as RUNTIME_SCHEMA
        db = connect(output, create=False)
        try:
            db.execute('BEGIN IMMEDIATE')
            for statement in NEW_SCHEMA:
                db.execute(statement)
            if delta['target_schema'] >= 3:
                for statement in RUNTIME_SCHEMA:
                    db.execute(statement)
            if ASSET_TABLES <= set(delta['target_tables']):
                for statement in ASSET_SCHEMA:
                    db.execute(statement)
            require(tables(db) == set(delta['target_tables']), 'Recovery table set differs', 'RECOVERY_MISMATCH')
            require(set(delta['changes']) == tables(db), 'Recovery changes have unexpected tables', 'RECOVERY_MISMATCH')
            db.execute('PRAGMA defer_foreign_keys=ON')
            for name, change in delta['changes'].items():
                columns = [r[1] for r in db.execute('PRAGMA table_info(' + quote(name) + ')')]
                require(columns == change['columns'], 'Recovery columns differ', 'RECOVERY_MISMATCH')
                db.executemany('DELETE FROM ' + quote(name) + ' WHERE rowid=?', [(i,) for i in change['delete_rowids']])
                placeholders = ','.join('?' for _ in range(len(columns) + 1))
                db.executemany('INSERT INTO ' + quote(name) + '(rowid,' + ','.join(map(quote, columns))
                               + ') VALUES(' + placeholders + ')', change['rows'])
            db.execute('PRAGMA user_version=' + str(delta['target_schema']))
            inspect_schema(db)
            require(fingerprint(db) == delta['target_fingerprint'] and not db.execute('PRAGMA foreign_key_check').fetchall(),
                    'Recovered state differs from exported target', 'RECOVERY_MISMATCH')
            db.commit()
        except BaseException:
            db.rollback(); db.close(); output.unlink(missing_ok=True)
            raise
        else:
            db.close()
    return {'path': str(output), 'delta_applied': bool(delta), 'verification': verify(output)}
