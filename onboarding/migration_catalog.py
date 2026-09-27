"""Offline, pinned migration identities. Does not connect, migrate or grant.

Only reviewed files belong here. A planned migration is not an installed or
known version. Migration application and HTTP diagnostics share these identities.
"""
import hashlib
from pathlib import Path

from .errors import ErrorCode, ServiceError


_MIGRATIONS = (
    (1, '001_core.sql', '3fb6617853233370e867cbf7768b9e2953f929f29ecbd60eb638fa577c0f7782'),
    (2, '002_pools.sql', 'b763180ee4d782e6e88f2a78ac3bbf34df026425133e8c05ca99143eb56ec764'),
)


def _descriptor(version):
    if type(version) is int:
        for item in _MIGRATIONS:
            if item[0] == version:
                return item
    raise ServiceError(ErrorCode.INVALID_INPUT)


def expected_schema_versions(target=1):
    """Return the reviewed contiguous prefix, never infer an unknown version."""
    _descriptor(target)
    return tuple((version, checksum) for version, _, checksum in _MIGRATIONS
                 if version <= target)


def load_migration(version):
    """Verify bytes against a pinned identity before returning trusted SQL."""
    _, filename, checksum = _descriptor(version)
    try:
        source = (Path(__file__).parent / 'migrations' / filename).read_bytes()
    except OSError:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    if hashlib.sha256(source).hexdigest() != checksum:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    try:
        return source.decode('utf-8')
    except UnicodeError:
        raise ServiceError(ErrorCode.VERSION_CONFLICT) from None


def validate_applied(rows, *, minimum=1):
    """Validate ordered DB tuples without sorting away gaps or duplicates.

    Known later versions may coexist with an earlier core consumer; unknown,
    malformed or drifted history always fails closed. No downgrade is implied.
    """
    _descriptor(minimum)
    if type(rows) not in (list, tuple) or not rows:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    for row in rows:
        if (type(row) is not tuple or len(row) != 2 or type(row[0]) is not int
                or type(row[1]) is not str):
            raise ServiceError(ErrorCode.VERSION_CONFLICT)
    installed = rows[-1][0]
    known = {item[0] for item in _MIGRATIONS}
    if (installed not in known or installed < minimum
            or tuple(rows) != expected_schema_versions(installed)):
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return installed
