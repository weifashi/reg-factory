"""Session-bound one-use synthetic downloads; no public HTTP endpoint.

Locks: task -> operator/session -> approval -> secret -> grant. Clear bytes leave
only after our transaction commits; failed/unknown delivery never restores a grant.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
from uuid import uuid4

from psycopg.rows import dict_row
from . import audit, repository, security, storage
from .errors import ErrorCode, ServiceError
from .secret_store import SecretStore

@dataclass(frozen=True)
class Grant:
    id: str
    token: str = field(repr=False)


def _row(conn, query, params):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


_NO_STORE = object()


def _boundary(conn, actor, store=_NO_STORE):
    repository.require_transaction(conn)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    if store is not _NO_STORE and type(store) is not SecretStore:
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _revision(task, secret_id, revision, session_id):
    return hashlib.sha256(json.dumps([repository.resource_revision(task), secret_id,
                                    revision, session_id], separators=(',', ':')).encode()).hexdigest()


def _task(conn, actor, task_id):
    task = repository.lock_task(conn, actor, str(task_id), permission='keys:download')
    if task['cancel_requested'] or task['status'] in ('PAUSED', 'CANCELLED_SAFE', 'CONFLICT'):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    return task


def _secret(conn, actor, secret_id, revision):
    repository._uuid(secret_id)
    if type(revision) is not int or revision < 1:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    row = _row(conn, 'SELECT id,kind,revision,access_policy,expires_at,revoked_at '
                     'FROM secret_objects WHERE id=%s FOR UPDATE', (secret_id,))
    if (row is None or row['kind'] != 'fixture' or row['revision'] != revision
            or row['access_policy'] != 'operator:' + actor.operator_id):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
    return row


def _live(conn, actor, secret, approval=None, grant=None):
    security.revalidate(conn, actor, 'keys:download')
    now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
    if secret['revoked_at'] is not None or (secret['expires_at'] and secret['expires_at'] <= now):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
    if approval and (approval['revoked_at'] is not None or approval['expires_at'] <= now):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    if grant and (grant['revoked_at'] is not None or grant['expires_at'] <= now):
        raise ServiceError(ErrorCode.GRANT_UNAVAILABLE)


def approve(conn, actor, task_id, secret_id, secret_revision, ttl_seconds, *, store):
    _boundary(conn, actor, store)
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 600:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    task = _task(conn, actor, task_id)
    # Old approvals precede secret locks, including approval replacement.
    rows = conn.execute("UPDATE approvals SET revoked_at=clock_timestamp(),version=version+1,"
                        "updated_at=clock_timestamp() WHERE task_id=%s AND action='download' "
                        'AND consumed_at IS NULL AND revoked_at IS NULL RETURNING id', (task_id,)).fetchall()
    for row in rows:
        audit.append(conn, actor.operator_id, task_id, 'approval.revoke', str(row[0]), 'REPLACED', str(row[0]))
    secret = _secret(conn, actor, secret_id, secret_revision)
    _live(conn, actor, secret)
    ident = str(uuid4())
    approval = _row(conn, 'INSERT INTO approvals '
                    '(id,task_id,action,resource_revision,config_revision,actor_id,expires_at) '
                    "VALUES(%s,%s,'download',%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING *",
                    (ident, task_id, _revision(task, secret_id, secret_revision, actor.session_id),
                     task['config_revision'], actor.operator_id, ttl_seconds))
    audit.append(conn, actor.operator_id, task_id, 'download.approve', ident, 'APPROVED', ident)
    _live(conn, actor, secret, approval)
    return ident


def _approval(conn, actor, approval_id, *, protective=False):
    repository._uuid(approval_id)
    locator = conn.execute('SELECT task_id FROM approvals WHERE id=%s', (approval_id,)).fetchone()
    if locator is None:
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    task = (repository.lock_task(conn, actor, str(locator[0]), permission='keys:download')
            if protective else _task(conn, actor, locator[0]))
    row = _row(conn, 'SELECT * FROM approvals WHERE id=%s FOR UPDATE', (approval_id,))
    if row is None or row['action'] != 'download' or str(row['actor_id']) != actor.operator_id:
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    return task, row


def _bound(task, actor, approval, secret_id, revision):
    if (approval['resource_revision'] != _revision(task, secret_id, revision, actor.session_id)
            or approval['config_revision'] != task['config_revision']):
        raise ServiceError(ErrorCode.APPROVAL_INVALID)


def _token_hash(session_id, token):
    return hashlib.sha256((session_id + '|' + token).encode()).hexdigest()


def issue(conn, actor, approval_id, secret_id, secret_revision, *, store):
    _boundary(conn, actor, store)
    task, approval = _approval(conn, actor, approval_id)
    _bound(task, actor, approval, secret_id, secret_revision)
    if approval['consumed_at'] is not None:
        raise ServiceError(ErrorCode.APPROVAL_INVALID)
    secret = _secret(conn, actor, secret_id, secret_revision)
    _live(conn, actor, secret, approval)
    ident, receipt_id, token = str(uuid4()), str(uuid4()), secrets.token_urlsafe(32)
    revision = approval['resource_revision']
    conn.execute('INSERT INTO operation_receipts '
                 '(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,result_code,generation) '
                 "VALUES(%s,%s,'download.grant',%s,%s,%s,'SUCCEEDED',0,'GRANT_ISSUED',%s)",
                 (receipt_id, task['id'], revision, approval_id, revision, task['generation']))
    grant = _row(conn, 'INSERT INTO download_grants '
                 '(id,approval_id,secret_id,secret_revision,operator_id,token_hash,expires_at) '
                 "VALUES(%s,%s,%s,%s,%s,%s,LEAST(%s,clock_timestamp()+interval '60 seconds')) RETURNING *",
                 (ident, approval_id, secret_id, secret_revision, actor.operator_id,
                  _token_hash(actor.session_id, token), approval['expires_at']))
    conn.execute('UPDATE approvals SET consumed_at=clock_timestamp(),receipt_id=%s,version=version+1,'
                 'updated_at=clock_timestamp() WHERE id=%s', (receipt_id, approval_id))
    audit.append(conn, actor.operator_id, task['id'], 'download.issue', ident, 'ISSUED', ident)
    _live(conn, actor, secret, approval, grant)
    return Grant(ident, token)


def consume(settings, actor, grant_id, raw_token, *, store):
    repository._uuid(grant_id)
    if type(raw_token) is not str or re.fullmatch(r'[A-Za-z0-9_-]{43}', raw_token) is None:
        raise ServiceError(ErrorCode.GRANT_UNAVAILABLE)
    with storage.unit_of_work(settings) as conn:
        _boundary(conn, actor, store)
        locator = _row(conn, 'SELECT approval_id,secret_id,secret_revision FROM download_grants WHERE id=%s', (grant_id,))
        if locator is None:
            raise ServiceError(ErrorCode.GRANT_UNAVAILABLE)
        task, approval = _approval(conn, actor, str(locator['approval_id']))
        secret = _secret(conn, actor, str(locator['secret_id']), locator['secret_revision'])
        grant = _row(conn, 'SELECT * FROM download_grants WHERE id=%s FOR UPDATE', (grant_id,))
        if (grant is None or grant['consumed_at'] is not None
                or str(grant['operator_id']) != actor.operator_id
                or not hmac.compare_digest(grant['token_hash'], _token_hash(actor.session_id, raw_token))):
            raise ServiceError(ErrorCode.GRANT_UNAVAILABLE)
        _bound(task, actor, approval, str(grant['secret_id']), grant['secret_revision'])
        if approval['consumed_at'] is None or approval['receipt_id'] is None:
            raise ServiceError(ErrorCode.APPROVAL_INVALID)
        _live(conn, actor, secret, approval, grant)
        clear = store.read_for_download(conn, actor, str(grant['secret_id']), grant['secret_revision'])
        conn.execute('UPDATE download_grants SET consumed_at=clock_timestamp(),version=version+1,'
                     'updated_at=clock_timestamp() WHERE id=%s', (grant_id,))
        audit.append(conn, actor.operator_id, task['id'], 'download.consume', grant_id, 'CONSUMED', grant_id)
        _live(conn, actor, secret, approval, grant)
    return clear


def revoke(conn, actor, grant_id):
    _boundary(conn, actor)
    repository._uuid(grant_id)
    row = conn.execute('SELECT approval_id FROM download_grants WHERE id=%s', (grant_id,)).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.GRANT_UNAVAILABLE)
    task, _ = _approval(conn, actor, str(row[0]), protective=True)
    grant = _row(conn, 'SELECT * FROM download_grants WHERE id=%s FOR UPDATE', (grant_id,))
    if str(grant['operator_id']) != actor.operator_id:
        raise ServiceError(ErrorCode.GRANT_UNAVAILABLE)
    if grant['consumed_at'] is None and grant['revoked_at'] is None:
        conn.execute('UPDATE download_grants SET revoked_at=clock_timestamp(),version=version+1,'
                     'updated_at=clock_timestamp() WHERE id=%s', (grant_id,))
        audit.append(conn, actor.operator_id, task['id'], 'download.revoke', grant_id, 'REVOKED', grant_id)
    security.revalidate(conn, actor, 'keys:download')
