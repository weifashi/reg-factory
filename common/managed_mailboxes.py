"""One-way ownership barrier between managed mailboxes and legacy file consumers.

This is NOT a mailbox/credential database or a lease service. It stores only
hashed identities and permanent management/legacy-delivery tombstones. Old
cross-platform use remains unchanged; a mailbox once delivered to legacy code
cannot be silently adopted, even after cursors are reset or a process dies.

All participating processes must run this guard and use the same physical data
root. A pre-upgrade process, another host, or an actor able to delete/replace the
entire control directory is outside this advisory-lock boundary. Never remove
these files to recover a failed import: reconcile ownership explicitly instead.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from uuid import UUID

from common.file_lock import file_lock

_SCHEMA = 1
_MARKER = b'\0rf-mailbox-ownership-v1\n'
_MAX_BYTES = 8 * 1024 * 1024
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_THREAD_LOCK = threading.RLock()
_LOCAL = threading.local()


class MailboxBoundaryError(RuntimeError):
    """Fixed, secret-free rejection; uncertainty never grants consumption."""
    def __init__(self):
        super().__init__('mailbox ownership boundary unavailable')


def _fail_closed(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError, UnicodeError):
            raise MailboxBoundaryError() from None
    return guarded


@_fail_closed
def mailbox_source_root(source_file):
    """Canonical source root; a hard link must never mint a second boundary.

    Missing pools retain the legacy not-found behavior. A symlink may resolve
    to the one real source root; callers requiring a fixed basename/root (asset
    inventory) additionally reject source symlinks rather than changing roots.
    """
    source = Path(os.path.realpath(source_file))
    try:
        info = source.stat()
    except FileNotFoundError:
        return source.parent
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MailboxBoundaryError()
    return source.parent


def _email(value):
    if not isinstance(value, str):
        raise MailboxBoundaryError()
    value = value.strip().lower()
    if (not value or len(value) > 320 or value.count('@') != 1
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(not part for part in value.split('@'))):
        raise MailboxBoundaryError()
    return value


def _key(email):
    return hashlib.sha256(('rf-mailbox-ownership:v1\0' + _email(email)).encode()).hexdigest()


def _owner(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise MailboxBoundaryError() from None
    return value


def _directory(path, *, private_root=False):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (not private_root and info.st_mode & 0o022)
            or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
        raise MailboxBoundaryError()


def _read(path, *, control=True):
    """Do not silently treat symlinks, damaged files or oversized files as empty."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_mode & (0o022 if control else 0o002) or before.st_size > _MAX_BYTES
            or (hasattr(os, 'getuid') and before.st_uid != os.getuid())):
        raise MailboxBoundaryError()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MailboxBoundaryError()
        value = handle.read(_MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
    named = path.lstat()
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                            st.st_ctime_ns, st.st_mode, st.st_nlink)
    if (len(value) > _MAX_BYTES or identity(before) != identity(after)
            or identity(after) != identity(named)):
        raise MailboxBoundaryError()
    return value


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MailboxBoundaryError()
        result[key] = value
    return result


def _json(raw):
    if type(raw) is not bytes:
        raise MailboxBoundaryError()
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_object_pairs)


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, value):
    """Durable replace; a failed write never permits a credential to escape."""
    fd, name = tempfile.mkstemp(prefix='.managed-mailbox-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _Boundary:
    def __init__(self, root, path):
        self.root, self.path = root, path
        self._active = True
        self._holder = (os.getpid(), threading.get_ident())
        self._load()

    def _load(self):
        if not self._active or self._holder != (os.getpid(), threading.get_ident()):
            raise MailboxBoundaryError()
        raw = _read(self.path)
        if raw is None:
            raise MailboxBoundaryError()
        document = _json(raw)
        if (type(document) is not dict or set(document) != {'version', 'entries'}
                or type(document['version']) is not int or document['version'] != _SCHEMA
                or type(document['entries']) is not dict):
            raise MailboxBoundaryError()
        for key, entry in document['entries'].items():
            if not _DIGEST.fullmatch(key) or type(entry) is not dict:
                raise MailboxBoundaryError()
            if entry == {'mode': 'legacy-exposed'}:
                continue
            if set(entry) != {'mode', 'owner'} or entry['mode'] != 'managed':
                raise MailboxBoundaryError()
            _owner(entry['owner'])
        self._document = document

    @_fail_closed
    def is_managed(self, email):
        self._load()
        return self._document['entries'].get(_key(email), {}).get('mode') == 'managed'

    @_fail_closed
    def filter_unmanaged(self, records, email_for):
        """Filter one batch under this lock with one validated registry read.

        This snapshot grants no delivery authority. The eventual consumer must
        still call record_legacy_use, which independently reloads and persists.
        Source-only assets retain their existing unidentified-record behavior.
        """
        self._load()
        entries = self._document['entries']
        selected = []
        for record in records:
            email = email_for(record)
            if not email or entries.get(_key(email), {}).get('mode') != 'managed':
                selected.append(record)
        return selected

    @_fail_closed
    def require_owner(self, email, owner_id):
        """Read-only exact ownership assertion; never adopts an unowned mailbox."""
        self._load()
        expected = {'mode': 'managed', 'owner': _owner(owner_id)}
        if self._document['entries'].get(_key(email)) != expected:
            raise MailboxBoundaryError()

    @_fail_closed
    def record_legacy_use(self, email):
        self._load()
        key = _key(email)
        entry = self._document['entries'].get(key)
        if entry is not None:
            if entry != {'mode': 'legacy-exposed'}:
                raise MailboxBoundaryError()
            return
        self._document['entries'][key] = {'mode': 'legacy-exposed'}
        _write(self.path, self._document)

    @_fail_closed
    def register(self, email, owner_id):
        """Reserve management before the DB import; failures keep the reservation."""
        self._load()
        email, owner_id = _email(email), _owner(owner_id)
        key = _key(email)
        expected = {'mode': 'managed', 'owner': owner_id}
        existing = self._document['entries'].get(key)
        if existing is not None and existing != expected:
            raise MailboxBoundaryError()
        self._check_history(email)
        if existing is None:
            self._document['entries'][key] = expected
            _write(self.path, self._document)

    def _check_history(self, email):
        """Old history is evidence of use/uncertainty, never proof of eligibility."""
        paths = set(self.root.glob('emails_used_*.txt')) | set(self.root.glob('emails_error_*.txt'))
        paths.update(self.root / name for name in ('emails_used.txt', 'emails_error.txt'))
        paths.update(self.path.parent / name for name in
                     ('outlook_sale_emails.txt', 'outlook_registration_emails.txt'))
        known = {email}
        known.update(self._check_platform_history(email))
        for path in sorted(paths):
            raw = _read(path, control=False)
            if raw is None:
                continue
            for line in raw.decode('utf-8-sig').splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                address = _email(line.split('----', 1)[0])
                if address == email:
                    raise MailboxBoundaryError()
                known.add(address)
        # The old cursor has no stable identity proof; an advanced pre-existing
        # cursor means some credential may already have escaped without a claim.
        raw = _read(self.path.parent / 'asset_api_cursors.json', control=False)
        if raw is not None:
            cursors = _json(raw)
            if (type(cursors) is not dict or any(type(k) is not str or type(v) is not int or v < 0
                                               for k, v in cursors.items())
                    or any(cursors.values())):
                raise MailboxBoundaryError()
        raw = _read(self.path.parent / 'asset_api_claims.json', control=False)
        if raw is not None:
            claims = _json(raw)
            if (type(claims) is not dict or set(claims) != {'version', 'scopes'}
                    or type(claims['version']) is not int or claims['version'] != 1
                    or type(claims['scopes']) is not dict):
                raise MailboxBoundaryError()
            # Claims are opaque hashes; unless all are tied to known *other*
            # identities, their absence from this email's hash proves nothing.
            pool = _read(self.root / 'emails.txt', control=False)
            if pool is not None:
                for line in pool.decode('utf-8-sig').splitlines():
                    if line.strip() and not line.lstrip().startswith('#'):
                        try:
                            known.add(_email(line.split('----', 1)[0]))
                        except MailboxBoundaryError:
                            pass  # Other import formats cannot establish a claim identity.
            for scope, values in claims['scopes'].items():
                if (type(scope) is not str or not scope or type(values) is not list
                        or any(type(v) is not str or not _DIGEST.fullmatch(v) for v in values)):
                    raise MailboxBoundaryError()
                safe = {hashlib.sha256(f'{scope}\0email:{other}'.encode()).hexdigest()
                        for other in known if other != email}
                if any(value not in safe for value in values):
                    raise MailboxBoundaryError()

    def _check_history_tree(self, directory):
        """rglob skips symlink subdirectories and some I/O errors; reject those."""
        def fail(error):
            raise error
        for parent, directories, _files in os.walk(directory, onerror=fail, followlinks=False):
            for name in directories:
                if (Path(parent) / name).is_symlink():
                    raise MailboxBoundaryError()

    def _check_platform_history(self, email):
        """Existing credentials are use evidence even without a used ledger."""
        known = set()
        configured = os.environ.get('TOKEN_OUTPUT_DIR', '').strip()
        if not configured:
            env_path = Path(os.environ.get('REG_FACTORY_ENV_FILE') or self.root / '.env')
            raw = _read(env_path, control=False)
            if raw is not None:
                for line in raw.decode('utf-8').splitlines():
                    if line.partition('=')[0].strip() == 'TOKEN_OUTPUT_DIR':
                        configured = line.partition('=')[2].strip().strip('"').strip("'")
                        break
        token_root = Path(configured or 'tokens')
        if not token_root.is_absolute():
            token_root = self.root / token_root
        if token_root.exists():
            if token_root.is_symlink():
                raise MailboxBoundaryError()
            self._check_history_tree(token_root)
            for path in sorted(token_root.rglob('*.json')):
                payload = _json(_read(path, control=False))
                if type(payload) is not dict:
                    raise MailboxBoundaryError()
                user = payload.get('user', {})
                account = payload.get('account', {})
                address = payload.get('email') or (user.get('email') if type(user) is dict else None)
                address = address or (account.get('email') if type(account) is dict else None)
                address = _email(address)
                if address == email:
                    raise MailboxBoundaryError()
                known.add(address)
        cookie_root = self.root / 'cookies'
        if cookie_root.exists():
            if cookie_root.is_symlink():
                raise MailboxBoundaryError()
            self._check_history_tree(cookie_root)
            for path in sorted(cookie_root.rglob('accounts.txt')):
                for line in _read(path, control=False).decode('utf-8-sig').splitlines():
                    if not line.strip() or line.lstrip().startswith('#'):
                        continue
                    address = _email(line.split('|', 1)[0])
                    if address == email:
                        raise MailboxBoundaryError()
                    known.add(address)
            # Cookie JSON has no trusted mailbox identity of its own. A missing
            # account mapping is unknown history, not evidence of non-use.
            for path in sorted(cookie_root.rglob('full_*.json')):
                payload = _json(_read(path, control=False))
                accounts = _read(path.parent / 'accounts.txt', control=False)
                if type(payload) is not list or accounts is None:
                    raise MailboxBoundaryError()
                mapping = {}
                for line in accounts.decode('utf-8-sig').splitlines():
                    parts = line.strip().split('|', 2)
                    if len(parts) == 3 and parts[2]:
                        mapping[parts[2]] = _email(parts[0])
                identified = {mapping[item['value']] for item in payload
                              if type(item) is dict and type(item.get('value')) is str
                              and item['value'] in mapping}
                if len(identified) != 1 or email in identified:
                    raise MailboxBoundaryError()
        return known


@contextmanager
def management_lock(data_root):
    """Hold one reentrant process/thread boundary through the *whole* operation.

    Initialization precedes source snapshots so creating the control directories
    cannot invalidate a later source-root identity check. The lock-file marker
    makes deleting just the registry fail closed instead of reinitializing it.
    """
    boundary = None
    yielded = False
    try:
        root = Path(data_root).resolve(strict=True)
        with _THREAD_LOCK:
            held = getattr(_LOCAL, 'held', {})
            key = (os.getpid(), str(root))
            if key in held:
                yielded = True
                yield held[key]
                return
            _directory(root)
            private_root = not root.stat().st_mode & 0o077
            state = root / 'runtime' / 'state'
            for directory in (root / 'runtime', state):
                directory.mkdir(mode=0o700, exist_ok=True)
                _directory(directory, private_root=private_root)
            target = root / '.managed-mailbox-boundary'
            lock_path = Path(str(target) + '.lock')
            if not lock_path.exists() and not lock_path.is_symlink():
                # Publish a nonempty inode atomically. The existing file_lock
                # writes its sentinel before acquiring the OS lock when empty;
                # exposing an empty inode would race at first simultaneous use.
                fd, temporary = tempfile.mkstemp(prefix='.mailbox-lock-', dir=root)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(b'\0')
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        os.link(temporary, lock_path)
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(temporary)
            _read(lock_path)
            with file_lock(target):
                marker = _read(lock_path)
                path = state / 'managed_mailbox_ownership.json'
                existing = _read(path)
                if marker == b'\0' and existing is None:
                    # Marker is synced FIRST. Any interrupted initialization
                    # therefore leaves a blocked boundary, not an empty pool.
                    with lock_path.open('r+b') as handle:
                        handle.write(_MARKER)
                        handle.truncate()
                        handle.flush()
                        os.fsync(handle.fileno())
                    _sync_directory(root)
                    _write(path, {'version': _SCHEMA, 'entries': {}})
                elif marker != _MARKER or existing is None:
                    raise MailboxBoundaryError()
                boundary = _Boundary(root, path)
                held[key] = boundary
                _LOCAL.held = held
                try:
                    yielded = True
                    yield boundary
                finally:
                    held.pop(key, None)
                    boundary._active = False
    except (OSError, ValueError, TypeError, UnicodeError):
        if yielded:
            raise
        raise MailboxBoundaryError() from None


def register_managed_mailbox(data_root, email, owner_id):
    """Management-only registration; no secrets, provider history or PG writes."""
    with management_lock(data_root) as boundary:
        boundary.register(email, owner_id)
