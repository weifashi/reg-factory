"""Write-only typed synthetic secrets. No decrypt, consumer, HTTP or real mode."""
from dataclasses import dataclass, field
import hmac
import json
import re
import secrets
from uuid import uuid4

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import ErrorCode, ServiceError
from .keyring import Keyring
from .pool_secret_types import PoolResource, SecretRef, _encode_payload
from .secret_store import aad_for
from .settings import BASE, Settings, _read_private_file, _unique_object, validate_settings
from . import audit, migration_catalog, repository, security

_TEST_SCHEMA = re.compile(r'rf_p1b_test_([a-f0-9]{32})\Z')


@dataclass(frozen=True, repr=False)
class SyntheticPoolPolicy:
    settings: Settings = field(repr=False)

    def __post_init__(self):
        self._validate()

    @classmethod
    def from_settings(cls, settings):
        return cls(settings)

    def __repr__(self):
        return '<SyntheticPoolPolicy>'

    def _validate(self):
        validate_settings(getattr(self, 'settings', None), role='app')
        match = _TEST_SCHEMA.fullmatch(self.settings.schema)
        if match is None:
            raise ServiceError(ErrorCode.FORBIDDEN)
        try:
            manifest = json.loads(_read_private_file(BASE / (self.settings.schema + '.json')),
                                  object_pairs_hook=_unique_object)
            if (type(manifest) is not dict or manifest != {
                    'instance_marker': self.settings.instance_marker, 'schema': self.settings.schema,
                    'schema_token': match[1], 'owner': 'rf_onboarding_migrator'}):
                raise ValueError
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ServiceError(ErrorCode.FORBIDDEN) from None

    def _connection(self, conn):
        self._validate()
        if (not isinstance(conn, psycopg.Connection) or conn.info.host != self.settings.host
                or conn.info.port != self.settings.port or conn.info.dbname != self.settings.dbname
                or conn.info.user != self.settings.user):
            raise ServiceError(ErrorCode.FORBIDDEN)
        repository.require_transaction(conn)
        # Fully qualified catalog expressions before any business-table access.
        row = conn.execute('SELECT pg_catalog.current_database(), current_user, '
                           'pg_catalog.current_schema(), pg_catalog.inet_server_addr(), '
                           "pg_catalog.current_setting('listen_addresses'), "
                           "pg_catalog.shobj_description(d.oid,'pg_database'), "
                           'pg_catalog.pg_get_userbyid(n.nspowner), '
                           "pg_catalog.current_setting('search_path'), pg_catalog.pg_my_temp_schema() "
                           'FROM pg_catalog.pg_database d JOIN pg_catalog.pg_namespace n ON n.nspname=%s '
                           'WHERE d.datname=pg_catalog.current_database()', (self.settings.schema,)).fetchone()
        if row is None or row[:7] != (self.settings.dbname, self.settings.user, self.settings.schema,
                                     None, '', self.settings.instance_marker, 'rf_onboarding_migrator'):
            raise ServiceError(ErrorCode.FORBIDDEN)
        # Reject hidden/missing prefix schemas as well as actual temporary ones.
        path = tuple(part.strip().removeprefix('"').removesuffix('"') for part in row[7].split(','))
        if path != (self.settings.schema, 'pg_catalog') or row[8] != 0:
            raise ServiceError(ErrorCode.FORBIDDEN)
        versions = conn.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
        migration_catalog.validate_applied(versions, minimum=2)


class PoolVault:
    __slots__ = ('_keyring', '_policy')

    def __init__(self, keyring, policy):
        if type(keyring) is not Keyring or type(policy) is not SyntheticPoolPolicy:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        if keyring.active_version == 'request-mac':
            raise ServiceError(ErrorCode.FORBIDDEN)
        policy._validate()
        self._keyring, self._policy = keyring, policy

    def __repr__(self):
        return '<PoolVault>'

    def _keys(self):
        if self._keyring.active_version == 'request-mac':
            raise ServiceError(ErrorCode.FORBIDDEN)
        self._keyring.__post_init__()
        try:
            key = self._keyring.key(self._keyring.active_version)
            mac = _read_private_file(self._keyring.directory / 'request-mac.key')
            if len(mac) != 32 or hmac.compare_digest(key, mac):
                raise ValueError
            return key
        except (OSError, ValueError, TypeError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def _resource(self, conn, actor, resource, payload):
        # Caller already owns operator/session locks. Never acquire a task lock.
        if resource.kind == 'mailbox':
            row = conn.execute('SELECT owner_operator_id,email_norm FROM mailbox_registry '
                               'WHERE id=%s FOR UPDATE', (resource.id,)).fetchone()
            if row is not None and (str(row[0]) != actor.operator_id or row[1] != payload.email_norm):
                raise ServiceError(ErrorCode.FORBIDDEN)
        elif resource.kind == 'platform':
            locator = conn.execute('SELECT mailbox_id FROM mailbox_platform_states WHERE id=%s',
                                   (resource.id,)).fetchone()
            if locator is not None:
                mailbox = conn.execute('SELECT owner_operator_id,email_norm FROM mailbox_registry '
                                       'WHERE id=%s FOR UPDATE', (locator[0],)).fetchone()
                row = conn.execute('SELECT mailbox_id,platform FROM mailbox_platform_states '
                                   'WHERE id=%s FOR UPDATE', (resource.id,)).fetchone()
                if (mailbox is None or row is None or row[0] != locator[0]
                        or str(mailbox[0]) != actor.operator_id or mailbox[1] != payload.email_norm
                        or row[1] != payload.platform):
                    raise ServiceError(ErrorCode.FORBIDDEN)
        elif resource.kind == 'card':
            row = conn.execute('SELECT owner_operator_id FROM payment_cards WHERE id=%s FOR UPDATE',
                               (resource.id,)).fetchone()
            if row is not None and str(row[0]) != actor.operator_id:
                raise ServiceError(ErrorCode.FORBIDDEN)
        else:
            # billing_identities is append-only for app; taking FOR UPDATE
            # would incorrectly require privileges deliberately not granted.
            row = conn.execute('SELECT owner_operator_id FROM billing_identities WHERE id=%s',
                               (resource.id,)).fetchone()
            if row is not None and str(row[0]) != actor.operator_id:
                raise ServiceError(ErrorCode.FORBIDDEN)

    def put_locked(self, conn, actor, resource, payload):
        # Validate synthetic exact types before any connection/SQL/encryption.
        kind, resource_kind, permission, clear = _encode_payload(payload)
        if type(resource) is not PoolResource:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        resource.__post_init__()
        if resource.kind != resource_kind:
            raise ServiceError(ErrorCode.FORBIDDEN)
        self._policy._connection(conn)
        security.revalidate(conn, actor, permission)
        self._resource(conn, actor, resource, payload)
        security.revalidate(conn, actor, permission)
        key = self._keys()
        secret_id, nonce = str(uuid4()), secrets.token_bytes(12)
        key_version = self._keyring.active_version
        ciphertext = AESGCM(key).encrypt(nonce, clear,
            aad_for(self._policy.settings.schema, secret_id, kind, 1))
        policy = f'pool:{actor.operator_id}:{resource.kind}:{resource.id}:v1'
        conn.execute('INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) '
                     'VALUES(%s,%s,%s,%s,%s,%s)',
                     (secret_id, kind, key_version, nonce, ciphertext, policy))
        audit.append(conn, actor.operator_id, None, 'secret.put', secret_id, 'CREATED', secret_id,
                     after_summary={'version': 1})
        # Any resource/audit wait can cross TTL. Validate again before returning
        # only a reference; caller retains transaction/commit ownership.
        self._policy._connection(conn)
        security.revalidate(conn, actor, permission)
        self._resource(conn, actor, resource, payload)
        security.revalidate(conn, actor, permission)
        if self._keyring.active_version != key_version or not hmac.compare_digest(key, self._keys()):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn, actor, permission)
        return SecretRef(secret_id, 1)
