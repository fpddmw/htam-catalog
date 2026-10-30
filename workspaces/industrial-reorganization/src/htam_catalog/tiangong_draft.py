"""Preview how an indexed source and an explicit native model could be curated."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .catalog import CatalogError
from .tiangong_cards import projected_card
from .tiangong_model import inspect_tiangong_model
from .tiangong_sync import IngestError, read_db


def _indexed_process(db, process_id, version):
    if not process_id or not version:
        return None
    return db.execute("SELECT * FROM processes WHERE id=? AND version=?",
                      (process_id, version)).fetchone()


def _port_candidates(db, instance, direction, flow_id, flow_version):
    if not instance or not instance["indexed"] or not flow_id:
        return []
    rows = db.execute(
        """SELECT internal_id,flow_version,is_reference FROM exchanges
           WHERE process_id=? AND process_version=? AND direction=? AND flow_id=?
           ORDER BY internal_id""",
        (instance["process_id"], instance["requested_version"], direction, flow_id))
    candidates = []
    for row in rows:
        actual_version = row["flow_version"]
        match = ("exact" if flow_version and actual_version == flow_version else
                 "conflict" if flow_version and actual_version else "version_unresolved")
        candidates.append({"exchange_internal_id": row["internal_id"],
                           "flow_version": actual_version, "version_match": match,
                           "is_reference": bool(row["is_reference"])})
    return candidates


def _connection_status(edge, upstream, downstream, output_candidates, input_candidates):
    if not edge["from_flow_id"] or edge["from_flow_id"] != edge["to_flow_id"]:
        return "model_flow_identity_conflict"
    if edge["flow_version_match"] == "conflict":
        return "model_flow_version_conflict"
    if not upstream or not downstream:
        return "instance_identity_unresolved"
    if not upstream["indexed"] or not downstream["indexed"]:
        return "process_not_indexed"
    if len(output_candidates) > 1 or len(input_candidates) > 1:
        return "ambiguous_exchange"
    if not output_candidates or not input_candidates:
        return "exchange_unresolved"
    if (output_candidates[0]["version_match"] == "conflict"
            or input_candidates[0]["version_match"] == "conflict"):
        return "exchange_flow_version_conflict"
    if (edge["flow_version_match"] != "exact"
            or output_candidates[0]["version_match"] != "exact"
            or input_candidates[0]["version_match"] != "exact"):
        return "version_unresolved"
    return "single_candidate_pair"


def preview_model_draft(db, process_id, version, model_payload):
    """Combine observed source interfaces and model topology without saving a card."""
    source = _indexed_process(db, process_id, version)
    if source is None:
        raise IngestError(f"exact indexed source process {process_id}@{version} not found")
    card = projected_card(db, source)
    model = inspect_tiangong_model(model_payload)
    association_matches = [ref for ref in model["resulting_process_refs"]
                           if ref["type"] == "process data set" and
                           ref["id"] == process_id and ref["version"] == version]
    association = ("native_resulting_process_reference" if association_matches else
                   "native_resulting_process_conflict" if model["resulting_process_refs"] else
                   "explicitly_supplied_unverified")

    instances = []
    by_instance = {}
    for native in model["instances"]:
        row = _indexed_process(db, native["process_id"], native["requested_version"])
        exchanges = ([dict(item) for item in db.execute(
            """SELECT internal_id,direction,flow_id,flow_version FROM exchanges
               WHERE process_id=? AND process_version=? ORDER BY internal_id""",
            (native["process_id"], native["requested_version"]))]
                     if row is not None else [])
        instance = {**native, "indexed": row is not None,
                    "indexed_process_name": row["name"] if row is not None else None,
                    "indexed_content_hash": row["content_hash"] if row is not None else None,
                    "indexed_source_locator": row["source_locator"] if row is not None else None,
                    "indexed_exchanges": exchanges,
                    "module_mapping": None}
        instances.append(instance)
        if native["instance_id"] is not None:
            if native["instance_id"] in by_instance:
                by_instance[native["instance_id"]] = None
            else:
                by_instance[native["instance_id"]] = instance

    connections = []
    for native in model["connections"]:
        upstream = by_instance.get(native["from_instance"])
        downstream = by_instance.get(native["to_instance"])
        outputs = _port_candidates(db, upstream, "Output", native["from_flow_id"],
                                   native["from_flow_version"])
        inputs = _port_candidates(db, downstream, "Input", native["to_flow_id"],
                                  native["to_flow_version"])
        connections.append({
            **native,
            "output_exchange_candidates": outputs,
            "input_exchange_candidates": inputs,
            "candidate_status": _connection_status(native, upstream, downstream, outputs, inputs),
        })

    external = card["extracted"]["data"]["interfaces"]
    return {
        "schema": "htam.tiangong-curation-draft.v1",
        "mode": "preview",
        "source_process": {
            "id": process_id, "version": version,
            "content_hash": source["content_hash"],
            "source_locator": source["source_locator"],
            "association_to_model": association,
            "association_locator": association_matches[0]["locator"] if association_matches else None,
        },
        "whole_source_card_preview": card,
        "model": {
            "id": model["model_id"], "version": model["requested_version"],
            "content_hash": model["content_hash"],
            "reference_instance_id": model["reference_instance_id"],
            "resulting_process_refs": model["resulting_process_refs"],
            "structurally_consistent": model["structurally_consistent"],
            "diagnostics": model["diagnostics"],
        },
        "candidate_topology": {"instances": instances, "connections": connections},
        "boundary_mapping": {
            "status": "unresolved",
            "source_port_ids": [port["id"] for port in external],
            "reason": "Native flow links do not identify the curated child-module ports or external bindings.",
        },
        "unresolved": (["source_process_model_association"] if not association_matches else []) +
                      ["child_module_identity", "external_boundary_bindings",
                       "quantitative_relations"],
    }


def preview_from_files(db_path, process_id, version, model_file):
    model_payload = json.loads(Path(model_file).read_text(encoding="utf-8-sig"))
    with read_db(db_path) as db:
        return preview_model_draft(db, process_id, version, model_payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing local TianGong source index")
    parser.add_argument("--process-id", required=True, help="Exact indexed aggregate/source process")
    parser.add_argument("--version", required=True, help="Exact indexed process version")
    parser.add_argument("--model-file", required=True, help="Explicitly selected native lifecyclemodel JSON")
    args = parser.parse_args(argv)
    try:
        result = preview_from_files(args.db, args.process_id, args.version, args.model_file)
    except (IngestError, CatalogError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "unresolved", "detail": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
