"""Synthetic owner-only mailbox observations; no claim, decrypt or snapshot promise.

READ COMMITTED keyset pages are not a frozen cross-request snapshot: changed
filters/data and late commits can change membership. MAC directories are trusted
local composition, never HTTP input; Settings has no key-directory registry.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import re
import unicodedata
from uuid import UUID

from .errors import ErrorCode, ServiceError
from .request_mac import RequestMac
from .settings import Settings, _unique_object

_PLATFORMS = ('google', 'claude', 'chatgpt', 'grok', 'kiro', 'github', 'k12')
_HEALTH = ('UNKNOWN', 'HEALTHY', 'NEEDS_REVIEW', 'DISABLED')
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z', re.ASCII)


@dataclass(frozen=True)
class Page:
    items: tuple[dict, ...]
    next_cursor: str | None


def _text(value, maximum, minimum=0):
    if (type(value) is not str or not minimum <= len(value) <= maximum
            or any(unicodedata.category(c) in ('Cc', 'Cf') for c in value)):
        raise ValueError
    value.encode('utf-8')
    return value


def _filters(platform, search, group_ref, health, occupied, limit):
    if platform is None:
        platform = 'all'
    if type(platform) is not str or platform not in ('all', *_PLATFORMS):
        raise ValueError
    search = _text(search, 320).lower()
    if group_ref is not None:
        _text(group_ref, 128)
    if health is not None and (type(health) is not str or health not in _HEALTH):
        raise ValueError
    if occupied is not None and type(occupied) is not bool:
        raise ValueError
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError
    return dict(platform=platform, search=search, group_ref=group_ref,
                health=health, occupied=occupied, limit=limit)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError
    return value


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError
    return value


def _utc(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _parse_time(value):
    if type(value) is not str or not _TIME.fullmatch(value):
        raise ValueError
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if _utc(result) != value:
        raise ValueError
    return result


def _list_filter_mac(settings, owner_id, *, platform, search, group_ref, health, occupied, limit, mac):
    try:
        if type(settings) is not Settings or type(mac) is not RequestMac:
            raise ValueError
        filters = _filters(platform, search, group_ref, health, occupied, limit)
        value = {'purpose': 'mailbox.list.v1', 'scope': {'schema': settings.schema,
                 'instance_marker': settings.instance_marker, 'mac_directory': str(mac.directory)},
                 'order': 'created_at_desc_id_desc', **filters}
        return mac.request_digest('mailbox.filter.v1', owner_id, _canonical(value))
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _encode_list_cursor(owner_id, filter_mac, created_at, mailbox_id, *, mac):
    try:
        if type(mac) is not RequestMac:
            raise ValueError
        raw = _canonical({'v': 1, 'f': _digest(filter_mac), 't': _utc(created_at), 'i': _uuid(mailbox_id)})
        tag = mac.request_digest('mailbox.list.cursor.v1', owner_id, raw)
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') + '.' + tag
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _decode_list_cursor(token, owner_id, filter_mac, *, mac):
    try:
        if (type(mac) is not RequestMac or type(token) is not str or len(token) > 512
                or not token.isascii() or token.count('.') != 1):
            raise ValueError
        _digest(filter_mac)
        encoded, tag = token.split('.')
        _digest(tag)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
            raise ValueError
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') != encoded:
            raise ValueError
        # Authenticate bytes before interpreting data; malformed keys stay SECRET_UNAVAILABLE.
        actual = mac.request_digest('mailbox.list.cursor.v1', owner_id, raw)
        if not hmac.compare_digest(tag, actual):
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'v', 'f', 't', 'i'}
                or type(value['v']) is not int or value['v'] != 1 or _canonical(value) != raw):
            raise ValueError
        if not hmac.compare_digest(_digest(value['f']), filter_mac):
            raise ValueError
        return _parse_time(value['t']), _uuid(value['i'])
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


# Every selected value is explicitly allowed for the public DTO. Occupied is an
# observation of logical holds, not worker-TTL liveness and not a claim permit.
_DATA_SQL = """
WITH visible AS (
 SELECT m.id,m.email_norm,m.source_type,m.group_ref,m.health,m.disabled,
        m.ever_registration_attempted,m.sale_eligibility,m.pool_status,m.version,
        m.created_at,m.updated_at,m.last_used_at,
        (EXISTS (SELECT 1 FROM resource_leases l WHERE l.resource_kind='mailbox'
                 AND l.resource_id=m.id::text AND l.task_id IS NOT NULL)
         OR EXISTS (SELECT 1 FROM onboarding_tasks t WHERE t.execution_scope='pool'
                    AND t.mailbox_id=m.id
                    AND t.status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE'))) AS occupied,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
             'platform',p.platform,'identity_status',p.identity_status,
             'usage_status',p.usage_status,'version',p.version,'checked_at',p.checked_at)
             ORDER BY array_position(ARRAY['google','claude','chatgpt','grok','kiro','github','k12'],p.platform))
          FROM mailbox_platform_states p WHERE p.mailbox_id=m.id
            AND (%(platform)s='all' OR p.platform=%(platform)s)), '[]'::jsonb) AS platforms
 FROM mailbox_registry m
 WHERE m.owner_operator_id=%(owner)s
   AND strpos(m.email_norm,%(search)s)>0
   AND (%(group_ref)s::text IS NULL OR m.group_ref=%(group_ref)s)
   AND (%(health)s::text IS NULL OR m.health=%(health)s)
   AND (%(platform)s='all' OR EXISTS (SELECT 1 FROM mailbox_platform_states p
                                     WHERE p.mailbox_id=m.id AND p.platform=%(platform)s))
   AND (%(anchor_time)s::timestamptz IS NULL OR (m.created_at,m.id)<(%(anchor_time)s,%(anchor_id)s::uuid))
)
SELECT id,email_norm,source_type,group_ref,health,disabled,ever_registration_attempted,
       sale_eligibility,pool_status,version,created_at,updated_at,last_used_at,occupied,platforms
FROM visible WHERE (%(occupied)s::boolean IS NULL OR occupied=%(occupied)s)
ORDER BY created_at DESC,id DESC LIMIT %(fetch_limit)s
"""


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        raise ValueError
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError
    return value


def _version(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError
    return value


def _optional_time(value):
    return None if value is None else _utc(value)


def _platform_dto(value, platform):
    if type(value) is not list or len(value) > len(_PLATFORMS):
        raise ValueError
    items, seen = [], []
    for row in value:
        if type(row) is not dict or set(row) != {'platform','identity_status','usage_status','version','checked_at'}:
            raise ValueError
        name = _enum(row['platform'], _PLATFORMS)
        index = _PLATFORMS.index(name)
        if (seen and index <= seen[-1]) or (platform != 'all' and name != platform):
            raise ValueError
        seen.append(index)
        checked = row['checked_at']
        if checked is not None:
            if type(checked) is not str or len(checked) > 40:
                raise ValueError
            checked = _utc(datetime.fromisoformat(checked))
        items.append({'platform': name,
            'identity_status': _enum(row['identity_status'], ('UNKNOWN','EXISTING','NEW_CONFIRMED')),
            'usage_status': _enum(row['usage_status'], ('UNUSED','RESERVED','SUCCEEDED',
                'FAILED_CONFIRMED','UNKNOWN','CONFLICT','HISTORY_UNRECONCILED')),
            'version': _version(row['version']), 'checked_at': checked})
    if platform != 'all' and len(items) != 1:
        raise ValueError
    return tuple(items)


def _mailbox_dto(row, platform):
    try:
        if type(row) is not tuple or len(row) != 15 or type(row[0]) is not UUID:
            raise ValueError
        email = _text(row[1], 320, 3)
        if email != email.lower():
            raise ValueError
        return {'id': str(row[0]), 'email': email,
            'source_type': _enum(row[2], ('outlook','icloud','other')),
            'group_ref': _text(row[3], 128), 'health': _enum(row[4], _HEALTH),
            'disabled': _boolean(row[5]), 'ever_registration_attempted': _boolean(row[6]),
            'sale_eligibility': _enum(row[7], ('UNVERIFIED','ELIGIBLE','INELIGIBLE')),
            'pool_status': _enum(row[8], ('AVAILABLE','EXPORTED','QUARANTINED')),
            'version': _version(row[9]), 'created_at': _utc(row[10]),
            'updated_at': _utc(row[11]), 'last_used_at': _optional_time(row[12]),
            'occupied': _boolean(row[13]), 'platforms': _platform_dto(row[14], platform)}
    except (ValueError, TypeError, UnicodeError, OverflowError):
        # No malformed DB value or its repr may escape into the error DTO.
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def list_page(settings, actor, *, platform=None, search='', group_ref=None,
              health=None, occupied=None, cursor=None, limit=50, policy, mac):
    """One owner-scoped read SELECT plus existing environment/auth checks.

    No idle-session refresh, resource/audit/receipt write or secret decryption.
    The immutable Page is returned only after the UoW has completed COMMIT.
    """
    from . import security, storage
    from .pool_vault import SyntheticPoolPolicy

    if (type(settings) is not Settings or type(policy) is not SyntheticPoolPolicy
            or type(mac) is not RequestMac or settings != policy.settings):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    try:
        filters = _filters(platform, search, group_ref, health, occupied, limit)
        if cursor is not None and (type(cursor) is not str or len(cursor) > 512):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    policy._validate()
    probe = mac.request_digest('mailbox.list.keycheck.v1', actor.operator_id, b'fixture:stable-key')
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        digest = _list_filter_mac(settings, actor.operator_id, platform=platform, search=search,
                                  group_ref=group_ref, health=health, occupied=occupied, limit=limit, mac=mac)
        anchor = (None, None) if cursor is None else _decode_list_cursor(cursor, actor.operator_id, digest, mac=mac)
        params = {**filters, 'owner': actor.operator_id, 'anchor_time': anchor[0],
                  'anchor_id': anchor[1], 'fetch_limit': limit + 1}
        rows = conn.execute(_DATA_SQL, params).fetchall()
        # Validate even the extra row: a corrupt sentinel is not trustworthy evidence.
        items = tuple(_mailbox_dto(row, filters['platform']) for row in rows)
        next_cursor = None
        if len(items) > limit:
            last = rows[limit - 1]
            next_cursor = _encode_list_cursor(actor.operator_id, digest, last[10], str(last[0]), mac=mac)
        result = Page(items[:limit], next_cursor)
        policy._connection(conn)
        current = mac.request_digest('mailbox.list.keycheck.v1', actor.operator_id, b'fixture:stable-key')
        if not hmac.compare_digest(probe, current):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn, actor, 'onboarding:read')
    return result
