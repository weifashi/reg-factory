"""Task-bound encrypted Google key sink and closed, commit-gated consumer.

Internal service, not a credential upload/download endpoint or a runtime-mode
switch. Uses the supplied validated local database/keyring; no key generation,
fallback, arbitrary callbacks or plaintext read API. The real worker must bind
its sink closure to the committed key-create receipt and supply a target-guarded
Sub2APIImporter. Existing synthetic stores and their restrictions are unchanged.
"""
from dataclasses import replace
import hmac
import json
import re
import secrets
from uuid import UUID, uuid5

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.rows import dict_row

from . import audit, live_effects, security, storage
from .async_runtime import settled_thread
from .errors import ErrorCode, ServiceError
from .google_cloud import GooglePlan
from .keyring import Keyring
from .settings import _read_private_file, validate_settings
from .sub2api_import import ACTION, APPROVED_BUILD_SHA256, ImportPlan, Sub2APIImporter, _credential
from .sub2api_probe import _json

KEY_ACTION = 'service_account.key.create'


def _deny():
    raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)


def _serialized(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _validate(context, source_receipt_id, plan):
    live_effects._validate(context)
    live_effects._uuid(source_receipt_id)
    if type(plan) is not GooglePlan:
        _deny()
    try:
        plan.__post_init__()
    except Exception:
        _deny()
    if plan.task_id != context.task_id or plan.owner_id != context.actor.operator_id:
        _deny()


def _identity(context, source_receipt_id, plan):
    identity = str(uuid5(UUID(source_receipt_id), 'reg-factory.service-account-json.v1'))
    policy = ':'.join(('live-key-v1', context.actor.operator_id, context.task_id,
                       str(context.generation), source_receipt_id, plan.effect_digest(KEY_ACTION)))
    return identity, policy


def _envelope(raw, external_ref, plan, source_receipt_id):
    try:
        if type(raw) is not bytes or len(raw) > 32768:
            raise ValueError()
        data = _json(raw)
        key_id = data.get('private_key_id')
        if type(key_id) is not str or re.fullmatch('[a-f0-9]{40}', key_id) is None:
            raise ValueError()
        credential = _credential(raw, ImportPlan(plan.task_id, plan.project_id, plan.service_account_id,
            plan.region, key_id, source_receipt_id, APPROVED_BUILD_SHA256))
        pattern = (re.escape('projects/' + plan.project_id + '/serviceAccounts/') +
                   '(?:' + re.escape(plan.sa_email) + '|[1-9][0-9]{0,29})' +
                   re.escape('/keys/' + key_id))
        if type(external_ref) is not str or re.fullmatch(pattern, external_ref) is None:
            raise ValueError()
        return dict(version=1, credential=credential, external_ref=external_ref)
    except Exception:
        raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None


class ServiceAccountKeyStore:
    __slots__ = ('_settings', '_keyring')

    def __init__(self, settings, keyring):
        validate_settings(settings, role='app')
        if type(keyring) is not Keyring:
            _deny()
        self._settings, self._keyring = settings, keyring
        self._key(keyring.active_version)

    def __repr__(self):
        return '<ServiceAccountKeyStore>'

    def _key(self, version):
        if version == 'request-mac':
            _deny()
        try:
            key = self._keyring.key(version)
            try:
                mac = _read_private_file(self._keyring.directory / 'request-mac.key')
            except FileNotFoundError:
                mac = None
            if mac is not None and (len(mac) != 32 or hmac.compare_digest(mac, key)):
                _deny()
            return key
        except (OSError, ValueError, TypeError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def _aad(self, identity, policy):
        return _serialized(['reg-factory.live-key.v1', self._settings.schema,
                            identity, 'service_account_json', 1, policy])

    def _source(self, conn, context, source_receipt_id, plan, *, consuming):
        _validate(context, source_receipt_id, plan)
        task = live_effects._task(conn, context)
        row = live_effects._row(conn, source_receipt_id)
        source_context = replace(context, step_key=KEY_ACTION, effect_digest=plan.effect_digest(KEY_ACTION))
        if (row is None or str(row['task_id']) != context.task_id
                or row['generation'] != context.generation or row['action'] != KEY_ACTION
                or row['step_key'] != KEY_ACTION or row['config_revision'] != context.config_revision
                or row['effect_digest'] != plan.effect_digest(KEY_ACTION)
                or row['request_hash'] != live_effects.request_hash(source_context, KEY_ACTION)
                or row['resource_revision'] != live_effects.resource_revision(context)
                or row['fence_bindings'] != live_effects._bindings(context)
                or row['phase'] not in (('SUCCEEDED',) if consuming else ('INTENT', 'UNKNOWN', 'SUCCEEDED'))):
            _deny()
        live_effects._current(conn, context, task, sending=consuming)
        return task, row

    def _row(self, conn, identity):
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute('SELECT * FROM secret_objects WHERE id=%s FOR UPDATE', (identity,)).fetchone()

    def _decode(self, conn, row, identity, policy, plan, source_receipt_id):
        now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
        if (row is None or str(row['id']) != identity or row['kind'] != 'service_account_json'
                or row['revision'] != 1 or row['access_policy'] != policy or row['revoked_at'] is not None
                or row['expires_at'] is not None and row['expires_at'] <= now):
            _deny()
        try:
            if (type(row['nonce']) is not bytes or len(row['nonce']) != 12
                    or type(row['ciphertext']) is not bytes or not 16 <= len(row['ciphertext']) <= 65536):
                raise ValueError()
            key = self._key(row['key_version'])
            clear = AESGCM(key).decrypt(row['nonce'], row['ciphertext'], self._aad(identity, policy))
            data = _json(clear)
            if (type(data) is not dict or set(data) != {'version', 'credential', 'external_ref'}
                    or type(data['version']) is not int or data['version'] != 1):
                raise ValueError()
            normalized = _envelope(_serialized(data['credential']), data['external_ref'], plan, source_receipt_id)
            if data != normalized:
                raise ValueError()
            return normalized
        except (InvalidTag, ValueError, TypeError, KeyError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def save(self, *, context, source_receipt_id, plan, raw, external_ref):
        """Persist a received key even after cancel, but only under current fence.

        Receipt-derived identity makes a COMMIT_UNKNOWN recoverable without
        generating another Google key. Returns only after confirmed DB commit.
        This method itself is not proof the Google key-creation step succeeded.
        """
        _validate(context, source_receipt_id, plan)
        if context.step_key != KEY_ACTION or context.effect_digest != plan.effect_digest(KEY_ACTION):
            _deny()
        envelope = _envelope(raw, external_ref, plan, source_receipt_id)
        identity, policy = _identity(context, source_receipt_id, plan)
        with storage.unit_of_work(self._settings) as conn:
            task, source = self._source(conn, context, source_receipt_id, plan, consuming=False)
            row = self._row(conn, identity)
            if row is not None:
                if self._decode(conn, row, identity, policy, plan, source_receipt_id) != envelope:
                    _deny()
            else:
                if source['phase'] == 'SUCCEEDED':
                    _deny()
                version = self._keyring.active_version
                key, nonce = self._key(version), secrets.token_bytes(12)
                cipher = AESGCM(key).encrypt(nonce, _serialized(envelope), self._aad(identity, policy))
                conn.execute('INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) '
                    "VALUES(%s,'service_account_json',%s,%s,%s,%s)", (identity, version, nonce, cipher, policy))
                audit.append(conn, context.actor.operator_id, context.task_id, 'secret.put', identity,
                             'CREATED', source_receipt_id, after_summary={'version': 1})
                if version != self._keyring.active_version or not hmac.compare_digest(key, self._key(version)):
                    _deny()
            self._source(conn, context, source_receipt_id, plan, consuming=False)
        return identity

    def _for_import(self, context, secret_id, source_receipt_id, plan, importer, permit):
        _validate(context, source_receipt_id, plan)
        identity, policy = _identity(context, source_receipt_id, plan)
        if (type(importer) is not Sub2APIImporter or type(permit) is not live_effects.PreparedEffect
                or context.step_key != ACTION or identity != secret_id
                or context.effect_digest != importer.plan.effect_digest()
                or permit.effect_digest != context.effect_digest or permit.receipt_id != importer.plan.receipt_id
                or importer.plan.task_id != context.task_id or importer.plan.project_id != plan.project_id
                or importer.plan.service_account_id != plan.service_account_id or importer.plan.location != plan.region):
            _deny()
        with storage.unit_of_work(self._settings) as conn:
            task, source = self._source(conn, context, source_receipt_id, plan, consuming=True)
            dispatch = live_effects._row(conn, permit.receipt_id)
            if (dispatch is None or dispatch['phase'] != 'INTENT'
                    or dispatch['request_hash'] != live_effects.request_hash(context, ACTION)
                    or dispatch['fence_bindings'] != live_effects._bindings(context)):
                _deny()
            row = self._row(conn, identity)
            data = self._decode(conn, row, identity, policy, plan, source_receipt_id)
            if data['credential']['private_key_id'] != importer.plan.key_id:
                _deny()
            audit.append(conn, context.actor.operator_id, context.task_id, 'secret.use', identity,
                         'AVAILABLE', permit.receipt_id, after_summary={'version': row['version']})
            self._source(conn, context, source_receipt_id, plan, consuming=True)
            # The audit may wait past key expiry/revocation. Validate the locked
            # row and current key file again immediately before leaving the UoW.
            data = self._decode(conn, self._row(conn, identity), identity, policy, plan, source_receipt_id)
            live_effects._current(conn, context, task, sending=True)
        return _serialized(data['credential'])

    async def import_disabled(self, *, context, secret_id, source_receipt_id, plan, importer, permit):
        """The only consumer: no raw bytes/HTTP download/caller callback returns."""
        clear = await settled_thread(self._for_import, context, secret_id,
                                        source_receipt_id, plan, importer, permit)
        async def disclosure_guard():
            # Login/compliance/group requests may have waited. Recheck secret
            # revocation, expiry, envelope/key files and the current task after
            # them, immediately before the key-bearing create request.
            current = await settled_thread(self._for_import, context, secret_id,
                                               source_receipt_id, plan, importer, permit)
            return hmac.compare_digest(clear, current)
        try:
            return await importer.import_disabled(clear, permit, disclosure_guard=disclosure_guard)
        finally:
            # Python cannot guarantee zeroisation of immutable copies; it only
            # drops this reference. Never log traceback locals or provider JSON.
            clear = None
