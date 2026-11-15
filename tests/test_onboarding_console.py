"""Original index integration contracts; no real execution or credential reads."""
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.tags=[]; self.feed(text)
    def handle_starttag(self, tag, attrs): self.tags.append((tag,dict(attrs)))

class ConsoleTests(unittest.TestCase):
    def module(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('webui.onboarding_console'), 'original console integration missing')
        from webui import onboarding_console
        return onboarding_console

    def test_renderer_keeps_original_ids_and_externalizes_inline_style(self):
        m=self.module(); original=(ROOT/'webui/static/index.html').read_text()
        page,css=m.render_console(original)
        before=Tags(original).tags; after=Tags(page).tags
        self.assertEqual([a['id'] for _,a in before if 'id'in a], [a['id'] for _,a in after if 'id'in a])
        self.assertFalse(any('style'in a for _,a in after))
        self.assertTrue(any(t=='body' and a.get('data-console-mode')=='protected' for t,a in after))
        self.assertIn('display:none',css)
        scripts=[a.get('src') for t,a in after if t=='script']
        self.assertEqual(scripts, ['/static/console-protected.js', '/static/app.js?v=20260831-k12-3'])
        self.assertIn('/api/onboarding/console/styles.css',page)
        self.assertNotIn('data:image',page)
        self.assertEqual(m.render_console(original),(page,css))

    def test_renderer_escapes_attributes_and_retains_text_entities(self):
        m=self.module()
        source='<html><head></head><body><p id="a" title="A &quot; B" style="display:none">A &amp; B</p></body></html>'
        page,css=m.render_console(source)
        self.assertEqual(next(a for t,a in Tags(page).tags if t=='p')['title'],'A " B')
        self.assertIn('A &amp; B',page)
        self.assertIn('.console-inline-0{display:none}',css)

    def test_catalog_uses_all_original_tasks_but_not_execution_metadata(self):
        m=self.module()
        from onboarding_server_support import isolated_server
        with isolated_server() as server:
            scripts=server.schema
            dto=m.catalog(scripts)
        self.assertFalse(dto['execution_enabled'])
        self.assertEqual([x['id'] for x in dto['scripts']],[x['id'] for x in scripts.SCRIPTS])
        for original,public in zip(scripts.SCRIPTS,dto['scripts']):
            self.assertEqual(original['args'],public['args'])
            self.assertEqual(original['title'],public['title'])
            self.assertNotIn('file',public)
        self.assertEqual(dto['embeds'],[])
        self.assertEqual(len(dto['links']),len(scripts.EXTERNAL_LINKS))
        dto['scripts'][0]['args'].clear()
        self.assertTrue(scripts.SCRIPTS[0]['args'], 'catalog must not mutate source schema')

    def test_catalog_does_not_serialize_unknown_runtime_fields_or_unsafe_links(self):
        m=self.module()
        fixture=SimpleNamespace(SCRIPTS=[dict(id='a',title='A',category='C',args=[], file='/private/x', secret='canary')], EXTERNAL_LINKS=[dict(title='bad',url='javascript:alert(1)'),dict(title='bad',url='https://user:pass@host.test'),dict(title='ok',url='https://host.test/')])
        dto=m.catalog(fixture)
        self.assertNotIn('secret',dto['scripts'][0]);self.assertEqual(len(dto['links']),1)

    def test_exact_routes_require_session_ready_pool_and_get(self):
        self.module()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from webui.onboarding_auth import ProtectionMiddleware,WebConfig
        from onboarding.errors import ServiceError,ErrorCode
        paths=['/static/app.js','/static/console-protected.js','/static/console-protected.css','/api/onboarding/console/catalog','/api/onboarding/console/styles.css','/assets/telegram_qr.jpg']
        for ready in (True,False):
            app=FastAPI()
            for path in paths: app.add_api_route(path,lambda:{'ok':True},methods=['GET'])
            app.add_middleware(ProtectionMiddleware, config=WebConfig('protected','https://fixture.test',ready=True,pool_mode='synthetic',pool_ready=ready))
            with patch('webui.onboarding_auth._authenticate_request',return_value=object()) as auth, TestClient(app,base_url='https://fixture.test') as client:
                for path in paths:
                    self.assertEqual(client.get(path).status_code,200 if ready else 503,path)
                    if ready: self.assertEqual(auth.call_args.args[2],'onboarding:read')
                self.assertEqual(client.get('/static/other.js').status_code,403)
                self.assertEqual(client.post(paths[0],json={},headers={'Origin':'https://fixture.test'}).status_code,403)
                if ready:
                    auth.side_effect=ServiceError(ErrorCode.UNAUTHENTICATED)
                    for path in paths: self.assertEqual(client.get(path).status_code,401)

    def test_new_routes_revalidate_actor_even_on_direct_call(self):
        m=self.module()
        from fastapi import FastAPI
        from onboarding_server_support import FakeRequest
        captured=[]
        app=SimpleNamespace(include_router=captured.append)
        config=SimpleNamespace(settings=object(),ready=True,pool_ready=True)
        m.register_routes(app,config,ROOT/'webui/static',SimpleNamespace())
        from onboarding.errors import ServiceError,ErrorCode
        for route in captured[0].routes:
            if not route.path.startswith('/api/onboarding/console/'):continue
            with self.assertRaises(ServiceError) as exc: route.endpoint(FakeRequest())
            self.assertEqual(exc.exception.code,ErrorCode.UNAUTHENTICATED)

    def test_direct_read_rechecks_revocation_and_pool_readiness(self):
        m=self.module()
        from contextlib import nullcontext
        from onboarding_server_support import FakeRequest
        from onboarding.security import Actor
        from onboarding.errors import ServiceError,ErrorCode
        captured=[]
        config=SimpleNamespace(settings=object(),ready=True,pool_ready=True)
        m.register_routes(SimpleNamespace(include_router=captured.append),config,ROOT/'webui/static',SimpleNamespace(SCRIPTS=[],EXTERNAL_LINKS=[]))
        actor=Actor('operator',frozenset({'onboarding:read'}),'session',1)
        for route in captured[0].routes:
            with patch('onboarding.storage.unit_of_work',return_value=nullcontext(object())), patch('onboarding.security.revalidate',side_effect=ServiceError(ErrorCode.UNAUTHENTICATED)) as check:
                with self.assertRaises(ServiceError) as exc:route.endpoint(FakeRequest(actor=actor))
                self.assertEqual(exc.exception.code,ErrorCode.UNAUTHENTICATED)
                self.assertEqual(check.call_args.args[1:],(actor,'onboarding:read'))
            with self.assertRaises(ServiceError) as exc:route.endpoint(FakeRequest(actor=object()))
            self.assertEqual(exc.exception.code,ErrorCode.FORBIDDEN)
            config.pool_ready=False
            with self.assertRaises(ServiceError) as exc:route.endpoint(FakeRequest(actor=actor))
            self.assertEqual(exc.exception.code,ErrorCode.DEPENDENCY_UNAVAILABLE)
            config.pool_ready=True

    def test_renderer_rejects_unexpected_scripts_or_inline_handlers(self):
        m=self.module()
        for source in ('<body onload="alert(1)"></body>', '<script>alert(1)</script>', '<script src="/static/new.js"></script>', '<style>body{display:none}</style>'):
            with self.subTest(source=source), self.assertRaises(ValueError):m.render_console(source)

    def test_sensitive_defaults_and_unknown_arg_fields_are_not_exposed(self):
        m=self.module()
        fixture=SimpleNamespace(SCRIPTS=[dict(id='a',title='A',category='C',args=[dict(flag='--clash-secret',type='str',default='canary',runtime_value='hidden')])],EXTERNAL_LINKS=[])
        dto=m.catalog(fixture)
        self.assertEqual(dto['scripts'][0]['args'][0],dict(flag='--clash-secret',type='str',default=''))
        self.assertEqual(fixture.SCRIPTS[0]['args'][0]['default'],'canary')

    def test_inline_visibility_preserves_precedence_over_original_id_styles(self):
        m=self.module()
        _,css=m.render_console('<body><section id="view-plus" style="display:none"></section></body>')
        self.assertIn('#view-plus.console-inline-0{display:none}',css)

    def test_unified_renderer_embeds_pool_content_without_a_second_shell(self):
        m=self.module(); source=(ROOT/'webui/static/index.html').read_text()
        pool=(ROOT/'webui/static/onboarding-pools.html').read_text()
        page,css=m.render_console(source,pool_source=pool)
        tags=Tags(page).tags
        ids=[a['id'] for _,a in tags if 'id' in a]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(sum(t=='header' for t,a in tags),1)
        self.assertEqual(sum(t=='main' for t,a in tags),1)
        for ident in ('view-onboarding','main','operator','logout','mailbox-card','task-workbench','card-pool','global-config'):
            self.assertIn(ident,ids)
        self.assertNotIn('shell-menu',ids)
        self.assertEqual([a.get('src') for t,a in tags if t=='script'],['/static/console-protected.js','/static/app.js?v=20260831-k12-3','/static/onboarding-pools.js','/static/onboarding-cards.js'])
        self.assertIn('/static/onboarding-shell.css',page)
        self.assertEqual(css,m.render_console(source)[1])

    def test_unified_renderer_rejects_invalid_pool_fragment(self):
        m=self.module();source=(ROOT/'webui/static/index.html').read_text()
        for pool in ('missing', '<main id="main">unclosed', '<main id="main"><script src="evil"></script></main>', '<main id="main"><button onclick="evil()">x</button></main>', '<main id="main"></main><main id="main"></main>'):
            with self.subTest(pool=pool),self.assertRaises(ValueError):
                m.render_console(source,pool_source=pool)
