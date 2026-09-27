"""Protected import/lifecycle must not start old helpers or silently become off."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from onboarding_server_support import isolated_server


class LifecycleTests(unittest.TestCase):
    def test_protected_import_skips_git_proxy_and_installs_guard_and_routes(self):
        with isolated_server() as server:
            server._test_import_git.assert_not_called()
            server._test_import_proxy.effective_proxy_url.assert_not_called()
            server._test_auth_install.assert_called_once()
            server._test_route_register.assert_called_once_with(server.app, server.app.state.onboarding_config)
            self.assertEqual(server.index(), 'fixture-protected-page')

    def test_off_import_preserves_version_proxy_and_original_index_without_auth(self):
        with isolated_server('off') as server:
            server._test_import_git.assert_called_once()
            server._test_import_proxy.effective_proxy_url.assert_called_once()
            server._test_auth_install.assert_not_called()
            server._test_route_register.assert_not_called()
            self.assertEqual(server.index(), 'fixture-legacy-page')

    def test_invalid_mode_fails_closed_before_legacy_import_side_effects(self):
        for mode in ('PROTECTED', 'oops', '', 'off\n'):
            with self.subTest(mode=mode):
                with self.assertRaises(RuntimeError) as caught:
                    with isolated_server(mode):
                        pass
                self.assertEqual(str(caught.exception), 'INVALID_ONBOARDING_MODE')

    def test_protected_startup_shutdown_do_not_touch_old_services_or_browsers(self):
        with isolated_server() as server:
            with patch.object(server, '_read_config_val', return_value='1') as read, \
                 patch.object(server, '_k12_alive', return_value=False) as alive, \
                 patch.object(server, '_start_k12_service', new=AsyncMock()) as start, \
                 patch.object(server, '_stop_k12_service', new=AsyncMock()) as stop, \
                 patch.object(server, '_stop_plus_service_sync') as plus, \
                 patch.object(server, '_cleanup_registered_browser_profiles') as cleanup:
                asyncio.run(server.startup_local_services())
                asyncio.run(server.shutdown_local_services())
                for spy in (read, alive, start, stop, plus, cleanup):
                    spy.assert_not_called()

    def test_off_shutdown_preserves_old_cleanup(self):
        with isolated_server('off') as server:
            with patch.object(server, '_stop_k12_service', new=AsyncMock()) as stop, \
                 patch.object(server, '_stop_plus_service_sync') as plus, \
                 patch.object(server, '_cleanup_registered_browser_profiles') as cleanup:
                asyncio.run(server.shutdown_local_services())
                stop.assert_awaited_once()
                plus.assert_called_once()
                cleanup.assert_called_once()

    def test_protected_pages_are_real_asgi_routes_and_off_does_not_mount_aliases(self):
        import httpx
        for mode in ('protected', 'off'):
            with isolated_server(mode) as server:
                async def requests():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='https://fixture.test') as client:
                        return await client.get('/login'), await client.get('/onboarding')
                login, page = asyncio.run(requests())
                if mode == 'protected':
                    self.assertEqual((login.status_code, login.text), (200, 'fixture-login-page'))
                    self.assertEqual((page.status_code, page.text), (200, 'fixture-protected-page'))
                else:
                    self.assertEqual(login.status_code, 404)
                    self.assertEqual(page.status_code, 404)
