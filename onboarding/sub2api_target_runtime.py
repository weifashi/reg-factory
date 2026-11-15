"""Read-only Linux identity guard for the one approved temporary Sub2API.

No configurable target, subprocess, service discovery, HTTP request or secret
loading. Construct once for a task, then pass this object as ``importer.guard``
and ``guard.cleanup`` as its separate logout-only guard. Every check reads and
hashes the running /proc/PID/exe; the manifest's hash is never its own proof.

This is a pre-request identity check, not an atomic HTTP connection attestation.
The caller must retain its fixed loopback URL, no redirects/proxies, and one-shot
effect permit. A hostile same-UID/root process is outside this local trust model.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from .sub2api_import import ACTION, APPROVED_BUILD_SHA256

LOGOUT_ACTION = 'sub2api.logout'
_ROOT = Path('/workspace/reg-factory/.local/sub2api-temp-20260929')
_PROC = Path('/proc')
_FILE_LIMIT = 64 * 1024
_NET_LIMIT = 4 * 1024 * 1024
_EXE_LIMIT = 512 * 1024 * 1024
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _require(condition):
    if condition is not True:
        raise ValueError('TARGET_RUNTIME_UNAVAILABLE')


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_uid, st.st_mode, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _directory_identity(st):
    # New logs/temporary files do not change the identity of a directory.
    return (st.st_dev, st.st_ino, st.st_uid, st.st_mode)


@contextmanager
def _open(path, *, parent=None, directory=False, follow_proc_exe=False):
    flags = _READ_FLAGS | (os.O_DIRECTORY if directory else 0)
    if follow_proc_exe:
        flags &= ~os.O_NOFOLLOW  # Only /proc/<validated PID>/exe uses this.
    fd = os.open(path, flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _read(fd, limit):
    parts, size = [], 0
    while True:
        block = os.read(fd, min(65536, limit + 1 - size))
        if not block:
            return b''.join(parts)
        parts.append(block)
        size += len(block)
        _require(size <= limit)


def _read_at(parent, name, limit=_FILE_LIMIT):
    with _open(name, parent=parent) as fd:
        _require(stat.S_ISREG(os.fstat(fd).st_mode))
        return _read(fd, limit)


def _private(st, *, directory=False):
    _require(st.st_uid == os.geteuid())
    _require((stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)))
    _require(stat.S_IMODE(st.st_mode) == (0o700 if directory else 0o600))
    if not directory:
        _require(st.st_nlink == 1)


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _manifest(root_fd):
    with _open('runtime.json', parent=root_fd) as fd:
        before = os.fstat(fd)
        _private(before)
        raw = _read(fd, _FILE_LIMIT)
        _require(_identity(before) == _identity(os.fstat(fd)))
    data = json.loads(raw, object_pairs_hook=_unique_json)
    _require(type(data) is dict)
    _require(data.get('root') == str(_ROOT) and data.get('temporary') is True)
    _require(data.get('patch_sha256') == APPROVED_BUILD_SHA256)
    _require(data.get('disabled_import_verified') is True)
    pid = data.get('app_pid')
    _require(type(pid) is int and pid > 0)
    processes = data.get('processes')
    _require(type(processes) is dict)
    app = processes.get('app')
    _require(type(app) is dict and type(app.get('pid')) is int and app['pid'] == pid)
    start = app.get('start_ticks')
    _require(type(start) is str and start.isascii() and start.isdecimal()
             and str(int(start)) == start and int(start) > 0)
    _require(app.get('exe') == str(_ROOT / 'bin/sub2api-create-disabled'))
    _require(app.get('cwd') == str(_ROOT))
    return pid, start, (_identity(before), hashlib.sha256(raw).hexdigest())


def _process_identity(proc_fd, pid, start):
    owner = os.fstat(proc_fd).st_uid
    _require(owner == os.geteuid())
    raw = _read_at(proc_fd, 'stat').decode('ascii')
    prefix, fields = raw.rsplit(')', 1)
    _require(prefix.split(' ', 1)[0] == str(pid))
    fields = fields.split()
    _require(len(fields) >= 20 and fields[0] not in ('Z', 'X', 'x') and fields[19] == start)
    status = _read_at(proc_fd, 'status').decode('ascii')
    uids = [line.split()[1:] for line in status.splitlines() if line.startswith('Uid:')]
    _require(uids == [[str(owner)] * 4])
    exe = os.readlink('exe', dir_fd=proc_fd)
    cwd = os.readlink('cwd', dir_fd=proc_fd)
    _require(exe == str(_ROOT / 'bin/sub2api-create-disabled') and cwd == str(_ROOT))
    netns = os.readlink('ns/net', dir_fd=proc_fd)
    _require(netns == os.readlink(_PROC / 'self/ns/net'))
    return pid, start, owner, exe, cwd, netns


def _executable(proc_fd, bin_fd):
    with _open('exe', parent=proc_fd, follow_proc_exe=True) as fd:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid())
        _require(stat.S_IMODE(before.st_mode) == 0o700 and before.st_nlink == 1)
        _require(0 < before.st_size <= _EXE_LIMIT)
        installed = os.stat('sub2api-create-disabled', dir_fd=bin_fd, follow_symlinks=False)
        _require(_identity(before) == _identity(installed))
        digest, size = hashlib.sha256(), 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            size += len(block)
            _require(size <= _EXE_LIMIT)
            digest.update(block)
        _require(size == before.st_size and _identity(before) == _identity(os.fstat(fd)))
        _require(digest.hexdigest() == APPROVED_BUILD_SHA256)
        return _identity(before), digest.hexdigest()


def _listener(proc_fd):
    listeners = []
    for family in ('tcp', 'tcp6'):
        raw = _read_at(proc_fd, 'net/' + family, _NET_LIMIT).decode('ascii')
        for row in raw.splitlines()[1:]:
            fields = row.split()
            _require(len(fields) >= 10)
            local = fields[1].split(':')
            _require(len(local) == 2)
            if int(local[1], 16) == 8800 and fields[3] == '0A':
                listeners.append((family, local[0], fields[2], fields[7], fields[9]))
    _require(len(listeners) == 1)
    family, address, remote, uid, inode = listeners[0]
    _require(family == 'tcp' and address == '0100007F' and remote == '00000000:0000')
    _require(uid == str(os.geteuid()) and inode.isdecimal() and int(inode) > 0)
    with _open('fd', parent=proc_fd, directory=True) as fd_dir:
        owned = False
        for name in os.listdir(fd_dir):
            try:
                if os.readlink(name, dir_fd=fd_dir) == 'socket:[' + inode + ']':
                    owned = True
                    break
            except FileNotFoundError:
                # Unrelated short-lived outbound connections may close.
                continue
        _require(owned)
    return inode


@dataclass(frozen=True, repr=False)
class _Snapshot:
    root: tuple
    bin: tuple
    manifest: tuple
    process: tuple
    executable: tuple
    socket_inode: str


def _snapshot():
    # Do not accept a symlink at any point in the configured local root.
    _require(_ROOT.resolve(strict=True) == _ROOT)
    with _open(_ROOT, directory=True) as root_fd:
        root_stat = os.fstat(root_fd)
        _private(root_stat, directory=True)
        pid, start, manifest = _manifest(root_fd)
        with _open('bin', parent=root_fd, directory=True) as bin_fd:
            bin_stat = os.fstat(bin_fd)
            _private(bin_stat, directory=True)
            with _open(_PROC / str(pid), directory=True) as proc_fd:
                identity = _process_identity(proc_fd, pid, start)
                binary = _executable(proc_fd, bin_fd)
                inode = _listener(proc_fd)
                _require(identity == _process_identity(proc_fd, pid, start))
                _require(inode == _listener(proc_fd))
            _require(_directory_identity(bin_stat) == _directory_identity(os.stat('bin', dir_fd=root_fd,
                                                                                 follow_symlinks=False)))
        _require((pid, start, manifest) == _manifest(root_fd))
        _require(_directory_identity(root_stat) == _directory_identity(_ROOT.lstat()))
        return _Snapshot(_directory_identity(root_stat), _directory_identity(bin_stat),
                         manifest, identity, binary, inode)


class Sub2APITargetGuard:
    """A task-scoped pin, never automatically rebound after replacement/restart.

    Construction fails closed with a generic error. The callback returns only
    exact booleans and never logs filesystem contents or exception messages.
    Hashing on each request runs off the event loop. Creating a fresh guard is an
    explicit new pin, not a substitute for new task/effect authorization.
    """

    def __init__(self, *, task_guard):
        if not callable(task_guard):
            raise ValueError('TASK_GUARD_REQUIRED')
        self._task_guard = task_guard
        try:
            self._pinned = _snapshot()
        except Exception:
            raise ValueError('TARGET_RUNTIME_UNAVAILABLE') from None

    async def _target_matches(self):
        try:
            return await asyncio.to_thread(_snapshot) == self._pinned
        except Exception:
            return False

    async def __call__(self, action):
        if type(action) is not str or action != ACTION:
            return False
        try:
            if await self._task_guard(action) is not True:
                return False
            if await self._target_matches() is not True:
                return False
            # A cancellation arriving during a potentially large executable
            # hash must not silently turn into an allowed live effect.
            return await self._task_guard(action) is True
        except Exception:
            return False

    async def cleanup(self, action):
        """Logout only: task cancellation is allowed, changed target is not."""
        if type(action) is not str or action != LOGOUT_ACTION:
            return False
        return await self._target_matches()
