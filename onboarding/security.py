"""Local operator authentication. No Google/provider login and no HTTP routes."""
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets

from .errors import ErrorCode, ServiceError

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 1 << 17, 8, 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024


def normalize_username(username):
    if type(username) is not str or len(username) > 256:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    result = username.strip().casefold()
    if not re.fullmatch(r'[a-z0-9][a-z0-9._@+\-]{0,127}', result):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    return result


def _password_bytes(password):
    if type(password) is not str:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    try:
        value = password.encode('utf-8')
    except UnicodeError:
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    if not 12 <= len(value) <= 1024 or '\x00' in password:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    return value


def _derive(value, salt):
    return hashlib.scrypt(value, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                          p=_SCRYPT_P, dklen=32, maxmem=_SCRYPT_MAXMEM)


def hash_password(password):
    """One fixed production work factor; caller cannot supply scrypt parameters."""
    value = _password_bytes(password)
    salt = secrets.token_bytes(16)
    result = _derive(value, salt)
    return 'scrypt$131072$8$1$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(result).decode()


def verify_password(password, encoded):
    """Malformed/missing records still perform the same full-cost derivation."""
    valid = True
    try:
        value = _password_bytes(password)
    except ServiceError:
        value, valid = b'invalid-login-input', False
    salt, expected = bytes(16), bytes(32)
    try:
        if type(encoded) is not str or len(encoded) > 256:
            raise ValueError
        name, n, r, p, salt_text, digest_text = encoded.split('$')
        if (name, n, r, p) != ('scrypt', '131072', '8', '1'):
            raise ValueError
        parsed_salt = base64.b64decode(salt_text, validate=True)
        parsed_digest = base64.b64decode(digest_text, validate=True)
        if (len(parsed_salt) != 16 or len(parsed_digest) != 32
                or base64.b64encode(parsed_salt).decode() != salt_text
                or base64.b64encode(parsed_digest).decode() != digest_text):
            raise ValueError
        salt, expected = parsed_salt, parsed_digest
    except (ValueError, TypeError):
        valid = False
    actual = _derive(value, salt)
    return hmac.compare_digest(actual, expected) and valid


@dataclass(frozen=True)
class Actor:
    """Server-authenticated snapshot; dangerous services must revalidate it."""
    operator_id: str
    permissions: frozenset
    session_id: str
    auth_epoch: int


@dataclass(frozen=True)
class LoginResult:
    actor: Actor
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class LoginBootstrap:
    preauth_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


def _origin(origin, expected_origin):
    from urllib.parse import urlsplit
    try:
        if (type(origin) is not str or type(expected_origin) is not str
                or origin != expected_origin or not 1 <= len(expected_origin) <= 2048
                or not expected_origin.isascii()
                or any(ord(c) <= 32 or c == '\\' for c in expected_origin)):
            raise ValueError
        parsed = urlsplit(expected_origin)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.path or parsed.query
                or parsed.fragment or parsed.username is not None or parsed.password is not None
                or expected_origin != 'https://' + parsed.netloc
                or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?|\[[0-9a-f:]+\])(?::[1-9][0-9]{0,4})?',
                                    parsed.netloc)
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        return parsed.netloc
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.FORBIDDEN) from None


def check_request(origin, host, expected_origin, content_type='application/json'):
    """B3 must pass the actual Host/Origin, never an untrusted forwarded header."""
    authority = _origin(origin, expected_origin)
    if (type(host) is not str or host != authority or type(content_type) is not str
            or len(content_type) > 128
            or not re.fullmatch(r'application/json(?:;\s*charset=utf-8)?', content_type.lower())):
        raise ServiceError(ErrorCode.FORBIDDEN)

from uuid import UUID, uuid4
from psycopg.rows import dict_row
from . import audit, storage
from .repository import require_transaction

PERMISSIONS = frozenset({'onboarding:read', 'tasks:manage', 'config:manage', 'cards:manage',
                         'mailboxes:manage',
                         'challenges:manage', 'fees:approve', 'keys:download',
                         'scheduling:enable', 'legacy:admin'})


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(ErrorCode.UNAUTHENTICATED) from None
    return value


def _digest_token(token):
    if type(token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _one(conn, query, params):
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(query, params).fetchone()


def _authenticate(conn, operator_id, session_id, permission, *, snapshot_epoch=None):
    """Always operator then session; no task/approval/resource locks are acquired."""
    require_transaction(conn)
    if permission is not None and (type(permission) is not str or permission not in PERMISSIONS):
        raise ServiceError(ErrorCode.FORBIDDEN)
    operator_id, session_id = _uuid(operator_id), _uuid(session_id)
    operator = _one(conn, 'SELECT id,permissions,disabled,auth_epoch FROM operators '
                          'WHERE id=%s FOR SHARE', (operator_id,))
    session = _one(conn, 'SELECT * FROM operator_sessions WHERE id=%s FOR UPDATE', (session_id,))
    now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
    if (operator is None or session is None or str(session['operator_id']) != operator_id
            or operator['disabled'] or session['revoked_at'] is not None
            or session['auth_epoch'] != operator['auth_epoch']
            or session['expires_at'] <= now or session['idle_expires_at'] <= now
            or (snapshot_epoch is not None and (type(snapshot_epoch) is not int
                                                or snapshot_epoch != operator['auth_epoch']))):
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    if permission is not None and permission not in operator['permissions']:
        raise ServiceError(ErrorCode.FORBIDDEN)
    return Actor(operator_id, frozenset(operator['permissions']), session_id,
                 operator['auth_epoch']), session


def _from_token(conn, session_token, permission):
    require_transaction(conn)
    digest = _digest_token(session_token)
    # Nonlocking locator only: identity/token bindings are never mutable by API.
    row = conn.execute('SELECT operator_id,id FROM operator_sessions WHERE token_hash=%s',
                       (digest,)).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    actor, session = _authenticate(conn, str(row[0]), str(row[1]), permission)
    if not hmac.compare_digest(session['token_hash'], digest):
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    return actor, session


def require(conn, session_token, permission):
    """Authenticate and refresh idle after locks, never extending absolute expiry.

    A None permission is reserved for authentication-only endpoints (logout or
    session metadata); business services must name their concrete permission.
    """
    actor, session = _from_token(conn, session_token, permission)
    conn.execute('UPDATE operator_sessions SET '
                 "idle_expires_at=LEAST(expires_at,clock_timestamp()+interval '30 minutes'),"
                 'version=version+1,updated_at=clock_timestamp() WHERE id=%s', (actor.session_id,))
    audit.append(conn, actor.operator_id, None, 'auth.session_touch', actor.session_id,
                 'OK', str(uuid4()), before_summary={'version': session['version']},
                 after_summary={'version': session['version'] + 1})
    # Audit INSERT may block past an absolute/idle deadline. No stale Actor
    # escapes; failure rolls back both the refresh and its audit atomically.
    return revalidate(conn, actor, permission)


def revalidate(conn, actor, permission):
    """Never trust cached permissions/epoch. Does not refresh idle or mint tokens."""
    require_transaction(conn)
    if type(actor) is not Actor or type(actor.auth_epoch) is not int or actor.auth_epoch < 1:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    return _authenticate(conn, actor.operator_id, actor.session_id, permission,
                         snapshot_epoch=actor.auth_epoch)[0]


def check_csrf(conn, actor, csrf_token, *, origin, expected_origin):
    _origin(origin, expected_origin)
    current = revalidate(conn, actor, None)
    try:
        digest = _digest_token(csrf_token)
    except ServiceError:
        raise ServiceError(ErrorCode.FORBIDDEN) from None
    row = conn.execute('SELECT csrf_hash FROM operator_sessions WHERE id=%s',
                       (current.session_id,)).fetchone()
    if not hmac.compare_digest(row[0], digest):
        raise ServiceError(ErrorCode.FORBIDDEN)


def _permissions(permissions):
    if (type(permissions) not in (set, frozenset, list, tuple)
            or any(type(item) is not str or item not in PERMISSIONS for item in permissions)):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    return sorted(set(permissions))


def create_operator(conn, username, password, permissions):
    """Trusted local CLI/test bootstrap only; never expose as web registration."""
    require_transaction(conn)
    username, permissions = normalize_username(username), _permissions(permissions)
    encoded = hash_password(password)
    operator_id = str(uuid4())
    row = conn.execute('INSERT INTO operators (id,username_norm,password_hash,permissions) '
                       'VALUES (%s,%s,%s,%s) ON CONFLICT(username_norm) DO NOTHING RETURNING id',
                       (operator_id, username, encoded, permissions)).fetchone()
    if row is None:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    audit.append(conn, None, None, 'auth.operator_create', operator_id, 'CREATED', str(uuid4()),
                 after_summary={'version': 1})
    return operator_id


def update_operator(conn, operator_id, *, permissions=None, disabled=None):
    """Trusted local maintenance primitive, NOT a web self-service privilege API."""
    require_transaction(conn)
    operator_id = _uuid(operator_id)
    if permissions is None and disabled is None:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if disabled is not None and type(disabled) is not bool:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    if permissions is not None:
        permissions = _permissions(permissions)
    row = _one(conn, 'SELECT permissions,disabled,version FROM operators WHERE id=%s FOR UPDATE', (operator_id,))
    if row is None:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    conn.execute('UPDATE operators SET permissions=%s,disabled=%s,auth_epoch=auth_epoch+1,'
                 'version=version+1,updated_at=clock_timestamp() WHERE id=%s',
                 (row['permissions'] if permissions is None else permissions,
                  row['disabled'] if disabled is None else disabled, operator_id))
    audit.append(conn, None, None, 'auth.operator_update', operator_id, 'UPDATED', str(uuid4()),
                 before_summary={'version': row['version']}, after_summary={'version': row['version'] + 1})

_PREAUTH_LIMIT = 4096
_LOGIN_BUCKETS = 4096


def issue_login_bootstrap(settings, *, origin, expected_origin):
    """Ten-minute, one-use login CSRF ticket; only token digests reach the DB.

    The bounded pool reuses consumed/expired rows. Active-ticket exhaustion fails
    closed rather than storing unbounded attacker-controlled keys.
    """
    _origin(origin, expected_origin)
    result = LoginBootstrap(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
    key = 'preauth:' + _digest_token(result.preauth_token) + ':' + _digest_token(result.csrf_token)
    with storage.unit_of_work(settings) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(current_schema()),421906)')
        reusable = conn.execute(
            'SELECT bucket_key FROM auth_throttles WHERE bucket_key LIKE %s '
            'AND (failures>0 OR blocked_until<=clock_timestamp()) '
            'ORDER BY window_start,bucket_key LIMIT 1 FOR UPDATE SKIP LOCKED', ('preauth:%',)).fetchone()
        if reusable:
            conn.execute('UPDATE auth_throttles SET bucket_key=%s,window_start=clock_timestamp(),'
                         "failures=0,blocked_until=clock_timestamp()+interval '10 minutes',"
                         'version=version+1,updated_at=clock_timestamp() WHERE bucket_key=%s', (key, reusable[0]))
        else:
            count = conn.execute('SELECT count(*) FROM auth_throttles WHERE bucket_key LIKE %s', ('preauth:%',)).fetchone()[0]
            if count >= _PREAUTH_LIMIT:
                raise ServiceError(ErrorCode.UNAUTHENTICATED)
            conn.execute('INSERT INTO auth_throttles(bucket_key,window_start,failures,blocked_until) '
                         "VALUES(%s,clock_timestamp(),0,clock_timestamp()+interval '10 minutes')", (key,))
        audit.append(conn, None, None, 'auth.bootstrap', str(uuid4()), 'ISSUED', str(uuid4()))
    return result


def _source(source):
    import ipaddress
    try:
        if type(source) is not str or len(source) > 64 or '%' in source:
            raise ValueError
        address = ipaddress.ip_address(source)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        raise ServiceError(ErrorCode.INVALID_INPUT) from None


def _bucket(kind, value):
    # Fixed cardinality (8192 total). Hash collisions only conservatively limit.
    shard = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], 'big') % _LOGIN_BUCKETS
    return 'login:' + kind + ':' + str(shard)


def _lock_buckets(conn, username, source):
    keys = sorted((_bucket('user', username), _bucket('source', source)))
    rows = []
    for key in keys:
        conn.execute('INSERT INTO auth_throttles(bucket_key,window_start,failures) '
                     'VALUES(%s,clock_timestamp(),0) ON CONFLICT(bucket_key) DO NOTHING', (key,))
        rows.append(_one(conn, 'SELECT * FROM auth_throttles WHERE bucket_key=%s FOR UPDATE', (key,)))
    now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
    from datetime import timedelta
    for row in rows:
        if row['window_start'] <= now - timedelta(minutes=15):
            conn.execute('UPDATE auth_throttles SET failures=0,blocked_until=NULL,window_start=clock_timestamp(),'
                         'version=version+1,updated_at=clock_timestamp() WHERE bucket_key=%s', (row['bucket_key'],))
            row['failures'], row['blocked_until'] = 0, None
    return rows, any(row['failures'] >= 5 or row['blocked_until'] is not None
                    and row['blocked_until'] > now for row in rows)


def _consume_preauth(conn, preauth_token, csrf_token):
    try:
        key = 'preauth:' + _digest_token(preauth_token) + ':' + _digest_token(csrf_token)
    except ServiceError:
        return False
    row = _one(conn, 'SELECT failures,blocked_until FROM auth_throttles WHERE bucket_key=%s FOR UPDATE', (key,))
    now = conn.execute('SELECT clock_timestamp()').fetchone()[0]
    if row is None or row['failures'] != 0 or row['blocked_until'] is None or row['blocked_until'] <= now:
        return False
    conn.execute('UPDATE auth_throttles SET failures=1,version=version+1,updated_at=clock_timestamp() '
                 'WHERE bucket_key=%s', (key,))
    return True


def _login_failure(conn, buckets):
    for row in buckets:
        if row['failures'] < 5:
            conn.execute('UPDATE auth_throttles SET failures=failures+1,'
                         "blocked_until=CASE WHEN failures+1>=5 THEN window_start+interval '15 minutes' "
                         'ELSE blocked_until END,version=version+1,updated_at=clock_timestamp() WHERE bucket_key=%s',
                         (row['bucket_key'],))
    audit.append(conn, None, None, 'auth.login_failed', str(uuid4()), 'REJECTED', str(uuid4()))


def _new_session(conn, operator, previous_token):
    operator_id = str(operator['id'])
    if previous_token is not None:
        try:
            previous_hash = _digest_token(previous_token)
        except ServiceError:
            previous_hash = None
        # Never lock or revoke someone else's session, even if its bearer was supplied.
        previous = _one(conn, 'SELECT id,version,revoked_at FROM operator_sessions '
                              'WHERE operator_id=%s AND token_hash=%s FOR UPDATE', (operator_id, previous_hash))
        if previous and previous['revoked_at'] is None:
            conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp(),version=version+1,'
                         'updated_at=clock_timestamp() WHERE id=%s', (previous['id'],))
            audit.append(conn, operator_id, None, 'auth.logout', str(previous['id']), 'ROTATED', str(uuid4()))
    token, csrf, session_id = secrets.token_urlsafe(32), secrets.token_urlsafe(32), str(uuid4())
    conn.execute('INSERT INTO operator_sessions '
                 '(id,operator_id,token_hash,csrf_hash,auth_epoch,expires_at,idle_expires_at) '
                 "VALUES(%s,%s,%s,%s,%s,clock_timestamp()+interval '8 hours',clock_timestamp()+interval '30 minutes')",
                 (session_id, operator_id, _digest_token(token), _digest_token(csrf), operator['auth_epoch']))
    audit.append(conn, operator_id, None, 'auth.login', session_id, 'AUTHENTICATED', str(uuid4()))
    return LoginResult(Actor(operator_id, frozenset(operator['permissions']), session_id, operator['auth_epoch']), token, csrf)


def login(settings, username, password, source, *, preauth_token, csrf_token,
          origin, expected_origin, previous_token=None):
    """Local operator password only. Callers must pass a trusted connection IP.

    Failed authentication commits counters/audit BEFORE raising the uniform 401
    code. A DB or commit failure instead fails closed; no fallback session exists.
    """
    _origin(origin, expected_origin)
    source = _source(source)
    try:
        username = normalize_username(username)
    except ServiceError:
        username = None
    result = None
    with storage.unit_of_work(settings) as conn:
        buckets, blocked = _lock_buckets(conn, username or 'invalid-input', source)
        preauth_ok = _consume_preauth(conn, preauth_token, csrf_token)
        operator, accepted = None, False
        if not blocked and preauth_ok:
            operator = _one(conn, 'SELECT * FROM operators WHERE username_norm=%s FOR SHARE', (username,))
            # Missing, disabled and wrong-password paths all do the full scrypt.
            matched = verify_password(password, operator['password_hash'] if operator else None)
            accepted = operator is not None and matched and not operator['disabled']
        if accepted:
            result = _new_session(conn, operator, previous_token)
        else:
            _login_failure(conn, buckets)
    if result is None:
        raise ServiceError(ErrorCode.UNAUTHENTICATED)
    return result


def logout(settings, session_token, *, csrf_token, origin, expected_origin):
    _origin(origin, expected_origin)
    with storage.unit_of_work(settings) as conn:
        actor, session = _from_token(conn, session_token, None)
        check_csrf(conn, actor, csrf_token, origin=origin, expected_origin=expected_origin)
        conn.execute('UPDATE operator_sessions SET revoked_at=clock_timestamp(),version=version+1,'
                     'updated_at=clock_timestamp() WHERE id=%s', (actor.session_id,))
        audit.append(conn, actor.operator_id, None, 'auth.logout', actor.session_id, 'REVOKED', str(uuid4()),
                     before_summary={'version': session['version']}, after_summary={'version': session['version'] + 1})
