"""Fixture-only encrypted objects and commit-gated disclosure, never an HTTP API."""
import json
import secrets
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.rows import dict_row

from .errors import ErrorCode, ServiceError
from .keyring import Keyring


def aad_for(schema, secret_id, kind, revision):
    return json.dumps([schema, secret_id, kind, revision],
                      ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class FixtureConsumer:
    """Fixed no-I/O synthetic consumer. No arbitrary callback/provider hooks."""
    __slots__ = ('calls',)

    def __init__(self):
        self.calls = 0

    def consume(self, clear_bytes):
        self.calls += 1
        return clear_bytes

    def __repr__(self):
        return '<FixtureConsumer>'


class SecretStore:
    __slots__ = ('_keyring',)

    def __init__(self, keyring):
        if type(keyring) is not Keyring:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        self._keyring = keyring

    def __repr__(self):
        return '<SecretStore>'

    def _schema(self, conn):
        return conn.execute('SELECT current_schema()').fetchone()[0]

    def _identity(self, secret_id):
        try:
            if type(secret_id) is not str or str(UUID(secret_id)) != secret_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None
        return secret_id

    def _encrypt(self, conn, secret_id, revision, clear_bytes):
        version = self._keyring.active_version
        key = self._keyring.key(version)
        nonce = secrets.token_bytes(12)
        cipher = AESGCM(key).encrypt(nonce, clear_bytes,
                                    aad_for(self._schema(conn), secret_id, 'fixture', revision))
        return version, nonce, cipher

    def _decrypt(self, conn, row):
        try:
            if len(row['nonce']) != 12:
                raise ValueError
            key = self._keyring.key(row['key_version'])
            clear = AESGCM(key).decrypt(row['nonce'], row['ciphertext'],
                                       aad_for(self._schema(conn), str(row['id']), row['kind'], row['revision']))
            if not clear.startswith(b'fixture:') or not 8 < len(clear) <= 65536:
                raise ValueError
            return clear
        except (InvalidTag, ValueError, TypeError, KeyError):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE) from None

    def _locked(self, conn, actor, secret_id, permission):
        from . import repository, security
        repository.require_transaction(conn)
        security.revalidate(conn, actor, permission)
        secret_id = self._identity(secret_id)
        with conn.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute('SELECT * FROM secret_objects WHERE id=%s FOR UPDATE',
                                 (secret_id,)).fetchone()
        # Waiting on the secret row may cross the session's absolute/idle TTL.
        security.revalidate(conn, actor, permission)
        now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
        if (row is None or row['kind'] != 'fixture'
                or row['access_policy'] != 'operator:' + actor.operator_id
                or row['revoked_at'] is not None
                or row['expires_at'] is not None and row['expires_at'] <= now):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        return row

    def put(self, conn, actor, kind, clear_bytes, access_policy):
        from . import audit, repository, security
        repository.require_transaction(conn)
        security.revalidate(conn, actor, 'config:manage')
        if (type(kind) is not str or kind != 'fixture' or type(clear_bytes) is not bytes
                or not clear_bytes.startswith(b'fixture:') or not 8 < len(clear_bytes) <= 65536
                or type(access_policy) is not str or access_policy != 'operator:' + actor.operator_id):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        secret_id = str(uuid4())
        version, nonce, cipher = self._encrypt(conn, secret_id, 1, clear_bytes)
        conn.execute('INSERT INTO secret_objects '
                     '(id,kind,key_version,nonce,ciphertext,access_policy) VALUES (%s,%s,%s,%s,%s,%s)',
                     (secret_id, 'fixture', version, nonce, cipher, access_policy))
        security.revalidate(conn, actor, 'config:manage')
        audit.append(conn, actor.operator_id, None, 'secret.put', secret_id, 'CREATED', secret_id,
                     after_summary={'version': 1})
        security.revalidate(conn, actor, 'config:manage')
        return secret_id

    def _read(self, conn, actor, secret_id, expected_revision=None):
        from . import audit, security
        row = self._locked(conn, actor, secret_id, 'keys:download')
        if expected_revision is not None and row['revision'] != expected_revision:
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        clear = self._decrypt(conn, row)
        security.revalidate(conn, actor, 'keys:download')
        audit.append(conn, actor.operator_id, None, 'secret.use', str(row['id']), 'AVAILABLE',
                     str(row['id']), after_summary={'version': row['version']})
        # Audit INSERT can wait on a table lock too. A pre-audit clock check
        # cannot authorize disclosure after that wait crosses either deadline.
        self._locked(conn, actor, secret_id, 'keys:download')
        return clear

    def read_for_download(self, conn, actor, secret_id, expected_revision):
        """Internal transaction-local bytes; download owner must gate COMMIT before return.

        Not a standalone consumer/HTTP API. This method deliberately does not
        commit and must never be exposed directly as a response handler.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        return self._read(conn, actor, secret_id, expected_revision)

    def use(self, settings, actor, secret_id, purpose, consumer):
        from . import storage
        if type(purpose) is not str or purpose != 'fixture' or type(consumer) is not FixtureConsumer:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        with storage.unit_of_work(settings) as conn:
            clear = self._read(conn, actor, secret_id)
        # No finally, retry, or callback escape can disclose before confirmed
        # COMMIT. An unknown acknowledgement raises without calling consume.
        return consumer.consume(clear)

    def rotate(self, conn, actor, secret_id, expected_version):
        from . import audit, security
        if type(expected_version) is not int or expected_version < 1:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        row = self._locked(conn, actor, secret_id, 'config:manage')
        if row['version'] != expected_version:
            raise ServiceError(ErrorCode.VERSION_CONFLICT)
        clear = self._decrypt(conn, row)
        key_version, nonce, cipher = self._encrypt(conn, secret_id, row['revision'], clear)
        security.revalidate(conn, actor, 'config:manage')
        changed = conn.execute('UPDATE secret_objects SET key_version=%s,nonce=%s,ciphertext=%s, '
                               'version=version+1,updated_at=clock_timestamp() '
                               'WHERE id=%s AND version=%s RETURNING version',
                               (key_version, nonce, cipher, secret_id, expected_version)).fetchone()
        if changed is None:
            raise ServiceError(ErrorCode.VERSION_CONFLICT)
        audit.append(conn, actor.operator_id, None, 'secret.rotate', secret_id, 'ROTATED', secret_id,
                     before_summary={'version': expected_version}, after_summary={'version': changed[0]})
        self._locked(conn, actor, secret_id, 'config:manage')
        return changed[0]

    def revoke(self, conn, actor, secret_id, expected_version):
        from . import audit, security
        if type(expected_version) is not int or expected_version < 1:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        row = self._locked(conn, actor, secret_id, 'config:manage')
        if row['version'] != expected_version:
            raise ServiceError(ErrorCode.VERSION_CONFLICT)
        security.revalidate(conn, actor, 'config:manage')
        changed = conn.execute('UPDATE secret_objects SET revoked_at=clock_timestamp(), '
                               'version=version+1,updated_at=clock_timestamp() '
                               'WHERE id=%s AND version=%s RETURNING version',
                               (secret_id, expected_version)).fetchone()
        if changed is None:
            raise ServiceError(ErrorCode.VERSION_CONFLICT)
        audit.append(conn, actor.operator_id, None, 'secret.revoke', secret_id, 'REVOKED', secret_id,
                     before_summary={'version': expected_version}, after_summary={'version': changed[0]})
        security.revalidate(conn, actor, 'config:manage')
        return changed[0]
