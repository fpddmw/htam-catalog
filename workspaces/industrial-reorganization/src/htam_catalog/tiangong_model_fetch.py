"""Select an exact native lifecycle model from TianGong's supported search."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from .catalog import CatalogError, atomic_json, digest, read_json
from .tiangong_model import inspect_tiangong_model
from .tiangong_sync import IngestError, read_db, remote_json


def search_model(cli, model_id):
    """Use official hybrid search; identity checks happen after the response."""
    if not isinstance(model_id, str) or not model_id.strip():
        raise IngestError("model ID is required")
    with tempfile.TemporaryDirectory(prefix="htam-model-search-") as directory:
        path = Path(directory) / "request.json"
        path.write_text(json.dumps({"query": model_id.strip()}), encoding="utf-8")
        return remote_json([*cli, "search", "lifecyclemodel", "--input", str(path), "--json"],
                           "search lifecyclemodel")


def indexed_process_name(db_path, process_id, process_version):
    """Use only the exact source's existing name as a retrieval query."""
    with read_db(db_path) as db:
        row = db.execute("SELECT name FROM processes WHERE id=? AND version=?",
                         (process_id, process_version)).fetchone()
    if row is None or not isinstance(row["name"], str) or not row["name"].strip():
        raise IngestError(f"exact indexed source process {process_id}@{process_version} not found")
    return row["name"].strip()


def search_for_resulting_process(cli, process_id, process_version, query):
    """Ask official hybrid search for candidates with a native process-ref filter."""
    if not all(isinstance(value, str) and value.strip()
               for value in (process_id, process_version, query)):
        raise IngestError("exact resulting process ID/version and a nonempty query are required")
    request = {
        "query": query.strip(),
        "filter": {"lifeCycleModelDataSet": {"lifeCycleModelInformation": {
            "dataSetInformation": {"referenceToResultingProcess": {
                "@type": "process data set", "@refObjectId": process_id,
                "@version": process_version}}}}},
    }
    with tempfile.TemporaryDirectory(prefix="htam-model-search-") as directory:
        path = Path(directory) / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        return remote_json([*cli, "search", "lifecyclemodel", "--input", str(path), "--json"],
                           "search lifecyclemodel")


def matching_model_candidates(response, process_id, process_version, query):
    """Verify search hits against native refs; never treat empty recall as absence."""
    if isinstance(response, list) and not response:
        rows = []
    elif isinstance(response, dict) and isinstance(response.get("data"), list):
        rows = response["data"]
    else:
        raise IngestError("TianGong lifecyclemodel search response shape changed")
    if not all(isinstance(row, dict) for row in rows):
        raise IngestError("TianGong lifecyclemodel search returned a malformed row")
    counts = [row.get("total_count") for row in rows if row.get("total_count") is not None]
    if any(type(count) is not int or count < len(rows) for count in counts):
        raise IngestError("TianGong lifecyclemodel search count is malformed")
    total = max(counts, default=len(rows))
    candidates = []
    seen = set()
    for row in rows:
        payload = row.get("json")
        if not isinstance(payload, dict):
            raise IngestError("lifecyclemodel search hit has no native JSON object")
        inspected = inspect_tiangong_model(payload)
        model_id, version = inspected["model_id"], inspected["requested_version"]
        if not model_id or not version or row.get("id") != model_id or row.get("version") != version:
            raise IngestError("lifecyclemodel search row and native JSON identity/version disagree")
        matching = [ref for ref in inspected["resulting_process_refs"]
                    if ref["type"] == "process data set" and ref["id"] == process_id and
                    ref["version"] == process_version]
        if not matching:
            raise IngestError("lifecyclemodel search filter returned a different resulting process")
        if (model_id, version) in seen:
            raise IngestError("lifecyclemodel search returned a duplicate exact model version")
        seen.add((model_id, version))
        candidates.append({
            "model_id": model_id, "version": version, "content_hash": digest(payload),
            "association_locator": matching[0]["locator"],
            "structurally_consistent": inspected["structurally_consistent"],
            "diagnostics": inspected["diagnostics"],
        })
    return {
        "resulting_process": {"id": process_id, "version": process_version},
        "query": query,
        "status": "candidates_found" if candidates else "no_candidates_returned",
        "coverage": "partial_page" if total > len(rows) else "hybrid_retrieval_only",
        "returned_count": len(rows), "reported_total_count": total,
        "candidates": candidates,
    }


def select_model(response, model_id, version, process_id=None, process_version=None):
    """Return one exact model record and a small provenance summary."""
    if not isinstance(model_id, str) or not model_id.strip() or not isinstance(version, str) or not version.strip():
        raise IngestError("exact model ID and version are required")
    if (process_id is None) != (process_version is None):
        raise IngestError("process ID and version must be supplied together")
    if process_id is not None and (not isinstance(process_id, str) or not process_id.strip() or
                                   not isinstance(process_version, str) or not process_version.strip()):
        raise IngestError("exact process ID and version are required")
    if isinstance(response, list) and not response:
        rows = []
    elif isinstance(response, dict) and isinstance(response.get("data"), list):
        rows = response["data"]
    else:
        raise IngestError("TianGong lifecyclemodel search response shape changed")
    if not all(isinstance(row, dict) for row in rows):
        raise IngestError("TianGong lifecyclemodel search returned a malformed row")
    counts = [row.get("total_count") for row in rows if row.get("total_count") is not None]
    if counts and any(type(count) is not int or count > len(rows) or count < 0 for count in counts):
        raise IngestError("TianGong lifecyclemodel search result is incomplete or malformed")
    matches = [row for row in rows if row.get("id") == model_id and row.get("version") == version]
    if len(matches) != 1:
        raise IngestError(f"expected one exact lifecyclemodel {model_id}@{version}; found {len(matches)}")
    payload = matches[0].get("json")
    if not isinstance(payload, dict):
        raise IngestError("exact lifecyclemodel row has no native JSON object")
    inspected = inspect_tiangong_model(payload)
    if inspected["model_id"] != model_id or inspected["requested_version"] != version:
        raise IngestError("lifecyclemodel row and native JSON identity/version disagree")
    if not inspected["structurally_consistent"]:
        raise IngestError("native lifecyclemodel has structural diagnostics")
    refs = inspected["resulting_process_refs"]
    association = None
    if process_id is not None:
        association = next((ref for ref in refs if ref["type"] == "process data set" and
                            ref["id"] == process_id and ref["version"] == process_version), None)
        if association is None:
            raise IngestError("native lifecyclemodel does not reference the exact resulting process")
    return payload, {
        "model_id": model_id, "version": version, "content_hash": digest(payload),
        "resulting_process_refs": refs,
        "exact_resulting_process_reference": association,
        "instance_count": len(inspected["instances"]),
        "connection_count": len(inspected["connections"]),
    }


def save_selected(path, payload):
    destination = Path(path)
    if destination.exists():
        if digest(read_json(destination)) != digest(payload):
            raise IngestError(f"existing model file differs: {destination}")
        return "unchanged"
    atomic_json(destination, payload)
    return "saved"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="Exact lifecyclemodel ID for retrieval")
    parser.add_argument("--version", help="Exact lifecyclemodel version for retrieval")
    parser.add_argument("--process-id", help="Require native link to this resulting process")
    parser.add_argument("--process-version", help="Required with --process-id")
    parser.add_argument("--find-for-process", action="store_true",
                        help="Find model candidates for a resulting process; hybrid retrieval is not exhaustive")
    parser.add_argument("--db", help="Existing source index, used only to read the exact process name")
    parser.add_argument("--query", help="Process description to search instead of --db")
    parser.add_argument("--search-file", help="Previously captured official search response JSON")
    parser.add_argument("--out", help="Save only the selected native model JSON")
    parser.add_argument("--cli", nargs="+", default=["tiangong-lca"], help="Official CLI command")
    args = parser.parse_args(argv)
    try:
        if args.find_for_process:
            if args.id or args.version or args.out or args.search_file or bool(args.db) == bool(args.query):
                raise IngestError("discovery needs --process-id, --process-version and exactly one of --db or --query; no model ID/output file")
            if not args.process_id or not args.process_version:
                raise IngestError("discovery needs exact resulting process ID and version")
            query = (indexed_process_name(args.db, args.process_id, args.process_version)
                     if args.db else args.query.strip())
            response = search_for_resulting_process(args.cli, args.process_id,
                                                    args.process_version, query)
            print(json.dumps(matching_model_candidates(response, args.process_id,
                                                       args.process_version, query),
                             ensure_ascii=False, indent=2))
            return 0
        if not args.id or not args.version or args.db or args.query:
            raise IngestError("exact retrieval needs --id and --version; --db/--query are for --find-for-process")
        response = read_json(args.search_file) if args.search_file else search_model(args.cli, args.id)
        payload, summary = select_model(response, args.id, args.version,
                                        args.process_id, args.process_version)
        if args.out:
            summary["file_status"] = save_selected(args.out, payload)
            summary["saved_to"] = str(Path(args.out).resolve())
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (IngestError, CatalogError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "unresolved", "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
