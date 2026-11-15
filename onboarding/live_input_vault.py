"""Owned original-pool mailbox import; no registration, qualification or dispatch.

Reuses the registry, versioned encrypted secret table and operator authentication.
The source is trusted local composition, never an HTTP-supplied path. Live inputs
have a distinct payload/policy namespace; existing fixture contracts stay strict.
No plaintext getter or callback consumer is provided by this write-side service.
"""
from dataclasses import dataclass, fields
import hmac
from contextlib import contextmanager
import json
import re
import secrets
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import audit, migration_catalog, security, storage
from .errors import ErrorCode, ServiceError
from .keyring import Keyring
from .pool_secret_types import PoolResource, SecretRef
from .pool_vault import PoolVault, SyntheticPoolPolicy
from .request_mac import RequestMac
from .secret_store import aad_for
from .settings import Settings, validate_settings

_PLATFORMS = ('google','claude','chatgpt','grok','kiro','github','k12')
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')


def _deny(code=ErrorCode.INVALID_INPUT):
    raise ServiceError(code)


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            _deny()
    except (ValueError, TypeError, AttributeError):
        _deny()
    return value


def _text(value, maximum=16384):
    try:
        if (type(value) is not str or len(value.encode('utf-8')) > maximum
                or any(unicodedata.category(c) in ('Cc','Cf') for c in value)):
            _deny()
    except UnicodeError:
        _deny()


@dataclass(frozen=True, repr=False)
class LiveMailboxCredential:
    email_norm: str
    password: str
    refresh_token: str = ''
    client_id: str = ''
    provider: str = 'other'
    mail_api_url: str = ''
    mail_api_key: str = ''
    two_factor: str = ''

    def __post_init__(self):
        _encode_mailbox(self)

    def __repr__(self):
        return '<LiveMailboxCredential>'


def _encode_mailbox(payload):
    if (type(payload) is not LiveMailboxCredential
            or set(vars(payload)) != {f.name for f in fields(LiveMailboxCredential)}):
        _deny()
    for item in vars(payload).values():
        _text(item)
        if item.startswith('fixture:'):
            _deny(ErrorCode.FORBIDDEN)
    email = payload.email_norm
    if (len(email) > 320 or email != email.lower()
            or re.fullmatch(r'[a-z0-9][a-z0-9._+%\-]{0,63}@[a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?\.[a-z]{2,63}',email) is None
            or email.endswith(('.invalid','.test','.example')) or not payload.password):
        _deny()
    from common.account_records import is_icloud_email, is_outlook_email
    expected = 'icloud' if is_icloud_email(email) else 'outlook' if is_outlook_email(email) else 'other'
    if payload.provider != expected:
        _deny()
    if payload.mail_api_url:
        try:
            url = urlsplit(payload.mail_api_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
                _deny()
            url.port  # Validate malformed/out-of-range port without performing I/O.
        except ValueError:
            _deny()
    clear = json.dumps(dict(schema_version=1, **vars(payload)),sort_keys=True,
        separators=(',', ':'),ensure_ascii=True,allow_nan=False).encode()
    # Preview and persistence use one admission contract, including the MAC's
    # canonical byte limit (Unicode escaping can exceed the raw field lengths).
    if len(clear) > 65536:
        _deny()
    return clear


@dataclass(frozen=True, repr=False)
class LivePoolPolicy:
    settings: Settings

    def __post_init__(self):
        self._validate()

    @classmethod
    def from_settings(cls, settings):
        return cls(settings)

    def __repr__(self):
        return '<LivePoolPolicy>'

    def _validate(self):
        validate_settings(getattr(self,'settings',None))
        if self.settings.schema != 'rf_onboarding':
            # Disposable schemas still require their existing owner manifest.
            SyntheticPoolPolicy(self.settings)._validate()

    def _connection(self, conn):
        # Reuse strict socket/catalog/search_path checks, not synthetic admission.
        # That routine calls this policy's _validate and never inspects payloads.
        SyntheticPoolPolicy._connection(self,conn)
        versions=conn.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
        migration_catalog.validate_applied(versions,minimum=4)


class LiveInputVault:
    __slots__ = ('_keyring','_policy')

    def __init__(self,keyring,policy):
        if type(keyring) is not Keyring or type(policy) is not LivePoolPolicy:
            _deny()
        policy._validate()
        self._keyring,self._policy=keyring,policy
        self._keys()

    def __repr__(self):
        return '<LiveInputVault>'

    def _keys(self):
        # Same descriptor-checked AES keys; distinct request-MAC key required.
        return PoolVault._keys(self)

    def put_mailbox_locked(self,conn,actor,mailbox_id,payload):
        clear=_encode_mailbox(payload)
        resource=PoolResource('mailbox',_uuid(mailbox_id))
        self._policy._connection(conn)
        security.revalidate(conn,actor,'mailboxes:manage')
        PoolVault._resource(self,conn,actor,resource,payload)
        security.revalidate(conn,actor,'mailboxes:manage')
        key,version=self._keys(),self._keyring.active_version
        identity,nonce=str(uuid4()),secrets.token_bytes(12)
        ciphertext=AESGCM(key).encrypt(nonce,clear,aad_for(self._policy.settings.schema,identity,'mailbox_credential',1))
        access=f'pool-live-v1:{actor.operator_id}:mailbox:{mailbox_id}:v1'
        conn.execute('INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) '
            "VALUES(%s,'mailbox_credential',%s,%s,%s,%s)",(identity,version,nonce,ciphertext,access))
        audit.append(conn,actor.operator_id,None,'secret.put',identity,'CREATED',identity,after_summary={'version':1})
        self._policy._connection(conn)
        PoolVault._resource(self,conn,actor,resource,payload)
        if self._keyring.active_version!=version or not hmac.compare_digest(key,self._keys()):
            _deny(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn,actor,'mailboxes:manage')
        return SecretRef(identity,1)


def _dependencies(settings,actor,source,group_ref,vault,mac):
    from .live_mailbox_source import OwnedMailboxSource
    if (type(settings) is not Settings or type(vault) is not LiveInputVault
            or type(vault._policy) is not LivePoolPolicy or vault._policy.settings!=settings
            or type(mac) is not RequestMac or mac.directory!=vault._keyring.directory
            or type(source) is not OwnedMailboxSource):
        _deny()
    if type(actor) is not security.Actor:
        _deny(ErrorCode.UNAUTHENTICATED)
    _text(group_ref,128)
    vault._policy._validate()
    return vault._keyring.active_version,vault._keys(),mac.request_digest('live.mailbox.keycheck.v1',actor.operator_id,b'keycheck')


def _authorize(conn,actor,vault):
    vault._policy._connection(conn)
    security.revalidate(conn,actor,'mailboxes:manage')


def _tail(conn,actor,source,snapshot,vault,mac,keys):
    source.validate(snapshot,actor,mac)
    if (keys[0]!=vault._keyring.active_version or not hmac.compare_digest(keys[1],vault._keys())
            or not hmac.compare_digest(keys[2],mac.request_digest('live.mailbox.keycheck.v1',actor.operator_id,b'keycheck'))):
        _deny(ErrorCode.SECRET_UNAVAILABLE)
    _authorize(conn,actor,vault)


@contextmanager
def _managed_source(conn, actor, source, vault):
    # Authentication precedes any control-file creation. This one lock is also
    # held by every participating legacy credential-delivery path.
    from common.managed_mailboxes import management_lock, MailboxBoundaryError
    _authorize(conn, actor, vault)
    try:
        with management_lock(source.data_root) as boundary:
            yield boundary
    except MailboxBoundaryError:
        _deny(ErrorCode.RESOURCE_HELD)


def preview_source(settings,actor,source,group_ref,*,vault,mac):
    keys=_dependencies(settings,actor,source,group_ref,vault,mac)
    with storage.unit_of_work(settings) as conn, _managed_source(conn,actor,source,vault):
        _authorize(conn,actor,vault)
        snapshot=source.snapshot(actor,mac)
        result=dict(email=snapshot.payload.email_norm,provider=snapshot.payload.provider,
                    group_ref=group_ref,source_digest=snapshot.source_digest)
        _tail(conn,actor,source,snapshot,vault,mac,keys)
    return result


def _existing(conn,actor,payload,fingerprint):
    row=conn.execute('SELECT id,owner_operator_id,credential_ref,credential_version,source_fingerprint '
        'FROM mailbox_registry WHERE email_norm=%s FOR UPDATE',(payload.email_norm,)).fetchone()
    if row is None:
        return None
    if str(row[1])!=actor.operator_id:
        _deny(ErrorCode.FORBIDDEN)
    secret=conn.execute('SELECT kind,access_policy,version,revoked_at,expires_at,revision FROM secret_objects WHERE id=%s FOR SHARE',
                        (row[2],)).fetchone()
    if (secret is None or secret[0]!='mailbox_credential'
            or secret[1]!=f'pool-live-v1:{actor.operator_id}:mailbox:{row[0]}:v1'):
        _deny(ErrorCode.FORBIDDEN)
    if (type(row[4]) is not str or not _DIGEST.fullmatch(row[4])
            or not hmac.compare_digest(row[4],fingerprint) or row[3]!=1 or secret[2]!=1 or secret[5]!=1
            or secret[3] is not None or secret[4] is not None):
        _deny(ErrorCode.VERSION_CONFLICT)
    return str(row[0])


def _receipt(conn,actor,key,digest):
    row=conn.execute("SELECT request_hash,result_summary,phase,resource_revision,result_code,generation,fence,external_ref FROM operation_receipts "
        "WHERE task_id IS NULL AND scope_operator_id=%s AND action='live.mailbox.import' AND idempotency_key=%s",
        (actor.operator_id,key)).fetchone()
    if row is None:
        return None
    if type(row[0]) is not str or not _DIGEST.fullmatch(row[0]):
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if not hmac.compare_digest(row[0],digest):
        _deny(ErrorCode.IDEMPOTENCY_CONFLICT)
    value=row[1]
    if (type(value) is not dict or set(value)!={'mailbox_id','created','request_key'}
            or type(value['created']) is not bool or value['request_key']!=key
            or row[2:]!=('SUCCEEDED','pool-live-admin:'+actor.operator_id,'IMPORTED',1,0,None)):
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    try:
        _uuid(value['mailbox_id'])
    except ServiceError:
        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return dict(value)


def import_source(settings,actor,source,group_ref,expected_source_digest,request_key,*,vault,mac):
    keys=_dependencies(settings,actor,source,group_ref,vault,mac)
    if (type(expected_source_digest) is not str or not _DIGEST.fullmatch(expected_source_digest)
            or type(request_key) is not str or not _KEY.fullmatch(request_key)):
        _deny()
    with storage.unit_of_work(settings) as conn, _managed_source(conn,actor,source,vault) as boundary:
        _authorize(conn,actor,vault)
        # Two live imports with the same owner/key share one committed result,
        # including across independent login sessions. This never locks a raw
        # email/password and does not replace the global email uniqueness check.
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s),428710)',
            (settings.schema+':live.mailbox.import:'+actor.operator_id+':'+request_key,))
        _authorize(conn,actor,vault)
        snapshot=source.snapshot(actor,mac)
        if not hmac.compare_digest(snapshot.source_digest,expected_source_digest):
            _deny(ErrorCode.VERSION_CONFLICT)
        # Durable one-way ownership transfer BEFORE candidate DB writes. A
        # rollback or unknown commit intentionally does not free it for legacy
        # consumers; the same owner can reconcile/replay the import.
        boundary.register(source.email, actor.operator_id)
        payload=snapshot.payload
        fingerprint=mac.request_digest('live.mailbox.credential.v1',actor.operator_id,_encode_mailbox(payload))
        body=json.dumps(dict(v=1,schema=settings.schema,instance=settings.instance_marker,group=group_ref,
            source=expected_source_digest,credential=fingerprint),sort_keys=True,separators=(',',':')).encode()
        digest=mac.request_digest('live.mailbox.import.v1',actor.operator_id,body)
        result=_receipt(conn,actor,request_key,digest)
        identity=_existing(conn,actor,payload,fingerprint)
        if result is not None:
            if result['mailbox_id']!=identity:
                _deny(ErrorCode.RECONCILIATION_REQUIRED)
        else:
            created=identity is None
            if created:
                try:
                    # Different keys/sessions may race the global email index.
                    # Keep candidate ciphertext, audit and registry in one
                    # savepoint, and recover ONLY this known unique constraint.
                    with conn.transaction():
                        identity=_insert_mailbox(conn,actor,vault,payload,group_ref,fingerprint)
                except UniqueViolation as error:
                    if error.diag.constraint_name!='mailbox_registry_email_norm_key':
                        raise
                    identity=_existing(conn,actor,payload,fingerprint)
                    if identity is None:
                        _deny(ErrorCode.DEPENDENCY_UNAVAILABLE)
                    created=False
            result=dict(mailbox_id=identity,created=created,request_key=request_key)
            receipt=str(uuid4())
            conn.execute('INSERT INTO operation_receipts(id,task_id,scope_operator_id,action,resource_revision,'
                'idempotency_key,request_hash,phase,fence,generation,result_code,result_summary) '
                "VALUES(%s,NULL,%s,'live.mailbox.import',%s,%s,%s,'SUCCEEDED',0,1,'IMPORTED',%s)",
                (receipt,actor.operator_id,'pool-live-admin:'+actor.operator_id,request_key,digest,Jsonb(result)))
            audit.append(conn,actor.operator_id,None,'mailbox.import',identity,'IMPORTED',receipt,after_summary={'version':1})
        _tail(conn,actor,source,snapshot,vault,mac,keys)
    return result


def _insert_mailbox(conn,actor,vault,payload,group_ref,fingerprint):
    identity=str(uuid4())
    ref=vault.put_mailbox_locked(conn,actor,identity,payload)
    conn.execute('INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,group_ref,'
        'credential_ref,source_fingerprint) VALUES(%s,%s,%s,%s,%s,%s,%s)',
        (identity,actor.operator_id,payload.email_norm,payload.provider,group_ref,ref.id,fingerprint))
    for platform in _PLATFORMS:
        conn.execute('INSERT INTO mailbox_platform_states(id,mailbox_id,platform,usage_status) '
            "VALUES(%s,%s,%s,'HISTORY_UNRECONCILED')",(str(uuid4()),identity,platform))
    return identity
