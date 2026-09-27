"""Offline rejection tests; integration evidence is recorded by the B0 runner."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import uuid
from unittest import mock


class IsolationToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'tools/onboarding_test_db.py'
        cls.path = path

    def tool(self):
        self.assertTrue(self.path.is_file(), 'dedicated test database tool is missing')
        spec = importlib.util.spec_from_file_location('onboarding_test_db_tool', self.path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_target_is_frozen_and_remote_old_or_traversal_paths_rejected(self):
        t = self.tool()
        self.assertEqual(t.validate_base(t.BASE), t.BASE)
        for bad in ['/tmp/pg', '/workspace/gcloud/.local/pgdata', '/', str(t.BASE / '..'), str(t.BASE / 'other')]:
            with self.subTest(target=bad), self.assertRaises(t.Refusal):
                t.validate_base(bad)

    def test_manifest_requires_matching_instance_owner_and_uuid_schema(self):
        t = self.tool()
        marker = 'rf-onboarding-p1b-v1:' + str(uuid.uuid4())
        token = uuid.uuid4().hex
        schema = 'rf_p1b_test_' + token
        manifest = dict(instance_marker=marker, schema=schema, schema_token=token, owner=t.MIGRATOR)
        self.assertEqual(t.validate_manifest(manifest, schema, marker), token)
        for key, value in [('instance_marker', 'other'), ('owner', 'postgres'), ('schema_token', uuid.uuid4().hex), ('schema', 'rf_onboarding')]:
            with self.subTest(key=key), self.assertRaises(t.Refusal):
                t.validate_manifest(dict(manifest, **{key: value}), schema, marker)
        for bad in ['public', 'rf_onboarding', 'rf_p1b_test_', schema + '; DROP DATABASE postgres;']:
            with self.subTest(schema=bad), self.assertRaises(t.Refusal):
                t.validate_manifest(manifest, bad, marker)

    def test_private_json_is_0600_and_never_overwrites(self):
        t = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private.json'
            t.write_private_json(path, {'secret': 'canary'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(t.Refusal):
                t.write_private_json(path, {'secret': 'replacement'})
            self.assertEqual(json.loads(path.read_text()), {'secret': 'canary'})
            path.chmod(0o644)
            with self.assertRaises(t.Refusal):
                t.read_private_json(path)

    def test_binary_environment_uses_only_packaged_libs_and_no_pg_overrides(self):
        t = self.tool()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(t, 'BASE', Path(tmp)), \
                mock.patch.dict(os.environ, {'PGHOST': 'old-demo', 'LD_LIBRARY_PATH': '/untrusted'}):
            with mock.patch.object(t.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                t._run('initdb', ['--version'])
        env = run.call_args.kwargs['env']
        self.assertNotIn('PGHOST', env)
        self.assertEqual(env['LD_LIBRARY_PATH'], '/workspace/gcloud/.local/pgdist/usr/lib/x86_64-linux-gnu')

    def test_private_runtime_library_is_preferred_without_ambient_paths(self):
        t = self.tool()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(t, 'BASE', Path(tmp)):
            runtime = Path(tmp) / 'runtime-lib'
            runtime.mkdir(mode=0o700)
            with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/untrusted'}):
                with mock.patch.object(t.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                    t._run('pg_ctl', ['--version'])
            self.assertEqual(run.call_args.kwargs['env']['LD_LIBRARY_PATH'],
                             str(runtime) + ':/workspace/gcloud/.local/pgdist/usr/lib/x86_64-linux-gnu')

    def test_unsafe_private_runtime_library_directory_is_refused(self):
        t = self.tool()
        for scenario in ('wide_mode', 'symlink', 'dangling_symlink', 'file', 'owner'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(t, 'BASE', Path(tmp)):
                    runtime = Path(tmp) / 'runtime-lib'
                    if scenario == 'wide_mode':
                        runtime.mkdir(mode=0o755)
                        # mkdir is filtered by umask; this fixture must really be unsafe.
                        runtime.chmod(0o755)
                    elif scenario == 'symlink': runtime.symlink_to('/tmp', target_is_directory=True)
                    elif scenario == 'dangling_symlink': runtime.symlink_to(Path(tmp) / 'missing', target_is_directory=True)
                    elif scenario == 'owner': runtime.mkdir(mode=0o700)
                    else: runtime.write_text('not a directory'); runtime.chmod(0o700)
                    actual_uid = os.getuid()
                    with mock.patch.object(t.os, 'getuid', return_value=actual_uid + (scenario == 'owner')), \
                            mock.patch.object(t.subprocess, 'run') as run:
                        with self.assertRaises(t.Refusal): t._run('pg_ctl', ['--version'])
                        self.assertFalse(run.called, "unsafe path must not reach subprocess")

    def test_nonbootstrap_checks_do_not_require_read_all_settings(self):
        t = self.tool()
        marker = 'rf-onboarding-p1b-v1:' + str(uuid.uuid4())
        conn = mock.Mock()
        conn.execute.return_value.fetchone.side_effect = [(t.DBNAME, marker), ('',), ('55433',), (str(t.BASE / 'socket'),)]
        t._verify_server(conn, marker)
        self.assertEqual(conn.execute.call_count, 3)

    def test_connector_rejects_libpq_environment_before_connecting(self):
        t = self.tool()
        config = dict(host=str(t.BASE / 'socket'), port=t.PORT, dbname=t.DBNAME,
                      user=t.APP, password='canary')
        driver = mock.Mock()
        for key in ('PGHOSTADDR', 'PGSERVICE', 'PGOPTIONS', 'PGPASSFILE'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: 'override'}):
                with mock.patch.dict('sys.modules', {'psycopg': driver}):
                    with self.assertRaises(t.Refusal):
                        t._connect(config)
        driver.connect.assert_not_called()

    def test_partial_state_is_refused_without_rewriting_credentials(self):
        t = self.tool()
        state = dict(instance_marker='rf-onboarding-p1b-v1:' + str(uuid.uuid4()),
                     bootstrap_user=t.getpass.getuser(), state='preparing')
        with mock.patch.object(t, 'read_private_json', return_value=state) as read:
            with mock.patch.object(t, 'write_private_json') as write:
                with self.assertRaisesRegex(t.Refusal, 'partial preparation'):
                    t._state()
        self.assertEqual(read.call_count, 1)
        write.assert_not_called()

    def test_fifo_config_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / 'config.fifo'
            os.mkfifo(fifo, 0o600)
            program = ('from tools.onboarding_test_db import read_private_json, Refusal\n'
                       'import sys\n'
                       'try: read_private_json(sys.argv[1])\n'
                       'except Refusal: raise SystemExit(0)\n'
                       'raise SystemExit(1)\n')
            try:
                result = subprocess.run([sys.executable, '-c', program, str(fifo)],
                                        cwd=self.path.parents[1], capture_output=True,
                                        text=True, timeout=2)
            except subprocess.TimeoutExpired:
                self.fail('FIFO configuration blocked before regular-file rejection')
            self.assertEqual(result.returncode, 0, 'FIFO must be rejected promptly')

    def test_private_json_rejects_oversized_files(self):
        t = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            t.write_private_json(path, {'padding': 'x' * 16384})
            with self.assertRaises(t.Refusal):
                t.read_private_json(path)

    def test_private_json_rejects_duplicate_keys(self):
        t = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            t.write_private_json(path, {})
            path.write_text('{"instance_marker":"first","instance_marker":"second"}')
            with self.assertRaises(t.Refusal):
                t.read_private_json(path)

    def test_symlink_config_rejected(self):
        t = self.tool()
        with tempfile.TemporaryDirectory() as tmp:
            target, link = Path(tmp) / 'private.json', Path(tmp) / 'link.json'
            t.write_private_json(target, {})
            link.symlink_to(target)
            with self.assertRaises(t.Refusal):
                t.read_private_json(link)
            with self.assertRaises(t.Refusal):
                t.write_private_json(link, {})

    def test_config_rejects_target_overrides_and_never_repr_password(self):
        t = self.tool()
        marker = 'rf-onboarding-p1b-v1:' + str(uuid.uuid4())
        config = dict(host=str(t.BASE / 'socket'), port=55433, dbname=t.DBNAME,
                      user=t.APP, password='never-print-canary', instance_marker=marker,
                      schema='rf_onboarding')
        t.validate_config(config, t.APP, marker)
        for field, bad in [('host', 'localhost'), ('port', 5432), ('dbname', 'postgres'), ('user', t.MIGRATOR), ('schema', 'public'), ('options', '-c role=postgres')]:
            with self.subTest(field=field), self.assertRaises(t.Refusal) as caught:
                t.validate_config(dict(config, **{field: bad}), t.APP, marker)
            self.assertNotIn(config['password'], str(caught.exception))


class InstanceIntegrationTests(unittest.TestCase):
    tool = IsolationToolTests.tool
    setUpClass = classmethod(IsolationToolTests.setUpClass.__func__)
    """These tests require prepare; an unavailable DB is a failure, never skip."""
    def test_prepare_idempotence_and_status_do_not_expose_or_change_credentials(self):
        t = self.tool()
        before = {name: (t.BASE / name).read_bytes() for name in ('app.json', 'migrator.json')}
        result = t.prepare()
        self.assertEqual(result['state'], 'running')
        for name, original in before.items():
            self.assertEqual(hashlib.sha256((t.BASE / name).read_bytes()).hexdigest(), hashlib.sha256(original).hexdigest())
            self.assertTrue(json.loads(original)['password'] not in json.dumps(result), 'status must not expose credentials')
        self.assertFalse(result['tcp_listening'])

    def test_application_cannot_create_schema_public_tables_or_temp_tables(self):
        t = self.tool()
        import psycopg
        with t._connect(t.read_private_json(t.BASE / 'app.json')) as conn:
            for statement in ('CREATE SCHEMA forbidden_app_schema',
                              'CREATE TABLE public.forbidden_app_table (id int)',
                              'CREATE TEMP TABLE forbidden_temp (id int)',
                              'CREATE TABLE rf_onboarding.forbidden_app_table (id int)'):
                with self.subTest(sql=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)

    def test_cleanup_verifies_manifest_database_marker_owner_and_table_token(self):
        t = self.tool()
        from psycopg import sql
        marker = t.read_private_json(t.BASE / 'instance.json')['instance_marker']
        token = uuid.uuid4().hex
        schema = 'rf_p1b_test_' + token
        with tempfile.TemporaryDirectory(prefix='tool-manifest-', dir=t.BASE) as tmp:
            path = Path(tmp) / 'manifest.json'
            manifest = dict(instance_marker=marker, schema=schema, schema_token=token, owner=t.MIGRATOR)
            t.write_private_json(path, manifest)
            with t._connect(t.read_private_json(t.BASE / 'migrator.json')) as conn:
                conn.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(schema), sql.Identifier(t.MIGRATOR)))
                conn.execute(sql.SQL('CREATE TABLE {}._test_marker (instance_marker text, schema_token text)').format(sql.Identifier(schema)))
                conn.execute(sql.SQL('INSERT INTO {}._test_marker VALUES (%s,%s)').format(sql.Identifier(schema)), (marker, 'wrong'))
                with self.assertRaises(t.Refusal):
                    t.cleanup_schema(schema, path)
                self.assertIsNotNone(conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone())
                conn.execute(sql.SQL('UPDATE {}._test_marker SET schema_token=%s').format(sql.Identifier(schema)), (token,))
                self.assertEqual(t.cleanup_schema(schema, path)['state'], 'cleaned')
                self.assertIsNone(conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone())
                self.assertEqual(conn.execute('SELECT current_database()').fetchone(), (t.DBNAME,))

    def test_cleanup_refuses_external_view_fk_and_transitive_dependencies(self):
        from onboarding_support import SchemaFixture
        from psycopg import sql
        t = self.tool()
        for kind in ('view', 'foreign_key', 'view_chain'):
            with self.subTest(kind=kind):
                target, external = SchemaFixture(), SchemaFixture()
                try:
                    with target.migrator() as conn:
                        conn.execute(sql.SQL('CREATE TABLE {}.source (id integer PRIMARY KEY)').format(sql.Identifier(target.schema)))
                        if kind == 'foreign_key':
                            conn.execute(sql.SQL('CREATE TABLE {}.dependent (id integer REFERENCES {}.source(id))').format(
                                sql.Identifier(external.schema), sql.Identifier(target.schema)))
                        else:
                            conn.execute(sql.SQL('CREATE VIEW {}.dependent AS SELECT id FROM {}.source').format(
                                sql.Identifier(external.schema), sql.Identifier(target.schema)))
                            if kind == 'view_chain':
                                conn.execute(sql.SQL('CREATE VIEW {}.chained AS SELECT * FROM {}.dependent').format(
                                    sql.Identifier(external.schema), sql.Identifier(external.schema)))
                        with self.assertRaisesRegex(t.Refusal, 'external|unscoped'):
                            t.cleanup_schema(target.schema, target.manifest)
                        self.assertTrue(target.manifest.exists())
                        self.assertEqual(conn.execute('SELECT count(*) FROM pg_namespace WHERE nspname=ANY(%s)',
                                                      ([target.schema, external.schema],)).fetchone()[0], 2)
                        self.assertIsNotNone(conn.execute('SELECT to_regclass(%s)',
                                                         (external.schema + '.dependent',)).fetchone()[0])
                        if kind == 'foreign_key':
                            self.assertEqual(conn.execute('SELECT count(*) FROM pg_constraint WHERE connamespace=(SELECT oid FROM pg_namespace WHERE nspname=%s) AND contype=%s',
                                                          (external.schema, 'f')).fetchone()[0], 1)
                finally:
                    # Explicitly dismantle this test-created cross-schema FK.
                    # The cleanup tool itself conservatively refuses its internal
                    # RI triggers on the other schema, in either direction.
                    if kind == 'foreign_key':
                        with external.migrator() as conn:
                            exists = conn.execute('SELECT to_regclass(%s)', (external.schema + '.dependent',)).fetchone()[0]
                            if exists:
                                conn.execute(sql.SQL('ALTER TABLE {}.dependent DROP CONSTRAINT IF EXISTS dependent_id_fkey').format(sql.Identifier(external.schema)))
                    # Both are this test's UUID schemas; remove the dependent one first.
                    for fixture in (external, target):
                        with fixture.migrator() as conn:
                            exists = conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (fixture.schema,)).fetchone()
                        if exists:
                            fixture.close()
                        else:
                            fixture.manifest.unlink(missing_ok=True)

    def test_manifest_outside_base_or_symlink_or_world_readable_refused(self):
        t = self.tool()
        token = uuid.uuid4().hex
        schema = 'rf_p1b_test_' + token
        with tempfile.TemporaryDirectory() as external:
            external_path = Path(external) / 'manifest.json'
            t.write_private_json(external_path, {})
            with self.assertRaises(t.Refusal):
                t.cleanup_schema(schema, external_path)
            with tempfile.TemporaryDirectory(prefix='tool-manifest-', dir=t.BASE) as private:
                readable = Path(private) / 'readable.json'
                t.write_private_json(readable, {})
                readable.chmod(0o644)
                with self.assertRaises(t.Refusal):
                    t.cleanup_schema(schema, readable)
                link = Path(private) / 'manifest.json'
                link.symlink_to(external_path)
                with self.assertRaises(t.Refusal):
                    t.cleanup_schema(schema, link)


if __name__ == '__main__':
    unittest.main()
