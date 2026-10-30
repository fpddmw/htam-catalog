from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
import time
from urllib.parse import quote
from datetime import datetime, timezone


class HarnessError(Exception):
    def __init__(self, message, code='VALIDATION_ERROR'):
        super().__init__(message)
        self.code = code


class ValidationTimeout(HarnessError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def require(condition, message, code='VALIDATION_ERROR'):
    if not condition:
        raise HarnessError(message, code)


def local_path(root, value):
    p = Path(value)
    p = (root / p).resolve() if not p.is_absolute() else p.resolve()
    require(p.is_relative_to(root.resolve()), "Path is outside the selected workspace")
    return p


_validation = ContextVar('htam_validation_scope', default=None)


@contextmanager
def validation_scope(timeout=None):
    """One invocation only: content/stat changes invalidate cached file facts."""
    parent = _validation.get()
    if parent is not None:
        yield parent
        return
    value = {'values': {}, 'hits': 0, 'loads': 0, 'deadline': time.monotonic() + timeout if timeout else None}
    token = _validation.set(value)
    try:
        yield value
    finally:
        _validation.reset(token)


def remaining_timeout(default):
    scope = _validation.get()
    if not scope or scope['deadline'] is None:
        return default
    remaining = scope['deadline'] - time.monotonic()
    if remaining <= 0:
        raise ValidationTimeout('Validation runtime budget expired; this action remains unverified')
    return min(default, remaining)


def cached(key, loader):
    scope = _validation.get()
    remaining_timeout(60)
    if scope is None:
        return loader()
    if key in scope['values']:
        scope['hits'] += 1
        return scope['values'][key]
    scope['loads'] += 1
    value = loader()
    scope['values'][key] = value
    return value


def digest(path):
    path = Path(path).resolve(); st = path.stat()
    def load():
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    return cached(('sha256', str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino), load)


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def connect(path, readonly=False, create=True):
    if not readonly and create:
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = 'ro' if readonly else 'rwc' if create else 'rw'
    db = sqlite3.connect('file:' + quote(str(path.resolve()), safe='/') + '?mode=' + mode, uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    if readonly:
        db.execute('PRAGMA query_only=ON')
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    return db
