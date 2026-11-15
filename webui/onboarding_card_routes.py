"""Protected synthetic card DTOs; no plaintext secrets in responses or URLs."""
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse

from onboarding import card_import, card_management, cards, security, storage
from onboarding.errors import ErrorCode, ServiceError
from .onboarding_auth import read_json
from .onboarding_pool_routes import exact, invalid, project, query, request_key, _DIGEST
from .onboarding_routes import _web_actor

_CARD = ('id', 'alias', 'brand', 'last4', 'enabled', 'account_limit', 'linked_count',
         'reserved_count', 'reconciliation_required', 'version', 'revision', 'sms_configured',
         'created_at', 'updated_at', 'last_assigned_at')
_PREVIEW = ('line', 'alias', 'brand', 'last4', 'account_limit', 'sms_configured',
            'billing_configured', 'expiry_configured')


def _template(settings, actor, policy):
    # Even a read-only header must revalidate the live session and schema policy.
    policy._validate()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'cards:manage')
        result = {'csv': card_import.csv_template(), 'data_policy': 'synthetic_only'}
        security.revalidate(conn, actor, 'cards:manage')
    return result


def register_routes(app, config):
    if config.mode != 'protected' or not config.pool_ready:
        return
    router = APIRouter()

    def context(request, permission='cards:manage'):
        actor = getattr(request.state, 'onboarding_actor', None)
        _web_actor(actor)
        if permission not in actor.permissions:
            raise ServiceError(ErrorCode.FORBIDDEN)
        if config.settings is None:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        return config.settings, actor

    async def import_body(request, confirm=False):
        query(request)
        body = exact(await read_json(request, 2 * 1024 * 1024),
                     ('text', 'preview_digest', 'request_key') if confirm else ('text',))
        if type(body['text']) is not str:
            invalid()
        try:
            if len(body['text'].encode('utf-8')) > card_import._MAX_BYTES:
                invalid()
        except UnicodeError:
            invalid()
        if confirm:
            if type(body['preview_digest']) is not str or not _DIGEST.fullmatch(body['preview_digest']):
                invalid()
            request_key(body['request_key'])
        return body

    @router.get('/static/onboarding-cards.js')
    async def script(request: Request):
        context(request, 'onboarding:read')
        query(request)
        return FileResponse(Path(__file__).parent / 'static' / 'onboarding-cards.js', media_type='text/javascript')

    @router.get('/api/onboarding/cards')
    async def list_cards(request: Request):
        settings, actor = context(request)
        values = query(request, ('search', 'enabled', 'cursor', 'limit'))
        if 'enabled' in values:
            if values['enabled'] not in ('true', 'false'):
                invalid()
            values['enabled'] = values['enabled'] == 'true'
        try:
            card_management._metadata(values.get('search', ''), 80, empty=True)
            if 'cursor' in values and not 1 <= len(values['cursor']) <= 512:
                invalid()
        except (ValueError, TypeError, UnicodeError):
            invalid()
        result = await run_in_threadpool(card_management.list_page, settings, actor, **values,
                                        policy=config.pool_policy, mac=config.pool_mac)
        return {'items': [project(item, _CARD) for item in result.items], 'next_cursor': result.next_cursor}

    @router.get('/api/onboarding/cards/template')
    async def template(request: Request):
        settings, actor = context(request)
        query(request)
        return await run_in_threadpool(_template, settings, actor, config.pool_policy)

    @router.post('/api/onboarding/cards/import-preview')
    async def preview(request: Request):
        settings, actor = context(request)
        body = await import_body(request)
        result = await run_in_threadpool(cards.preview_import, settings, actor, body['text'],
                                        vault=config.pool_vault, mac=config.pool_mac)
        return {**project(result, ('accepted_count', 'duplicate_count', 'conflict_count', 'can_import', 'preview_digest')),
                'items': [project(item, _PREVIEW) for item in result['items']],
                'issues': [project(item, ('line', 'code')) for item in result['issues']]}

    @router.post('/api/onboarding/cards/import')
    async def import_cards(request: Request):
        settings, actor = context(request)
        body = await import_body(request, True)
        result = await run_in_threadpool(cards.import_csv, settings, actor, body['text'],
            body['preview_digest'], body['request_key'], vault=config.pool_vault, mac=config.pool_mac)
        return {'created_ids': list(result.created_ids), 'skipped_ids': list(result.skipped_ids),
                'request_key': result.request_key}

    @router.patch('/api/onboarding/cards/{card_id}')
    async def update_card(request: Request, card_id: str):
        settings, actor = context(request)
        query(request)
        body = exact(await read_json(request), ('expected_version', 'changes', 'request_key'))
        changes = card_management._inputs(card_id, body['expected_version'], body['changes'], body['request_key'])
        result = await run_in_threadpool(card_management.update, settings, actor, card_id,
            body['expected_version'], changes, body['request_key'], policy=config.pool_policy, mac=config.pool_mac)
        return project(result, ('card_id', 'version', 'request_key'))

    app.include_router(router)
