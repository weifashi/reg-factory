"""Owner-only synthetic card metadata. No secret reads, allocation or deletion.

Pages are bounded observations, not reservations or cross-request snapshots.
The MAC directory is trusted composition, never client-selected input.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import audit, card_import, security, storage
from .errors import ErrorCode, ServiceError
from .pool_vault import SyntheticPoolPolicy
from .request_mac import RequestMac
from .settings import Settings, _unique_object

_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_MAX_VERSION = 9223372036854775807


@dataclass(frozen=True)
class Page:
    items: tuple[dict, ...]
    next_cursor: str | None


def _metadata(value, maximum, *, empty=False):
    if type(value) is not str or not card_import._metadata(value, maximum, empty=empty):
        raise ValueError
    value.encode('utf-8')
    return value


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError
    return value


def _inputs(card_id, expected_version, changes, request_key):
    try:
        _uuid(card_id)
        if type(changes) is not dict:
            raise ValueError
        changes = changes.copy()
        if (type(expected_version) is not int or not 1 <= expected_version <= _MAX_VERSION
                or not changes or not set(changes) <= {'alias','account_limit','sms_channel_ref','enabled'}
                or type(request_key) is not str or not _KEY.fullmatch(request_key)):
            raise ValueError
        if 'alias' in changes:
            _metadata(changes['alias'], 80)
        if 'sms_channel_ref' in changes:
            _metadata(changes['sms_channel_ref'], 128, empty=True)
        if 'account_limit' in changes and (type(changes['account_limit']) is not int
                                         or not 1 <= changes['account_limit'] <= 10000):
            raise ValueError
        if 'enabled' in changes and type(changes['enabled']) is not bool:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    return changes


def _dependencies(settings, actor, policy, mac):
    if (type(settings) is not Settings or type(policy) is not SyntheticPoolPolicy
            or type(mac) is not RequestMac or settings != policy.settings):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    policy._validate()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _probe(mac, actor):
    return mac.request_digest('card.management.keycheck.v1', actor.operator_id, b'fixture:stable-key')


def _authorize(conn, actor, policy):
    policy._connection(conn)
    security.revalidate(conn, actor, 'cards:manage')


def _finish(conn, actor, policy, mac, probe):
    _authorize(conn, actor, policy)
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
    security.revalidate(conn, actor, 'cards:manage')


def _cursor(mac, actor, digest, anchor):
    raw = _canonical({'v':1, 'f':digest, 'i':anchor})
    return (base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') + '.'
            + mac.request_digest('card.list.cursor.v1', actor.operator_id, raw))


def _anchor(token, mac, actor, digest):
    try:
        if (type(token) is not str or len(token) > 512 or not token.isascii()
                or token.count('.') != 1):
            raise ValueError
        encoded, tag = token.split('.')
        if not _HASH.fullmatch(tag) or not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
            raise ValueError
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') != encoded:
            raise ValueError
        if not hmac.compare_digest(tag, mac.request_digest('card.list.cursor.v1', actor.operator_id, raw)):
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'v','f','i'} or type(value['v']) is not int
                or value['v'] != 1 or _canonical(value) != raw or type(value['f']) is not str
                or not _HASH.fullmatch(value['f']) or not hmac.compare_digest(value['f'], digest)):
            raise ValueError
        return _uuid(value['i'])
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _time(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def _dto(row):
    try:
        if type(row) is not tuple or len(row) != 15 or type(row[0]) is not UUID:
            raise ValueError
        _metadata(row[1],80)
        if (type(row[2]) is not str or row[2] not in ('visa','mastercard','amex','other')
                or type(row[3]) is not str or not re.fullmatch(r'[0-9]{4}',row[3])
                or any(type(row[i]) is not bool for i in (4,8,11))
                or type(row[5]) is not int or not 1 <= row[5] <= 10000
                or any(type(row[i]) is not int or row[i] < 0 for i in (6,7))
                or any(type(row[i]) is not int or not 1 <= row[i] <= _MAX_VERSION for i in (9,10))
                or (not row[8] and row[6]+row[7] > row[5])):
            raise ValueError
        return dict(id=str(row[0]),alias=row[1],brand=row[2],last4=row[3],enabled=row[4],
                    account_limit=row[5],linked_count=row[6],reserved_count=row[7],
                    reconciliation_required=row[8],version=row[9],revision=row[10],sms_configured=row[11],
                    created_at=_time(row[12]),updated_at=_time(row[13]),
                    last_assigned_at=None if row[14] is None else _time(row[14]))
    except (ValueError,TypeError,UnicodeError,OverflowError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def list_page(settings, actor, *, search='', enabled=None, cursor=None, limit=50, policy, mac):
    _dependencies(settings, actor, policy, mac)
    try:
        _metadata(search,80,empty=True)
        if (enabled is not None and type(enabled) is not bool
                or type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError
    except (ValueError,TypeError,UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    probe = _probe(mac,actor)
    digest = mac.request_digest('card.list.filter.v1', actor.operator_id, _canonical({
        'v':1,'schema':settings.schema,'instance_marker':settings.instance_marker,
        'mac_directory':str(mac.directory),'search':search.lower(),'enabled':enabled,'limit':limit,'order':'id_asc'}))
    with storage.unit_of_work(settings) as conn:
        _authorize(conn,actor,policy)
        anchor = None if cursor is None else _anchor(cursor,mac,actor,digest)
        rows = conn.execute('SELECT id,alias,brand,last4,enabled,account_limit,linked_count,reserved_count,'
            'reconciliation_required,version,revision,COALESCE(sms_channel_ref,\'\')<>\'\','
            'created_at,updated_at,last_assigned_at FROM payment_cards '
            'WHERE owner_operator_id=%s AND (strpos(lower(alias),%s)>0 OR strpos(last4,%s)>0) '
            'AND (%s::boolean IS NULL OR enabled=%s) AND (%s::uuid IS NULL OR id>%s::uuid) '
            'ORDER BY id ASC LIMIT %s',
            (actor.operator_id,search.lower(),search.lower(),enabled,enabled,anchor,anchor,limit+1)).fetchall()
        items = tuple(_dto(row) for row in rows)
        token = _cursor(mac,actor,digest,items[limit-1]['id']) if len(items)>limit else None
        result = Page(items[:limit],token)
        _finish(conn,actor,policy,mac,probe)
    return result


def _receipt(conn, actor, card_id, expected_version, key, digest):
    row = conn.execute('SELECT request_hash,phase,result_summary,resource_revision,generation,fence '
        'FROM operation_receipts WHERE task_id IS NULL AND scope_operator_id=%s '
        "AND action='card.update' AND idempotency_key=%s", (actor.operator_id,key)).fetchone()
    if row is None:
        return None
    if (type(row[0]) is not str or not _HASH.fullmatch(row[0]) or row[1] != 'SUCCEEDED'
            or row[3:] != ('pool-admin:'+actor.operator_id,1,0)):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if not hmac.compare_digest(row[0],digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    value = row[2]
    if (type(value) is not dict or set(value) != {'card_id','version','request_key'}
            or type(value['card_id']) is not str or value['card_id'] != card_id
            or type(value['version']) is not int or value['version'] != expected_version+1
            or value['version'] > _MAX_VERSION
            or type(value['request_key']) is not str or value['request_key'] != key):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return {'card_id':card_id,'version':value['version'],'request_key':key}


def update(settings, actor, card_id, expected_version, changes, request_key, *, policy, mac):
    _dependencies(settings,actor,policy,mac)
    changes = _inputs(card_id,expected_version,changes,request_key)
    probe = _probe(mac,actor)
    digest = mac.request_digest('card.update.v1',actor.operator_id,_canonical({
        'v':1,'schema':settings.schema,'instance_marker':settings.instance_marker,
        'card_id':card_id,'expected_version':expected_version,'changes':changes}))
    with storage.unit_of_work(settings) as conn:
        _authorize(conn,actor,policy)
        row = conn.execute('SELECT version,linked_count,reserved_count FROM payment_cards '
            'WHERE id=%s AND owner_operator_id=%s FOR UPDATE',(card_id,actor.operator_id)).fetchone()
        security.revalidate(conn,actor,'cards:manage')
        if row is None:
            raise ServiceError(ErrorCode.FORBIDDEN)
        # Replay the durable outcome even when the card has since advanced.
        result = _receipt(conn,actor,card_id,expected_version,request_key,digest)
        if result is None:
            if row[0] != expected_version or row[0] == _MAX_VERSION:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            if 'account_limit' in changes and changes['account_limit'] < row[1]+row[2]:
                raise ServiceError(ErrorCode.INVALID_INPUT)
            updated = conn.execute('UPDATE payment_cards SET '
                'alias=CASE WHEN %s THEN %s ELSE alias END,'
                'account_limit=CASE WHEN %s THEN %s ELSE account_limit END,'
                'sms_channel_ref=CASE WHEN %s THEN %s ELSE sms_channel_ref END,'
                'enabled=CASE WHEN %s THEN %s ELSE enabled END,'
                'version=version+1,updated_at=clock_timestamp() '
                'WHERE id=%s AND owner_operator_id=%s AND version=%s RETURNING version',
                ('alias' in changes,changes.get('alias'),'account_limit' in changes,changes.get('account_limit'),
                 'sms_channel_ref' in changes,changes.get('sms_channel_ref'),'enabled' in changes,
                 changes.get('enabled'),card_id,actor.operator_id,expected_version)).fetchone()
            if updated is None:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            result = {'card_id':card_id,'version':expected_version+1,'request_key':request_key}
            receipt_id = str(uuid4())
            inserted = conn.execute('INSERT INTO operation_receipts '
                '(id,task_id,scope_operator_id,action,resource_revision,generation,fence,phase,'
                'idempotency_key,request_hash,result_summary) '
                "VALUES(%s,NULL,%s,'card.update',%s,1,0,'SUCCEEDED',%s,%s,%s) "
                'ON CONFLICT DO NOTHING RETURNING id',
                (receipt_id,actor.operator_id,'pool-admin:'+actor.operator_id,request_key,digest,Jsonb(result))).fetchone()
            if inserted is None:
                # Another card may have won the same owner/key. Roll this candidate
                # back, never commit an extra update without its own audit/receipt.
                _receipt(conn,actor,card_id,expected_version,request_key,digest)
                raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            audit.append(conn,actor.operator_id,None,'card.update',card_id,'SUCCEEDED',receipt_id,
                         before_summary={'version':expected_version},after_summary={'version':expected_version+1})
        _finish(conn,actor,policy,mac,probe)
    # COMMIT_UNKNOWN is deliberately not retried or reclassified as not committed.
    return result
