"""Offline ASGI boundary contracts; mocks stop before any database/provider I/O."""
import inspect
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from onboarding import security
from onboarding.errors import ErrorCode, ServiceError
from onboarding.settings import Settings, SOCKET
from webui import onboarding_auth, onboarding_routes

ORIGIN = 'https://fixture.local'
HEADERS = {'Origin': ORIGIN, 'X-CSRF-Token': 'c' * 43}
ID = '12345678-1234-4234-9234-123456789abc'


class PoolApiOfflineTests(unittest.TestCase):
    def settings(self):
        return Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
                        'fixture-only-placeholder', 'rf-onboarding-p1b-v1:fixture', 'rf_onboarding')

    @contextmanager
    def client(self, mode='synthetic', ready=True, authenticate=True):
        with ExitStack() as stack:
            # The trusted BOOT dependency seam is the sole mocked pool boundary.
            stack.enter_context(patch('webui.onboarding_auth._pool_context',
                return_value=(object(), object()) if ready else None, create=True))
            actor = security.Actor(ID, frozenset({'onboarding:read', 'tasks:manage', 'config:manage', 'mailboxes:manage'}), ID, 1)
            auth = stack.enter_context(patch('webui.onboarding_auth._authenticate_request',
                return_value=actor, side_effect=None if authenticate else ServiceError(ErrorCode.UNAUTHENTICATED)))
            app = FastAPI()
            kwargs = {}
            # Old implementation reaches its real guard and fails 403, not import/TypeError.
            if 'pool_mode' in inspect.signature(onboarding_auth.install).parameters:
                kwargs = dict(pool_mode=mode, pool_keyring=object(), pool_mac=object())
            config = onboarding_auth.install(app, mode='protected', expected_origin=ORIGIN,
                settings=self.settings(), **kwargs)
            onboarding_routes.register_routes(app, config)
            if getattr(config, 'pool_ready', False):
                from webui.onboarding_pool_routes import register_routes
                register_routes(app, config)
            client = stack.enter_context(TestClient(app, base_url=ORIGIN))
            yield client, auth, config

    def test_known_synthetic_path_reaches_service(self):
        from onboarding.mailbox_read import Page
        with patch('onboarding.mailbox_read.list_page', return_value=Page((), None)) as service:
            with self.client() as (client, auth, config):
                response = client.get('/api/onboarding/mailboxes')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'items': [], 'next_cursor': None})
        self.assertEqual(auth.call_args.args[-1], 'onboarding:read')
        service.assert_called_once()

    def test_missing_pool_dependencies_known_503_unknown_403(self):
        with self.client(ready=False) as (client, _, config):
            self.assertEqual(client.get('/api/onboarding/mailboxes').status_code, 503)
            self.assertEqual(client.get('/api/onboarding/cards').status_code, 403)
            self.assertEqual(client.head('/api/onboarding/mailboxes').status_code, 403)

    def test_pool_off_does_not_construct_dependencies_or_mount(self):
        app = FastAPI()
        with patch('webui.onboarding_auth._pool_context', create=True) as construct:
            config = onboarding_auth.install(app, mode='protected', expected_origin=ORIGIN, settings=self.settings())
        construct.assert_not_called()
        self.assertFalse(getattr(config, 'pool_ready', False))
        self.assertNotIn('/api/onboarding/mailboxes', [getattr(r, 'path', None) for r in app.routes])

    def test_unknown_methods_paths_encoded_and_unapproved_assets_fail_closed(self):
        with self.client() as (client, _, config):
            for method, path in [('HEAD','/api/onboarding/mailboxes'), ('OPTIONS','/api/onboarding/config'),
                    ('GET','/api/onboarding/cards'), ('GET','/api/onboarding/mailboxes/'+ID+'/history'),
                    ('GET','/static/onboarding-pools.html'), ('GET','/api/onboarding/%6dailboxes'),
                    ('POST','/api/onboarding/tasks/'+ID+'/future')]:
                self.assertEqual(client.request(method,path,headers=HEADERS).status_code,403,(method,path))

    def test_queries_strict_and_never_call_service(self):
        with patch('onboarding.mailbox_read.list_page') as service:
            with self.client() as (client, _, config):
                for query in ('limit=1&limit=2','limit=0','limit=101','limit=true','limit=+1','limit=01',
                              'occupied=1','platform=evil','actor=evil','health=healthy'):
                    self.assertEqual(client.get('/api/onboarding/mailboxes?'+query).status_code,422,query)
        service.assert_not_called()

    def test_preview_exact_shape_duplicate_keys_and_256k_limit(self):
        with patch('onboarding.mailboxes.preview_import') as service:
            with self.client() as (client, _, config):
                for raw in ('{}','{"text":"x","text":"y","group_ref":""}',
                            '{"text":"x","group_ref":"","settings":{}}',
                            '{"text":true,"group_ref":""}',
                            '{"text":"' + 'x'*262144 + '","group_ref":""}'):
                    response=client.post('/api/onboarding/mailboxes/import-preview', content=raw,
                        headers={**HEADERS,'Content-Type':'application/json'})
                    self.assertEqual(response.status_code,422)
        service.assert_not_called()

    def test_import_rejects_update_or_expected_versions_not_silently_ignored(self):
        body=dict(text='fixture',group_ref='',duplicate_action='skip',preview_digest='a'*64,
                  expected_versions={},request_key='fixture-key')
        with patch('onboarding.mailboxes.import_text') as service:
            with self.client() as (client, _, config):
                for change in ({'duplicate_action':'update'},{'expected_versions':{ID:1}},{'expected_versions':[]},
                               {'request_key':True},{'preview_digest':None},{'vault':{}}):
                    self.assertEqual(client.post('/api/onboarding/mailboxes/import',json={**body,**change},headers=HEADERS).status_code,422)
        service.assert_not_called()

    def test_patch_rejects_noncanonical_id_bool_version_and_extra_changes(self):
        body=dict(expected_version=1,changes={'disabled':True},request_key='fixture-key')
        with patch('onboarding.mailbox_update.update') as service:
            with self.client() as (client, _, config):
                for identity, change in ((ID.upper(),{}),(ID,{'expected_version':True}),
                    (ID,{'changes':{}}),(ID,{'changes':{'health':'HEALTHY'}}),(ID,{'changes':{'disabled':1}})):
                    self.assertEqual(client.patch('/api/onboarding/mailboxes/'+identity,json={**body,**change},headers=HEADERS).status_code,422)
        service.assert_not_called()

    def test_config_get_passes_manage_permission_and_no_query(self):
        with patch('onboarding.pool_config.get_current',return_value=None) as service:
            with self.client() as (client, auth, config):
                self.assertEqual(client.get('/api/onboarding/config').status_code,200)
                self.assertEqual(auth.call_args.args[-1],'config:manage')
                self.assertEqual(service.call_args.kwargs['permission'],'config:manage')
                self.assertEqual(client.get('/api/onboarding/config?scope=pool').status_code,422)
        service.assert_called_once()

    def test_unauthenticated_and_unsafe_origin_are_rejected(self):
        with self.client(authenticate=False) as (client,_,_):
            self.assertEqual(client.get('/api/onboarding/mailboxes').status_code,401)
        with self.client() as (client,_,_):
            self.assertEqual(client.post('/api/onboarding/preflight',json={}).status_code,403)

    def test_batch_bool_count_and_unknown_override_rejected(self):
        body=dict(selection='automatic',requested_count=1,mailbox_ids=[],expected_config_revision='pool-'+ID,request_key='fixture-key')
        with patch('onboarding.pool_batches.create') as service:
            with self.client() as (client,_,_):
                for change in ({'requested_count':True},{'mailbox_ids':[ID]},{'health':'HEALTHY'},{'expected_config_revision':None}):
                    self.assertEqual(client.post('/api/onboarding/batches',json={**body,**change},headers=HEADERS).status_code,422)
        service.assert_not_called()

    def test_pool_task_commands_commit_response_without_followup_read(self):
        with patch('onboarding.task_read.locate_scope',return_value='pool') as locate, \
             patch('onboarding.pool_commands.pause',return_value={'receipt_id':ID,'phase':'SUCCEEDED'}) as command, \
             patch('onboarding.task_read.read_pool',side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)) as read, \
             patch('webui.onboarding_routes.command_task',side_effect=AssertionError('fixture fallback forbidden')):
            with self.client() as (client,_,_):
                response=client.post('/api/onboarding/tasks/'+ID+'/pause',json={'expected_version':1,'request_key':'fixture-key'},headers=HEADERS)
                self.assertEqual(response.status_code,202)
                self.assertEqual(response.json(),{'accepted':True,'synthetic':True,'execution_scope':'pool',
                    'receipt_id':ID,'phase':'SUCCEEDED','task':None,'snapshot_pending':True})
                read.assert_not_called()
                self.assertEqual(locate.call_args.kwargs['permission'],'tasks:manage')
                self.assertEqual(client.get('/api/onboarding/tasks/'+ID).status_code,503)
                self.assertEqual(locate.call_args.kwargs['permission'],'onboarding:read')
        command.assert_called_once()

    def test_locator_scope_denial_has_no_fallback(self):
        for error in (ErrorCode.FORBIDDEN,ErrorCode.UNAUTHENTICATED):
            with patch('onboarding.task_read.locate_scope',side_effect=ServiceError(error)), \
                 patch('webui.onboarding_routes.read_task') as fixture, \
                 patch('onboarding.task_read.read_pool') as pool:
                with self.client() as (client,_,_):
                    self.assertEqual(client.get('/api/onboarding/tasks/'+ID).status_code,403 if error==ErrorCode.FORBIDDEN else 401)
                fixture.assert_not_called();pool.assert_not_called()

    def test_scope_changed_after_locator_does_not_try_other_service(self):
        with patch('onboarding.task_read.locate_scope',return_value='pool'), \
             patch('onboarding.pool_commands.cancel',side_effect=ServiceError(ErrorCode.FORBIDDEN)) as pool, \
             patch('webui.onboarding_routes.command_task') as fixture:
            with self.client() as (client,_,_):
                response=client.post('/api/onboarding/tasks/'+ID+'/cancel',json={'expected_version':1,'request_key':'fixture-key'},headers=HEADERS)
                self.assertEqual(response.status_code,403)
        pool.assert_called_once();fixture.assert_not_called()

    def test_task_commands_validate_before_locator_and_query_cannot_choose_scope(self):
        with patch('onboarding.task_read.locate_scope') as locate:
            with self.client() as (client,_,_):
                for suffix, body in (('',{'expected_version':True,'request_key':'fixture-key'}),
                                     ('?scope=fixture',{'expected_version':1,'request_key':'fixture-key'})):
                    self.assertEqual(client.post('/api/onboarding/tasks/'+ID+'/cancel'+suffix,json=body,headers=HEADERS).status_code,422)
                self.assertEqual(client.get('/api/onboarding/tasks/'+ID+'?scope=pool').status_code,422)
        locate.assert_not_called()

    def test_server_boot_defaults_pool_off(self):
        from onboarding_server_support import isolated_server
        with isolated_server() as server:
            self.assertEqual(server._test_auth_install.call_args.kwargs.get('pool_mode'),'off')
            server._test_pool_route_register.assert_not_called()

    def test_invalid_pool_boot_mode_fails_closed(self):
        from onboarding_server_support import isolated_server
        with self.assertRaisesRegex(RuntimeError,'INVALID_ONBOARDING_POOL_MODE'):
            with isolated_server(values={'ONBOARDING_POOL_MODE':'real'}):
                pass

    def test_pool_off_task_handlers_do_not_import_pool_dependencies(self):
        import builtins
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if 'pool' in name:
                raise AssertionError('pool dependency in off handler')
            return original(name, *args, **kwargs)
        with self.client(mode='off') as (client, _, _), \
             patch('webui.onboarding_routes.read_task',return_value={'id':ID}), \
             patch('webui.onboarding_routes.command_task',return_value={'accepted':True}), \
             patch('builtins.__import__',side_effect=guarded):
            self.assertEqual(client.get('/api/onboarding/tasks/'+ID).status_code,200)
            self.assertEqual(client.post('/api/onboarding/tasks/'+ID+'/pause',json={'expected_version':1,'request_key':'fixture-key'},headers=HEADERS).status_code,202)

    def test_preview_projects_nested_allowlist_and_accepts_more_than_8192_bytes(self):
        result=dict(items=[dict(line=1,email='fixture@example.invalid',provider='other',group_ref='',secret_ref='private')],
                    issues=[dict(line=2,code='INVALID_FORMAT',raw_line='private')],accepted_count=1,duplicate_count=0,
                    conflict_count=0,preview_digest=None,private='private')
        with patch('onboarding.mailboxes.preview_import',return_value=result):
            with self.client() as (client,_,_):
                response=client.post('/api/onboarding/mailboxes/import-preview',json={'text':'x'*9000,'group_ref':''},headers=HEADERS)
                self.assertEqual(response.status_code,200)
                self.assertNotIn('private',response.text)

    def test_pool_batch_response_exact_seven_keys_and_202(self):
        result=dict(batch_id=ID,task_ids=[ID],mailbox_ids=[ID],config_id=ID,config_revision='pool-'+ID,
                    receipt_id=ID,phase='SUCCEEDED',secret_ref='private')
        body=dict(selection='specified',requested_count=1,mailbox_ids=[ID],expected_config_revision='pool-'+ID,request_key='fixture-key')
        with patch('onboarding.pool_batches.create',return_value=result):
            with self.client() as (client,_,_):
                response=client.post('/api/onboarding/batches',json=body,headers=HEADERS)
                self.assertEqual(response.status_code,202)
                self.assertEqual(set(response.json()),set(result)-{'secret_ref'})

    def test_auth_transaction_ends_before_pool_business(self):
        events=[]
        @contextmanager
        def uow(settings):
            events.append('auth-begin');yield object();events.append('auth-end')
        actor=security.Actor(ID,frozenset({'tasks:manage'}),ID,1)
        def preflight(*args,**kwargs):
            events.append('business')
            return dict(selection='automatic',requested_count=1,eligible_count=0,config_revision=None,
                        can_create=False,reason_codes=['CONFIG_MISSING'],observation_only=True)
        original=onboarding_auth._authenticate_request
        with self.client() as (client,auth,_), patch('onboarding.storage.unit_of_work',uow), \
             patch('onboarding.security.require',return_value=actor), patch('onboarding.security.check_csrf'), \
             patch('onboarding.pool_batches.preflight',side_effect=preflight):
            auth.side_effect=original
            response=client.post('/api/onboarding/preflight',json={'selection':'automatic','requested_count':1,'mailbox_ids':[]},headers=HEADERS)
            self.assertEqual(response.status_code,200)
        self.assertEqual(events,['auth-begin','auth-end','business'])

    def test_boot_context_checks_schema_policy_inside_uow_and_hides_dependencies(self):
        # Test exact trusted classes without touching private files or opening PG.
        from onboarding.keyring import Keyring
        from onboarding.request_mac import RequestMac
        from onboarding.pool_vault import SyntheticPoolPolicy
        from pathlib import Path
        keyring=object.__new__(Keyring);mac=object.__new__(RequestMac)
        object.__setattr__(keyring,'directory',Path('/private/fixture'));object.__setattr__(keyring,'active_version','v1')
        object.__setattr__(mac,'directory',keyring.directory)
        policy=object.__new__(SyntheticPoolPolicy)
        object.__setattr__(policy,'settings',self.settings())
        events=[]
        @contextmanager
        def uow(settings):
            events.append('begin');yield object();events.append('end')
        with patch.object(Keyring,'__post_init__'), patch.object(RequestMac,'_key'), \
             patch.object(SyntheticPoolPolicy,'from_settings',return_value=policy), \
             patch.object(SyntheticPoolPolicy,'_validate'), \
             patch.object(SyntheticPoolPolicy,'_connection',side_effect=lambda c:events.append('schema2-check')), \
             patch('onboarding.pool_vault.PoolVault._keys'), patch('onboarding.storage.unit_of_work',uow):
            app=FastAPI()
            config=onboarding_auth.install(app,mode='protected',expected_origin=ORIGIN,settings=self.settings(),
                pool_mode='synthetic',pool_keyring=keyring,pool_mac=mac)
            self.assertTrue(config.pool_ready)
            self.assertNotIn('/private',repr(config))
            self.assertEqual(events,['begin','schema2-check','end'])

    def test_boot_bad_dependencies_and_schema_fail_closed_without_mount(self):
        app=FastAPI()
        config=onboarding_auth.install(app,mode='protected',expected_origin=ORIGIN,settings=self.settings(),pool_mode='synthetic')
        self.assertFalse(config.pool_ready)
        with TestClient(app,base_url=ORIGIN) as client:
            self.assertEqual(client.get('/api/onboarding/config').status_code,503)
            self.assertEqual(client.get('/api/onboarding/not-approved').status_code,403)


    def test_actual_server_pool_script_route_precedes_static_mount(self):
        """Execute server.py's own registration order, not a test-built app."""
        import importlib.util
        import sys
        from onboarding_server_support import isolated_server
        from webui.onboarding_pool_routes import register_routes as actual_pool_routes
        original_spec = importlib.util.spec_from_file_location

        def server_spec(name, location, *args, **kwargs):
            spec = original_spec(name, location, *args, **kwargs)
            if not name.startswith('fixture_webui_server_'):
                return spec
            original_execute = spec.loader.exec_module

            def execute(module):
                # The fixture has now installed its no-I/O stubs. Replace only
                # composition stubs with actual route/auth registration; keep
                # settings/private-key/PG checks at the trusted dependency seam.
                def install(app, **options):
                    options.pop('settings_path', None)
                    return onboarding_auth.install(app, settings=self.settings(), **options)
                sys.modules['webui.onboarding_auth'].install = install
                sys.modules['webui.onboarding_routes'].register_routes = onboarding_routes.register_routes
                sys.modules['webui.onboarding_pool_routes'].register_routes = actual_pool_routes
                original_execute(module)

            spec.loader.exec_module = execute
            return spec

        actor = security.Actor(ID, frozenset({'onboarding:read'}), ID, 1)
        with patch('importlib.util.spec_from_file_location', side_effect=server_spec), \
             patch('webui.onboarding_auth._pool_context', return_value=(object(), object())), \
             patch('webui.onboarding_auth._authenticate_request', return_value=actor):
            with isolated_server(values={'ONBOARDING_POOL_MODE': 'synthetic'}) as server:
                with TestClient(server.app, base_url='https://fixture.test') as client:
                    for suffix in ('?unexpected=1', '?limit=1&limit=2'):
                        response = client.get('/static/onboarding-pools.js' + suffix)
                        self.assertEqual(response.status_code, 422)
                        self.assertEqual(response.json()['code'], 'INVALID_INPUT')
                    self.assertEqual(client.get('/static/onboarding-pools.js').status_code, 200)
                    self.assertEqual(client.get('/static/app.js').status_code, 403)
                    self.assertEqual(client.head('/static/onboarding-pools.js').status_code, 403)


    CONFIG_BODY = dict(expected_revision=None, request_key='fixture:not-committed', fields=dict(
        model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api', group_ref='fixture:group',
        project_prefix='fixture-project', timeout_seconds=1800, concurrency=1, retention_days=30))

    def proof_error(self, code=ErrorCode.VERSION_CONFLICT, cls=ServiceError, flag=True):
        return cls(code, not_committed=flag)

    def test_service_error_flag_defaults_false_and_only_true_counts(self):
        self.assertIs(ServiceError(ErrorCode.VERSION_CONFLICT).not_committed, False)
        self.assertIs(ServiceError(ErrorCode.VERSION_CONFLICT, not_committed='yes').not_committed, False)
        self.assertIs(ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True).not_committed, True)

    def test_not_committed_field_on_each_allowlisted_write(self):
        routes = [
            ('onboarding.pool_config.replace', 'put', '/api/onboarding/config', self.CONFIG_BODY),
            ('onboarding.pool_batches.create', 'post', '/api/onboarding/batches', dict(selection='specified',
                requested_count=1, mailbox_ids=[ID], expected_config_revision='pool-' + ID, request_key='fixture:k')),
            ('onboarding.mailbox_update.update', 'patch', '/api/onboarding/mailboxes/' + ID,
                dict(expected_version=1, changes={'group_ref': 'fixture:group'}, request_key='fixture:k')),
            ('onboarding.mailboxes.import_text', 'post', '/api/onboarding/mailboxes/import', dict(text='x',
                group_ref='fixture:group', duplicate_action='skip', preview_digest='a' * 64, expected_versions={},
                request_key='fixture:k')),
        ]
        for target, method, path, body in routes:
            with self.subTest(path=path):
                with patch(target, side_effect=self.proof_error()):
                    with self.client() as (client, _, _config):
                        response = getattr(client, method)(path, json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(set(response.json()), {'code', 'correlation_id', 'not_committed'})
                self.assertIs(response.json()['not_committed'], True)

    def test_not_committed_field_on_pool_task_command(self):
        with patch('onboarding.task_read.locate_scope', return_value='pool'), \
             patch('onboarding.pool_commands.pause', side_effect=self.proof_error()):
            with self.client() as (client, _, _config):
                response = client.post('/api/onboarding/tasks/' + ID + '/pause',
                                       json={'expected_version': 1, 'request_key': 'fixture:k'}, headers=HEADERS)
        self.assertEqual(response.status_code, 409)
        self.assertIs(response.json()['not_committed'], True)

    def test_not_committed_field_dropped_unless_all_conditions_hold(self):
        class Subclassed(ServiceError):
            pass
        cases = [
            ('unflagged', self.proof_error(flag=False)),
            ('other code', self.proof_error(code=ErrorCode.RESOURCE_HELD)),
            ('subclass', self.proof_error(cls=Subclassed)),
        ]
        for name, error in cases:
            with self.subTest(case=name):
                with patch('onboarding.pool_config.replace', side_effect=error):
                    with self.client() as (client, _, _config):
                        response = client.put('/api/onboarding/config', json=self.CONFIG_BODY, headers=HEADERS)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(set(response.json()), {'code', 'correlation_id'})
        with patch('onboarding.mailbox_read.list_page', side_effect=self.proof_error()):
            with self.client() as (client, _, _config):
                response = client.get('/api/onboarding/mailboxes')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(set(response.json()), {'code', 'correlation_id'}, 'read route never carries the proof')


# Real PostgreSQL tests are deliberately not selected by the agent's offline run.
# The parent owns the sole approved PG window and preserves all raw results.
from onboarding_pool_support import PoolCase


class PoolApiDatabaseTests(PoolCase):
    def setUp(self):
        super().setUp()
        from webui.onboarding_pool_routes import register_routes
        self.app=FastAPI()
        self.config=onboarding_auth.install(self.app,mode='protected',expected_origin=ORIGIN,
            settings=self.settings,pool_mode='synthetic',pool_keyring=self.keyring,pool_mac=self.mac)
        self.assertTrue(self.config.pool_ready)
        onboarding_routes.register_routes(self.app,self.config)
        register_routes(self.app,self.config)
        self.client=TestClient(self.app,base_url=ORIGIN)
        self.addCleanup(self.client.close)
        self.client.cookies.set(onboarding_auth.SESSION_COOKIE,self.token)
        self.headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf}

    def call(self,method,path,body=None,status=200):
        response=self.client.request(method,'/api/onboarding/'+path,json=body,headers=self.headers)
        self.assertEqual(response.status_code,status,response.text)
        return response.json()

    def imported(self):
        import json
        raw=json.dumps({'email':'http@fixture.invalid','password':'fixture:mailbox-http','provider':'outlook'})
        preview=self.call('POST','mailboxes/import-preview',{'text':raw,'group_ref':'fixture:group'})
        self.assertNotIn('fixture:mailbox-http',repr(preview))
        result=self.call('POST','mailboxes/import',dict(text=raw,group_ref='fixture:group',duplicate_action='skip',
            preview_digest=preview['preview_digest'],expected_versions={},request_key='fixture:http-import'))
        return result['created_ids'][0]

    def configured(self):
        fields=dict(model='fixture-model',region='fixture-region',instance_ref='fixture:sub2api',
                    group_ref='fixture:group',project_prefix='fixture-project',timeout_seconds=1800,concurrency=1,retention_days=30)
        return self.call('PUT','config',dict(expected_revision=None,fields=fields,request_key='fixture:http-config'))

    def seeded_batch(self):
        mid=self.imported();config=self.configured()
        # Only this trusted test observer supplies reconciliation evidence. No
        # route can edit health/UNUSED or wash away import's unknown history.
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET health='HEALTHY' WHERE id=%s",(mid,))
            conn.execute("UPDATE mailbox_platform_states SET usage_status='UNUSED',evidence_ref='fixture:http-observer',"
                         "checked_at=clock_timestamp() WHERE mailbox_id=%s AND platform='google'",(mid,))
        body=dict(selection='specified',requested_count=1,mailbox_ids=[mid],
                  expected_config_revision=config['revision'],request_key='fixture:http-batch')
        return mid,body,self.call('POST','batches',body,202)

    def test_real_import_patch_config_preflight_batch_tasks_and_receipts(self):
        mid=self.imported()
        listed=self.call('GET','mailboxes')['items']
        self.assertEqual([item['id'] for item in listed],[mid])
        self.assertEqual(listed[0]['health'],'UNKNOWN')
        self.assertEqual({p['usage_status'] for p in listed[0]['platforms']},{'HISTORY_UNRECONCILED'})
        self.call('PATCH','mailboxes/'+mid,dict(expected_version=1,changes={'group_ref':'fixture:group-alt'},request_key='fixture:http-patch'))
        self.assertEqual(self.read('SELECT group_ref,version FROM mailbox_registry WHERE id=%s',(mid,)),[('fixture:group-alt',2)])
        config=self.configured()
        self.assertEqual(self.call('GET','config')['revision'],config['revision'])
        preflight=dict(selection='specified',requested_count=1,mailbox_ids=[mid])
        observed=self.call('POST','preflight',preflight)
        self.assertFalse(observed['can_create']);self.assertTrue(observed['observation_only'])
        with self.uow() as conn:
            conn.execute("UPDATE mailbox_registry SET health='HEALTHY' WHERE id=%s",(mid,))
            conn.execute("UPDATE mailbox_platform_states SET usage_status='UNUSED',evidence_ref='fixture:http-observer',checked_at=clock_timestamp() WHERE mailbox_id=%s AND platform='google'",(mid,))
        self.assertTrue(self.call('POST','preflight',preflight)['can_create'])
        body={**preflight,'expected_config_revision':config['revision'],'request_key':'fixture:http-batch'}
        batch=self.call('POST','batches',body,202)
        self.assertEqual(set(batch),{'batch_id','task_ids','mailbox_ids','config_id','config_revision','receipt_id','phase'})
        self.assertEqual(self.call('POST','batches',body,202),batch)
        tid=batch['task_ids'][0]
        self.assertEqual(self.call('GET','tasks?scope=pool')['items'][0]['id'],tid)
        detail=self.call('GET','tasks/'+tid)
        self.assertEqual(detail['execution_scope'],'pool')
        with patch('onboarding.task_read.read_pool',side_effect=ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)):
            accepted=self.call('POST','tasks/'+tid+'/pause',{'expected_version':detail['version'],'request_key':'fixture:http-pause'},202)
            self.assertIsNone(accepted['task']);self.assertTrue(accepted['snapshot_pending'])
            self.call('GET','tasks/'+tid,status=503)
        self.assertEqual(self.read('SELECT phase FROM operation_receipts WHERE id=%s',(accepted['receipt_id'],)),[('SUCCEEDED',)])
        for action in ('recheck','cancel'):
            detail=self.call('GET','tasks/'+tid)
            self.call('POST','tasks/'+tid+'/'+action,{'expected_version':detail['version'],'request_key':'fixture:http-'+action},202)
        self.assertEqual(self.read('SELECT cancel_requested FROM onboarding_tasks WHERE id=%s',(tid,)),[(True,)])
        self.assertEqual(self.read('SELECT count(*) FROM resource_leases WHERE task_id=%s',(tid,)),[(1,)])
        fixture=self.task()
        self.assertEqual(self.call('GET','tasks/'+fixture['id'])['id'],fixture['id'])
        self.assertIsInstance(self.call('POST','tasks/'+fixture['id']+'/pause',{'expected_version':1,'request_key':'fixture:old-pause'},202)['task'],dict)

    def test_config_only_tasks_only_and_permission_revoked_after_http_auth(self):
        self.configured()
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',(['config:manage'],self.actor.operator_id))
        self.call('GET','config')
        self.call('GET','mailboxes',status=403)
        original=onboarding_auth._authenticate_request
        def revoke(request,config,permission):
            actor=original(request,config,permission)
            with self.uow() as conn:
                conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',([],self.actor.operator_id))
            return actor
        with patch('webui.onboarding_auth._authenticate_request',side_effect=revoke):
            self.call('GET','config',status=403)
        with self.uow() as conn:
            conn.execute('UPDATE operators SET permissions=%s WHERE id=%s',(['tasks:manage'],self.actor.operator_id))
        fixture=self.task()
        self.call('POST','tasks/'+fixture['id']+'/pause',{'expected_version':1,'request_key':'fixture:tasks-only'},202)
        self.call('GET','tasks/'+fixture['id'],status=403)

    def test_other_owner_patch_task_and_selected_preflight_denied(self):
        mid,body,batch=self.seeded_batch()
        other=str(uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,%s,%s)',
                         (other,'fixture-other-'+uuid4().hex,'fixture-only',[]))
            conn.execute('UPDATE mailbox_registry SET owner_operator_id=%s WHERE id=%s',(other,mid))
            conn.execute('UPDATE onboarding_batches SET created_by=%s WHERE id=%s',(other,batch['batch_id']))
        self.call('PATCH','mailboxes/'+mid,dict(expected_version=1,changes={'disabled':True},request_key='fixture:other'),403)
        self.call('GET','tasks/'+batch['task_ids'][0],status=403)
        self.call('POST','preflight',dict(selection='specified',requested_count=1,mailbox_ids=[mid]),403)

    def test_real_scope_change_between_locator_and_pool_lock_has_no_fallback(self):
        from onboarding import task_read
        mid,body,batch=self.seeded_batch();tid=batch['task_ids'][0]
        locate=task_read.locate_scope
        def change_scope(*args,**kwargs):
            scope=locate(*args,**kwargs)
            with self.uow() as conn:
                conn.execute("UPDATE onboarding_tasks SET execution_scope='fixture',mailbox_id=NULL,platform=NULL,"
                    "credential_version=NULL,mailbox_credential_ref=NULL,platform_plan='[]'::jsonb,platform_credential_pins='{}'::jsonb WHERE id=%s",(tid,))
            return scope
        with patch('onboarding.task_read.locate_scope',side_effect=change_scope), \
             patch('webui.onboarding_routes.command_task',side_effect=AssertionError('fallback forbidden')) as fallback:
            self.call('POST','tasks/'+tid+'/pause',{'expected_version':1,'request_key':'fixture:scope-race'},403)
        fallback.assert_not_called()
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE idempotency_key='fixture:scope-race'"),[(0,)])

    def test_committed_command_response_failure_needs_original_key_reconciliation(self):
        _, _, batch = self.seeded_batch()
        task_id = batch['task_ids'][0]
        body = {'expected_version': 2, 'request_key': 'fixture:response-fault'}
        # Only response construction fails: the actual pool service has already
        # committed. A generic HTTP 503 does not prove the mutation rolled back.
        with patch('webui.onboarding_routes.JSONResponse',
                   side_effect=RuntimeError('fixture:private-response-canary')):
            failed = self.call('POST', 'tasks/' + task_id + '/pause', body, 503)
        self.assertEqual(failed['code'], 'DEPENDENCY_UNAVAILABLE')
        self.assertNotIn('canary', str(failed))
        self.assertEqual(self.read('SELECT status,version FROM onboarding_tasks WHERE id=%s',
                                   (task_id,)), [('PAUSED', 3)])
        receipts = self.read("SELECT id FROM operation_receipts WHERE task_id=%s "
                             "AND action='pool.command.pause' AND idempotency_key=%s",
                             (task_id, body['request_key']))
        self.assertEqual(len(receipts), 1)
        before = {table: self.read('SELECT * FROM ' + table + ' ORDER BY 1') for table in
                  ('onboarding_tasks', 'operation_receipts', 'resource_leases', 'task_steps')}
        # HTTP authentication legitimately appends auth.session_touch; only the
        # task's business audit must not be duplicated by receipt replay.
        audit_before = self.read('SELECT * FROM audit_events WHERE task_id=%s ORDER BY 1', (task_id,))
        replay = self.call('POST', 'tasks/' + task_id + '/pause', body, 202)
        self.assertEqual(replay['receipt_id'], str(receipts[0][0]))
        self.assertIsNone(replay['task'])
        self.assertEqual(before, {table: self.read('SELECT * FROM ' + table + ' ORDER BY 1') for table in before})
        self.assertEqual(audit_before, self.read('SELECT * FROM audit_events WHERE task_id=%s ORDER BY 1', (task_id,)))

    def error(self, method, path, body):
        response = self.client.request(method, '/api/onboarding/' + path, json=body, headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        return response.json()

    def test_http_not_committed_on_each_proved_write(self):
        mid, batch_body, batch = self.seeded_batch()
        config = self.call('GET', 'config')
        self.call('PUT', 'config', dict(expected_revision=config['revision'], fields=config['nonsecret_config'],
                                        request_key='fixture:http-config-2'))
        stale_config = dict(expected_revision=config['revision'], fields=config['nonsecret_config'],
                            request_key='fixture:http-stale-config')
        stale_batch = dict(batch_body, request_key='fixture:http-stale-batch')
        task = batch['task_ids'][0]
        stale_command = dict(expected_version=1, request_key='fixture:http-stale-command')
        self.call('PATCH', 'mailboxes/' + mid, dict(expected_version=1, changes={'group_ref': 'fixture:moved'},
                                                    request_key='fixture:http-move'))
        stale_patch = dict(expected_version=1, changes={'group_ref': 'fixture:late'}, request_key='fixture:http-stale')
        for method, path, body in (('PUT', 'config', stale_config), ('POST', 'batches', stale_batch),
                                   ('POST', 'tasks/' + task + '/pause', stale_command),
                                   ('PATCH', 'mailboxes/' + mid, stale_patch)):
            with self.subTest(path=path):
                body_seen = self.error(method, path, body)
                self.assertEqual(body_seen['code'], 'VERSION_CONFLICT')
                self.assertEqual(set(body_seen), {'code', 'correlation_id', 'not_committed'})
                self.assertIs(body_seen['not_committed'], True)

    def test_http_import_conflict_carries_proof(self):
        import json
        self.imported()
        raw = json.dumps({'email': 'http@fixture.invalid', 'password': 'fixture:other-http', 'provider': 'outlook'})
        preview = self.call('POST', 'mailboxes/import-preview', {'text': raw, 'group_ref': 'fixture:group'})
        body = self.error('POST', 'mailboxes/import', dict(text=raw, group_ref='fixture:group', duplicate_action='skip',
            preview_digest=preview['preview_digest'], expected_versions={}, request_key='fixture:http-conflict'))
        self.assertIs(body['not_committed'], True)

    def test_http_unproved_conflicts_keep_exact_error_shape(self):
        # No config exists yet: an expected revision here cannot be proved stale (spec position 4).
        fields = dict(model='fixture-model', region='fixture-region', instance_ref='fixture:sub2api',
                      group_ref='fixture:group', project_prefix='fixture-project', timeout_seconds=1800,
                      concurrency=1, retention_days=30)
        body = self.error('PUT', 'config', dict(expected_revision='pool-' + ID, fields=fields,
                                                request_key='fixture:http-ghost'))
        self.assertEqual(set(body), {'code', 'correlation_id'})
        mid, batch_body, batch = self.seeded_batch()
        body = self.error('POST', 'tasks/' + batch['task_ids'][0] + '/pause',
                          dict(expected_version=9, request_key='fixture:http-ahead'))
        self.assertEqual(set(body), {'code', 'correlation_id'})
