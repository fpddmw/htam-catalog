"""Turn indexed TianGong source processes into traceable, whole-process catalog cards."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from .catalog import Catalog, CatalogError, digest, refresh_change, validate_card


def module_id(source_id, version):
    identity = f"{source_id}@{version}".encode("utf-8")
    return "tg-" + hashlib.sha256(identity).hexdigest()[:24]


def read_processes(db, source_id=None, version=None, offset=0, limit=20):
    if offset < 0 or limit < 1:
        raise ValueError("offset must be nonnegative and limit positive")
    clauses, values = [], []
    if source_id:
        clauses.append("id=?")
        values.append(source_id)
    if version:
        clauses.append("version=?")
        values.append(version)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return db.execute("SELECT * FROM processes" + where +
                      " ORDER BY id,version LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()


def projected_card(db, process):
    """Extract only indexed source facts; leave engineering interpretation unknown."""
    process = dict(process)
    exchanges = [dict(row) for row in db.execute("""SELECT e.*,f.name AS flow_name,
      f.flow_type FROM exchanges e LEFT JOIN flows f ON f.id=e.flow_id
      AND f.version=e.flow_version WHERE e.process_id=? AND e.process_version=?
      ORDER BY e.internal_id""", (process["id"], process["version"]))]
    issues = [dict(row) for row in db.execute("""SELECT field_path,reason
      FROM projection_issues WHERE kind='process' AND source_id=? AND version=?
      ORDER BY field_path""", (process["id"], process["version"]))]
    interfaces, source_exchanges = [], []
    for exchange in exchanges:
        flow_type = (exchange["flow_type"] or "").casefold()
        kind = "environment" if flow_type == "elementary flow" else "unspecified"
        unknowns = ["交换的单位、数量基准和物性尚未核对"]
        if kind == "unspecified":
            unknowns.append("介质类别尚未核实；可能是物料、能量或服务")
        if not exchange["flow_name"]:
            unknowns.append("确切版本的流记录尚未取得")
        if not exchange["flow_version"]:
            unknowns.append("来源交换未提供流版本")
        port_id = "x-" + hashlib.sha256(exchange["internal_id"].encode("utf-8")).hexdigest()[:12]
        flow_ref = {"provider": "tiangong-lca", "id": exchange["flow_id"]}
        if exchange["flow_version"]:
            flow_ref["version"] = exchange["flow_version"]
        interfaces.append({"id": port_id,
                           "direction": exchange["direction"].lower(), "kind": kind,
                           "terms": [exchange["flow_name"]] if exchange["flow_name"] else [],
                           "flow_refs": [flow_ref], "unknowns": unknowns})
        source_exchanges.append({"port_id": port_id, "internal_id": exchange["internal_id"],
                                 "flow_id": exchange["flow_id"], "flow_version": exchange["flow_version"],
                                 "flow_type": exchange["flow_type"],
                                 "mean_amount": exchange["mean_amount"],
                                 "resulting_amount": exchange["resulting_amount"],
                                 "is_reference": bool(exchange["is_reference"])})
    unknowns = ["功能、适用条件、过程边界和内部工序尚未解释",
                "交换单位、数量基准和物性尚未从完整来源记录核对"]
    if issues:
        unknowns.append(f"来源投影有 {len(issues)} 个跳过字段；需核对原始记录")
    if not exchanges:
        unknowns.append("未取得可用的输入输出交换")
    data = {
        "name": process["name"],
        "summary": process["technology_text"] or "天工来源过程尚无可用的工艺描述。",
        "aliases": [], "categories": [], "functions": [],
        "industries": [process["industry_class"]] if process["industry_class"] else [],
        "interfaces": interfaces,
        "representations": [{"id": "whole", "kind": "whole"}],
        "external_refs": [{"provider": "tiangong-lca", "id": process["id"],
                           "requested_version": process["version"],
                           "resolved_version": process["version"],
                           "relation": "partial", "scope": "来源过程及已索引交换的摘要；工艺解释未审阅",
                           "sha256": process["content_hash"]}],
        "unknowns": unknowns,
        "parameters": {"source_process": {
            "type": process["process_type"], "location": process["location"],
            "reference_year": process["reference_year"],
            "reference_exchange_id": process["reference_exchange_id"]},
            "source_exchanges": source_exchanges, "projection_issues": issues},
    }
    fingerprint = digest({"source_content_hash": process["content_hash"], "data": data})
    source_name = "projection-" + fingerprint[:24]
    source = {"id": source_name, "kind": "database",
              "citation": f"天工过程 {process['id']}@{process['version']} 的 HTAM 工艺投影",
              "locator": process["source_locator"],
              "retrieved_at": process["synced_at"], "sha256": fingerprint}
    card = {"schema_version": 1,
            "module_id": module_id(process["id"], process["version"]), "revision": 1,
            "sources": [source],
            "extracted": {"origin": "source_extraction", "source_ids": [source_name], "data": data},
            "local": {"origin": "hypothesis", "source_ids": [], "data": {}}}
    validate_card(card)
    return card


def build(db_path, workspace, *, source_id=None, version=None, offset=0, limit=20,
          apply=False, show_card=False):
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.is_file():
        raise ValueError(f"Source index does not exist: {db_path}")
    catalog = Catalog(workspace)
    marker = catalog.root / "catalog.json"
    if marker.exists():
        catalog.check()
    elif (catalog.root / "modules").exists() or (catalog.root / "index").exists():
        raise ValueError("Catalog workspace has data but no catalog marker")
    if apply:
        catalog.init()
    result = {"mode": "apply" if apply else "preview", "source_db": str(db_path),
              "workspace": str(catalog.root), "selected": 0, "created": 0,
              "refreshed": 0, "unchanged": 0, "failed": 0, "items": []}
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for process in read_processes(db, source_id, version, offset, limit):
            result["selected"] += 1
            try:
                change = None
                card = projected_card(db, process)
                if show_card and "sample_card" not in result:
                    result["sample_card"] = card
                mid = card["module_id"]
                revisions = catalog.revisions(mid) if marker.exists() or apply else []
                if not revisions:
                    action = "created"
                    if apply:
                        catalog.save(card, 0)
                else:
                    previous = catalog.get(mid)["card"]
                    refs = previous["extracted"]["data"].get("external_refs", [])
                    if not any(ref.get("provider") == "tiangong-lca" and
                               ref.get("id") == process["id"] and
                               ref.get("resolved_version") == process["version"] for ref in refs):
                        raise ValueError("Existing module has a different source identity")
                    if previous["extracted"]["source_ids"] == card["extracted"]["source_ids"]:
                        action = "unchanged"
                    else:
                        action = "refreshed"
                        change = {"kind": "source_refresh", "base_revision": previous["revision"],
                                  **refresh_change(previous, card["extracted"])}
                        if apply:
                            saved = catalog.refresh(mid, {"extracted": card["extracted"],
                                                          "sources": card["sources"]},
                                                    previous["revision"])
                            change = saved["change"]
                result[action] += 1
                if len(result["items"]) < 20:
                    item = {"process_id": process["id"], "version": process["version"],
                            "module_id": mid, "action": action,
                            "interfaces": len(card["extracted"]["data"]["interfaces"])}
                    if change is not None:
                        item["change"] = change
                    result["items"].append(item)
            except (CatalogError, ValueError, sqlite3.Error) as exc:
                result["failed"] += 1
                if len(result["items"]) < 20:
                    result["items"].append({"process_id": process["id"],
                                            "version": process["version"],
                                            "action": "failed", "reason": str(exc)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing TianGong SQLite projection")
    parser.add_argument("--workspace", required=True, help="Catalog card workspace")
    parser.add_argument("--process-id")
    parser.add_argument("--version")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true", help="Write new/changed cards; default is preview")
    parser.add_argument("--show-card", action="store_true", help="Include the first generated card in JSON output")
    args = parser.parse_args(argv)
    try:
        result = build(args.db, args.workspace, source_id=args.process_id,
                       version=args.version, offset=args.offset, limit=args.limit,
                       apply=args.apply, show_card=args.show_card)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["failed"] else 0
    except (CatalogError, ValueError, sqlite3.Error, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
