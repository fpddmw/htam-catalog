"""Immutable records of selected process uses and proposed relationships."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import re

from .catalog import (CatalogError, atomic_json, digest, identifier, integer,
                      keyed, one_of, read_json, require, shape, words)


RELATIONS = {"supply", "receive", "substitute", "insert"}
BASES = {"observed", "inferred", "hypothesis"}


class CandidateStore:
    def __init__(self, catalog):
        self.catalog = catalog

    def revisions(self, candidate_id):
        require(identifier(candidate_id), "Invalid candidate id")
        return sorted(int(path.stem) for path in
                      (self.catalog.root / "candidates" / candidate_id).glob("*.json")
                      if re.fullmatch(r"[0-9]{8}", path.stem))

    def get(self, candidate_id, revision=None):
        self.catalog.check()
        versions = self.revisions(candidate_id)
        require(bool(versions), f"Unknown candidate: {candidate_id}", "NOT_FOUND")
        selected = versions[-1] if revision is None else revision
        require(type(selected) is int and selected in versions,
                f"Missing exact candidate revision: {candidate_id}@{selected}", "NOT_FOUND")
        path = self.catalog.root / "candidates" / candidate_id / f"{selected:08d}.json"
        envelope = read_json(path)
        require(isinstance(envelope, dict) and isinstance(envelope.get("candidate"), dict),
                "Malformed candidate record", "INTEGRITY_ERROR")
        candidate = envelope["candidate"]
        require(candidate.get("candidate_id") == candidate_id
                and candidate.get("revision") == selected
                and digest(candidate) == envelope.get("content_hash"),
                f"Saved candidate changed: {candidate_id}@{selected}", "INTEGRITY_ERROR")
        self._validate(candidate, check_refs=False)
        return envelope

    def save(self, value, expected_revision):
        self.catalog.check()
        require(type(expected_revision) is int and expected_revision >= 0,
                "expect-revision must be a nonnegative integer")
        candidate = copy.deepcopy(value)
        self._validate(candidate, check_refs=True)
        with self.catalog.lock():
            revisions = self.revisions(candidate["candidate_id"])
            head = revisions[-1] if revisions else 0
            require(head == expected_revision, f"Expected candidate revision {expected_revision}, actual {head}",
                    "REVISION_CONFLICT")
            require(candidate["revision"] == head + 1 and candidate["revision"] < 100000000,
                    "New candidate revision must be current + 1")
            path = (self.catalog.root / "candidates" / candidate["candidate_id"]
                    / f"{candidate['revision']:08d}.json")
            require(not path.exists(), "Candidate revision already exists", "REVISION_CONFLICT")
            envelope = {"candidate": candidate, "content_hash": digest(candidate),
                        "saved_at": datetime.now(timezone.utc).isoformat()}
            atomic_json(path, envelope)
        return {"candidate_id": candidate["candidate_id"], "revision": candidate["revision"],
                "content_hash": envelope["content_hash"], "path": str(path)}

    def _validate(self, candidate, *, check_refs):
        shape(candidate, {"schema_version", "candidate_id", "revision", "uses",
                          "relations", "unknowns"}, {"summary"}, "candidate")
        require(type(candidate["schema_version"]) is int and candidate["schema_version"] == 1,
                "Unsupported candidate schema_version")
        require(identifier(candidate["candidate_id"]), "candidate_id: invalid")
        require(integer(candidate["revision"]), "candidate.revision: positive integer required")
        if "summary" in candidate:
            require(isinstance(candidate["summary"], str) and bool(candidate["summary"].strip()),
                    "candidate.summary: nonempty text required")
        words(candidate["unknowns"], "candidate.unknowns")
        uses = keyed(candidate["uses"], "candidate.uses")
        require(bool(uses), "candidate.uses: at least one use is required")
        source_ids = {}
        ports = {}
        for use in uses.values():
            shape(use, {"id", "module_id", "revision", "representation"},
                  {"content_hash", "origin_position"}, "candidate.use")
            require(identifier(use["module_id"]) and integer(use["revision"])
                    and identifier(use["representation"]), "candidate.use: invalid fixed reference")
            if "content_hash" in use:
                require(isinstance(use["content_hash"], str)
                        and re.fullmatch(r"[a-f0-9]{64}", use["content_hash"]) is not None,
                        "candidate.use.content_hash: expected SHA-256")
            if "origin_position" in use:
                position = use["origin_position"]
                shape(position, {"root_module_id", "root_revision", "root_representation", "path"},
                      set(), "candidate.use.origin_position")
                require(identifier(position["root_module_id"]) and integer(position["root_revision"])
                        and identifier(position["root_representation"])
                        and isinstance(position["path"], str) and bool(position["path"].strip()),
                        "candidate.use.origin_position: invalid location")
            if check_refs:
                record = self.catalog.get(use["module_id"], use["revision"])
                require(use["representation"] in keyed(record["effective"]["representations"], "representations"),
                        "candidate.use: missing fixed representation", "NOT_FOUND")
                if "content_hash" in use:
                    require(use["content_hash"] == record["content_hash"],
                            "candidate.use: module content changed", "INTEGRITY_ERROR")
                else:
                    use["content_hash"] = record["content_hash"]
                if "origin_position" in use:
                    self._check_position(use)
                source_ids[use["id"]] = {item["id"] for item in record["card"]["sources"]}
                ports[use["id"]] = keyed(record["effective"]["interfaces"], "interfaces")
        relations = keyed(candidate["relations"], "candidate.relations")
        for relation in relations.values():
            shape(relation, {"id", "kind", "basis", "reason"},
                  {"from", "to", "evidence", "unknowns"}, "candidate.relation")
            require(one_of(relation["kind"], RELATIONS), "candidate.relation.kind")
            require(one_of(relation["basis"], BASES), "candidate.relation.basis")
            require(isinstance(relation["reason"], str) and bool(relation["reason"].strip()),
                    "candidate.relation.reason: nonempty text required")
            require("from" in relation or "to" in relation,
                    "candidate.relation: specify at least one use")
            words(relation.get("unknowns", []), "candidate.relation.unknowns")
            for side in ("from", "to"):
                if side not in relation:
                    continue
                endpoint = relation[side]
                shape(endpoint, {"use_id"}, {"port_id"}, f"candidate.relation.{side}")
                require(identifier(endpoint["use_id"]) and endpoint["use_id"] in uses,
                        f"candidate.relation.{side}: unknown use")
                if "port_id" in endpoint:
                    require(identifier(endpoint["port_id"]), f"candidate.relation.{side}: invalid port")
                    if check_refs:
                        require(endpoint["port_id"] in ports[endpoint["use_id"]],
                                f"candidate.relation.{side}: missing port", "NOT_FOUND")
                        if relation["kind"] in {"supply", "receive"}:
                            direction = "output" if side == "from" else "input"
                            require(ports[endpoint["use_id"]][endpoint["port_id"]]["direction"] == direction,
                                    f"candidate.relation.{side}: expected {direction} port")
            evidence = relation.get("evidence", [])
            require(isinstance(evidence, list), "candidate.relation.evidence: expected list")
            if relation["basis"] == "observed":
                require(bool(evidence), "Observed relation requires cited evidence")
            for item in evidence:
                shape(item, {"use_id", "source_id", "locator"}, {"note"}, "candidate.relation.evidence")
                require(identifier(item["use_id"]) and item["use_id"] in uses,
                        "candidate.relation.evidence: unknown use")
                require(isinstance(item["source_id"], str) and bool(item["source_id"].strip())
                        and isinstance(item["locator"], str) and bool(item["locator"].strip()),
                        "candidate.relation.evidence: source and locator required")
                require("note" not in item or isinstance(item["note"], str),
                        "candidate.relation.evidence.note: expected text")
                if check_refs:
                    require(item["source_id"] in source_ids[item["use_id"]],
                            "candidate.relation.evidence: source not present in referenced module")

    def _check_position(self, use):
        position = use["origin_position"]
        parts = position["path"].split("/")
        require(parts[0] == position["root_module_id"] and all(identifier(part) for part in parts),
                "candidate.use.origin_position: path must begin with the root module")
        module_id = position["root_module_id"]
        revision = position["root_revision"]
        representation = position["root_representation"]
        for instance in parts[1:]:
            record = self.catalog.get(module_id, revision)
            expressions = keyed(record["effective"]["representations"], "representations")
            require(representation in expressions, "origin position: missing expression", "NOT_FOUND")
            children = keyed(expressions[representation].get("children", []), "children")
            require(instance in children, "origin position: missing child instance", "NOT_FOUND")
            child = children[instance]
            module_id, revision, representation = (child["module_id"], child["revision"],
                                                    child["representation"])
        self.catalog.get(module_id, revision)
        require((module_id, revision, representation)
                == (use["module_id"], use["revision"], use["representation"]),
                "candidate.use.origin_position: selected use differs from located instance")
