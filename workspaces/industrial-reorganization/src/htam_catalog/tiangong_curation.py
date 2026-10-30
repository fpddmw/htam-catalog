"""Turn an explicit, evidenced TianGong model mapping into a catalog revision."""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys

from .catalog import (Catalog, CatalogError, effective, identifier, keyed, read_json,
                      refresh_change, require, shape, validate_card)
from .tiangong_draft import preview_from_files
from .tiangong_sync import IngestError


SCHEMA = "htam.tiangong-curation-decision.v1"


def decision_template(draft):
    """Expose observed identities while leaving all semantic decisions open."""
    model_source_id = "model-" + draft["model"]["content_hash"][:24]
    association = ([{"source_id": model_source_id,
                     "locator": draft["source_process"]["association_locator"],
                     "note": "Native model references this exact resulting process ID and version"}]
                   if draft["source_process"]["association_to_model"] ==
                   "native_resulting_process_reference" else [])
    return {
        "schema": SCHEMA,
        "source_process": {key: draft["source_process"][key]
                           for key in ("id", "version", "content_hash")},
        "model": {key: draft["model"][key] for key in ("id", "version", "content_hash")},
        "module": {"id": None, "name": None, "summary": None,
                   "source_relation": None, "source_scope": None,
                   "unknowns": [], "evidence": []},
        "evidence_sources": [], "association_evidence": association,
        "instances": [{"instance_id": item["instance_id"], "module_id": None,
                       "revision": None, "representation": None, "evidence": []}
                      for item in draft["candidate_topology"]["instances"]],
        "connections": [{"index": index, "from_port": None, "to_port": None,
                         "from_exchange_internal_id": None,
                         "to_exchange_internal_id": None, "evidence": []}
                        for index, _ in enumerate(draft["candidate_topology"]["connections"])],
        "boundary_bindings": [{"source_port_id": pid, "instance_id": None,
                               "port_id": None, "exchange_internal_id": None,
                               "evidence": []}
                              for pid in draft["boundary_mapping"]["source_port_ids"]],
    }


def _evidence(items, sources, path):
    require(isinstance(items, list) and bool(items), f"{path}: evidence is required")
    for item in items:
        shape(item, {"source_id", "locator", "note"}, set(), path)
        require(isinstance(item["source_id"], str), f"{path}: source_id must be text")
        require(item["source_id"] in sources, f"{path}: unknown evidence source {item['source_id']}")
        require(all(isinstance(value, str) and value.strip() for value in item.values()),
                f"{path}: evidence fields must be nonempty strings")


def _same_flow(port, flow_id, flow_version, direction, path):
    require(port["direction"] == direction, f"{path}: direction differs from source exchange")
    require(bool(flow_id and flow_version), f"{path}: exact flow identity/version required")
    require(any(ref.get("provider") == "tiangong-lca" and ref.get("id") == flow_id
                and ref.get("version") == flow_version for ref in port.get("flow_refs", [])),
            f"{path}: port lacks exact source flow identity/version")


def _child_ports(catalog, mapping, native, path):
    shape(mapping, {"instance_id", "module_id", "revision", "representation", "evidence"},
          set(), path)
    require(mapping["instance_id"] == native["instance_id"], f"{path}: native instance mismatch")
    require(identifier(mapping["representation"]), f"{path}: invalid child representation")
    loaded = catalog.get(mapping["module_id"], mapping["revision"])["effective"]
    require(mapping["representation"] in keyed(loaded["representations"], "representations"),
            f"{path}: child representation missing")
    require(any(ref.get("provider") == "tiangong-lca" and
                ref.get("id") == native["process_id"] and
                ref.get("resolved_version") == native["requested_version"] and
                ref.get("sha256") == native["indexed_content_hash"] and
                ref.get("relation") != "candidate"
                for ref in loaded.get("external_refs", [])),
            f"{path}: child module does not cite the current exact native process content")
    source_exchanges = loaded.get("parameters", {}).get("source_exchanges", [])
    require(isinstance(source_exchanges, list) and
            all(isinstance(item, dict) for item in source_exchanges),
            f"{path}: malformed child source exchanges")
    return keyed(loaded["interfaces"], "interfaces"), source_exchanges


def _exchange(candidates, internal_id, path):
    matches = [item for item in candidates if item["exchange_internal_id"] == internal_id]
    require(len(matches) == 1 and matches[0]["version_match"] == "exact",
            f"{path}: selected exchange is absent or its flow version is unresolved")


def _port(ports, port_id, path):
    require(identifier(port_id), f"{path}: invalid child port ID")
    require(port_id in ports, f"{path}: unknown child port {port_id}")
    return ports[port_id]


def _source_exchange(exchanges, internal_id, port_id, path):
    """Check an exact exchange-to-port link when a child card records one."""
    if exchanges:
        require(any(item.get("internal_id") == internal_id and
                    item.get("port_id") == port_id for item in exchanges),
                f"{path}: child card maps the selected exchange to another port")


def build_card(draft, decision, catalog, expected_revision):
    """Validate research decisions against exact source/model/child revisions."""
    shape(decision, {"schema", "source_process", "model", "module", "evidence_sources",
                     "association_evidence", "instances", "connections", "boundary_bindings"},
          set(), "decision")
    require(decision["schema"] == SCHEMA, "Unsupported curation decision schema")
    for name in ("source_process", "model"):
        shape(decision[name], {"id", "version", "content_hash"}, set(), name)
        observed = draft[name]
        require(all(decision[name][key] == observed[key] for key in ("id", "version", "content_hash")),
                f"{name}: identity, version or content hash changed")
    require(draft["model"]["structurally_consistent"], "Native model has structural diagnostics")
    require(draft["source_process"]["association_to_model"] !=
            "native_resulting_process_conflict",
            "Native model references a different resulting process", "MODEL_SOURCE_MISMATCH")
    require(type(expected_revision) is int and expected_revision >= 0,
            "expect-revision must be nonnegative")
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
                                                       for v in module[field])), f"module.{field}: invalid list")

    base = copy.deepcopy(draft["whole_source_card_preview"])
    projection_source = base["sources"][0]
    model_source = {"id": "model-" + draft["model"]["content_hash"][:24],
                    "kind": "database",
                    "citation": f"天工生命周期模型 {draft['model']['id']}@{draft['model']['version']}",
                    "locator": "lifeCycleModelDataSet.lifeCycleModelInformation",
                    "sha256": draft["model"]["content_hash"]}
    require(isinstance(decision["evidence_sources"], list), "evidence_sources: expected list")
    sources = keyed([projection_source, model_source] + decision["evidence_sources"], "sources")
    for source in decision["evidence_sources"]:
        shape(source, {"id", "kind", "citation"}, {"uri", "locator", "retrieved_at", "sha256"},
              "evidence_source")
    _evidence(decision["association_evidence"], sources, "association_evidence")
    _evidence(module["evidence"], sources, "module.evidence")

    native_instances = keyed([{"id": item["instance_id"], **item}
                              for item in draft["candidate_topology"]["instances"]], "native instances")
    require(isinstance(decision["instances"], list), "instances: expected list")
    mappings = {}
    child_ports = {}
    child_exchanges = {}
    children = []
    for item in decision["instances"]:
        require(isinstance(item, dict) and isinstance(item.get("instance_id"), str) and
                item["instance_id"] in native_instances,
                "instances: unknown native instance")
        iid = item["instance_id"]
        require(iid not in mappings, f"instances: duplicate mapping for {iid}")
        require(native_instances[iid]["indexed"], f"instances: process {iid} is not indexed")
        _evidence(item.get("evidence"), sources, f"instances[{iid}].evidence")
        child_ports[iid], child_exchanges[iid] = _child_ports(
            catalog, item, native_instances[iid], f"instances[{iid}]")
        mappings[iid] = item
        children.append({"id": iid, "module_id": item["module_id"],
                         "revision": item["revision"], "representation": item["representation"]})
    require(set(mappings) == set(native_instances), "Every native instance needs an explicit child mapping")

    native_edges = draft["candidate_topology"]["connections"]
    require(isinstance(decision["connections"], list), "connections: expected list")
    connections = []
    seen_edges = set()
    for item in decision["connections"]:
        shape(item, {"index", "from_port", "to_port", "from_exchange_internal_id",
                     "to_exchange_internal_id", "evidence"}, set(), "connection mapping")
        index = item["index"]
        require(type(index) is int and 0 <= index < len(native_edges) and index not in seen_edges,
                "connection mapping: invalid or duplicate native edge index")
        seen_edges.add(index)
        edge = native_edges[index]
        require(edge["candidate_status"] in {"single_candidate_pair", "ambiguous_exchange"},
                f"connections[{index}]: unresolved native flow or process identity")
        _evidence(item["evidence"], sources, f"connections[{index}].evidence")
        _exchange(edge["output_exchange_candidates"], item["from_exchange_internal_id"],
                  f"connections[{index}].from")
        _exchange(edge["input_exchange_candidates"], item["to_exchange_internal_id"],
                  f"connections[{index}].to")
        start = _port(child_ports[edge["from_instance"]], item["from_port"], f"connections[{index}]")
        end = _port(child_ports[edge["to_instance"]], item["to_port"], f"connections[{index}]")
        _same_flow(start, edge["from_flow_id"], edge["from_flow_version"], "output",
                   f"connections[{index}].from")
        _same_flow(end, edge["to_flow_id"], edge["to_flow_version"], "input",
                   f"connections[{index}].to")
        _source_exchange(child_exchanges[edge["from_instance"]],
                         item["from_exchange_internal_id"], item["from_port"],
                         f"connections[{index}].from")
        _source_exchange(child_exchanges[edge["to_instance"]],
                         item["to_exchange_internal_id"], item["to_port"],
                         f"connections[{index}].to")
        connections.append({"from": {"instance": edge["from_instance"], "port": item["from_port"]},
                            "to": {"instance": edge["to_instance"], "port": item["to_port"]}})
    require(seen_edges == set(range(len(native_edges))),
            "Every native connection needs an explicit exchange and port mapping")

    current = None
    if expected_revision:
        current = catalog.get(module["id"], expected_revision)["card"]
        require(catalog.revisions(module["id"])[-1] == expected_revision,
                "Expected revision is no longer the head", "REVISION_CONFLICT")
        previous_ref = current["extracted"]["data"].get("external_refs", [])
        require(any(ref.get("provider") == "tiangong-lca" and
                    ref.get("id") == draft["source_process"]["id"] and
                    ref.get("resolved_version") == draft["source_process"]["version"]
                    for ref in previous_ref), "Existing module has a different extracted source")
        card = copy.deepcopy(current)
        card["extracted"] = copy.deepcopy(base["extracted"])
    else:
        require(not catalog.revisions(module["id"]), "Module already exists; pass its current revision",
                "REVISION_CONFLICT")
        card = base
        card["module_id"] = module["id"]
    card["revision"] = expected_revision + 1
    existing_sources = keyed(card["sources"], "sources")
    for sid, source in sources.items():
        if sid in existing_sources:
            require(existing_sources[sid] == source, f"Source ID collision: {sid}")
        else:
            card["sources"].append(source)
    data = effective(card)[0]
    parent_ports = keyed(data["interfaces"], "interfaces")
    source_ports = keyed(base["extracted"]["data"]["interfaces"], "source interfaces")
    require(parent_ports.keys() == source_ports.keys(),
            "Existing module interfaces no longer match the selected source projection")
    overrides = module.get("interface_overrides", [])
    require(isinstance(overrides, list), "module.interface_overrides: expected list")
    seen_overrides = set()
    for item in overrides:
        shape(item, {"port_id", "kind", "terms", "evidence"}, set(), "interface override")
        pid = item["port_id"]
        require(isinstance(pid, str) and pid in parent_ports and pid not in seen_overrides,
                "Interface override port unknown or duplicated")
        seen_overrides.add(pid)
        require(isinstance(item["kind"], str) and
                item["kind"] in {"material", "energy", "service", "environment", "unspecified"},
                f"interface override {pid}: invalid kind")
        require(isinstance(item["terms"], list) and
                all(isinstance(term, str) and term.strip() for term in item["terms"]),
                f"interface override {pid}: invalid terms")
        _evidence(item["evidence"], sources, f"interface override {pid}.evidence")
        parent_ports[pid] = {**parent_ports[pid], "kind": item["kind"], "terms": item["terms"]}
    require(isinstance(decision["boundary_bindings"], list), "boundary_bindings: expected list")
    bindings = {}
    for item in decision["boundary_bindings"]:
        shape(item, {"source_port_id", "instance_id", "port_id", "exchange_internal_id",
                     "evidence"}, set(), "boundary mapping")
        pid, iid = item["source_port_id"], item["instance_id"]
        require(isinstance(pid, str) and pid in source_ports and pid not in bindings,
                "Boundary source port unknown or duplicated")
        require(isinstance(iid, str) and iid in mappings, "Boundary child instance unknown")
        _evidence(item["evidence"], sources, f"boundary[{pid}].evidence")
        outer = parent_ports[pid]
        inner = _port(child_ports[iid], item["port_id"], f"boundary[{pid}]")
        refs = [ref for ref in source_ports[pid].get("flow_refs", [])
                if ref.get("provider") == "tiangong-lca" and ref.get("version")]
        require(len(refs) == 1, f"boundary[{pid}]: exact source flow version required")
        ref = refs[0]
        _same_flow(outer, ref["id"], ref["version"], source_ports[pid]["direction"],
                   f"boundary[{pid}].outer")
        _same_flow(inner, ref["id"], ref["version"], source_ports[pid]["direction"],
                   f"boundary[{pid}].inner")
        require(any(exchange["internal_id"] == item["exchange_internal_id"] and
                    exchange["direction"].lower() == source_ports[pid]["direction"] and
                    exchange["flow_id"] == ref["id"] and
                    exchange["flow_version"] == ref["version"]
                    for exchange in native_instances[iid]["indexed_exchanges"]),
                f"boundary[{pid}]: selected exchange not found on indexed child")
        _source_exchange(child_exchanges[iid], item["exchange_internal_id"],
                         item["port_id"], f"boundary[{pid}]")
        bindings[pid] = {"instance": iid, "port": item["port_id"]}
    require(set(bindings) == set(source_ports),
            "Every source boundary port needs an explicit child binding")

    rep_id = "tiangong-model-" + draft["model"]["content_hash"][:12]
    representation = {"id": rep_id, "kind": "composite", "children": children,
                      "connections": connections, "boundary_bindings": bindings,
                      "description": "Evidenced mapping of the selected native lifecycle model; instance factors remain source observations."}
    reps = [rep for rep in data["representations"] if rep["id"] != rep_id] + [representation]
    refs = [ref for ref in data.get("external_refs", [])
            if not (ref["provider"] == "tiangong-lca" and
                    ref["id"] == draft["source_process"]["id"] and
                    ref.get("resolved_version") == draft["source_process"]["version"])]
    source_ref = copy.deepcopy(base["extracted"]["data"]["external_refs"][0])
    source_ref.update(relation=module["source_relation"], scope=module["source_scope"])
    refs.append(source_ref)
    refs = [ref for ref in refs if not (ref["provider"] == "tiangong-lca-lifecyclemodel" and
                                        ref["id"] == draft["model"]["id"] and
                                        ref.get("resolved_version") == draft["model"]["version"])]
    refs.append({"provider": "tiangong-lca-lifecyclemodel", "id": draft["model"]["id"],
                 "requested_version": draft["model"]["version"],
                 "resolved_version": draft["model"]["version"],
                 "relation": "partial", "scope": "Selected native instance and connection topology",
                 "sha256": draft["model"]["content_hash"]})
    parameters = copy.deepcopy(base["extracted"]["data"]["parameters"])
    parameters.update({key: value for key, value in data.get("parameters", {}).items()
                       if key not in {"source_process", "source_exchanges", "projection_issues",
                                      "curation_evidence"}})
    parameters["curation_evidence"] = {
        "schema": SCHEMA, "association": decision["association_evidence"],
        "module": module["evidence"],
        "instances": [{"instance_id": item["instance_id"], "evidence": item["evidence"]}
                      for item in decision["instances"]],
        "connections": [{"index": item["index"], "evidence": item["evidence"]}
                        for item in decision["connections"]],
        "boundaries": [{"source_port_id": item["source_port_id"], "evidence": item["evidence"]}
                       for item in decision["boundary_bindings"]],
        "native_reference_instance_id": draft["model"]["reference_instance_id"],
        "native_instance_factors": {iid: native_instances[iid]["multiplication_factor"]
                                    for iid in native_instances}}
    local = card["local"]
    if not local["data"]:
        local["origin"] = "sourced_supplement"
    local["source_ids"] = sorted(set(local["source_ids"]) |
                                 {entry["source_id"] for entry in
                                  decision["association_evidence"] + module["evidence"]} |
                                 {model_source["id"], projection_source["id"]})
    local["data"].update({"name": module["name"], "summary": module["summary"],
                          "representations": reps, "external_refs": refs,
                          "unknowns": module["unknowns"],
                          "parameters": parameters})
    if overrides:
        local["data"]["interfaces"] = list(parent_ports.values())
    for field in ("functions", "categories"):
        if field in module:
            local["data"][field] = module[field]
    # Include every source cited by a relation, even if it did not support the
    # module's top-level name or summary.
    relation_evidence = [ev for item in decision["instances"] + decision["connections"] +
                         decision["boundary_bindings"] + overrides for ev in item["evidence"]]
    local["source_ids"] = sorted(set(local["source_ids"]) |
                                 {ev["source_id"] for ev in relation_evidence})
    validated = validate_card(card)
    catalog._check_references(card, validated)
    return card


def curate(db_path, process_id, version, model_file, decision_file, workspace,
           expected_revision=0, apply=False):
    draft = preview_from_files(db_path, process_id, version, model_file)
    decision = read_json(decision_file)
    catalog = Catalog(workspace)
    catalog.check()
    card = build_card(draft, decision, catalog, expected_revision)
    result = {"mode": "apply" if apply else "preview", "module_id": card["module_id"],
              "revision": card["revision"], "representation_id": card["local"]["data"]["representations"][-1]["id"],
              "card": card}
    if expected_revision:
        result["source_change"] = refresh_change(
            catalog.get(card["module_id"], expected_revision)["card"],
            draft["whole_source_card_preview"]["extracted"])
    if apply:
        result["saved"] = catalog.save(card, expected_revision)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--process-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--model-file", required=True)
    parser.add_argument("--decision-file")
    parser.add_argument("--workspace")
    parser.add_argument("--template", action="store_true", help="Print an unfilled decision template")
    parser.add_argument("--expect-revision", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Save validated revision; default previews")
    args = parser.parse_args(argv)
    try:
        if args.template:
            require(not args.apply and not args.decision_file, "Template mode does not accept --apply/--decision-file")
            result = decision_template(preview_from_files(args.db, args.process_id,
                                                           args.version, args.model_file))
        else:
            require(bool(args.decision_file and args.workspace),
                    "--decision-file and --workspace are required for preview/apply")
            result = curate(args.db, args.process_id, args.version, args.model_file,
                            args.decision_file, args.workspace, args.expect_revision, args.apply)
    except (CatalogError, IngestError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "unresolved", "detail": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
