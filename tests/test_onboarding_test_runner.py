import importlib.util
import unittest
from pathlib import Path

class RunnerTests(unittest.TestCase):
    def test_explicit_no_skip_runner_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.onboarding_test_runner'))

    def test_selected_suites_are_explicit_and_nonempty(self):
        from tools.onboarding_test_runner import selected_modules
        for suite in ('db','security','web','all'):
            self.assertTrue(selected_modules(suite))
        with self.assertRaises(ValueError):
            selected_modules('unexpected')

    def test_skip_is_never_success(self):
        from tools.onboarding_test_runner import passed
        result = unittest.TestResult()
        self.assertTrue(passed(result))
        result.skipped.append(('fixture', 'not allowed'))
        self.assertFalse(passed(result))

    def test_all_suite_covers_every_onboarding_test_module(self):
        from tools.onboarding_test_runner import selected_modules
        files = {path.stem for path in Path(__file__).parent.glob('test_onboarding_*.py')}
        names = selected_modules('all')
        self.assertEqual(set(names), files)
        self.assertEqual(len(names), len(set(names)))

    def test_pure_and_pool_groups_are_explicit(self):
        from tools.onboarding_test_runner import selected_modules
        self.assertEqual(set(selected_modules('offline')), {
            'test_onboarding_dependencies', 'test_onboarding_migration_catalog',
            'test_onboarding_mailbox_parser', 'test_onboarding_pool_secret_types',
            'test_onboarding_request_mac', 'test_onboarding_mailbox_cursor',
            'test_onboarding_pool_contracts', 'test_onboarding_not_committed_guard'})
        self.assertEqual(set(selected_modules('pools')), {
            'test_onboarding_migration_sequence', 'test_onboarding_pool_schema',
            'test_onboarding_pool_vault', 'test_onboarding_mailbox_import',
            'test_onboarding_mailbox_import_races', 'test_onboarding_mailbox_reads',
            'test_onboarding_mailbox_update', 'test_onboarding_mailbox_update_races',
            'test_onboarding_pool_config', 'test_onboarding_pool_config_races',
            'test_onboarding_pool_repository', 'test_onboarding_pool_leases',
            'test_onboarding_pool_commands', 'test_onboarding_pool_batches',
            'test_onboarding_pool_preflight', 'test_onboarding_task_read'})

    def test_web_group_explicitly_includes_pool_http_and_page(self):
        from tools.onboarding_test_runner import selected_modules
        self.assertEqual(set(selected_modules('web')), {
            'test_onboarding_route_guard', 'test_onboarding_routes',
            'test_onboarding_legacy_security', 'test_onboarding_lifecycle',
            'test_onboarding_pages', 'test_onboarding_test_runner',
            'test_onboarding_pool_api', 'test_onboarding_pool_ui'})

    def test_server_fixture_isolates_pool_registration_and_static_samples(self):
        from onboarding_server_support import isolated_server
        with isolated_server() as server:
            self.assertTrue(hasattr(server.app.state.onboarding_config, 'pool_mode'))
            self.assertEqual(server.app.state.onboarding_config.pool_mode, 'off')
            self.assertFalse(server.app.state.onboarding_config.pool_ready)
            server._test_pool_route_register.assert_not_called()
            static = Path(server.WEBUI) / 'static'
            self.assertEqual((static / 'onboarding-pools.html').read_text(), 'fixture-pools-page')
            self.assertEqual((static / 'onboarding-pools.js').read_text(), '// fixture-pools-script')

    def test_actual_off_server_never_constructs_pool_keys(self):
        from unittest.mock import patch
        from onboarding_server_support import isolated_server
        with patch('onboarding.keyring.Keyring', side_effect=AssertionError('pool key construction while off')) as keys, \
             patch('onboarding.request_mac.RequestMac', side_effect=AssertionError('pool MAC construction while off')) as mac:
            with isolated_server('off', {'ONBOARDING_POOL_MODE': 'synthetic',
                    'ONBOARDING_POOL_KEYRING_DIR': '/fixture/private/must-not-open',
                    'ONBOARDING_POOL_KEY_VERSION': 'fixture-version'}) as server:
                server._test_auth_install.assert_not_called()
                server._test_pool_route_register.assert_not_called()
            keys.assert_not_called()
            mac.assert_not_called()

    def test_named_legacy_cases_pass_with_scoped_platform_and_reload_fixtures(self):
        import os
        from tools.onboarding_test_runner import legacy_environment, legacy_case_environment
        cases = (
            'test_webui_update.WebUIUpdateTests.test_frozen_runtime_uses_portable_updater',
            'test_webui_update.WebUIUpdateTests.test_starts_detached_windows_updater',
            'test_webui_env_reload.WebUIEnvReloadTests.test_proxy_save_persists_protocol_link_and_payment_egress',
        )
        host_name = os.name
        with legacy_environment():
            from webui import server
            old_os = server.os
            for name in cases:
                with self.subTest(case=name):
                    result = unittest.TestResult()
                    with legacy_case_environment(name):
                        self.assertEqual(os.name, host_name, 'Never mutate host os.name/pathlib platform')
                        unittest.defaultTestLoader.loadTestsFromName(name).run(result)
                    self.assertTrue(result.wasSuccessful(), str(result.failures + result.errors))
                    self.assertIs(server.os, old_os)
            with legacy_case_environment('test_webui_update.WebUIUpdateTests.test_frozen_macos_runtime_requires_release_update'):
                self.assertIs(server.os, old_os)
