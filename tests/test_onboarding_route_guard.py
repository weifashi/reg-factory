"""Isolated ASGI protection tests: never import legacy server or real env."""
import ast
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class GuardTests(unittest.TestCase):
    def module(self):
        from webui import onboarding_auth
        return onboarding_auth

    def app(self, mode='protected', settings=None):
        app = FastAPI()
        @app.get('/unregistered')
        def unknown():
            return {'must_not': 'escape'}
        @app.get('/')
        def home():
            return {'ok': True}
        self.module().install(app, mode=mode, expected_origin='https://fixture.local', settings=settings)
        return app

    def test_off_leaves_routes_and_imports_untouched(self):
        with patch('builtins.__import__', wraps=__import__) as imports:
            app = self.app('off')
        self.assertFalse(any(c.args[0].startswith('onboarding.') for c in imports.call_args_list))
        with TestClient(app, base_url='https://fixture.local') as client:
            self.assertEqual(client.get('/unregistered').status_code, 200)
            self.assertEqual(client.get('/api/auth/session').status_code, 404)

    def test_invalid_config_fails_closed_including_public_assets(self):
        with TestClient(self.app(), base_url='https://fixture.local') as client:
            for path in ('/', '/unregistered', '/login', '/static/onboarding.css', '/docs'):
                response = client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(set(response.json()), {'code', 'correlation_id'})

    def valid_settings(self):
        from onboarding.settings import Settings, SOCKET
        return Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
                        'fixture-only-placeholder', 'rf-onboarding-p1b-v1:fixture', 'rf_onboarding')

    def test_unknown_and_wrong_methods_denied_without_database(self):
        with TestClient(self.app(settings=self.valid_settings()), base_url='https://fixture.local') as client:
            for method, path in [('GET','/unregistered'), ('POST','/login'), ('HEAD','/'),
                                 ('GET','/static/app.js')]:
                response = client.request(method,path)
                self.assertEqual(response.status_code,403)
                if method != 'HEAD':
                    self.assertEqual(response.json()['code'],'FORBIDDEN')

    def test_public_allowlist_transport_and_no_forwarded_trust(self):
        with TestClient(self.app(settings=self.valid_settings()), base_url='https://fixture.local') as client:
            self.assertEqual(client.get('/login').status_code,422)  # sanitized missing UI fixture route
            for headers in ({'host':'evil.local'}, {'origin':'https://evil.local'},
                            {'sec-fetch-site':'cross-site'}, {'host':'evil.local','x-forwarded-host':'fixture.local'}):
                self.assertEqual(client.get('/login',headers=headers).status_code,403)
            self.assertEqual(client.get('http://fixture.local/login',headers={'x-forwarded-proto':'https'}).status_code,403)

    def test_websocket_rejected(self):
        from starlette.websockets import WebSocketDisconnect
        with TestClient(self.app(settings=self.valid_settings()), base_url='https://fixture.local') as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/ws'): pass

    def test_fixed_legacy_enumeration_covers_baseline_not_future_routes(self):
        module = self.module()
        source = ast.parse((Path(__file__).parents[1]/'webui/server.py').read_text())
        pairs = set()
        for node in ast.walk(source):
            if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)): continue
            for dec in node.decorator_list:
                if not isinstance(dec,ast.Call) or not isinstance(dec.func,ast.Attribute): continue
                if not isinstance(dec.func.value,ast.Name) or dec.func.value.id!='app': continue
                if dec.func.attr not in ('get','post','api_route'): continue
                path = ast.literal_eval(dec.args[0])
                if path in ('/','/api/env') or path.startswith('/api/auth/') or path.startswith('/api/onboarding') or path in ('/login','/onboarding'): continue
                methods = [dec.func.attr.upper()] if dec.func.attr!='api_route' else ast.literal_eval(next(k.value for k in dec.keywords if k.arg=='methods'))
                pairs.update((method,path) for method in methods)
        self.assertTrue(pairs)
        self.assertEqual(pairs,set(module.LEGACY_ROUTES))

    def test_liveness_and_security_headers_without_database(self):
        with patch('onboarding.storage.unit_of_work',side_effect=AssertionError('no DB for liveness')):
            with TestClient(self.app(settings=self.valid_settings()),base_url='https://fixture.local') as client:
                response=client.get('/healthz')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json(),{'alive':True})
                for path in ('/healthz','/unregistered'):
                    response=client.get(path)
                    self.assertEqual(response.headers['referrer-policy'],'no-referrer')
                    self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
                    self.assertIn("script-src 'self'",response.headers['content-security-policy'])
                    self.assertEqual(response.headers['cache-control'],'no-store')


class AuthRouterTests(unittest.TestCase):
    """Network-boundary tests mock only DB services; no legacy app import."""
    module = GuardTests.module
    app = GuardTests.app
    valid_settings = GuardTests.valid_settings

    def setUp(self):
        rate = patch('webui.onboarding_auth._http_rate_limit', return_value=True, create=True)
        rate.start()
        self.addCleanup(rate.stop)
    def client(self):
        return TestClient(self.app(settings=self.valid_settings()), base_url='https://fixture.local',
                          client=('127.0.0.1',12345))

    def test_bootstrap_no_origin_sets_only_secure_preauth(self):
        from onboarding.security import LoginBootstrap
        from webui.onboarding_auth import PREAUTH_COOKIE
        with patch('onboarding.security.issue_login_bootstrap', return_value=LoginBootstrap('p'*43,'c'*43)) as issue:
            with self.client() as client:
                response = client.get('/api/auth/bootstrap')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'csrf_token':'c'*43})
        self.assertNotIn('p'*43,response.text)
        issue.assert_called_once()
        cookie = response.headers['set-cookie']
        self.assertIn(PREAUTH_COOKIE,cookie)
        for flag in ('Secure','HttpOnly','SameSite=strict','Path=/'): self.assertIn(flag,cookie)
        self.assertNotIn('Domain=',cookie)

    def test_login_rotation_trusted_source_and_no_token_json(self):
        from onboarding.security import Actor, LoginResult
        from webui.onboarding_auth import SESSION_COOKIE, PREAUTH_COOKIE, CSRF_COOKIE
        result = LoginResult(Actor('operator',frozenset({'onboarding:read'}),'session',1),'s'*43,'c'*43)
        with patch('onboarding.security.login',return_value=result) as login:
            with self.client() as client:
                client.cookies.set(PREAUTH_COOKIE,'p'*43)
                client.cookies.set(SESSION_COOKIE,'old')
                response=client.post('/api/auth/login',json={'username':'fixture-user','password':'fixture-not-a-secret'},
                                     headers={'Origin':'https://fixture.local','X-CSRF-Token':'c'*43,
                                              'X-Forwarded-For':'1.2.3.4'})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('s'*43,response.text)
        self.assertEqual(login.call_args.args[3],'127.0.0.1')
        self.assertEqual(login.call_args.kwargs['previous_token'],'old')
        cookies=response.headers.get_list('set-cookie')
        self.assertTrue(any(c.startswith(CSRF_COOKIE+'=') for c in cookies))
        self.assertTrue(any(c.startswith(SESSION_COOKIE+'=') for c in cookies))
        self.assertTrue(all('HttpOnly' in c and 'Secure' in c and 'SameSite=strict' in c for c in cookies))

    def test_login_bad_json_and_fields_never_call_login(self):
        with patch('onboarding.security.login') as login:
            with self.client() as client:
                for raw in ('{"username":"x","username":"y","password":"z"}',
                            '{"username":"x","password":NaN}', '[]',
                            '{"username":"x","password":"z","permissions":["legacy:admin"]}',
                            '{"username":3,"password":"z"}', '{"username":"'+'x'*9000+'"}'):
                    response=client.post('/api/auth/login',content=raw,
                                         headers={'Origin':'https://fixture.local','Content-Type':'application/json',
                                                  'X-CSRF-Token':'c'*43})
                    self.assertEqual(response.status_code,422)
                    self.assertEqual(set(response.json()),{'code','correlation_id'})
        login.assert_not_called()

    def test_unsafe_requires_origin_json_before_login(self):
        with patch('onboarding.security.login') as login:
            with self.client() as client:
                for headers in ({}, {'Origin':'https://evil.local'},
                                {'Origin':'https://fixture.local','Content-Type':'text/plain'}):
                    self.assertEqual(client.post('/api/auth/login',content='{}',headers=headers).status_code,403)
        login.assert_not_called()

    def test_uniform_login_error_never_echoes_exception_or_body(self):
        from onboarding.errors import ServiceError, ErrorCode
        for error, code in ((ServiceError(ErrorCode.UNAUTHENTICATED),401),
                            (RuntimeError('do-not-leak-fixture-detail'),503)):
            with patch('onboarding.security.login',side_effect=error):
                with self.client() as client:
                    response=client.post('/api/auth/login',json={'username':'fixture-user','password':'fixture-not-a-secret'},
                                         headers={'Origin':'https://fixture.local','X-CSRF-Token':'c'*43})
            self.assertEqual(response.status_code,code)
            self.assertEqual(set(response.json()),{'code','correlation_id'})
            self.assertNotIn('fixture',response.text)

    def test_permission_and_csrf_transactions_close_before_handler(self):
        from onboarding.security import Actor
        from webui.onboarding_auth import install
        events=[]
        @contextmanager
        def uow(settings):
            events.append('begin'); yield object(); events.append('end')
        app=FastAPI()
        @app.post('/api/env')
        def business():
            events.append('handler'); return {'ok':True}
        install(app,mode='protected',expected_origin='https://fixture.local',settings=self.valid_settings())
        actor=Actor('operator',frozenset(),'session',1)
        with patch('onboarding.storage.unit_of_work',uow), patch('onboarding.security.require',return_value=actor) as require, patch('onboarding.security.check_csrf') as csrf:
            with TestClient(app,base_url='https://fixture.local') as client:
                response=client.post('/api/env',json={},headers={'Origin':'https://fixture.local','X-CSRF-Token':'c'*43})
        self.assertEqual(response.status_code,200)
        self.assertEqual(events,['begin','end','handler'])
        self.assertEqual(require.call_args.args[-1],'config:manage')
        csrf.assert_called_once()

    def test_legacy_admin_still_blocked_and_dynamic_routes_match(self):
        from onboarding.security import Actor
        from webui.onboarding_auth import _policy
        for path in ('/api/logs/run1','/api/assets/cookies/platform','/chatgpt-plus/nested/a','/api/chatgpt-plus/workbench/a/b'):
            self.assertEqual(_policy('GET',path),('legacy','legacy:admin'))
        with patch('webui.onboarding_auth._authenticate_request',return_value=Actor('operator',frozenset({'legacy:admin'}),'session',1)):
            with self.client() as client:
                response=client.get('/api/status')
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['code'],'LEGACY_EXECUTION_BLOCKED')

    def test_navigation_redirect_only_known_pages_no_api_redirect(self):
        from onboarding.errors import ServiceError,ErrorCode
        with patch('webui.onboarding_auth._authenticate_request',side_effect=ServiceError(ErrorCode.UNAUTHENTICATED)):
            with self.client() as client:
                response=client.get('/',headers={'Accept':'text/html'},follow_redirects=False)
                self.assertEqual(response.status_code,303)
                self.assertEqual(response.headers['location'],'/login?reason=expired')
                response=client.get('/api/auth/session',headers={'Accept':'text/html'},follow_redirects=False)
                self.assertEqual(response.status_code,401)

    def test_empty_origin_bad_referer_and_duplicate_headers_denied(self):
        with self.client() as client:
            for headers in ({'Origin':''}, {'Referer':'https://['},
                            [('Host','fixture.local'),('Host','fixture.local')],
                            [('Origin','https://fixture.local'),('Origin','https://fixture.local')]):
                response=client.get('/login',headers=headers)
                self.assertEqual(response.status_code,403)

    def test_source_rate_limit_is_429_without_auth_call(self):
        with patch('webui.onboarding_auth._http_rate_limit',return_value=False,create=True) as limit, patch('onboarding.security.issue_login_bootstrap') as issue:
            with self.client() as client:
                response=client.get('/api/auth/bootstrap')
        self.assertEqual(response.status_code,429)
        self.assertEqual(response.json()['code'],'RATE_LIMITED')
        issue.assert_not_called()
        limit.assert_called_once()

    def test_auth_query_credentials_are_not_accepted(self):
        with patch('onboarding.security.login') as login:
            with self.client() as client:
                response=client.post('/api/auth/login?password=fixture-query-canary',
                                     json={'username':'fixture','password':'fixture-body'},
                                     headers={'Origin':'https://fixture.local','X-CSRF-Token':'c'*43})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('canary',response.text)
        login.assert_not_called()

    def test_http_exception_detail_is_sanitized(self):
        from fastapi import HTTPException
        from webui.onboarding_auth import install
        app=FastAPI()
        @app.get('/login')
        def fail(): raise HTTPException(400,detail='private-error-canary')
        install(app,mode='protected',expected_origin='https://fixture.local',settings=self.valid_settings())
        with TestClient(app,base_url='https://fixture.local') as client:
            response=client.get('/login')
        self.assertEqual(response.status_code,422)
        self.assertEqual(set(response.json()),{'code','correlation_id'})
        self.assertNotIn('canary',response.text)


from onboarding_b2_support import B2Case


class AuthDatabaseTests(B2Case):
    def setUp(self):
        super().setUp()
        from webui.onboarding_auth import install, SESSION_COOKIE, CSRF_COOKIE
        self.app = FastAPI()
        @self.app.get('/')
        def home(): return {'safe':True}
        @self.app.get('/api/env')
        def env(): return {'safe':True}
        install(self.app,mode='protected',expected_origin='https://fixture.local',settings=self.settings)
        self.client=TestClient(self.app,base_url='https://fixture.local',client=('127.0.0.1',23456))
        self.addCleanup(self.client.close)
        self.client.cookies.set(SESSION_COOKIE,self.token)
        self.client.cookies.set(CSRF_COOKIE,self.csrf)

    def test_session_projection_and_bound_csrf_not_bearer(self):
        from webui.onboarding_auth import CSRF_COOKIE
        response=self.client.get('/api/auth/session')
        self.assertEqual(response.status_code,200)
        self.assertEqual(set(response.json()),{'display_name','permissions','csrf_token'})
        self.assertEqual(response.json()['csrf_token'],self.csrf)
        self.assertNotIn(self.token,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.client.cookies.set(CSRF_COOKIE,'x'*43)
        self.assertEqual(self.client.get('/api/auth/session').status_code,403)

    def test_epoch_disabled_permissions_and_expiry_checked_by_guard(self):
        self.assertEqual(self.client.get('/api/env').status_code,200)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',
                         (['onboarding:read'],self.actor.operator_id))
        self.assertEqual(self.client.get('/api/env').status_code,403)
        self.assertEqual(self.client.get('/').status_code,200)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET auth_epoch=auth_epoch+1 WHERE id=%s',(self.actor.operator_id,))
        self.assertEqual(self.client.get('/').status_code,401)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET auth_epoch=1,disabled=true WHERE id=%s',(self.actor.operator_id,))
        self.assertEqual(self.client.get('/').status_code,401)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET disabled=false WHERE id=%s',(self.actor.operator_id,))
            conn.execute("UPDATE operator_sessions SET idle_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(self.session_id,))
        self.assertEqual(self.client.get('/').status_code,401)

    def test_real_login_bootstrap_rotation_logout_and_durable_failure(self):
        from onboarding import security
        from webui.onboarding_auth import SESSION_COOKIE,CSRF_COOKIE,PREAUTH_COOKIE
        import secrets
        password=secrets.token_urlsafe(24)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET username_norm=%s,password_hash=%s WHERE id=%s',
                         ('fixture-route-user',security.hash_password(password),self.actor.operator_id))
        # Real old cookie is passed to login and its DB row must be revoked.
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE,self.token,domain='fixture.local',path='/')
        bootstrap=self.client.get('/api/auth/bootstrap')
        self.assertEqual(bootstrap.status_code,200)
        response=self.client.post('/api/auth/login',json={'username':'fixture-route-user','password':password},
                                  headers={'Origin':'https://fixture.local','X-CSRF-Token':bootstrap.json()['csrf_token']})
        self.assertEqual(response.status_code,200)
        self.assertTrue(self.read('SELECT revoked_at IS NOT NULL FROM operator_sessions WHERE id=%s',(self.session_id,))[0][0])
        self.assertNotEqual(self.client.cookies.get(SESSION_COOKIE),self.token)
        self.assertIsNone(self.client.cookies.get(PREAUTH_COOKIE))
        session=self.client.get('/api/auth/session')
        self.assertEqual(session.status_code,200)
        csrf=session.json()['csrf_token']
        self.assertEqual(self.client.post('/api/auth/logout',json={},headers={'Origin':'https://fixture.local','X-CSRF-Token':'x'*43}).status_code,403)
        self.assertEqual(self.client.get('/api/auth/session').status_code,200)
        response=self.client.post('/api/auth/logout',json={},headers={'Origin':'https://fixture.local','X-CSRF-Token':csrf})
        self.assertEqual(response.status_code,200)
        self.assertIsNone(self.client.cookies.get(SESSION_COOKIE))
        self.assertIsNone(self.client.cookies.get(CSRF_COOKIE))
        self.assertEqual(self.client.get('/api/auth/session').status_code,401)
        bootstrap=self.client.get('/api/auth/bootstrap')
        response=self.client.post('/api/auth/login',json={'username':'fixture-route-user','password':secrets.token_urlsafe(24)},
                                  headers={'Origin':'https://fixture.local','X-CSRF-Token':bootstrap.json()['csrf_token']})
        self.assertEqual(response.status_code,401)
        self.assertEqual(self.read("SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE 'login:%%' AND failures=1")[0][0],2)
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE action='auth.login_failed'")[0][0],1)


    def test_http_source_quota_is_durable_bounded_and_resets_by_database_clock(self):
        from webui.onboarding_auth import _http_rate_limit
        from uuid import uuid4
        source='127.0.0.1'
        self.assertTrue(_http_rate_limit(self.settings,source,'bootstrap',str(uuid4())))
        with self.uow() as conn:
            conn.execute("UPDATE auth_throttles SET failures=30 WHERE bucket_key LIKE 'http:bootstrap:%%'")
        before=self.read("SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE 'preauth:%%'")[0][0]
        response=self.client.get('/api/auth/bootstrap',headers={'X-Forwarded-For':'203.0.113.9'})
        self.assertEqual(response.status_code,429)
        self.assertEqual(response.json()['code'],'RATE_LIMITED')
        self.assertEqual(self.read("SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE 'preauth:%%'")[0][0],before)
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE outcome_code='RATE_LIMITED'")[0][0],1)
        self.assertEqual(self.client.get('/api/auth/bootstrap').status_code,429)
        self.assertEqual(self.read("SELECT count(*) FROM audit_events WHERE outcome_code='RATE_LIMITED'")[0][0],1)
        with self.uow() as conn:
            conn.execute("UPDATE auth_throttles SET window_start=clock_timestamp()-interval '61 seconds',blocked_until=clock_timestamp()-interval '1 second' WHERE bucket_key LIKE 'http:bootstrap:%%'")
        self.assertEqual(self.client.get('/api/auth/bootstrap').status_code,200)
        self.assertTrue(_http_rate_limit(self.settings,source,'login',str(uuid4())))
        with self.uow() as conn:
            conn.execute("UPDATE auth_throttles SET failures=10 WHERE bucket_key LIKE 'http:login:%%'")
        response=self.client.post('/api/auth/login',json={'username':'fixture','password':'fixture'},
                                  headers={'Origin':'https://fixture.local','X-CSRF-Token':'x'*43})
        self.assertEqual(response.status_code,429)
        self.assertEqual(self.read("SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE 'http:%%'")[0][0],2)
        self.assertEqual(self.read("SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE 'login:%%'")[0][0],0)
