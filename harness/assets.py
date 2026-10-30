"""Versioned, local reuse records; registration never adopts a scientific result.

Asset snapshots and scoped reuse records live in the existing business database.
Reading compares only captured local evidence and declared dependencies. It does
not discover remote updates or change a work's scientific/completion assessment.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .common import HarnessError, digest, encoded, local_path, now, require, validation_scope


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS assets(
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
        current_version TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS asset_versions(
        asset_id TEXT NOT NULL REFERENCES assets(id), version TEXT NOT NULL,
        body TEXT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL,
        at TEXT NOT NULL, PRIMARY KEY(asset_id,version))""",
    """CREATE TABLE IF NOT EXISTS asset_revisions(
        asset_id TEXT NOT NULL REFERENCES assets(id), revision INTEGER NOT NULL,
        version TEXT NOT NULL, annotations TEXT NOT NULL, source TEXT NOT NULL,
        at TEXT NOT NULL, PRIMARY KEY(asset_id,revision),
        FOREIGN KEY(asset_id,version) REFERENCES asset_versions(asset_id,version))""",
    """CREATE TABLE IF NOT EXISTS asset_uses(
        id INTEGER PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
        requirement TEXT NOT NULL, requirement_revision INTEGER NOT NULL,
        request_revision INTEGER NOT NULL, asset_id TEXT NOT NULL,
        version TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL,
        at TEXT NOT NULL,
        FOREIGN KEY(asset_id,version) REFERENCES asset_versions(asset_id,version))""",
    "CREATE INDEX IF NOT EXISTS asset_uses_work ON asset_uses(work_id,requirement)",
)

USE_STATES = {"unverified", "unreviewed", "available", "conditional", "unavailable", "needs_review",
              "reused", "adopted", "rejected", "verified", "reviewed"}
LIFECYCLE = {"draft", "available", "conditional", "reviewed", "retired", "retracted", "superseded"}
RELATIONS = {'supports', 'challenges', 'derived_from', 'applies_to'}


def text_match(query, value):
    """Literal identities/Chinese phrases, with conjunction for spaced terms."""
    text, needle = encoded(value).casefold(), query.strip().casefold()
    return not needle or needle in text or all(term in text for term in needle.split())


def _text(value, label):
    require(isinstance(value, str) and bool(value.strip()), label + " must be nonempty text")
    return value


class AssetRegistry:
    def __init__(self, state):
        self.state = state
        self.db = state.db
        self.root = state.root

    def _present(self):
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {"assets", "asset_versions", "asset_revisions", "asset_uses"} <= tables

    def _ensure_schema(self):
        require(not self.state.readonly, "Read-only asset registry cannot modify business state")
        # Individual statements participate in State.transaction; executescript
        # would implicitly commit an enclosing business-state transaction.
        for statement in SCHEMA:
            self.db.execute(statement)

    def _row(self, identifier, version=None):
        _text(identifier, "Asset id")
        require(self._present(), "Unknown asset: " + identifier)
        head = self.db.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone()
        require(head is not None, "Unknown asset: " + identifier)
        version = head["current_version"] if version is None else _text(version, "Asset version")
        snapshot = self.db.execute("SELECT * FROM asset_versions WHERE asset_id=? AND version=?",
                                   (identifier, version)).fetchone()
        require(snapshot is not None, "Unknown asset version: " + identifier + "@" + version)
        revision = self.db.execute("SELECT * FROM asset_revisions WHERE asset_id=? AND version=? ORDER BY revision DESC LIMIT 1",
                                   (identifier, version)).fetchone()
        return dict(head), dict(snapshot), dict(revision)

    def _bind_evidence(self, references, label, required=False):
        require(isinstance(references, list), label + " must be a list")
        require(not required or bool(references), label + " needs exact local evidence")
        bound = []
        for supplied in references:
            require(isinstance(supplied, dict), label + " reference must be an object")
            ref = deepcopy(supplied)
            path = local_path(self.root, _text(ref.get("path"), label + " path"))
            require(path.is_file(), label + " file is unavailable: " + str(path.relative_to(self.root)))
            actual = digest(path)
            require(not ref.get("sha256") or ref["sha256"] == actual, label + " content changed")
            ref.update(path=str(path.relative_to(self.root)), sha256=actual)
            bound.append(ref)
        return bound

    def _bind_uses(self, uses):
        require(isinstance(uses, list), "Asset uses must be a list")
        bound = []
        for supplied in uses:
            require(isinstance(supplied, dict), "Asset use must be an object")
            use = deepcopy(supplied)
            _text(use.get("purpose"), "Asset use purpose")
            require(use.get("state") in USE_STATES, "Invalid asset use state")
            require(bool(use.get("scope")), "Asset use needs an explicit applicability scope")
            use["evidence"] = self._bind_evidence(use.get("evidence", []), "Asset use evidence",
                use["state"] in {"available", "conditional", "adopted", "verified", "reviewed"})
            bound.append(use)
        return bound

    def _bind_body(self, identifier, supplied):
        require(isinstance(supplied, dict), "Asset body must be an object")
        body = json.loads(encoded(supplied))
        if 'applies' in body:
            from .experiences import context_match
            context_match(body['applies'], {})
        for key in ("version", "type", "title", "description"):
            _text(body.get(key), "Asset " + key)
        if 'aliases' in body:
            require(isinstance(body['aliases'], list) and all(isinstance(alias, str) and alias.strip() for alias in body['aliases']), 'Asset aliases must be text names')
        if 'relations' in body:
            body['relations'] = self._bind_relations(identifier, body['version'], body['relations'])
        body["provenance"] = self._bind_evidence(body.get("provenance"), "Asset provenance", True)
        body["uses"] = self._bind_uses(body.get("uses"))
        require(isinstance(body.get("limitations"), list), "Asset limitations must be a list")
        if "status" in body:
            require(body["status"] in LIFECYCLE, "Invalid asset lifecycle status")
        deps = body.get("depends_on")
        require(isinstance(deps, list), "Asset depends_on must be a list")
        seen = set()
        for dep in deps:
            require(isinstance(dep, dict) and set(dep) <= {"id", "version", "purpose"}, "Invalid asset dependency")
            other = _text(dep.get("id"), "Dependency id")
            version = _text(dep.get("version"), "Dependency version")
            require(other != identifier, "An asset cannot depend on itself; keep prior-version lineage in provenance")
            require((other, version) not in seen, "Duplicate asset dependency")
            seen.add((other, version))
            self._row(other, version)
        return body

    def _bind_relations(self, identifier, version, relations):
        """Small semantic links in the original asset; not a second knowledge store."""
        require(isinstance(relations, list), 'Asset relations must be a list')
        bound = []
        for supplied in relations:
            require(isinstance(supplied, dict) and set(supplied) <= {'kind', 'target', 'reason', 'state', 'evidence'}, 'Invalid asset relation')
            relation = deepcopy(supplied)
            require(relation.get('kind') in RELATIONS, 'Unknown asset relation kind')
            _text(relation.get('reason'), 'Relation reason')
            relation.setdefault('state', 'candidate')
            require(relation['state'] in {'candidate', 'reviewed'}, 'Relation state must be candidate or reviewed')
            target = relation.get('target')
            require(isinstance(target, dict), 'Relation needs an exact asset or original-file target')
            if 'id' in target:
                require(set(target) == {'id', 'version'}, 'Relation asset target needs exact id/version')
                require((target['id'], target['version']) != (identifier, version), 'Relation cannot target the same asset version')
                self._row(target['id'], target['version'])
            else:
                # Original professional manifests/tables/paragraphs stay in
                # place; locator is preserved alongside captured file identity.
                relation['target'] = self._bind_evidence([target], 'Relation original target', True)[0]
            relation['evidence'] = self._bind_evidence(relation.get('evidence', []), 'Relation evidence', relation['state'] == 'reviewed')
            bound.append(relation)
        return bound

    def put(self, identifier, expected, body, source):
        """Create a snapshot/new version using the stable asset's current revision.

        Repeating the identical current version at the current revision is a no-op.
        Existing version bytes cannot be replaced, including historical versions.
        """
        _text(identifier, "Asset id"); _text(source, "Source")
        require(type(expected) is int and expected >= 0, "Expected revision must be a nonnegative integer")
        with self.state.transaction(), validation_scope():
            self._ensure_schema()
            head = self.db.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone()
            revision = head["revision"] if head else 0
            require(revision == expected, f"Stale asset revision: expected {expected}, actual {revision}")
            bound = self._bind_body(identifier, body)
            serialized = encoded(bound)
            old = self.db.execute("SELECT * FROM asset_versions WHERE asset_id=? AND version=?",
                                  (identifier, bound["version"])).fetchone()
            if old:
                require(old["body"] == serialized, "Asset version is immutable; supply a new version")
                require(head["current_version"] == bound["version"], "Historical versions remain historical; use an explicit lifecycle revision")
            else:
                stamp = now(); revision += 1
                self.db.execute("INSERT INTO assets VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,current_version=excluded.current_version,updated_at=excluded.updated_at",
                    (identifier, revision, bound["version"], stamp))
                self.db.execute("INSERT INTO asset_versions VALUES(?,?,?,?,?,?)", (identifier, bound["version"], serialized,
                    hashlib.sha256(serialized.encode("utf-8")).hexdigest(), source, stamp))
                self.db.execute("INSERT INTO asset_revisions VALUES(?,?,?,?,?,?)", (identifier, revision, bound["version"], "{}", source, stamp))
                self.state.event(None, "asset_version_registered", source, {"id": identifier, "version": bound["version"],
                    "revision": revision, "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest()})
        return self.get(identifier)

    def revise(self, identifier, expected, patch, source, version=None):
        """Append explicit lifecycle/use annotations without replacing snapshot bytes."""
        _text(source, "Source")
        require(type(expected) is int and expected >= 0, "Expected revision must be a nonnegative integer")
        require(isinstance(patch, dict) and patch and set(patch) <= {"status", "limitations", "uses", "superseded_by", "aliases", "relations"},
                "Only lifecycle, limitations, uses, aliases, relations and superseded_by may be annotated")
        with self.state.transaction(), validation_scope():
            self._ensure_schema()
            head, snapshot, previous = self._row(identifier, version)
            require(head["revision"] == expected, "Stale asset revision")
            patch = deepcopy(patch)
            if "status" in patch:
                require(patch["status"] in LIFECYCLE, "Invalid asset lifecycle status")
            if "limitations" in patch:
                require(isinstance(patch["limitations"], list), "Asset limitations must be a list")
            if "uses" in patch:
                patch["uses"] = self._bind_uses(patch["uses"])
            if 'aliases' in patch:
                require(isinstance(patch['aliases'], list) and all(isinstance(alias, str) and alias.strip() for alias in patch['aliases']), 'Asset aliases must be text names')
            if 'relations' in patch:
                patch['relations'] = self._bind_relations(identifier, snapshot['version'], patch['relations'])
            if "superseded_by" in patch:
                other = patch["superseded_by"]
                require(isinstance(other, dict) and set(other) == {"id", "version"}, "superseded_by needs exact id/version")
                self._row(other["id"], other["version"])
                require((other["id"], other["version"]) != (identifier, snapshot["version"]), "Asset version cannot supersede itself")
            annotations = {**json.loads(previous["annotations"]), **patch}
            revision = expected + 1; stamp = now()
            self.db.execute("UPDATE assets SET revision=?,updated_at=? WHERE id=?", (revision, stamp, identifier))
            self.db.execute("INSERT INTO asset_revisions VALUES(?,?,?,?,?,?)", (identifier, revision, snapshot["version"], encoded(annotations), source, stamp))
            self.state.event(None, "asset_lifecycle_revised", source, {"id": identifier, "version": snapshot["version"],
                "revision": revision, "patch": patch})
        return self.get(identifier, snapshot["version"])

    def _evidence_issues(self, references, location):
        issues = []
        for ref in references:
            issue = {"location": location, "path": ref["path"], "expected_sha256": ref["sha256"]}
            try:
                path = local_path(self.root, ref["path"])
                if not path.is_file():
                    issues.append({**issue, "kind": "file_unavailable"})
                else:
                    actual = digest(path)
                    if actual != ref["sha256"]:
                        issues.append({**issue, "kind": "file_changed", "actual_sha256": actual})
            except (OSError, HarnessError):
                issues.append({**issue, "kind": "file_unavailable"})
        return issues

    def _validity(self, identifier, version, memo, trail):
        key = (identifier, version)
        if key in memo:
            return deepcopy(memo[key])
        if key in trail:
            return [{"kind": "dependency_cycle", "id": identifier, "version": version}]
        head, snapshot, revision = self._row(identifier, version)
        body = {**json.loads(snapshot["body"]), **json.loads(revision["annotations"])}
        issues = self._evidence_issues(body["provenance"], "provenance")
        if head["current_version"] != version:
            issues.append({"kind": "asset_has_new_version", "current_version": head["current_version"],
                "meaning": "Historical content remains captured; review the scope before using the newer version"})
        for use in body["uses"]:
            issues.extend(self._evidence_issues(use["evidence"], "declared_use:" + use["purpose"]))
        if body.get("status") in {"retired", "retracted", "superseded"}:
            issues.append({"kind": "lifecycle_requires_review", "status": body["status"]})
        for dep in body["depends_on"]:
            try:
                dep_head, _, _ = self._row(dep["id"], dep["version"])
                if dep_head["current_version"] != dep["version"]:
                    issues.append({"kind": "dependency_has_new_version", **dep, "current_version": dep_head["current_version"],
                        "meaning": "Pinned content retained; review applicability of the newer version"})
                nested = self._validity(dep["id"], dep["version"], memo, trail | {key})
                if nested:
                    issues.append({"kind": "dependency_needs_review", **dep, "issues": nested})
            except HarnessError as exc:
                issues.append({"kind": "dependency_unavailable", **dep, "reason": str(exc)})
        memo[key] = issues
        return deepcopy(issues)

    def _usage(self, identifier, version):
        records, latest = [], {}
        rows = self.db.execute("SELECT * FROM asset_uses WHERE asset_id=? AND version=? ORDER BY id", (identifier, version)).fetchall()
        for row in rows:
            latest[(row["work_id"], row["requirement"], json.loads(row["body"])["description"])] = row["id"]
        for row in rows:
            item = dict(row); body = json.loads(item.pop("body")); issues = []
            try:
                work = self.state.get(item["work_id"])
                requirement = next((r for r in work["requirements"] if r["id"] == item["requirement"]), None)
                if work["request_revision"] != item["request_revision"]:
                    issues.append({"kind": "work_request_changed"})
                if not requirement or requirement["revision"] != item["requirement_revision"]:
                    issues.append({"kind": "requirement_changed"})
            except (HarnessError, KeyError):
                issues.append({"kind": "work_unavailable"})
            issues.extend(self._evidence_issues(body.get("evidence", []), "reuse_evidence"))
            current = latest[(item["work_id"], item["requirement"], body["description"])] == item["id"]
            records.append({**item, "work": item["work_id"], **body, "current": current,
                "applicability": "historical_use" if not current else "needs_review" if issues else
                "scoped_adoption" if body["state"] in {"adopted", "conditional"} else
                "rejected" if body["state"] == "rejected" else "unreviewed",
                "issues": issues})
        return records

    def get(self, identifier, version=None):
        with validation_scope():
            head, snapshot, revision = self._row(identifier, version)
            issues = self._validity(identifier, snapshot["version"], {}, set())
            usage = self._usage(identifier, snapshot["version"])
            for item in usage:
                if issues and item["current"]:
                    item["applicability"] = "needs_review"
                    item["issues"] = [*item["issues"], {"kind": "asset_evidence_or_dependency_changed"}]
            return {"id": identifier, "revision": head["revision"], "version": snapshot["version"],
                "current_version": head["current_version"], "is_current": snapshot["version"] == head["current_version"],
                "body": json.loads(snapshot["body"]), "annotations": json.loads(revision["annotations"]),
                "version_revision": revision["revision"], "sha256": snapshot["sha256"],
                "registered_at": snapshot["at"], "source": snapshot["source"], "updated_at": head["updated_at"],
                "method_selections": self.method_selections(identifier),
                "validity": {"state": "needs_review" if issues else "captured_local_evidence_matches", "issues": issues,
                    "checked_at": now(), "scope": "Captured local files and exact declared asset dependencies only",
                    "remote_state": "not_rechecked", "scientific_adoption": "unchanged_by_registry"}, "usage": usage}

    def list(self, work=None, query=None):
        if work is not None:
            self.state.get(work)
        if query is not None:
            _text(query, "Asset query")
        if not self._present():
            return []
        ids = [r[0] for r in self.db.execute("SELECT id FROM assets ORDER BY id")]
        if work is not None:
            ids = [r[0] for r in self.db.execute("SELECT DISTINCT asset_id FROM asset_uses WHERE work_id=? ORDER BY asset_id", (work,))]
        values = []
        with validation_scope():
            for identifier in ids:
                item = self.get(identifier)
                if work is not None:
                    # A work may still use an older version: preserve that pin in
                    # the view even though the registry's current version changed.
                    versions = [r[0] for r in self.db.execute("SELECT DISTINCT version FROM asset_uses WHERE asset_id=? AND work_id=? ORDER BY version", (identifier, work))]
                    item["work_usage"] = [u for version in versions for u in self.get(identifier, version)["usage"] if u["work_id"] == work]
                if query is None or query.casefold() in encoded({"id": identifier, "body": item["body"], "annotations": item["annotations"]}).casefold():
                    values.append(item)
        return values

    def metadata(self, work=None, include_history=False):
        """Read exact registered versions/dispositions without validating files.

        A work view enumerates each version it actually used. The global view
        shows current registry heads. Both retain recorded scoped-use history;
        callers must not infer present applicability from these recorded facts.
        """
        if work is not None:
            self.state.get(work)
        if not self._present():
            return []
        if work is None and include_history:
            pairs = [(r['asset_id'], r['version']) for r in self.db.execute(
                'SELECT asset_id,version FROM asset_versions ORDER BY asset_id,version')]
        elif work is None:
            pairs = [(r["id"], r["current_version"]) for r in self.db.execute(
                "SELECT id,current_version FROM assets ORDER BY id")]
        else:
            pairs = [(r["asset_id"], r["version"]) for r in self.db.execute(
                "SELECT DISTINCT asset_id,version FROM asset_uses WHERE work_id=? ORDER BY asset_id,version", (work,))]
        values = []
        for identifier, version in pairs:
            head, snapshot, revision = self._row(identifier, version)
            rows = self.db.execute("SELECT * FROM asset_uses WHERE asset_id=? AND version=? ORDER BY id", (identifier, version)).fetchall()
            latest = {}
            for row in rows:
                latest[(row["work_id"], row["requirement"], json.loads(row["body"])["description"])] = row["id"]
            usage = []
            for row in rows:
                item = dict(row); body = json.loads(item.pop("body"))
                current = latest[(item["work_id"], item["requirement"], body["description"])] == item["id"]
                usage.append({**item, "work": item["work_id"], **body, "current": current,
                    "applicability": "not_rechecked", "issues": [],
                    "verification_scope": "Recorded use disposition only; source evidence, work request and requirement revision are not rechecked"})
            value = {"id": identifier, "revision": head["revision"], "version": version,
                "current_version": head["current_version"], "is_current": version == head["current_version"],
                "body": json.loads(snapshot["body"]), "annotations": json.loads(revision["annotations"]),
                "version_revision": revision["revision"], "sha256": snapshot["sha256"],
                "registered_at": snapshot["at"], "source": snapshot["source"], "updated_at": head["updated_at"],
                "validity": {"state": "not_rechecked", "issues": [],
                    "scope": "SQLite registration metadata only; files, upstream versions, lifecycle applicability and scientific evidence are not rechecked",
                    "remote_state": "not_rechecked", "scientific_adoption": "unchanged_by_registry"},
                "usage": usage}
            if work is not None:
                value["work_usage"] = [u for u in usage if u["work_id"] == work]
            values.append(value)
        return values

    def search(self, query='', context=None, *, limit=10, can_read=None, include_history=True):
        """Purpose-filtered metadata; exact versions and old conclusions remain distinct.

        can_read is supplied by the trusted local caller/configuration, never by
        a context label. Restricted records are opaque. No scientific adoption or
        remote verification is inferred from a hit.
        """
        from .experiences import ExperienceRegistry, context_match
        context = ExperienceRegistry(self.state).resolve_context(context or {})
        context_match({}, context)
        require(type(limit) is int and 0 <= limit <= 50, 'Asset search limit must be 0..50')
        can_read = can_read or ExperienceRegistry(self.state)._local_read
        matches, restricted = [], 0
        metadata = self.metadata(include_history=True)
        visible = {}
        for item in metadata:
            body = {**item['body'], **item['annotations']}
            if body.get('access', body.get('access_scope', 'local_workspace')) in ('local_workspace', 'workspace', 'local') and all(
                    can_read(ref['path']) for ref in self._path_references(body)):
                visible[(item['id'], item['version'])] = body
        direct = {key for key, body in visible.items() if text_match(query, {'id': key[0], 'body': body})}
        linked_by = {}
        if query:
            for origin, body in visible.items():
                for relation in body.get('relations', []):
                    target = relation['target']
                    target_key = (target.get('id'), target.get('version'))
                    link = {'from': {'id': origin[0], 'version': origin[1]}, **relation,
                        'meaning': 'Declared semantic relation; inspect its evidence and review state'}
                    if target_key in direct:
                        linked_by.setdefault(origin, []).append(link)
                    if origin in direct and target_key in visible:
                        linked_by.setdefault(target_key, []).append(link)
        for item in metadata:
            choices = self.method_selections(item['id'])
            selection = next((c for c in choices if c['scope'].get('work') == context.get('work') and
                c['scope'].get('purpose') == context.get('purpose') and context.get('work') and context.get('purpose')), None)
            pins = context.get('asset_versions', {})
            if pins and (item['id'] not in pins or pins[item['id']] != item['version']):
                continue
            if not pins and selection and item['version'] != selection['version']:
                continue
            if not include_history and not item['is_current'] and not (selection and item['version'] == selection['version']):
                continue
            body = {**item['body'], **item['annotations']}
            # Filter before any file inspection or search snippet projection.
            if body.get('access', body.get('access_scope', 'local_workspace')) not in ('local_workspace', 'workspace', 'local') or any(
                    not can_read(ref['path']) for ref in self._path_references(body)):
                restricted += 1
                continue
            key = (item['id'], item['version'])
            linked = linked_by.get(key, [])
            if query and key not in direct and not linked:
                continue
            applicability = context_match(body.get('applies', {}), context)
            if applicability['state'] == 'inapplicable':
                continue
            uses = body['uses']
            if context.get('purpose') and not body.get('applies', {}).get('purposes'):
                matching_uses = [u for u in uses if context['purpose'].casefold() in encoded(u).casefold()]
                if not matching_uses and applicability['state'] == 'declared_match':
                    applicability = {'state': 'unclassified', 'mismatches': [], 'missing': ['declared_purpose']}
                uses = matching_uses or uses
            status = body.get('status', 'unspecified')
            matches.append({'id': item['id'], 'version': item['version'], 'current_version': item['current_version'],
                'is_current': item['is_current'], 'revision': item['revision'], 'sha256': item['sha256'],
                'title': body['title'], 'type': body['type'], 'uses': uses, 'limitations': body['limitations'],
                'description': body['description'], 'aliases': body.get('aliases', []), 'relations': body.get('relations', []),
                'match_basis': 'direct_metadata_or_alias' if key in direct else 'declared_relation', 'related_matches': linked,
                'access': {'scope': 'trusted_local_workspace', 'basis': 'Existing local path scope; no access granted by query labels'},
                'status': status, 'superseded_by': body.get('superseded_by'), 'applies': body.get('applies', {}),
                'applicability': applicability, 'validity': 'not_rechecked',
                'default_recommendation': (selection is not None and selection['state'] == 'active' and selection['authority'] == 'current' and item['version'] == selection['version'] or not choices and item['is_current'] and body['type'] != 'method') and status not in {'draft', 'retired', 'retracted', 'superseded'} and applicability['state'] == 'declared_match',
                'entry': 'asset-show --id ' + item['id'] + ' --version ' + item['version']})
            entry = None
            if selection and selection['version'] == item['version']:
                entry = {'kind': selection.get('kind', 'experiment_bundle'), 'bundle': selection['bundle'], 'files': selection['files'], 'sha256': selection['sha256']}
            elif body.get('publication', {}).get('immutable_bundle'):
                entry = {'bundle': body['publication']['immutable_bundle'], 'files': body['publication']['files'],
                    'sha256': body['publication']['bundle_sha256']}
            elif body.get('publication', {}).get('original_files'):
                entry = {'kind': 'ordinary_approved_change', 'bundle': '.', 'files': body['publication']['original_files'],
                    'sha256': hashlib.sha256(encoded(body['publication']['original_files']).encode()).hexdigest()}
            if entry and any(not can_read(str(__import__('pathlib').Path(entry['bundle']) / path)) for path in entry['files']):
                entry = {'state': 'outside_trusted_read_scope', 'required_load': True}
                matches[-1]['default_recommendation'] = False
            matches[-1].update(scope_selections=choices, scope_selection=selection, execution_entry=entry,
                active_for_scope=item['version'] == selection['version'] and selection['state'] == 'active' and selection['authority'] == 'current' if selection else None,
                requires_purpose_scope=bool(choices) and selection is None,
                selection_coverage='Current registered method scope decisions only; global head/history and prior task pins remain distinct')
            requested_usage = [u for u in item['usage'] if u.get('work_id') == context.get('work')]
            visible_usage = [u for u in requested_usage if all(can_read(ref['path']) for ref in self._path_references(u))]
            matches[-1]['requested_work_usage'] = visible_usage
            matches[-1]['restricted_usage_count'] = len(requested_usage) - len(visible_usage)
        matches.sort(key=lambda v: (not v['default_recommendation'], v['id'], v['version']))
        return {'query': query, 'context': context, 'items': matches[:limit], 'count': len(matches),
            'omitted': max(0, len(matches) - limit), 'restricted_count': restricted,
            'coverage': 'Exact registered metadata/versions only; old scientific scopes remain distinct; current files/remote/rights are not revalidated'}

    def method_selections(self, identifier):
        """Current scoped selection in the same journal, including rollback.

        This is an approved method-use route, never a replacement for the
        immutable asset version/global head. Ordinary assets have no records.
        """
        latest = {}
        for row in self.db.execute("SELECT id,body,source,at FROM observations WHERE asset=? AND kind='note' ORDER BY id", ('asset-selection:' + identifier,)):
            value = json.loads(row['body']).get('method_selection', {})
            if value.get('schema') != 'htam.method-selection/v1':
                continue
            value = {**value, 'observation_id': row['id'], 'source': row['source'], 'at': row['at']}
            authority_approval = None
            try:
                if value.get('kind') == 'ordinary_approved_change':
                    from .experiences import ExperienceRegistry
                    registry = ExperienceRegistry(self.state)
                    candidate = registry._active_candidate(value['experience_id'], value['experience_sha256'])
                    # A renewed approval for these same candidate bytes can
                    # restore the existing release. Publication history remains
                    # unchanged; stale embedded approval is not the active one.
                    authority_approval = next((record for record in reversed(registry._records(value['experience_id']))
                        if record['experience']['record_type'] == 'approval'), None)
                    require(authority_approval is not None, 'Published method has no current experience approval')
                    registry._approval(candidate, value['experience_sha256'], authority_approval['experience']['approval'], receipt=True)
                    release = candidate.get('method_release', {})
                    require(release.get('asset_id') == identifier and release.get('version') == value['version'] and release.get('scope') == value['scope'], 'Ordinary release identity/scope changed')
                else:
                    from .experiments.journal import human
                    human(self.state, value['scope']['work'], value['approval'], value['plan_sha256'])
                    plan_row = self.db.execute("SELECT body FROM observations WHERE asset=? AND kind='note' AND json_extract(body,'$.experiment.kind')='publication_plan' AND json_extract(body,'$.experiment.data.sha256')=? ORDER BY id DESC LIMIT 1", ('experiment:' + value.get('experiment', ''), value['plan_sha256'])).fetchone()
                    plan = json.loads(plan_row['body'])['experiment']['data'] if plan_row else None
                    if plan and value['version'] == plan['version']:
                        from .experiences import ExperienceRegistry
                        ExperienceRegistry(self.state)._active_candidate(plan['experience_id'], plan['experience_sha256'])
                authority = 'current'
            except (HarnessError, OSError, ValueError, KeyError, TypeError):
                authority = 'needs_reapproval'
            latest[encoded(value['scope'])] = {k: value.get(k) for k in ('scope', 'version', 'previous_version', 'bundle', 'files', 'sha256',
                'experiment', 'plan_sha256', 'reason', 'observation_id', 'source', 'at', 'experience_id', 'experience_sha256', 'kind')}
            latest[encoded(value['scope'])]['state'] = value.get('state', 'active')
            latest[encoded(value['scope'])]['authority'] = authority
            latest[encoded(value['scope'])]['authority_approval_observation_id'] = authority_approval['id'] if authority_approval and authority == 'current' else None
        return list(latest.values())

    @staticmethod
    def _path_references(value):
        if isinstance(value, dict):
            if isinstance(value.get('path'), str):
                yield value
            for nested in value.values():
                yield from AssetRegistry._path_references(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from AssetRegistry._path_references(nested)

    def handoff(self, identifier, version, purpose, *, verify=False, can_read=None, context=None):
        """Restore an explicit version/purpose without creating an asset/use record."""
        from .experiences import ExperienceRegistry
        can_read = can_read or ExperienceRegistry(self.state)._local_read
        _text(version, 'Handoff exact version'); _text(purpose, 'Handoff purpose')
        context = dict(context or {})
        require(not context.get('purpose') or context['purpose'] == purpose, 'Handoff purpose/context differs')
        context.update(purpose=purpose, asset_versions={identifier: version})
        candidates = self.search(identifier, context, limit=50, can_read=can_read)
        item = next((a for a in candidates['items'] if a['id'] == identifier and a['version'] == version), None)
        require(item is not None, 'Selected version/purpose is outside registered accessible metadata', 'ASSET_SCOPE')
        head, snapshot, revision = self._row(identifier, version)
        body = {**json.loads(snapshot['body']), **json.loads(revision['annotations'])}
        if verify:
            # get() verifies declared upstreams too. Respect the same trusted
            # scope before any dependency file can be opened for hashing.
            pending, checked = list(body['depends_on']), set()
            while pending:
                dependency = pending.pop()
                key = (dependency['id'], dependency['version'])
                if key in checked:
                    continue
                checked.add(key)
                _, upstream, annotations = self._row(*key)
                upstream_body = {**json.loads(upstream['body']), **json.loads(annotations['annotations'])}
                require(upstream_body.get('access', 'local_workspace') == 'local_workspace' and all(can_read(ref['path']) for ref in self._path_references(upstream_body)),
                    'Dependency verification is outside the trusted local read scope', 'ACCESS_SCOPE')
                pending.extend(upstream_body['depends_on'])
        validity = {'state': 'not_rechecked'}
        if verify:
            entry = item.get('execution_entry')
            if entry and entry.get('files'):
                from pathlib import Path
                base = local_path(self.root, entry['bundle'])
                require(not base.is_symlink() and not any(p.is_symlink() for p in base.parents), 'Immutable method bundle symlink rejected', 'ACCESS_SCOPE')
                paths = ([local_path(self.root, name) for name in entry['files']] if entry.get('kind') == 'ordinary_approved_change' else
                    [p for p in base.rglob('*') if not p.is_dir() or p.is_symlink()])
                require(all(p.is_file() and not p.is_symlink() for p in paths) and {str(p.relative_to(base)) for p in paths} == set(entry['files']), 'Immutable method bundle file boundary changed', 'CONTENT_CHANGED')
                require(all((base / name).stat().st_size == metadata['bytes'] for name, metadata in entry['files'].items()), 'Immutable method file size changed', 'CONTENT_CHANGED')
                require(all(can_read(str(p.relative_to(self.root))) for p in paths), 'Immutable method bundle outside read scope', 'ACCESS_SCOPE')
                require(all(digest(base / name) == metadata['sha256'] for name, metadata in entry['files'].items()), 'Immutable method content changed', 'CONTENT_CHANGED')
                validity = {'state': 'original_method_matches' if entry.get('kind') == 'ordinary_approved_change' else 'immutable_bundle_matches',
                    'scope': 'Exact registered original method files' if entry.get('kind') == 'ordinary_approved_change' else 'Exact scoped/pinned execution bundle; mutable original-entry evidence remains historical'}
            elif entry and entry.get('required_load'):
                raise HarnessError('Immutable method entry outside trusted scope', 'ACCESS_SCOPE')
            else:
                validity = self.get(identifier, version)['validity']
        return {**item, 'provenance': body['provenance'], 'depends_on': body['depends_on'],
            'validity': validity,
            'meaning': 'Read-only handoff; original scientific adoption, permissions and work usage remain unchanged'}

    def summary(self, work=None, limit=3):
        """Bounded context metadata; no file reads/hashes or remote validation."""
        require(type(limit) is int and 0 <= limit <= 50, "Asset summary limit must be 0..50")
        if work is not None:
            self.state.get(work)
        coverage = "Registered metadata only; local evidence, dependencies, scientific applicability and remote state are not rechecked"
        if not self._present():
            return {"count": 0, "items": [], "omitted": 0, "coverage": coverage,
                    "registry": "not_present"}
        if work is None:
            ids = [r[0] for r in self.db.execute("SELECT id FROM assets ORDER BY id")]
        else:
            ids = [r[0] for r in self.db.execute("SELECT DISTINCT asset_id FROM asset_uses WHERE work_id=? ORDER BY asset_id", (work,))]
        items = []
        for identifier in ids[:limit]:
            head, snapshot, revision = self._row(identifier)
            body = {**json.loads(snapshot["body"]), **json.loads(revision["annotations"])}
            uses = []
            sql = "SELECT work_id,requirement,version,body,at FROM asset_uses WHERE asset_id=?"
            args = (identifier,)
            if work is not None:
                sql += " AND work_id=?"; args += (work,)
            sql += " ORDER BY id"
            for row in self.db.execute(sql, args):
                use = json.loads(row["body"])
                uses.append({"work": row["work_id"], "requirement": row["requirement"],
                    "version": row["version"], "purpose": use["description"][:240], "state": use["state"],
                    "recorded_at": row["at"], "validity": "not_rechecked"})
            declared = [{"purpose": use["purpose"][:240], "state": use["state"],
                "scope": encoded(use["scope"])[:500], "validity": "not_rechecked"} for use in body["uses"][:3]]
            items.append({"id": identifier, "version": snapshot["version"], "revision": head["revision"],
                "title": body["title"], "type": body["type"], "recorded_uses": declared,
                "omitted_declared_uses": max(0, len(body["uses"]) - len(declared)),
                "work_usage": uses[-5:], "omitted_usage_records": max(0, len(uses) - 5),
                "recorded_validity": "not_rechecked"})
            items[-1]['method_scope_selections'] = [{k: selection[k] for k in ('scope', 'version', 'authority', 'state', 'experiment', 'reason')}
                for selection in self.method_selections(identifier) if work is None or selection['scope'].get('work') == work]
        return {"count": len(ids), "items": items, "omitted": max(0, len(ids) - len(items)),
                "coverage": coverage, "registry": "present"}

    def use(self, work, requirement, identifier, version, purpose, source):
        """Record reuse, or an explicit scoped adoption; never update a work assessment."""
        _text(source, "Source")
        _text(version, "Exact asset-use version")
        supplied = {"description": purpose, "state": "reused"} if isinstance(purpose, str) else deepcopy(purpose)
        require(isinstance(supplied, dict), "Reuse purpose must be text or an object")
        _text(supplied.get("description"), "Reuse description")
        require(supplied.get("state", "reused") in {"reused", "adopted", "conditional", "rejected"}, "Invalid scoped reuse state")
        supplied.setdefault("state", "reused")
        with self.state.transaction(), validation_scope():
            self._ensure_schema()
            current = self.state.get(work)
            req = next((r for r in current.get("requirements", []) if r["id"] == requirement), None)
            require(req is not None, "Unknown requirement for asset use: " + str(requirement))
            self._row(identifier, version)
            if supplied.get('task'):
                task = self.state.task(supplied['task'])
                require(task['work_id'] == work, 'Method/asset use task belongs to another work')
            if 'outcome' in supplied:
                outcome = supplied['outcome']
                require(isinstance(outcome, dict) and set(outcome) <= {'assessment', 'explanation', 'evidence'} and outcome.get('assessment') in {'benefit', 'failure', 'no_gain', 'unknown'}, 'Use outcome needs an explicit assessment')
                _text(outcome.get('explanation'), 'Use outcome explanation')
                outcome['evidence'] = self._bind_evidence(outcome.get('evidence', []), 'Use outcome evidence', outcome['assessment'] != 'unknown')
            scope = {"work": work, "requirement": requirement}
            if supplied["state"] in {"adopted", "conditional"}:
                require(supplied.get("scope") == scope, "Scoped adoption must name this exact work and requirement")
                require("limitations" in supplied and isinstance(supplied["limitations"], list), "Scoped adoption needs explicit limitations")
            else:
                require(not supplied.get("scope") or supplied["scope"] == scope, "Reuse scope differs from the selected work and requirement")
            supplied["scope"] = scope
            supplied["evidence"] = self._bind_evidence(supplied.get("evidence", []), "Scoped reuse evidence",
                supplied["state"] in {"adopted", "conditional"})
            cursor = self.db.execute("INSERT INTO asset_uses(work_id,requirement,requirement_revision,request_revision,asset_id,version,body,source,at) VALUES(?,?,?,?,?,?,?,?,?)",
                (work, requirement, req["revision"], current["request_revision"], identifier, version, encoded(supplied), source, now()))
            use_id = cursor.lastrowid
            self.state.event(work, "asset_use_recorded", source, {"id": use_id, "asset": identifier, "version": version,
                "requirement": requirement, "state": supplied["state"], "meaning": "Use record only; overall requirement assessment is unchanged"})
        return next(u for u in self.get(identifier, version)["usage"] if u["id"] == use_id)

    def history(self, identifier):
        self._row(identifier)
        return [{**dict(r), "annotations": json.loads(r["annotations"])} for r in self.db.execute(
            "SELECT * FROM asset_revisions WHERE asset_id=? ORDER BY revision", (identifier,))]

    def impact(self, identifier, version=None):
        """Show declared transitive dependents and scoped uses, without blocking them.

        Without a version, include all captured versions of the stable identity so
        a new upstream version also reveals consumers pinned to its old version.
        """
        _text(identifier, "Asset id")
        if not self._present():
            return {"id": identifier, "version": version, "assets": [], "uses": [], "works": [],
                    "coverage": "No local asset registry; no inference about absent project assets"}
        self._row(identifier, version)
        seeds = {(identifier, version)} if version is not None else {(identifier, r[0]) for r in self.db.execute(
            "SELECT version FROM asset_versions WHERE asset_id=?", (identifier,))}
        reverse = {}
        for row in self.db.execute("SELECT asset_id,version,body FROM asset_versions"):
            for dep in json.loads(row["body"])["depends_on"]:
                reverse.setdefault((dep["id"], dep["version"]), set()).add((row["asset_id"], row["version"]))
        pending = list(seeds); seen = set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key); pending.extend(reverse.get(key, set()) - seen)
        assets, uses = [], []
        with validation_scope():
            for asset_id, asset_version in sorted(seen):
                value = self.get(asset_id, asset_version)
                assets.append({"id": asset_id, "version": asset_version, "relation": "selected" if (asset_id, asset_version) in seeds else "declared_dependent",
                    "validity": value["validity"], "usage_count": len(value["usage"])})
                uses.extend(value["usage"])
        related = []
        for item in self.metadata(include_history=True):
            body = {**item['body'], **item['annotations']}
            for relation in body.get('relations', []):
                target = relation['target']
                origin = (item['id'], item['version'])
                if origin in seen or (target.get('id'), target.get('version')) in seen:
                    related.append({'from': {'id': origin[0], 'version': origin[1]}, **relation})
        return {"id": identifier, "version": version, "assets": assets, "uses": uses, 'semantic_relations': related,
                "works": sorted({u["work_id"] for u in uses}),
                "coverage": "Exact registered dependencies, use records and explicit semantic links; semantic links do not create blocking dependencies or scientific adoption"}
