"""Exact card HTTP boundaries; only trusted boot/auth seams are mocked offline."""
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from onboarding import card_import, security
from onboarding.errors import ErrorCode, ServiceError
from onboarding.settings import Settings, SOCKET
from webui import onboarding_auth

ORIGIN = 'https://fixture.local'
ID = '12345678-1234-4234-9234-123456789abc'
HEADERS = {'Origin': ORIGIN, 'X-CSRF-Token': 'c' * 43}
CARD_FIELDS = {'id', 'alias', 'brand', 'last4', 'enabled', 'account_limit', 'linked_count',
               'reserved_count', 'reconciliation_required', 'version', 'revision', 'sms_configured',
               'created_at', 'updated_at', 'last_assigned_at'}


class CardApiOfflineTests(unittest.TestCase):
    @contextmanager
    def client(self, *, ready=True, mode='synthetic', permission=True, actor=True):
        settings = Settings(SOCKET, 55433, 'rf_onboarding_test', 'rf_onboarding_app',
                            'fixture-only-placeholder', 'rf-onboarding-p1b-v1:fixture', 'rf_onboarding')
        with ExitStack() as stack:
            stack.enter_context(patch('webui.onboarding_auth._pool_context',
                                      return_value=(object(), object()) if ready else None))
            identity = security.Actor(ID, frozenset({'onboarding:read', 'cards:manage'} if permission
                                                   else {'onboarding:read'}), ID, 1) if actor else object()
            auth = stack.enter_context(patch('webui.onboarding_auth._authenticate_request', return_value=identity))
            app = FastAPI()
            config = onboarding_auth.install(app, mode='protected', expected_origin=ORIGIN,
                settings=settings, pool_mode=mode, pool_keyring=object(), pool_mac=object())
            from webui.onboarding_pool_routes import register_routes
            register_routes(app, config)
            client = stack.enter_context(TestClient(app, base_url=ORIGIN))
            yield client, auth, config

    def preview(self):
        return dict(items=[dict(line=2, alias='fixture:card', brand='visa', last4='1111', account_limit=3,
                        sms_configured=True, billing_configured=True, expiry_configured=True,
                        pan='private-pan', sms_channel_ref='private-channel')],
                    issues=[dict(line=3, code='INVALID_RECORD', raw='private-csv')], accepted_count=1,
                    duplicate_count=0, conflict_count=0, can_import=False, preview_digest=None,
                    private='private-extra')

    def test_preview_is_reachable_cards_manage_and_allowlist_projected(self):
        with patch('onboarding.cards.preview_import', return_value=self.preview()) as service:
            with self.client() as (client, auth, config):
                response = client.post('/api/onboarding/cards/import-preview', json={'text': 'fixture'}, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(auth.call_args.args[-1], 'cards:manage')
        self.assertNotIn('private', response.text)
        self.assertEqual(set(response.json()), {'items', 'issues', 'accepted_count', 'duplicate_count',
                                               'conflict_count', 'can_import', 'preview_digest'})
        service.assert_called_once()
        self.assertEqual(service.call_args.args[2], 'fixture')
        self.assertIs(service.call_args.kwargs['vault'], config.pool_vault)

    def test_readiness_off_unknown_method_and_transport_fail_closed(self):
        paths = ['/api/onboarding/cards', '/api/onboarding/cards/template',
                 '/api/onboarding/cards/import-preview', '/api/onboarding/cards/import', '/api/onboarding/cards/'+ID]
        methods = ['GET', 'GET', 'POST', 'POST', 'PATCH']
        for ready, mode, status in ((False, 'synthetic', 503), (True, 'off', 403)):
            with self.client(ready=ready, mode=mode) as (client, _, _):
                for method, path in zip(methods, paths):
                    self.assertEqual(client.request(method, path, headers=HEADERS, json={}).status_code, status)
        with self.client() as (client, _, _):
            for method, path in [('HEAD', paths[0]), ('OPTIONS', paths[0]), ('DELETE', paths[-1]),
                                 ('GET', paths[-1]), ('GET', paths[0]+'/future'),
                                 ('GET', '/api/onboarding/%63ards')]:
                self.assertEqual(client.request(method, path, headers=HEADERS).status_code, 403)
            self.assertEqual(client.get(paths[0], headers={'Origin': 'https://evil.invalid'}).status_code, 403)

    def test_handler_does_not_trust_missing_permission_or_fixture_actor(self):
        with patch('onboarding.cards.preview_import') as service:
            for options in ({'permission': False}, {'actor': False}):
                with self.client(**options) as (client, _, _):
                    response = client.post('/api/onboarding/cards/import-preview', json={'text': 'fixture'}, headers=HEADERS)
                    self.assertEqual(response.status_code, 403)
        service.assert_not_called()

    def test_preview_exact_body_no_secret_query_duplicate_keys_or_oversize(self):
        invalid = ['{}', '{"text":"x","text":"y"}', '{"text":true}', '{"text":null}',
                   '{"text":"x","cvv":"123"}', '{"text":"x","actor":"other"}',
                   json.dumps({'text': 'x'*262145}), json.dumps({'text': '\ud800'})]
        with patch('onboarding.cards.preview_import') as service:
            with self.client() as (client, _, _):
                for raw in invalid:
                    response = client.post('/api/onboarding/cards/import-preview', content=raw,
                                           headers={**HEADERS, 'Content-Type':'application/json'})
                    self.assertEqual(response.status_code, 422, len(raw))
                    self.assertEqual(set(response.json()), {'code', 'correlation_id'})
                self.assertEqual(client.post('/api/onboarding/cards/import-preview?pan=4111111111111111',
                                 json={'text':'fixture'}, headers=HEADERS).status_code, 422)
        service.assert_not_called()

    def test_json_envelope_allows_maximum_csv_escaping_overhead(self):
        with patch('onboarding.cards.preview_import', return_value=self.preview()) as service:
            with self.client() as (client, _, _):
                raw = json.dumps({'text':'\x00'*262144})
                self.assertGreater(len(raw), 262144)
                response = client.post('/api/onboarding/cards/import-preview', content=raw,
                                       headers={**HEADERS, 'Content-Type':'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(service.call_args.args[2].encode()), 262144)

    def test_import_exact_input_and_result_no_proof_extension(self):
        body = dict(text='fixture', preview_digest='a'*64, request_key='fixture:key')
        with patch('onboarding.cards.import_csv', return_value=SimpleNamespace(
                created_ids=(ID,), skipped_ids=(), request_key='fixture:key', secret_ref='private')) as service:
            with self.client() as (client, auth, _):
                response = client.post('/api/onboarding/cards/import', json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), dict(created_ids=[ID], skipped_ids=[], request_key='fixture:key'))
                self.assertEqual(auth.call_args.args[-1], 'cards:manage')
                for extra in ({'preview_digest': True}, {'request_key': True}, {'preview_digest':'A'*64},
                              {'duplicate_action':'update'}, {'cvv':'123'}, {'pan':'4111111111111111'}):
                    self.assertEqual(client.post('/api/onboarding/cards/import', json={**body, **extra},
                                                 headers=HEADERS).status_code, 422)
                self.assertEqual(service.call_count, 1)
                service.side_effect = ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)
                response = client.post('/api/onboarding/cards/import', json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(set(response.json()), {'code', 'correlation_id'})

    def test_template_checks_live_policy_and_permission_before_returning_header(self):
        events = []
        @contextmanager
        def uow(settings):
            events.append('begin')
            yield object()
            events.append('commit')
        with self.client() as (client, auth, config), patch('onboarding.storage.unit_of_work', uow), \
             patch('onboarding.security.revalidate', side_effect=lambda conn, actor, permission:
                   events.append(permission)) as live:
            object.__setattr__(config, 'pool_policy', SimpleNamespace(
                _validate=lambda: events.append('validate'), _connection=lambda conn: events.append('schema')))
            response = client.get('/api/onboarding/cards/template')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'csv': card_import.csv_template(), 'data_policy':'synthetic_only'})
            self.assertEqual(auth.call_args.args[-1], 'cards:manage')
            self.assertEqual(events, ['validate', 'begin', 'schema', 'cards:manage', 'cards:manage', 'commit'])
            live.side_effect = ServiceError(ErrorCode.FORBIDDEN)
            response = client.get('/api/onboarding/cards/template')
            self.assertEqual(response.status_code, 403)
            self.assertEqual(set(response.json()), {'code', 'correlation_id'})
            self.assertEqual(client.get('/api/onboarding/cards/template?pan=x').status_code, 422)

    def test_static_cards_script_is_explicit_read_only_and_query_strict(self):
        from starlette.responses import Response
        with self.client(permission=False) as (client, auth, _), \
             patch('webui.onboarding_card_routes.FileResponse', return_value=Response('// fixture card script')):
            response = client.get('/static/onboarding-cards.js')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(auth.call_args.args[-1], 'onboarding:read')
            for path in ('/static/onboarding-cards.js?x=1', '/static/onboarding-cards.js?limit=1&limit=2'):
                self.assertEqual(client.get(path).status_code, 422)
            self.assertEqual(client.head('/static/onboarding-cards.js').status_code, 403)

    def test_list_typed_filters_paging_and_exact_response_projection(self):
        record = {field: None for field in CARD_FIELDS}
        record.update(id=ID, alias='fixture:card', last4='1111', enabled=True, account_limit=3,
                      linked_count=0, reserved_count=1, reconciliation_required=False,
                      version=1, revision=1, sms_configured=False, pan_secret_ref='private-pan',
                      billing_identity_ref='private-billing', sms_channel_ref='private-channel')
        with patch('onboarding.card_management.list_page', return_value=SimpleNamespace(items=(record,), next_cursor='next')) as service:
            with self.client() as (client, auth, config):
                response = client.get('/api/onboarding/cards?search=1111&enabled=false&limit=2&cursor=cursor')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(auth.call_args.args[-1], 'cards:manage')
                self.assertEqual(set(response.json()['items'][0]), CARD_FIELDS)
                self.assertEqual(response.json()['next_cursor'], 'next')
                self.assertNotIn('private', response.text)
                self.assertEqual(service.call_args.kwargs, dict(search='1111', enabled=False, limit=2,
                                  cursor='cursor', policy=config.pool_policy, mac=config.pool_mac))

    def test_list_rejects_unknown_duplicate_invalid_and_pan_queries_before_service(self):
        with patch('onboarding.card_management.list_page') as service:
            with self.client() as (client, _, _):
                for params in ('search=a&search=b', 'enabled=1', 'enabled=True', 'limit=0', 'limit=101',
                               'limit=01', 'limit=true', 'pan=4111111111111111', 'owner=other',
                               'search=4111111111111111', 'search=%00', 'search='+('x'*81), 'cursor='+('x'*513)):
                    self.assertEqual(client.get('/api/onboarding/cards?'+params).status_code, 422, params[:50])
        service.assert_not_called()

    def test_patch_exact_metadata_contract_projection_and_no_commit_proof(self):
        body = dict(expected_version=1, changes={'enabled':False}, request_key='fixture:update')
        with patch('onboarding.card_management.update', return_value=dict(card_id=ID, version=2,
                   request_key='fixture:update', secret_ref='private')) as service:
            with self.client() as (client, auth, _):
                response = client.patch('/api/onboarding/cards/'+ID, json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), dict(card_id=ID, version=2, request_key='fixture:update'))
                self.assertEqual(auth.call_args.args[-1], 'cards:manage')
                self.assertEqual(service.call_args.args[2:], (ID, 1, {'enabled':False}, 'fixture:update'))
                service.side_effect = ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)
                response = client.patch('/api/onboarding/cards/'+ID, json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(set(response.json()), {'code', 'correlation_id'})

    def test_patch_rejects_secret_counters_wrongtypes_duplicates_and_url_body_leaks(self):
        body = dict(expected_version=1, changes={'enabled':False}, request_key='fixture:update')
        changes = [{}, {'pan':'4111111111111111'}, {'cvv':'123'}, {'linked_count':1}, {'reserved_count':0},
                   {'reconciliation_required':False}, {'enabled':1}, {'account_limit':True},
                   {'account_limit':0}, {'account_limit':10001}, {'alias':''}, {'alias':'4111111111111111'},
                   {'sms_channel_ref':True}, {'sms_channel_ref':'x'*129}]
        with patch('onboarding.card_management.update') as service:
            with self.client() as (client, _, _):
                for change in changes:
                    self.assertEqual(client.patch('/api/onboarding/cards/'+ID, json={**body,'changes':change}, headers=HEADERS).status_code, 422)
                for extra in ({'expected_version':True}, {'request_key':True}, {'actor':'other'}):
                    self.assertEqual(client.patch('/api/onboarding/cards/'+ID, json={**body,**extra}, headers=HEADERS).status_code, 422)
                self.assertEqual(client.patch('/api/onboarding/cards/'+ID.upper(), json=body, headers=HEADERS).status_code, 422)
                self.assertEqual(client.patch('/api/onboarding/cards/'+ID+'?cvv=123', json=body, headers=HEADERS).status_code, 422)
                raw='{"expected_version":1,"changes":{"enabled":true,"enabled":false},"request_key":"fixture:k"}'
                self.assertEqual(client.patch('/api/onboarding/cards/'+ID, content=raw,
                                 headers={**HEADERS,'Content-Type':'application/json'}).status_code, 422)
        service.assert_not_called()


from onboarding_pool_support import PoolCase


class CardApiDatabaseTests(PoolCase):
    """Real auth, service and PostgreSQL in disposable synthetic fixture schemas."""
    def setUp(self):
        super().setUp()
        from webui.onboarding_pool_routes import register_routes
        self.app = FastAPI()
        config = onboarding_auth.install(self.app, mode='protected', expected_origin=ORIGIN,
            settings=self.settings, pool_mode='synthetic', pool_keyring=self.keyring, pool_mac=self.mac)
        self.assertTrue(config.pool_ready)
        register_routes(self.app, config)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.addCleanup(self.client.close)
        self.client.cookies.set(onboarding_auth.SESSION_COOKIE, self.token)
        self.headers = {'Origin':ORIGIN, 'X-CSRF-Token':self.csrf}

    def csv(self, second=False):
        import csv
        import io
        out = io.StringIO(newline='')
        writer = csv.writer(out, lineterminator='\n')
        writer.writerow(card_import.csv_template().strip().split(','))
        for alias, pan in [('fixture:visa','4111111111111111')] + (
                [('fixture:mastercard','5555555555554444')] if second else []):
            writer.writerow([alias, pan, 12, 2099, 'fixture:holder', 'US', 'fixture:line', '',
                             'fixture:city', 'fixture:region', 'fixture:postal', 3, 'fixture:sms'])
        return out.getvalue()

    def call(self, method, suffix, body=None, status=200):
        response = self.client.request(method, '/api/onboarding/cards'+suffix, json=body, headers=self.headers)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()

    def imported(self, second=False):
        text = self.csv(second)
        preview = self.call('POST', '/import-preview', {'text':text})
        body = dict(text=text, preview_digest=preview['preview_digest'], request_key='fixture:http-import')
        result = self.call('POST', '/import', body)
        return result, body

    def test_real_preview_persist_list_update_and_idempotent_replay(self):
        self.assertEqual(self.call('GET', '/template'), {'csv':card_import.csv_template(), 'data_policy':'synthetic_only'})
        preview = self.call('POST', '/import-preview', {'text':self.csv()})
        self.assertTrue(preview['can_import'])
        self.assertEqual(self.read('SELECT count(*) FROM payment_cards'), [(0,)])
        result, body = self.imported()
        self.assertEqual(self.call('POST', '/import', body), result)
        self.assertEqual(self.read("SELECT count(*) FROM operation_receipts WHERE action='card.import'"), [(1,)])
        item = self.call('GET', '')['items'][0]
        self.assertEqual(set(item), CARD_FIELDS)
        self.assertEqual(item['id'], result['created_ids'][0])
        self.assertEqual(item['last4'], '1111')
        for secret in ('4111111111111111', 'fixture:holder', 'fixture:line', 'fixture:sms', 'secret_ref', 'billing_identity_ref'):
            self.assertNotIn(secret, repr(item)+repr(preview))
        command = dict(expected_version=item['version'], changes={'enabled':False,'alias':'fixture:updated'},
                       request_key='fixture:http-update')
        updated = self.call('PATCH', '/'+item['id'], command)
        self.assertEqual(updated['version'], item['version']+1)
        self.assertEqual(self.call('PATCH', '/'+item['id'], command), updated)
        item = self.call('GET', '?enabled=false')['items'][0]
        self.assertEqual(item['alias'], 'fixture:updated')
        self.assertFalse(item['enabled'])
        self.assertEqual(item['reserved_count'], 0)
        self.assertEqual(item['linked_count'], 0)
        stale = self.call('PATCH', '/'+item['id'], {**command,'request_key':'fixture:stale'}, status=409)
        self.assertEqual(set(stale), {'code','correlation_id'})
        self.assertEqual(stale['code'], 'VERSION_CONFLICT')

    def test_real_list_search_filter_and_cursor_bindings(self):
        result, _ = self.imported(second=True)
        first = self.call('GET', '?limit=1')
        self.assertEqual(len(first['items']), 1)
        self.assertIsNotNone(first['next_cursor'])
        second = self.call('GET', '?limit=1&cursor='+first['next_cursor'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual({first['items'][0]['id'], second['items'][0]['id']}, set(result['created_ids']))
        self.assertEqual(self.call('GET', '?search=1111')['items'][0]['last4'], '1111')
        self.assertEqual(len(self.call('GET', '?search=fixture%3Amastercard')['items']), 1)
        self.assertEqual(self.call('GET', '?enabled=false')['items'], [])
        self.call('GET', '?search=1111&cursor='+first['next_cursor'], status=422)

    def test_real_owner_isolation_and_forbidden_update(self):
        from uuid import uuid4
        import secrets
        from onboarding import cards
        other, session = str(uuid4()), str(uuid4())
        with self.uow() as conn:
            conn.execute('INSERT INTO operators(id,username_norm,password_hash,permissions) VALUES(%s,%s,%s,%s)',
                         (other, 'fixture-other', 'fixture-only', ['cards:manage']))
            conn.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                         "VALUES(%s,%s,%s,%s,1,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                         (session, other, secrets.token_hex(32), secrets.token_hex(32)))
        actor = security.Actor(other, frozenset({'cards:manage'}), session, 1)
        text = self.csv()
        preview = cards.preview_import(self.settings, actor, text, vault=self.vault, mac=self.mac)
        result = cards.import_csv(self.settings, actor, text, preview['preview_digest'], 'fixture:other',
                                  vault=self.vault, mac=self.mac)
        self.assertEqual(self.call('GET', '')['items'], [])
        self.call('PATCH', '/'+result.created_ids[0], dict(expected_version=1, changes={'enabled':False},
                  request_key='fixture:other-write'), status=403)

    def test_real_csrf_session_and_permission_revocation_no_writes(self):
        response = self.client.post('/api/onboarding/cards/import-preview', json={'text':self.csv()},
                                    headers={'Origin':ORIGIN,'X-CSRF-Token':'wrong'})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/onboarding/cards/import-preview', json={'text':self.csv()},
                                    headers={'Origin':'https://evil.invalid','X-CSRF-Token':self.csrf})
        self.assertEqual(response.status_code, 403)
        with self.uow() as conn:
            conn.execute("UPDATE operators SET permissions=array_remove(permissions,'cards:manage') WHERE id=%s",
                         (self.actor.operator_id,))
        self.call('GET', '', status=403)
        self.call('GET', '/template', status=403)
        self.call('POST', '/import-preview', {'text':self.csv()}, status=403)
        self.assertEqual(self.read('SELECT count(*) FROM payment_cards'), [(0,)])
        self.client.cookies.clear()
        self.call('GET', '', status=401)
