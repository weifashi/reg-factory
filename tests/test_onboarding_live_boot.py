"""Live HTTP boot is explicit, local-only, and never starts a persisted run."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest
import importlib
import importlib.util
import json
import os
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from onboarding import security
from onboarding.settings import Settings, SOCKET
from webui import onboarding_auth

ID = '12345678-1234-4234-9234-123456789abc'
ORIGIN = 'https://fixture.local'


class LiveBootTests(unittest.TestCase):
    def settings(self):
        return Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
                        'fixture-only-placeholder', 'rf-onboarding-p1b-v1:fixture', 'rf_onboarding')

    def test_live_install_uses_distinct_policy_and_owned_shutdown_without_start(self):
        app = FastAPI()
        manager = SimpleNamespace(shutdown=AsyncMock(), submit=AsyncMock())
        values = dict(live_policy=object(), live_vault=object(), live_source=object(),
                      live_manager=manager, live_mailbox_id=ID)
        with patch('webui.onboarding_auth._live_context', return_value=values, create=True) as live, \
             patch('webui.onboarding_auth._pool_context') as synthetic:
            config = onboarding_auth.install(app, mode='protected', expected_origin=ORIGIN,
                settings=self.settings(), pool_mode='live', pool_keyring=object(), pool_mac=object())
            self.assertTrue(config.ready)
            self.assertTrue(config.pool_ready)
            self.assertIs(config.live_manager, manager)
            self.assertIsNone(config.pool_policy)
            self.assertIsNone(config.pool_vault)
            with TestClient(app, base_url=ORIGIN):
                manager.submit.assert_not_called()
                manager.shutdown.assert_not_called()
        live.assert_called_once()
        synthetic.assert_not_called()
        manager.shutdown.assert_awaited_once()

    def test_live_failed_dependencies_leave_auth_available_but_live_api_closed(self):
        app = FastAPI()
        with patch('webui.onboarding_auth._live_context', side_effect=ValueError, create=True):
            config = onboarding_auth.install(app, mode='protected', expected_origin=ORIGIN,
                settings=self.settings(), pool_mode='live')
        self.assertTrue(config.ready)
        self.assertFalse(config.pool_ready)
        with TestClient(app, base_url=ORIGIN) as client:
            self.assertEqual(client.get('/healthz').status_code, 200)
            self.assertEqual(client.get('/api/onboarding/live/capabilities').status_code, 503)
            self.assertEqual(client.post('/api/onboarding/live/future', headers={'Origin':ORIGIN}).status_code, 403)

    def test_live_allowlist_never_unlocks_synthetic_writes_or_legacy(self):
        app = FastAPI()
        paths = [('GET','/api/onboarding/live/capabilities'), ('POST','/api/onboarding/live/start'),
                 ('GET','/api/onboarding/config'), ('GET','/api/onboarding/tasks')]
        for verb,path in paths:
            app.add_api_route(path, lambda: {'reached':True}, methods=[verb])
        cfg = onboarding_auth.WebConfig('protected', ORIGIN, self.settings(), True, 'live', True)
        app.add_middleware(onboarding_auth.ProtectionMiddleware, config=cfg)
        actor = security.Actor(ID,frozenset({'onboarding:read','tasks:manage','legacy:admin'}),ID,1)
        with patch('webui.onboarding_auth._authenticate_request',return_value=actor), \
             TestClient(app,base_url=ORIGIN) as client:
            for verb,path in paths:
                self.assertEqual(client.request(verb,path,headers={'Origin':ORIGIN,'Content-Type':'application/json'}).status_code,200,path)
            for verb,path in [('POST','/api/onboarding/batches'),('POST','/api/onboarding/preflight'),
                ('POST','/api/onboarding/mailboxes/import'),('PATCH','/api/onboarding/mailboxes/'+ID),
                ('GET','/api/onboarding/live/future'),('HEAD','/api/onboarding/live/capabilities')]:
                self.assertEqual(client.request(verb,path,headers={'Origin':ORIGIN}).status_code,403,path)
            self.assertEqual(client.post('/api/run',headers={'Origin':ORIGIN,'Content-Type':'application/json'}).status_code,503)

    def test_original_shell_exposes_only_explicit_render_mode(self):
        from webui.onboarding_console import render_console
        static = Path(__file__).parents[1]/'webui/static'
        html = render_console((static/'index.html').read_text(),
            pool_source=(static/'onboarding-pools.html').read_text(), pool_mode='live')[0]
        self.assertIn('data-pool-mode="live"', html)
        self.assertEqual(html.count('id="main"'),1)
        with self.assertRaises(ValueError):
            render_console((static/'index.html').read_text(), pool_mode='arbitrary')

    def test_actual_server_live_boot_preserves_old_protected_configuration_entry(self):
        from onboarding_server_support import isolated_server
        with isolated_server(values={'ONBOARDING_POOL_MODE':'live',
                                     'ONBOARDING_LIVE_BINDING_FILE':'/trusted/binding.json'}) as server:
            self.assertEqual(server._test_auth_install.call_args.kwargs['pool_mode'],'live')
            self.assertEqual(server._test_auth_install.call_args.kwargs['live_binding_path'],'/trusted/binding.json')
            self.assertEqual(server.index().status_code,503)
            self.assertEqual(server.onboarding_diagnostics_page(),'fixture-protected-page')

    def test_actual_server_rejects_unprotected_live_mode(self):
        from onboarding_server_support import isolated_server
        with self.assertRaisesRegex(RuntimeError,'INVALID_ONBOARDING_POOL_MODE'):
            with isolated_server(mode='off',values={'ONBOARDING_POOL_MODE':'live'}):
                self.fail('unprotected live boot')


class LiveBindingTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('webui.onboarding_live_boot'))
        return importlib.import_module('webui.onboarding_live_boot')

    def test_private_binding_exact_fields_no_account_url_or_arbitrary_override(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'binding.json'
            body = dict(owner_id=ID, data_root=root, mailbox_id=None, sub2api_config_file=None)
            def write(value):
                path.write_text(json.dumps(value)); path.chmod(0o600)
            write(body)
            self.assertEqual(m.load_binding(str(path)), body)
            for changes in ({'owner_id':True}, {'owner_id':ID.upper()}, {'data_root':'relative'},
                {'data_root':root+'/../x'}, {'mailbox_id':True}, {'mailbox_id':ID},
                {'sub2api_config_file':root+'/admin.json'}, {'account':'someone@example.com'},
                {'sdk_path':'/tmp/sdk'}, {'url':'http://evil.example'}):
                write({**body,**changes})
                with self.assertRaises(ValueError):
                    m.load_binding(str(path))
            write({**body,'mailbox_id':ID,'sub2api_config_file':root+'/admin.json'})
            self.assertEqual(m.load_binding(str(path))['mailbox_id'],ID)

    def test_binding_rejects_bad_permissions_links_duplicate_keys_and_secret_errors(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'binding.json'; path.write_text('{"owner_id":"SECRET_CANARY","owner_id":"x"}')
            path.chmod(0o600)
            for candidate in (path,):
                with self.assertRaises(ValueError) as caught: m.load_binding(str(candidate))
                self.assertNotIn('SECRET_CANARY',str(caught.exception))
            body=dict(owner_id=ID,data_root=root,mailbox_id=None,sub2api_config_file=None)
            path.write_text(json.dumps(body)); path.chmod(0o644)
            with self.assertRaises(ValueError): m.load_binding(str(path))
            path.chmod(0o600)
            link=Path(root)/'link'; link.symlink_to(path)
            with self.assertRaises(ValueError): m.load_binding(str(link))

    def test_missing_binding_is_read_ready_not_an_implicit_source_or_manager(self):
        m=self.module()
        # The validated key/catalog layer is a local seam. No source or provider
        # constructor should be reached for the explicitly unbound UI.
        policy,vault=object(),object()
        with patch.object(m,'_base',return_value=(policy,vault)) as base, \
             patch.object(m,'OwnedMailboxSource',side_effect=AssertionError('implicit source')), \
             patch.object(m,'LiveRunManager',side_effect=AssertionError('implicit worker')):
            result=m.prepare(object(),object(),object(),None)
        self.assertEqual(result,dict(live_policy=policy,live_vault=vault,
            live_source=None,live_manager=None,live_mailbox_id=None))
        base.assert_called_once()

    def test_source_only_binding_never_loads_sub2api_or_constructs_manager(self):
        m=self.module()
        body=dict(owner_id=ID,data_root='/tmp/authorized-source',mailbox_id=None,sub2api_config_file=None)
        with patch.object(m,'_base',return_value=(object(),object())), \
             patch.object(m,'load_binding',return_value=body), \
             patch.object(m,'load_sub2api_config',side_effect=AssertionError('no target yet')), \
             patch.object(m,'LiveRunManager',side_effect=AssertionError('no run yet')):
            result=m.prepare(object(),object(),object(),'/trusted/binding.json')
        self.assertEqual(result['live_source'].email,'weifashi68@gmail.com')
        self.assertEqual(result['live_source'].owner_id,ID)
        self.assertIsNone(result['live_manager'])

    def test_bound_manager_is_fixed_validated_and_never_submitted_at_boot(self):
        m=self.module()
        settings,keyring,mac,policy,vault,store,target,manager=[object() for _ in range(8)]
        body=dict(owner_id=ID,data_root='/tmp/authorized-source',mailbox_id=ID,sub2api_config_file='/trusted/admin.json')
        with patch.object(m,'_base',return_value=(policy,vault)), \
             patch.object(m,'load_binding',return_value=body), \
             patch.object(m,'load_sub2api_config',return_value=target) as load, \
             patch.object(m,'ServiceAccountKeyStore',return_value=store) as keys, \
             patch.object(m,'validate_dependencies') as ready, \
             patch.object(m,'LiveRunManager',return_value=manager) as factory:
            result=m.prepare(settings,keyring,mac,'/trusted/binding.json')
        load.assert_called_once_with('/trusted/admin.json',allow_loopback_http=True)
        keys.assert_called_once_with(settings,keyring)
        ready.assert_called_once()
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args,(settings,ID))
        self.assertIs(factory.call_args.kwargs['sub2api_config'],target)
        self.assertEqual(factory.call_args.kwargs['google_auth_config'].account,'weifashi68@gmail.com')
        self.assertIs(result['live_manager'],manager)
