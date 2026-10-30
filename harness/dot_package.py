"""Bounded framework excerpts for native Dot attachment delivery; no State access."""
from __future__ import annotations

import ctypes
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from .common import HarnessError, encoded, now, require

FORMAT = 'htam.dot-package/v1'
PROFILE = 'framework_onboarding_only'
SELECTION_FORMAT = 'h2-excerpts-lf/v1'
SOURCES = (
    ('architecture/personal-harness-design.md', 'references/architecture-guide.md', (
        '1 架构各部分的职责', '5 Dot统筹、执行与必要审阅', '6 共同障碍、专业能力与投入选择',
        '7 反馈、同步和效果验证', '8 运行、迁移与恢复', '10 Dot云端接入安排')),
    ('harness/README.md', 'references/operations-guide.md', (
        '文档与状态的维护位置', '日常使用', '防止失焦的日常判断', '经验同步与RSI')),
)
STOP = '订阅实验的受信Python入口'
PAYLOADS = ('current.md', 'references/architecture-guide.md', 'references/operations-guide.md')
ALLOWLIST = tuple(sorted((*PAYLOADS, 'manifest.json')))
GUIDE_PREFIX = {
    'references/architecture-guide.md': '# HTAM架构节选\n\n来源：architecture/personal-harness-design.md；仅含批准的框架章节。\n\n',
    'references/operations-guide.md': '# HTAM操作节选\n\n来源：harness/README.md；仅含批准的框架章节。\n\n',
}
LIMIT = 8 * 1024 * 1024


def _fail(message, code='DOT_PACKAGE_INVALID'):
    raise HarnessError(message, code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected),
            'Unexpected or missing Dot package manifest fields', 'DOT_PACKAGE_INVALID')


def _safe_path(value):
    path = Path(value)
    require('..' not in path.parts, 'Parent traversal is outside the Dot package path contract', 'DOT_PACKAGE_INVALID')
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (path, *path.parents)),
            'Dot package paths must not contain symlinks', 'DOT_PACKAGE_INVALID')
    return path


def _read(path):
    require(path.is_file() and not path.is_symlink(), 'Missing or linked Dot package source/member: ' + str(path), 'DOT_PACKAGE_INVALID')
    require(path.stat().st_size <= LIMIT, 'Dot package source/member exceeds the bounded size', 'DOT_PACKAGE_INVALID')
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    require(len(data) <= LIMIT, 'Dot package source/member exceeds the bounded size', 'DOT_PACKAGE_INVALID')
    return data


def _matches(heading, selector):
    # Only exact titles and a single historical parenthetical suffix are accepted.
    return re.fullmatch(re.escape(selector) + r'(?:[ \t]*(?:（[^（）\r\n]+）|\([^()\r\n]+\)))?', heading) is not None


def _headings(lines):
    result, fence = [], None
    for index, line in enumerate(lines):
        stripped = line.lstrip(' ')
        if len(line) - len(stripped) <= 3:
            match = re.match(r'(`{3,}|~{3,})', stripped)
            if match:
                marker = match.group(1)
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence) and not stripped[len(marker):].strip():
                    fence = None
                continue
        if fence is None:
            match = re.match(r'^(#{1,6})[ \t]+(.+?)[ \t]*(?:[ \t]+#+[ \t]*)?\n?$', line)
            if match:
                result.append((index, len(match.group(1)), match.group(2)))
    return result


def _select(raw, selectors, *, operations=False):
    text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    lines = text.splitlines(keepends=True)
    headings = _headings(lines)
    records, blocks = [], []
    for selector in selectors:
        matches = [(i, title) for i, level, title in headings if level == 2 and _matches(title, selector)]
        require(len(matches) == 1, 'Source H2 missing or ambiguous: ' + selector, 'DOT_PACKAGE_SOURCE_INVALID')
        start, title = matches[0]
        end = next((i for i, level, _ in headings if i > start and level <= 2), len(lines))
        if operations and selector == '经验同步与RSI':
            stops = [i for i, level, heading in headings if start < i < end and level == 3 and _matches(heading, STOP)]
            require(len(stops) == 1, 'Source RSI boundary missing or ambiguous', 'DOT_PACKAGE_SOURCE_INVALID')
            end = stops[0]
        block = (''.join(lines[start:end]).rstrip('\n') + '\n').encode('utf-8')
        records.append({'selector': selector, 'heading': title, 'bytes': len(block), 'sha256': _sha(block)})
        blocks.append(block)
    return b'\n'.join(blocks), records


def _capture(root):
    sources, guides, raw_files = [], {}, {}
    for name, guide, selectors in SOURCES:
        raw = _read(_safe_path(root / name))
        selected, sections = _select(raw, selectors, operations=name == 'harness/README.md')
        raw_files[name] = raw
        sources.append({'path': name, 'guide': guide, 'sha256': _sha(raw),
                        'selected_payload_sha256': _sha(selected), 'sections': sections})
        guides[guide] = GUIDE_PREFIX[guide].encode('utf-8') + selected
    return sources, guides, raw_files


def _config(source_thread, authorization_reference, cloud_directory, local_source_root):
    values = {'source_thread': source_thread, 'authorization_reference': authorization_reference,
              'cloud_directory': cloud_directory, 'local_source_root': local_source_root}
    require(all(isinstance(v, str) and v.strip() and len(v) <= 4096 and
                not any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values.values()),
            'Dot package configuration needs nonempty single-line text', 'DOT_PACKAGE_INVALID')
    cloud = PurePosixPath(cloud_directory)
    require(cloud.is_absolute() and len(cloud.parts) > 1 and str(cloud) == cloud_directory
            and '..' not in cloud.parts and '\\' not in cloud_directory,
            'Cloud directory must be an exact absolute POSIX directory', 'DOT_PACKAGE_INVALID')
    return values


def _current(config):
    # Configuration is rendered as JSON strings, so it cannot add Markdown instructions.
    quoted = {k: json.dumps(v, ensure_ascii=False) for k, v in config.items()}
    return ('''# HTAM框架接续入口

本包用于Dot恢复HTAM的框架职责、日常判断和同步方式。科研工作范围为空；正式工作状态、原始资料和运行责任由本地原入口承接。

Dot统筹用户用途、整体取舍、会话选择与返回；执行者实施准确授权范围并自查，必要的独立审阅沿原review owner。接续时先读本页和manifest，再按用途读[架构节选](references/architecture-guide.md)和[操作节选](references/operations-guide.md)；用户当前输入优先。

稳定云端读取入口（实际存在及长期保留情况由外部交付回执核实）：
```json
''' + quoted['cloud_directory'] + '''
```

来源会话：
```json
''' + quoted['source_thread'] + '''
```

本次授权出处标签（由调用者明确提供，使用时回查原用户消息）：
```json
''' + quoted['authorization_reference'] + '''
```

本地来源根目录：
```json
''' + quoted['local_source_root'] + '''
```

本地可调用帮助：在来源根目录运行 `python3 scripts/harness.py dot-package --help`；具体参数见 `dot-package export --help` 和 `dot-package check --help`。`check --check-sources`分别核选定章节和完整源文件漂移，未请求来源核对时新鲜度为unknown。

包缺失、陈旧或云端环境重建时，从本地原件生成一个新目录，经校验后使用已获授权的原生聊天附件交付，沿实际原生落盘与回读回执恢复入口。沿同一原生HTAM定位笔记找回本目录的 `current.md` 和 `manifest.json`；笔记承担定位，正式状态和授权回查原来源。云端唯一新产物先回收并核对，再整理旧包。

本包生成状态为prepared_local_only；生成与本地校验不声明已传输、已在云端落盘或已被Dot读取。实际交付、目录及文件SHA256回执保存在包外的原任务记录。manifest自身SHA256由命令输出报告并可另存回执，避免自引用。

本包不导出State、科研摘要、模型、实验逐例记录或凭证。它提供框架读取副本；运行接管、科学采用、外部操作、后台任务和新增访问均沿准确原授权及实际入口。本包不授予这些权限，也不恢复旧运行或旧授权。
''').encode('utf-8')


def _identity(manifest):
    return _sha(encoded({k: v for k, v in manifest.items() if k not in ('package_id', 'generated_at')}).encode('utf-8'))


def _publish_new(staging, output):
    """Atomic directory publication without replacing even an empty raced destination."""
    if os.name == 'nt':
        os.rename(staging, output)  # Windows rename refuses an existing destination.
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None, 'Atomic no-replace directory publication is unavailable on this platform', 'DOT_PACKAGE_OUTPUT_INVALID')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if rename(parent_fd, os.fsencode(staging.name), parent_fd, os.fsencode(output.name), 1) != 0:
            error = ctypes.get_errno()
            if error in (errno.EEXIST, errno.ENOTEMPTY):
                _fail('Dot package destination already exists; use a new directory', 'DOT_PACKAGE_OUTPUT_EXISTS')
            raise OSError(error, os.strerror(error))
    finally:
        os.close(parent_fd)


def export(root, output, *, source_thread, authorization_reference, cloud_directory):
    staging = None
    try:
        root, output = _safe_path(root), _safe_path(output)
        require(not output.exists(), 'Dot package destination already exists; use a new directory', 'DOT_PACKAGE_OUTPUT_EXISTS')
        require(output.parent.is_dir(), 'Dot package destination parent must already exist', 'DOT_PACKAGE_OUTPUT_INVALID')
        config = _config(source_thread, authorization_reference, cloud_directory, str(root))
        sources, payloads, before = _capture(root)
        payloads['current.md'] = _current(config)
        manifest = {'format': FORMAT, 'version': 1, 'profile': PROFILE, 'selection_format': SELECTION_FORMAT,
                    'generated_at': now(), 'config': config, 'research_work_scopes': [], 'state_exported': False,
                    'runtime_events_exported': False, 'delivery_status': 'prepared_local_only',
                    'allowlist': list(ALLOWLIST), 'sources': sources,
                    'files': {name: {'bytes': len(payloads[name]), 'sha256': _sha(payloads[name])} for name in PAYLOADS}}
        manifest['package_id'] = _identity(manifest)
        payloads['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        staging = Path(tempfile.mkdtemp(prefix='.dot-package-pending-', dir=output.parent))
        (staging / 'references').mkdir()
        for name, data in payloads.items():
            with (staging / name).open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        check(staging)
        # Full raw snapshots, including unselected parts, must remain stable during capture.
        after = {name: _read(_safe_path(root / name)) for name, _, _ in SOURCES}
        require(before == after, 'Source changed during Dot package capture; regenerate from stable sources', 'DOT_PACKAGE_SOURCE_CHANGED')
        _safe_path(output)
        _publish_new(staging, output)
        staging = None
        return {'package': str(output), 'package_id': manifest['package_id'], 'file_count': 4,
                'manifest_sha256': _sha(payloads['manifest.json']), 'delivery_status': 'prepared_local_only',
                'source_freshness': 'current_at_capture', 'coverage': 'Fixed framework excerpts only; no State opened or native delivery performed'}
    except HarnessError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        _fail('Dot package export failed: ' + str(exc))
    finally:
        if staging is not None:
            shutil.rmtree(staging)


def _validate(manifest, payloads):
    _keys(manifest, ('format', 'version', 'profile', 'selection_format', 'generated_at', 'config', 'research_work_scopes',
                     'state_exported', 'runtime_events_exported', 'delivery_status', 'allowlist', 'sources', 'files', 'package_id'))
    require(manifest['format'] == FORMAT and type(manifest['version']) is int and manifest['version'] == 1
            and manifest['profile'] == PROFILE and manifest['selection_format'] == SELECTION_FORMAT
            and isinstance(manifest['generated_at'], str) and bool(manifest['generated_at'])
            and manifest['research_work_scopes'] == [] and manifest['state_exported'] is False
            and manifest['runtime_events_exported'] is False and manifest['delivery_status'] == 'prepared_local_only'
            and manifest['allowlist'] == list(ALLOWLIST), 'Dot package profile/schema differs', 'DOT_PACKAGE_INVALID')
    require(datetime.fromisoformat(manifest['generated_at']).tzinfo is not None,
            'Dot package observation time needs an explicit timezone', 'DOT_PACKAGE_INVALID')
    _keys(manifest['config'], ('source_thread', 'authorization_reference', 'cloud_directory', 'local_source_root'))
    _config(**manifest['config'])
    require(payloads['current.md'] == _current(manifest['config']), 'Generated Dot entry differs from fixed configuration', 'DOT_PACKAGE_INVALID')
    _keys(manifest['files'], PAYLOADS)
    for name in PAYLOADS:
        item = manifest['files'][name]
        _keys(item, ('bytes', 'sha256'))
        require(type(item['bytes']) is int and item['bytes'] == len(payloads[name]) and _hash(item['sha256'])
                and item['sha256'] == _sha(payloads[name]), 'Dot package member differs: ' + name, 'DOT_PACKAGE_INVALID')
    require(isinstance(manifest['sources'], list) and len(manifest['sources']) == len(SOURCES), 'Dot source scope differs', 'DOT_PACKAGE_INVALID')
    for record, (name, guide, selectors) in zip(manifest['sources'], SOURCES):
        _keys(record, ('path', 'guide', 'sha256', 'selected_payload_sha256', 'sections'))
        require(record['path'] == name and record['guide'] == guide and _hash(record['sha256'])
                and _hash(record['selected_payload_sha256']) and isinstance(record['sections'], list),
                'Dot source identity differs', 'DOT_PACKAGE_INVALID')
        prefix = GUIDE_PREFIX[guide].encode('utf-8')
        require(payloads[guide].startswith(prefix), 'Dot guide prefix differs', 'DOT_PACKAGE_INVALID')
        selected = payloads[guide][len(prefix):]
        # The RSI stop heading is deliberately absent from the exported excerpt.
        reconstructed, sections = _select(selected, selectors)
        require(selected == reconstructed and record['sections'] == sections
                and record['selected_payload_sha256'] == _sha(selected), 'Dot guide selection/hash differs', 'DOT_PACKAGE_INVALID')
    require(_hash(manifest['package_id']) and manifest['package_id'] == _identity(manifest),
            'Dot package identity differs', 'DOT_PACKAGE_INVALID')


def _freshness(root, sources):
    if root is None:
        return {'status': 'unknown', 'selected_sections': 'unknown', 'full_source_files': 'unknown', 'reason': 'Source comparison not requested'}
    rows = []
    for record, (name, _, selectors) in zip(sources, SOURCES):
        row = {'path': name, 'selected_sections': 'unknown', 'full_source_file': 'unknown'}
        try:
            raw = _read(_safe_path(Path(root) / name))
            row['full_source_file'] = 'current' if _sha(raw) == record['sha256'] else 'stale'
            selected, _ = _select(raw, selectors, operations=name == 'harness/README.md')
            row['selected_sections'] = 'current' if _sha(selected) == record['selected_payload_sha256'] else 'stale'
        except (HarnessError, OSError, UnicodeError) as exc:
            row['reason'] = str(exc)
        rows.append(row)
    def aggregate(values):
        return 'stale' if 'stale' in values else 'unknown' if 'unknown' in values else 'current'
    selected = aggregate([row['selected_sections'] for row in rows])
    full = aggregate([row['full_source_file'] for row in rows])
    return {'status': aggregate([selected, full]), 'selected_sections': selected, 'full_source_files': full, 'files': rows}


def check(package, *, source_root=None, manifest_sha256=None):
    try:
        package = _safe_path(package)
        require(package.is_dir(), 'Dot package directory is missing', 'DOT_PACKAGE_INVALID')
        expected = set(ALLOWLIST) | {'references'}
        actual = set()
        for path in package.iterdir():
            require(not path.is_symlink(), 'Dot package contains a symlink', 'DOT_PACKAGE_INVALID')
            actual.add(path.name)
            if path.name == 'references' and path.is_dir():
                for member in path.iterdir():
                    require(member.is_file() and not member.is_symlink(), 'Dot package contains a linked or non-file member', 'DOT_PACKAGE_INVALID')
                    actual.add('references/' + member.name)
            else:
                require(path.is_file(), 'Dot package contains an unexpected directory', 'DOT_PACKAGE_INVALID')
        require(actual == expected, 'Dot package has missing or extra files/directories', 'DOT_PACKAGE_INVALID')
        raw_manifest = _read(package / 'manifest.json')
        actual_sha = _sha(raw_manifest)
        if manifest_sha256 is not None:
            require(_hash(manifest_sha256) and actual_sha == manifest_sha256, 'Dot manifest SHA256 differs from external identity', 'DOT_PACKAGE_INVALID')
        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, 'Duplicate Dot manifest JSON key', 'DOT_PACKAGE_INVALID')
                result[key] = value
            return result
        manifest = json.loads(raw_manifest.decode('utf-8'), object_pairs_hook=no_duplicates)
        payloads = {name: _read(package / name) for name in PAYLOADS}
        _validate(manifest, payloads)
        return {'package': str(package), 'package_id': manifest['package_id'], 'integrity': 'valid', 'file_count': 4,
                'manifest_sha256': actual_sha, 'external_manifest_matched': manifest_sha256 is not None,
                'source_freshness': _freshness(source_root, manifest['sources']), 'delivery_status': 'not_checked',
                'coverage': 'Package bytes/schema and requested source comparison only; no State or native delivery checked'}
    except HarnessError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        _fail('Malformed or unreadable Dot package: ' + str(exc))
