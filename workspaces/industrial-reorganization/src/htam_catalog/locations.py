"""Locate fixed module uses in the currently saved recursive catalog."""
from __future__ import annotations

from collections import defaultdict


def occurrence_index(records, latest, *, max_depth=32, max_nodes=10000):
    """Traverse all expressions of latest roots while retaining pinned children.

    Each entry is a directory occurrence, not a claim that a research scheme
    selected that expression or that an upstream source observed its connection.
    """
    occurrences = defaultdict(list)
    diagnostics = []
    visited = 0

    for root_id, root_revision in sorted(latest):
        root = records[(root_id, root_revision)]
        root_data = root["effective"]
        root_context = {
            "module_id": root_id,
            "revision": root_revision,
            "name": root_data["name"],
            "sources": root["card"]["sources"],
            "external_refs": root_data.get("external_refs", []),
        }
        for root_rep in root_data["representations"]:
            if visited >= max_nodes:
                diagnostics.append({"code": "NODE_LIMIT", "root_module_id": root_id})
                return dict(occurrences), diagnostics

            def walk(module_id, revision, representation, path, depth, stack):
                nonlocal visited
                if visited >= max_nodes:
                    diagnostics.append({"code": "NODE_LIMIT", "path": path})
                    return
                key = (module_id, revision)
                record = records.get(key)
                if record is None:
                    diagnostics.append({"code": "MISSING_PINNED_REVISION", "path": path})
                    return
                expressions = {item["id"]: item for item in record["effective"]["representations"]}
                rep = expressions.get(representation)
                if rep is None:
                    diagnostics.append({"code": "MISSING_PINNED_EXPRESSION", "path": path})
                    return
                visited += 1
                occurrence = {
                    "root": root_context,
                    "root_representation": root_rep["id"],
                    "path": path,
                    "depth": depth,
                    "representation": representation,
                    "representation_origin": record["field_origins"].get("representations"),
                }
                occurrences[key].append(occurrence)
                identity = (module_id, revision, representation)
                if identity in stack:
                    diagnostics.append({"code": "CONTAINMENT_CYCLE", "path": path})
                elif rep.get("children") and depth >= max_depth:
                    diagnostics.append({"code": "DEPTH_LIMIT", "path": path})
                else:
                    for child in rep.get("children", []):
                        walk(child["module_id"], child["revision"], child["representation"],
                             path + "/" + child["id"], depth + 1, stack | {identity})

            walk(root_id, root_revision, root_rep["id"], root_id, 0, set())
    return dict(occurrences), diagnostics
