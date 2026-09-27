"""B2 local-operator authentication only; never Google login or production data."""
import importlib.util
import unittest


class SecurityInterfaceTests(unittest.TestCase):
    def test_security_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.security'))

    def test_mailbox_management_permission_is_explicit_and_not_implied(self):
        from onboarding import security
        self.assertIn('mailboxes:manage', security.PERMISSIONS)
        self.assertEqual(security._permissions(['mailboxes:manage']), ['mailboxes:manage'])
        for granted in ([], ['onboarding:read'], ['tasks:manage'], ['cards:manage']):
            self.assertNotIn('mailboxes:manage', security._permissions(granted))
        self.assertEqual(security._permissions(['mailboxes:manage', 'mailboxes:manage']),
                         ['mailboxes:manage'])


class PasswordTests(unittest.TestCase):
    def test_strong_scrypt_random_salts_roundtrip_and_rejection(self):
        from onboarding import security
        self.assertTrue(callable(getattr(security, 'hash_password', None)))
        password = 'Synthetic-' + 'aA!9' * 5
        first = security.hash_password(password)
        second = security.hash_password(password)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith('scrypt$131072$8$1$'))
        self.assertTrue(security.verify_password(password, first))
        self.assertFalse(security.verify_password('not-the-password', first))
        self.assertFalse(security.verify_password(password, first.replace('$131072$', '$1024$')))
        self.assertFalse(security.verify_password(password, 'malformed'))
        self.assertNotIn(password, first)

    def test_password_and_username_input_bounds(self):
        from onboarding import security
        from onboarding.errors import ErrorCode, ServiceError
        self.assertTrue(callable(getattr(security, 'normalize_username', None)))
        self.assertEqual(security.normalize_username(' Fixture.User@EXAMPLE.invalid '),
                         'fixture.user@example.invalid')
        for password in ('short', '', 'x' * 1025, None):
            with self.subTest(value=type(password).__name__), self.assertRaises(ServiceError) as caught:
                security.hash_password(password)
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        for username in ('', 'x' * 129, 'a\nuser', None):
            with self.subTest(value=type(username).__name__), self.assertRaises(ServiceError):
                security.normalize_username(username)


class TransportTests(unittest.TestCase):
    def test_exact_https_origin_host_and_json_required(self):
        from onboarding import security
        from onboarding.errors import ErrorCode, ServiceError
        self.assertTrue(callable(getattr(security, 'check_request', None)))
        expected = 'https://control.fixture.invalid:8443'
        security.check_request(expected, 'control.fixture.invalid:8443', expected)
        security.check_request(expected, 'control.fixture.invalid:8443', expected,
                               content_type='application/json; charset=utf-8')
        for origin, host, configured, mime in (
                ('null', 'control.fixture.invalid:8443', expected, 'application/json'),
                (expected + '/', 'control.fixture.invalid:8443', expected, 'application/json'),
                (expected, 'attacker.invalid', expected, 'application/json'),
                (expected, 'control.fixture.invalid:8443', expected, 'text/plain'),
                ('http://control.fixture.invalid', 'control.fixture.invalid',
                 'http://control.fixture.invalid', 'application/json'),
                ('https://user@control.fixture.invalid', 'user@control.fixture.invalid',
                 'https://user@control.fixture.invalid', 'application/json'),
                ('https://control.fixture.invalid\n', 'control.fixture.invalid',
                 'https://control.fixture.invalid\n', 'application/json')):
            with self.subTest(origin=origin), self.assertRaises(ServiceError) as caught:
                security.check_request(origin, host, configured, content_type=mime)
            self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)

    def test_auth_records_are_frozen_and_hide_bearer_credentials(self):
        from dataclasses import FrozenInstanceError
        from onboarding import security
        self.assertTrue(hasattr(security, 'Actor'))
        actor = security.Actor('operator', frozenset({'onboarding:read'}), 'session', 1)
        with self.assertRaises(FrozenInstanceError):
            actor.auth_epoch = 2
        result = security.LoginResult(actor, 'session-bearer-canary', 'csrf-canary')
        self.assertNotIn('canary', repr(result))
        bootstrap = security.LoginBootstrap('preauth-canary', 'csrf-canary')
        self.assertNotIn('canary', repr(bootstrap))


from dataclasses import replace
from unittest.mock import patch
from onboarding_b2_support import B2Case
from onboarding import security
from onboarding.errors import ErrorCode, ServiceError


class SessionTests(B2Case):
    def test_require_revalidates_database_and_refreshes_bounded_idle(self):
        self.assertTrue(callable(getattr(security, 'require', None)))
        with self.uow() as conn:
            conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '5 minutes', "
                         "idle_expires_at=clock_timestamp()+interval '1 minute' WHERE id=%s", (self.session_id,))
            actor = security.require(conn, self.token, 'tasks:manage')
        self.assertEqual(actor.operator_id, self.actor.operator_id)
        self.assertEqual(actor.session_id, self.session_id)
        row = self.read('SELECT idle_expires_at=expires_at,version FROM operator_sessions WHERE id=%s',
                        (self.session_id,))[0]
        self.assertEqual(row, (True, 2))
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='auth.session_touch'")[0][0], 1)

    def test_missing_permission_and_forged_snapshot_never_grant_access(self):
        self.assertTrue(callable(getattr(security, 'revalidate', None)))
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=ARRAY['onboarding:read'] WHERE id=%s", (self.actor.operator_id,))
        forged = replace(self.actor, permissions=frozenset({'legacy:admin', 'tasks:manage'}))
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.revalidate(conn, forged, 'tasks:manage')
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        with self.uow() as conn:
            refreshed = security.revalidate(conn, forged, 'onboarding:read')
        self.assertEqual(refreshed.permissions, frozenset({'onboarding:read'}))

    def test_revoked_expired_idle_disabled_epoch_and_other_actor_rejected(self):
        self.assertTrue(callable(getattr(security, 'revalidate', None)))
        for scenario in ('revoked', 'absolute', 'idle', 'disabled', 'operator_epoch', 'snapshot_epoch'):
            with self.subTest(scenario=scenario):
                with self.uow() as conn:
                    conn.execute("UPDATE operator_sessions SET revoked_at=NULL, "
                                 "expires_at=clock_timestamp()+interval '8 hours', "
                                 "idle_expires_at=clock_timestamp()+interval '30 minutes' WHERE id=%s", (self.session_id,))
                    conn.execute('UPDATE operators SET disabled=false,auth_epoch=1 WHERE id=%s', (self.actor.operator_id,))
                    if scenario == 'revoked':
                        conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s', (self.session_id,))
                    elif scenario == 'absolute':
                        conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()-interval '1 second', "
                                     "idle_expires_at=clock_timestamp()-interval '2 seconds' WHERE id=%s", (self.session_id,))
                    elif scenario == 'idle':
                        conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (self.session_id,))
                    elif scenario == 'disabled':
                        conn.execute('UPDATE operators SET disabled=true WHERE id=%s', (self.actor.operator_id,))
                    elif scenario == 'operator_epoch':
                        conn.execute('UPDATE operators SET auth_epoch=2 WHERE id=%s', (self.actor.operator_id,))
                actor = replace(self.actor, auth_epoch=2) if scenario == 'snapshot_epoch' else self.actor
                with self.assertRaises(ServiceError) as caught, self.uow() as conn:
                    security.revalidate(conn, actor, 'onboarding:read')
                self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_invalid_tokens_and_autocommit_are_rejected(self):
        self.assertTrue(callable(getattr(security, 'require', None)))
        for token in (None, '', 'x' * 10000, 'invalid'):
            with self.subTest(token_type=type(token).__name__):
                with self.assertRaises(ServiceError) as caught, self.uow() as conn:
                    security.require(conn, token, 'onboarding:read')
                self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        with self.fixture.app() as conn:
            with self.assertRaises(ServiceError) as caught:
                security.require(conn, self.token, 'onboarding:read')
            self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)

    def test_csrf_checks_origin_and_bound_current_session(self):
        self.assertTrue(callable(getattr(security, 'check_csrf', None)))
        origin = 'https://control.fixture.invalid'
        with self.uow() as conn:
            security.check_csrf(conn, self.actor, self.csrf, origin=origin, expected_origin=origin)
        for csrf, actual_origin in (('wrong', origin), (self.csrf, 'https://attacker.invalid')):
            with self.assertRaises(ServiceError) as caught, self.uow() as conn:
                security.check_csrf(conn, self.actor, csrf, origin=actual_origin, expected_origin=origin)
            self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        with self.uow() as conn:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp() WHERE id=%s', (self.session_id,))
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.check_csrf(conn, self.actor, self.csrf, origin=origin, expected_origin=origin)
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_idle_refresh_audit_failure_rolls_back_session_change(self):
        self.assertTrue(callable(getattr(security, 'require', None)))
        before = self.read('SELECT idle_expires_at,version FROM operator_sessions WHERE id=%s', (self.session_id,))
        with patch('onboarding.audit.append', side_effect=RuntimeError('fixture fault')) as fail:
            with self.assertRaises(ServiceError), self.uow() as conn:
                security.require(conn, self.token, 'onboarding:read')
            fail.assert_called_once()
        self.assertEqual(self.read('SELECT idle_expires_at,version FROM operator_sessions WHERE id=%s', (self.session_id,)), before)


    def test_revalidate_requires_typed_positive_epoch_and_human_actor(self):
        for actor in (replace(self.actor, auth_epoch=None), replace(self.actor, auth_epoch=True),
                      replace(self.actor, auth_epoch=0), self.fixture_actor):
            with self.subTest(epoch=getattr(actor, 'auth_epoch', 'fixture')):
                with self.assertRaises(ServiceError) as caught, self.uow() as conn:
                    security.revalidate(conn, actor, 'onboarding:read')
                self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)


    def test_require_rechecks_expiry_after_audit_latency(self):
        with self.uow() as conn:
            expires = conn.execute("UPDATE operator_sessions SET expires_at=clock_timestamp()+interval '1 second', "
                                   "idle_expires_at=clock_timestamp()+interval '0.5 second' WHERE id=%s RETURNING expires_at",
                                   (self.session_id,)).fetchone()[0]
        original = security.audit.append
        def slow_audit(conn, *args, **kwargs):
            result = original(conn, *args, **kwargs)
            conn.execute('SELECT pg_sleep_until(%s)', (expires,))
            return result
        with patch('onboarding.audit.append', side_effect=slow_audit) as delayed:
            with self.assertRaises(ServiceError) as caught, self.uow() as conn:
                security.require(conn, self.token, 'onboarding:read')
            self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
            delayed.assert_called_once()
        self.assertEqual(self.read('SELECT version FROM operator_sessions WHERE id=%s', (self.session_id,))[0][0], 1)


class OperatorTests(B2Case):
    def test_local_creation_has_explicit_permissions_and_never_overwrites(self):
        self.assertTrue(callable(getattr(security, 'create_operator', None)))
        password = 'Fixture-' + 'aA!9' * 5
        with self.uow() as conn:
            operator_id = security.create_operator(conn, 'New.Fixture', password, {'onboarding:read'})
        row = self.read('SELECT username_norm,password_hash,permissions FROM operators WHERE id=%s', (operator_id,))[0]
        self.assertEqual(row[0], 'new.fixture')
        self.assertTrue(security.verify_password(password, row[1]))
        self.assertEqual(row[2], ['onboarding:read'])
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.create_operator(conn, 'new.fixture', password, {'legacy:admin'})
        self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.read('SELECT permissions FROM operators WHERE id=%s', (operator_id,))[0][0], ['onboarding:read'])
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.create_operator(conn, 'bad.fixture', password, {'all:*'})
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)

    def test_trusted_update_invalidates_existing_session_epoch(self):
        self.assertTrue(callable(getattr(security, 'update_operator', None)))
        with self.uow() as conn:
            security.update_operator(conn, self.actor.operator_id, permissions={'onboarding:read'})
        self.assertEqual(self.read('SELECT auth_epoch,version,permissions FROM operators WHERE id=%s',
                                  (self.actor.operator_id,))[0], (2, 2, ['onboarding:read']))
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.revalidate(conn, self.actor, 'onboarding:read')
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        with self.uow() as conn:
            security.update_operator(conn, self.actor.operator_id, disabled=True)
        self.assertEqual(self.read('SELECT auth_epoch,disabled FROM operators WHERE id=%s',
                                  (self.actor.operator_id,))[0], (3, True))

    def test_operator_audit_failure_rolls_back_mutation(self):
        self.assertTrue(callable(getattr(security, 'update_operator', None)))
        with patch('onboarding.audit.append', side_effect=RuntimeError('fixture fault')) as fail:
            with self.assertRaises(ServiceError), self.uow() as conn:
                security.update_operator(conn, self.actor.operator_id, disabled=True)
            fail.assert_called_once()
        self.assertEqual(self.read('SELECT auth_epoch,disabled FROM operators WHERE id=%s',
                                  (self.actor.operator_id,))[0], (1, False))


class LoginTests(B2Case):
    origin = 'https://control.fixture.invalid'

    @classmethod
    def setUpClass(cls):
        cls.password = 'Fixture-' + 'aA!9' * 5
        cls.encoded = security.hash_password(cls.password)

    def setUp(self):
        super().setUp()
        self.username = 'fixture.user'
        with self.uow() as conn:
            conn.execute('UPDATE operators SET username_norm=%s,password_hash=%s WHERE id=%s',
                         (self.username, self.encoded, self.actor.operator_id))

    def bootstrap(self):
        self.assertTrue(callable(getattr(security, 'issue_login_bootstrap', None)))
        return security.issue_login_bootstrap(self.settings, origin=self.origin, expected_origin=self.origin)

    def login(self, *, password=None, username=None, source='127.0.0.1', previous_token=None, bootstrap=None):
        bootstrap = bootstrap or self.bootstrap()
        return security.login(self.settings, username or self.username,
                              self.password if password is None else password, source,
                              preauth_token=bootstrap.preauth_token, csrf_token=bootstrap.csrf_token,
                              origin=self.origin, expected_origin=self.origin,
                              previous_token=previous_token)

    def test_login_persists_only_hashes_and_rotates_own_previous_session(self):
        self.assertTrue(callable(getattr(security, 'login', None)))
        result = self.login(previous_token=self.token)
        self.assertEqual(result.actor.operator_id, self.actor.operator_id)
        self.assertNotEqual(result.session_token, self.token)
        rows = self.read('SELECT token_hash,csrf_hash FROM operator_sessions')
        self.assertNotIn(result.session_token, repr(rows))
        self.assertNotIn(result.csrf_token, repr(rows))
        self.assertIsNotNone(self.read('SELECT revoked_at FROM operator_sessions WHERE id=%s', (self.session_id,))[0][0])
        with self.uow() as conn:
            current = security.require(conn, result.session_token, 'onboarding:read')
        self.assertEqual(current.session_id, result.actor.session_id)
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.require(conn, self.token, 'onboarding:read')
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_preauth_is_short_lived_single_use_and_csrf_bound(self):
        grant = self.bootstrap()
        self.login(bootstrap=grant)
        with self.assertRaises(ServiceError) as caught:
            self.login(bootstrap=grant)
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        grant = self.bootstrap()
        with self.assertRaises(ServiceError) as caught:
            security.login(self.settings, self.username, self.password, '127.0.0.1',
                           preauth_token=grant.preauth_token, csrf_token='bad-csrf',
                           origin=self.origin, expected_origin=self.origin)
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        grant = self.bootstrap()
        with self.uow() as conn:
            conn.execute("UPDATE auth_throttles SET blocked_until=clock_timestamp()-interval '1 second' WHERE bucket_key LIKE 'preauth:%'")
        with self.assertRaises(ServiceError) as caught:
            self.login(bootstrap=grant)
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)

    def test_failure_counters_commit_then_block_until_window_expires(self):
        self.assertTrue(callable(getattr(security, 'login', None)))
        for attempt in range(5):
            with self.assertRaises(ServiceError) as caught:
                self.login(password='invalid-fixture-password')
            self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        failures = self.read("SELECT failures FROM auth_throttles WHERE bucket_key LIKE %s", ('login:%',))
        self.assertEqual(sorted(row[0] for row in failures), [5, 5])
        with self.assertRaises(ServiceError) as caught:
            self.login()
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='auth.login_failed'")[0][0], 6)
        with self.uow() as conn:
            conn.execute("UPDATE auth_throttles SET window_start=clock_timestamp()-interval '16 minutes', "
                         "blocked_until=clock_timestamp()-interval '1 minute' WHERE bucket_key LIKE 'login:%'")
        self.assertEqual(self.login().actor.operator_id, self.actor.operator_id)

    def test_unknown_username_and_wrong_password_both_do_full_scrypt_and_uniform_error(self):
        self.assertTrue(callable(getattr(security, 'login', None)))
        original = security._derive
        for username in (self.username, 'unknown.fixture'):
            with self.subTest(username=username), patch.object(security, '_derive', wraps=original) as derive:
                with self.assertRaises(ServiceError) as caught:
                    self.login(username=username, password='invalid-fixture-password')
                self.assertEqual(str(caught.exception), ErrorCode.UNAUTHENTICATED.value)
                derive.assert_called_once()

    def test_source_bucket_and_username_bucket_are_independent(self):
        self.assertTrue(callable(getattr(security, 'login', None)))
        # Different guessed users must not bypass the source bucket.
        for index in range(5):
            with self.assertRaises(ServiceError):
                self.login(username='missing.' + str(index), password='invalid-fixture-password')
        with self.assertRaises(ServiceError) as caught:
            self.login()
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)
        # The real user has not reached its own limit, so another source works.
        self.assertEqual(self.login(source='127.0.0.2').actor.operator_id, self.actor.operator_id)

    def test_login_audit_failure_rolls_back_new_session_and_preauth_consumption(self):
        grant = self.bootstrap()
        count = self.read('SELECT count(*) FROM operator_sessions')[0][0]
        original = security.audit.append
        def fail_login(*args, **kwargs):
            if args[3] == 'auth.login':
                raise RuntimeError('fixture fault')
            return original(*args, **kwargs)
        with patch('onboarding.audit.append', side_effect=fail_login) as fail:
            with self.assertRaises(ServiceError) as caught:
                self.login(bootstrap=grant)
            self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
            self.assertTrue(any(call.args[3] == 'auth.login' for call in fail.call_args_list))
        self.assertEqual(self.read('SELECT count(*) FROM operator_sessions')[0][0], count)
        self.assertEqual(self.login(bootstrap=grant).actor.operator_id, self.actor.operator_id)

    def test_bounded_preauth_pool_recycles_consumed_records(self):
        self.assertTrue(callable(getattr(security, 'issue_login_bootstrap', None)))
        with patch.object(security, '_PREAUTH_LIMIT', 2):
            first, second = self.bootstrap(), self.bootstrap()
            with self.assertRaises(ServiceError):
                self.bootstrap()
            self.login(bootstrap=first)
            self.bootstrap()
            self.assertEqual(self.read("SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE %s", ('preauth:%',))[0][0], 2)

    def test_logout_checks_csrf_then_revokes_atomically(self):
        self.assertTrue(callable(getattr(security, 'logout', None)))
        with self.assertRaises(ServiceError) as caught:
            security.logout(self.settings, self.token, csrf_token='wrong',
                            origin=self.origin, expected_origin=self.origin)
        self.assertEqual(caught.exception.code, ErrorCode.FORBIDDEN)
        self.assertIsNone(self.read('SELECT revoked_at FROM operator_sessions WHERE id=%s', (self.session_id,))[0][0])
        security.logout(self.settings, self.token, csrf_token=self.csrf,
                        origin=self.origin, expected_origin=self.origin)
        with self.assertRaises(ServiceError) as caught, self.uow() as conn:
            security.revalidate(conn, self.actor, 'onboarding:read')
        self.assertEqual(caught.exception.code, ErrorCode.UNAUTHENTICATED)


class BootstrapCLITests(unittest.TestCase):
    def test_bootstrap_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.bootstrap'))

    def test_non_tty_refused_before_settings_or_password_read(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.bootstrap'))
        from onboarding import bootstrap
        import io
        with patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', new_callable=io.StringIO), \
                patch('onboarding.settings.load_settings') as load, patch('getpass.getpass') as prompt:
            result = bootstrap.main(['operator-create', '--settings', '/not-read', '--permission', 'onboarding:read'])
        self.assertEqual(result, 2)
        load.assert_not_called()
        prompt.assert_not_called()

    def test_password_argv_rejected_without_echoing_unrecognized_value(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.bootstrap'))
        from onboarding import bootstrap
        import io
        with patch('sys.stderr', new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as caught:
                bootstrap.main(['operator-create', '--settings', '/not-read', '--permission', 'onboarding:read',
                                '--password', 'argv-canary-must-not-echo'])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn('canary', output.getvalue())

    def test_tty_flow_uses_getpass_and_explicit_permissions(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.bootstrap'))
        from onboarding import bootstrap
        from contextlib import contextmanager
        import io
        password = 'CLI-fixture-' + 'aA!9' * 5
        @contextmanager
        def uow(settings):
            yield 'fixture-connection'
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), \
                patch('builtins.input', return_value='fixture.cli'), patch('getpass.getpass', side_effect=[password, password]) as prompt, \
                patch('onboarding.settings.load_settings', return_value='fixture-settings'), \
                patch('onboarding.storage.unit_of_work', uow), \
                patch('onboarding.security.create_operator', return_value='fixture-id') as create, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            result = bootstrap.main(['operator-create', '--settings', '/fixture', '--permission', 'onboarding:read'])
        self.assertEqual(result, 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(create.call_args.args, ('fixture-connection', 'fixture.cli', password, ['onboarding:read']))
        self.assertNotIn(password, output.getvalue())


    def test_getpass_echo_fallback_is_refused_before_creation(self):
        from onboarding import bootstrap
        import getpass
        import io
        import warnings
        password = 'CLI-fixture-' + 'aA!9' * 5
        def echo_fallback(prompt):
            warnings.warn('fixture cannot disable echo', getpass.GetPassWarning)
            return password
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), \
                patch('builtins.input', return_value='fixture.cli'), patch('getpass.getpass', side_effect=echo_fallback), \
                patch('onboarding.settings.load_settings', return_value='fixture-settings'), \
                patch('onboarding.security.create_operator') as create, \
                patch('onboarding.storage.unit_of_work') as transaction, \
                warnings.catch_warnings(record=True):
            result = bootstrap.main(['operator-create', '--settings', '/fixture', '--permission', 'onboarding:read'])
        self.assertEqual(result, 2)
        create.assert_not_called()
        transaction.assert_not_called()
