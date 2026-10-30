"""Explore TianGong processes and flows and maintain a local source index."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile


PROJECTION_VERSION = 3

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
CREATE TABLE IF NOT EXISTS flow_lookup_queue (
  flow_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_issues (
  kind TEXT NOT NULL, source_id TEXT NOT NULL, version TEXT NOT NULL,
  field_path TEXT NOT NULL, reason TEXT NOT NULL, observed_at TEXT NOT NULL,
  PRIMARY KEY (kind, source_id, version, field_path)
);
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
    raw_year = nested(process_info, "time", "common:referenceYear")
    year = str(raw_year) if type(raw_year) is int else text_value(raw_year)
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
    issues = []
    seen = set()
    for number, raw in enumerate(raw_exchanges):
        place = f"exchanges.exchange[{number}]"
        try:
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
        except (IngestError, AttributeError) as exc:
            issues.append(("process", source_id, version, place, str(exc), now()))
    facets = (location, year, industry, industry_code, technology)
    return source_id, version, name, text_value(process_type), reference, facets, exchanges, issues


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
    db.execute("CREATE INDEX IF NOT EXISTS processes_industry ON processes(industry_code)")
    db.execute("CREATE INDEX IF NOT EXISTS processes_location ON processes(location)")
    db.execute("CREATE INDEX IF NOT EXISTS processes_type ON processes(process_type)")
    return db


@contextmanager
def read_db(path):
    """Open an existing source index without creating or migrating it."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"local source index does not exist: {path}")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def ingest_row(db, kind, row, locator):
    """Return inserted/updated/unchanged; reject identity disagreement."""
    if kind not in ("process", "flow"):
        raise IngestError(f"unsupported kind {kind}")
    payload = row.get(kind) if isinstance(row, dict) and kind in row else row
    data = dataset(payload, kind)
    if kind == "process":
        source_id, version, name, data_type, reference, facets, exchanges, issues = process_projection(data)
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
            db.execute("DELETE FROM projection_issues WHERE kind='process' AND source_id=? AND version=?",
                       (source_id, version))
            db.executemany("INSERT INTO projection_issues VALUES (?,?,?,?,?,?)", issues)
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
    if isinstance(payload, dict) and payload.get("status") == f"resolved_remote_{kind}":
        return [{"id": payload.get(f"{kind}_id"), "version": payload.get("resolved_version"),
                 "state_code": payload.get("state_code"), "modified_at": payload.get("modified_at"),
                 kind: payload.get(kind)}]
    if isinstance(payload, dict) and ("processDataSet" in payload or "flowDataSet" in payload):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise IngestError("file must contain a TianGong list/get response, TIDAS data set, or row array")


def cli_page(cli, kind, state_codes, page_size, offset):
    command = [*cli, kind, "list"]
    for code in state_codes:
        command += ["--state-code", str(code)]
    command += ["--limit", str(page_size), "--offset", str(offset), "--json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"TianGong CLI {kind} list timed out") from exc
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


def cli_ids(cli, kind, ids, page_size=100):
    if kind not in ("process", "flow") or not ids:
        raise IngestError("ID lookup requires a source kind and at least one ID")
    command = [*cli, kind, "list"]
    for source_id in ids:
        command += ["--id", source_id]
    command += ["--state-code", "100", "--all", "--page-size", str(page_size), "--json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"TianGong CLI {kind} list by ID timed out") from exc
    if result.returncode:
        raise IngestError(f"TianGong CLI {kind} list by ID failed (exit {result.returncode})")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IngestError(f"TianGong CLI returned invalid {kind}-list JSON") from exc
    expected = "listed_remote_processes" if kind == "process" else "listed_remote_flows"
    if not isinstance(payload, dict) or payload.get("status") != expected or not isinstance(payload.get("rows"), list):
        raise IngestError(f"TianGong CLI {kind}-list response shape changed")
    return payload["rows"]


def cli_flow_ids(cli, ids, page_size=100):
    return cli_ids(cli, "flow", ids, page_size)


def remote_json(command, label):
    """Run the official CLI without exposing its authentication to this project."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise IngestError(f"TianGong CLI {label} timed out") from exc
    except OSError as exc:
        raise IngestError(f"TianGong CLI {label} could not start: {exc}") from exc
    if result.returncode:
        raise IngestError(f"TianGong CLI {label} failed (exit {result.returncode}); check auth status locally")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IngestError(f"TianGong CLI {label} returned invalid JSON") from exc


def remote_search(cli, kind, query, filter_value=None):
    """Search remotely; only the selected IDs need ever enter the local index."""
    if kind not in ("process", "flow") or not isinstance(query, str) or not query.strip():
        raise IngestError("remote search requires process/flow and a nonempty query")
    request = {"query": query.strip()}
    if filter_value is not None:
        if not isinstance(filter_value, (dict, str)):
            raise IngestError("search filter must be a JSON object or string")
        request["filter"] = filter_value
    with tempfile.TemporaryDirectory(prefix="htam-search-") as directory:
        path = Path(directory) / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        payload = remote_json([*cli, "search", kind, "--input", str(path), "--json"],
                              f"search {kind}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise IngestError(f"TianGong CLI search {kind} response shape changed")
    return {"kind": kind, "query": request["query"], "filter": filter_value,
            "count": len(payload["data"]), "data": payload["data"]}


def search_hit_summaries(kind, hits):
    """Show enough identity to pick saved search hits without opening their full JSON."""
    summaries = []
    for position, hit in enumerate(hits, 1):
        name = None
        if isinstance(hit, dict):
            try:
                _, _, name = identity(dataset(hit.get("json"), kind), kind)
            except IngestError:
                pass
        summaries.append({"position": position,
                          "id": hit.get("id") if isinstance(hit, dict) else None,
                          "version": hit.get("version") if isinstance(hit, dict) else None,
                          "name": name})
    return summaries


def remote_get(cli, kind, source_id, version=None):
    """Read one remote record and reject the official CLI's latest-version fallback."""
    if kind not in ("process", "flow") or not isinstance(source_id, str) or not source_id.strip():
        raise IngestError("remote get requires process/flow and a nonempty ID")
    if version is not None and (not isinstance(version, str) or not version.strip()):
        raise IngestError("requested version must be nonempty text")
    source_id = source_id.strip()
    version = version.strip() if version else None
    command = [*cli, kind, "get", "--id", source_id]
    if version:
        command += ["--version", version]
    payload = remote_json([*command, "--json"], f"{kind} get")
    expected_status = f"resolved_remote_{kind}"
    if not isinstance(payload, dict) or payload.get("status") != expected_status:
        raise IngestError(f"TianGong CLI {kind} get response shape changed")
    resolved_id = payload.get(f"{kind}_id")
    resolved_version = payload.get("resolved_version")
    if resolved_id != source_id or not isinstance(resolved_version, str) or not resolved_version:
        raise IngestError(f"TianGong CLI {kind} get returned a different or incomplete identity")
    if version and resolved_version != version:
        raise IngestError(f"requested {kind} {source_id}@{version} resolved as {resolved_version}")
    data = dataset(payload.get(kind), kind)
    embedded_id, embedded_version, _ = identity(data, kind)
    if (embedded_id, embedded_version) != (resolved_id, resolved_version):
        raise IngestError(f"TianGong CLI {kind} get metadata disagrees with TIDAS payload")
    return payload


def selected_refs(ids=(), version=None, refs_file=None):
    """Normalize explicit selections from repeated IDs or a JSON reference list."""
    if version and (len(ids) != 1 or refs_file):
        raise IngestError("--version requires exactly one --id and no --refs-file")
    refs = [{"id": source_id, "version": version} for source_id in ids]
    if refs_file:
        loaded = json.loads(Path(refs_file).read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, list):
            raise IngestError("references file must be a JSON array")
        for item in loaded:
            if isinstance(item, str):
                refs.append({"id": item, "version": None})
            elif isinstance(item, dict) and set(item) <= {"id", "version"}:
                refs.append({"id": item.get("id"), "version": item.get("version")})
            else:
                raise IngestError("each reference must be an ID or an {id, version} object")
    if not refs:
        raise IngestError("provide at least one --id or --refs-file entry")
    return normalize_refs(refs)


def refs_from_search(path, positions, kind):
    """Turn explicitly picked search results into exact source references."""
    if not positions:
        raise IngestError("--search-file requires at least one --pick position")
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("kind") != kind or not isinstance(payload.get("data"), list):
        raise IngestError("search file must be a matching HTAM remote-search result")
    refs = []
    for position in positions:
        if position < 1 or position > len(payload["data"]):
            raise IngestError(f"search result position {position} is out of range")
        hit = payload["data"][position - 1]
        if not isinstance(hit, dict):
            raise IngestError(f"search result {position} is not an object")
        if not isinstance(hit.get("version"), str) or not hit["version"].strip():
            raise IngestError(f"search result {position} has no exact version")
        refs.append({"id": hit.get("id"), "version": hit.get("version")})
    return normalize_refs(refs)


def normalize_refs(refs):
    """Validate and deduplicate source references while preserving order."""
    unique = []
    seen = set()
    for ref in refs:
        source_id, requested_version = ref["id"], ref["version"]
        if not isinstance(source_id, str) or not source_id.strip():
            raise IngestError("reference ID must be nonempty text")
        if requested_version is not None and (not isinstance(requested_version, str) or not requested_version.strip()):
            raise IngestError("reference version must be nonempty text when supplied")
        pair = (source_id.strip(), requested_version.strip() if requested_version else None)
        if pair not in seen:
            unique.append({"id": pair[0], "version": pair[1]})
            seen.add(pair)
    return unique


def add_selected(db, kind, refs, cli):
    """Fetch only explicitly selected records and upsert their local projections."""
    if kind not in ("process", "flow"):
        raise IngestError("selected import requires process or flow")
    result = {"kind": kind, "requested": len(refs), "inserted": 0, "updated": 0,
              "unchanged": 0, "failed": 0, "items": []}
    for ref in refs:
        item = {"id": ref["id"], "requested_version": ref["version"]}
        try:
            payload = remote_get(cli, kind, ref["id"], ref["version"])
            resolved_version = payload["resolved_version"]
            row = {"id": ref["id"], "version": resolved_version,
                   "state_code": payload.get("state_code"),
                   "modified_at": payload.get("modified_at"), kind: payload[kind]}
            locator = f"tiangong:{kind}:get#id={ref['id']}@{resolved_version}"
            with db:
                status = ingest_row(db, kind, row, locator)
            item.update({"resolved_version": resolved_version, "status": status})
        except (IngestError, TypeError, ValueError, sqlite3.Error) as exc:
            status = "failed"
            item.update({"status": status, "reason": str(exc)})
        result[status] += 1
        result["items"].append(item)
    return result


def sync_selected_flow_references(db, cli, process_id, version=None):
    """Fetch missing flows referenced by one locally indexed process."""
    if not isinstance(process_id, str) or not process_id.strip():
        raise IngestError("--process-id must be nonempty text")
    process_id = process_id.strip()
    exists = db.execute("SELECT 1 FROM processes WHERE id=? AND (? IS NULL OR version=?) LIMIT 1",
                        (process_id, version, version)).fetchone()
    if not exists:
        raise IngestError("selected process is not present in the local source index")
    rows = db.execute("""SELECT DISTINCT flow_id,flow_version FROM exchanges
      WHERE process_id=? AND (? IS NULL OR process_version=?)
      ORDER BY flow_id,flow_version""", (process_id, version, version)).fetchall()
    refs = []
    for flow_id, flow_version in rows:
        known = db.execute("SELECT 1 FROM flows WHERE id=? AND (? IS NULL OR version=?) LIMIT 1",
                           (flow_id, flow_version, flow_version)).fetchone()
        if not known:
            refs.append({"id": flow_id, "version": flow_version})
    result = add_selected(db, "flow", refs, cli)
    result.update({"process_id": process_id, "process_version": version,
                   "referenced": len(rows), "already_indexed": len(rows) - len(refs)})
    return result


def retry_failed_rows(db, kind, cli, batch_size=40, max_batches=None):
    if kind not in ("process", "flow") or batch_size < 1:
        raise IngestError("invalid failure retry kind or batch size")
    locators = [row[0] for row in db.execute(
        "SELECT source_locator FROM ingest_failures WHERE kind=? ORDER BY source_locator", (kind,))]
    ids = sorted({match.group(1) for locator in locators
                  if (match := re.search(r"#id=([^@#]+)@", locator))})
    totals = {"requested_ids": 0, "inserted": 0, "updated": 0, "unchanged": 0, "failed": 0}
    for start in range(0, len(ids), batch_size):
        if max_batches is not None and start // batch_size >= max_batches:
            break
        batch = ids[start:start + batch_size]
        rows = cli_ids(cli, kind, batch)
        counts = ingest_page(db, kind, rows, f"tiangong:{kind}:100")
        totals["requested_ids"] += len(batch)
        for key, value in counts.items():
            totals[key] += value
    totals["remaining_failures"] = db.execute(
        "SELECT COUNT(*) FROM ingest_failures WHERE kind=?", (kind,)).fetchone()[0]
    return totals


def sync_flow_references(db, cli, batch_size=40, max_batches=None, retry_unresolved=False):
    if batch_size < 1 or max_batches is not None and max_batches < 1:
        raise IngestError("invalid reference-sync batch size or limit")
    with db:
        db.execute("""DELETE FROM flow_lookup_queue WHERE flow_id NOT IN
          (SELECT DISTINCT flow_id FROM exchanges)""")
        db.execute("""INSERT OR IGNORE INTO flow_lookup_queue(flow_id,status,updated_at)
          SELECT DISTINCT e.flow_id,'pending',? FROM exchanges e
          WHERE NOT EXISTS (SELECT 1 FROM flows f WHERE f.id=e.flow_id
          AND f.version=e.flow_version)""", (now(),))
        if retry_unresolved:
            db.execute("UPDATE flow_lookup_queue SET status='pending',updated_at=? WHERE status<>'pending'", (now(),))
    totals = {"batches": 0, "requested_ids": 0, "inserted": 0,
              "updated": 0, "unchanged": 0, "failed": 0}
    while True:
        ids = [row[0] for row in db.execute("""SELECT flow_id FROM flow_lookup_queue
          WHERE status='pending' ORDER BY flow_id LIMIT ?""", (batch_size,))]
        if not ids or max_batches is not None and totals["batches"] >= max_batches:
            break
        rows = cli_flow_ids(cli, ids)
        counts = ingest_page(db, "flow", rows, "tiangong:flow:references")
        with db:
            for flow_id in ids:
                found = db.execute("SELECT 1 FROM flows WHERE id=? LIMIT 1", (flow_id,)).fetchone()
                db.execute("UPDATE flow_lookup_queue SET status=?,updated_at=? WHERE flow_id=?",
                           ("found" if found else "not_found_or_invalid", now(), flow_id))
        totals["batches"] += 1
        totals["requested_ids"] += len(ids)
        for key, value in counts.items():
            totals[key] += value
    totals["pending_ids"] = db.execute(
        "SELECT COUNT(*) FROM flow_lookup_queue WHERE status='pending'").fetchone()[0]
    return totals


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
        except IngestError:
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


def search(db, term, limit=20, offset=0):
    if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
        raise IngestError("search limit must be positive and offset non-negative")
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return [dict(row) for row in db.execute("""SELECT p.id,p.version,p.name,p.process_type,p.state_code,
      p.location,p.reference_year,p.industry_class,p.industry_code,p.source_locator
      FROM processes p WHERE p.name LIKE ? ESCAPE '\\'
      OR p.industry_class LIKE ? ESCAPE '\\' OR p.industry_code=?
      OR p.technology_text LIKE ? ESCAPE '\\'
      OR EXISTS (SELECT 1 FROM exchanges e LEFT JOIN flows f ON f.id=e.flow_id
        AND f.version=COALESCE(e.flow_version,f.version) WHERE e.process_id=p.id
        AND e.process_version=p.version AND (f.name LIKE ? ESCAPE '\\' OR e.flow_id=?))
      ORDER BY p.name,p.id,p.version LIMIT ? OFFSET ?""",
      (pattern, pattern, term, pattern, pattern, term, limit, offset))]


def query_processes(db, *, name=None, industry_code=None, location=None, process_type=None,
                    reference_year=None, input_flow=None, output_flow=None,
                    input_flow_version=None, output_flow_version=None, limit=20, offset=0):
    if limit < 1 or offset < 0:
        raise IngestError("query limit must be positive and offset non-negative")
    clauses, values = [], []
    exact = {"industry_code": industry_code, "location": location,
             "process_type": process_type, "reference_year": reference_year}
    for column, value in exact.items():
        if value is not None:
            clauses.append(f"p.{column}=?")
            values.append(value)
    if name is not None:
        clauses.append("p.name LIKE ? ESCAPE '\\'")
        values.append("%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    for direction, flow_id, flow_version in (("Input", input_flow, input_flow_version),
                                             ("Output", output_flow, output_flow_version)):
        if flow_version is not None and (not isinstance(flow_version, str) or not flow_version.strip()
                                         or not flow_id):
            raise IngestError(f"{direction.lower()} flow version requires a flow ID and nonempty version")
        if flow_id is not None:
            clause = """EXISTS (SELECT 1 FROM exchanges e WHERE e.process_id=p.id
              AND e.process_version=p.version AND e.direction=? AND e.flow_id=?"""
            values += [direction, flow_id]
            if flow_version is not None:
                clause += " AND e.flow_version=?"
                values.append(flow_version)
            clauses.append(clause + ")")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = """SELECT p.id,p.version,p.name,p.process_type,p.industry_class,p.industry_code,
      p.location,p.reference_year,p.source_locator,
      EXISTS(SELECT 1 FROM projection_issues i WHERE i.kind='process'
        AND i.source_id=p.id AND i.version=p.version) AS has_projection_issues
      FROM processes p""" + where + " ORDER BY p.name,p.id,p.version LIMIT ? OFFSET ?"
    return [dict(row) for row in db.execute(sql, (*values, limit, offset))]


def source_detail(db, kind, source_id, version):
    """Read one indexed source and its projected facts without interpreting it as a module."""
    if kind not in ("process", "flow") or not source_id or not version:
        raise IngestError("source detail requires kind, ID and exact version")
    table = "processes" if kind == "process" else "flows"
    row = db.execute(f"SELECT * FROM {table} WHERE id=? AND version=?",
                     (source_id, version)).fetchone()
    if row is None:
        raise IngestError(f"indexed {kind} {source_id}@{version} not found")
    result = {"kind": kind, "id": source_id, "version": version, kind: dict(row)}
    if kind == "process":
        result["exchanges"] = [dict(item) for item in db.execute("""SELECT
          e.internal_id,e.direction,e.flow_id,e.flow_version,e.mean_amount,
          e.resulting_amount,e.is_reference,f.name AS flow_name,f.flow_type,
          f.content_hash AS flow_content_hash
          FROM exchanges e LEFT JOIN flows f ON f.id=e.flow_id AND f.version=e.flow_version
          WHERE e.process_id=? AND e.process_version=? ORDER BY e.internal_id""",
          (source_id, version))]
        result["issues"] = [dict(item) for item in db.execute("""SELECT field_path,reason,observed_at
          FROM projection_issues WHERE kind='process' AND source_id=? AND version=?
          ORDER BY field_path""", (source_id, version))]
    return result


def source_changes(db, kind=None, source_id=None, version=None, limit=100):
    """Read observed content-hash changes without inferring module impact."""
    if kind is not None and kind not in ("process", "flow"):
        raise IngestError("change kind must be process or flow")
    if type(limit) is not int or limit < 1:
        raise IngestError("limit must be a positive integer")
    clauses, values = [], []
    for field, value in (("kind", kind), ("id", source_id), ("version", version)):
        if value is not None:
            clauses.append(f"{field}=?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [dict(row) for row in db.execute("""SELECT kind,id,version,previous_hash,
      current_hash,changed_at,source_locator FROM source_changes""" + where +
      " ORDER BY changed_at DESC,rowid DESC LIMIT ?", (*values, limit))]


def matching_connections(db, process_id, version, limit=100, offset=0):
    """Find shared flow IDs; label knowledge gaps rather than claiming feasibility."""
    if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
        raise IngestError("match limit must be positive and offset non-negative")
    if db.execute("SELECT 1 FROM processes WHERE id=? AND version=?", (process_id, version)).fetchone() is None:
        raise IngestError(f"indexed process {process_id}@{version} not found")
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
      ORDER BY src.flow_id,p.name,p.id,p.version,src.internal_id,dst.internal_id
      LIMIT ? OFFSET ?""",
                      (process_id, version, limit, offset)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        flow_type = (item["flow_type"] or "").lower()
        if flow_type == "elementary flow":
            item["status"] = "environmental_exchange"
        elif (item["output_flow_version"] and item["input_flow_version"] and
              item["output_flow_version"] != item["input_flow_version"]):
            item["status"] = "version_conflict"
        elif not item["output_flow_version"] or not item["input_flow_version"]:
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
        "skipped_exchange_items": db.execute(
            "SELECT COUNT(*) FROM projection_issues WHERE kind='process'").fetchone()[0],
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
    unresolved = {"unversioned_flow_reference": 0, "flow_id_absent": 0,
                  "requested_flow_version_absent": 0}
    for category, number in db.execute("""SELECT category,COUNT(*) FROM (
      SELECT CASE WHEN e.flow_version IS NULL THEN 'unversioned_flow_reference'
        WHEN NOT EXISTS (SELECT 1 FROM flows f WHERE f.id=e.flow_id) THEN 'flow_id_absent'
        ELSE 'requested_flow_version_absent' END AS category
      FROM exchanges e WHERE NOT EXISTS (SELECT 1 FROM flows f WHERE f.id=e.flow_id
        AND f.version=e.flow_version)) GROUP BY category"""):
        unresolved[category] = number
    counts["quality"].update(unresolved)
    counts["jobs"] = [dict(row) for row in db.execute("SELECT * FROM sync_jobs ORDER BY kind,scope")]
    counts["flow_lookup_queue"] = {row[0]: row[1] for row in db.execute(
        "SELECT status,COUNT(*) FROM flow_lookup_queue GROUP BY status ORDER BY status")}
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Local SQLite projection path; unnecessary for remote read-only commands")
    sub = parser.add_subparsers(dest="command", required=True)
    remote_search_parser = sub.add_parser("remote-search", help="Search TianGong without changing the local index")
    remote_search_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    remote_search_parser.add_argument("--query", required=True)
    remote_search_parser.add_argument("--filter-file", help="Optional JSON object or string for the TianGong search filter")
    remote_search_parser.add_argument("--out", help="Save the full UTF-8 search result here; print only its summary")
    remote_search_parser.add_argument("--cli", nargs="+", default=["tiangong-lca"])
    remote_get_parser = sub.add_parser("remote-get", help="Read one exact TianGong process or flow without local writes")
    remote_get_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    remote_get_parser.add_argument("--id", required=True)
    remote_get_parser.add_argument("--version", help="Require this exact version; reject fallback to latest")
    remote_get_parser.add_argument("--cli", nargs="+", default=["tiangong-lca"])
    selected_parser = sub.add_parser("add-ids", help="Add only selected remote IDs to the local source index")
    selected_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    selected_parser.add_argument("--id", action="append", default=[], help="Repeat for multiple latest reachable records")
    selected_parser.add_argument("--version", help="Exact version when one --id is given")
    selected_parser.add_argument("--refs-file", help="JSON array of IDs or {id, version} references")
    selected_parser.add_argument("--search-file", help="Saved HTAM remote-search result; requires --pick")
    selected_parser.add_argument("--pick", type=int, action="append", default=[], help="One-based search result position; repeat to select more")
    selected_parser.add_argument("--cli", nargs="+", default=["tiangong-lca"])
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
    refs_parser = sub.add_parser("sync-flow-refs", help="Fetch only flows referenced by local processes")
    refs_parser.add_argument("--cli", nargs="+", default=["tiangong-lca"])
    refs_parser.add_argument("--process-id", help="Limit lookup to one indexed process")
    refs_parser.add_argument("--process-version", help="Require this version of --process-id")
    refs_parser.add_argument("--batch-size", type=int, default=40)
    refs_parser.add_argument("--max-batches", type=int)
    refs_parser.add_argument("--retry-unresolved", action="store_true")
    retry_parser = sub.add_parser("retry-failures", help="Refetch failed source IDs")
    retry_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    retry_parser.add_argument("--cli", nargs="+", default=["tiangong-lca"])
    retry_parser.add_argument("--batch-size", type=int, default=40)
    retry_parser.add_argument("--max-batches", type=int)
    search_parser = sub.add_parser("search", help="Search names and known flow references")
    search_parser.add_argument("term")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--offset", type=int, default=0)
    query_parser = sub.add_parser("query", help="Filter processes by source fields and exact flow IDs")
    query_parser.add_argument("--name")
    query_parser.add_argument("--industry-code")
    query_parser.add_argument("--location")
    query_parser.add_argument("--process-type")
    query_parser.add_argument("--reference-year")
    query_parser.add_argument("--input-flow")
    query_parser.add_argument("--input-flow-version")
    query_parser.add_argument("--output-flow")
    query_parser.add_argument("--output-flow-version")
    query_parser.add_argument("--limit", type=int, default=20)
    query_parser.add_argument("--offset", type=int, default=0)
    detail_parser = sub.add_parser("source-detail", help="Read one locally indexed source and its projected exchanges")
    detail_parser.add_argument("--kind", choices=("process", "flow"), required=True)
    detail_parser.add_argument("--id", required=True)
    detail_parser.add_argument("--version", required=True)
    changes_parser = sub.add_parser("changes", help="Read observed process/flow content changes from the local index")
    changes_parser.add_argument("--kind", choices=("process", "flow"))
    changes_parser.add_argument("--id")
    changes_parser.add_argument("--version")
    changes_parser.add_argument("--limit", type=int, default=100)
    match_parser = sub.add_parser("matches", help="Find exact-flow output-to-input connections")
    match_parser.add_argument("--process-id", required=True)
    match_parser.add_argument("--version", required=True)
    match_parser.add_argument("--limit", type=int, default=100)
    match_parser.add_argument("--offset", type=int, default=0)
    failures_parser = sub.add_parser("failures", help="Inspect failed source records for retry")
    failures_parser.add_argument("--limit", type=int, default=100)
    issues_parser = sub.add_parser("issues", help="Inspect skipped source fields")
    issues_parser.add_argument("--limit", type=int, default=100)
    sub.add_parser("status", help="Show local projection counts and failed records")
    args = parser.parse_args(argv)
    try:
        if args.command == "remote-search":
            filter_value = None
            if args.filter_file:
                filter_value = json.loads(Path(args.filter_file).read_text(encoding="utf-8-sig"))
            result = remote_search(args.cli, args.kind, args.query, filter_value)
            if args.out:
                path = Path(args.out)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                result = {"kind": args.kind, "query": result["query"],
                          "count": result["count"], "saved_to": str(path.resolve()),
                          "hits": search_hit_summaries(args.kind, result["data"])}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "remote-get":
            result = remote_get(args.cli, args.kind, args.id, args.version)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not args.db:
            raise IngestError("--db is required for local index commands")
        if args.command in ("source-detail", "changes", "search", "query", "matches",
                            "failures", "issues", "status"):
            with read_db(args.db) as db:
                if args.command == "source-detail":
                    result = source_detail(db, args.kind, args.id, args.version)
                elif args.command == "changes":
                    result = source_changes(db, args.kind, args.id, args.version, args.limit)
                elif args.command == "search":
                    result = search(db, args.term, args.limit, args.offset)
                elif args.command == "query":
                    result = query_processes(db, name=args.name, industry_code=args.industry_code,
                                             location=args.location, process_type=args.process_type,
                                             reference_year=args.reference_year, input_flow=args.input_flow,
                                             output_flow=args.output_flow,
                                             input_flow_version=args.input_flow_version,
                                             output_flow_version=args.output_flow_version,
                                             limit=args.limit, offset=args.offset)
                elif args.command == "matches":
                    result = matching_connections(db, args.process_id, args.version, args.limit, args.offset)
                elif args.command == "failures":
                    result = [dict(row) for row in db.execute("""SELECT kind,source_locator,reason,occurred_at
                      FROM ingest_failures ORDER BY occurred_at DESC LIMIT ?""", (args.limit,))]
                elif args.command == "issues":
                    result = [dict(row) for row in db.execute("""SELECT kind,source_id,version,field_path,reason,observed_at
                      FROM projection_issues ORDER BY observed_at DESC LIMIT ?""", (args.limit,))]
                else:
                    result = status_report(db)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "sync-flow-refs" and args.process_version and not args.process_id:
            raise IngestError("--process-version requires --process-id")
        if args.command == "sync-flow-refs" and args.process_id and (
            args.batch_size != 40 or args.max_batches is not None or args.retry_unresolved
        ):
            raise IngestError("batch queue options cannot be combined with --process-id")
        refs = None
        if args.command == "add-ids":
            if args.search_file:
                if args.id or args.refs_file or args.version:
                    raise IngestError("--search-file cannot be combined with --id, --version or --refs-file")
                refs = refs_from_search(args.search_file, args.pick, args.kind)
            else:
                if args.pick:
                    raise IngestError("--pick requires --search-file")
                refs = selected_refs(args.id, args.version, args.refs_file)
        with closing(open_db(args.db)) as db, db:
            if args.command == "add-ids":
                result = add_selected(db, args.kind, refs, args.cli)
            elif args.command == "import-file":
                result = ingest_page(db, args.kind, read_export(args.file, args.kind), str(Path(args.file).resolve()))
            elif args.command == "sync":
                result = sync_cli(db, args.kind, args.cli, args.state_code or [100],
                                  args.page_size, args.resume, args.max_pages)
            elif args.command == "sync-flow-refs":
                if args.process_id:
                    result = sync_selected_flow_references(db, args.cli, args.process_id,
                                                           args.process_version)
                else:
                    result = sync_flow_references(db, args.cli, args.batch_size,
                                                  args.max_batches, args.retry_unresolved)
            elif args.command == "retry-failures":
                result = retry_failed_rows(db, args.kind, args.cli,
                                           args.batch_size, args.max_batches)
            else:
                raise IngestError(f"unsupported local write command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        selected_write = args.command == "add-ids" or (
            args.command == "sync-flow-refs" and args.process_id)
        return 1 if selected_write and result["failed"] else 0
    except (IngestError, OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
