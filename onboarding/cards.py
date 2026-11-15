"""Atomic synthetic card imports; no payments, secret reads, or HTTP surface."""
from dataclasses import dataclass
import hmac
import json
import re
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from . import audit, card_import, security, storage
from .errors import ErrorCode, ServiceError
from .pool_secret_types import PoolResource, _encode_payload
from .pool_vault import PoolVault
from .request_mac import RequestMac
from .settings import Settings

_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')


@dataclass(frozen=True)
class CardImportResult:
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


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _stable_key(mac, actor, probe):
    current = mac.request_digest('card.keycheck.v1', actor.operator_id, b'fixture:stable-key')
    if not hmac.compare_digest(probe, current):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _prepare(settings, actor, csv_text, mac):
    # Always parse the original CSV: ParsedCard/preview objects are not input APIs.
    parsed = card_import.parse_for_import(csv_text)
    probe = mac.request_digest('card.keycheck.v1', actor.operator_id, b'fixture:stable-key')
    records = sorted(((mac.pan_fingerprint(record.pan), record) for record in parsed.records),
                     key=lambda item: item[0])
    payload_macs = []
    for fingerprint, record in records:
        # Validate exact synthetic payloads again, including their full shape;
        # ordinary hashes of secrets or PANs are never used here.
        payload = {name: json.loads(_encode_payload(getattr(record, name))[3])
                   for name in ('pan', 'expiry', 'holder', 'address')}
        payload.update(alias=record.alias, brand=record.brand, last4=record.last4,
                       account_limit=record.account_limit, sms_channel_ref=record.sms_channel_ref)
        payload_macs.append(mac.request_digest('card.payload.v1', actor.operator_id, _json(payload)))
    digest = mac.request_digest('card.import.v1', actor.operator_id,
        _json({'schema': settings.schema, 'action': 'card.import', 'version': 1,
               'mode': 'skip', 'payload_macs': payload_macs}))
    _stable_key(mac, actor, probe)
    return parsed, records, digest, probe


def _authorize(conn, actor, vault):
    vault._policy._connection(conn)
    security.revalidate(conn, actor, 'cards:manage')


def _finish(conn, actor, vault, mac, probe, encryption):
    _authorize(conn, actor, vault)
    if (vault._keyring.active_version != encryption[0]
            or not hmac.compare_digest(vault._keys(), encryption[1])):
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
    _stable_key(mac, actor, probe)
    security.revalidate(conn, actor, 'cards:manage')


def preview_import(settings, actor, csv_text, *, vault, mac):
    encryption = _dependencies(settings, actor, vault, mac)
    parsed, records, digest, probe = _prepare(settings, actor, csv_text, mac)
    with storage.unit_of_work(settings) as conn:
        _authorize(conn, actor, vault)
        # Use the parser's exact allowlisted six-field display contract. Neither
        # plaintext typed payloads nor private PAN fingerprints escape this API.
        result = card_import.preview(csv_text)
        result['preview_digest'] = digest if result['can_import'] else None
        _finish(conn, actor, vault, mac, probe, encryption)
    return result


def _receipt(conn, actor, key, digest, count):
    row = conn.execute('SELECT request_hash,phase,result_summary,resource_revision,generation,fence '
        'FROM operation_receipts WHERE task_id IS NULL AND scope_operator_id=%s '
        "AND action='card.import' AND idempotency_key=%s", (actor.operator_id, key)).fetchone()
    if row is None:
        return None
    if type(row[0]) is not str or not _DIGEST.fullmatch(row[0]):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if not hmac.compare_digest(row[0], digest):
        raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
    value = row[2]
    valid = (row[1] == 'SUCCEEDED' and row[3:] == ('pool-admin:' + actor.operator_id, 1, 0)
             and type(value) is dict and set(value) == {'created_ids', 'skipped_ids', 'request_key'})
    if valid:
        created, skipped = value['created_ids'], value['skipped_ids']
        valid = (type(created) is list and type(skipped) is list
                 and len(created) + len(skipped) == count and 1 <= count <= 1000
                 and type(value['request_key']) is str and value['request_key'] == key)
    if valid:
        try:
            identifiers = created + skipped
            valid = (all(type(item) is str and str(UUID(item)) == item for item in identifiers)
                     and len(set(identifiers)) == len(identifiers))
        except (ValueError, TypeError, AttributeError):
            valid = False
    if not valid:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return CardImportResult(tuple(created), tuple(skipped), key)


def _existing(conn, actor, fingerprint):
    row = conn.execute('SELECT id,owner_operator_id FROM payment_cards WHERE pan_fingerprint=%s FOR UPDATE',
                       (fingerprint,)).fetchone()
    if row is None:
        return None
    if str(row[1]) != actor.operator_id:
        raise ServiceError(ErrorCode.FORBIDDEN)
    return str(row[0])


def _insert_card(conn, actor, vault, record, fingerprint):
    identity, billing_id = str(uuid4()), str(uuid4())
    card_resource, billing_resource = PoolResource('card', identity), PoolResource('billing', billing_id)
    pan = vault.put_locked(conn, actor, card_resource, record.pan)
    expiry = vault.put_locked(conn, actor, card_resource, record.expiry)
    holder = vault.put_locked(conn, actor, billing_resource, record.holder)
    address = vault.put_locked(conn, actor, billing_resource, record.address)
    # Billing identities are append-only; never SELECT them FOR UPDATE.
    conn.execute('INSERT INTO billing_identities '
        '(id,owner_operator_id,holder_secret_ref,address_secret_ref,country,validation_status) '
        "VALUES(%s,%s,%s,%s,%s,'UNVERIFIED')",
        (billing_id, actor.operator_id, holder.id, address.id, record.address.country))
    conn.execute('INSERT INTO payment_cards '
        '(id,owner_operator_id,alias,brand,last4,expiry_ref,pan_secret_ref,billing_identity_ref,'
        'pan_fingerprint,account_limit,sms_channel_ref) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (identity, actor.operator_id, record.alias, record.brand, record.last4, expiry.id,
         pan.id, billing_id, fingerprint, record.account_limit, record.sms_channel_ref))
    return identity


def import_csv(settings, actor, csv_text, preview_digest, request_key, *, vault, mac):
    encryption = _dependencies(settings, actor, vault, mac)
    if (type(request_key) is not str or not _KEY.fullmatch(request_key)
            or type(preview_digest) is not str or not _DIGEST.fullmatch(preview_digest)):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    parsed, records, digest, probe = _prepare(settings, actor, csv_text, mac)
    if not records or any(issue.code != 'DUPLICATE_CARD' for issue in parsed.issues):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    with storage.unit_of_work(settings) as conn:
        _authorize(conn, actor, vault)
        # Serialize this service per owner without upgrading operators' SHARE
        # locks. Independent sessions retain their own session-row locks.
        conn.execute('SELECT pg_advisory_xact_lock(428703, pg_catalog.hashtext(%s))', (actor.operator_id,))
        _authorize(conn, actor, vault)
        if not hmac.compare_digest(preview_digest, digest):
            raise ServiceError(ErrorCode.IDEMPOTENCY_CONFLICT)
        result = _receipt(conn, actor, request_key, digest, len(records))
        if result is None:
            created, skipped = [], []
            for fingerprint, record in records:
                existing = _existing(conn, actor, fingerprint)
                if existing is not None:
                    skipped.append(existing)
                    continue
                try:
                    # A global PAN race can happen across owners. Its failed
                    # candidate's four secrets, billing and audits roll back
                    # together before the winner's ownership is checked.
                    with conn.transaction():
                        identity = _insert_card(conn, actor, vault, record, fingerprint)
                    created.append(identity)
                except UniqueViolation as error:
                    if error.diag.constraint_name != 'payment_cards_pan_fingerprint_key':
                        raise
                    existing = _existing(conn, actor, fingerprint)
                    if existing is None:
                        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
                    skipped.append(existing)
            summary = {'created_ids': created, 'skipped_ids': skipped, 'request_key': request_key}
            receipt_id = str(uuid4())
            conn.execute('INSERT INTO operation_receipts '
                '(id,task_id,scope_operator_id,action,resource_revision,idempotency_key,request_hash,'
                'phase,fence,generation,result_summary) '
                "VALUES(%s,NULL,%s,'card.import',%s,%s,%s,'SUCCEEDED',0,1,%s)",
                (receipt_id, actor.operator_id, 'pool-admin:' + actor.operator_id,
                 request_key, digest, Jsonb(summary)))
            audit.append(conn, actor.operator_id, None, 'card.import', receipt_id, 'SUCCEEDED', receipt_id,
                         after_summary={'version': 1})
            result = CardImportResult(tuple(created), tuple(skipped), request_key)
        _finish(conn, actor, vault, mac, probe, encryption)
    # A COMMIT acknowledgement failure is unknown. Never retry or publish a
    # result from inside the unit of work; only an explicit original-key replay.
    return result
