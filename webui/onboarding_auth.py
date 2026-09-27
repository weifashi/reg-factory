"""Explicit fail-closed ASGI policy for the local onboarding control plane."""
from dataclasses import dataclass, field

LEGACY_ROUTES = (('POST', '/api/test/{target}'),
 ('GET', '/api/assets/summary'),
 ('GET', '/api/assets/emails'),
 ('GET', '/api/assets/cookies/{platform}'),
 ('POST', '/api/assets/cursors/reset'),
 ('POST', '/api/assets/export'),
 ('GET', '/api/assets/scan'),
 ('POST', '/api/assets/scan'),
 ('GET', '/api/scripts'),
 ('GET', '/api/links'),
 ('GET', '/api/embeds'),
 ('GET', '/api/k12/status'),
 ('POST', '/api/k12/start'),
 ('GET', '/api/chatgpt-plus/status'),
 ('POST', '/api/chatgpt-plus/start'),
 ('POST', '/api/chatgpt-plus/import-codex'),
 ('GET', '/api/chatgpt-plus/protocol-status'),
 ('POST', '/api/chatgpt-plus/protocol-batch'),
 ('GET', '/api/chatgpt-plus/export-ats'),
 ('GET', '/chatgpt-plus/{path:path}'),
 ('POST', '/chatgpt-plus/{path:path}'),
 ('PUT', '/chatgpt-plus/{path:path}'),
 ('DELETE', '/chatgpt-plus/{path:path}'),
 ('OPTIONS', '/chatgpt-plus/{path:path}'),
 ('GET', '/api/chatgpt-plus/workbench/{path:path}'),
 ('POST', '/api/chatgpt-plus/workbench/{path:path}'),
 ('PUT', '/api/chatgpt-plus/workbench/{path:path}'),
 ('DELETE', '/api/chatgpt-plus/workbench/{path:path}'),
 ('OPTIONS', '/api/chatgpt-plus/workbench/{path:path}'),
 ('GET', '/api/mailpool'),
 ('POST', '/api/mailpool'),
 ('POST', '/api/sms/rent'),
 ('POST', '/api/sms/code'),
 ('POST', '/api/sms/release'),
 ('GET', '/api/sms/rents'),
 ('GET', '/api/sms/custom'),
 ('POST', '/api/sms/custom'),
 ('GET', '/api/gopay/status'),
 ('GET', '/api/gopay/accounts'),
 ('POST', '/api/gopay/accounts/{phone}/balance'),
 ('POST', '/api/gopay/accounts/{phone}/relogin'),
 ('POST', '/api/gopay/accounts/{phone}/delete'),
 ('POST', '/api/gopay/accounts/clear'),
 ('GET', '/api/gopay/phones'),
 ('POST', '/api/gopay/phones/import'),
 ('POST', '/api/gopay/phones/{phone}/delete'),
 ('POST', '/api/gopay/phones/clear'),
 ('GET', '/api/gopay/sms'),
 ('POST', '/api/gopay/sms'),
 ('GET', '/api/gopay/register/jobs'),
 ('POST', '/api/gopay/register'),
 ('POST', '/api/gopay/register/batch'),
 ('POST', '/api/gopay/register/jobs/{job_id}/otp'),
 ('GET', '/api/gopay/payments'),
 ('GET', '/api/gopay/payments/{job_id}'),
 ('POST', '/api/gopay/payments'),
 ('POST', '/api/gopay/payments/{job_id}/otp'),
 ('POST', '/api/update'),
 ('GET', '/api/status'),
 ('GET', '/api/proxy'),
 ('POST', '/api/proxy'),
 ('POST', '/api/proxy/rotate'),
 ('POST', '/api/proxy/test'),
 ('POST', '/api/run'),
 ('POST', '/api/authorize-outlook'),
 ('GET', '/api/logs/{run_id}'),
 ('POST', '/api/stop/{run_id}'),
 ('POST', '/api/stop-all'))

@dataclass(frozen=True)
class WebConfig:
    mode: str
    expected_origin: str | None = None
    settings: object = field(default=None, repr=False)
    ready: bool = False
    pool_mode: str = 'off'
    pool_ready: bool = False
    pool_keyring: object = field(default=None, repr=False)
    pool_mac: object = field(default=None, repr=False)
    pool_policy: object = field(default=None, repr=False)
    pool_vault: object = field(default=None, repr=False)

# This is an explicit policy, not runtime discovery: a future route stays denied.
_PUBLIC = {('GET', path) for path in (
    '/login', '/static/onboarding-login.html', '/static/onboarding-login.js',
    '/static/onboarding.css', '/api/auth/bootstrap', '/healthz')}
_FIXED = {
    **{('GET', path): 'onboarding:read' for path in (
        '/', '/onboarding', '/static/onboarding.js', '/docs', '/redoc',
        '/openapi.json', '/docs/oauth2-redirect', '/api/onboarding/diagnostics')},
    ('GET', '/api/env'): 'config:manage', ('POST', '/api/env'): 'config:manage',
    ('GET', '/api/auth/session'): None, ('POST', '/api/auth/logout'): None,
}
SESSION_COOKIE = '__Host-rf_session'
CSRF_COOKIE = '__Host-rf_csrf'
PREAUTH_COOKIE = '__Host-rf_preauth'


_RESPONSE_HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                                "frame-ancestors 'none'; form-action 'self'"),
}


def error_response(code, status=None, correlation_id=None, not_committed=False):
    from uuid import uuid4
    from starlette.responses import JSONResponse
    code = getattr(code, 'value', code)
    statuses = {'UNAUTHENTICATED': 401, 'FORBIDDEN': 403, 'INVALID_INPUT': 422,
                'RATE_LIMITED': 429,
                'VERSION_CONFLICT': 409, 'RESOURCE_HELD': 409, 'STALE_FENCE': 409,
                'IDEMPOTENCY_CONFLICT': 409, 'APPROVAL_INVALID': 409,
                'RECONCILIATION_REQUIRED': 409, 'LEGACY_EXECUTION_BLOCKED': 503,
                'DEPENDENCY_UNAVAILABLE': 503, 'COMMIT_UNKNOWN': 503,
                'SECRET_UNAVAILABLE': 503, 'GRANT_UNAVAILABLE': 503}
    if code not in statuses:
        code = 'DEPENDENCY_UNAVAILABLE'
    body = {'code': code, 'correlation_id': correlation_id or str(uuid4())}
    if not_committed is True and code == 'VERSION_CONFLICT':
        body['not_committed'] = True
    return JSONResponse(body, status_code=status or statuses[code], headers=_RESPONSE_HEADERS)


# The only writes whose 409 may carry the not-committed proof (spec §5.3).
_UUID_PATH = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_NOT_COMMITTED_ROUTES = (
    ('POST', '/api/onboarding/batches'),
    ('POST', '/api/onboarding/tasks/' + _UUID_PATH + '/(?:pause|cancel|recheck)'),
    ('PATCH', '/api/onboarding/mailboxes/' + _UUID_PATH),
    ('PUT', '/api/onboarding/config'),
    ('POST', '/api/onboarding/mailboxes/import'),
)


def _proves_not_committed(request, exc):
    import re
    from onboarding.errors import ErrorCode, ServiceError
    return (type(exc) is ServiceError and exc.code is ErrorCode.VERSION_CONFLICT
            and exc.not_committed is True
            and any(request.method == method and re.fullmatch(pattern, request.url.path)
                    for method, pattern in _NOT_COMMITTED_ROUTES))


async def read_json(request, max_bytes=8192):
    import json
    from onboarding.errors import ErrorCode, ServiceError
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError
    try:
        length = request.headers.get('content-length')
        if length is not None and (not length.isdecimal() or int(length) > max_bytes):
            raise ValueError
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError
        value = json.loads(body.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=invalid_constant)
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


_POOL_FIXED = {
    **{('GET', path): 'onboarding:read' for path in (
        '/onboarding/pools', '/static/onboarding-pools.js',
        '/api/onboarding/mailboxes', '/api/onboarding/tasks')},
    ('POST', '/api/onboarding/mailboxes/import-preview'): 'mailboxes:manage',
    ('POST', '/api/onboarding/mailboxes/import'): 'mailboxes:manage',
    ('GET', '/api/onboarding/config'): 'config:manage',
    ('PUT', '/api/onboarding/config'): 'config:manage',
    ('POST', '/api/onboarding/preflight'): 'tasks:manage',
    ('POST', '/api/onboarding/batches'): 'tasks:manage',
}


def _pool_permission(method, path):
    import re
    if (method, path) in _POOL_FIXED:
        return _POOL_FIXED[method, path]
    if method == 'PATCH' and re.fullmatch(r'/api/onboarding/mailboxes/[^/]+', path):
        return 'mailboxes:manage'
    return None


def _policy(method, path):
    import re
    if (method, path) in _PUBLIC or (method, path) == ('POST', '/api/auth/login'):
        return 'public', None
    if (method, path) in _FIXED:
        return 'authenticated', _FIXED[method, path]
    if method == 'GET' and re.fullmatch(r'/api/onboarding/tasks/[^/]+', path):
        return 'authenticated', 'onboarding:read'
    if method == 'POST' and re.fullmatch(r'/api/onboarding/tasks/[^/]+/(pause|cancel|recheck)', path):
        return 'authenticated', 'tasks:manage'
    for verb, pattern in LEGACY_ROUTES:
        escaped = re.escape(pattern)
        escaped = re.sub(r'\\\{[^}]+:path\\\}', '.*', escaped)
        escaped = re.sub(r'\\\{[^}]+\\\}', '[^/]+', escaped)
        if verb == method and re.fullmatch(escaped, path):
            return 'legacy', 'legacy:admin'
    return 'deny', None


def _transport(request, config):
    from onboarding import security
    from onboarding.errors import ErrorCode, ServiceError
    from urllib.parse import urlsplit
    # ASGI scheme and Host are authoritative. Forwarded headers are ignored.
    for name in ('host', 'origin', 'content-type', 'content-length', 'x-csrf-token', 'sec-fetch-site', 'referer'):
        if len(request.headers.getlist(name)) > 1:
            raise ServiceError(ErrorCode.FORBIDDEN)
    if request.scope.get('scheme') != 'https':
        raise ServiceError(ErrorCode.FORBIDDEN)
    origin = request.headers.get('origin')
    security.check_request(config.expected_origin if origin is None else origin, request.headers.get('host'),
                           config.expected_origin)
    if request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none'):
        raise ServiceError(ErrorCode.FORBIDDEN)
    referer = request.headers.get('referer')
    if referer:
        try:
            parsed = urlsplit(referer)
        except ValueError:
            raise ServiceError(ErrorCode.FORBIDDEN) from None
        if parsed.scheme + '://' + parsed.netloc != config.expected_origin:
            raise ServiceError(ErrorCode.FORBIDDEN)
    if request.method not in ('GET', 'HEAD'):
        security.check_request(origin, request.headers.get('host'), config.expected_origin,
                               request.headers.get('content-type'))
    # Duplicate cookie names are ambiguous across parsers; never select a winner.
    names = []
    for header in request.headers.getlist('cookie'):
        for item in header.split(';'):
            name = item.partition('=')[0].strip()
            if name in (SESSION_COOKIE, CSRF_COOKIE, PREAUTH_COOKIE):
                if name in names:
                    raise ServiceError(ErrorCode.FORBIDDEN)
                names.append(name)


def _authenticate_request(request, config, permission):
    from onboarding import security, storage
    with storage.unit_of_work(config.settings) as conn:
        actor = security.require(conn, request.cookies.get(SESSION_COOKIE), permission)
        if request.method not in ('GET', 'HEAD'):
            security.check_csrf(conn, actor, request.headers.get('x-csrf-token'),
                                origin=request.headers.get('origin'), expected_origin=config.expected_origin)
    # The transaction is committed/closed before any task-first business lock.
    return actor


class ProtectionMiddleware:
    """Pure ASGI; denial covers mounts, methods, websocket and future routes."""
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        from uuid import uuid4
        from starlette.requests import Request
        from starlette.concurrency import run_in_threadpool
        from onboarding.errors import ServiceError
        request = Request(scope, receive)
        correlation = str(uuid4())
        scope.setdefault('state', {})['correlation_id'] = correlation
        response = None
        try:
            if not self.config.ready:
                response = error_response('DEPENDENCY_UNAVAILABLE', correlation_id=correlation)
            else:
                _transport(request, self.config)
                policy, permission = _policy(request.method, request.url.path)
                pool_permission = _pool_permission(request.method, request.url.path)
                if pool_permission is not None and self.config.pool_mode == 'synthetic':
                    policy, permission = 'pool', pool_permission
                if '%' in scope.get('raw_path', b'').decode('ascii', errors='replace') or '\\' in request.url.path:
                    policy = 'deny'
                if request.url.path.startswith('/api/auth/') and request.url.query:
                    from onboarding.errors import ErrorCode
                    raise ServiceError(ErrorCode.INVALID_INPUT)
                if policy == 'public' and request.url.path in ('/api/auth/bootstrap', '/api/auth/login'):
                    operation = request.url.path.rsplit('/', 1)[1]
                    allowed = await run_in_threadpool(_http_rate_limit, self.config.settings,
                                                      request.client.host if request.client else None,
                                                      operation, correlation)
                    if not allowed:
                        response = error_response('RATE_LIMITED', correlation_id=correlation)
                if policy == 'deny':
                    response = error_response('FORBIDDEN', correlation_id=correlation)
                elif policy == 'pool' and not self.config.pool_ready:
                    response = error_response('DEPENDENCY_UNAVAILABLE', correlation_id=correlation)
                elif policy != 'public':
                    actor = await run_in_threadpool(_authenticate_request, request, self.config, permission)
                    scope['state']['onboarding_actor'] = actor
                    if policy == 'legacy':
                        response = error_response('LEGACY_EXECUTION_BLOCKED', correlation_id=correlation)
        except ServiceError as exc:
            if (exc.code.value == 'UNAUTHENTICATED' and request.method == 'GET'
                    and request.url.path in ('/', '/onboarding', '/onboarding/pools')
                    and 'text/html' in request.headers.get('accept', '')):
                from starlette.responses import RedirectResponse
                response = RedirectResponse('/login?reason=expired', status_code=303,
                                            headers={'Cache-Control': 'no-store'})
            else:
                response = error_response(exc.code, correlation_id=correlation)
        except Exception:
            response = error_response('DEPENDENCY_UNAVAILABLE', correlation_id=correlation)
        if response is not None:
            response.headers.update(_RESPONSE_HEADERS)
            await response(scope, receive, send)
            return
        async def secured_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message)
                replaced = {name.lower().encode() for name in _RESPONSE_HEADERS}
                replaced.update((b'access-control-allow-origin', b'access-control-allow-credentials'))
                message['headers'] = [(k,v) for k,v in message.get('headers', []) if k.lower() not in replaced]
                message['headers'] += [(name.lower().encode(),value.encode()) for name,value in _RESPONSE_HEADERS.items()]
            await send(message)
        started = False
        async def tracked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await secured_send(message)
        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            if started:
                raise
            await error_response('DEPENDENCY_UNAVAILABLE', correlation_id=correlation)(scope, receive, send)


def _pool_context(settings, keyring, mac):
    """BOOT-only dependency verification, including schema-2 checksum."""
    from onboarding import storage
    from onboarding.keyring import Keyring
    from onboarding.request_mac import RequestMac
    from onboarding.pool_vault import SyntheticPoolPolicy, PoolVault
    if type(keyring) is not Keyring or type(mac) is not RequestMac or keyring.directory != mac.directory:
        raise ValueError
    keyring.__post_init__()
    mac._key()
    policy = SyntheticPoolPolicy.from_settings(settings)
    vault = PoolVault(keyring, policy)
    vault._keys()
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
    return policy, vault


def install(app, *, mode, expected_origin=None, settings_path=None, settings=None,
            pool_mode='off', pool_keyring=None, pool_mac=None):
    """Off mode neither imports the database stack nor changes app routes."""
    if mode == 'off' and pool_mode in ('off', 'synthetic'):
        return WebConfig(mode, expected_origin)
    ready = False
    try:
        from onboarding import security
        from onboarding.settings import load_settings, validate_settings
        security.check_request(expected_origin, security._origin(expected_origin, expected_origin), expected_origin)
        if settings is not None and settings_path is not None:
            raise ValueError
        if settings is None:
            settings = load_settings(settings_path)
        validate_settings(settings)
        ready = mode == 'protected' and pool_mode in ('off', 'synthetic')
    except Exception:
        settings = None
    pool_ready, policy, vault = False, None, None
    if ready and pool_mode == 'synthetic':
        try:
            policy, vault = _pool_context(settings, pool_keyring, pool_mac)
            pool_ready = True
        except Exception:
            policy, vault = None, None
    config = WebConfig(mode, expected_origin, settings, ready, pool_mode, pool_ready,
                       pool_keyring if pool_ready else None, pool_mac if pool_ready else None,
                       policy, vault)
    app.state.onboarding_config = config
    app.add_middleware(ProtectionMiddleware, config=config)
    if ready:
        _install_auth_routes(app, config)
    return config


def _cookie(response, name, value, max_age):
    response.set_cookie(name, value, max_age=max_age, path='/', secure=True,
                        httponly=True, samesite='strict')


def _session_view(request, config):
    from onboarding import security, storage
    with storage.unit_of_work(config.settings) as conn:
        actor = security.revalidate(conn, request.state.onboarding_actor, None)
        csrf = request.cookies.get(CSRF_COOKIE)
        security.check_csrf(conn, actor, csrf, origin=config.expected_origin,
                            expected_origin=config.expected_origin)
        username = conn.execute('SELECT username_norm FROM operators WHERE id=%s',
                                (actor.operator_id,)).fetchone()[0]
        actor = security.revalidate(conn, actor, None)
    return {'display_name': username, 'permissions': sorted(actor.permissions), 'csrf_token': csrf}


def _install_auth_routes(app, config):
    from fastapi import APIRouter, Request
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from onboarding import security
    from onboarding.errors import ErrorCode, ServiceError
    router = APIRouter()

    @router.get('/healthz')
    async def liveness():
        return {'alive': True}

    @router.get('/api/auth/bootstrap')
    async def bootstrap(request: Request):
        result = await run_in_threadpool(security.issue_login_bootstrap, config.settings,
                                        origin=config.expected_origin, expected_origin=config.expected_origin)
        response = JSONResponse({'csrf_token': result.csrf_token})
        _cookie(response, PREAUTH_COOKIE, result.preauth_token, 600)
        return response

    @router.post('/api/auth/login')
    async def login(request: Request):
        body = await read_json(request)
        if (set(body) != {'username', 'password'} or type(body['username']) is not str
                or type(body['password']) is not str):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        result = await run_in_threadpool(
            security.login, config.settings, body['username'], body['password'],
            request.client.host if request.client else None,
            preauth_token=request.cookies.get(PREAUTH_COOKIE),
            csrf_token=request.headers.get('x-csrf-token'),
            origin=request.headers.get('origin'), expected_origin=config.expected_origin,
            previous_token=request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({'authenticated': True})
        _cookie(response, SESSION_COOKIE, result.session_token, 28800)
        _cookie(response, CSRF_COOKIE, result.csrf_token, 28800)
        _cookie(response, PREAUTH_COOKIE, '', 0)
        return response

    @router.get('/api/auth/session')
    async def session(request: Request):
        return JSONResponse(await run_in_threadpool(_session_view, request, config))

    @router.post('/api/auth/logout')
    async def logout(request: Request):
        if await read_json(request):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        await run_in_threadpool(security.logout, config.settings,
                                request.cookies.get(SESSION_COOKIE),
                                csrf_token=request.headers.get('x-csrf-token'),
                                origin=request.headers.get('origin'), expected_origin=config.expected_origin)
        response = JSONResponse({'authenticated': False})
        for name in (SESSION_COOKIE, CSRF_COOKIE, PREAUTH_COOKIE):
            _cookie(response, name, '', 0)
        return response

    async def service_error(request, exc):
        return error_response(exc.code, correlation_id=getattr(request.state, 'correlation_id', None),
                              not_committed=_proves_not_committed(request, exc))

    async def validation_error(request, exc):
        return error_response('INVALID_INPUT', correlation_id=getattr(request.state, 'correlation_id', None))

    app.add_exception_handler(ServiceError, service_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, validation_error)
    app.include_router(router)


def _http_rate_limit(settings, source, operation, correlation_id):
    """Bounded HTTP source quota, independent of the B2 account failure quota."""
    from hashlib import sha256
    from datetime import timedelta
    from onboarding import security, storage, audit
    from onboarding.errors import ErrorCode, ServiceError
    if operation not in ('bootstrap', 'login'):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    source = security._source(source)
    shard = int.from_bytes(sha256(source.encode('ascii')).digest()[:8], 'big') % 4096
    key = 'http:' + operation + ':' + str(shard)
    limit = 30 if operation == 'bootstrap' else 10
    with storage.unit_of_work(settings) as conn:
        conn.execute('INSERT INTO auth_throttles(bucket_key,window_start,failures) '
                     'VALUES(%s,clock_timestamp(),0) ON CONFLICT(bucket_key) DO NOTHING', (key,))
        start, failures, blocked = conn.execute(
            'SELECT window_start,failures,blocked_until FROM auth_throttles WHERE bucket_key=%s FOR UPDATE',
            (key,)).fetchone()
        now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
        if start <= now - timedelta(seconds=60):
            start, failures, blocked = now, 0, None
            conn.execute('UPDATE auth_throttles SET window_start=%s,failures=0,blocked_until=NULL,'
                         'version=version+1,updated_at=clock_timestamp() WHERE bucket_key=%s', (now, key))
        allowed = failures < limit and (blocked is None or blocked <= now)
        if allowed:
            conn.execute('UPDATE auth_throttles SET failures=failures+1,version=version+1,'
                         'updated_at=clock_timestamp() WHERE bucket_key=%s', (key,))
        elif blocked is None:
            conn.execute('UPDATE auth_throttles SET blocked_until=%s,version=version+1,'
                         'updated_at=clock_timestamp() WHERE bucket_key=%s', (start + timedelta(seconds=60), key))
            action = 'auth.bootstrap' if operation == 'bootstrap' else 'auth.login_failed'
            audit.append(conn, None, None, action, key, 'RATE_LIMITED', correlation_id)
    # No exception inside UoW: quota and first-rejection audit commit durably.
    return allowed
