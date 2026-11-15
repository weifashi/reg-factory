"""Versioned upgrades only in manifest-owned disposable schemas."""
import unittest
import uuid
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from onboarding import migrate, migration_catalog
from onboarding.errors import ErrorCode, ServiceError


class SequenceInterfaceTests(unittest.TestCase):
    def test_explicit_sequence_entrypoint_exists(self):
        self.assertTrue(callable(getattr(migrate, 'apply_all', None)))

    def test_migration_timeout_is_bounded_separately_from_business_transactions(self):
        from unittest.mock import MagicMock
        from psycopg.pq import TransactionStatus
        from onboarding import storage
        conn=MagicMock()
        conn.autocommit=True
        conn.info.transaction_status=TransactionStatus.IDLE
        def execute(statement,params=None):
            if statement.startswith('SELECT current_database'):
                raise ServiceError(ErrorCode.INVALID_INPUT)
            return MagicMock()
        conn.execute.side_effect=execute
        with self.assertRaises(ServiceError):
            migrate.apply_migration(conn,'rf_onboarding')
        commands=[call.args[0] for call in conn.execute.call_args_list]
        self.assertIn("SET LOCAL statement_timeout = '30s'",commands)
        self.assertIn("SET LOCAL lock_timeout = '2s'",commands)
        self.assertIn("SET LOCAL idle_in_transaction_session_timeout = '10s'",commands)
        # Exercise the runtime transaction separately; it must retain the short bound.
        connection=MagicMock()
        with patch.object(storage,'open_app',return_value=connection), \
             patch.object(storage,'_schema_owner',return_value=('rf_onboarding_migrator',)):
            with storage.unit_of_work(SimpleNamespace(schema='rf_onboarding')):
                pass
        runtime_commands=[call.args[0] for call in connection.execute.call_args_list]
        self.assertIn("SET LOCAL statement_timeout = '5s'",runtime_commands)

    def test_cli_target_is_explicit_and_default_stays_core(self):
        for arguments, expected in (([], 1), (['--target-version', '2'], 2), (['--target-version', '3'], 3)):
            with self.subTest(expected=expected), \
                 patch('onboarding.settings.load_settings', return_value=SimpleNamespace(schema='rf_onboarding')) as load, \
                 patch('onboarding.storage.open_migrator') as connection, \
                 patch.object(migrate, 'apply_all', return_value=True) as apply, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(migrate.main(['--settings', '/fixture-private-config', *arguments]), 0)
                load.assert_called_once_with('/fixture-private-config', role='migrator')
                apply.assert_called_once_with(connection.return_value.__enter__.return_value,
                                              'rf_onboarding', target_version=expected)
                self.assertIn(f'{expected:03d}', output.getvalue())


class SequenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        from onboarding_support import SchemaFixture
        self.fixture = SchemaFixture()
        self.addCleanup(self.fixture.close)

    def versions(self):
        with self.fixture.migrator() as conn:
            return conn.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()

    def test_new_schema_upgrade_repeat_and_core_compatibility(self):
        with self.fixture.migrator() as conn:
            self.assertTrue(migrate.apply_all(conn, self.fixture.schema, target_version=2))
            self.assertFalse(migrate.apply_all(conn, self.fixture.schema, target_version=2))
            self.assertFalse(migrate.apply_migration(conn, self.fixture.schema))
        self.assertEqual(tuple(self.versions()), migration_catalog.expected_schema_versions(2))

    def test_003_upgrade_repeat_and_least_privilege(self):
        with self.fixture.migrator() as conn:
            self.assertTrue(migrate.apply_all(conn,self.fixture.schema,target_version=3))
            self.assertFalse(migrate.apply_all(conn,self.fixture.schema,target_version=3))
            self.assertFalse(migrate.apply_all(conn,self.fixture.schema,target_version=2))
            self.assertEqual(migration_catalog.validate_applied(self.versions(),minimum=3),3)
            row=conn.execute("SELECT is_nullable,column_default FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='onboarding_tasks' AND column_name='execution_mode'",
                (self.fixture.schema,)).fetchone()
            self.assertEqual(row,('NO',"'synthetic'::text"))
        with self.fixture.app() as conn:
            table=self.fixture.schema+'.live_effect_journal'
            for privilege in ('SELECT','INSERT','UPDATE'):
                self.assertTrue(conn.execute('SELECT has_table_privilege(current_user,%s,%s)',
                                              (table,privilege)).fetchone()[0])
            for privilege in ('DELETE','TRUNCATE','TRIGGER'):
                self.assertFalse(conn.execute('SELECT has_table_privilege(current_user,%s,%s)',
                                               (table,privilege)).fetchone()[0])

    def test_existing_core_data_survives_upgrade(self):
        operator = uuid.uuid4()
        with self.fixture.migrator() as conn:
            migrate.apply_migration(conn, self.fixture.schema)
            conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,'fixture-preserved','fixture-only')", (operator,))
            self.assertTrue(migrate.apply_all(conn, self.fixture.schema, target_version=2))
            self.assertEqual(conn.execute('SELECT username_norm FROM operators WHERE id=%s', (operator,)).fetchone(), ('fixture-preserved',))

    def test_unknown_or_drifted_history_is_refused_before_new_ddl(self):
        with self.fixture.migrator() as conn:
            migrate.apply_migration(conn, self.fixture.schema)
            for statement in ("UPDATE schema_migrations SET checksum=repeat('0',64) WHERE version=1",
                              "INSERT INTO schema_migrations(version,checksum) VALUES(4,repeat('0',64))"):
                with conn.transaction(force_rollback=True):
                    conn.execute(statement)
                    with self.assertRaises(ServiceError) as caught:
                        migrate.apply_all(conn, self.fixture.schema, target_version=2)
                    # Migrator refuses a borrowed transaction before considering history.
                    self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            conn.execute("UPDATE schema_migrations SET checksum=repeat('0',64) WHERE version=1")
            with self.assertRaises(ServiceError) as caught:
                migrate.apply_all(conn, self.fixture.schema, target_version=2)
            self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
        self.assertNotIn('mailbox_registry', self.fixture.table_names())

    def test_unknown_committed_version_is_refused_even_by_core_consumer(self):
        with self.fixture.migrator() as conn:
            migrate.apply_migration(conn, self.fixture.schema)
            conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES(4,repeat('0',64))")
            with self.assertRaises(ServiceError) as caught:
                migrate.apply_migration(conn, self.fixture.schema)
            self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)

    def test_002_failure_rolls_back_only_002_and_preserves_001(self):
        class FailAfterPoolDDL:
            def __init__(self, connection):
                self.connection = connection
                self.injected = 0
            @property
            def autocommit(self): return self.connection.autocommit
            @property
            def info(self): return self.connection.info
            def transaction(self): return self.connection.transaction()
            def execute(self, statement, params=None):
                result = self.connection.execute(statement, params)
                if isinstance(statement, str) and statement == migration_catalog.load_migration(2):
                    self.injected += 1
                    raise psycopg.errors.SyntaxError('fixture post-DDL fault')
                return result

        with self.fixture.migrator() as conn:
            failing = FailAfterPoolDDL(conn)
            with self.assertRaises(ServiceError) as caught:
                migrate.apply_all(failing, self.fixture.schema, target_version=2)
            self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertEqual(failing.injected, 1, 'An environmental failure is not the injected rollback test')
        self.assertEqual(tuple(self.versions()), migration_catalog.expected_schema_versions(1))
        self.assertNotIn('mailbox_registry', self.fixture.table_names())

    def test_missing_prerequisite_and_invalid_targets_do_not_apply(self):
        with self.fixture.migrator() as conn:
            with self.assertRaises(ServiceError) as caught:
                migrate.apply_migration(conn, self.fixture.schema, target_version=2)
            self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
            for target in (True, 0, 5, '2'):
                with self.subTest(target=target), self.assertRaises(ServiceError) as caught:
                    migrate.apply_all(conn, self.fixture.schema, target_version=target)
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
            with self.assertRaises(ServiceError) as caught:
                migrate.apply_migration(conn, self.fixture.schema, target_version=2, script='SELECT 1')
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        self.assertEqual(self.fixture.table_names(), {'_test_marker'})


if __name__ == '__main__':
    unittest.main()
