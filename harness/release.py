"""Fixed local code bundles and read-only deployment CAS plans. No production installer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil

from .common import digest, encoded, now, require
from . import migrations

FORMAT = 'htam.release/v1'
EXTENSIONS = {'.py', '.mjs', '.json', '.md', '.sh', '.txt', '.ps1'}


def _relative(name):
    value = PurePosixPath(name)
    require(not value.is_absolute() and name == value.as_posix() and all(part not in ('', '.', '..') for part in value.parts)
            and '\\' not in name and ':' not in name, 'Invalid release member path', 'RELEASE_INVALID')
    require(value.parts[0] in ('harness', 'scripts', 'tests') and value.suffix in EXTENSIONS,
            'Release member is outside approved code scope', 'RELEASE_INVALID')
    return value


def _source_files(root, include_tests=False):
    root = Path(root).resolve()
    result = []
    roots = [root / 'harness'] + ([root / 'tests/harness'] if include_tests else [])
    for folder in roots:
        require(folder.is_dir() and not folder.is_symlink(), 'Missing or linked code directory: ' + str(folder), 'RELEASE_INVALID')
        for path in folder.rglob('*'):
            if any(part in ('__pycache__', '.pytest_cache') for part in path.relative_to(root).parts):
                continue
            require(not path.is_symlink(), 'Symlink is outside fixed release file contract', 'RELEASE_INVALID')
            if path.is_file():
                require(path.suffix in EXTENSIONS, 'Unexpected non-code release file: ' + str(path), 'RELEASE_INVALID')
                result.append(path)
    for name in ('scripts/harness.py', 'scripts/harness_release.py'):
        path = root / name
        require(path.is_file() and not path.is_symlink(), 'Missing exact script entry: ' + name, 'RELEASE_INVALID')
        result.append(path)
    return sorted(result)


def _identity(files):
    return hashlib.sha256(encoded(files).encode()).hexdigest()


def package(source, output, *, include_tests=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    require(not output.is_relative_to(source), 'Bundle destination must be outside the source checkout', 'RELEASE_INVALID')
    files = _source_files(source, include_tests)
    output.mkdir(parents=True, exist_ok=False)
    records = {}
    for path in files:
        name = path.relative_to(source).as_posix(); _relative(name)
        target = output / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        records[name] = {'sha256': digest(target), 'bytes': target.stat().st_size, 'mode': path.stat().st_mode & 0o777}
    require(sorted(p.relative_to(source).as_posix() for p in _source_files(source, include_tests)) == sorted(records)
            and all(digest(source / name) == item['sha256'] for name, item in records.items()),
            'Source changed during release capture; prepare a new fixed bundle', 'RELEASE_SOURCE_CHANGED')
    manifest = {'format': FORMAT, 'release_id': _identity(records), 'observed_at': now(), 'files': records,
        'schema_version': migrations.VERSION, 'includes_tests': include_tests,
        'scope': 'Explicit harness code, exact script entries and optional harness tests. No workspaces, state/index databases, credentials, native CLI or scientific payloads.'}
    with (output / 'release-manifest.json').open('x') as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return verify(output)


def verify(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / 'release-manifest.json').read_text())
    require(manifest.get('format') == FORMAT and isinstance(manifest.get('files'), dict)
            and manifest.get('release_id') == _identity(manifest['files']), 'Release identity differs', 'RELEASE_INVALID')
    actual = set()
    for path in bundle.rglob('*'):
        if '__pycache__' in path.relative_to(bundle).parts:
            continue
        require(not path.is_symlink(), 'Release contains a symlink', 'RELEASE_INVALID')
        if path.is_file() and path.relative_to(bundle).as_posix() != 'release-manifest.json':
            actual.add(path.relative_to(bundle).as_posix())
    require(actual == set(manifest['files']), 'Release has missing or extra files', 'RELEASE_INVALID')
    for name, item in manifest['files'].items():
        _relative(name); target = bundle / name
        require(target.is_file() and target.stat().st_size == item['bytes'] and digest(target) == item['sha256'],
                'Release member changed: ' + name, 'RELEASE_INVALID')
    return {'release_id': manifest['release_id'], 'file_count': len(actual), 'manifest': manifest, 'verified_at': now()}


def install_new(bundle, target):
    """Rehearsal/new installation only. Never overwrite an existing checkout."""
    evidence = verify(bundle); target = Path(target).resolve(); target.mkdir(parents=True, exist_ok=False)
    for name, item in evidence['manifest']['files'].items():
        output = target / name; output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(bundle) / name, output); output.chmod(item['mode'])
    shutil.copyfile(Path(bundle) / 'release-manifest.json', target / 'release-manifest.json')
    return verify(target)


def deployment_plan(bundle, workspace, state_path):
    """Read-only point-in-time plan; apply still needs root's in-flight coordination."""
    evidence = verify(bundle); workspace, state_path = Path(workspace).resolve(), Path(state_path).resolve()
    before = {}
    for name in evidence['manifest']['files']:
        if name.startswith('tests/'):
            continue
        path = workspace / name
        require(not any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(workspace))
                and (not path.exists() or path.is_file()), 'Deployment target is linked/not a file: ' + name, 'RELEASE_INVALID')
        before[name] = digest(path) if path.exists() else None
    return {'format': 'htam.deployment-plan/v1', 'release_id': evidence['release_id'], 'workspace': str(workspace),
        'state_path': str(state_path), 'observed_at': now(), 'before_file_hashes': before,
        'migration_plan': migrations.preflight(state_path),
        'coverage': 'Read-only observed code and logical data CAS. Not a writer lock or approval. No production code/data written.',
        'required_before_execution': ['Coordinate all old writers and in-flight owned runtime responsibilities',
            'Fresh persistent SQLite backup under the explicit migration lock', 'Recheck exact code hashes and migration fingerprint',
            'Install reviewed source bytes only and keep existing entry/hook caller compatibility',
            'Verify all prior rows and reconcile any post-backup delta before recovery; never replace live state with an old snapshot']}


def check_plan(bundle, plan):
    require(plan.get('format') == 'htam.deployment-plan/v1', 'Unknown deployment plan', 'RELEASE_INVALID')
    evidence = verify(bundle)
    require(evidence['release_id'] == plan['release_id'], 'Bundle differs from deployment plan', 'RELEASE_PLAN_STALE')
    require(set(plan['before_file_hashes']) == {name for name in evidence['manifest']['files'] if not name.startswith('tests/')},
            'Deployment file scope differs from fixed bundle', 'RELEASE_INVALID')
    root = Path(plan['workspace'])
    for name, expected in plan['before_file_hashes'].items():
        _relative(name); path = root / name
        require(not path.is_symlink() and (digest(path) if path.is_file() else None) == expected,
                'Code changed after deployment plan: ' + name, 'RELEASE_PLAN_STALE')
    current = migrations.preflight(plan['state_path'])
    require(all(current[key] == plan['migration_plan'][key] for key in
                ('fingerprint', 'source_version', 'target_version', 'migration_id', 'migration_checksum')),
            'State/migration changed after deployment plan', 'RELEASE_PLAN_STALE')
    return {'matched': True, 'release_id': evidence['release_id'], 'observed_at': now(),
        'coverage': 'Point-in-time CAS matched; no lock retained and no production apply performed'}
