"""Protected P1b control surface. Only owner-scoped synthetic records are usable.

No provider, task creation, approvals, scheduling, download or replay endpoints.
Authentication transactions finish in the guard before task-first business locks.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from onboarding import coordinator, repository, security, storage
from onboarding.errors import ErrorCode, ServiceError
from onboarding.migration_catalog import validate_applied

_TASK_FIELDS = ('id', 'status', 'version', 'reason_code', 'current_step',
                'generation', 'cancel_requested')


def _web_actor(actor):
    # Internal B1 fixture identities are never acceptable at the HTTP boundary.
    if type(actor) is not security.Actor:
        raise ServiceError(ErrorCode.FORBIDDEN)


def _snapshot(task):
    return {**{key: task[key] for key in _TASK_FIELDS}, 'synthetic': True}


def diagnostics(settings, actor):
    _web_actor(actor)
    with storage.unit_of_work(settings) as conn:
        security.revalidate(conn, actor, 'onboarding:read')
        versions = conn.execute('SELECT version,checksum FROM schema_migrations '
                                'ORDER BY version').fetchall()
        try:
            installed = validate_applied(versions)
        except ServiceError:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
        security.revalidate(conn, actor, 'onboarding:read')
    return {'mode': 'protected', 'scope': 'offline_foundation',
            'schema_version': installed, 'ready': True, 'real_flows_connected': False}


def read_task(settings, actor, task_id):
    _web_actor(actor)
    with storage.unit_of_work(settings) as conn:
        task = repository.lock_task(conn, actor, task_id, permission='onboarding:read')
        return _snapshot(task)


def command_task(settings, actor, task_id, action, expected_version, request_key):
    _web_actor(actor)
    commands = {'pause': coordinator.pause, 'cancel': coordinator.cancel,
                'recheck': coordinator.recheck}
    if type(action) is not str or action not in commands:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    with storage.unit_of_work(settings) as conn:
        # Each B1 command locks task, then revalidates the live B2 session and
        # scope. Never authenticate (session lock) then acquire task in this UoW.
        result = commands[action](conn, actor, task_id, expected_version, request_key)
        task = repository.lock_task(conn, actor, task_id, permission='tasks:manage')
        response = {'accepted': True, 'synthetic': True,
                    'receipt_id': result['receipt_id'], 'task': _snapshot(task)}
    # Accepted only after the entire transaction committed; UNKNOWN is not retried.
    return response


def _task_request(request, task_id, body=None):
    """Validate the original task DTO without importing any pool dependencies."""
    import re
    from uuid import UUID
    try:
        if request.query_params or type(task_id) is not str or str(UUID(task_id)) != task_id:
            raise ValueError
        if body is not None and (set(body) != {'expected_version', 'request_key'}
                or type(body['expected_version']) is not int
                or not 1 <= body['expected_version'] <= 9223372036854775807
                or type(body['request_key']) is not str
                or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', body['request_key'])):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def register_routes(app, config):
    if config.mode != 'protected':
        return
    from .onboarding_auth import read_json
    router = APIRouter()

    def context(request):
        actor = getattr(request.state, 'onboarding_actor', None)
        _web_actor(actor)
        if config.settings is None:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        return config.settings, actor

    @router.get('/api/onboarding/diagnostics')
    async def diagnostic_route(request: Request):
        return await run_in_threadpool(diagnostics, *context(request))

    @router.get('/api/onboarding/tasks/{task_id}')
    async def task_route(request: Request, task_id: str):
        _task_request(request, task_id)
        settings, actor = context(request)
        if getattr(config, 'pool_ready', False):
            from onboarding import task_read
            scope = await run_in_threadpool(task_read.locate_scope, settings, actor, task_id,
                                           policy=config.pool_policy, permission='onboarding:read')
            if scope == 'pool':
                result = await run_in_threadpool(task_read.read_pool, settings, actor, task_id,
                                                policy=config.pool_policy)
                from .onboarding_pool_routes import task_project
                return task_project(result)
            if scope != 'fixture':
                raise ServiceError(ErrorCode.FORBIDDEN)
        return await run_in_threadpool(read_task, settings, actor, task_id)

    async def command_route(request: Request, task_id: str, action: str):
        settings, actor = context(request)
        body = await read_json(request)
        _task_request(request, task_id, body)
        if getattr(config, 'pool_ready', False):
            from onboarding import task_read, pool_commands
            scope = await run_in_threadpool(task_read.locate_scope, settings, actor, task_id,
                                           policy=config.pool_policy, permission='tasks:manage')
            if scope == 'pool':
                commands = {'pause': pool_commands.pause, 'cancel': pool_commands.cancel,
                            'recheck': pool_commands.recheck}
                result = await run_in_threadpool(commands[action], settings, actor, task_id,
                    body['expected_version'], body['request_key'], policy=config.pool_policy, mac=config.pool_mac)
                # The service already committed. A separate read must never erase
                # this acknowledgement or encourage an automatic command retry.
                return JSONResponse({'accepted': True, 'synthetic': True, 'execution_scope': 'pool',
                    'receipt_id': result['receipt_id'], 'phase': result['phase'],
                    'task': None, 'snapshot_pending': True}, status_code=202)
            if scope != 'fixture':
                raise ServiceError(ErrorCode.FORBIDDEN)
        result = await run_in_threadpool(command_task, settings, actor, task_id, action,
                                        body['expected_version'], body['request_key'])
        return JSONResponse(result, status_code=202)

    # Concrete routes only: no dynamic action can expose a future coordinator.
    @router.post('/api/onboarding/tasks/{task_id}/pause')
    async def pause_route(request: Request, task_id: str):
        return await command_route(request, task_id, 'pause')

    @router.post('/api/onboarding/tasks/{task_id}/cancel')
    async def cancel_route(request: Request, task_id: str):
        return await command_route(request, task_id, 'cancel')

    @router.post('/api/onboarding/tasks/{task_id}/recheck')
    async def recheck_route(request: Request, task_id: str):
        return await command_route(request, task_id, 'recheck')

    app.include_router(router)
