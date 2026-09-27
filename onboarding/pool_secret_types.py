"""Exact, immutable synthetic inputs. No storage, callbacks or real-data mode."""
from dataclasses import dataclass, fields
import json
import re
from uuid import UUID

from .errors import ErrorCode, ServiceError

_PLATFORMS = frozenset(('google', 'claude', 'chatgpt', 'grok', 'kiro', 'github', 'k12'))
_PANS = frozenset(('4111111111111111', '5555555555554444'))
_CLIENT = '00000000-0000-4000-8000-000000000001'
_TOTP = 'JBSWY3DPEHPK3PXP'
_EMAIL = re.compile(r'[a-z0-9][a-z0-9._+%-]{0,63}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*fixture\.invalid\Z')


def _shape(record):
    if set(vars(record)) != {item.name for item in fields(record)}:
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _ref(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _text(value, *, maximum=16384):
    try:
        if (type(value) is not str or len(value.encode('utf-8')) > maximum
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ServiceError(ErrorCode.FORBIDDEN) from None


def _synthetic(value, *, empty=False):
    _text(value)
    if not (empty and value == '') and not (value.startswith('fixture:') and len(value) > 8):
        raise ServiceError(ErrorCode.FORBIDDEN)


def _email(value):
    _text(value, maximum=320)
    if not _EMAIL.fullmatch(value):
        raise ServiceError(ErrorCode.FORBIDDEN)


class _Hidden:
    def __repr__(self):
        return '<' + type(self).__name__ + '>'


class _Payload(_Hidden):
    def __post_init__(self):
        _encode_payload(self)


@dataclass(frozen=True, repr=False)
class MailboxCredential(_Payload):
    email_norm: str
    password: str = ''
    refresh_token: str = ''
    client_id: str = ''
    provider: str = 'outlook'
    mail_api_url: str = ''
    mail_api_key: str = ''
    two_factor: str = ''


@dataclass(frozen=True, repr=False)
class PlatformCredential(_Payload):
    email_norm: str
    platform: str
    account_password: str


@dataclass(frozen=True, repr=False)
class Pan(_Payload):
    value: str


@dataclass(frozen=True, repr=False)
class BillingHolder(_Payload):
    name: str


@dataclass(frozen=True, repr=False)
class BillingAddress(_Payload):
    country: str
    line1: str
    line2: str
    city: str
    region: str
    postal_code: str


@dataclass(frozen=True, repr=False)
class CardExpiry(_Payload):
    month: int
    year: int


@dataclass(frozen=True, repr=False)
class PoolResource(_Hidden):
    kind: str
    id: str

    def __post_init__(self):
        _shape(self)
        if type(self.kind) is not str or self.kind not in ('mailbox', 'platform', 'card', 'billing'):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        _ref(self.id)


@dataclass(frozen=True, repr=False)
class SecretRef(_Hidden):
    id: str
    revision: int

    def __post_init__(self):
        _shape(self)
        _ref(self.id)
        if type(self.revision) is not int or self.revision < 1:
            raise ServiceError(ErrorCode.INVALID_INPUT)


_TYPES = {
    MailboxCredential: ('mailbox_credential', 'mailbox', 'mailboxes:manage'),
    PlatformCredential: ('platform_credential', 'platform', 'mailboxes:manage'),
    Pan: ('pan', 'card', 'cards:manage'),
    BillingHolder: ('billing_holder', 'billing', 'cards:manage'),
    BillingAddress: ('billing_address', 'billing', 'cards:manage'),
    CardExpiry: ('card_expiry', 'card', 'cards:manage'),
}


def _encode_payload(payload):
    """Private validation/encoding; re-run at every write, not constructor-only."""
    kind = type(payload)
    if kind not in _TYPES:
        raise ServiceError(ErrorCode.FORBIDDEN)
    _shape(payload)
    if kind is MailboxCredential:
        _email(payload.email_norm)
        for value in (payload.password, payload.refresh_token, payload.mail_api_key):
            _synthetic(value, empty=True)
        for value in (payload.client_id, payload.provider, payload.mail_api_url, payload.two_factor):
            _text(value)
        if (payload.client_id not in ('', _CLIENT) or payload.two_factor not in ('', _TOTP)
                or payload.provider not in ('outlook', 'graph', 'microsoft', 'icloud')
                or payload.mail_api_url not in ('', 'https://mail.fixture.invalid')
                or not (payload.password or payload.refresh_token or payload.mail_api_url or payload.provider == 'icloud')):
            raise ServiceError(ErrorCode.FORBIDDEN)
    elif kind is PlatformCredential:
        _email(payload.email_norm)
        if type(payload.platform) is not str or payload.platform not in _PLATFORMS:
            raise ServiceError(ErrorCode.FORBIDDEN)
        _synthetic(payload.account_password)
    elif kind is Pan:
        if type(payload.value) is not str or payload.value not in _PANS:
            raise ServiceError(ErrorCode.FORBIDDEN)
    elif kind is BillingHolder:
        if type(payload.name) is not str or payload.name != 'fixture:holder':
            raise ServiceError(ErrorCode.FORBIDDEN)
    elif kind is BillingAddress:
        if type(payload.country) is not str or payload.country != 'US':
            raise ServiceError(ErrorCode.FORBIDDEN)
        for name, expected in (('line1','fixture:line'), ('city','fixture:city'),
                               ('region','fixture:region'), ('postal_code','fixture:postal')):
            if type(getattr(payload,name)) is not str or getattr(payload,name) != expected:
                raise ServiceError(ErrorCode.FORBIDDEN)
        if type(payload.line2) is not str or payload.line2 not in ('','fixture:line2'):
            raise ServiceError(ErrorCode.FORBIDDEN)
    elif kind is CardExpiry:
        if type(payload.month) is not int or not 1 <= payload.month <= 12 or type(payload.year) is not int or payload.year != 2099:
            raise ServiceError(ErrorCode.FORBIDDEN)
    clear = json.dumps({field.name: getattr(payload, field.name) for field in fields(payload)},
                       ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(clear) > 65536:
        raise ServiceError(ErrorCode.FORBIDDEN)
    return (*_TYPES[kind], clear)
