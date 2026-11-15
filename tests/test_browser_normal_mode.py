import asyncio
import inspect
import unittest
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from common import browser


class NormalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self, mode):
        self.assertIn('automation_mode', inspect.signature(browser.open_and_connect).parameters)
        page = SimpleNamespace()
        context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page),
                                  set_extra_http_headers=AsyncMock())
        connected = SimpleNamespace(contexts=[context])
        p = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=connected)))
        bb = MagicMock()
        bb.open_browser.return_value = {'ws': 'ws://secret-cdp-token'}
        with patch.object(browser, 'selected_browser_provider', return_value='bitbrowser'), \
             patch.object(browser, 'BitBrowser', return_value=bb), \
             patch.object(browser, 'create_browser_with_retry', return_value='profile') as create, \
             patch.object(browser, '_prepare_browser_options', return_value={}) as prepare, \
             patch.object(browser, 'install_traffic_saver', AsyncMock()) as traffic, \
             patch.object(browser, 'inject_stealth', AsyncMock()) as stealth, \
             patch('builtins.print') as output:
            kwargs = {} if mode is None else {'automation_mode': mode}
            result = await browser.open_and_connect('fixture', p=p, browser_options={'proxyType': 'http'}, **kwargs)
            return result, prepare, traffic, stealth, output, create

    async def test_normal_skips_stealth_traffic_fingerprint_and_cdp_output(self):
        result, prepare, traffic, stealth, output, create = await self.connect('normal')
        prepare.assert_not_called()
        traffic.assert_not_awaited()
        stealth.assert_not_awaited()
        self.assertNotIn('secret-cdp-token', str(output.call_args_list))
        self.assertEqual(result[1], 'profile')
        self.assertEqual(create.call_args.kwargs['proxyType'], 'http')

    async def test_default_preserves_legacy_hooks(self):
        _, prepare, traffic, stealth, _, _ = await self.connect(None)
        prepare.assert_called_once()
        traffic.assert_awaited_once()
        stealth.assert_awaited_once()

    async def test_invalid_mode_rejected_before_provider(self):
        self.assertIn('automation_mode', inspect.signature(browser.open_and_connect).parameters)
        with patch.object(browser, 'BitBrowser') as factory:
            with self.assertRaises(ValueError):
                await browser.open_and_connect('x', automation_mode='typo')
            factory.assert_not_called()

    async def test_normal_rejects_extreme_provider_launch_filter_before_create(self):
        with patch('common.traffic_saver.configured_mode', return_value='extreme'), \
             patch.object(browser, 'BitBrowser') as factory:
            with self.assertRaisesRegex(RuntimeError, '^BROWSER_OPEN_FAILED$'):
                await browser.open_and_connect('fixture', automation_mode='normal')
            factory.assert_not_called()

    async def test_cloak_normal_skips_application_hooks(self):
        page = SimpleNamespace()
        context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page),
                                  set_extra_http_headers=AsyncMock())
        handle = MagicMock()
        handle.launch = AsyncMock(return_value=(handle, 'cloak-profile', context))
        with patch.object(browser, 'selected_browser_provider', return_value='cloak'), \
             patch('common.cloak_browser.CloakBrowserHandle', return_value=handle), \
             patch.object(browser, 'install_traffic_saver', AsyncMock()) as traffic, \
             patch.object(browser, 'inject_stealth', AsyncMock()) as stealth:
            result = await browser.open_and_connect('fixture', automation_mode='normal')
            self.assertEqual(result[1], 'cloak-profile')
            traffic.assert_not_awaited()
            stealth.assert_not_awaited()

    def test_create_retry_redaction_is_explicit_and_keeps_legacy_logs(self):
        self.assertIn('redact_errors', inspect.signature(browser.create_browser_with_retry).parameters)
        for redacted in (True, False):
            client = MagicMock()
            client.create_browser.side_effect = [RuntimeError('network private-proxy-password'), 'profile']
            with patch('time.sleep'), patch('builtins.print') as output:
                self.assertEqual(browser.create_browser_with_retry(client, 'fixture', redact_errors=redacted), 'profile')
            logs = str(output.call_args_list)
            self.assertEqual('private-proxy-password' in logs, not redacted)
            if redacted:
                output.assert_called_once_with('BROWSER_CREATE_RETRY')
            self.assertNotIn('redact_errors', client.create_browser.call_args.kwargs)

    async def test_normal_create_open_and_cdp_errors_are_fixed_and_owned_profile_cleaned(self):
        for stage in ('create', 'open', 'cdp'):
            with self.subTest(stage=stage):
                client = MagicMock()
                client.create_browser.return_value = 'owned-profile'
                client.open_browser.return_value = {'ws':'ws://private-cdp-token'}
                p = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock()))
                failure = RuntimeError('private-proxy-password ws://private-cdp-token')
                if stage == 'create':
                    client.create_browser.side_effect = failure
                elif stage == 'open':
                    client.open_browser.side_effect = failure
                else:
                    p.chromium.connect_over_cdp.side_effect = failure
                with patch.object(browser, 'selected_browser_provider', return_value='bitbrowser'),                      patch.object(browser, 'BitBrowser', return_value=client),                      patch('builtins.print') as output:
                    with self.assertRaisesRegex(RuntimeError, '^BROWSER_OPEN_FAILED$') as caught:
                        await browser.open_and_connect('fixture', p=p, automation_mode='normal')
                rendered = ''.join(traceback.format_exception(caught.exception)) + str(output.call_args_list)
                self.assertNotIn('private-proxy-password', rendered)
                self.assertNotIn('private-cdp-token', rendered)
                if stage != 'create':
                    client.close_browser.assert_called_once_with('owned-profile')
                    client.delete_browser.assert_called_once_with('owned-profile')

    async def test_normal_open_retry_and_language_warning_hide_provider_text(self):
        context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=SimpleNamespace()),
            set_extra_http_headers=AsyncMock(side_effect=RuntimeError('private-header-secret')))
        p = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
            return_value=SimpleNamespace(contexts=[context]))))
        client = MagicMock()
        client.create_browser.return_value = 'owned-profile'
        client.open_browser.side_effect = [RuntimeError('network private-proxy-password'), {'ws':'ws://private-cdp-token'}]
        with patch.object(browser, 'selected_browser_provider', return_value='bitbrowser'),              patch.object(browser, 'BitBrowser', return_value=client),              patch.object(browser.asyncio, 'sleep', AsyncMock()), patch('builtins.print') as output:
            await browser.open_and_connect('fixture', p=p, automation_mode='normal')
        logs = str(output.call_args_list)
        self.assertNotIn('private-', logs)
        self.assertEqual([args.args[0] for args in output.call_args_list],
                         ['BROWSER_OPEN_RETRY', 'BROWSER_LANGUAGE_SETUP_FAILED'])

    async def test_normal_retains_baseexception_semantics_and_cleanup(self):
        for failure in (asyncio.CancelledError(), SystemExit(7)):
            client = MagicMock()
            client.create_browser.return_value = 'owned-profile'
            client.open_browser.return_value = {'ws':'ws://private-cdp-token'}
            p = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=failure)))
            with patch.object(browser, 'selected_browser_provider', return_value='bitbrowser'),                  patch.object(browser, 'BitBrowser', return_value=client):
                with self.assertRaises(type(failure)) as caught:
                    await browser.open_and_connect('fixture', p=p, automation_mode='normal')
            self.assertIs(caught.exception, failure)
            client.close_browser.assert_called_once_with('owned-profile')
            client.delete_browser.assert_called_once_with('owned-profile')

    async def test_cloak_normal_failure_is_fixed_and_closes_owned_context(self):
        handle = MagicMock()
        handle.context = object()
        handle.launch = AsyncMock(side_effect=RuntimeError('private-cloak-endpoint'))
        handle.close_browser_async = AsyncMock()
        with patch.object(browser, 'selected_browser_provider', return_value='cloak'),              patch('common.cloak_browser.CloakBrowserHandle', return_value=handle):
            with self.assertRaisesRegex(RuntimeError, '^BROWSER_OPEN_FAILED$'):
                await browser.open_and_connect('fixture', automation_mode='normal')
        handle.close_browser_async.assert_awaited_once()
