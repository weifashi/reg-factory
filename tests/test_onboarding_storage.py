"""P1b connection and transaction isolation boundary tests."""
import importlib.util
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import uuid
from unittest.mock import Mock, patch
import unittest

import psycopg


class StorageContractTests(unittest.TestCase):
    def test_storage_boundary_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("onboarding.storage"),
                             "P1b requires a safe storage boundary")


class StorageTests(unittest.TestCase):
    def setUp(self):
        from onboarding import storage
        from onboarding.errors import ErrorCode, ServiceError
        from onboarding.settings import Settings
        self.api = storage
        self.assertTrue(callable(getattr(storage, "open_app", None)),
                        "open_app boundary is not implemented")
        self.codes, self.error = ErrorCode, ServiceError
        self.config = Settings(
            host="/workspace/reg-factory/.local/onboarding-p1b/socket",
            port=55433, dbname="rf_onboarding_test", user="rf_onboarding_app",
            password="private-fixture-password", schema="rf_onboarding",
            instance_marker="rf-onboarding-p1b-v1:fixture",
        )
        self.conn = Mock()
        self.conn.info.host = self.config.host
        self.conn.info.port = self.config.port
        self.identity = (self.config.dbname, self.config.user,
                         self.config.instance_marker, None, "")
        self.owner = ("rf_onboarding_migrator",)
        self.stat = patch.object(storage, "_validate_socket", return_value=None)
        self.stat.start()
        self.addCleanup(self.stat.stop)
        self.connect = patch.object(storage.psycopg, "connect", return_value=self.conn)
        self.mock_connect = self.connect.start()
        self.addCleanup(self.connect.stop)
        self.conn.execute.return_value.fetchone.side_effect = lambda: self.identity

    def open_rows(self, owner=None):
        self.conn.execute.return_value.fetchone.side_effect = [
            self.identity, self.owner if owner is None else owner,
        ]

    def expect_error(self, code, call):
        with self.assertRaises(self.error) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code.value)
        return caught.exception

    def test_bad_targets_and_roles_never_connect(self):
        for changes in (dict(host="127.0.0.1"), dict(dbname="gcloud_demo"),
                        dict(schema="public"), dict(user="rf_onboarding_migrator")):
            with self.subTest(changes=changes):
                self.expect_error(self.codes.INVALID_INPUT,
                                  lambda: self.api.open_app(replace(self.config, **changes)))
        self.mock_connect.assert_not_called()

    def test_libpq_target_overrides_are_rejected_before_connect(self):
        for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "untrusted"}):
                self.expect_error(self.codes.INVALID_INPUT,
                                  lambda: self.api.open_app(self.config))
        self.mock_connect.assert_not_called()

    def test_autocommit_and_safe_search_path(self):
        self.open_rows()
        self.assertIs(self.api.open_app(self.config), self.conn)
        args = self.mock_connect.call_args.kwargs
        self.assertTrue(args["autocommit"])
        self.assertEqual(args["connect_timeout"], 3)
        self.assertEqual(args["host"], self.config.host)
        sql = [call.args[0] for call in self.conn.execute.call_args_list]
        path_sql = [item.as_string() for item in sql if hasattr(item, "as_string")]
        self.assertIn('SET search_path TO "rf_onboarding", "pg_catalog"', path_sql)

    def test_wrong_database_role_marker_tcp_or_listeners_close_connection(self):
        for index, value in ((0, "gcloud_demo"), (1, "postgres"), (2, "wrong-marker"),
                             (3, "127.0.0.1"), (4, "localhost")):
            with self.subTest(index=index):
                identity = list(self.identity)
                identity[index] = value
                self.conn.execute.return_value.fetchone.side_effect = [tuple(identity)]
                self.expect_error(self.codes.INVALID_INPUT,
                                  lambda: self.api.open_app(self.config))
                self.conn.close.assert_called()

    def test_wrong_schema_owner_is_rejected(self):
        self.open_rows(owner=("rf_onboarding_app",))
        self.expect_error(self.codes.INVALID_INPUT, lambda: self.api.open_app(self.config))
        self.conn.close.assert_called()

    def test_actual_socket_endpoint_must_match_configuration(self):
        self.open_rows()
        self.conn.info.host = "/workspace/gcloud/old-demo-socket"
        self.expect_error(self.codes.INVALID_INPUT, lambda: self.api.open_app(self.config))
        self.conn.close.assert_called()

    def test_unmigrated_schema_can_connect_but_cannot_start_uow(self):
        self.conn.execute.return_value.fetchone.side_effect = [self.identity, None]
        self.assertIs(self.api.open_app(self.config), self.conn)
        self.conn.execute.return_value.fetchone.side_effect = [self.identity, None, None]
        def start():
            with self.api.unit_of_work(self.config):
                self.fail("missing schema must not grant a business transaction")
        self.expect_error(self.codes.DEPENDENCY_UNAVAILABLE, start)

    def test_connect_error_hides_driver_message(self):
        self.mock_connect.side_effect = psycopg.OperationalError("secret-DSN-password")
        self.expect_error(self.codes.DEPENDENCY_UNAVAILABLE,
                          lambda: self.api.open_app(self.config))

    def test_uow_uses_one_transaction_three_timeouts_and_closes(self):
        self.open_rows()
        self.conn.execute.return_value.fetchone.side_effect = [self.identity, self.owner, self.owner]
        transaction = Mock()
        transaction.__enter__ = Mock(return_value=transaction)
        transaction.__exit__ = Mock(return_value=False)
        self.conn.transaction.return_value = transaction
        with self.api.unit_of_work(self.config) as conn:
            self.assertIs(conn, self.conn)
        commands = [str(call.args[0]) for call in self.conn.execute.call_args_list]
        for command in ("SET LOCAL lock_timeout = '2s'", "SET LOCAL statement_timeout = '5s'",
                        "SET LOCAL idle_in_transaction_session_timeout = '10s'",
                        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"):
            self.assertIn(command, commands)
        self.conn.transaction.assert_called_once()
        self.conn.close.assert_called_once()

    def test_body_database_failure_is_dependency_unavailable(self):
        self.conn.execute.return_value.fetchone.side_effect = [self.identity, self.owner, self.owner]
        @contextmanager
        def transaction():
            yield
        self.conn.transaction.side_effect = transaction
        def execute():
            with self.api.unit_of_work(self.config):
                raise psycopg.OperationalError("secret body text")
        self.expect_error(self.codes.DEPENDENCY_UNAVAILABLE, execute)
        self.conn.close.assert_called_once()

    def test_successful_body_but_commit_connection_loss_is_unknown(self):
        self.conn.execute.return_value.fetchone.side_effect = [self.identity, self.owner, self.owner]
        @contextmanager
        def transaction():
            yield
            raise psycopg.OperationalError("secret commit DSN")
        self.conn.transaction.side_effect = transaction
        def execute():
            with self.api.unit_of_work(self.config):
                pass
        self.expect_error(self.codes.COMMIT_UNKNOWN, execute)
        self.conn.close.assert_called_once()

    def test_existing_domain_error_is_preserved(self):
        self.conn.execute.return_value.fetchone.side_effect = [self.identity, self.owner, self.owner]
        @contextmanager
        def transaction():
            yield
        self.conn.transaction.side_effect = transaction
        def execute():
            with self.api.unit_of_work(self.config):
                raise self.error(self.codes.VERSION_CONFLICT)
        self.expect_error(self.codes.VERSION_CONFLICT, execute)


class SocketPathTests(unittest.TestCase):
    def test_socket_validation_rejects_a_symlink_before_connect(self):
        from onboarding import storage
        self.assertTrue(callable(getattr(storage, "_validate_socket", None)),
                        "socket filesystem boundary is not implemented")
        from onboarding.errors import ErrorCode, ServiceError
        from onboarding.settings import SOCKET
        info = Mock(st_mode=0o120777)
        with patch.object(storage.os, "lstat", return_value=info):
            with self.assertRaises(ServiceError) as caught:
                storage._validate_socket(SOCKET)
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)


class LiveStorageTests(unittest.TestCase):
    """Mandatory real dedicated-PG tests: missing DB/configuration is failure."""

    def setUp(self):
        from onboarding import storage
        from onboarding.settings import BASE, load_settings
        self.api, self.base = storage, BASE
        self.app = load_settings(BASE / "app.json")
        self.migrator = load_settings(BASE / "migrator.json", role="migrator")

    def test_real_identity_roles_socket_and_search_path(self):
        for config, opener in ((self.app, self.api.open_app),
                               (self.migrator, self.api.open_migrator)):
            with self.subTest(role=config.user), opener(config) as conn:
                self.assertTrue(conn.autocommit)
                self.assertEqual(conn.info.host, config.host)
                row = conn.execute("SELECT current_database(), current_user, inet_server_addr(), "
                                   "current_setting('listen_addresses'), current_setting('search_path')").fetchone()
                self.assertEqual(row, (config.dbname, config.user, None, "",
                                       config.schema + ", pg_catalog"))
                flags = conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                                     "FROM pg_roles WHERE rolname=current_user").fetchone()
                self.assertEqual(flags, (False,) * 5)

    def test_real_schema_absence_and_wrong_instance_marker_are_rejected(self):
        from onboarding.errors import ErrorCode, ServiceError
        absent = replace(self.app, schema="rf_p1b_test_" + uuid.uuid4().hex)
        with self.api.open_app(absent):
            pass
        with self.assertRaises(ServiceError) as caught:
            with self.api.unit_of_work(absent):
                self.fail("absent schema allowed a transaction")
        self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        with self.assertRaises(ServiceError) as caught:
            self.api.open_app(replace(self.app, instance_marker="rf-onboarding-p1b-v1:wrong"))
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)

    def test_real_transaction_commit_rollback_and_timeouts(self):
        from psycopg import sql
        from onboarding.errors import ErrorCode, ServiceError
        from tools.onboarding_test_db import cleanup_schema, write_private_json
        token = uuid.uuid4().hex
        schema = "rf_p1b_test_" + token
        app = replace(self.app, schema=schema)
        migrator = replace(self.migrator, schema=schema)
        # Retain the exact manifest if cleanup fails: never orphan a schema by
        # deleting its recovery record in TemporaryDirectory.__exit__.
        with nullcontext(self.base) as folder:
            manifest = Path(folder) / (schema + ".storage.json")
            write_private_json(manifest, dict(schema=schema, schema_token=token,
                               owner=migrator.user, instance_marker=app.instance_marker))
            with self.api.open_migrator(migrator) as conn, conn.transaction():
                conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(schema), sql.Identifier(migrator.user)))
                conn.execute("CREATE TABLE _test_marker(instance_marker text NOT NULL, schema_token text NOT NULL)")
                conn.execute("INSERT INTO _test_marker VALUES (%s,%s)", (app.instance_marker, token))
                conn.execute("CREATE TABLE storage_probe(id integer PRIMARY KEY)")
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(app.user)))
                conn.execute(sql.SQL("GRANT SELECT,INSERT ON storage_probe TO {}").format(sql.Identifier(app.user)))
            try:
                with self.api.unit_of_work(app) as conn:
                    settings = conn.execute("SELECT current_setting('lock_timeout'), "
                                            "current_setting('statement_timeout'), "
                                            "current_setting('idle_in_transaction_session_timeout'), "
                                            "current_setting('transaction_isolation')").fetchone()
                    self.assertEqual(settings, ("2s", "5s", "10s", "read committed"))
                    conn.execute("INSERT INTO storage_probe VALUES (1)")
                with self.assertRaises(ServiceError) as caught:
                    with self.api.unit_of_work(app) as conn:
                        conn.execute("INSERT INTO storage_probe VALUES (2)")
                        conn.execute("SELECT 1/0")
                self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
                with self.api.open_app(app) as conn:
                    self.assertEqual(conn.execute("SELECT id FROM storage_probe ORDER BY id").fetchall(), [(1,)])
            finally:
                self.assertEqual(cleanup_schema(schema, manifest)["state"], "cleaned")
                manifest.unlink()


if __name__ == "__main__":
    unittest.main()
