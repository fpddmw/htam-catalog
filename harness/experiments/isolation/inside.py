"""Trusted sandbox supervisor. No host mount or credential material is available."""
from __future__ import annotations

import ctypes
import io
import json
import os
from pathlib import Path
import selectors
import resource
import signal
import stat
import subprocess
import sys
import tarfile
import time


def main() -> int:
    settings = json.loads(Path('/runner/settings.json').read_text())
    libc = ctypes.CDLL(None, use_errno=True)
    # The candidate must not obtain the supervisor's stdout archive FD via procfs.
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), 'cannot protect supervisor procfs')
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot enable child reaping')
    # All candidate code runs after this boundary. Preserve a stricter inherited
    # limit while avoiding the unrelated host UID's pre-unshare task count.
    inherited_nproc = resource.getrlimit(resource.RLIMIT_NPROC)
    requested_nproc = settings['processes']
    effective_nproc = min([requested_nproc] + [v for v in inherited_nproc if v != resource.RLIM_INFINITY])
    resource.setrlimit(resource.RLIMIT_NPROC, (effective_nproc, effective_nproc))
    process_limit = {'requested': requested_nproc, 'inherited': list(inherited_nproc),
        'effective': list(resource.getrlimit(resource.RLIMIT_NPROC)), 'applied_before_candidate': True,
        'scope': 'Real UID in the fresh user namespace; inherited ancestor limits still apply'}
    started = time.monotonic()
    bootstrap = 'import runpy,sys;sys.path.insert(0,"/candidate");runpy.run_path(sys.argv.pop(1),run_name="__main__")'
    proc = subprocess.Popen(['/usr/bin/python3', '-I', '-B', '-c', bootstrap,
        '/candidate/' + settings['entrypoint'], *settings['args']], cwd='/candidate',
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    for name, stream in [('stdout', proc.stdout), ('stderr', proc.stderr)]:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    reason = 'candidate_exit'
    while selector.get_map():
        if time.monotonic() - started > settings['candidate_wall_seconds']:
            reason = 'candidate_timeout'
            break
        for key, _ in selector.select(0.05):
            block = os.read(key.fileobj.fileno(), 65536)
            if not block:
                selector.unregister(key.fileobj)
                continue
            target = buffers[key.data]
            available = max(0, settings['log_bytes'] - len(target))
            target.extend(block[:available])
            if len(block) > available:
                reason = 'log_limit'
                break
        if reason == 'log_limit':
            break
        # A background descendant can keep pipes open after its parent exits.
        if proc.poll() is not None:
            break
    # This procfs is the private PID namespace. Kill all candidate descendants,
    # including setsid children; bwrap PID 1 and this trusted supervisor survive.
    killed = set()
    returncode = proc.poll()
    for _ in range(6):
        victims = [int(p.name) for p in Path('/proc').iterdir()
            if p.name.isdigit() and int(p.name) not in {1, os.getpid()}]
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
        if returncode is None:
            try:
                returncode = proc.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0] != 0:
                pass
        except ChildProcessError:
            pass
        if not victims:
            break
        time.sleep(0.02)
    if returncode is None:
        try:
            returncode = proc.wait(timeout=0.2)
        except (subprocess.TimeoutExpired, ChildProcessError):
            returncode = None
    for key in list(selector.get_map().values()):
        try:
            while True:
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    break
                target = buffers[key.data]
                available = max(0, settings['log_bytes'] - len(target))
                target.extend(block[:available])
                if len(block) > available:
                    reason = 'log_limit'
        except BlockingIOError:
            pass
    selector.close()
    remaining = [int(p.name) for p in Path('/proc').iterdir()
        if p.name.isdigit() and int(p.name) not in {1, os.getpid()}]
    # Only regular files enter the archive. Symlinks/devices/path escapes fail.
    files = []
    total = 0
    artifact_error = None
    for base, dirs, names in os.walk('/output', followlinks=False):
        for name in dirs + names:
            path = Path(base) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                artifact_error = 'non_regular_output'
                break
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                files.append(path)
                if len(files) > settings['artifact_files'] or total > settings['output_bytes']:
                    artifact_error = 'artifact_limit'
                    break
        if artifact_error:
            break
    if artifact_error:
        reason = artifact_error
        files = []
    result = {'protocol': 1, 'reason': reason, 'candidate_returncode': returncode,
        'elapsed_seconds': time.monotonic() - started, 'descendant_pids_killed': sorted(killed),
        'remaining_namespace_pids': remaining, 'artifact_files': len(files), 'artifact_bytes': total,
        'logs': {name: len(value) for name, value in buffers.items()}, 'process_limit': process_limit}
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for name, data in [('_control/result.json', json.dumps(result).encode()),
            ('_control/stdout.bin', buffers['stdout']), ('_control/stderr.bin', buffers['stderr'])]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for path in files:
            archive.add(path, arcname='artifacts/' + str(path.relative_to('/output')), recursive=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
