"""Unprivileged, no-network bwrap launcher for frozen Python candidates.

The launcher is a local primitive, not an evaluator or model gateway. It exposes
only frozen candidate/input files and a size-limited tmpfs artifact directory.
Host output files are written by this trusted parent after safe archive parsing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import resource
import selectors
import signal
import stat
import subprocess
import tarfile
import time
import uuid


ROOT = Path(__file__).resolve().parent
BWRAP = Path('/usr/bin/bwrap')


class IsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 5.0
    cpu_seconds: int = 2
    memory_bytes: int = 256 * 1024 * 1024
    processes: int = 96
    open_files: int = 64
    file_bytes: int = 1024 * 1024
    output_bytes: int = 8 * 1024 * 1024
    artifact_files: int = 256
    log_bytes: int = 32768
    source_bytes: int = 4 * 1024 * 1024
    source_files: int = 256

    def validate(self):
        if not 0.2 <= self.wall_seconds <= 120:
            raise IsolationError('wall limit outside supported range')
        if not 1 <= self.cpu_seconds <= 30 or not 64 * 1024 * 1024 <= self.memory_bytes <= 1024**3:
            raise IsolationError('CPU/memory limit outside supported range')
        if not 16 <= self.processes <= 256 or not 32 <= self.open_files <= 256:
            raise IsolationError('process/FD limit outside supported range')
        for name in ['file_bytes', 'output_bytes', 'log_bytes', 'source_bytes']:
            if not 1024 <= getattr(self, name) <= 32 * 1024 * 1024:
                raise IsolationError(f'{name} outside supported range')
        if not 1 <= self.artifact_files <= 1024 or not 1 <= self.source_files <= 1024:
            raise IsolationError('file-count limit outside supported range')


def _snapshot(source: Path, destination: Path, limits: Limits) -> dict:
    source = source.absolute()
    # Explicitly reject symlinks in source and each parent. Do not dereference
    # an untrusted source entry while freezing it.
    for parent in [source, *source.parents]:
        if parent.is_symlink():
            raise IsolationError('source path contains a symlink')
    if not source.is_dir():
        raise IsolationError('source must be a directory')
    destination.mkdir(mode=0o700)
    count = total = 0
    manifest = {}
    for base, dirs, names in os.walk(source, followlinks=False):
        for name in dirs + names:
            path = Path(base) / name
            info = path.lstat()
            relative = path.relative_to(source)
            target = destination / relative
            if stat.S_ISLNK(info.st_mode):
                raise IsolationError('source symlink rejected')
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(mode=0o700)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise IsolationError('source special file rejected')
            count += 1
            total += info.st_size
            if count > limits.source_files or total > limits.source_bytes:
                raise IsolationError('source limit exceeded')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    raise IsolationError('source changed during snapshot')
                body = stream.read(limits.source_bytes + 1)
            if len(body) != info.st_size:
                raise IsolationError('source size changed during snapshot')
            target.write_bytes(body)
            target.chmod(0o400)
            manifest[str(relative)] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    return manifest


def extract_archive(data: bytes, destination: Path, limits: Limits) -> dict:
    """Reject links/duplicates/traversal; never call tarfile.extract[all]."""
    destination.mkdir(mode=0o700)
    seen = set()
    total = 0
    result = None
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in {'_control', 'artifacts'}:
                raise IsolationError('archive path rejected')
            if member.name in seen or not member.isfile():
                raise IsolationError('archive duplicate/non-regular member rejected')
            seen.add(member.name)
            if len(seen) > limits.artifact_files + 3:
                raise IsolationError('archive member limit exceeded')
            total += member.size
            if member.size < 0 or member.size > max(limits.file_bytes, limits.log_bytes, 65536) or total > limits.output_bytes + limits.log_bytes * 2 + 65536:
                raise IsolationError('archive size rejected')
            body = archive.extractfile(member).read(member.size + 1)
            if len(body) != member.size:
                raise IsolationError('archive truncated')
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open('xb') as stream:
                stream.write(body)
            if member.name == '_control/result.json':
                result = json.loads(body)
    if result is None or result.get('protocol') != 1:
        raise IsolationError('missing supervisor result')
    return result


def launch(candidate_dir: Path, input_dir: Path, run_root: Path, *, entrypoint='main.py', args=(), limits=None) -> dict:
    limits = limits or Limits()
    limits.validate()
    entry = PurePosixPath(entrypoint)
    if entry.is_absolute() or '..' in entry.parts or not entry.parts or entry.suffix != '.py':
        raise IsolationError('entrypoint must be a relative Python file')
    if len(args) > 32 or any(not isinstance(a, str) or len(a) > 4096 for a in args):
        raise IsolationError('invalid candidate arguments')
    if not BWRAP.is_file() or BWRAP.stat().st_uid != 0 or BWRAP.stat().st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise IsolationError('expected root-owned, non-setuid /usr/bin/bwrap')
    run_root = Path(run_root).absolute()
    for parent in [run_root, *run_root.parents]:
        if parent.is_symlink():
            raise IsolationError('run root symlink rejected')
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = run_root / str(uuid.uuid4())
    run_dir.mkdir(mode=0o700)
    frozen = run_dir / 'frozen'
    frozen.mkdir(mode=0o700)
    manifest = {'candidate': _snapshot(Path(candidate_dir), frozen / 'candidate', limits),
        'input': _snapshot(Path(input_dir), frozen / 'input', limits)}
    if entrypoint not in manifest['candidate']:
        raise IsolationError('entrypoint absent from frozen candidate')
    runner = frozen / 'runner'
    runner.mkdir(mode=0o700)
    runner.joinpath('inside.py').write_bytes(ROOT.joinpath('inside.py').read_bytes())
    settings = {'entrypoint': entrypoint, 'args': list(args), 'candidate_wall_seconds': limits.wall_seconds,
        'log_bytes': limits.log_bytes, 'artifact_files': limits.artifact_files, 'output_bytes': limits.output_bytes,
        'processes': limits.processes}
    runner.joinpath('settings.json').write_text(json.dumps(settings))
    command = [str(BWRAP), '--unshare-all', '--unshare-user', '--disable-userns', '--assert-userns-disabled',
        '--die-with-parent', '--new-session', '--as-pid-1', '--cap-drop', 'ALL',
        '--ro-bind', '/usr/bin', '/usr/bin', '--ro-bind', '/usr/lib', '/usr/lib',
        '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib']
    if Path('/usr/lib64').is_dir():
        command += ['--ro-bind', '/usr/lib64', '/usr/lib64', '--symlink', 'usr/lib64', '/lib64']
    command += ['--proc', '/proc', '--remount-ro', '/proc', '--dev', '/dev',
        '--remount-ro', '/dev/pts', '--remount-ro', '/dev',
        '--ro-bind', str(frozen / 'candidate'), '/candidate', '--ro-bind', str(frozen / 'input'), '/input',
        '--ro-bind', str(runner), '/runner', '--size', str(limits.output_bytes), '--tmpfs', '/output',
        '--symlink', '/output', '/tmp', '--remount-ro', '/', '--chdir', '/candidate',
        '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/nonexistent',
        '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'TZ', 'UTC',
        '--', '/usr/bin/python3', '-I', '-B', '/runner/inside.py']
    def restrict():
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), 'no_new_privs failed')
        # NPROC is installed by the trusted PID1 after user-namespace creation.
        # Applying it here charges unrelated host-UID threads before bwrap can
        # unshare, and can reject startup even when the sandbox has no tasks.
        for kind, value in [(resource.RLIMIT_AS, limits.memory_bytes),
            (resource.RLIMIT_NOFILE, limits.open_files), (resource.RLIMIT_FSIZE, limits.file_bytes), (resource.RLIMIT_CORE, 0)]:
            resource.setrlimit(kind, (value, value))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1))
    begin = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, close_fds=True,
        start_new_session=True, preexec_fn=restrict)
    selector = selectors.DefaultSelector()
    captured = {'stdout': bytearray(), 'stderr': bytearray()}
    for name, stream in [('stdout', proc.stdout), ('stderr', proc.stderr)]:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    transport_reason = None
    archive_limit = limits.output_bytes + limits.artifact_files * 2048 + limits.log_bytes * 2 + 131072
    try:
        while selector.get_map():
            if time.monotonic() - begin > limits.wall_seconds + 2:
                transport_reason = 'outer_timeout'
                break
            for key, _ in selector.select(0.05):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                limit = archive_limit if key.data == 'stdout' else limits.log_bytes
                available = max(0, limit - len(captured[key.data]))
                captured[key.data].extend(block[:available])
                if len(block) > available:
                    transport_reason = 'archive_limit' if key.data == 'stdout' else 'transport_log_limit'
                    break
            if transport_reason:
                break
    finally:
        if not transport_reason:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                transport_reason = 'post_output_process_timeout'
        if transport_reason or proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=2)
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
    if proc.returncode != 0 and transport_reason is None:
        transport_reason = 'sandbox_start_or_supervisor_failure'
    result = {'protocol': 'htam.isolation-probe.v1', 'run_id': run_dir.name, 'run_dir': str(run_dir),
        'started_at': started_at, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'host_launcher_pid': os.getpid(), 'bwrap_pid': proc.pid, 'bwrap_returncode': proc.returncode,
        'elapsed_seconds': time.monotonic() - begin, 'transport_reason': transport_reason,
        'limits': asdict(limits), 'command': command, 'source_manifest': manifest,
        'transport_stderr': captured['stderr'].decode('utf-8', errors='replace'),
        'archive_bytes': len(captured['stdout']),
        'process_limit_boundary': 'Trusted PID1 after user namespace creation, before candidate spawn; ancestor host limits remain inherited'}
    if not transport_reason and proc.returncode == 0:
        try:
            result['candidate'] = extract_archive(bytes(captured['stdout']), run_dir / 'result', limits)
        except (IsolationError, tarfile.TarError, OSError, ValueError) as error:
            result['transport_reason'] = 'invalid_archive'
            result['archive_error'] = str(error)
    control = result.get('candidate', {})
    result['execution_succeeded'] = (result['transport_reason'] is None and proc.returncode == 0 and
        control.get('reason') == 'candidate_exit' and control.get('candidate_returncode') == 0 and
        control.get('remaining_namespace_pids') == [])
    result['runner_sha256'] = hashlib.sha256(ROOT.joinpath('inside.py').read_bytes()).hexdigest()
    result['artifacts'] = []
    if result.get('candidate') and (run_dir / 'result/artifacts').is_dir():
        for path in sorted((run_dir / 'result/artifacts').rglob('*')):
            if path.is_file():
                body = path.read_bytes()
                result['artifacts'].append({'path': str(path.relative_to(run_dir / 'result/artifacts')),
                    'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
    run_dir.joinpath('run.json').write_text(json.dumps(result, indent=2))
    return result
