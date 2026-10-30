"""Curate one indexed TianGong source as a sourced whole-process module."""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys

from .catalog import (Catalog, CatalogError, effective, identifier, keyed, read_json,
                      refresh_change, require, shape, validate_card)
from .tiangong_cards import projected_card
from .tiangong_curation import _evidence
from .tiangong_draft import _indexed_process
from .tiangong_sync import IngestError, read_db


SCHEMA = "htam.tiangong-whole-decision.v1"


def source_card(db_path, process_id, version):
    with read_db(db_path) as db:
        row = _indexed_process(db, process_id, version)
        if row is None:
            raise IngestError(f"exact indexed source process {process_id}@{version} not found")
        return projected_card(db, row)


def decision_template(card):
    ref = card["extracted"]["data"]["external_refs"][0]
    return {"schema": SCHEMA,
            "source_process": {"id": ref["id"], "version": ref["resolved_version"],
                               "content_hash": ref["sha256"]},
            "module": {"id": None, "name": None, "summary": None,
                       "source_relation": None, "source_scope": None,
                       "unknowns": [], "evidence": [], "interface_overrides": []},
            "evidence_sources": []}


def build_card(base, decision, catalog, expected_revision):
    shape(decision, {"schema", "source_process", "module", "evidence_sources"}, set(),
          "decision")
    require(decision["schema"] == SCHEMA, "Unsupported whole-module decision schema")
    shape(decision["source_process"], {"id", "version", "content_hash"}, set(),
          "source_process")
    expected_source = decision_template(base)["source_process"]
    require(decision["source_process"] == expected_source,
            "Source identity, version or content hash changed")
    module = decision["module"]
    shape(module, {"id", "name", "summary", "source_relation", "source_scope",
                   "unknowns", "evidence"},
          {"functions", "categories", "interface_overrides"}, "module")
    require(identifier(module["id"]), "module.id is invalid")
    require(isinstance(module["source_relation"], str) and
            module["source_relation"] in {"exact", "partial", "aggregate_contains"},
            "module.source_relation must be a supported relationship")
    require(all(isinstance(module[key], str) and module[key].strip()
                for key in ("name", "summary", "source_scope")),
            "module name, summary and source_scope must be nonempty")
    for field in ("functions", "categories", "unknowns"):
        require(field not in module or
                (isinstance(module[field], list) and all(isinstance(v, str) and v.strip()
                                                       for v in module[field])),
                f"module.{field}: invalid list")
    require(type(expected_revision) is int and expected_revision >= 0,
            "expect-revision must be nonnegative")
    require(isinstance(decision["evidence_sources"], list), "evidence_sources: expected list")
    sources = keyed(base["sources"] + decision["evidence_sources"], "sources")
    for source in decision["evidence_sources"]:
        shape(source, {"id", "kind", "citation"}, {"uri", "locator", "retrieved_at", "sha256"},
              "evidence_source")
    _evidence(module["evidence"], sources, "module.evidence")

    if expected_revision:
        current = catalog.get(module["id"], expected_revision)["card"]
        require(catalog.revisions(module["id"])[-1] == expected_revision,
                "Expected revision is no longer the head", "REVISION_CONFLICT")
        refs = current["extracted"]["data"].get("external_refs", [])
        require(any(ref.get("provider") == "tiangong-lca" and
                    ref.get("id") == expected_source["id"] and
                    ref.get("resolved_version") == expected_source["version"] for ref in refs),
                "Existing module has a different extracted source")
        card = copy.deepcopy(current)
        card["extracted"] = copy.deepcopy(base["extracted"])
    else:
        require(not catalog.revisions(module["id"]), "Module already exists; pass its current revision",
                "REVISION_CONFLICT")
        card = copy.deepcopy(base)
        card["module_id"] = module["id"]
    card["revision"] = expected_revision + 1
    existing_sources = keyed(card["sources"], "sources")
    for sid, source in sources.items():
        if sid in existing_sources:
            require(existing_sources[sid] == source, f"Source ID collision: {sid}")
        else:
            card["sources"].append(source)
    data = effective(card)[0]
    ports = keyed(data["interfaces"], "interfaces")
    source_ports = keyed(base["extracted"]["data"]["interfaces"], "source interfaces")
    require(ports.keys() == source_ports.keys(),
            "Existing module interfaces no longer match the selected source projection")
    overrides = module.get("interface_overrides", [])
    require(isinstance(overrides, list), "module.interface_overrides: expected list")
    seen = set()
    for item in overrides:
        shape(item, {"port_id", "kind", "terms", "evidence"}, set(), "interface override")
        pid = item["port_id"]
        require(isinstance(pid, str) and pid in ports and pid not in seen,
                "Interface override port unknown or duplicated")
        seen.add(pid)
        require(isinstance(item["kind"], str) and
                item["kind"] in {"material", "energy", "service", "environment", "unspecified"},
                f"interface override {pid}: invalid kind")
        require(isinstance(item["terms"], list) and
                all(isinstance(term, str) and term.strip() for term in item["terms"]),
                f"interface override {pid}: invalid terms")
        _evidence(item["evidence"], sources, f"interface override {pid}.evidence")
        ports[pid] = {**ports[pid], "kind": item["kind"], "terms": item["terms"]}
    local = card["local"]
    if not local["data"]:
        local["origin"] = "sourced_supplement"
    local["source_ids"] = sorted(set(local["source_ids"]) |
                                 {entry["source_id"] for entry in module["evidence"]} |
                                 {entry["source_id"] for item in overrides for entry in item["evidence"]} |
                                 {base["sources"][0]["id"]})
    ref = copy.deepcopy(base["extracted"]["data"]["external_refs"][0])
    ref.update(relation=module["source_relation"], scope=module["source_scope"])
    refs = [item for item in data.get("external_refs", [])
            if not (item["provider"] == "tiangong-lca" and
                    item["id"] == expected_source["id"] and
                    item.get("resolved_version") == expected_source["version"])] + [ref]
    parameters = copy.deepcopy(base["extracted"]["data"]["parameters"])
    parameters.update({key: value for key, value in data.get("parameters", {}).items()
                       if key not in {"source_process", "source_exchanges", "projection_issues",
                                      "curation_evidence"}})
    parameters["curation_evidence"] = {"schema": SCHEMA, "module": module["evidence"],
                                       "interfaces": [{"port_id": item["port_id"],
                                                       "evidence": item["evidence"]}
                                                      for item in overrides]}
    local["data"].update({"name": module["name"], "summary": module["summary"],
                          "external_refs": refs, "unknowns": module["unknowns"],
                          "parameters": parameters})
    if overrides:
        local["data"]["interfaces"] = list(ports.values())
    for field in ("functions", "categories"):
        if field in module:
            local["data"][field] = module[field]
    validated = validate_card(card)
    catalog._check_references(card, validated)
    return card


def curate(db_path, process_id, version, decision_file, workspace, expected_revision=0,
           apply=False):
    base = source_card(db_path, process_id, version)
    decision = read_json(decision_file)
    catalog = Catalog(workspace)
    catalog.check()
    card = build_card(base, decision, catalog, expected_revision)
    result = {"mode": "apply" if apply else "preview", "module_id": card["module_id"],
              "revision": card["revision"], "card": card}
    if expected_revision:
        result["source_change"] = refresh_change(
            catalog.get(card["module_id"], expected_revision)["card"], base["extracted"])
    if apply:
        result["saved"] = catalog.save(card, expected_revision)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--process-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--decision-file")
    parser.add_argument("--workspace")
    parser.add_argument("--template", action="store_true", help="Print an unfilled whole-module decision")
    parser.add_argument("--expect-revision", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Save validated revision; default previews")
    args = parser.parse_args(argv)
    try:
        if args.template:
            require(not args.apply and not args.decision_file,
                    "Template mode does not accept --apply/--decision-file")
            result = decision_template(source_card(args.db, args.process_id, args.version))
        else:
            require(bool(args.decision_file and args.workspace),
                    "--decision-file and --workspace are required for preview/apply")
            result = curate(args.db, args.process_id, args.version, args.decision_file,
                            args.workspace, args.expect_revision, args.apply)
    except (CatalogError, IngestError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "unresolved", "detail": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
