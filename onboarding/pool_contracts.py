"""Pure pool input records, not authentication or permission to execute.

Frozen records must still be revalidated at service boundaries. These checks
only validate shape; they do not authenticate an Actor, claim a lease or check
its freshness. The owner label is not an operator or worker identity.
"""
from dataclasses import dataclass
from enum import Enum
import re
from uuid import UUID

from .errors import ErrorCode, ServiceError
from .security import Actor


class Platform(str, Enum):
    GOOGLE = 'google'
    CLAUDE = 'claude'
    CHATGPT = 'chatgpt'
    GROK = 'grok'
    KIRO = 'kiro'
    GITHUB = 'github'
    K12 = 'k12'


_LEASE_FIELDS = frozenset(('task_id', 'resource_kind', 'resource_id', 'owner_id',
                           'fence', 'credential_version'))
_CONTEXT_FIELDS = frozenset(('actor', 'task_id', 'platform', 'lease'))
_ACTOR_FIELDS = frozenset(('operator_id', 'permissions', 'session_id', 'auth_epoch'))
_OWNER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_MAX_BIGINT = 9223372036854775807


def _shape(record, expected_type, expected_fields):
    # Check exact type before inspecting attributes: no subclass hooks execute.
    if type(record) is not expected_type:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    values = object.__getattribute__(record, '__dict__')
    if (type(values) is not dict or any(type(key) is not str for key in values)
            or set(values) != expected_fields):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _uuid(value):
    if type(value) is not str:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _positive_bigint(value):
    if type(value) is not int or not 1 <= value <= _MAX_BIGINT:
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _actor(value):
    _shape(value, Actor, _ACTOR_FIELDS)
    _uuid(value.operator_id)
    _uuid(value.session_id)
    _positive_bigint(value.auth_epoch)
    if (type(value.permissions) is not frozenset
            or any(type(permission) is not str for permission in value.permissions)):
        raise ServiceError(ErrorCode.INVALID_INPUT)


@dataclass(frozen=True, repr=False)
class PoolLeaseToken:
    task_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    fence: int
    credential_version: int

    def __post_init__(self):
        PoolLeaseToken.validate(self)

    def __repr__(self):
        return '<PoolLeaseToken>'

    def validate(self):
        """Validate this complete record; returns None, never a capability."""
        _shape(self, PoolLeaseToken, _LEASE_FIELDS)
        _uuid(self.task_id)
        _uuid(self.resource_id)
        if type(self.resource_kind) is not str or self.resource_kind != 'mailbox':
            raise ServiceError(ErrorCode.INVALID_INPUT)
        if type(self.owner_id) is not str or _OWNER.fullmatch(self.owner_id) is None:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        _positive_bigint(self.fence)
        _positive_bigint(self.credential_version)


@dataclass(frozen=True, repr=False)
class PoolExecutionContext:
    actor: Actor
    task_id: str
    platform: Platform
    lease: PoolLeaseToken

    def __post_init__(self):
        PoolExecutionContext.validate(self)

    def __repr__(self):
        return '<PoolExecutionContext>'

    def validate(self):
        """Recheck all nested records without trusting instance callbacks."""
        _shape(self, PoolExecutionContext, _CONTEXT_FIELDS)
        _actor(self.actor)
        _uuid(self.task_id)
        # str.__new__ can forge an exact Platform without declaring an Enum
        # member. Identity rejects it without reading untrusted attributes.
        if (type(self.platform) is not Platform
                or not any(self.platform is member for member in Platform)):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        PoolLeaseToken.validate(self.lease)
        if self.task_id != self.lease.task_id:
            raise ServiceError(ErrorCode.INVALID_INPUT)
