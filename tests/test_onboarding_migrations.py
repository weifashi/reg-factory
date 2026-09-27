"""B0 real-PostgreSQL migration acceptance; missing fixture is an error, not skip."""
import hashlib
import importlib
import importlib.util
import unittest
from pathlib import Path


class MigrationInterfaceTests(unittest.TestCase):
    def test_migration_interface_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.migrate'),
                             'Migration interface missing')
        module = importlib.import_module('onboarding.migrate')
        self.assertTrue(callable(getattr(module, 'apply_migration', None)))
        self.assertTrue(callable(getattr(module, 'migration_text', None)))


class MigrationIntegrationTests(unittest.TestCase):
    def setUp(self):
        from onboarding_support import SchemaFixture
        self.fixture = SchemaFixture()
        self.addCleanup(self.fixture.close)
        self.migrate = importlib.import_module('onboarding.migrate')

    def apply(self, script=None):
        with self.fixture.migrator() as conn:
            return self.migrate.apply_migration(conn, self.fixture.schema, script=script)

    def test_migration_atomicity(self):
        source = self.migrate.migration_text()
        broken = source.replace('CREATE TABLE resource_leases',
                                'THIS IS NOT SQL;\nCREATE TABLE resource_leases', 1)
        self.assertNotEqual(source, broken)
        from onboarding.errors import ServiceError
        with self.assertRaises(ServiceError):
            self.apply(broken)
        self.assertEqual(self.fixture.table_names(), {'_test_marker'})
        self.assertTrue(self.apply())
        self.assertEqual(self.fixture.table_names() - {'_test_marker'}, EXPECTED_TABLES)

    def test_checksum_repeat_is_noop_and_drift_rejected(self):
        from onboarding.errors import ServiceError, ErrorCode
        self.assertTrue(self.apply())
        self.assertFalse(self.apply())
        with self.assertRaises(ServiceError) as caught:
            self.apply(self.migrate.migration_text() + '\n-- unexpected drift\n')
        self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
        with self.fixture.migrator() as conn:
            rows = conn.execute('SELECT version, checksum FROM schema_migrations').fetchall()
        self.assertEqual(rows, [(1, hashlib.sha256(
            self.migrate.migration_text().encode()).hexdigest())])

    def test_app_role_cannot_create_alter_or_mutate_audit(self):
        import psycopg
        self.apply()
        for sql in ('CREATE TABLE forbidden(id int)',
                    'ALTER TABLE onboarding_tasks ADD COLUMN forbidden int',
                    'DELETE FROM audit_events',
                    'UPDATE audit_events SET action=\'forbidden\'',
                    'TRUNCATE audit_events',
                    'UPDATE global_configs SET revision=\'forbidden\'',
                    'CREATE TEMP TABLE forbidden(id int)',
                    'CREATE TABLE public.forbidden(id int)'):
            with self.subTest(sql=sql), self.fixture.app() as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql)

    def test_app_can_append_audit_and_read_migration(self):
        self.apply()
        with self.fixture.app() as conn:
            row = conn.execute("INSERT INTO audit_events(action,object_ref,outcome_code,correlation_id) "
                               "VALUES('fixture','fixture','OK','fixture') RETURNING id").fetchone()
            self.assertGreater(row[0], 0)
            self.assertEqual(conn.execute('SELECT version FROM schema_migrations').fetchone()[0], 1)

    def test_constraints_and_foreign_keys_exist(self):
        self.apply()
        with self.fixture.migrator() as conn:
            indexes = {r[0] for r in conn.execute(
                'SELECT indexname FROM pg_catalog.pg_indexes WHERE schemaname=%s',
                (self.fixture.schema,)).fetchall()}
            self.assertTrue({'approvals_one_open', 'receipts_request_unique',
                             'receipts_one_unresolved', 'secret_nonce_unique',
                             'steps_one_generation'} <= indexes)
            fks = conn.execute("SELECT count(*) FROM pg_catalog.pg_constraint "
                               "WHERE connamespace=%s::regnamespace AND contype='f'",
                               (self.fixture.schema,)).fetchone()[0]
            self.assertGreaterEqual(fks, 14)

    def test_wrong_role_and_schema_rejected(self):
        from onboarding.errors import ServiceError
        self.apply()
        with self.fixture.app() as conn:
            with self.assertRaises(ServiceError):
                self.migrate.apply_migration(conn, self.fixture.schema)
        with self.fixture.migrator() as conn:
            for schema in ('public', 'p1b', 'rf_p1b_test_bad', 'x; DROP SCHEMA public'):
                with self.subTest(schema=schema), self.assertRaises(ServiceError):
                    self.migrate.apply_migration(conn, schema)

    def test_database_enforces_real_uniques_checks_and_foreign_keys(self):
        import psycopg
        import uuid
        self.apply()
        operator, config, batch, task = [uuid.uuid4() for _ in range(4)]
        with self.fixture.migrator() as conn:
            conn.execute("INSERT INTO operators(id,username_norm,password_hash) VALUES(%s,'fixture','fixture-hash')", (operator,))
            conn.execute("INSERT INTO global_configs(id,revision,nonsecret_config,changed_by) VALUES(%s,'fixture-v1','{}',%s)", (config, operator))
            conn.execute("INSERT INTO onboarding_batches(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) VALUES(%s,'specified',1,'[\"fixture-mail\"]',%s,%s)", (batch, config, operator))
            conn.execute("INSERT INTO onboarding_tasks(id,batch_id,mailbox_ref,config_id) VALUES(%s,%s,'fixture-mail',%s)", (task, batch, config))
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                conn.execute("INSERT INTO onboarding_tasks(id,batch_id,mailbox_ref,config_id) VALUES(%s,%s,'fixture-mail',%s)", (uuid.uuid4(), uuid.uuid4(), config))
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO onboarding_batches(id,selection_mode,requested_count,selected_mailbox_refs,config_id,created_by) VALUES(%s,'specified',2,'[]',%s,%s)", (uuid.uuid4(), config, operator))
            secret_sql = "INSERT INTO secret_objects(id,kind,key_version,nonce,ciphertext,access_policy) VALUES(%s,%s,'fixture-key',%s,%s,'fixture')"
            for kind, nonce in [('otp', b'x'*12), ('cvv', b'x'*12), ('fixture', b'x'*11)]:
                with self.subTest(kind=kind, length=len(nonce)), self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute(secret_sql, (uuid.uuid4(), kind, nonce, b'x'*16))
            conn.execute(secret_sql, (uuid.uuid4(), 'fixture', b'x'*12, b'x'*16))
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(secret_sql, (uuid.uuid4(), 'fixture', b'x'*12, b'x'*16))
            approve_sql = "INSERT INTO approvals(id,task_id,action,resource_revision,config_revision,actor_id,expires_at) VALUES(%s,%s,'test','fixture-resource','fixture-v1',%s,clock_timestamp()+interval '1 hour')"
            conn.execute(approve_sql, (uuid.uuid4(), task, operator))
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(approve_sql, (uuid.uuid4(), task, operator))
            receipt_sql = "INSERT INTO operation_receipts(id,task_id,action,resource_revision,idempotency_key,request_hash,phase,fence,generation) VALUES(%s,%s,'test',%s,%s,%s,'INTENT',1,1)"
            conn.execute(receipt_sql, (uuid.uuid4(), task, 'r1', 'request-1', 'a'*64))
            for resource, request in [('r2','request-1'), ('r1','request-2')]:
                with self.subTest(resource=resource, request=request), self.assertRaises(psycopg.errors.UniqueViolation):
                    conn.execute(receipt_sql, (uuid.uuid4(), task, resource, request, 'a'*64))
            step_sql = "INSERT INTO task_steps(id,task_id,step_key,generation) VALUES(%s,%s,'fixture',1)"
            conn.execute(step_sql, (uuid.uuid4(), task))
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(step_sql, (uuid.uuid4(), task))



EXPECTED_TABLES = {
    'schema_migrations', 'operators', 'operator_sessions', 'auth_throttles',
    'global_configs', 'onboarding_batches', 'onboarding_tasks', 'task_steps',
    'resource_leases', 'operation_receipts', 'audit_events', 'secret_objects',
    'approvals', 'download_grants',
}


class MigrationCLITests(unittest.TestCase):
    def test_cli_app_credentials_refused_without_traceback(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-m', 'onboarding.migrate', '--settings',
                                 '/workspace/reg-factory/.local/onboarding-p1b/app.json'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), 'migration refused: INVALID_INPUT')
        self.assertNotIn('Traceback', result.stderr)
