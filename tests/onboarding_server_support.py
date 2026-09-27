"""Import legacy server only under synthetic env/files and no-I/O import spies."""
from contextlib import contextmanager, ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'webui' / 'server.py'


@contextmanager
def isolated_server(mode='protected', values=None):
    with tempfile.TemporaryDirectory(prefix='rf-web-fixture-') as directory, ExitStack() as stack:
        root = Path(directory)
        (root / 'webui' / 'static').mkdir(parents=True)
        (root / 'assets').mkdir()
        (root / 'webui' / 'static' / 'index.html').write_text('fixture-legacy-page')
        (root / 'webui' / 'static' / 'onboarding.html').write_text('fixture-protected-page')
        (root / 'webui' / 'static' / 'onboarding-login.html').write_text('fixture-login-page')
        (root / 'webui' / 'static' / 'onboarding-pools.html').write_text('fixture-pools-page')
        (root / 'webui' / 'static' / 'onboarding-pools.js').write_text('// fixture-pools-script')
        (root / '.env').write_text('CLASH_SECRET=fixture-env-canary\nCLASH_API=http://fixture-controller\nK12_AUTO_START=0\n')
        (root / '.env.example').write_text('K12_AUTO_START=0\n')
        env = {'ONBOARDING_MODE': mode, 'REG_FACTORY_ENV_FILE': str(root / '.env'),
               'REG_FACTORY_DATA_DIR': str(root), 'ONBOARDING_ORIGIN': 'https://fixture.test',
               'ONBOARDING_SETTINGS_FILE': '/fixture/private/settings.json'}
        env.update(values or {})
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        fake_config = ModuleType('config')
        fake_config.CLAUDE_PROTOCOL_VERSION = 'fixture'
        fake_config.CLAUDE_REGISTRATION_PROTOCOL = 'browser'
        proxy = ModuleType('common.proxy_switch')
        proxy.effective_proxy_url = Mock(return_value='')
        proxy.platform_environment = Mock(side_effect=lambda env, platform: env)
        from webui.onboarding_auth import error_response, read_json
        auth = ModuleType('webui.onboarding_auth')
        auth.error_response = error_response
        auth.read_json = read_json
        def install(app, **kwargs):
            config = SimpleNamespace(mode=kwargs['mode'], expected_origin=kwargs.get('expected_origin'),
                                     settings=object(), ready=True,
                                     pool_mode=kwargs.get('pool_mode', 'off'),
                                     pool_ready=False)
            app.state.onboarding_config = config
            return config
        auth.install = Mock(side_effect=install)
        routes = ModuleType('webui.onboarding_routes')
        routes.register_routes = Mock()
        pool_routes = ModuleType('webui.onboarding_pool_routes')
        pool_routes.register_routes = Mock()
        stack.enter_context(patch.dict(sys.modules, {'config': fake_config,
                'common.proxy_switch': proxy, 'webui.onboarding_auth': auth,
                'webui.onboarding_routes': routes,
                'webui.onboarding_pool_routes': pool_routes}))
        import common
        stack.enter_context(patch.object(common, 'proxy_switch', proxy, create=True))
        git = stack.enter_context(patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='fixture-git')))
        stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden in fixture')))
        stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden in fixture')))
        stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('socket forbidden in fixture')))
        stack.enter_context(patch('socket.socket.connect_ex', side_effect=AssertionError('socket forbidden in fixture')))
        stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden in fixture')))
        module_name = 'fixture_webui_server_' + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(module_name, SOURCE)
        module = importlib.util.module_from_spec(spec)
        # Drive every ROOT/env/static lookup to synthetic files, not the checkout.
        module.__file__ = str(root / 'webui' / 'server.py')
        old_path = list(sys.path)
        stack.callback(lambda: sys.path.__setitem__(slice(None), old_path))
        spec.loader.exec_module(module)
        module._test_import_git = git
        module._test_import_proxy = proxy
        module._test_auth_install = auth.install
        module._test_route_register = routes.register_routes
        module._test_pool_route_register = pool_routes.register_routes
        yield module


class FakeRequest:
    def __init__(self, data=None, actor=None):
        self.data = data or {}
        self.state = SimpleNamespace(onboarding_actor=actor, correlation_id='fixture-correlation')
        self.headers = {}
        self.client = SimpleNamespace(host='127.0.0.1')

    async def json(self):
        return self.data

    async def stream(self):
        yield json.dumps(self.data).encode()
