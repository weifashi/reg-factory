"""Synthetic-only defense-in-depth for legacy web environment and execution."""
import asyncio
from contextlib import contextmanager
import inspect
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from onboarding_server_support import FakeRequest, isolated_server


@contextmanager
def authorized(server):
    from onboarding import security, storage
    from contextlib import nullcontext
    with patch.object(storage, 'unit_of_work', return_value=nullcontext(object())), \
         patch.object(security, 'revalidate', return_value=object()) as check:
        yield FakeRequest(actor=object()), check


class LegacySecurityTests(TestCase):
    def test_env_requires_service_actor_even_when_directly_called(self):
        with isolated_server() as s:
            self.assertEqual(s.api_env_get(FakeRequest()).status_code, 401)
            self.assertEqual(asyncio.run(s.api_env_set(FakeRequest({'env': {}}))).status_code, 401)

    def test_protected_env_redacts_secrets_and_credential_urls(self):
        with isolated_server() as s, authorized(s) as (req, check):
            with open(s.ENV_PATH, 'a') as f:
                f.write('CLASH_PROXY=http://fixture-user:fixture-url-canary@proxy.test\n')
            result = s.api_env_get(req)
            items = {i['key']: i for g in result['groups'] for i in g['items']}
            for key in ('CLASH_SECRET', 'CLASH_PROXY'):
                self.assertEqual(items[key]['value'], '')
                self.assertTrue(items[key]['configured'])
                self.assertTrue(items[key]['secret'])
            self.assertNotIn('fixture-env-canary', json.dumps(result))
            self.assertNotIn('fixture-url-canary', json.dumps(result))
            check.assert_called_once()

    def test_secret_actions_keep_replace_clear_without_runtime_adapters(self):
        with isolated_server() as s, authorized(s) as (req, check), patch('importlib.reload') as reload:
            for action, value, expected in [('keep', None, 'fixture-env-canary'),
                                           ('replace', 'fixture-new', 'fixture-new'), ('clear', None, '')]:
                dto = {'action': action}
                if value is not None: dto['value'] = value
                req.data = {'env': {'CLASH_SECRET': dto}}
                result = asyncio.run(s.api_env_set(req))
                self.assertTrue(result['ok'])
                self.assertFalse(result['effective_now'])
                self.assertEqual(s._parse_env_file(s.ENV_PATH)['CLASH_SECRET'], expected)
            reload.assert_not_called()
            s._test_import_proxy.effective_proxy_url.assert_not_called()

    def test_invalid_payload_is_atomic_and_never_clears_secret(self):
        invalids = [{'CLASH_SECRET': ''}, {'CLASH_SECRET': {'action': 'replace', 'value': ''}},
                    {'CLASH_API': 'x\nEVIL=y'}, {'CLASH_API': 'x\rEVIL=y'}, {'CLASH_API': 'x\x00y'},
                    {'CLASH_API': {'action': 'bad'}}, {'unknown': 'x'},
                    {'ONBOARDING_MODE': 'off'}, {'RF_ONBOARDING_DSN': 'fixture'},
                    {'CLASH_SECRET': {'action': 'keep', 'value': 'fixture'}}]
        with isolated_server() as s, authorized(s) as (req, check):
            before = Path(s.ENV_PATH).read_bytes()
            for bad in invalids:
                with self.subTest(bad=bad):
                    req.data = {'env': {'K12_AUTO_START': '1', **bad}}
                    response = asyncio.run(s.api_env_set(req))
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(Path(s.ENV_PATH).read_bytes(), before)

    def test_both_modes_reject_security_config_writes(self):
        for mode in ('off', 'protected'):
            with isolated_server(mode, {'ONBOARDING_DSN_ENV': 'FIXTURE_DSN'}) as s, authorized(s) as (req, check):
                for key in ('ONBOARDING_MODE', 'RF_ONBOARDING_KEY', 'FIXTURE_DSN',
                            'ONBOARDING_POOL_MODE', 'ONBOARDING_POOL_KEYRING_DIR',
                            'ONBOARDING_POOL_KEY_VERSION'):
                    req.data = {'env': {key: 'fixture'}}
                    self.assertEqual(asyncio.run(s.api_env_set(req)).status_code, 422 if s._PROTECTED_MODE else 400)

    def test_child_and_updater_strip_security_env_after_all_merges(self):
        for mode in ('off', 'protected'):
            with isolated_server(mode, {'ONBOARDING_DSN_ENV': 'FIXTURE_DSN', 'FIXTURE_DSN': 'fixture:dsn',
                                        'RF_ONBOARDING_SECRET_ENV_KEYS': 'FIXTURE_KEY,FIXTURE_PASS',
                                        'FIXTURE_KEY': 'fixture:key', 'FIXTURE_PASS': 'fixture:pass',
                                        'ONBOARDING_POOL_MODE': 'off',
                                        'ONBOARDING_POOL_KEYRING_DIR': '/fixture/private/keyring',
                                        'ONBOARDING_POOL_KEY_VERSION': 'fixture-version',
                                        'UNRELATED': 'fixture:keep'}) as s:
                with open(s.ENV_PATH, 'a') as f:
                    f.write('ONBOARDING_SAVED_SECRET_ENV=FIXTURE_SAVED\nFIXTURE_SAVED=fixture:saved\n')
                for env in (s._child_env(), s._update_child_env()):
                    self.assertEqual(env['UNRELATED'], 'fixture:keep')
                    self.assertFalse(any(k.startswith(('ONBOARDING_', 'RF_ONBOARDING_')) for k in env))
                    self.assertFalse({'FIXTURE_DSN','FIXTURE_KEY','FIXTURE_PASS','FIXTURE_SAVED'} & set(env))

    def test_every_legacy_endpoint_and_direct_handler_blocks_before_execution(self):
        with isolated_server() as s:
            count = 0
            for route in s.app.routes:
                endpoint = getattr(route, 'endpoint', None)
                if endpoint is None or endpoint.__module__ != s.__name__ or endpoint.__name__ in {'index','api_env_get','api_env_set','onboarding_login_page'}:
                    continue
                count += 1
                # Wrapper must fail before it tries to read required request/provider inputs.
                result = endpoint()
                if inspect.isawaitable(result): result = asyncio.run(result)
                self.assertEqual(result.status_code, 503, route.path)
                self.assertEqual(json.loads(result.body)['code'], 'LEGACY_EXECUTION_BLOCKED')
                self.assertIs(route.dependant.call, endpoint)
                self.assertIs(getattr(s, endpoint.__name__), endpoint)
            self.assertEqual(count, 60)

    def test_proxy_helper_rejects_before_startup_or_forwarding_credentials(self):
        with isolated_server() as s, patch.object(s, '_plus_runtime_environment') as runtime:
            req = FakeRequest(); req.headers = {'Cookie': 'fixture:session', 'Authorization': 'fixture:token'}
            result = asyncio.run(s._proxy_local_plus(req, 'fixture'))
            self.assertEqual(result.status_code, 503)
            runtime.assert_not_called()

    def test_off_env_keeps_legacy_projection_and_scalar_save(self):
        with isolated_server('off') as s, patch.object(s, '_apply_saved_env'):
            items = {i['key']: i for g in s.api_env_get()['groups'] for i in g['items']}
            self.assertEqual(items['CLASH_SECRET']['value'], 'fixture-env-canary')
            result = asyncio.run(s.api_env_set(FakeRequest({'env': {'K12_AUTO_START': 1, 'unknown': 'ignored'}})))
            self.assertTrue(result['effective_now'])
            self.assertEqual(s._parse_env_file(s.ENV_PATH)['K12_AUTO_START'], '1')

    def test_expired_or_revoked_actor_blocks_reads_and_save_before_file_mutation(self):
        from onboarding.errors import ServiceError, ErrorCode
        with isolated_server() as s, authorized(s) as (req, check):
            before = Path(s.ENV_PATH).read_bytes()
            check.side_effect = ServiceError(ErrorCode.UNAUTHENTICATED)
            self.assertEqual(s.api_env_get(req).status_code, 401)
            req.data = {'env': {'K12_AUTO_START': '1'}}
            self.assertEqual(asyncio.run(s.api_env_set(req)).status_code, 401)
            self.assertEqual(Path(s.ENV_PATH).read_bytes(), before)
            check.side_effect = [object(), ServiceError(ErrorCode.FORBIDDEN)]
            self.assertEqual(asyncio.run(s.api_env_set(req)).status_code, 403)
            self.assertEqual(Path(s.ENV_PATH).read_bytes(), before)

    def test_secret_query_url_requires_explicit_action_and_is_redacted(self):
        with isolated_server() as s, authorized(s) as (req, check):
            req.data = {'env': {'CLASH_API': 'https://fixture.test/?api_key=fixture-query-canary'}}
            self.assertEqual(asyncio.run(s.api_env_set(req)).status_code, 422 if s._PROTECTED_MODE else 400)
            req.data['env']['CLASH_API'] = {'action': 'replace', 'value': 'https://fixture.test/?api_key=fixture-query-canary'}
            self.assertTrue(asyncio.run(s.api_env_set(req))['ok'])
            self.assertNotIn('fixture-query-canary', json.dumps(s.api_env_get(req)))

    def test_malformed_envelope_and_controls_in_off_mode_are_atomic(self):
        for mode in ('off', 'protected'):
            with isolated_server(mode) as s, authorized(s) as (req, check):
                before = Path(s.ENV_PATH).read_bytes()
                for data in ([1], {'env': []}, {'env': {'CLASH_API': 'fixture\nINJECTED=1'}}):
                    req.data = data
                    self.assertEqual(asyncio.run(s.api_env_set(req)).status_code, 422 if s._PROTECTED_MODE else 400)
                    self.assertEqual(Path(s.ENV_PATH).read_bytes(), before)

    def test_protected_env_uses_strict_bounded_json_and_fixed_error_contract(self):
        from unittest.mock import AsyncMock
        with isolated_server() as s, authorized(s) as (req, check):
            before = Path(s.ENV_PATH).read_bytes()
            for raw in (b'{"env":{},"unknown":1}', b'{"env":{},"env":{}}',
                        b'{"env":{"K12_AUTO_START":NaN}}', b'\xff', b' ' * 8193):
                async def stream():
                    yield raw
                req.stream = stream
                req.json = AsyncMock(return_value={'env': {}})
                response = asyncio.run(s.api_env_set(req))
                self.assertEqual(response.status_code, 422)
                body = json.loads(response.body)
                self.assertEqual(set(body), {'code', 'correlation_id'})
                self.assertEqual(body['code'], 'INVALID_INPUT')
                req.json.assert_not_called()
                self.assertEqual(Path(s.ENV_PATH).read_bytes(), before)

    def test_credential_url_fragment_is_never_returned(self):
        with isolated_server() as s, authorized(s) as (req, check):
            with open(s.ENV_PATH, 'a') as stream:
                stream.write('CLASH_PROXY=https://fixture.test/#access_token=fixture-fragment-canary\n')
            self.assertNotIn('fixture-fragment-canary', json.dumps(s.api_env_get(req)))

    def test_any_query_or_fragment_url_is_sensitive_without_guessing_parameter_names(self):
        with isolated_server() as s, authorized(s) as (req, check):
            for suffix in ('?auth=fixture-short-canary', '?sig=fixture-short-canary', '?custom=fixture-short-canary', '#custom=fixture-short-canary'):
                with open(s.ENV_PATH, 'a') as stream:
                    stream.write('CLASH_API=https://fixture.test/' + suffix + '\n')
                self.assertNotIn('fixture-short-canary', json.dumps(s.api_env_get(req)))
