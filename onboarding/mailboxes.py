"""Atomic synthetic mailbox import. No HTTP, secret read or platform inference."""
from dataclasses import dataclass
import hmac
import json
import re
import unicodedata
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import audit, legacy_mailboxes, security, storage
from .errors import ErrorCode, ServiceError
from .pool_secret_types import MailboxCredential, PoolResource, _encode_payload
from .pool_vault import PoolVault
from .request_mac import RequestMac
from .settings import Settings

_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')
_PLATFORMS = ('google','claude','chatgpt','grok','kiro','github','k12')


@dataclass(frozen=True)
class MailboxImportResult:
    created_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    request_key: str


def _dependencies(settings, actor, vault, mac):
    if (type(settings) is not Settings or type(vault) is not PoolVault
            or type(mac) is not RequestMac or settings != vault._policy.settings
            or mac.directory != vault._keyring.directory):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    vault._policy._validate()
    return vault._keyring.active_version, vault._keys()


def _prepare(text, group_ref, actor, mac):
    try:
        if (type(group_ref) is not str or len(group_ref)>128
                or any(unicodedata.category(char) in ('Cc','Cf') for char in group_ref)):
            raise ValueError
        group_ref.encode('utf-8')
    except (ValueError,UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    parsed = legacy_mailboxes.parse_for_import(text)
    issues = [{'line':issue.line,'code':issue.code} for issue in parsed.issues]
    records = []
    for record in parsed.records:
        if record.account_password:
            issues.append({'line':record.line,'code':'PLATFORM_BINDING_REQUIRED'})
        else:
            payload = MailboxCredential(record.email_norm, record.password, record.refresh_token,
                record.client_id, record.provider, record.mail_api_url, record.mail_api_key, record.two_factor)
            records.append((record,payload))
    records.sort(key=lambda item:item[0].email_norm)
    probe = mac.request_digest('mailbox.keycheck.v1',actor.operator_id,b'fixture:stable-key')
    fingerprints=[]
    for record,payload in records:
        clear=_encode_payload(payload)[3]
        fingerprints.append(mac.request_digest('mailbox.credential.v1',actor.operator_id,clear))
    header=json.dumps({'group_ref':group_ref,'version':1,'mode':'skip'},ensure_ascii=False,
                      sort_keys=True,separators=(',',':')).encode('utf-8')
    batch=(b'rf-mailbox-import:v1'+len(header).to_bytes(4,'big')+header
           +len(records).to_bytes(4,'big')+b''.join(bytes.fromhex(value) for value in fingerprints))
    digest=mac.request_digest('mailbox.import.v1',actor.operator_id,batch)
    _stable_key(mac,actor,probe)
    return parsed,issues,tuple(zip(records,fingerprints)),digest,probe


def _stable_key(mac,actor,probe):
    if not hmac.compare_digest(probe,mac.request_digest('mailbox.keycheck.v1',actor.operator_id,b'fixture:stable-key')):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _authorize(conn,actor,vault):
    vault._policy._connection(conn)
    security.revalidate(conn,actor,'mailboxes:manage')


def _finish(conn,actor,vault,mac,probe,encryption):
    _authorize(conn,actor,vault)
    if (vault._keyring.active_version != encryption[0]
            or not hmac.compare_digest(vault._keys(),encryption[1])):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
    _stable_key(mac,actor,probe)
    security.revalidate(conn,actor,'mailboxes:manage')


def preview_import(settings,actor,text,group_ref,*,vault,mac):
    encryption=_dependencies(settings,actor,vault,mac)
    parsed,issues,records,digest,probe=_prepare(text,group_ref,actor,mac)
    with storage.unit_of_work(settings) as conn:
        _authorize(conn,actor,vault)
        result={'items':[{'line':record.line,'email':record.email_norm,'provider':record.provider,
                          'group_ref':group_ref} for record in parsed.records],
                'issues':issues,'accepted_count':len(parsed.records),
                'duplicate_count':sum(issue['code']=='DUPLICATE_EMAIL' for issue in issues),
                'conflict_count':sum(issue['code']=='CONFLICTING_EMAIL' for issue in issues),
                'preview_digest':digest if records and not issues else None}
        _finish(conn,actor,vault,mac,probe,encryption)
    return result


def _receipt(conn,actor,key,digest,count):
    row=conn.execute('SELECT request_hash,phase,result_summary,resource_revision,generation,fence '
        'FROM operation_receipts WHERE task_id IS NULL AND scope_operator_id=%s '
        "AND action='mailbox.import' AND idempotency_key=%s",(actor.operator_id,key)).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row[0],digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    value=row[2]
    valid=(row[1]=='SUCCEEDED' and row[3:]==('pool-admin:'+actor.operator_id,1,0)
           and type(value) is dict and set(value)=={'created_ids','skipped_ids','request_key'})
    if valid:
        created,skipped=value['created_ids'],value['skipped_ids']
        valid=(type(created) is list and type(skipped) is list and len(created)+len(skipped)==count
               and 1<=count<=1000 and type(value['request_key']) is str and value['request_key']==key)
    if valid:
        try:
            identifiers=created+skipped
            valid=(len(set(identifiers))==len(identifiers)
                   and all(type(item) is str and str(UUID(item))==item for item in identifiers))
        except (ValueError,TypeError,AttributeError):
            valid=False
    if not valid:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return MailboxImportResult(tuple(created),tuple(skipped),key)


class _FingerprintConflict(Exception):
    """A well-formed stored fingerprint differs; import_text decides the proof."""


def _existing(conn,actor,email,fingerprint):
    row=conn.execute('SELECT id,owner_operator_id,source_fingerprint FROM mailbox_registry '
                     'WHERE email_norm=%s FOR UPDATE',(email,)).fetchone()
    if row is None:
        return None
    if str(row[1])!=actor.operator_id:
        raise ServiceError(ErrorCode.FORBIDDEN)
    if type(row[2]) is not str or not _DIGEST.fullmatch(row[2]):
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    if not hmac.compare_digest(row[2],fingerprint):
        raise _FingerprintConflict()
    return str(row[0])


def _insert_mailbox(conn,actor,vault,payload,group_ref,fingerprint):
    identity=str(uuid4())
    secret=vault.put_locked(conn,actor,PoolResource('mailbox',identity),payload)
    source='icloud' if payload.provider=='icloud' else 'outlook'
    conn.execute('INSERT INTO mailbox_registry(id,owner_operator_id,email_norm,source_type,group_ref,'
                 'credential_ref,source_fingerprint) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                 (identity,actor.operator_id,payload.email_norm,source,group_ref,secret.id,fingerprint))
    for platform in _PLATFORMS:
        conn.execute('INSERT INTO mailbox_platform_states(id,mailbox_id,platform,usage_status) '
                     "VALUES(%s,%s,%s,'HISTORY_UNRECONCILED')",(str(uuid4()),identity,platform))
    return identity


def import_text(settings,actor,text,group_ref,preview_digest,request_key,*,vault,mac):
    encryption=_dependencies(settings,actor,vault,mac)
    if (type(request_key) is not str or not _KEY.fullmatch(request_key)
            or type(preview_digest) is not str or not _DIGEST.fullmatch(preview_digest)):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    parsed,issues,records,digest,probe=_prepare(text,group_ref,actor,mac)
    if issues or not records:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    with storage.unit_of_work(settings) as conn:
        _authorize(conn,actor,vault)
        if not hmac.compare_digest(preview_digest,digest):
            raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
        result=_receipt(conn,actor,request_key,digest,len(records))
        if result is None:
            created,skipped=[],[]
            try:
                for (record,payload),fingerprint in records:
                    existing=_existing(conn,actor,record.email_norm,fingerprint)
                    if existing is not None:
                        skipped.append(existing)
                        continue
                    try:
                        # Only the global normalized-email race is recoverable. The
                        # candidate secret and its audit are in this same savepoint.
                        with conn.transaction():
                            identity=_insert_mailbox(conn,actor,vault,payload,group_ref,fingerprint)
                        created.append(identity)
                    except UniqueViolation as error:
                        if error.diag.constraint_name!='mailbox_registry_email_norm_key':
                            raise
                        existing=_existing(conn,actor,record.email_norm,fingerprint)
                        if existing is None:
                            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
                        skipped.append(existing)
            except _FingerprintConflict:
                # Spec position 5: the preview-digest chain makes a differing stored
                # fingerprint proof that another request owns this email; the receipt
                # re-read is defence in depth (records and receipt commit together).
                if _receipt(conn,actor,request_key,digest,len(records)) is not None:
                    raise ServiceError(ErrorCode.VERSION_CONFLICT) from None
                _finish(conn,actor,vault,mac,probe,encryption)
                raise ServiceError(ErrorCode.VERSION_CONFLICT,not_committed=True) from None
            candidate=MailboxImportResult(tuple(created),tuple(skipped),request_key)
            summary={'created_ids':created,'skipped_ids':skipped,'request_key':request_key}
            receipt_id=str(uuid4())
            inserted=conn.execute('INSERT INTO operation_receipts '
                '(id,task_id,scope_operator_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation,result_summary) '
                "VALUES(%s,NULL,%s,'mailbox.import',%s,%s,%s,'SUCCEEDED',0,1,%s) "
                'ON CONFLICT DO NOTHING RETURNING id',
                (receipt_id,actor.operator_id,'pool-admin:'+actor.operator_id,request_key,digest,Jsonb(summary))).fetchone()
            if inserted is None:
                # Different hash raises: the whole transaction, including any
                # candidate resources, is rolled back. Same hash uses original IDs.
                result=_receipt(conn,actor,request_key,digest,len(records))
                if result is None:
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
                if set(result.created_ids+result.skipped_ids)!=set(created+skipped) or created:
                    raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
            else:
                result=candidate
                audit.append(conn,actor.operator_id,None,'mailbox.import',receipt_id,'SUCCEEDED',receipt_id,
                             after_summary={'version':1})
        _finish(conn,actor,vault,mac,probe,encryption)
    # Never return from inside the transaction: COMMIT_UNKNOWN stays unknown.
    return result

# Explicit read facade; the import implementation above is unchanged.
from .mailbox_read import Page, list_page

# Explicit metadata facade; existing import/read implementation is unchanged.
from .mailbox_update import update
