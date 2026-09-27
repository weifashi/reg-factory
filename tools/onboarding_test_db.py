"""Manage ONLY the dedicated local synthetic onboarding PostgreSQL 16 instance.

No DSNs, arbitrary data directories, database deletion, or credential output.
A partial prepare is deliberately not automatically repaired or overwritten.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager

BASE = Path('/workspace/reg-factory/.local/onboarding-p1b')
BIN = Path('/workspace/gcloud/.local/pgdist/usr/lib/postgresql/16/bin')
DBNAME = 'rf_onboarding_test'
APP = 'rf_onboarding_app'
MIGRATOR = 'rf_onboarding_migrator'
PORT = 55433
PREFIX = 'rf-onboarding-p1b-v1:'
MAX_PRIVATE_JSON_BYTES = 16 * 1024
CONFIG_KEYS = {'host', 'port', 'dbname', 'user', 'password', 'instance_marker', 'schema'}


class Refusal(RuntimeError):
    """Safe fixed diagnostic, never carrying a password/DSN/database exception."""


def validate_base(base):
    path = Path(base)
    if str(path) != str(BASE) or '..' in path.parts:
        raise Refusal('target directory is not the dedicated instance')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Refusal('symlink in instance path')
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Refusal('instance directory must be owned by this user with mode 0700')
    return path


def write_private_json(path, value):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Refusal('private file already exists or cannot be safely created') from None
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal('duplicate private JSON key')
        result[key] = value
    return result


def read_private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise Refusal('private file must be regular, single-linked, owned, mode 0600')
            if info.st_size > MAX_PRIVATE_JSON_BYTES:
                raise Refusal('private JSON exceeds 16 KiB limit')
            content = stream.read(MAX_PRIVATE_JSON_BYTES + 1)
            if len(content) > MAX_PRIVATE_JSON_BYTES:
                raise Refusal('private JSON exceeds 16 KiB limit')
            result = json.loads(content, object_pairs_hook=_unique_json_pairs)
    except (OSError, ValueError):
        raise Refusal('private file missing, unsafe, or invalid') from None
    if not isinstance(result, dict):
        raise Refusal('private file must contain a JSON object')
    return result


def valid_marker(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return False
    try:
        return str(uuid.UUID(value[len(PREFIX):])) == value[len(PREFIX):]
    except ValueError:
        return False


def validate_config(config, role, marker):
    expected = dict(host=str(BASE / 'socket'), port=PORT, dbname=DBNAME,
                    user=role, instance_marker=marker, schema='rf_onboarding')
    if set(config) != CONFIG_KEYS or role not in (APP, MIGRATOR) or not valid_marker(marker):
        raise Refusal('configuration fields or marker invalid')
    if any(config.get(key) != value for key, value in expected.items()):
        raise Refusal('configuration target rejected')
    if type(config['port']) is not int or not isinstance(config['password'], str) or not config['password']:
        raise Refusal('configuration credential or port invalid')


def validate_manifest(manifest, schema, marker):
    token = manifest.get('schema_token')
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise Refusal('manifest schema token invalid')
    if (not valid_marker(marker) or manifest.get('instance_marker') != marker
            or manifest.get('owner') != MIGRATOR or manifest.get('schema') != schema
            or schema != 'rf_p1b_test_' + token):
        raise Refusal('manifest target, marker, or owner rejected')
    return token


def _safe_child(path, mode=None):
    path = Path(path)
    try:
        path.relative_to(BASE)
    except ValueError:
        raise Refusal('path is outside the dedicated instance') from None
    if '..' in path.parts:
        raise Refusal('path traversal refused')
    for part in (path, *path.parents):
        if part == BASE:
            break
        if part.is_symlink():
            raise Refusal('symlink in dedicated instance')
        if part.exists():
            info = part.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise Refusal('instance child ownership or permissions unsafe')
    if mode is not None and (not path.exists() or stat.S_IMODE(path.stat().st_mode) != mode):
        raise Refusal('instance child permissions incorrect')
    return path


@contextmanager
def _management_lock():
    validate_base(BASE)
    BASE.parent.mkdir(parents=True, exist_ok=True)
    BASE.mkdir(mode=0o700, exist_ok=True)
    lock = BASE / '.management.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refusal('management lock unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _run(binary, args, *, allowed=(0,)):
    # All arguments are generated here and contain no secrets; do not inherit PG*.
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env['LC_ALL'] = 'C'
    shared_lib = str(BIN.parents[2] / 'x86_64-linux-gnu')
    runtime = BASE / 'runtime-lib'
    # Optional per-instance dependency, never mutate the shared PG distribution.
    if runtime.exists() or runtime.is_symlink():
        validate_base(BASE)
        _safe_child(runtime, 0o700)
        if not runtime.is_dir():
            raise Refusal('private runtime library path is not a directory')
        env['LD_LIBRARY_PATH'] = str(runtime) + ':' + shared_lib
    else:
        env['LD_LIBRARY_PATH'] = shared_lib
    result = subprocess.run([str(BIN / binary), *args], env=env, capture_output=True, text=True, timeout=60)
    if result.returncode not in allowed:
        raise Refusal('PostgreSQL management command failed: ' + binary)
    return result.returncode


def _running():
    return _run('pg_ctl', ['-D', str(BASE / 'data'), 'status'], allowed=(0, 3)) == 0


def _state():
    validate_base(BASE)
    state = read_private_json(BASE / 'instance.json')
    if not valid_marker(state.get('instance_marker')) or state.get('bootstrap_user') != getpass.getuser():
        raise Refusal('instance state marker or bootstrap user invalid')
    if state.get('state') != 'ready':
        raise Refusal('partial preparation; inspect local state without regenerating credentials')
    for role, filename in ((APP, 'app.json'), (MIGRATOR, 'migrator.json')):
        config = read_private_json(BASE / filename)
        validate_config(config, role, state['instance_marker'])
    _safe_child(BASE / 'data', 0o700)
    _safe_child(BASE / 'socket', 0o700)
    if (BASE / 'data' / 'PG_VERSION').read_text().strip() != '16':
        raise Refusal('instance is not PostgreSQL 16')
    return state


def _connect(config=None, *, dbname=DBNAME):
    if any(key.startswith('PG') and value for key, value in os.environ.items()):
        raise Refusal('ambient PostgreSQL connection overrides are forbidden')
    import psycopg
    if config is None:
        return psycopg.connect(host=str(BASE / 'socket'), port=PORT, dbname=dbname,
                               user=getpass.getuser(), password='', passfile=str(BASE / 'disabled-password-file'),
                               hostaddr='', options='', connect_timeout=5, autocommit=True)
    return psycopg.connect(**{key: config[key] for key in ('host', 'port', 'dbname', 'user', 'password')},
                           passfile=str(BASE / 'disabled-password-file'), hostaddr='', options='',
                           connect_timeout=5, autocommit=True)


def _verify_server(conn, marker, *, bootstrap=False):
    row = conn.execute("SELECT current_database(), shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()
    if row != (DBNAME, marker):
        raise Refusal('database identity mismatch')
    if conn.execute('SHOW listen_addresses').fetchone()[0] != '':
        raise Refusal('TCP listening is forbidden')
    if conn.execute('SHOW port').fetchone()[0] != str(PORT):
        raise Refusal('server port mismatch')
    if bootstrap and conn.execute('SHOW unix_socket_directories').fetchone()[0] != str(BASE / 'socket'):
        raise Refusal('server socket mismatch')
    if bootstrap and conn.execute('SHOW data_directory').fetchone()[0] != str(BASE / 'data'):
        raise Refusal('server data directory mismatch')


def _start():
    # Explicit server options prevent changed listen/socket settings opening TCP.
    options = "-c listen_addresses='' -c unix_socket_directories=" + str(BASE / 'socket') + ' -p ' + str(PORT)
    _run('pg_ctl', ['-D', str(BASE / 'data'), '-l', str(BASE / 'postgres.log'), '-o', options, '-w', 'start'])


def prepare():
    with _management_lock():
        if (BASE / 'instance.json').exists():
            state = _state()
            if not _running():
                _start()
            with _connect() as conn:
                _verify_server(conn, state['instance_marker'], bootstrap=True)
            return status()
        if set(path.name for path in BASE.iterdir()) != {'.management.lock'}:
            raise Refusal('unidentified existing files; refusing initialization')
        marker = PREFIX + str(uuid.uuid4())
        state = dict(instance_marker=marker, bootstrap_user=getpass.getuser(), state='preparing')
        write_private_json(BASE / 'instance.json', state)
        for role, filename in ((APP, 'app.json'), (MIGRATOR, 'migrator.json')):
            write_private_json(BASE / filename, dict(host=str(BASE / 'socket'), port=PORT,
                               dbname=DBNAME, user=role, password=secrets.token_urlsafe(36),
                               instance_marker=marker, schema='rf_onboarding'))
        return _initialize_fresh_cluster(state)


def _initialize_fresh_cluster(state):
    # Internal one-time initialization. prepare never calls this for partial state.
    (BASE / 'socket').mkdir(mode=0o700, exist_ok=True)
    _run('initdb', ['-D', str(BASE / 'data'), '--auth-local=peer', '--auth-host=reject', '--encoding=UTF8', '--locale=C', '--no-instructions'])
    hba = (f'local all {getpass.getuser()} peer\n'
           f'local {DBNAME} {APP},{MIGRATOR} scram-sha-256\n'
           'local all all reject\nhost all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n')
    (BASE / 'data' / 'pg_hba.conf').write_text(hba)
    with (BASE / 'data' / 'postgresql.conf').open('a') as stream:
        stream.write("\nlisten_addresses = ''\nport = 55433\nunix_socket_permissions = 0700\npassword_encryption = 'scram-sha-256'\nlog_statement = 'none'\nlog_min_error_statement = 'panic'\n")
    _start()
    from psycopg import sql
    with _connect(dbname='postgres') as conn:
        conn.execute("SET log_min_error_statement = 'panic'")
        for role, filename in ((APP, 'app.json'), (MIGRATOR, 'migrator.json')):
            password = read_private_json(BASE / filename)['password']
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(DBNAME)))
        conn.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(DBNAME), sql.Literal(state['instance_marker'])))
        conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(DBNAME)))
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}, {}').format(sql.Identifier(DBNAME), sql.Identifier(APP), sql.Identifier(MIGRATOR)))
        conn.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(DBNAME), sql.Identifier(MIGRATOR)))
    with _connect() as conn:
        conn.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
        conn.execute(sql.SQL('CREATE SCHEMA rf_onboarding AUTHORIZATION {}').format(sql.Identifier(MIGRATOR)))
        conn.execute('REVOKE ALL ON SCHEMA rf_onboarding FROM PUBLIC')
        _verify_server(conn, state['instance_marker'], bootstrap=True)
    # Only this state file is atomically replaced; credential files never are.
    state['state'] = 'ready'
    write_private_json(BASE / 'instance.ready.json', state)
    os.replace(BASE / 'instance.ready.json', BASE / 'instance.json')
    return status()

def status():
    if not BASE.exists():
        return {'state': 'absent', 'base': str(BASE)}
    state = _state()
    result = {'state': 'stopped', 'base': str(BASE), 'instance_marker': state['instance_marker'],
              'app_config': str(BASE / 'app.json'), 'migrator_config': str(BASE / 'migrator.json')}
    if not _running():
        return result
    with _connect() as conn:
        _verify_server(conn, state['instance_marker'], bootstrap=True)
        rows = conn.execute('SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)', ([APP, MIGRATOR],)).fetchall()
        if len(rows) != 2 or any(any(row[1:]) for row in rows):
            raise Refusal('database roles have unexpected privileges')
    for role, filename in ((APP, 'app.json'), (MIGRATOR, 'migrator.json')):
        with _connect(read_private_json(BASE / filename)) as conn:
            _verify_server(conn, state['instance_marker'])
            if conn.execute('SELECT current_user').fetchone()[0] != role:
                raise Refusal('login role mismatch')
            privileges = conn.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE'), has_database_privilege(current_user,current_database(),'TEMP'), has_schema_privilege(current_user,'public','CREATE')").fetchone()
            if privileges != (role == MIGRATOR, False, False):
                raise Refusal('database/schema privilege mismatch')
    result.update(state='running', tcp_listening=False, roles_verified=True)
    return result


def stop():
    with _management_lock():
        state = _state()
        if _running():
            with _connect() as conn:
                _verify_server(conn, state['instance_marker'], bootstrap=True)
            _run('pg_ctl', ['-D', str(BASE / 'data'), '-m', 'fast', '-w', 'stop'])
        return {'state': 'stopped', 'base': str(BASE)}


def _guard_cascade_scope(conn, schema):
    """Conservatively reject a cascade whose dependency closure escapes schema.

    pg_depend is traversed both towards dependents and towards internal/automatic
    owners: dropping a view's rewrite rule also drops its owning view. Unscoped
    extension/cast/etc. objects are refused, not assumed harmless. Only automatic
    TOAST storage belonging to a target table is treated as part of that table.
    """
    rows = conn.execute("""
        WITH RECURSIVE edges AS (
            SELECT refclassid AS srcclass, refobjid AS srcobj,
                   classid AS dstclass, objid AS dstobj FROM pg_depend
            UNION
            SELECT classid, objid, refclassid, refobjid FROM pg_depend
            WHERE deptype IN ('i', 'a', 'e')
        ), affected(classid, objid) AS (
            SELECT 'pg_namespace'::regclass::oid, oid FROM pg_namespace WHERE nspname=%s
            UNION
            SELECT e.dstclass, e.dstobj FROM affected a JOIN edges e
            ON e.srcclass=a.classid AND e.srcobj=a.objid
        )
        SELECT CASE
            WHEN a.classid='pg_namespace'::regclass THEN
                (SELECT nspname FROM pg_namespace WHERE oid=a.objid)
            WHEN a.classid='pg_constraint'::regclass THEN
                (SELECT n.nspname FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE c.oid=a.objid)
            WHEN a.classid='pg_rewrite'::regclass THEN
                (SELECT n.nspname FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE r.oid=a.objid)
            WHEN a.classid='pg_trigger'::regclass THEN
                (SELECT n.nspname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE t.oid=a.objid)
            WHEN a.classid='pg_attrdef'::regclass THEN
                (SELECT n.nspname FROM pg_attrdef d JOIN pg_class c ON c.oid=d.adrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE d.oid=a.objid)
            WHEN a.classid='pg_class'::regclass AND ident.schema='pg_toast' THEN
                (SELECT n.nspname FROM pg_class owner JOIN pg_namespace n ON n.oid=owner.relnamespace
                 WHERE owner.reltoastrelid=a.objid OR owner.reltoastrelid=(SELECT indrelid FROM pg_index WHERE indexrelid=a.objid))
            ELSE ident.schema END AS logical_schema
        FROM affected a CROSS JOIN LATERAL pg_identify_object(a.classid,a.objid,0) ident
    """, (schema,)).fetchall()
    if not rows or any(row[0] != schema for row in rows):
        raise Refusal('external or unscoped cascade dependency; schema and manifest retained')


def cleanup_schema(schema, manifest_path):
    state = _state()
    path = _safe_child(manifest_path, 0o600)
    manifest = read_private_json(path)
    token = validate_manifest(manifest, schema, state['instance_marker'])
    from psycopg import sql
    with _connect(read_private_json(BASE / 'migrator.json')) as conn:
        _verify_server(conn, state['instance_marker'])
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ('rf_onboarding.migration.' + schema,))
            owner = conn.execute('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s', (schema,)).fetchone()
            if owner != (MIGRATOR,):
                raise Refusal('schema absent or owner mismatch')
            conn.execute(sql.SQL('LOCK TABLE {}._test_marker IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(schema)))
            rows = conn.execute(sql.SQL('SELECT instance_marker, schema_token FROM {}._test_marker').format(sql.Identifier(schema))).fetchall()
            if rows != [(state['instance_marker'], token)]:
                raise Refusal('schema marker mismatch')
            # Lock all existing test tables before inspecting dependencies. This
            # blocks concurrent view/FK references while the check/drop proceeds.
            tables = conn.execute('SELECT c.relname FROM pg_class c JOIN pg_namespace n '
                                  'ON n.oid=c.relnamespace WHERE n.nspname=%s '
                                  "AND c.relkind IN ('r','p','m') ORDER BY c.relname", (schema,)).fetchall()
            for (table,) in tables:
                conn.execute(sql.SQL('LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE').format(
                    sql.Identifier(schema), sql.Identifier(table)))
            _guard_cascade_scope(conn, schema)
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    return {'state': 'cleaned', 'schema': schema}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'status', 'stop', 'cleanup-schema'))
    parser.add_argument('--base', default=str(BASE))
    parser.add_argument('--schema')
    parser.add_argument('--manifest')
    args = parser.parse_args(argv)
    try:
        validate_base(args.base)
        if args.command == 'cleanup-schema':
            if not args.schema or not args.manifest:
                raise Refusal('cleanup requires exact schema and manifest')
            result = cleanup_schema(args.schema, args.manifest)
        else:
            if args.schema or args.manifest:
                raise Refusal('schema/manifest only allowed with cleanup-schema')
            result = {'prepare': prepare, 'status': status, 'stop': stop}[args.command]()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Refusal as exc:
        print('refused: ' + str(exc), file=sys.stderr)
        return 2
    except Exception:
        # psycopg exceptions can echo SQL or connection secrets. Never render them.
        print('refused: management operation failed; partial state retained for diagnosis', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
