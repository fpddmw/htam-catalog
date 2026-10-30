from __future__ import annotations

import fnmatch
import hashlib
from functools import lru_cache
from itertools import chain
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3

from .common import HarnessError, cached, connect, digest, encoded, local_path, now, require


DEFAULT_EXCLUDES = [".git", ".venv", "node_modules", ".runtime", "__pycache__", ".harness-local",
                    ".env", ".env.*", "*.pem", "*.key", "*credentials*", "auth.json",
                    ".ssh", ".aws", ".azure", ".kube", ".gnupg"]
TEXT_SUFFIXES = {".md", ".txt", ".py", ".csv", ".tsv", ".rst"}
MAX_TEXT = 2 * 1024 * 1024
MAX_HASH = 128 * 1024 * 1024
# These existing workspace routes are exact reads within the saved scan scope.
# Skills are recovered from indexed paths only; retrieval never walks a tree.
CAPABILITY_PATHS = (
    'wiki/capabilities-and-needs.md',
    'wiki/tiangong-lca-tools.md',
    'wiki/tiangong-resources.md',
)
INACTIVE_ENTRIES = {'retired', 'retracted', 'superseded', 'archived'}


def safe_text(value):
    # Search indexes need identifiers, not signed URL parameters.
    return re.sub(r'(https?://[^\s<>"?]+)\?[^\s<>" ]+', r'\1?[query omitted]', value)


@lru_cache(maxsize=1)
def _casefold_sources():
    """Non-ASCII sources of Python casefold characters, for a SQL superset.

    SQLite lower() handles ASCII only. A row containing a possible Unicode
    source remains a candidate; exact casefold matching happens after access
    checks. No file contents cross into a Python SQLite callback.
    """
    sources = {}
    for ordinal in range(128, 0x110000):
        char = chr(ordinal)
        folded = char.casefold()
        if char != folded:
            for output in set(folded) or {''}:
                sources.setdefault(output, set()).add(char)
    return sources


class Discovery:
    def __init__(self, root, directory, state=None, readonly=False):
        self.root = root.resolve()
        self.state = state
        self.readonly = readonly
        missing = readonly and not (directory / 'index.sqlite').exists()
        self.db = sqlite3.connect(':memory:') if missing else connect(directory / 'index.sqlite', readonly=readonly)
        self.db.row_factory = sqlite3.Row
        if not readonly or missing:
            self.db.executescript("""
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha TEXT, size INTEGER,
        mtime INTEGER, ctime INTEGER, presence TEXT, text_state TEXT, identifiers TEXT,
        text TEXT, error TEXT, scanned_at TEXT);
        CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, report TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def scan(self, config):
        require(not self.readonly, 'Read-only discovery cannot rebuild the index')
        roots = [local_path(self.root, p) for p in config["roots"]]
        excludes = list(dict.fromkeys(DEFAULT_EXCLUDES + config.get("exclude", [])))
        stamp = now()
        report = {"at": stamp, "roots": config["roots"], "exclusions": excludes,
                  "errors": [], "files": 0, "metadata_only": 0, "partial_text": 0,
                  "gitignore_used": False, "symlinks_followed": False,
                  "entrypoints": config.get('entrypoints', [])}
        require(isinstance(report['entrypoints'], list), 'Discovery entrypoints must be a list')
        entry_ids = set()
        for entry in report['entrypoints']:
            require(isinstance(entry, dict) and entry.get('id') and entry.get('path') and entry.get('kind') in
                ('project_entry', 'professional_manifest', 'reuse_package'), 'Invalid explicit discovery entrypoint')
            require(entry['id'] not in entry_ids, 'Duplicate discovery entrypoint ID')
            entry_ids.add(entry['id'])
            local_path(self.root, entry['path'])
            if 'reference_base' in entry:
                local_path(self.root, entry['reference_base'])
        records = {}
        failed = []

        def excluded(p):
            rel = p.relative_to(self.root)
            return any(fnmatch.fnmatchcase(part.lower(), pat.lower()) for part in rel.parts for pat in excludes)

        def fail(path, error):
            failed.append(path)
            report["errors"].append({"path": str(path.relative_to(self.root)), "error": str(error)})

        def record(p):
            if p.is_symlink() or excluded(p):
                return
            rel = str(p.relative_to(self.root))
            try:
                st = p.stat()
                prior = self.db.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
                if prior and prior["presence"] == "present" and (st.st_mtime_ns, st.st_ctime_ns, st.st_size) == (prior["mtime"], prior["ctime"], prior["size"]):
                    item = dict(prior)
                    item["scanned_at"] = stamp
                else:
                    sha = digest(p) if st.st_size <= MAX_HASH else None
                    content = ""
                    text_state = "metadata_only"
                    identifiers = []
                    if p.suffix.lower() in TEXT_SUFFIXES:
                        with p.open("rb") as f:
                            raw = f.read(MAX_TEXT + 1)
                        try:
                            content = raw[:MAX_TEXT].decode("utf-8-sig")
                            text_state = "partial" if len(raw) > MAX_TEXT else "complete"
                        except UnicodeDecodeError:
                            content = raw[:MAX_TEXT].decode("utf-8-sig", errors="replace")
                            text_state = "partial_encoding"
                    elif p.suffix.lower() == ".json" and st.st_size <= MAX_TEXT:
                        # Do not index arbitrary JSON bodies (tokens and signed URLs may occur).
                        try:
                            data = json.loads(p.read_text(encoding="utf-8-sig"))
                            selected = []
                            def visit(obj):
                                if isinstance(obj, dict):
                                    for k, v in obj.items():
                                        if k.lower() in {"title", "name", "doi", "document_title", "source_title", "uuid", "id"} and isinstance(v, (str, int)):
                                            selected.append(str(v))
                                        elif isinstance(v, (dict, list)):
                                            visit(v)
                                elif isinstance(obj, list):
                                    for v in obj:
                                        visit(v)
                            visit(data)
                            content = "\n".join(selected)
                            text_state = "selected_metadata"
                        except (ValueError, UnicodeError, RecursionError):
                            text_state = "unparsed_json"
                    content = safe_text(content)
                    identifiers = sorted(set(re.findall(r"10\.\d{4,9}/[^\s<>\]\)\"，。]+", content, re.I)))[:100]
                    item = {"path": rel, "sha": sha, "size": st.st_size, "mtime": st.st_mtime_ns,
                            "ctime": st.st_ctime_ns, "presence": "present", "text_state": text_state,
                            "identifiers": encoded(identifiers), "text": content, "error": None,
                            "scanned_at": stamp}
                records[rel] = item
            except OSError as e:
                fail(p, e)
                records[rel] = {"path": rel, "sha": None, "size": None, "mtime": None, "ctime": None,
                                "presence": "unknown", "text_state": "unreadable", "identifiers": "[]",
                                "text": "", "error": str(e), "scanned_at": stamp}

        for scope in roots:
            if scope.is_symlink() or excluded(scope):
                fail(scope, "Scope is excluded or a symlink")
            elif scope.is_file():
                record(scope)
            elif scope.is_dir():
                def onerror(e):
                    fail(Path(e.filename), e)
                for folder, dirs, files in os.walk(scope, onerror=onerror, followlinks=False):
                    parent = Path(folder)
                    dirs[:] = sorted(d for d in dirs if not (parent / d).is_symlink() and not excluded(parent / d))
                    for name in sorted(files):
                        record(parent / name)
            else:
                fail(scope, "Configured scope is unavailable")

        self.db.execute("BEGIN IMMEDIATE")
        try:
            for previous in self.db.execute("SELECT path FROM files").fetchall():
                rel = previous["path"]
                if rel in records:
                    continue
                path = self.root / rel
                in_scope = any(path == scope or path.is_relative_to(scope) for scope in roots)
                unavailable = any(path == p or path.is_relative_to(p) for p in failed)
                presence = "excluded" if not in_scope or excluded(path) else "unknown" if unavailable else "missing"
                self.db.execute("UPDATE files SET presence=?,scanned_at=? WHERE path=?", (presence, stamp, rel))
            for item in records.values():
                self.db.execute("INSERT OR REPLACE INTO files VALUES(:path,:sha,:size,:mtime,:ctime,:presence,:text_state,:identifiers,:text,:error,:scanned_at)", item)
            report["files"] = len(records)
            report["metadata_only"] = sum(i["text_state"] not in ("complete", "partial", "partial_encoding") for i in records.values())
            report["partial_text"] = sum(i["text_state"].startswith("partial") for i in records.values())
            self.db.execute("INSERT INTO scans(report) VALUES(?)", (encoded(report),))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return report

    def coverage(self):
        row = self.db.execute("SELECT report FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else {"status": "not_indexed", "roots": []}

    @contextmanager
    def _read_scope(self, coverage=None):
        previous = getattr(self, '_permission_policy', None)
        if previous is not None and (coverage is None or coverage is previous['coverage']):
            yield previous
            return
        policy = self._compile_policy(self.coverage() if coverage is None else coverage)
        self._permission_policy = policy
        try:
            yield policy
        finally:
            self._permission_policy = previous

    def _compile_policy(self, coverage):
        policy = {'coverage': coverage,
            'hard': [p.lower() for p in DEFAULT_EXCLUDES if p != '.harness-local'],
            'exclusions': [p.lower() for p in coverage.get('exclusions', DEFAULT_EXCLUDES)],
            'entry_snapshots': {}}
        try:
            entries = [(local_path(self.root, entry['path']), entry.get('access', 'local_workspace'))
                       for entry in coverage.get('entrypoints', [])]
            policy['denied'] = {path for path, access in entries if access != 'local_workspace'}
            policy['exact'] = {path for path, access in entries if access == 'local_workspace'}
        except (OSError, ValueError, HarnessError):
            policy['entry_error'] = True
        try:
            policy['roots'] = tuple(local_path(self.root, root) for root in coverage.get('roots', []))
        except (OSError, ValueError, HarnessError):
            policy['root_error'] = True
        return policy

    def _permitted(self, path, coverage=None):
        """Existing trusted local scan configuration, never a query access label.

        Exact explicit entry files can recover an intentionally unindexed local
        reference. Credential exclusions are never overridden by an entry.
        """
        current = getattr(self, '_permission_policy', None)
        coverage = coverage or (current['coverage'] if current else self.coverage())
        policy = current if current and coverage is current['coverage'] else self._compile_policy(coverage)
        try:
            raw = self.root / path
            if raw.is_symlink() or any(parent.is_symlink() for parent in raw.parents if parent.is_relative_to(self.root)):
                return False
            # The workspace was resolved when Discovery opened. Resolve the
            # candidate on every check; positive permissions are never cached.
            candidate = raw.resolve()
            if not candidate.is_relative_to(self.root):
                return False
            relative = candidate.relative_to(self.root)
            parts = tuple(part.lower() for part in relative.parts)
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in policy['hard']):
                return False
            if policy.get('entry_error') or candidate in policy['denied']:
                return False
            if candidate in policy['exact'] and candidate.is_file():
                return True
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in policy['exclusions']):
                return False
            return not policy.get('root_error') and any(candidate == root or candidate.is_relative_to(root) for root in policy['roots'])
        except (OSError, ValueError, HarnessError):
            return False

    def _entrypoints(self, coverage):
        """Existing declarations plus exact capability/Skill routes, never a crawl."""
        entries = list(coverage.get('entrypoints', []))
        declared = {e['path'] for e in entries}
        paths = list(CAPABILITY_PATHS)
        paths.extend(row['path'] for row in self.db.execute(
            "SELECT path FROM files WHERE path LIKE '.agents/skills/%/SKILL.md' "
            "AND presence IN ('present','missing','unknown') ORDER BY path")
            if len(Path(row['path']).parts) == 4)
        for path in paths:
            if path not in declared and self._permitted(path, coverage):
                entries.append({'id': 'file:' + path, 'path': path, 'kind': 'project_entry',
                    'discovery_role': 'skill_entry' if Path(path).name == 'SKILL.md' else 'capability_entry',
                    'discovery_basis': 'Existing exact workspace route within trusted saved read scope'})
        return entries

    def _entry_snapshot(self, entry):
        """Refresh only a known small original text; keep the persisted index intact."""
        path = entry['path']
        if not self._permitted(path):
            return None
        cache = self._permission_policy['entry_snapshots']
        if path in cache:
            return cache[path]
        row = self.db.execute('SELECT * FROM files WHERE path=?', (path,)).fetchone()
        item = dict(row) if row else None
        try:
            original = local_path(self.root, path)
            st = original.stat()
            current = item and item['presence'] == 'present' and (st.st_size, st.st_mtime_ns, st.st_ctime_ns) == (item['size'], item['mtime'], item['ctime'])
            if current:
                item['current_file'] = 'matches_scan_metadata'
            elif original.is_file() and original.suffix.lower() in TEXT_SUFFIXES and st.st_size <= MAX_TEXT:
                # Check the original location before reading. The byte limit is
                # also enforced on the read if the file grows after stat().
                if not self._permitted(path):
                    return None
                with original.open('rb') as stream:
                    raw = stream.read(MAX_TEXT + 1)
                if len(raw) > MAX_TEXT:
                    raise OSError('Original entry exceeds bounded read size')
                text = safe_text(raw.decode('utf-8-sig', errors='replace'))
                item = {'path': path, 'sha': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                    'mtime': st.st_mtime_ns, 'ctime': st.st_ctime_ns, 'presence': 'present',
                    'text_state': 'complete', 'identifiers': '[]', 'text': text,
                    'error': None, 'scanned_at': row['scanned_at'] if row else None,
                    'current_file': 'read_current_original'}
            elif item:
                item['current_file'] = 'changed_since_index'
        except OSError:
            if item:
                item['current_file'] = 'unavailable'
        cache[path] = item
        return item

    @staticmethod
    def _candidate_rank(item):
        applicability = item.get('applicability', {})
        scope = applicability.get('state') if isinstance(applicability, dict) else applicability
        # Ranking a usable navigation route is separate from scientific use.
        # Explicit conflicting/missing conditions lower its discovery priority;
        # an unclassified text hit still carries no adoption recommendation.
        scope_rank = 4 if scope == 'inapplicable' else 1 if scope == 'needs_context' else 0
        knowledge = item.get('knowledge', {})
        statuses = {item.get('status'), item.get('knowledge_status'), knowledge.get('status')}
        current = item.get('current_file', knowledge.get('current_file'))
        problem = 4 if statuses & (INACTIVE_ENTRIES | {'rejected'}) else 3 if current == 'unavailable' or item.get('presence') == 'missing' else 2 if 'conflict' in statuses or current == 'changed_since_index' else 0
        kind = item.get('kind', 'indexed_file')
        source = 0 if kind == 'registered_asset' and (item.get('active_for_scope') or item.get('explicit_version_pin')) else 1 if kind == 'original_entry' or item.get('original_entries') else 2 if kind == 'registered_asset' else 3 if kind == 'experience_candidate' else 4
        historical = any(part in {'history', 'original', 'source-snapshots', 'docs-before'} for part in Path(item.get('path', '')).parts)
        return (max(scope_rank, 0 if item.get('explicit_version_pin') else problem), source, item.get('entry_match_rank', 0), historical, item.get('path', item.get('id', '')))

    def _matching_entries(self, query, context=None):
        from .assets import text_match
        from .experiences import context_match
        entries = []
        for entry in self._entrypoints(self._permission_policy['coverage']):
            if not query.strip() and entry.get('discovery_basis'):
                continue  # General capability routes need a discovery query.
            if not self._permitted(entry['path']):
                continue
            item = self._entry_snapshot(entry)
            # Metadata is useful even when the original is unavailable; its
            # currentness stays visible and it never becomes a recommendation.
            metadata_match = text_match(query, {k: v for k, v in entry.items()
                if k not in {'discovery_basis', 'discovery_role', 'access', 'kind', 'status'}})
            body_match = item is not None and text_match(query, {'path': item['path'], 'text': item['text'], 'identifiers': item['identifiers']})
            if query.strip() and not metadata_match and not body_match:
                continue
            applicability = context_match(entry.get('applies', {}), context or {})
            if entry.get('discovery_basis') and not entry.get('applies'):
                applicability = {'state': 'unclassified', 'mismatches': [], 'missing': ['declared_applicability']}
            visible_text = re.sub(r'\]\([^)]*\)', ']', item['text']) if item else ''
            match_rank = 0 if metadata_match else 1 if text_match(query, visible_text) else 2
            text = item['text'] if item else ''
            needle = query.strip().casefold()
            where = text.casefold().find(needle) if needle else -1
            if where < 0:
                where = next((text.casefold().find(term) for term in needle.split() if term in text.casefold()), -1)
            entries.append({'kind': 'original_entry', 'id': entry['id'], 'path': entry['path'],
                'entry_kind': entry['kind'], 'status': entry.get('status', 'unspecified'),
                'applies': entry.get('applies', {}), 'applicability': applicability,
                'default_recommendation': False,
                'current_file': item.get('current_file', 'unavailable') if item else 'unavailable',
                'sha': item['sha'] if item else None,
                'knowledge_status': self.knowledge(item['sha'])['status'] if item else 'unknown',
                'entry_match_rank': match_rank,
                'snippet': text[max(0, where - 90):max(0, where - 90) + 320] if where >= 0 else '',
                'match_line': text.count('\n', 0, where) + 1 if where >= 0 else None,
                'entry': 'Discovery.inspect_entry(' + repr(entry['id']) + ')',
                'meaning': 'Original local entry candidate; declared scope and current text do not establish scientific applicability or adoption'})
        return sorted(entries, key=self._candidate_rank)

    def inspect_entry(self, identifier, *, max_members=20, text_chars=2400):
        """Read one declared original entry/manifest without registering members."""
        require(type(max_members) is int and 0 <= max_members <= 100, 'Manifest member limit must be 0..100')
        require(type(text_chars) is int and 0 <= text_chars <= 10000, 'Entry text budget must be 0..10000')
        coverage = self.coverage()
        with self._read_scope(coverage):
            entry = next((e for e in self._entrypoints(coverage) if e['id'] == identifier), None)
        require(entry is not None, 'Unknown discovery entrypoint')
        require(self._permitted(entry['path'], coverage), 'Entry is outside the trusted local read scope', 'ACCESS_SCOPE')
        path = local_path(self.root, entry['path'])
        require(path.is_file() and not path.is_symlink(), 'Original entrypoint is unavailable')
        require(path.stat().st_size <= MAX_TEXT, 'Entry exceeds bounded inspection size')
        # Permission check occurs before the first body read.
        text = path.read_text(encoding='utf-8-sig')
        result = {'id': identifier, 'path': str(path.relative_to(self.root)), 'sha256': digest(path),
            'kind': entry['kind'], 'applies': entry.get('applies', {}), 'access': 'trusted_local_scope',
            'observed_at': now(), 'registration': 'not_created', 'scientific_adoption': 'unchanged',
            'coverage': 'This exact original entry and bounded declared references only'}
        if path.suffix.lower() != '.json':
            result.update(text=safe_text(text[:text_chars]), partial=len(text) > text_chars)
            return result
        data = json.loads(text)
        selected = {}
        if isinstance(data, dict):
            for key in ('schema', 'schema_version', 'id', 'version', 'title', 'name', 'license', 'rights', 'access', 'limitations'):
                if key in data:
                    selected[key] = safe_text(encoded(data[key]))[:1200]
        members = []
        examined = 0
        def visit(obj, key='', depth=0):
            nonlocal examined
            if depth > 8 or examined >= 5000:
                return
            examined += 1
            if isinstance(obj, dict):
                for name, value in obj.items():
                    visit(value, name, depth + 1)
            elif isinstance(obj, list):
                for value in obj:
                    visit(value, key, depth + 1)
            elif isinstance(obj, str) and key.lower() in {'path', 'file', 'manifest', 'entrypoint', 'artifact', 'source_file'} and len(obj) <= 4000:
                if obj.startswith(('http://', 'https://')):
                    members.append({'url': safe_text(obj), 'state': 'remote_not_read'})
                    return
                try:
                    if entry.get('reference_base'):
                        target = local_path(self.root, str(local_path(self.root, entry['reference_base']) / obj))
                    else:
                        direct = local_path(self.root, obj)
                        sibling = local_path(self.root, str(path.parent / obj))
                        # Relative professional outputs conventionally belong
                        # to the manifest directory; a repository README with
                        # the same basename must not shadow that member.
                        target = sibling if sibling.exists() or not direct.exists() else direct
                    relative = str(target.relative_to(self.root))
                    if self._permitted(relative, coverage):
                        members.append({'path': relative, 'presence': 'present' if target.exists() else 'missing',
                            'meaning': 'Declared professional reference; no automatic registration/adoption'})
                    else:
                        members.append({'state': 'outside_declared_read_scope', 'path': '[withheld]'})
                except (OSError, HarnessError):
                    members.append({'state': 'outside_workspace', 'path': '[withheld]'})
        visit(data)
        result.update(metadata=selected, members=members[:max_members], declared_reference_count=len(members),
            omitted_members=max(0, len(members) - max_members), traversal_examined=examined,
            traversal_complete=examined < 5000)
        return result

    def knowledge(self, sha):
        if not self.state or not sha:
            return {'status': 'unknown', 'basis': 'No connected exact-content adoption evidence', 'records': []}
        def load():
            by_hash = {}
            for row in self.state.db.execute("SELECT * FROM observations WHERE kind IN ('adoption','reading','access') ORDER BY id"):
                record = dict(row); body = json.loads(record.pop('body'))
                identity = body.get('content_identity', {})
                if not identity.get('sha256'):
                    continue  # Legacy paths cannot prove which old bytes were adopted.
                by_hash.setdefault(identity['sha256'], []).append({**record, **body})
            return by_hash
        records = cached(('discovery-observations', id(self.state.db), self.state.db.total_changes), load).get(sha, [])
        adoptions = [r for r in records if r['kind'] == 'adoption']
        # Event order is used only within the same explicit asset/scope history.
        latest = {}
        for record in adoptions:
            latest[(record['asset'], encoded(record.get('scope', 'unspecified')))] = record
        statuses = {r.get('status', 'unknown') for r in latest.values()}
        scope_statuses = {}
        for r in latest.values():
            scope_statuses.setdefault(encoded(r.get('scope','unspecified')), set()).add(r.get('status','unknown'))
        conflict = any(v & {'adopted','conditional'} and v & {'superseded','retracted','rejected'} for v in scope_statuses.values())
        status = 'conflict' if conflict else next(iter(statuses)) if len(statuses) == 1 else 'multiple_scopes' if statuses else 'unknown'
        return {'status': status, 'current_scopes': list(latest.values()), 'records': records,
                'basis': 'Exact captured content; review scope and source remain authoritative, dates alone do not decide applicability'}

    def find(self, query, limit=10, *, _include_assets=True):
        with self._read_scope():
            return self._find(query, limit, _include_assets=_include_assets)

    def _index_candidates(self, query):
        """Only paths leave SQLite; false positives retain the exact final check."""
        clauses, parameters = [], []
        sources = _casefold_sources()
        # A subset stays conservative for unusually long queries and avoids
        # SQLite expression-depth limits; final text_match still checks all terms.
        terms = sorted(dict.fromkeys(query.strip().casefold().split()), key=len, reverse=True)[:16]
        for term in terms:
            # JSON quoting mirrors text_match's encoded string, including
            # escaped control characters/backslashes and its quote wrapper.
            clause = 'instr(lower(search_text), ?) > 0'
            parameters.append(term)
            variants = set(sources.get('', ()))
            for char in set(term):
                variants.update(sources.get(char, ()))
            if variants:
                variants = sorted(variants)
                if len(variants) <= 16:
                    # Single-character instr avoids a Unicode character-class
                    # comparison at every character of large indexed bodies.
                    clause = '(' + clause + ' OR ' + ' OR '.join('instr(search_text, ?) > 0' for _ in variants) + ')'
                    parameters.extend(variants)
                else:
                    # All sources are non-ASCII: none can be GLOB's [, ], - or ^.
                    clause = '(' + clause + ' OR search_text GLOB ?)'
                    parameters.append('*[' + ''.join(variants) + ']*')
            clauses.append(clause)
        return self.db.execute("SELECT path FROM (SELECT path, "
            "json_quote(path || char(10) || identifiers || char(10) || text) AS search_text "
            "FROM files WHERE presence IN ('present','missing','unknown')) WHERE "
            + ' AND '.join(clauses) + ' ORDER BY path', parameters)

    def _find(self, query, limit, *, _include_assets, context=None):
        require(bool(query.strip()), "Empty search query")
        require(1 <= limit <= 50, "Search limit must be 1..50")
        from .assets import text_match
        # Literal identities and Chinese phrases retain their meaning; spaced
        # terms can match separate parts of the same original text.
        needle = query.casefold()
        found = []
        coverage = self._permission_policy['coverage']
        entries = self._matching_entries(query, context)
        by_path = {}
        for entry in entries:
            by_path.setdefault(entry['path'], []).append(entry)
        examined = restricted = candidates = 0
        metadata_rows = self._index_candidates(query)
        # A known original can now match even when its indexed bytes do not.
        # This set deduplicates candidates; it never caches access decisions.
        seen = set()
        for metadata in chain(metadata_rows, ({'path': path} for path in by_path)):
            if metadata['path'] in seen:
                continue
            seen.add(metadata['path'])
            candidates += 1
            if not self._permitted(metadata['path'], coverage):
                restricted += 1
                continue
            # Only permitted rows load indexed body text into the caller.
            snapshots = self._permission_policy['entry_snapshots']
            row = snapshots[metadata['path']] if metadata['path'] in snapshots else self.db.execute('SELECT * FROM files WHERE path=?', (metadata['path'],)).fetchone()
            if row is None:
                continue
            examined += 1
            item = dict(row)
            hay = (item["path"] + "\n" + item["identifiers"] + "\n" + item["text"]).casefold()
            if not text_match(query, hay) and item['path'] not in by_path:
                continue
            where = item["text"].casefold().find(needle)
            if where < 0:
                where = next((item['text'].casefold().find(term) for term in needle.split() if term in item['text'].casefold()), -1)
            snippet = item["text"][max(0, where - 90):max(0, where - 90) + 320] if where >= 0 else ""
            knowledge = self.knowledge(item['sha'])
            currentness = item.get('current_file', 'unavailable')
            if 'current_file' not in item:
                try:
                    st = local_path(self.root, item['path']).stat()
                    currentness = 'matches_scan_metadata' if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) == (item['size'], item['mtime'], item['ctime']) else 'changed_since_index'
                except OSError:
                    pass
            knowledge = {**knowledge, 'applies_to_sha256': item['sha'], 'current_file': currentness}
            if currentness not in {'matches_scan_metadata', 'read_current_original'}:
                knowledge = {'status': 'needs_reindex', 'indexed_content': knowledge, 'current_file': currentness}
            entry_refs = [{k: e[k] for k in ('id', 'entry', 'status', 'applicability')} for e in by_path.get(item['path'], [])]
            candidate = {k: item[k] for k in ("path", "sha", "size", "presence", "text_state", "error", "scanned_at")} | {
                'identifiers': json.loads(item['identifiers']), 'snippet': snippet,
                'match_line': item['text'].count("\n", 0, where) + 1 if where >= 0 and item['text_state'] in ('complete', 'partial', 'partial_encoding') else None,
                'knowledge': knowledge, 'current_file': currentness,
                'match_basis': 'current_original_entry' if currentness == 'read_current_original' else 'indexed_text_or_declared_entry',
                'original_entries': entry_refs}
            if entry_refs:
                candidate.update(applicability=entry_refs[0]['applicability'], status=entry_refs[0]['status'],
                    entry_match_rank=by_path[item['path']][0]['entry_match_rank'])
            validity_rank = 0 if knowledge['status'] in ('adopted', 'conditional') else 2 if knowledge['status'] in ('superseded', 'retracted', 'rejected') else 1
            score = (*self._candidate_rank(candidate)[:-1], validity_rank, needle not in item['path'].casefold(), item['path'])
            found.append((score, candidate))
        found.sort(key=lambda x: x[0])
        groups = {}
        for _, item in found:
            identity = (item["sha"] or item["path"], item["presence"])
            if identity in groups:
                groups[identity]["other_paths"].append(item["path"])
            else:
                groups[identity] = {**item, "other_paths": []}
        registered = []
        if self.state and _include_assets:
            from .assets import AssetRegistry
            registered = AssetRegistry(self.state).search(query, limit=50,
                can_read=lambda path: self._permitted(path, coverage), include_history=False)['items']
        return {"query": query, "coverage": coverage, "matching_files": len(found),
                'search_coverage': {'permitted_bodies_examined': examined, 'restricted_metadata_rows': restricted,
                    'candidate_paths_checked': candidates,
                    'candidate_filter': 'Conservative SQLite text candidates plus exact original entries; dynamic access checks and exact text_match remain in force'},
                'registered_assets': registered[:limit], 'matching_assets': len(registered),
                'original_entries': entries[:limit], 'matching_entries': len(entries),
                'original_read_coverage': 'Exact declared entries, known capability pages and indexed Skill paths only; current bounded text reads do not rebuild the index',
                "results": list(groups.values())[:limit], "not_found_means": "No match in the declared indexed scope and bounded original entries; not proof of absence from the project"}

    def retrieve(self, query, context, *, limit=8, budget=16000):
        """Bounded cross-entry retrieval with separately protected required facts."""
        with self._read_scope():
            return self._retrieve(query, context, limit=limit, budget=budget)

    def _retrieve(self, query, context, *, limit, budget):
        from .assets import AssetRegistry
        from .experiences import ExperienceRegistry, bounded_selection, context_match
        context_match({}, context)
        coverage = self._permission_policy['coverage']
        permitted = lambda path: self._permitted(path, coverage)
        if self.state:
            context = ExperienceRegistry(self.state, can_read=permitted).resolve_context(context)
        files = self._find(query, 50, _include_assets=False, context=context) if query.strip() else {'results': [], 'matching_files': 0}
        entries = self._matching_entries(query, context)
        optional, unclassified = [], []
        for item in files['results']:
            if item.get('original_entries'):
                continue  # The original route already carries its focused excerpt.
            declared = [e for e in coverage.get('entrypoints', []) if e['path'] == item['path'] and e.get('applies')]
            matches = [context_match(e['applies'], context) for e in declared]
            if not declared:
                unclassified.append({k: item[k] for k in ('path', 'sha', 'presence', 'text_state', 'snippet', 'match_line')})
            optional.append({'kind': 'indexed_file', **item,
                'applicability': 'entry_declared_match' if any(m['state'] == 'declared_match' for m in matches) else
                    matches[0]['state'] if matches else 'unclassified_discovery_only',
                'default_recommendation': False,
                'meaning': 'A text hit; source adoption and current applicability require its original evidence'})
        asset_registry = AssetRegistry(self.state) if self.state else None
        registered = asset_registry.search(query, context, limit=50, can_read=permitted, include_history=False) if asset_registry else {'items': [], 'count': 0}
        entries = [entry for entry in entries if entry['status'] not in INACTIVE_ENTRIES]
        # Legacy asset uses keep their original natural-language purposes.
        # A trusted original entry can classify a receiving purpose without
        # rewriting that scientific use or elevating the query into authority.
        pins = context.get('asset_versions', {})
        asset_items = {(i['id'], i['version']): i for i in registered['items']
            if i['status'] not in {'retired', 'retracted', 'superseded'} or pins.get(i['id']) == i['version']}
        for entry in coverage.get('entrypoints', []):
            if not asset_registry or not entry.get('registered_asset') or not any(e['id'] == entry['id'] and e['applicability']['state'] == 'declared_match' for e in entries):
                continue
            legacy_context = {k: v for k, v in context.items() if k != 'purpose'}
            linked = asset_registry.search(entry['registered_asset'], legacy_context, limit=50, can_read=permitted, include_history=False)
            for item in linked['items']:
                if item['status'] in {'retired', 'retracted', 'superseded'} and pins.get(item['id']) != item['version']:
                    continue
                if item['id'] == entry['registered_asset'] and (item['is_current'] or context.get('asset_versions', {}).get(item['id']) == item['version']):
                    asset_items[(item['id'], item['version'])] = {**item, 'receiving_purpose': context.get('purpose'),
                        'via_original_entry': entry['id'], 'purpose_basis': 'Trusted entry classification; original declared uses and scientific adoption remain authoritative'}
        # Trusted capability/Skill entries already belong to the scan's
        # explicit entrypoints or full text; no new crawling service is needed.
        optional = ([{'kind': 'registered_asset', **item, 'explicit_version_pin': pins.get(item['id']) == item['version']} for item in asset_items.values()] + entries + optional)
        mandatory = []
        experience = None
        if self.state:
            registry = ExperienceRegistry(self.state, can_read=permitted)
            experience = registry.select(context, query, limit=limit, budget=1000000)
            mandatory = registry.required(experience['required_context'])
            optional += experience['selected']
        optional.sort(key=self._candidate_rank)
        # A small budget may retain the usable original route without loading
        # all its display metadata. Required constraints/counterexamples remain
        # protected by bounded_selection and are never compacted here.
        available = budget - len(encoded(mandatory).encode())
        compacted, selection_count = [], 0
        for item in optional:
            if item.get('kind') == 'original_entry' and len(encoded(item).encode()) > available:
                item = {key: item[key] for key in ('kind', 'id', 'path', 'sha', 'entry', 'status', 'knowledge_status', 'applicability', 'current_file', 'default_recommendation')}
                item['details_omitted'] = True
            compacted.append(item)
            cost = len(encoded(item).encode())
            if selection_count < limit and cost <= available:
                available -= cost
                selection_count += 1
        result = bounded_selection(mandatory, compacted, limit=limit, budget=budget)
        if any(item.get('required_load') for item in mandatory):
            result['context_complete'] = False
            result['unresolved_required'] = [item for item in mandatory if item.get('required_load')]
        return {**result, 'query': query, 'context': context, 'coverage': coverage,
            'matching_files': files['matching_files'], 'matching_assets': len(asset_items),
            'search_coverage': files.get('search_coverage', {'state': 'no_file_query'}),
            'direct_registry_match_count': registered['count'],
            'available_asset_candidates': len(asset_items),
            'matching_entries': len(entries), 'inactive_experience_count': experience.get('inactive_count', 0) if experience else 0,
            'unclassified_files': unclassified[:limit], 'unclassified_file_count': len(unclassified),
            'unclassified_meaning': 'Accessible unscoped text excerpts are discovery candidates; inspect the original and applicability before use',
            'not_found_means': 'No match in declared local index/registry/bounded original entries; not proof of absence; inaccessible and unregistered sources may exist'}
