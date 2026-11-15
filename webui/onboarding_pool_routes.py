"""Fixed synthetic pool HTTP DTOs. No provider, key path or actor from HTTP."""
import re
import unicodedata
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

from onboarding import mailbox_read, mailbox_update, mailboxes, pool_batches, pool_config
from onboarding.errors import ErrorCode, ServiceError
from .onboarding_auth import read_json
from .onboarding_routes import _web_actor

_KEY = re.compile(r'[A-Za-z0-9._:-]{1,128}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')
_FIELDS = ('model', 'region', 'instance_ref', 'group_ref', 'project_prefix',
           'timeout_seconds', 'concurrency', 'retention_days')
_TASK = ('id', 'execution_scope', 'batch_id', 'config_id', 'config_revision', 'status',
         'version', 'reason_code', 'current_step', 'generation', 'cancel_requested', 'synthetic', 'execution_mode')
_MAILBOX = ('id', 'email', 'source_type', 'group_ref', 'health', 'disabled',
            'ever_registration_attempted', 'sale_eligibility', 'pool_status', 'version',
            'created_at', 'updated_at', 'last_used_at', 'occupied')


def invalid():
    raise ServiceError(ErrorCode.INVALID_INPUT)


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        invalid()
    return value


def uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            invalid()
    except (ValueError, TypeError, AttributeError):
        invalid()
    return value


def integer(value, high=9223372036854775807):
    if type(value) is not int or not 1 <= value <= high:
        invalid()
    return value


def text(value, maximum):
    if (type(value) is not str or len(value) > maximum
            or any(unicodedata.category(c) in ('Cc', 'Cf') for c in value)):
        invalid()
    try:
        value.encode('utf-8')
    except UnicodeError:
        invalid()
    return value


def request_key(value):
    if type(value) is not str or not _KEY.fullmatch(value):
        invalid()
    return value


def revision(value, optional=False):
    if value is None and optional:
        return value
    if type(value) is not str or not value.startswith('pool-'):
        invalid()
    uuid(value[5:])
    return value


def query(request, allowed=()):
    pairs = list(request.query_params.multi_items())
    values = dict(pairs)
    if len(values) != len(pairs) or not set(values) <= set(allowed):
        invalid()
    if 'limit' in values:
        if not re.fullmatch(r'[1-9][0-9]{0,2}', values['limit'], flags=re.ASCII):
            invalid()
        values['limit'] = integer(int(values['limit']), 100)
    return values


def selection(body):
    if type(body['selection']) is not str or body['selection'] not in ('automatic', 'specified'):
        invalid()
    integer(body['requested_count'], 100)
    ids = body['mailbox_ids']
    if type(ids) is not list or len(ids) > 100:
        invalid()
    for identity in ids:
        uuid(identity)
    if (len(set(ids)) != len(ids) or body['selection'] == 'automatic' and ids
            or body['selection'] == 'specified' and len(ids) != body['requested_count']):
        invalid()


def project(value, fields):
    # Even a future service expansion must not widen the response boundary.
    return {key: value[key] for key in fields}


def task_project(value, listed=False):
    return project(value, (*_TASK, 'created_at') if listed else _TASK)


def register_routes(app, config):
    if config.mode != 'protected' or not config.pool_ready:
        return
    router = APIRouter()

    def context(request):
        actor = getattr(request.state, 'onboarding_actor', None)
        _web_actor(actor)
        return config.settings, actor

    def dependencies():
        return dict(policy=config.pool_policy, mac=config.pool_mac)

    @router.get('/onboarding/pools')
    async def page(request: Request):
        context(request)
        query(request)
        from .onboarding_console import render_console
        static = Path(__file__).parent / 'static'
        page, _ = render_console((static / 'index.html').read_text(encoding='utf-8'),
                                 pool_source=(static / 'onboarding-pools.html').read_text(encoding='utf-8'),
                                 pool_mode=config.pool_mode)
        return HTMLResponse(page)

    @router.get('/static/onboarding-pools.js')
    async def script(request: Request):
        context(request)
        query(request)
        return FileResponse(Path(__file__).parent / 'static' / 'onboarding-pools.js', media_type='text/javascript')

    @router.get('/api/onboarding/mailboxes')
    async def list_mailboxes(request: Request):
        values = query(request, ('platform', 'search', 'group_ref', 'health', 'occupied', 'cursor', 'limit'))
        if 'occupied' in values:
            if values['occupied'] not in ('true', 'false'):
                invalid()
            values['occupied'] = values['occupied'] == 'true'
        try:
            mailbox_read._filters(values.get('platform'), values.get('search', ''), values.get('group_ref'),
                                  values.get('health'), values.get('occupied'), values.get('limit', 50))
        except (ValueError, TypeError, UnicodeError):
            invalid()
        if config.pool_mode == 'live':
            from onboarding import live_mailbox_read
            from onboarding.async_runtime import settled_thread
            result = await settled_thread(live_mailbox_read.list_page, *context(request), **values,
                                          policy=config.live_policy, mac=config.pool_mac)
        else:
            result = await run_in_threadpool(mailbox_read.list_page, *context(request), **values, **dependencies())
        items = [{**project(item, _MAILBOX), 'platforms': [project(p, ('platform', 'identity_status',
            'usage_status', 'version', 'checked_at')) for p in item['platforms']]} for item in result.items]
        return {'items': items, 'next_cursor': result.next_cursor}

    async def import_body(request, confirm=False):
        query(request)
        fields = ('text', 'group_ref', 'duplicate_action', 'preview_digest', 'expected_versions', 'request_key') if confirm else ('text', 'group_ref')
        body = exact(await read_json(request, 256 * 1024), fields)
        if type(body['text']) is not str:
            invalid()
        text(body['group_ref'], 128)
        if confirm:
            if (body['duplicate_action'] != 'skip' or type(body['expected_versions']) is not dict
                    or body['expected_versions'] or type(body['preview_digest']) is not str
                    or not _DIGEST.fullmatch(body['preview_digest'])):
                invalid()
            request_key(body['request_key'])
        return body

    @router.post('/api/onboarding/mailboxes/import-preview')
    async def preview(request: Request):
        body = await import_body(request)
        result = await run_in_threadpool(mailboxes.preview_import, *context(request), body['text'], body['group_ref'],
                                        vault=config.pool_vault, mac=config.pool_mac)
        return {**project(result, ('accepted_count', 'duplicate_count', 'conflict_count', 'preview_digest')),
                'items': [project(item, ('line', 'email', 'provider', 'group_ref')) for item in result['items']],
                'issues': [project(item, ('line', 'code')) for item in result['issues']]}

    @router.post('/api/onboarding/mailboxes/import')
    async def import_mailboxes(request: Request):
        body = await import_body(request, True)
        result = await run_in_threadpool(mailboxes.import_text, *context(request), body['text'], body['group_ref'],
            body['preview_digest'], body['request_key'], vault=config.pool_vault, mac=config.pool_mac)
        return {'created_ids': list(result.created_ids), 'skipped_ids': list(result.skipped_ids), 'request_key': result.request_key}

    @router.patch('/api/onboarding/mailboxes/{mailbox_id}')
    async def update_mailbox(request: Request, mailbox_id: str):
        query(request)
        uuid(mailbox_id)
        body = exact(await read_json(request), ('expected_version', 'changes', 'request_key'))
        changes = mailbox_update._inputs(mailbox_id, body['expected_version'], body['changes'], body['request_key'])
        result = await run_in_threadpool(mailbox_update.update, *context(request), mailbox_id,
            body['expected_version'], changes, body['request_key'], **dependencies())
        return project(result, ('mailbox_id', 'version', 'request_key'))

    @router.get('/api/onboarding/config')
    async def get_config(request: Request):
        if config.pool_mode == 'live':
            from .onboarding_live_routes import config_get
            return await config_get(request, config)
        query(request)
        result = await run_in_threadpool(pool_config.get_current, *context(request), policy=config.pool_policy,
                                        permission='config:manage')
        if result is None:
            return None
        pool_config._require_synthetic(result)
        return {**project(result, ('id', 'revision', 'scope', 'secrets_configured', 'created_at')),
                'nonsecret_config': project(result['nonsecret_config'], _FIELDS)}

    @router.put('/api/onboarding/config')
    async def replace_config(request: Request):
        if config.pool_mode == 'live':
            from .onboarding_live_routes import config_put
            return await config_put(request, config)
        query(request)
        body = exact(await read_json(request), ('expected_revision', 'fields', 'request_key'))
        fields = pool_config._inputs(body['expected_revision'], body['fields'], body['request_key'])
        result = await run_in_threadpool(pool_config.replace, *context(request), body['expected_revision'],
                                        fields, body['request_key'], **dependencies())
        return project(result, ('config_id', 'revision', 'request_key'))

    @router.post('/api/onboarding/preflight')
    async def preflight(request: Request):
        query(request)
        body = exact(await read_json(request), ('selection', 'requested_count', 'mailbox_ids'))
        selection(body)
        result = await run_in_threadpool(pool_batches.preflight, *context(request), body['selection'],
                                        body['requested_count'], body['mailbox_ids'], **dependencies())
        return project(result, ('selection', 'requested_count', 'eligible_count', 'config_revision',
                                'can_create', 'reason_codes', 'observation_only'))

    @router.post('/api/onboarding/batches')
    async def batches(request: Request):
        query(request)
        body = exact(await read_json(request), ('selection', 'requested_count', 'mailbox_ids',
                                               'expected_config_revision', 'request_key'))
        selection(body)
        revision(body['expected_config_revision'])
        request_key(body['request_key'])
        result = await run_in_threadpool(pool_batches.create, *context(request), body['selection'],
            body['requested_count'], body['mailbox_ids'], body['expected_config_revision'], body['request_key'], **dependencies())
        return JSONResponse(project(result, ('batch_id', 'task_ids', 'mailbox_ids', 'config_id',
                                             'config_revision', 'receipt_id', 'phase')), status_code=202)

    @router.get('/api/onboarding/tasks')
    async def tasks(request: Request):
        if config.pool_mode == 'live':
            from .onboarding_live_routes import tasks_list
            return await tasks_list(request, config)
        from onboarding import task_read
        values = query(request, ('scope', 'batch_id', 'cursor', 'limit'))
        if values.get('scope', 'all') not in ('all', 'fixture', 'pool'):
            invalid()
        if 'batch_id' in values:
            uuid(values['batch_id'])
        result = await run_in_threadpool(task_read.list_page, *context(request), **values, **dependencies())
        return {'items': [task_project(item, True) for item in result.items], 'next_cursor': result.next_cursor}

    @router.get('/api/onboarding/tasks/{task_id}/events')
    async def task_events(request: Request, task_id: str):
        if config.pool_mode == 'live':
            from .onboarding_live_routes import task_events as live_events
            return await live_events(request, config, task_id)
        from onboarding import task_events as events_service
        query(request)
        uuid(task_id)
        result = await run_in_threadpool(events_service.read, *context(request), task_id,
                                        policy=config.pool_policy)
        response = {**project(result, ('task_id', 'mode', 'execution_connected', 'has_more')),
                'events': [project(event, ('id', 'occurred_at', 'action', 'outcome', 'version'))
                           for event in result['events']]}
        if result['mode'] == 'live':
            response.update(effects_has_more=result['effects_has_more'], effect_events=[
                project(event, ('receipt_id', 'action', 'phase', 'result_code', 'version', 'updated_at'))
                for event in result['effect_events']])
        return response

    app.include_router(router)
    from .onboarding_card_routes import register_routes as register_card_routes
    register_card_routes(app, config)
    if config.pool_mode == 'live':
        from .onboarding_live_routes import register_routes as register_live_routes
        register_live_routes(app, config)
