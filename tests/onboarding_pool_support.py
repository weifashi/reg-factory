"""Synthetic pool integration fixture; no production provider or public route."""
import hashlib
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import psycopg
from onboarding_b2_support import B2Case
from onboarding import security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.keyring import Keyring
from onboarding.migrate import apply_all
from onboarding.pool_vault import PoolVault, SyntheticPoolPolicy
from onboarding.request_mac import RequestMac

# Every business table a not-committed proof (and a failed queued original) must leave untouched.
PROOF_TABLES = ('global_configs', 'onboarding_batches', 'onboarding_tasks', 'mailbox_registry',
                'mailbox_platform_states', 'secret_objects', 'resource_leases', 'operation_receipts',
                'audit_events', 'task_steps')


@contextmanager
def after_query(hook):
    """Call hook(cursor, text) right after every real query: the interleaving seam."""
    execute = psycopg.Cursor.execute
    def run(cursor, query, params=None, **kwargs):
        result = execute(cursor, query, params, **kwargs)
        hook(cursor, query if type(query) is str else str(query))
        return result
    with patch.object(psycopg.Cursor, 'execute', run):
        yield


class LockHold:
    """Pause thread 'fixture-r0' right after its first query containing marker (locks held)."""
    def __init__(self, marker):
        self.marker, self.pid, self.rollback = marker, None, False
        self.locked, self.release = threading.Event(), threading.Event()

    def hook(self, cursor, text):
        if threading.current_thread().name != 'fixture-r0' or self.marker not in text or self.locked.is_set():
            return
        self.pid = cursor.connection.info.backend_pid
        self.locked.set()
        if not self.release.wait(8):
            raise RuntimeError('fixture:hold-not-released')
        if self.rollback:
            raise RuntimeError('fixture:original-rolls-back')


@contextmanager
def parked_open():
    """Thread 'fixture-r0' waits right after opening its connection, before any SQL."""
    arrived, gate = threading.Event(), threading.Event()
    original = storage.open_app
    def opened(value):
        conn = original(value)
        if threading.current_thread().name == 'fixture-r0':
            arrived.set()
            if not gate.wait(10):
                raise RuntimeError('fixture:parked-original-not-released')
        return conn
    with patch.object(storage, 'open_app', opened):
        yield arrived, gate


def outcome_of(call):
    try:
        return ('OK', call())
    except ServiceError as exc:
        return (exc.code.value, exc)
    except Exception as exc:
        return (type(exc).__name__, exc)


class PoolCase(B2Case):
    def setUp(self):
        super().setUp()
        with self.fixture.migrator() as conn:
            apply_all(conn, self.fixture.schema, target_version=2)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=permissions||ARRAY['mailboxes:manage','cards:manage'] WHERE id=%s", (self.actor.operator_id,))
        self.old_store = self.make_store()
        fd = os.open(self.key_directory / 'request-mac.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(secrets.token_bytes(32))
        self.keyring = Keyring(self.key_directory, 'v1')
        self.vault = PoolVault(self.keyring, SyntheticPoolPolicy.from_settings(self.settings))
        self.mac = RequestMac(self.key_directory)

    def proof(self, call):
        """The VERSION_CONFLICT raised by call; returns its not_committed flag."""
        with self.assertRaises(ServiceError) as caught:
            call()
        self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
        return caught.exception.not_committed

    def business_snapshot(self):
        return {table: self.read('SELECT * FROM ' + table + ' ORDER BY 1') for table in PROOF_TABLES}

    def second_session(self):
        """Same operator, independent session: avoids the session-row lock queue."""
        sid = str(uuid.uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                         "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                         (sid, self.actor.operator_id, hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                          hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
        return security.Actor(self.actor.operator_id, self.actor.permissions, sid, 1)

    def drift_migrations(self):
        """Commit a schema_migrations checksum drift now; returns the value to restore."""
        checksum = self.read('SELECT checksum FROM schema_migrations WHERE version=2')[0][0]
        self.set_migration_checksum('0' * 64)
        return checksum

    def set_migration_checksum(self, checksum):
        with self.fixture.migrator() as conn:
            conn.execute('UPDATE schema_migrations SET checksum=%s WHERE version=2', (checksum,))

    def rotate_request_mac(self):
        """Rotate the request MAC key in place; returns a RequestMac on the new key."""
        (self.key_directory / 'request-mac.key').write_bytes(secrets.token_bytes(32))
        return RequestMac(self.key_directory)

    def wait_blocked_by(self, blocker_pid, seconds=3):
        deadline = time.monotonic() + seconds
        with self.fixture.app() as observer:
            while time.monotonic() < deadline:
                if observer.execute('SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))',
                                    (blocker_pid,)).fetchone()[0]:
                    return
                time.sleep(0.01)
        self.fail('replay never blocked behind the original')

    def race_original(self, marker, original, replay, *, rollback=False):
        """Original holds its serialization lock (after marker) while replay blocks behind it."""
        hold, results, threads = LockHold(marker), {}, []
        with after_query(hold.hook):
            try:
                first = threading.Thread(target=lambda: results.__setitem__('original', outcome_of(original)),
                                         name='fixture-r0')
                threads.append(first)
                first.start()
                self.assertTrue(hold.locked.wait(10), 'original never took its lock')
                second = threading.Thread(target=lambda: results.__setitem__('replay', outcome_of(replay)),
                                          name='fixture-r1')
                threads.append(second)
                second.start()
                self.wait_blocked_by(hold.pid)
                hold.rollback = rollback
            finally:
                # Never leak a lock-holding thread into later tests, even when an assertion fails.
                hold.release.set()
                for thread in threads:
                    thread.join(15)
        return results['original'], results['replay']

    def queued_original_never_commits(self, original, replay):
        """R0 queued before its transaction; R1 proves; then R0 runs and must fail with no writes."""
        results = {}
        with parked_open() as (arrived, gate):
            thread = threading.Thread(target=lambda: results.__setitem__('original', outcome_of(original)),
                                      name='fixture-r0')
            thread.start()
            try:
                self.assertTrue(arrived.wait(10), 'original never opened its connection')
                self.assertTrue(self.proof(replay), 'replay must prove the key never committed')
                before = self.business_snapshot()
            finally:
                gate.set()
                thread.join(15)
        self.assertNotEqual(results['original'][0], 'OK', 'queued original committed after the proof')
        self.assertEqual(before, self.business_snapshot())
        return results['original']
