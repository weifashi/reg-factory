"""Read-only bridge from the trusted original mailbox file to a live payload.

Composition supplies the path, owner and rollout identity; HTTP input must never
select them. This helper is NOT an authorization service. The caller must check
the authenticated actor against current database authority before ``snapshot``
and again before persisting its result, then call ``validate`` before commit.
No mailbox is consumed, claimed, rewritten or sent to a provider here.

Filesystem guarantees cover path substitution, unsafe permissions and changes
during/after the bounded read. A malicious process with our own UID remains
outside this boundary, as does any change after the final revalidation.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from .errors import ErrorCode, ServiceError
from .google_auth_runtime import _ACCOUNT
from .legacy_mailboxes import MAX_BYTES, parse_for_import
from .live_input_vault import LiveMailboxCredential, _encode_mailbox
from .request_mac import RequestMac
from .security import Actor

_ACTION = 'live.mailbox.source'
_PATH_TYPE = type(Path())


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError
    return value


def _identity(info):
    # atime is deliberately excluded: merely reading a file may update it.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _root(info):
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError


def _file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_BYTES):
        raise ValueError


def _open_root(path):
    """Do not resolve symlinks: walk every component with directory handles."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        _root(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_source(path):
    """Read the sole fixed basename and compare handles plus named identities."""
    directory = _open_root(path)
    try:
        before_root = _identity(os.fstat(directory))
        fd = os.open('emails.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            before = os.fstat(fd)
            _file(before)
            chunks, count = [], 0
            while True:
                chunk = os.read(fd, min(16384, MAX_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > MAX_BYTES:
                    raise ValueError
            after = os.fstat(fd)
            named = os.stat('emails.txt', dir_fd=directory, follow_symlinks=False)
            _file(after)
            _file(named)
            if (_identity(before) != _identity(after)
                    or _identity(after) != _identity(named) or count != before.st_size
                    or before_root != _identity(os.fstat(directory))):
                raise ValueError
            # Detect renaming/replacing the trusted root during the read too.
            current = _open_root(path)
            try:
                if before_root != _identity(os.fstat(current)):
                    raise ValueError
            finally:
                os.close(current)
            return b''.join(chunks), before_root, _identity(after)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _actor_binding(actor, owner_id):
    if (type(actor) is not Actor or actor.operator_id != owner_id
            or type(actor.permissions) is not frozenset or len(actor.permissions) > 128
            or any(type(item) is not str or not item or len(item) > 128
                   for item in actor.permissions)
            or type(actor.auth_epoch) is not int or actor.auth_epoch < 1):
        raise ValueError
    _uuid(actor.session_id)
    return (actor.operator_id, actor.session_id, actor.auth_epoch,
            tuple(sorted(actor.permissions)))


@dataclass(frozen=True, repr=False)
class MailboxSnapshot:
    """Short-lived secret-bearing value; never serialize to logs or a response."""
    payload: LiveMailboxCredential
    source_digest: str
    _source: object = field(repr=False)
    _actor: tuple = field(repr=False)
    _root_identity: tuple = field(repr=False)
    _file_identity: tuple = field(repr=False)

    def __repr__(self):
        return '<MailboxSnapshot>'


@dataclass(frozen=True, repr=False)
class OwnedMailboxSource:
    data_root: Path
    owner_id: str
    email: str
    _binding: object = field(default_factory=object, init=False, repr=False)

    def __post_init__(self):
        try:
            if (type(self.data_root) is not _PATH_TYPE or not self.data_root.is_absolute()
                    or self.data_root == Path('/') or '..' in self.data_root.parts
                    or '\x00' in str(self.data_root)
                    or type(self.email) is not str or self.email != _ACCOUNT):
                raise ValueError
            _uuid(self.owner_id)
        except (TypeError, ValueError, AttributeError):
            raise ServiceError(ErrorCode.INVALID_INPUT) from None

    def __repr__(self):
        return '<OwnedMailboxSource>'

    def _read(self, actor, mac):
        # Reject stale/malformed identities before touching the original pool.
        try:
            if type(self) is not OwnedMailboxSource or type(mac) is not RequestMac:
                raise ValueError
            self.__post_init__()
            actor_binding = _actor_binding(actor, self.owner_id)
        except (TypeError, ValueError, AttributeError):
            raise ServiceError(ErrorCode.FORBIDDEN) from None
        try:
            raw, root_identity, file_identity = _read_source(self.data_root)
            # Recognize legacy three-field paid-account ambiguity too. Explicit
            # JSON two_factor remains supported without inventing a password.
            parsed = parse_for_import(raw.decode('utf-8'), plus_credentials=True)
            selected = [item for item in parsed.records if item.email_norm == self.email]
            if parsed.issues or len(selected) != 1:
                raise ValueError
            record = selected[0]
            if not record.password or record.account_password:
                raise ValueError
            payload = LiveMailboxCredential(email_norm=record.email_norm,
                password=record.password, refresh_token=record.refresh_token,
                client_id=record.client_id, provider='other',
                mail_api_url=record.mail_api_url, mail_api_key=record.mail_api_key,
                two_factor=record.two_factor)
            _encode_mailbox(payload)
            # The strong whole-file hash stays inside the keyed request MAC.
            # Returning a plain credential/file hash would enable guessing.
            # RequestMac frames the owner separately. Keep this digest stable
            # across that owner's sessions for owner-scoped import replay;
            # the private snapshot still binds the exact actor below.
            canonical = json.dumps(dict(domain='rf-live-mailbox-source:v1',
                path=str(self.data_root / 'emails.txt'), email=self.email,
                root=root_identity, file=file_identity,
                content_sha256=hashlib.sha256(raw).hexdigest()),
                sort_keys=True, separators=(',', ':')).encode('utf-8')
            digest = mac.request_digest(_ACTION, self.owner_id, canonical)
            return MailboxSnapshot(payload, digest, self._binding, actor_binding,
                                   root_identity, file_identity)
        except (OSError, TypeError, ValueError, UnicodeError, AttributeError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def snapshot(self, actor: Actor, mac: RequestMac) -> MailboxSnapshot:
        return self._read(actor, mac)

    def validate(self, snapshot: MailboxSnapshot, actor: Actor, mac: RequestMac) -> bool:
        try:
            if (type(snapshot) is not MailboxSnapshot or snapshot._source is not self._binding
                    or snapshot._actor != _actor_binding(actor, self.owner_id)
                    or type(snapshot.source_digest) is not str):
                raise ValueError
            current = self._read(actor, mac)
            if (snapshot._root_identity != current._root_identity
                    or snapshot._file_identity != current._file_identity
                    or not hmac.compare_digest(snapshot.source_digest, current.source_digest)
                    or not hmac.compare_digest(_encode_mailbox(snapshot.payload),
                                               _encode_mailbox(current.payload))):
                raise ValueError
            return True
        except (TypeError, ValueError, AttributeError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None
