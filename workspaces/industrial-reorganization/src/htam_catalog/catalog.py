#!/usr/bin/env python3
"""Local recursive process catalog. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

VERSION = 1
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
DATA_FIELDS = {"name", "summary", "aliases", "categories", "functions", "industries",
               "interfaces", "representations", "external_refs", "unknowns", "parameters"}
LIST_FIELDS = {"aliases", "categories", "functions", "industries", "unknowns"}
ORIGINS = {"source_extraction", "sourced_supplement", "hypothesis", "synthetic"}


class CatalogError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message, code="INVALID_CARD"):
    if not condition:
        raise CatalogError(code, message)


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def integer(value):
    return type(value) is int and value > 0


def one_of(value, values):
    return isinstance(value, str) and value in values


def words(value, path):
    require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value),
            f"{path}: expected a list of nonempty strings")


def shape(value, required, optional, path):
    require(isinstance(value, dict), f"{path}: expected an object")
    require(set(required) <= value.keys(), f"{path}: missing {sorted(set(required) - value.keys())}")
    require(value.keys() <= set(required) | set(optional),
            f"{path}: unknown fields {sorted(value.keys() - set(required) - set(optional))}")


def keyed(items, path):
    require(isinstance(items, list), f"{path}: expected a list")
    result = {}
    for item in items:
        require(isinstance(item, dict) and identifier(item.get("id")), f"{path}: invalid id")
        require(item["id"] not in result, f"{path}: duplicate id {item['id']}")
        result[item["id"]] = item
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, f"Duplicate JSON key: {key}", "INVALID_JSON")
            result[key] = value
        return result

    def invalid(value):
        raise CatalogError("INVALID_JSON", f"Non-finite JSON number: {value}")

    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                          parse_constant=invalid)
    except (OSError, ValueError) as exc:
        raise CatalogError("READ_FAILED", f"{path}: {exc}") from exc


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def effective(card):
    data, origins = {}, {}
    for layer_name in ("extracted", "local"):
        layer = card[layer_name]
        for key, value in layer["data"].items():
            data[key] = copy.deepcopy(value)
            origins[key] = {"layer": layer_name, "origin": layer["origin"],
                            "source_ids": layer["source_ids"]}
    return data, origins


def refresh_change(current, extracted):
    """Describe source-layer changes before saving a refreshed card."""
    shape(extracted, {"origin", "source_ids", "data"}, set(), "extracted")
    require(isinstance(extracted["data"], dict), "extracted.data must be object")
    before, after = current["extracted"]["data"], extracted["data"]
    changed = sorted(k for k in before.keys() | after.keys()
                     if (k in before) != (k in after) or before.get(k) != after.get(k))
    old_ports = keyed(before.get("interfaces", []), "interfaces")
    new_ports = keyed(after.get("interfaces", []), "interfaces")
    return {"changed_source_fields": changed,
            "changed_interface_ids": {
                "added": sorted(new_ports.keys() - old_ports.keys()),
                "removed": sorted(old_ports.keys() - new_ports.keys()),
                "modified": sorted(key for key in old_ports.keys() & new_ports.keys()
                                   if old_ports[key] != new_ports[key])},
            "locally_overridden_fields": sorted(set(changed) & current["local"]["data"].keys()),
            "review_required": bool(changed) or current["extracted"] != extracted}


def validate_card(card):
    shape(card, {"schema_version", "module_id", "revision", "sources", "extracted", "local"}, set(), "card")
    require(type(card["schema_version"]) is int and card["schema_version"] == VERSION, "Unsupported schema_version")
    require(identifier(card["module_id"]), "module_id: use lowercase letters, digits, _ or - (max 80)")
    require(integer(card["revision"]), "revision: expected positive integer")
    sources = keyed(card["sources"], "sources")
    for source in sources.values():
        shape(source, {"id", "kind", "citation"}, {"uri", "locator", "retrieved_at", "sha256"}, "source")
        require(one_of(source["kind"], {"database", "paper", "patent", "report", "synthetic", "other"}), "source.kind")
        require(all(isinstance(v, str) and v.strip() for v in source.values()), "source fields must be nonempty strings")
    for layer_name in ("extracted", "local"):
        layer = card[layer_name]
        shape(layer, {"origin", "source_ids", "data"}, set(), layer_name)
        require(one_of(layer["origin"], ORIGINS), f"{layer_name}.origin")
        words(layer["source_ids"], f"{layer_name}.source_ids")
        require(set(layer["source_ids"]) <= sources.keys(), f"{layer_name}: unresolved source_ids")
        require(isinstance(layer["data"], dict) and layer["data"].keys() <= DATA_FIELDS, f"{layer_name}.data: unknown fields")
        if layer["data"] and layer["origin"] in {"source_extraction", "sourced_supplement"}:
            require(bool(layer["source_ids"]), f"{layer_name}: sourced data requires source_ids")
        validate_data(layer["data"], partial=True)
    data, _ = effective(card)
    validate_data(data, partial=False)
    # Canonical encoding also rejects NaN supplied by direct Python callers.
    try:
        canonical(card)
    except (TypeError, ValueError) as exc:
        raise CatalogError("INVALID_CARD", str(exc)) from exc
    return data


def validate_data(data, partial):
    if not partial:
        require({"name", "summary", "interfaces", "representations", "unknowns"} <= data.keys(),
                "Effective data needs name, summary, interfaces, representations and unknowns")
    for key in ("name", "summary"):
        if key in data:
            require(isinstance(data[key], str) and bool(data[key].strip()), f"data.{key}: nonempty string required")
    for key in LIST_FIELDS & data.keys():
        words(data[key], key)
    if "parameters" in data:
        require(isinstance(data["parameters"], dict), "parameters: expected an object (record only, no evaluation)")
    ports = keyed(data.get("interfaces", []), "interfaces")
    for port in ports.values():
        shape(port, {"id", "direction", "kind", "terms"}, {"properties", "requirements", "flow_refs", "unknowns"}, "interface")
        require(one_of(port["direction"], {"input", "output"}), "interface.direction")
        require(one_of(port["kind"], {"material", "energy", "service", "environment", "unspecified"}), "interface.kind")
        words(port["terms"], "interface.terms")
        words(port.get("unknowns", []), "interface.unknowns")
        for field in ("properties", "requirements"):
            require(isinstance(port.get(field, []), list), f"interface.{field}: expected list")
            seen = set()
            for item in port.get(field, []):
                required = {"key", "value", "unit", "basis"} | ({"op"} if field == "requirements" else set())
                shape(item, required, set(), field)
                require(isinstance(item["key"], str) and item["key"].strip(), f"{field}.key")
                require(all(isinstance(item[k], str) for k in ("unit", "basis")), f"{field}: unit/basis must be strings")
                val = item["value"]
                require(val is None or isinstance(val, str) or (type(val) in (int, float) and math.isfinite(val)), f"{field}: finite scalar or null required")
                if field == "requirements":
                    require(one_of(item["op"], {"eq", "le", "ge"}), "requirement.op")
                    if item["op"] != "eq":
                        require(val is None or type(val) in (int, float), "Numeric inequality requires numeric value or null")
                else:
                    require(item["key"] not in seen, "Duplicate property key")
                    seen.add(item["key"])
        require(isinstance(port.get("flow_refs", []), list), "flow_refs: expected list")
        for ref in port.get("flow_refs", []):
            shape(ref, {"provider", "id"}, {"version"}, "flow_ref")
            require(all(isinstance(v, str) and v for v in ref.values()), "flow_ref values must be nonempty strings")
    reps = keyed(data.get("representations", []), "representations")
    if not partial:
        require(bool(reps), "At least one representation required")
    for rep in reps.values():
        shape(rep, {"id", "kind"}, {"description", "children", "connections", "boundary_bindings"}, "representation")
        require(one_of(rep["kind"], {"whole", "composite"}), "representation.kind")
        require("description" not in rep or isinstance(rep["description"], str), "representation.description")
        children = keyed(rep.get("children", []), "children")
        if rep["kind"] == "whole":
            require(not any(rep.get(k) for k in ("children", "connections", "boundary_bindings")), "Whole representation has no internal topology")
        else:
            require(bool(children), "Composite representation needs children")
        for child in children.values():
            shape(child, {"id", "module_id", "revision", "representation"}, set(), "child")
            require(identifier(child["module_id"]) and integer(child["revision"]) and identifier(child["representation"]), "Invalid child reference")
        require(isinstance(rep.get("connections", []), list), "connections: expected list")
        for edge in rep.get("connections", []):
            shape(edge, {"from", "to"}, set(), "connection")
            for endpoint in edge.values():
                validate_endpoint(endpoint, children)
        bindings = rep.get("boundary_bindings", {})
        require(isinstance(bindings, dict), "boundary_bindings: expected object")
        for port_id, endpoint in bindings.items():
            if not partial:
                require(port_id in ports, f"Unknown boundary port: {port_id}")
            validate_endpoint(endpoint, children)
    require(isinstance(data.get("external_refs", []), list), "external_refs: expected list")
    for ref in data.get("external_refs", []):
        shape(ref, {"provider", "id", "relation", "scope"},
              {"requested_version", "resolved_version", "uri", "retrieved_at", "sha256"}, "external_ref")
        require(one_of(ref["relation"], {"exact", "partial", "aggregate_contains", "candidate"}), "external_ref.relation")
        require(all(isinstance(v, str) and v.strip() for v in ref.values()), "external_ref fields must be nonempty strings")


def validate_endpoint(endpoint, children):
    shape(endpoint, {"instance", "port"}, set(), "endpoint")
    require(identifier(endpoint["instance"]) and endpoint["instance"] in children and identifier(endpoint["port"]), "Invalid child endpoint")


class Catalog:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def init(self):
        source_root = Path(__file__).resolve().parent.parent
        require(not self.root.is_relative_to(source_root), "Workspace must be outside the source package directory", "WORKSPACE_LOCATION")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock():
            marker = self.root / "catalog.json"
            if marker.exists():
                self.check()
                return {"workspace": str(self.root), "created": False}
            require(not any((self.root / p).exists() for p in ("modules", "index")),
                    "modules/index already exists without a catalog marker", "WORKSPACE_CONFLICT")
            atomic_json(marker, {"schema_version": VERSION, "kind": "recursive-process-catalog"})
        return {"workspace": str(self.root), "created": True}

    def check(self):
        require((self.root / "catalog.json").is_file(), "Run init for this workspace first", "NOT_INITIALIZED")
        require(read_json(self.root / "catalog.json") == {"schema_version": VERSION, "kind": "recursive-process-catalog"},
                "Unsupported workspace marker", "WORKSPACE_VERSION")

    @contextmanager
    def lock(self):
        path = self.root / ".catalog.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise CatalogError("WORKSPACE_BUSY", "Writer lock exists; retry after the writer finishes. Inspect a stale lock before removing it.") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            path.unlink()

    def revisions(self, module_id):
        require(identifier(module_id), "Invalid module id")
        return sorted(int(p.stem) for p in (self.root / "modules" / module_id).glob("*.json")
                      if re.fullmatch(r"[0-9]{8}", p.stem))

    def get(self, module_id, revision=None):
        self.check()
        versions = self.revisions(module_id)
        require(bool(versions), f"Unknown module: {module_id}", "NOT_FOUND")
        rev = versions[-1] if revision is None else revision
        require(type(rev) is int and rev in versions, f"Missing exact revision: {module_id}@{rev}", "NOT_FOUND")
        envelope = read_json(self.root / "modules" / module_id / f"{rev:08d}.json")
        require(isinstance(envelope, dict) and isinstance(envelope.get("card"), dict), "Malformed saved record", "INTEGRITY_ERROR")
        card = envelope["card"]
        require(card.get("module_id") == module_id and card.get("revision") == rev and digest(card) == envelope.get("content_hash"),
                f"Saved content changed: {module_id}@{rev}", "INTEGRITY_ERROR")
        data = validate_card(card)
        return {**envelope, "effective": data, "field_origins": effective(card)[1]}

    def _check_references(self, card, data):
        ports = keyed(data["interfaces"], "interfaces")
        for rep in data["representations"]:
            child_ports = {}
            for child in rep.get("children", []):
                require((child["module_id"], child["revision"]) != (card["module_id"], card["revision"]),
                        "A representation cannot include its own revision", "CONTAINMENT_CYCLE")
                loaded = self.get(child["module_id"], child["revision"])["effective"]
                require(child["representation"] in keyed(loaded["representations"], "representations"), "Missing child representation", "NOT_FOUND")
                child_ports[child["id"]] = keyed(loaded["interfaces"], "interfaces")
            def endpoint(ref):
                require(ref["port"] in child_ports[ref["instance"]], f"Missing child port: {ref}")
                return child_ports[ref["instance"]][ref["port"]]
            for edge in rep.get("connections", []):
                start, end = endpoint(edge["from"]), endpoint(edge["to"])
                require(start["direction"] == "output" and end["direction"] == "input", "Connection must be output -> input")
                require(start["kind"] == end["kind"], "Connection kinds differ")
            for key, ref in rep.get("boundary_bindings", {}).items():
                inner = endpoint(ref)
                require(ports[key]["direction"] == inner["direction"] and ports[key]["kind"] == inner["kind"], "Boundary direction/kind mismatch")

    def _save(self, card, expected, change=None):
        data = validate_card(card)
        require(type(expected) is int and expected >= 0, "expect-revision must be a nonnegative integer")
        versions = self.revisions(card["module_id"])
        head = versions[-1] if versions else 0
        require(head == expected, f"Expected revision {expected}, actual {head}", "REVISION_CONFLICT")
        require(card["revision"] == head + 1 and card["revision"] < 100000000, "New revision must be current + 1")
        self._check_references(card, data)
        envelope = {"card": card, "content_hash": digest(card), "saved_at": datetime.now(timezone.utc).isoformat(), "change": change}
        path = self.root / "modules" / card["module_id"] / f"{card['revision']:08d}.json"
        require(not path.exists(), "Revision already exists", "REVISION_CONFLICT")
        atomic_json(path, envelope)
        return {"module_id": card["module_id"], "revision": card["revision"], "content_hash": envelope["content_hash"], "path": str(path), "change": change}

    def save(self, card, expected):
        self.check()
        with self.lock():
            return self._save(card, expected)

    def refresh(self, module_id, payload, expected):
        self.check()
        shape(payload, {"extracted", "sources"}, set(), "refresh")
        with self.lock():
            current = self.get(module_id)["card"]
            require(current["revision"] == expected, "Source refresh based on stale revision", "REVISION_CONFLICT")
            updated = copy.deepcopy(current)
            sources = keyed(updated["sources"], "sources")
            for key, source in keyed(payload["sources"], "sources").items():
                require(key not in sources or sources[key] == source, f"Source {key} changed; use a new source id for the new observation", "SOURCE_CONFLICT")
                sources[key] = source
            change = {"kind": "source_refresh", "base_revision": expected,
                      **refresh_change(current, payload["extracted"])}
            updated.update(revision=expected + 1, extracted=payload["extracted"], sources=list(sources.values()))
            return self._save(updated, expected, change)

    def heads(self):
        self.check()
        # An interrupted first save can leave a directory without a committed revision.
        return [self.get(p.name) for p in sorted((self.root / "modules").glob("*"))
                if p.is_dir() and self.revisions(p.name)]

    def reindex(self):
        self.check()
        with self.lock():
            records = self.heads()
            snapshot = {r["card"]["module_id"]: r["content_hash"] for r in records}
            index = {"schema_version": VERSION, "snapshot": snapshot,
                     "rows": [{"module_id": r["card"]["module_id"], "revision": r["card"]["revision"],
                               "data": r["effective"], "field_origins": r["field_origins"]} for r in records]}
            index["rows_hash"] = digest(index["rows"])
            atomic_json(self.root / "index" / "catalog.json", index)
        return index

    def index(self):
        records = self.heads()
        snapshot = {r["card"]["module_id"]: r["content_hash"] for r in records}
        path = self.root / "index" / "catalog.json"
        if path.exists():
            try:
                cache = read_json(path)
                if (isinstance(cache, dict) and cache.get("schema_version") == VERSION
                        and cache.get("snapshot") == snapshot and isinstance(cache.get("rows"), list)
                        and cache.get("rows_hash") == digest(cache["rows"])):
                    return cache
            except CatalogError:
                pass  # A disposable index is rebuilt; damaged source records still fail above.
        return self.reindex()

    def search(self, query="", function="", category="", input_term="", output_term=""):
        results = []
        for row in self.index()["rows"]:
            data = row["data"]
            fields = {k: data.get(k, []) for k in ("name", "summary", "aliases", "functions", "categories", "industries")}
            for direction in ("input", "output"):
                fields[direction] = [t for p in data["interfaces"] if p["direction"] == direction for t in p["terms"]]
            reasons = []
            filters = [(None, token) for token in normalize(query).split()]
            filters += [(key, value) for key, value in (("functions", function), ("categories", category), ("input", input_term), ("output", output_term)) if value]
            for key, value in filters:
                matched = [k for k, values in fields.items() if (key is None or key == k)
                           and any(normalize(value) in normalize(v) for v in ([values] if isinstance(values, str) else values))]
                if not matched:
                    break
                reasons.append({"query": value, "fields": matched})
            else:
                results.append({**row, "match_reasons": reasons, "score": sum(len(r["fields"]) for r in reasons)})
        return sorted(results, key=lambda r: (-r["score"], r["module_id"]))

    def _revision_inventory(self):
        """Read saved revisions and follow fixed child references from every latest card."""
        self.check()
        records, latest = {}, set()
        for directory in sorted((self.root / "modules").glob("*")):
            if not directory.is_dir():
                continue
            revisions = self.revisions(directory.name)
            for revision in revisions:
                records[(directory.name, revision)] = self.get(directory.name, revision)
            if revisions:
                latest.add((directory.name, revisions[-1]))
        reachable, pending = set(), list(latest)
        while pending:
            key = pending.pop()
            if key in reachable or key not in records:
                continue
            reachable.add(key)
            for rep in records[key]["effective"]["representations"]:
                pending.extend((child["module_id"], child["revision"])
                               for child in rep.get("children", []))
        return records, latest, reachable

    def uses_source(self, provider, source_id, version=None):
        """Find saved module revisions that explicitly map to one external source."""
        require(isinstance(provider, str) and provider.strip() and
                isinstance(source_id, str) and source_id.strip(),
                "provider and source ID are required")
        records, latest, reachable = self._revision_inventory()
        matches = []
        for (module_id, revision_number), record in records.items():
            for ref in record["effective"].get("external_refs", []):
                actual_version = ref.get("resolved_version") or ref.get("requested_version")
                if ref["provider"] != provider or ref["id"] != source_id:
                    continue
                if version is not None and actual_version != version:
                    continue
                key = (module_id, revision_number)
                matches.append({"module_id": module_id,
                                "revision": revision_number,
                                "is_latest": key in latest,
                                "reachable_from_latest": key in reachable,
                                "name": record["effective"]["name"],
                                "reference": ref,
                                "field_origin": record["field_origins"].get("external_refs")})
        return matches

    def uses_flow(self, provider, flow_id, version=None):
        """Find interfaces citing a flow; unversioned references remain possible matches."""
        require(isinstance(provider, str) and provider.strip() and
                isinstance(flow_id, str) and flow_id.strip(),
                "provider and flow ID are required")
        require(version is None or isinstance(version, str) and version.strip(),
                "version must be nonempty text")
        records, latest, reachable = self._revision_inventory()
        matches = []
        for (module_id, revision_number), record in records.items():
            for port in record["effective"]["interfaces"]:
                for ref in port.get("flow_refs", []):
                    if ref["provider"] != provider or ref["id"] != flow_id:
                        continue
                    ref_version = ref.get("version")
                    if version is not None and ref_version not in (None, version):
                        continue
                    key = (module_id, revision_number)
                    matches.append({"module_id": module_id,
                                    "revision": revision_number,
                                    "is_latest": key in latest,
                                    "reachable_from_latest": key in reachable,
                                    "name": record["effective"]["name"],
                                    "interface": {"id": port["id"], "direction": port["direction"],
                                                  "kind": port["kind"]},
                                    "flow_ref": ref,
                                    "version_match": ("not_checked" if version is None else
                                                      "unresolved" if ref_version is None else "exact"),
                                    "field_origin": record["field_origins"].get("interfaces")})
        return matches

    def uses_module(self, module_id, revision=None):
        """Find saved parent revisions directly pinned to a child module revision."""
        require(identifier(module_id), "Invalid module id")
        require(revision is None or integer(revision), "revision must be a positive integer")
        records, latest, reachable = self._revision_inventory()
        matches = []
        for (parent_id, parent_revision), record in records.items():
            for rep in record["effective"]["representations"]:
                for child in rep.get("children", []):
                    if child["module_id"] != module_id or (revision is not None and child["revision"] != revision):
                        continue
                    key = (parent_id, parent_revision)
                    matches.append({"parent_module_id": parent_id,
                                    "parent_revision": parent_revision,
                                    "parent_is_latest": key in latest,
                                    "parent_reachable_from_latest": key in reachable,
                                    "parent_name": record["effective"]["name"],
                                    "representation": rep["id"],
                                    "instance": child["id"],
                                    "child_revision": child["revision"],
                                    "child_representation": child["representation"],
                                    "field_origin": record["field_origins"].get("representations")})
        return matches

    def locations(self, module_id, revision=None):
        """Return direct source pointers and possible positions in saved module expressions."""
        selected = self.get(module_id, revision)
        records, latest, _ = self._revision_inventory()
        from .locations import occurrence_index
        occurrences, diagnostics = occurrence_index(records, latest)
        key = (module_id, selected["card"]["revision"])
        return {"module_id": module_id, "revision": key[1],
                "sources": selected["card"]["sources"],
                "external_refs": selected["effective"].get("external_refs", []),
                "occurrences": occurrences.get(key, []),
                "diagnostics": diagnostics,
                "status": "partial" if diagnostics else "complete",
                "scope": "All expressions of current module heads; child revisions and expressions are pinned. Scheme selection unassessed."}

    def save_candidate(self, candidate, expected_revision):
        from .selections import CandidateStore
        return CandidateStore(self).save(candidate, expected_revision)

    def get_candidate(self, candidate_id, revision=None):
        from .selections import CandidateStore
        return CandidateStore(self).get(candidate_id, revision)

    def candidate_revisions(self, candidate_id):
        from .selections import CandidateStore
        self.check()
        return CandidateStore(self).revisions(candidate_id)

    def expand(self, module_id, revision=None, representation=None, depth=5, max_nodes=500):
        require(type(depth) is int and 0 <= depth <= 32, "depth must be 0..32")
        require(type(max_nodes) is int and 1 <= max_nodes <= 10000, "max-nodes must be 1..10000")
        diagnostics, visited = [], 0
        def visit(mid, rev, rep_id, path, stack, remaining):
            nonlocal visited
            if visited >= max_nodes:
                diagnostics.append({"path": path, "code": "NODE_LIMIT"})
                return {"path": path, "truncated": True}
            visited += 1
            record = self.get(mid, rev)
            data = record["effective"]
            selected = rep_id or data["representations"][0]["id"]
            reps = keyed(data["representations"], "representations")
            require(selected in reps, f"Missing representation {mid}:{selected}", "NOT_FOUND")
            rep = reps[selected]
            identity = (mid, record["card"]["revision"], selected)
            node = {"path": path, "module_id": mid, "revision": identity[1], "representation": selected,
                    "name": data["name"], "interfaces": data["interfaces"], "unknowns": data["unknowns"],
                    "connections": rep.get("connections", []), "boundary_bindings": rep.get("boundary_bindings", {}), "children": []}
            if identity in stack:
                diagnostics.append({"path": path, "code": "CONTAINMENT_CYCLE"})
            elif rep.get("children") and remaining == 0:
                diagnostics.append({"path": path, "code": "DEPTH_LIMIT"})
            else:
                for child in rep.get("children", []):
                    node["children"].append(visit(child["module_id"], child["revision"], child["representation"],
                                                  path + "/" + child["id"], stack | {identity}, remaining - 1))
                    if visited >= max_nodes:
                        if len(node["children"]) < len(rep["children"]):
                            diagnostics.append({"path": path, "code": "NODE_LIMIT"})
                        break
            return node
        tree = visit(module_id, revision, representation, module_id, set(), depth)
        return {"tree": tree, "nodes": visited, "diagnostics": diagnostics, "status": "partial" if diagnostics else "complete"}

    def candidates(self, module_id, port_id, revision=None):
        record = self.get(module_id, revision)
        ports = keyed(record["effective"]["interfaces"], "interfaces")
        require(port_id in ports, "Select an existing input or output port")
        selected = ports[port_id]
        if selected["kind"] == "environment":
            return []
        found = []
        for row in self.index()["rows"]:
            for port in row["data"]["interfaces"]:
                if port["direction"] == selected["direction"] or port["kind"] == "environment":
                    continue
                output, target = (selected, port) if selected["direction"] == "output" else (port, selected)
                output_row, target_row = (record, row) if selected["direction"] == "output" else (row, record)
                if output["kind"] != target["kind"] and "unspecified" not in {output["kind"], target["kind"]}:
                    continue
                shared_terms = sorted({normalize(t) for t in output["terms"]} & {normalize(t) for t in target["terms"]})
                flow_matches, flow_conflicts = compare_flow_refs(output, target)
                shared_refs = sorted({(ref["provider"], ref["id"]) for ref in flow_matches})
                if "unspecified" in {output["kind"], target["kind"]} and not shared_refs:
                    continue
                if not shared_terms and not shared_refs:
                    continue
                checks = check_requirements(output, target)
                output_data = output_row.get("effective", output_row.get("data"))
                target_data = target_row.get("effective", target_row.get("data"))
                unknowns = list(output_data["unknowns"])
                unknowns += output.get("unknowns", [])
                unknowns += target_data["unknowns"]
                unknowns += target.get("unknowns", [])
                if any(ref["version_match"] == "unresolved" for ref in flow_matches):
                    unknowns.append("连接流的版本至少有一端未明确")
                if flow_conflicts:
                    unknowns.append("两端有相同流 ID 但明确版本不同；术语连接仍需核对流版本")
                if "unspecified" in {output["kind"], target["kind"]}:
                    unknowns.append("连接接口类别至少有一端未明确")
                if not checks:
                    unknowns.append("接收接口尚未声明可检查条件")
                unknowns = list(dict.fromkeys(unknowns))
                states = [c["status"] for c in checks]
                status = "not_satisfied" if "not_satisfied" in states else (
                    "unknown" if not checks or "unknown" in states or unknowns else "satisfied")
                from_id, from_rev = ((module_id, record["card"]["revision"]) if selected["direction"] == "output"
                                     else (row["module_id"], row["revision"]))
                to_id, to_rev = ((row["module_id"], row["revision"]) if selected["direction"] == "output"
                                 else (module_id, record["card"]["revision"]))
                found.append({"from": {"module_id": from_id, "revision": from_rev, "port": output["id"]},
                              "to": {"module_id": to_id, "revision": to_rev, "port": target["id"]},
                              "query": {"module_id": module_id, "revision": record["card"]["revision"],
                                        "port": port_id, "direction": selected["direction"]},
                              "match_reasons": {"shared_terms": shared_terms, "shared_flow_ids": shared_refs,
                                                "shared_flow_refs": flow_matches,
                                                "conflicting_flow_refs": flow_conflicts},
                              "condition_status": status, "checks": checks, "unknowns": unknowns,
                              "provenance": {"from_interfaces": output_row["field_origins"]["interfaces"],
                                             "to_interfaces": target_row["field_origins"]["interfaces"]},
                              "scope": "Declared scalar requirements only; candidate connection, engineering feasibility unassessed"})
        if found:
            records, latest, _ = self._revision_inventory()
            from .locations import occurrence_index
            occurrences, diagnostics = occurrence_index(records, latest)
            for candidate in found:
                for endpoint in (candidate["from"], candidate["to"]):
                    key = (endpoint["module_id"], endpoint["revision"])
                    location_record = records[key]
                    endpoint["sources"] = location_record["card"]["sources"]
                    endpoint["occurrences"] = occurrences.get(key, [])
                candidate["location_status"] = "partial" if diagnostics else "complete"
                candidate["location_scope"] = "All current module expressions; scheme selection unassessed"
                if diagnostics:
                    candidate["location_diagnostics"] = diagnostics
        return found


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def compare_flow_refs(output, target):
    """Separate version-compatible flow references from explicit conflicts."""
    left = {(r["provider"], r["id"], r.get("version")) for r in output.get("flow_refs", [])}
    right = {(r["provider"], r["id"], r.get("version")) for r in target.get("flow_refs", [])}
    matches, conflicts = [], []
    for provider, flow_id, output_version in sorted(left, key=lambda r: (r[0], r[1], r[2] or "")):
        for other_provider, other_id, input_version in sorted(right, key=lambda r: (r[0], r[1], r[2] or "")):
            if (provider, flow_id) != (other_provider, other_id):
                continue
            item = {"provider": provider, "id": flow_id,
                    "output_version": output_version, "input_version": input_version}
            if output_version and input_version and output_version != input_version:
                conflicts.append(item)
            else:
                item["version_match"] = "exact" if output_version and input_version else "unresolved"
                matches.append(item)
    return matches, conflicts


def check_requirements(output, target):
    properties = {p["key"]: p for p in output.get("properties", [])}
    checks = []
    for rule in target.get("requirements", []):
        actual = properties.get(rule["key"])
        item = {"requirement": rule, "actual": actual, "status": "unknown"}
        if actual is None or actual["value"] is None or rule["value"] is None:
            item["reason"] = "missing_value"
        elif (type(actual["value"]) in (int, float) or type(rule["value"]) in (int, float)) and (
                not actual["unit"].strip() or not rule["unit"].strip()
                or not actual["basis"].strip() or not rule["basis"].strip()):
            item["reason"] = "missing_unit_or_basis"
        elif (actual["unit"], actual["basis"]) != (rule["unit"], rule["basis"]):
            item["reason"] = "unit_or_basis_mismatch"
        else:
            left, right = actual["value"], rule["value"]
            numeric = type(left) in (int, float) and type(right) in (int, float)
            if not numeric and not (isinstance(left, str) and isinstance(right, str) and rule["op"] == "eq"):
                item["reason"] = "incomparable_values"
            else:
                passed = {"eq": lambda: left == right, "le": lambda: left <= right, "ge": lambda: left >= right}[rule["op"]]()
                item.update(status="satisfied" if passed else "not_satisfied", reason="scalar_comparison")
        checks.append(item)
    return checks


def main(argv=None):
    # Keep the machine-readable boundary UTF-8 on Windows as well as Unix.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="Data workspace, separate from source code")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    p = sub.add_parser("validate"); p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("save"); p.add_argument("--file", type=Path, required=True); p.add_argument("--expect-revision", type=int, required=True)
    for command in ("get", "history", "expand", "candidates", "locations", "refresh"):
        p = sub.add_parser(command); p.add_argument("module_id")
        if command not in ("history", "refresh"):
            p.add_argument("--revision", type=int)
        if command == "expand":
            p.add_argument("--representation"); p.add_argument("--depth", type=int, default=5); p.add_argument("--max-nodes", type=int, default=500)
        if command == "candidates":
            p.add_argument("--port", required=True)
        if command == "refresh":
            p.add_argument("--file", type=Path, required=True); p.add_argument("--expect-revision", type=int, required=True)
    p = sub.add_parser("search")
    for name in ("query", "function", "category", "input", "output"):
        p.add_argument("--" + name, default="")
    sub.add_parser("reindex")
    p = sub.add_parser("fragment")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("candidate-save")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--expect-revision", type=int, required=True)
    p = sub.add_parser("candidate-get")
    p.add_argument("candidate_id")
    p.add_argument("--revision", type=int)
    p = sub.add_parser("candidate-history")
    p.add_argument("candidate_id")
    p = sub.add_parser("uses-source")
    p.add_argument("--provider", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--version")
    p = sub.add_parser("uses-flow")
    p.add_argument("--provider", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--version")
    p = sub.add_parser("uses-module")
    p.add_argument("module_id")
    p.add_argument("--revision", type=int)
    p = sub.add_parser("inspect-tiangong-model")
    p.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            card = read_json(args.file); validate_card(card)
            result = {"module_id": card["module_id"], "revision": card["revision"], "valid": True,
                      "scope": "Card structure only; workspace references checked on save"}
        elif args.command == "inspect-tiangong-model":
            from .tiangong_model import inspect_tiangong_model
            result = inspect_tiangong_model(read_json(args.file))
        elif args.command == "fragment":
            from .fragments import fragment_card
            card = fragment_card(read_json(args.file))
            if args.apply:
                require(args.workspace is not None, "--workspace is required for --apply", "WORKSPACE_REQUIRED")
                result = Catalog(args.workspace).save(card, 0)
            else:
                result = {"card": card, "valid": True,
                          "scope": "v1 source-located fragment preview; no process or LCA feasibility assertion"}
        else:
            require(args.workspace is not None, "--workspace is required", "WORKSPACE_REQUIRED")
            catalog = Catalog(args.workspace)
            cmd = args.command
            if cmd == "init": result = catalog.init()
            elif cmd == "save": result = catalog.save(read_json(args.file), args.expect_revision)
            elif cmd == "get": result = catalog.get(args.module_id, args.revision)
            elif cmd == "history":
                catalog.check(); result = [catalog.get(args.module_id, r) for r in catalog.revisions(args.module_id)]
            elif cmd == "refresh": result = catalog.refresh(args.module_id, read_json(args.file), args.expect_revision)
            elif cmd == "reindex":
                index = catalog.reindex(); result = {"count": len(index["rows"]), "snapshot": index["snapshot"]}
            elif cmd == "search": result = catalog.search(args.query, args.function, args.category, args.input, args.output)
            elif cmd == "uses-source": result = catalog.uses_source(args.provider, args.id, args.version)
            elif cmd == "uses-flow": result = catalog.uses_flow(args.provider, args.id, args.version)
            elif cmd == "uses-module": result = catalog.uses_module(args.module_id, args.revision)
            elif cmd == "expand": result = catalog.expand(args.module_id, args.revision, args.representation, args.depth, args.max_nodes)
            elif cmd == "candidates": result = catalog.candidates(args.module_id, args.port, args.revision)
            elif cmd == "locations": result = catalog.locations(args.module_id, args.revision)
            elif cmd == "candidate-save": result = catalog.save_candidate(read_json(args.file), args.expect_revision)
            elif cmd == "candidate-get": result = catalog.get_candidate(args.candidate_id, args.revision)
            elif cmd == "candidate-history": result = catalog.candidate_revisions(args.candidate_id)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (CatalogError, OSError, ValueError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": {"code": getattr(exc, "code", "IO_OR_FORMAT_ERROR"), "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
