"""Explicit INSERT-live single-account admission, never synthetic promotion.

This owns one transaction, returning only after confirmed COMMIT. A receipt
replay is observation, NOT another startup permission. No provider writes.
"""
import hmac
from contextlib import ExitStack
import json
import time
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from common.managed_mailboxes import management_lock, MailboxBoundaryError

from . import (audit, live_admission, live_config, pool_batches, pool_cancellation,
               pool_config, pool_leases, pool_repository, security, storage)
from .errors import ErrorCode, ServiceError

ACTION = 'live.batch.create_one'
_FIELDS = frozenset(('schema_version', 'execution_mode', 'workflow', 'batch_id', 'task_ids',
    'mailbox_ids', 'config_id', 'config_revision', 'selection', 'requested_count',
    'mailbox_credential_ref', 'credential_version', 'state_id', 'source_digest',
    'source_fingerprint', 'original_usage_status', 'admission'))


def _deny(code=ErrorCode.RECONCILIATION_REQUIRED):
    raise ServiceError(code)


def _summary(value):
    if (type(value) is not dict or set(value) != _FIELDS
            or not pool_cancellation._same({k: value[k] for k in ('schema_version', 'execution_mode',
                'workflow', 'selection', 'requested_count', 'original_usage_status')},
                dict(schema_version=1, execution_mode='live', workflow='existing_billing_cloud_v1',
                     selection='specified', requested_count=1, original_usage_status='HISTORY_UNRECONCILED'))):
        _deny()
    for name in ('batch_id', 'config_id', 'mailbox_credential_ref', 'state_id'):
        if not pool_batches._uuid(value[name]):
            _deny()
    for name in ('task_ids', 'mailbox_ids'):
        if type(value[name]) is not list or len(value[name]) != 1 or not pool_batches._uuid(value[name][0]):
            _deny()
    if not pool_batches._revision(value['config_revision']) or not pool_batches._integer(value['credential_version']):
        _deny()
    for name in ('source_digest', 'source_fingerprint'):
        if type(value[name]) is not str or not pool_batches._HASH.fullmatch(value[name]):
            _deny()
    admission = value['admission']
    if (type(admission) is not dict or set(admission) != {'account', 'billing_account', 'outcome'}
            or admission.get('account') != live_admission._ACCOUNT or admission.get('outcome') != 'BILLING_VISIBLE_ONLY'
            or type(admission.get('billing_account')) is not str
            or not live_config._BILLING.fullmatch(admission['billing_account'])):
        _deny()
    return value


def creation_details(conn, actor, task, events):
    """Strict durable provenance, accepting additional post-creation audit rows.

    No fallback to old receipts, no dependence on current config HEAD, and no
    implication that historical admission authorizes a new provider operation.
    """
    if len(events) < 2:
        return None
    created, claimed = events[:2]
    if (created[:4] != (UUID(actor.operator_id), 'task.create', task['id'], 'CREATED')
            or not pool_cancellation._same(created[5], {})
            or not pool_cancellation._same(created[6], {'status': 'QUEUED', 'version': 2})
            or claimed[:4] != (UUID(actor.operator_id), 'lease.claim', task['mailbox_id'], 'OK')
            or claimed[4] != created[4] or not pool_batches._uuid(created[4])):
        return None
    row = conn.execute('SELECT task_id,scope_operator_id,action,resource_revision,generation,fence,phase,'
        'result_code,external_ref,result_summary,request_hash FROM operation_receipts WHERE id=%s',
        (created[4],)).fetchone()
    if (row is None or row[:9] != (None, UUID(actor.operator_id), ACTION,
            'pool-live-admin:' + actor.operator_id, 1, 0, 'SUCCEEDED', 'LIVE_BATCH_ACCEPTED', None)
            or type(row[10]) is not str or not pool_batches._HASH.fullmatch(row[10])):
        return None
    try:
        value = _summary(row[9])
        expected = dict(batch_id=task['batch_id'], task_ids=[task['id']], mailbox_ids=[task['mailbox_id']],
            config_id=task['config_id'], config_revision=task['config_revision'],
            mailbox_credential_ref=task['mailbox_credential_ref'], credential_version=task['credential_version'])
        if not pool_cancellation._same({k: value[k] for k in expected}, expected):
            return None
        if (task['selection_mode'] != 'specified' or task['requested_count'] != 1
                or tuple(task['selected_mailbox_refs']) != (task['mailbox_id'],)):
            return None
        pin = task['platform_credential_pins']['google']
        if not pool_cancellation._same(pin, dict(state_id=value['state_id'],
                secret_ref=value['mailbox_credential_ref'], revision=value['credential_version'], identity_status='UNKNOWN')):
            return None
        members = conn.execute('SELECT id,mailbox_id,execution_mode FROM onboarding_tasks '
                               'WHERE batch_id=%s ORDER BY id', (task['batch_id'],)).fetchall()
        if members != [(UUID(task['id']), UUID(task['mailbox_id']), 'live')]:
            return None
        config = conn.execute('SELECT revision,scope,nonsecret_config FROM global_configs WHERE id=%s',
                              (task['config_id'],)).fetchone()
        if config is None or config[:2] != (value['config_revision'], 'pool'):
            return None
        fields = live_config._fields(config[2])
        if fields['billing_account'] != value['admission']['billing_account']:
            return None
        source = conn.execute('SELECT owner_operator_id,source_fingerprint FROM mailbox_registry WHERE id=%s',
                              (task['mailbox_id'],)).fetchone()
        if source != (UUID(actor.operator_id), value['source_fingerprint']):
            return None
        before, after = claimed[5:]
        names = {'resource_kind', 'version', 'fence', 'hold_reason'}
        if (type(before) is not dict or type(after) is not dict or set(before) != names or set(after) != names
                or before['resource_kind'] != 'mailbox' or after['resource_kind'] != 'mailbox'
                or before['hold_reason'] is not None or after['hold_reason'] != 'HELD'):
            return None
        for field in ('version', 'fence'):
            if (not pool_batches._integer(before[field], 0 if field == 'fence' else 1)
                    or not pool_batches._integer(after[field]) or after[field] != before[field] + 1):
                return None
    except (ServiceError, ValueError, TypeError, KeyError):
        return None
    return value.copy()


def creation_proof(conn, actor, task, events):
    return events[1][6].copy() if creation_details(conn, actor, task, events) is not None else None


def _digest(settings, actor, summary, key, mac):
    body = {k: summary[k] for k in ('workflow', 'execution_mode', 'mailbox_ids', 'config_revision',
        'mailbox_credential_ref', 'credential_version', 'state_id', 'source_digest', 'source_fingerprint', 'admission')}
    body.update(schema=settings.schema, instance=settings.instance_marker, request_key=key, v=1)
    return mac.request_digest('live.batch.create_one.v1', actor.operator_id,
                              json.dumps(body, sort_keys=True, separators=(',', ':')).encode())


def _task(conn, task_id):
    row = conn.execute('SELECT ' + ','.join(pool_repository._TASK_FIELDS) + ',execution_mode' +
                       ' FROM onboarding_tasks WHERE id=%s', (task_id,)).fetchone()
    if row is None:
        _deny()
    task = pool_repository._project_task(row[:-1])
    task['execution_mode'] = row[-1]
    batch = conn.execute('SELECT created_by,config_id,selected_mailbox_refs,requested_count,selection_mode '
                         'FROM onboarding_batches WHERE id=%s', (task['batch_id'],)).fetchone()
    task.update(pool_repository._project_batch(batch, task))
    row = conn.execute('SELECT revision FROM global_configs WHERE id=%s', (task['config_id'],)).fetchone()
    if row is None:
        _deny()
    task['config_revision'] = row[0]
    return task


def _replay(conn, settings, actor, row, mailbox_id, revision, key, mac):
    summary = _summary(row[1])
    if summary['mailbox_ids'] != [mailbox_id] or summary['config_revision'] != revision:
        _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
    if (type(row[2]) is not str or not hmac.compare_digest(row[2], _digest(settings, actor, summary, key, mac))):
        _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
    task = _task(conn, summary['task_ids'][0])
    if (task['execution_scope'] != 'pool' or task['execution_mode'] != 'live'
            or task['created_by'] != actor.operator_id or task['batch_config_id'] != task['config_id']
            or task['platform'] != 'google' or task['platform_plan'] != ('google',)):
        _deny()
    events = conn.execute('SELECT actor_id,action,object_ref,outcome_code,correlation_id,before_summary,after_summary '
                         'FROM audit_events WHERE task_id=%s ORDER BY id', (task['id'],)).fetchall()
    if creation_details(conn, actor, task, events) != summary or events[0][4] != str(row[0]):
        _deny()
    return {**summary, 'receipt_id': str(row[0]), 'replayed': True}


def observe_request(settings, actor, mailbox_id, expected_config_revision, request_key, *, policy, mac):
    """Observe an exact committed creation receipt before doing admission work.

    No source read, provider request, lock claim or executor permission. A None
    result is a momentary observation, NOT proof a prior transaction rolled
    back. Concurrent/lost-ack requests must still use the original key through
    create_one, which independently serializes and revalidates the receipt.
    Historical lookup deliberately does not require the current config head,
    a live lease or an available mailbox: the original task can be finished.
    The returned internal summary is not an HTTP response DTO.
    """
    live_admission.inputs(settings, actor, mailbox_id, expected_config_revision, policy, mac)
    if type(request_key) is not str or not pool_batches._KEY.fullmatch(request_key):
        _deny(ErrorCode.INVALID_INPUT)
    policy._validate()
    probe = mac.request_digest('live.batch.keycheck.v1', actor.operator_id, b'stable')
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'tasks:manage')
        if conn.execute("SELECT 1 FROM operation_receipts WHERE scope_operator_id=%s AND action='pool.batch.create' "
                        'AND idempotency_key=%s', (actor.operator_id, request_key)).fetchone():
            _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
        row = conn.execute('SELECT id,result_summary,request_hash FROM operation_receipts WHERE task_id IS NULL '
            'AND scope_operator_id=%s AND action=%s AND idempotency_key=%s',
            (actor.operator_id, ACTION, request_key)).fetchone()
        result = None if row is None else _replay(conn, settings, actor, row, mailbox_id,
                                                 expected_config_revision, request_key, mac)
        policy._connection(conn)
        if not hmac.compare_digest(probe, mac.request_digest('live.batch.keycheck.v1', actor.operator_id, b'stable')):
            _deny(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn, actor, 'tasks:manage')
    return result


def create_one(settings, actor, mailbox_id, expected_config_revision, request_key, *, policy, mac, admission):
    live_admission.inputs(settings, actor, mailbox_id, expected_config_revision, policy, mac)
    if type(request_key) is not str or not pool_batches._KEY.fullmatch(request_key):
        _deny(ErrorCode.INVALID_INPUT)
    policy._validate()
    probe = mac.request_digest('live.batch.keycheck.v1', actor.operator_id, b'stable')
    with storage.unit_of_work(settings) as conn, ExitStack() as source_locks:
        try:
            policy._connection(conn)
            security.revalidate(conn, actor, 'tasks:manage')
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s),428711)',
                         (settings.schema + ':' + ACTION + ':' + actor.operator_id + ':' + request_key,))
            security.revalidate(conn, actor, 'tasks:manage')
            if conn.execute("SELECT 1 FROM operation_receipts WHERE scope_operator_id=%s AND action='pool.batch.create' "
                            'AND idempotency_key=%s', (actor.operator_id, request_key)).fetchone():
                _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
            row = conn.execute('SELECT id,result_summary,request_hash FROM operation_receipts WHERE task_id IS NULL '
                'AND scope_operator_id=%s AND action=%s AND idempotency_key=%s',
                (actor.operator_id, ACTION, request_key)).fetchone()
            if row is not None:
                result = _replay(conn, settings, actor, row, mailbox_id, expected_config_revision, request_key, mac)
            else:
                pins = live_admission.check(admission, settings, actor, mailbox_id, expected_config_revision)
                pool_config._guard_locked(conn, shared=True)
                # Same order as import/replay: authenticated session -> source
                # ownership lock -> mailbox row. PG cannot detect an OS/row
                # lock cycle, so never acquire this after the mailbox lock.
                boundary = source_locks.enter_context(management_lock(admission._source.data_root))
                boundary.require_owner(admission._source.email, actor.operator_id)
                conn.execute('SELECT id FROM mailbox_registry WHERE id=%s FOR UPDATE NOWAIT', (mailbox_id,)).fetchone()
                conn.execute('SELECT id FROM mailbox_platform_states WHERE id=%s FOR UPDATE', (pins['state_id'],)).fetchone()
                before = pool_batches._lease(pool_leases._read_lease(conn, mailbox_id, lock=True), mailbox_id, free=True)
                if before is None:
                    conn.execute("INSERT INTO resource_leases(resource_kind,resource_id) VALUES('mailbox',%s)", (mailbox_id,))
                    before = pool_batches._lease(pool_leases._read_lease(conn, mailbox_id, lock=True), mailbox_id, free=True)
                if before['version'] == pool_batches._MAX or before['fence'] == pool_batches._MAX:
                    _deny(ErrorCode.VERSION_CONFLICT)
                pool_leases._read_secret(conn, pins['mailbox_credential_ref'], lock=True)
                current = live_admission.inspect(conn, settings, actor, admission._source, mailbox_id,
                                                expected_config_revision, policy, mac)
                if not pool_cancellation._same(current, pins):
                    _deny(ErrorCode.VERSION_CONFLICT)
                live_admission.consume(admission, settings, actor, mailbox_id, expected_config_revision)
                batch, task, receipt = str(uuid4()), str(uuid4()), str(uuid4())
                summary = dict(schema_version=1, execution_mode='live', workflow=pins['fields']['workflow'],
                    batch_id=batch, task_ids=[task], mailbox_ids=[mailbox_id], config_id=pins['config_id'],
                    config_revision=expected_config_revision, selection='specified', requested_count=1,
                    **{k: pins[k] for k in ('mailbox_credential_ref', 'credential_version', 'state_id',
                        'source_digest', 'source_fingerprint', 'original_usage_status', 'admission')})
                digest = _digest(settings, actor, summary, request_key, mac)
                pin = dict(state_id=pins['state_id'], secret_ref=pins['mailbox_credential_ref'],
                           revision=pins['credential_version'], identity_status='UNKNOWN')
                conn.execute('INSERT INTO onboarding_batches(id,selection_mode,requested_count,selected_mailbox_refs,'
                    "config_id,created_by) VALUES(%s,'specified',1,%s,%s,%s)",
                    (batch, Jsonb([mailbox_id]), pins['config_id'], actor.operator_id))
                conn.execute('INSERT INTO onboarding_tasks(id,batch_id,config_id,execution_scope,execution_mode,'
                    'mailbox_id,mailbox_ref,platform,platform_plan,credential_version,mailbox_credential_ref,'
                    "platform_credential_pins,status,generation,version) VALUES(%s,%s,%s,'pool','live',%s,%s,'google',"
                    "%s,%s,%s,%s,'QUEUED',1,2)", (task, batch, pins['config_id'], mailbox_id, mailbox_id,
                    Jsonb(['google']), pins['credential_version'], pins['mailbox_credential_ref'], Jsonb({'google': pin})))
                changed = conn.execute('UPDATE resource_leases SET task_id=%s,owner_id=%s,fence=fence+1,version=version+1,'
                    "lease_until=clock_timestamp()+interval '30 seconds',hold_reason='HELD',stopped_evidence_ref=NULL,"
                    "updated_at=clock_timestamp() WHERE resource_kind='mailbox' AND resource_id=%s AND version=%s "
                    'AND fence=%s AND task_id IS NULL AND owner_id IS NULL AND hold_reason IS NULL AND lease_until IS NULL '
                    'RETURNING ' + pool_leases._LEASE_COLUMNS, (task, 'batch:' + batch, mailbox_id, before['version'], before['fence'])).fetchone()
                if changed is None:
                    _deny(ErrorCode.RESOURCE_HELD)
                after = pool_batches._lease(changed, mailbox_id)
                changed = conn.execute("UPDATE mailbox_platform_states SET credential_ref=%s,credential_version=%s,"
                    "usage_status='RESERVED',last_task_id=%s,version=version+1,updated_at=clock_timestamp() "
                    "WHERE id=%s AND version=%s AND usage_status='HISTORY_UNRECONCILED' AND last_task_id IS NULL RETURNING id",
                    (pins['mailbox_credential_ref'], pins['credential_version'], task, pins['state_id'], pins['state_version'])).fetchone()
                if changed != (UUID(pins['state_id']),):
                    _deny(ErrorCode.VERSION_CONFLICT)
                audit.append(conn, actor.operator_id, task, 'task.create', task, 'CREATED', receipt,
                             after_summary={'status': 'QUEUED', 'version': 2})
                audit.append(conn, actor.operator_id, task, 'lease.claim', mailbox_id, 'OK', receipt,
                             before_summary=pool_leases._summary(before), after_summary=pool_leases._summary(after))
                conn.execute('INSERT INTO operation_receipts(id,task_id,scope_operator_id,action,resource_revision,'
                    'generation,fence,phase,idempotency_key,request_hash,result_code,external_ref,result_summary) '
                    "VALUES(%s,NULL,%s,%s,%s,1,0,'SUCCEEDED',%s,%s,'LIVE_BATCH_ACCEPTED',NULL,%s)",
                    (receipt, actor.operator_id, ACTION, 'pool-live-admin:' + actor.operator_id, request_key, digest, Jsonb(summary)))
                # Source and MAC rotations during the writes cannot yield success.
                snap = admission._source.snapshot(actor, mac)
                if snap.source_digest != pins['source_digest']:
                    _deny(ErrorCode.VERSION_CONFLICT)
                result = {**summary, 'receipt_id': receipt, 'replayed': False}
            policy._connection(conn)
            if not hmac.compare_digest(probe, mac.request_digest('live.batch.keycheck.v1', actor.operator_id, b'stable')):
                _deny(ErrorCode.SECRET_UNAVAILABLE)
            secret = None if result['replayed'] else pool_leases._read_secret(conn, pins['mailbox_credential_ref'])
            _, session = security._authenticate(conn, actor.operator_id, actor.session_id,
                                                'tasks:manage', snapshot_epoch=actor.auth_epoch)
            now = pool_batches._clock(conn)  # LAST SQL, deadlines share one DB instant.
            if any(pool_config._timestamp(session[name]) <= now for name in ('expires_at', 'idle_expires_at')):
                _deny(ErrorCode.UNAUTHENTICATED)
            if not result['replayed']:
                if time.monotonic() >= admission._deadline:
                    _deny(ErrorCode.FORBIDDEN)
                if after['lease_until'] <= now:
                    _deny(ErrorCode.RESOURCE_HELD)
                pool_batches._secret(secret,
                    pins['mailbox_credential_ref'], 'mailbox_credential',
                    f'pool-live-v1:{actor.operator_id}:mailbox:{mailbox_id}:v1', now)
        except MailboxBoundaryError:
            _deny(ErrorCode.RESOURCE_HELD)
        except psycopg.Error as exc:
            if exc.sqlstate == '55P03':
                _deny(ErrorCode.RESOURCE_HELD)
            raise
    return result
