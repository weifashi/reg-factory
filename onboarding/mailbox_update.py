"""Synthetic owner-only metadata commands; no credentials or allocation changes.

The MAC directory is trusted local composition, not caller-selected HTTP input.
A disabled mailbox retains its in-flight tasks, logical holds and history.
"""
import hmac
import json
import re
import unicodedata
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from . import audit, security, storage
from .errors import ErrorCode, ServiceError
from .pool_vault import SyntheticPoolPolicy
from .request_mac import RequestMac
from .settings import Settings

_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_MAX_VERSION = 9223372036854775807


def _inputs(mailbox_id, expected_version, changes, request_key):
    try:
        if type(changes) is not dict:
            raise ValueError
        # Snapshot before inspecting any keys or values: the caller may mutate
        # its dict concurrently, including while Unicode validation runs.
        changes = changes.copy()
        if (type(mailbox_id) is not str or str(UUID(mailbox_id)) != mailbox_id
                or type(expected_version) is not int or not 1 <= expected_version <= _MAX_VERSION
                or not changes
                or not set(changes) <= {'group_ref', 'disabled'}
                or type(request_key) is not str or not _KEY.fullmatch(request_key)):
            raise ValueError
        if 'group_ref' in changes:
            group = changes['group_ref']
            if (type(group) is not str or len(group) > 128
                    or any(unicodedata.category(char) in ('Cc', 'Cf') for char in group)):
                raise ValueError
            group.encode('utf-8')
        if 'disabled' in changes and type(changes['disabled']) is not bool:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    # Use one validated snapshot for both digest and SQL, not a mutable caller DTO.
    return changes


def _probe(mac, actor):
    return mac.request_digest('mailbox.update.keycheck.v1', actor.operator_id, b'fixture:stable-key')


def _stable(mac, actor, probe):
    if not hmac.compare_digest(probe, _probe(mac, actor)):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _receipt(conn, actor, mailbox_id, expected_version, key, digest):
    row = conn.execute('SELECT id,task_id,scope_operator_id,action,resource_revision,generation,fence,'
        'phase,idempotency_key,request_hash,result_summary FROM operation_receipts '
        "WHERE task_id IS NULL AND scope_operator_id=%s AND action='mailbox.update' "
        'AND idempotency_key=%s', (actor.operator_id, key)).fetchone()
    if row is None:
        return None
    # Treat malformed storage as dependency corruption before interpreting JSON.
    valid = (type(row[0]) is UUID and row[1] is None and str(row[2]) == actor.operator_id
        and row[3] == 'mailbox.update' and row[4] == 'pool-admin:' + actor.operator_id
        and type(row[5]) is int and row[5] == 1 and type(row[6]) is int and row[6] == 0
        and row[7] == 'SUCCEEDED' and type(row[8]) is str and row[8] == key
        and type(row[9]) is str and _HASH.fullmatch(row[9]))
    if not valid:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if not hmac.compare_digest(row[9], digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    value = row[10]
    valid = (type(value) is dict and set(value) == {'mailbox_id', 'version', 'request_key'})
    if valid:
        valid = (type(value['mailbox_id']) is str and value['mailbox_id'] == mailbox_id
            and type(value['version']) is int and value['version'] == expected_version + 1
            and value['version'] <= _MAX_VERSION
            and type(value['request_key']) is str and value['request_key'] == key)
    if not valid:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    # Return only validated fields, never arbitrary stored JSON.
    return {'mailbox_id': mailbox_id, 'version': value['version'], 'request_key': key}


def update(settings, actor, mailbox_id, expected_version, changes, request_key, *, policy, mac):
    """One terminal receipt per owner/key, one metadata CAS, commit before return."""
    if (type(settings) is not Settings or type(policy) is not SyntheticPoolPolicy
            or type(mac) is not RequestMac or settings != policy.settings):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    changes = _inputs(mailbox_id, expected_version, changes, request_key)
    policy._validate()
    probe = _probe(mac, actor)
    body = json.dumps({'v': 1, 'schema': settings.schema, 'instance_marker': settings.instance_marker,
        'mailbox_id': mailbox_id, 'expected_version': expected_version, 'changes': changes},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = mac.request_digest('mailbox.update.v1', actor.operator_id, body)
    _stable(mac, actor, probe)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'mailboxes:manage')
        row = conn.execute('SELECT version FROM mailbox_registry WHERE id=%s '
            'AND owner_operator_id=%s FOR UPDATE', (mailbox_id, actor.operator_id)).fetchone()
        security.revalidate(conn, actor, 'mailboxes:manage')
        if row is None:
            raise ServiceError(ErrorCode.FORBIDDEN)
        # A successful old-version replay must precede the current-version gate.
        result = _receipt(conn, actor, mailbox_id, expected_version, request_key, digest)
        if result is None:
            if row[0] > expected_version or row[0] == _MAX_VERSION:
                # Spec C4 before the not-committed proof (mailbox versions only increase).
                policy._connection(conn)
                _stable(mac, actor, probe)
                security.revalidate(conn, actor, 'mailboxes:manage')
                raise ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)
            if row[0] != expected_version:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            updated = conn.execute('UPDATE mailbox_registry SET '
                'group_ref=CASE WHEN %s THEN %s ELSE group_ref END,'
                'disabled=CASE WHEN %s THEN %s ELSE disabled END,'
                'version=version+1,updated_at=clock_timestamp() '
                'WHERE id=%s AND owner_operator_id=%s AND version=%s RETURNING version',
                ('group_ref' in changes, changes.get('group_ref'), 'disabled' in changes,
                 changes.get('disabled'), mailbox_id, actor.operator_id, expected_version)).fetchone()
            if updated is None:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            result = {'mailbox_id': mailbox_id, 'version': expected_version + 1, 'request_key': request_key}
            receipt_id = str(uuid4())
            inserted = conn.execute('INSERT INTO operation_receipts '
                '(id,task_id,scope_operator_id,action,resource_revision,generation,fence,phase,'
                'idempotency_key,request_hash,result_summary) '
                "VALUES(%s,NULL,%s,'mailbox.update',%s,1,0,'SUCCEEDED',%s,%s,%s) "
                'ON CONFLICT DO NOTHING RETURNING id',
                (receipt_id, actor.operator_id, 'pool-admin:' + actor.operator_id,
                 request_key, digest, Jsonb(result))).fetchone()
            if inserted is None:
                # A different target can win the owner/key unique index. Its
                # differing digest rolls back our candidate UPDATE and timestamp.
                _receipt(conn, actor, mailbox_id, expected_version, request_key, digest)
                # Equal digest after candidate UPDATE is impossible under target
                # row locking; never commit an extra unaudited modification.
                raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            audit.append(conn, actor.operator_id, None, 'mailbox.update', mailbox_id,
                'SUCCEEDED', receipt_id, before_summary={'version': expected_version},
                after_summary={'version': expected_version + 1})
        policy._connection(conn)
        _stable(mac, actor, probe)
        security.revalidate(conn, actor, 'mailboxes:manage')
    return result
