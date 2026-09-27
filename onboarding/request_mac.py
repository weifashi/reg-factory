"""Owner-private synthetic request MACs, independent from encryption keys.

canonical_bytes must come from an internal canonical DTO serializer: this module
frames bytes without claiming to validate the caller's business DTO or authority.
No automatic creation, key replacement, network access, or plaintext hash fallback.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from .errors import ErrorCode, ServiceError
from .keyring import _DIRECTORY, _VERSION
from .settings import BASE, _read_private_file

_ACTION = re.compile(r'[a-z][a-z0-9_.:-]{0,63}\Z')
_REQUEST_DOMAIN = b'rf-pool-request:v1'
_PAN_DOMAIN = b'rf-pool-pan:v1'
_PANS = frozenset(('4111111111111111', '5555555555554444'))


def _frame(*parts):
    return b''.join(len(part).to_bytes(4, 'big') + part for part in parts)


@dataclass(frozen=True, repr=False)
class RequestMac:
    directory: Path = field(repr=False)

    def __init__(self, keyring_directory):
        try:
            directory = Path(keyring_directory)
            if directory.parent != BASE / 'keyrings' or not _DIRECTORY.fullmatch(directory.name):
                raise ValueError
            object.__setattr__(self, 'directory', directory)
            self._key()
        except (OSError, TypeError, ValueError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def __repr__(self):
        return '<RequestMac>'

    def _key(self):
        """Recheck files each call; do not cache usable key bytes across changes."""
        try:
            key = _read_private_file(self.directory / 'request-mac.key')
            if len(key) != 32:
                raise ValueError
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError
                versions = 0
                with os.scandir(fd) as entries:
                    for scanned, entry in enumerate(entries, start=1):
                        if scanned > 128:
                            raise ValueError
                        name = entry.name
                        if name == 'request-mac.key' or not name.endswith('.key') or not _VERSION.fullmatch(name[:-4]):
                            continue
                        versions += 1
                        if versions > 64:
                            raise ValueError
                        # Different files can still contain identical material.
                        # Every syntactically valid Keyring version is a candidate,
                        # not just v1/v2: never reuse an AES key as the request MAC.
                        encryption_key = _read_private_file(self.directory / name)
                        if len(encryption_key) != 32 or hmac.compare_digest(key, encryption_key):
                            raise ValueError
            finally:
                os.close(fd)
            return key
        except (OSError, TypeError, ValueError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def request_digest(self, action: str, owner_id: str, canonical_bytes: bytes) -> str:
        try:
            if (type(action) is not str or not _ACTION.fullmatch(action)
                    or type(owner_id) is not str or str(UUID(owner_id)) != owner_id
                    or type(canonical_bytes) is not bytes or len(canonical_bytes) > 65536):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ServiceError(ErrorCode.INVALID_INPUT) from None
        return hmac.new(self._key(), _frame(_REQUEST_DOMAIN, action.encode('ascii'),
                        owner_id.encode('ascii'), canonical_bytes), hashlib.sha256).hexdigest()

    def pan_fingerprint(self, pan) -> str:
        # One-way dependency: typed payloads never import this module or a vault.
        from .pool_secret_types import Pan
        if type(pan) is not Pan or type(getattr(pan, 'value', None)) is not str or pan.value not in _PANS:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        # Deliberately no owner: the same synthetic physical card is globally unique.
        return hmac.new(self._key(), _frame(_PAN_DOMAIN, pan.value.encode('ascii')),
                        hashlib.sha256).hexdigest()
