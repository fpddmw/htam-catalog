"""Read-only TianGong process/flow sync into a local searchable projection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECTION_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS processes (
  id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL,
  name TEXT NOT NULL, process_type TEXT, reference_exchange_id TEXT,
  state_code INTEGER, modified_at TEXT, source_locator TEXT NOT NULL,
  synced_at TEXT NOT NULL, location TEXT, reference_year TEXT,
  industry_class TEXT, industry_code TEXT, technology_text TEXT,
  projection_version INTEGER NOT NULL DEFAULT 2,
  PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS exchanges (
  process_id TEXT NOT NULL, process_version TEXT NOT NULL, internal_id TEXT NOT NULL,
  direction TEXT NOT NULL, flow_id TEXT NOT NULL, flow_version TEXT,
  mean_amount TEXT, resulting_amount TEXT, is_reference INTEGER NOT NULL,
  PRIMARY KEY (process_id, process_version, internal_id),
  FOREIGN KEY (process_id, process_version) REFERENCES processes(id, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS exchanges_flow ON exchanges(flow_id, direction);
CREATE TABLE IF NOT EXISTS flows (
  id TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL,
  name TEXT NOT NULL, flow_type TEXT, state_code INTEGER, modified_at TEXT,
  source_locator TEXT NOT NULL, synced_at TEXT NOT NULL,
  PRIMARY KEY (id, version)
);
CREATE INDEX IF NOT EXISTS processes_name ON processes(name);
CREATE INDEX IF NOT EXISTS flows_name ON flows(name);
CREATE TABLE IF NOT EXISTS sync_jobs (
  kind TEXT NOT NULL, scope TEXT NOT NULL, page_size INTEGER NOT NULL,
  next_offset INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (kind, scope)
);
CREATE TABLE IF NOT EXISTS ingest_failures (
  kind TEXT NOT NULL, source_locator TEXT NOT NULL, reason TEXT NOT NULL,
  occurred_at TEXT NOT NULL, PRIMARY KEY (kind, source_locator)
);
CREATE TABLE IF NOT EXISTS source_changes (
  kind TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL,
  previous_hash TEXT NOT NULL, current_hash TEXT NOT NULL,
  changed_at TEXT NOT NULL, source_locator TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_changes_identity ON source_changes(kind,id,version);
"""


class IngestError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def obj(value, location):
    if not isinstance(value, dict):
        raise IngestError(f"{location}: expected object")
    return value


def field(value, key, location):
    item = obj(value, location).get(key)
    if not isinstance(item, str) or not item.strip():
        raise IngestError(f"{location}.{key}: missing text")
    return item.strip()


def nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def text_value(value):
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return text_value(value.get("#text"))
    if isinstance(value, list):
        named = [x for x in value if isinstance(x, dict) and x.get("@xml:lang") == "zh"]
        for candidate in named + value:
            answer = text_value(candidate)
            if answer:
                return answer
    return None


def dataset(payload, kind):
    key = "processDataSet" if kind == "process" else "flowDataSet"
    if not isinstance(payload, dict) or not isinstance(payload.get(key), dict):
        raise IngestError(f"{key}: missing TIDAS data set")
    return payload[key]


def identity(data, kind):
    info = "processInformation" if kind == "process" else "flowInformation"
    base = nested(data, info, "dataSetInformation")
    source_id = field(base, "common:UUID", f"{info}.dataSetInformation")
    version = nested(data, "administrativeInformation", "publicationAndOwnership", "common:dataSetVersion")
    if not isinstance(version, str) or not version.strip():
        raise IngestError("common:dataSetVersion: missing text")
    name = text_value(nested(base, "name", "baseName"))
    if not name:
        raise IngestError("name.baseName: missing text")
    return source_id, version.strip(), name


def process_projection(data):
    source_id, version, name = identity(data, "process")
    process_type = nested(data, "modellingAndValidation", "LCIMethodAndAllocation", "typeOfDataSet")
    reference = nested(data, "processInformation", "quantitativeReference", "referenceToReferenceFlow")
    references = [str(item) for item in reference] if isinstance(reference, list) else (
        [str(reference)] if reference is not None else [])
    reference = references[0] if len(references) == 1 else None
    process_info = nested(data, "processInformation")
    location = text_value(nested(process_info, "geography", "locationOfOperationSupplyOrProduction", "@location"))
    year = text_value(nested(process_info, "time", "common:referenceYear"))
    classes = nested(process_info, "dataSetInformation", "classificationInformation",
                     "common:classification", "common:class")
    if isinstance(classes, dict):
        classes = [classes]
    classes = classes if isinstance(classes, list) else []
    leaf = next((item for item in reversed(classes) if text_value(item)), None)
    industry = text_value(leaf)
    industry_code = text_value(leaf.get("@classId")) if isinstance(leaf, dict) else None
    technology = text_value(nested(process_info, "technology", "technologyDescriptionAndIncludedProcesses"))
    raw_exchanges = nested(data, "exchanges", "exchange") or []
    if isinstance(raw_exchanges, dict):
        raw_exchanges = [raw_exchanges]
    if not isinstance(raw_exchanges, list):
        raise IngestError("exchanges.exchange: expected list or object")
    exchanges = []
    seen = set()
    for number, raw in enumerate(raw_exchanges):
        place = f"exchanges.exchange[{number}]"
        internal_id = field(raw, "@dataSetInternalID", place)
        if internal_id in seen:
            raise IngestError(f"{place}: duplicate internal ID {internal_id}")
        seen.add(internal_id)
        direction = field(raw, "exchangeDirection", place)
        if direction not in ("Input", "Output"):
            raise IngestError(f"{place}.exchangeDirection: unsupported {direction}")
        flow_ref = obj(raw.get("referenceToFlowDataSet"), f"{place}.referenceToFlowDataSet")
        flow_id = field(flow_ref, "@refObjectId", f"{place}.referenceToFlowDataSet")
        flow_version = text_value(flow_ref.get("@version"))
        mean_amount = raw.get("meanAmount")
        resulting_amount = raw.get("resultingAmount")
        exchanges.append((source_id, version, internal_id, direction, flow_id,
                          flow_version,
                          str(mean_amount) if mean_amount is not None else None,
                          str(resulting_amount) if resulting_amount is not None else None,
                          int(internal_id in references)))
    facets = (location, year, industry, industry_code, technology)
    return source_id, version, name, text_value(process_type), reference, facets, exchanges


def flow_projection(data):
    source_id, version, name = identity(data, "flow")
    flow_type = nested(data, "modellingAndValidation", "LCIMethod", "typeOfDataSet")
    return source_id, version, name, text_value(flow_type)


def open_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    exchange_columns = {row[1] for row in db.execute("PRAGMA table_info(exchanges)")}
    if "amount" in exchange_columns and "mean_amount" not in exchange_columns:
        db.execute("ALTER TABLE exchanges RENAME COLUMN amount TO mean_amount")
        exchange_columns.add("mean_amount")
    if "resulting_amount" not in exchange_columns:
        db.execute("ALTER TABLE exchanges ADD COLUMN resulting_amount TEXT")
    existing = {row[1] for row in db.execute("PRAGMA table_info(processes)")}
    for column in ("location", "reference_year", "industry_class", "industry_code", "technology_text"):
        if column not in existing:
            db.execute(f"ALTER TABLE processes ADD COLUMN {column} TEXT")
    if "projection_version" not in existing:
        db.execute("ALTER TABLE processes ADD COLUMN projection_version INTEGER NOT NULL DEFAULT 1")
    return db


def ingest_row(db, kind, row, locator):
    """Return inserted/updated/unchanged; reject identity disagreement."""
    if kind not in ("process", "flow"):
        raise IngestError(f"unsupported kind {kind}")
    payload = row.get(kind) if isinstance(row, dict) and kind in row else row
    data = dataset(payload, kind)
    if kind == "process":
        source_id, version, name, data_type, reference, facets, exchanges = process_projection(data)
    else:
        source_id, version, name, data_type = flow_projection(data)
    if isinstance(row, dict):
        for key, expected in (("id", source_id), ("version", version)):
            if key in row and row[key] != expected:
                raise IngestError(f"wrapper {key} disagrees with TIDAS payload")
    content_hash = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
    table = "processes" if kind == "process" else "flows"
    previous = db.execute(f"SELECT content_hash{',projection_version' if kind == 'process' else ''} FROM {table} WHERE id=? AND version=?",
                          (source_id, version)).fetchone()
    same_projection = previous is not None and previous[0] == content_hash and (
        kind == "flow" or previous[1] == PROJECTION_VERSION)
    status = "inserted" if previous is None else "unchanged" if same_projection else "updated"
    state_code = row.get("state_code") if isinstance(row, dict) else None
    modified_at = row.get("modified_at") if isinstance(row, dict) else None
    stamp = now()
    if previous is not None and previous[0] != content_hash:
        db.execute("INSERT INTO source_changes VALUES (?,?,?,?,?,?,?)",
                   (kind, source_id, version, previous[0], content_hash, stamp, locator))
    if kind == "process":
        db.execute("""INSERT INTO processes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id,version) DO UPDATE SET content_hash=excluded.content_hash,
          name=excluded.name, process_type=excluded.process_type,
          reference_exchange_id=excluded.reference_exchange_id, state_code=excluded.state_code,
          modified_at=excluded.modified_at, source_locator=excluded.source_locator,
          synced_at=excluded.synced_at, location=excluded.location,
          reference_year=excluded.reference_year, industry_class=excluded.industry_class,
          industry_code=excluded.industry_code, technology_text=excluded.technology_text,
          projection_version=excluded.projection_version""",
                   (source_id, version, content_hash, name, data_type, reference,
                    state_code, modified_at, locator, stamp, *facets, PROJECTION_VERSION))
        if status != "unchanged":
            db.execute("DELETE FROM exchanges WHERE process_id=? AND process_version=?", (source_id, version))
            db.executemany("INSERT INTO exchanges VALUES (?,?,?,?,?,?,?,?,?)", exchanges)
    else:
        db.execute("""INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id,version) DO UPDATE SET content_hash=excluded.content_hash,
          name=excluded.name, flow_type=excluded.flow_type, state_code=excluded.state_code,
          modified_at=excluded.modified_at, source_locator=excluded.source_locator,
          synced_at=excluded.synced_at""",
                   (source_id, version, content_hash, name, data_type,
                    state_code, modified_at, locator, stamp))
    db.execute("DELETE FROM ingest_failures WHERE kind=? AND source_locator=?", (kind, locator))
    return status


def ingest_page(db, kind, rows, locator):
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
    for index, row in enumerate(rows):
        row_id = row.get("id") if isinstance(row, dict) else None
        row_version = row.get("version") if isinstance(row, dict) else None
        prefix = locator.split(":offset=", 1)[0] if locator.startswith("tiangong:") else locator
        suffix = f"id={row_id}@{row_version}" if row_id and row_version else f"row={index}"
        item_locator = f"{prefix}#{suffix}"
        try:
            with db:
                status = ingest_row(db, kind, row, item_locator)
            counts[status] += 1
        except (IngestError, TypeError, ValueError, sqlite3.Error) as exc:
            counts["failed"] += 1
            with db:
                db.execute("INSERT OR REPLACE INTO ingest_failures VALUES (?,?,?,?)",
                           (kind, item_locator, str(exc), now()))
    return counts


def read_export(path, kind):
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        expected = "listed_remote_processes" if kind == "process" else "listed_remote_flows"
        if payload.get("status") != expected:
            raise IngestError(f"unexpected CLI export status: {payload.get('status')}")
        return payload["rows"]
    if isinstance(payload, dict) and ("processDataSet" in payload or "flowDataSet" in payload):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise IngestError("file must contain a TianGong list response, TIDAS data set, or row array")


def cli_page(cli, kind, state_codes, page_size, offset):
    command = [*cli, kind, "list"]
    for code in state_codes:
        command += ["--state-code", str(code)]
    command += ["--limit", str(page_size), "--offset", str(offset), "--json"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise IngestError(f"TianGong CLI {kind} list failed (exit {result.returncode}); check auth status locally")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IngestError("TianGong CLI returned invalid JSON") from exc
    expected = "listed_remote_processes" if kind == "process" else "listed_remote_flows"
    if not isinstance(payload, dict) or payload.get("status") != expected or not isinstance(payload.get("rows"), list):
        raise IngestError("TianGong CLI list response shape changed")
    return payload["rows"]


def sync_cli(db, kind, cli, state_codes=(100,), page_size=100, resume=False, max_pages=None):
    if kind not in ("process", "flow") or not state_codes or page_size < 1:
        raise IngestError("invalid sync scope or page size")
    scope = ",".join(str(x) for x in sorted(set(state_codes)))
    job = db.execute("SELECT * FROM sync_jobs WHERE kind=? AND scope=?", (kind, scope)).fetchone()
    offset = job["next_offset"] if resume and job else 0
    if resume and job and job["page_size"] != page_size:
        raise IngestError("resume requires the original page size")
    totals = {"inserted": 0, "updated": 0, "unchanged": 0, "failed": 0, "pages": 0}
    while True:
        with db:
            db.execute("INSERT OR REPLACE INTO sync_jobs VALUES (?,?,?,?,?,?)",
                       (kind, scope, page_size, offset, "running", now()))
        try:
            rows = cli_page(cli, kind, state_codes, page_size, offset)
        except (IngestError, subprocess.TimeoutExpired):
            with db:
                db.execute("UPDATE sync_jobs SET status=?,updated_at=? WHERE kind=? AND scope=?",
                           ("failed", now(), kind, scope))
            raise
        counts = ingest_page(db, kind, rows, f"tiangong:{kind}:{scope}:offset={offset}")
        for key, value in counts.items():
            totals[key] += value
        totals["pages"] += 1
        offset += len(rows)
        done = len(rows) < page_size
        with db:
            db.execute("UPDATE sync_jobs SET next_offset=?,status=?,updated_at=? WHERE kind=? AND scope=?",
                       (offset, "complete" if done else "running", now(), kind, scope))
        if done or (max_pages is not None and totals["pages"] >= max_pages):
            break
    totals["next_offset"] = offset
    totals["status"] = "complete" if done else "running"
    return totals


def search(db, term, limit=20):
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return [dict(row) for row in db.execute("""SELECT p.id,p.version,p.name,p.process_type,p.state_code,
      p.location,p.reference_year,p.industry_class,p.industry_code,p.source_locator
      FROM processes p WHERE p.name LIKE ? ESCAPE '\\'
      OR p.industry_class LIKE ? ESCAPE '\\' OR p.industry_code=?
      OR p.technology_text LIKE ? ESCAPE '\\'
      OR EXISTS (SELECT 1 FROM exchanges e LEFT JOIN flows f ON f.id=e.flow_id
        AND f.version=COALESCE(e.flow_version,f.version) WHERE e.process_id=p.id
        AND e.process_version=p.version AND (f.name LIKE ? ESCAPE '\\' OR e.flow_id=?))
      ORDER BY p.name,p.id,p.version LIMIT ?""", (pattern, pattern, term, pattern, pattern, term, limit))]


def matching_connections(db, process_id, version, limit=100):
    """Find shared flow IDs; label knowledge gaps rather than claiming feasibility."""
    rows = db.execute("""SELECT src.internal_id AS output_exchange, src.flow_id,
      src.flow_version AS output_flow_version, dst.flow_version AS input_flow_version,
      f.name AS flow_name, f.flow_type,
      p.id AS receiving_process_id,p.version AS receiving_process_version,
      p.name AS receiving_process_name,dst.internal_id AS input_exchange
      FROM exchanges src JOIN exchanges dst ON src.flow_id=dst.flow_id
      JOIN processes p ON p.id=dst.process_id AND p.version=dst.process_version
      LEFT JOIN flows f ON f.id=src.flow_id AND f.version=src.flow_version
      WHERE src.process_id=? AND src.process_version=? AND src.direction='Output'
      AND dst.direction='Input' AND dst.process_id<>src.process_id
      ORDER BY src.flow_id,p.name,p.id,p.version LIMIT ?""",
                      (process_id, version, limit)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        flow_type = (item["flow_type"] or "").lower()
        if flow_type == "elementary flow":
            item["status"] = "environmental_exchange"
        elif not item["output_flow_version"] or item["output_flow_version"] != item["input_flow_version"]:
            item["status"] = "version_unresolved"
        elif flow_type in ("product flow", "waste flow"):
            item["status"] = "shared_technosphere_flow"
        else:
            item["status"] = "flow_type_unresolved"
        result.append(item)
    return result


def status_report(db):
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("processes", "flows", "exchanges", "ingest_failures", "source_changes")}
    counts["quality"] = {
        "processes_without_reference_exchange": db.execute("""SELECT COUNT(*) FROM processes p
          WHERE NOT EXISTS (SELECT 1 FROM exchanges e WHERE e.process_id=p.id
          AND e.process_version=p.version AND e.is_reference=1)""").fetchone()[0],
        "processes_without_industry_class": db.execute(
            "SELECT COUNT(*) FROM processes WHERE industry_class IS NULL").fetchone()[0],
        "processes_without_technology_text": db.execute(
            "SELECT COUNT(*) FROM processes WHERE technology_text IS NULL").fetchone()[0],
        "exchanges_without_exact_flow_record": db.execute("""SELECT COUNT(*) FROM exchanges e
          WHERE NOT EXISTS (SELECT 1 FROM flows f WHERE f.id=e.flow_id
          AND f.version=e.flow_version)""").fetchone()[0],
        "flows_without_type": db.execute(
            "SELECT COUNT(*) FROM flows WHERE flow_type IS NULL").fetchone()[0],
    }
    counts["jobs"] = [dict(row) for row in db.execute("SELECT * FROM sync_jobs ORDER BY kind,scope")]
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Local SQLite projection path")
    sub = parser.add_subparsers(dest="command", required=True)
    import_parser = sub.add_parser("import-file", help="Import a saved CLI list response or TIDAS data set")
    import_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    import_parser.add_argument("--file", required=True)
    sync_parser = sub.add_parser("sync", help="Page through TianGong CLI read-only list")
    sync_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    sync_parser.add_argument("--cli", nargs="+", default=["tiangong-lca"], help="CLI executable; include node and script path if needed")
    sync_parser.add_argument("--state-code", type=int, action="append", default=None)
    sync_parser.add_argument("--page-size", type=int, default=100)
    sync_parser.add_argument("--resume", action="store_true")
    sync_parser.add_argument("--max-pages", type=int)
    search_parser = sub.add_parser("search", help="Search names and known flow references")
    search_parser.add_argument("term")
    search_parser.add_argument("--limit", type=int, default=20)
    match_parser = sub.add_parser("matches", help="Find exact-flow output-to-input connections")
    match_parser.add_argument("--process-id", required=True)
    match_parser.add_argument("--version", required=True)
    match_parser.add_argument("--limit", type=int, default=100)
    failures_parser = sub.add_parser("failures", help="Inspect failed source records for retry")
    failures_parser.add_argument("--limit", type=int, default=100)
    sub.add_parser("status", help="Show local projection counts and failed records")
    args = parser.parse_args(argv)
    try:
        with open_db(args.db) as db:
            if args.command == "import-file":
                result = ingest_page(db, args.kind, read_export(args.file, args.kind), str(Path(args.file).resolve()))
            elif args.command == "sync":
                result = sync_cli(db, args.kind, args.cli, args.state_code or [100],
                                  args.page_size, args.resume, args.max_pages)
            elif args.command == "search":
                result = search(db, args.term, args.limit)
            elif args.command == "matches":
                result = matching_connections(db, args.process_id, args.version, args.limit)
            elif args.command == "failures":
                result = [dict(row) for row in db.execute("""SELECT kind,source_locator,reason,occurred_at
                  FROM ingest_failures ORDER BY occurred_at DESC LIMIT ?""", (args.limit,))]
            else:
                result = status_report(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (IngestError, OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
