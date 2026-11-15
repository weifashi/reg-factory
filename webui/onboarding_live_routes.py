"""Protected live HTTP adapters, never a client-selected execution environment.

Only the four /live URLs are registered here. Existing config/task URLs delegate
to the helpers below in explicit live mode, so no duplicate route can shadow a
different policy. Server BOOT owns the source, mailbox binding and manager;
import returns an identifier but never creates/rebinds a manager. No raw manager
summary, source path, credential reference or creation proof leaves this module.
"""
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from onboarding import live_commands, live_config, live_input_vault, live_read, pool_config
from onboarding.async_runtime import settled_thread
from onboarding.errors import ErrorCode, ServiceError
from onboarding.live_manager import Submission
from .onboarding_auth import read_json
from .onboarding_pool_routes import (exact, invalid, project, query, request_key,
                                     revision, task_project, uuid)
from .onboarding_routes import _task_request, _web_actor

_WORKFLOW = 'existing_billing_cloud_v1'
_SOURCE_REF = 'local-authorized-mailbox'
_START = ('batch_id', 'task_ids', 'mailbox_ids', 'config_id',
          'config_revision', 'receipt_id', 'replayed')


def _context(request, config):
    if config.mode != 'protected' or getattr(config, 'pool_mode', 'off') != 'live':
        raise ServiceError(ErrorCode.FORBIDDEN)
    actor = getattr(request.state, 'onboarding_actor', None)
    _web_actor(actor)
    if (not getattr(config, 'pool_ready', False) or config.settings is None
            or getattr(config, 'live_policy', None) is None or config.pool_mac is None):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    return config.settings, actor


def _dependencies(config):
    return dict(policy=config.live_policy, mac=config.pool_mac)


def _source(config, actor):
    source = getattr(config, 'live_source', None)
    if source is None or getattr(config, 'live_vault', None) is None:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if actor.operator_id != source.owner_id:
        raise ServiceError(ErrorCode.FORBIDDEN)
    return source


def _mailbox(config):
    value = getattr(config, 'live_mailbox_id', None)
    if value is not None:
        try:
            uuid(value)
        except ServiceError:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    return value


async def capabilities(request, config):
    query(request)
    _, actor = _context(request, config)
    source = getattr(config, 'live_source', None)
    available = (source is not None and actor.operator_id == source.owner_id
                 and getattr(config, 'live_vault', None) is not None)
    mailbox_id = _mailbox(config) if available else None
    manager = getattr(config, 'live_manager', None)
    return dict(mode='live', workflow=_WORKFLOW,
        source=dict(available=available, mailbox_id=mailbox_id, ref=_SOURCE_REF),
        start_available=available and mailbox_id is not None and manager is not None)


def _start_result(submission):
    # Submission.result is deliberately INTERNAL; copy only public scalar IDs
    # and arrays of IDs, not even the nested admission/creation-proof summaries.
    try:
        if type(submission) is not Submission or type(submission.scheduled) is not bool:
            raise ValueError
        value = project(submission.result, _START)
        for name in ('batch_id', 'config_id', 'receipt_id'):
            uuid(value[name])
        for name in ('task_ids', 'mailbox_ids'):
            if type(value[name]) is not list or len(value[name]) != 1:
                raise ValueError
            value[name] = [uuid(value[name][0])]
        revision(value['config_revision'])
        if (type(value['replayed']) is not bool
                or value['replayed'] and submission.scheduled):
            raise ValueError
    except (KeyError, TypeError, ValueError, ServiceError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    return dict(accepted=True, synthetic=False, execution_mode='live',
                scheduled=submission.scheduled, **value)


async def start_run(request, config):
    query(request)
    settings, actor = _context(request, config)
    body = exact(await read_json(request), ('mailbox_id', 'expected_config_revision', 'request_key'))
    uuid(body['mailbox_id'])
    revision(body['expected_config_revision'])
    request_key(body['request_key'])
    _source(config, actor)
    mailbox_id, manager = _mailbox(config), getattr(config, 'live_manager', None)
    if mailbox_id is None or manager is None:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
    if body['mailbox_id'] != mailbox_id:
        raise ServiceError(ErrorCode.FORBIDDEN)
    # Do not shield the whole request, schedule a second worker, retry unknown
    # commits, or perform a fallible follow-up read after the committed receipt.
    submission = await manager.submit(actor, body['expected_config_revision'], body['request_key'])
    return JSONResponse(_start_result(submission), status_code=202)


async def source_preview(request, config):
    query(request)
    settings, actor = _context(request, config)
    body = exact(await read_json(request), ('group_ref',))
    live_input_vault._text(body['group_ref'], 128)
    result = await settled_thread(live_input_vault.preview_source, settings, actor,
        _source(config, actor), body['group_ref'], vault=config.live_vault, mac=config.pool_mac)
    return {**project(result, ('email', 'provider', 'group_ref')), 'preview_digest': result['source_digest']}


async def source_import(request, config):
    query(request)
    settings, actor = _context(request, config)
    body = exact(await read_json(request), ('group_ref', 'preview_digest', 'request_key'))
    live_input_vault._text(body['group_ref'], 128)
    if type(body['preview_digest']) is not str or not live_input_vault._DIGEST.fullmatch(body['preview_digest']):
        invalid()
    request_key(body['request_key'])
    result = await settled_thread(live_input_vault.import_source, settings, actor,
        _source(config, actor), body['group_ref'], body['preview_digest'], body['request_key'],
        vault=config.live_vault, mac=config.pool_mac)
    # No manager creation or runtime config mutation. Binding this ID remains a
    # trusted BOOT operation, including when a prior import ACK was lost.
    return project(result, ('mailbox_id', 'created', 'request_key'))


async def config_get(request, config):
    query(request)
    result = await settled_thread(live_config.get_current, *_context(request, config),
        policy=config.live_policy, permission='config:manage')
    if result is None:
        return None
    try:
        fields = result['nonsecret_config']
        fields = live_config._fields(fields) if 'schema_version' in fields else pool_config._fields(fields)
    except (KeyError, ValueError, TypeError):
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    # A v1 head is still v1 history. Reading it never fills live defaults or
    # grants the original synthetic tasks a live execution capability.
    return {**project(result, ('id', 'revision', 'scope', 'secrets_configured', 'created_at')),
            'nonsecret_config': fields}


async def config_put(request, config):
    query(request)
    context = _context(request, config)
    body = exact(await read_json(request), ('expected_revision', 'fields', 'request_key'))
    fields = live_config._inputs(body['expected_revision'], body['fields'], body['request_key'])
    result = await settled_thread(live_config.replace, *context, body['expected_revision'], fields,
        body['request_key'], **_dependencies(config))
    return project(result, ('config_id', 'revision', 'request_key'))


async def tasks_list(request, config):
    values = query(request, ('scope', 'batch_id', 'cursor', 'limit'))
    if values.get('scope', 'all') not in ('all', 'fixture', 'pool'):
        invalid()
    if 'batch_id' in values:
        uuid(values['batch_id'])
    if 'cursor' in values and len(values['cursor']) > 512:
        invalid()
    result = await settled_thread(live_read.list_page, *_context(request, config),
                                  **values, **_dependencies(config))
    return dict(items=[task_project(item, True) for item in result.items], next_cursor=result.next_cursor)


async def task_detail(request, config, task_id):
    _task_request(request, task_id)
    result = await settled_thread(live_read.read_task, *_context(request, config), task_id,
                                  policy=config.live_policy)
    return task_project(result)


async def task_events(request, config, task_id):
    _task_request(request, task_id)
    result = await settled_thread(live_read.events, *_context(request, config), task_id,
                                  policy=config.live_policy)
    response = {**project(result, ('task_id', 'mode', 'execution_connected', 'has_more')),
        'events': [project(event, ('id', 'occurred_at', 'action', 'outcome', 'version'))
                   for event in result['events']]}
    if result['mode'] == 'live':
        response.update(effects_has_more=result['effects_has_more'], effect_events=[
            project(event, ('receipt_id', 'action', 'phase', 'result_code', 'version', 'updated_at'))
            for event in result['effect_events']])
    return response


async def task_command(request, config, task_id, action):
    context = _context(request, config)
    _task_request(request, task_id)
    if action not in ('pause', 'cancel'):
        raise ServiceError(ErrorCode.FORBIDDEN)
    body = await read_json(request)
    _task_request(request, task_id, body)
    located = await settled_thread(live_read.locate, *context, task_id,
                                   policy=config.live_policy, permission='tasks:manage')
    if located != {'execution_scope': 'pool', 'execution_mode': 'live'}:
        raise ServiceError(ErrorCode.FORBIDDEN)
    command = live_commands.pause if action == 'pause' else live_commands.cancel
    result = await settled_thread(command, *context, task_id, body['expected_version'],
                                  body['request_key'], **_dependencies(config))
    return JSONResponse(dict(accepted=True, synthetic=False, execution_scope='pool',
        execution_mode='live', **project(result, ('receipt_id', 'phase')),
        task=None, snapshot_pending=True), status_code=202)


def register_routes(app, config):
    if config.mode != 'protected' or getattr(config, 'pool_mode', 'off') != 'live':
        return
    router = APIRouter()

    @router.get('/api/onboarding/live/capabilities')
    async def capabilities_route(request: Request):
        return await capabilities(request, config)

    @router.post('/api/onboarding/live/start')
    async def start_route(request: Request):
        return await start_run(request, config)

    @router.post('/api/onboarding/live/source-preview')
    async def preview_route(request: Request):
        return await source_preview(request, config)

    @router.post('/api/onboarding/live/source-import')
    async def import_route(request: Request):
        return await source_import(request, config)

    app.include_router(router)
