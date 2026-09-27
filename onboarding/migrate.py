"""Explicit versioned migrations; no startup migration or live adapter."""
import hashlib
import re

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from onboarding.errors import ErrorCode, ServiceError
from onboarding import migration_catalog

_SCHEMA = re.compile(r'(?:rf_onboarding|rf_p1b_test_[a-f0-9]{32})\Z')


def migration_text(version=1):
    return migration_catalog.load_migration(version)


def _validate_connection(conn, schema):
    if (not isinstance(schema, str) or not _SCHEMA.fullmatch(schema)
            or not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE):
        raise ServiceError(ErrorCode.INVALID_INPUT)


def _installed_version(rows, override_checksum=None):
    if override_checksum is None:
        return migration_catalog.validate_applied(rows)
    # Preserve the trusted, local 001 fault-injection hook. Later versions are
    # still catalog-pinned; no HTTP path accepts SQL or this override.
    if (type(rows) is not list or not rows
            or any(type(row) is not tuple or len(row) != 2
                   or type(row[0]) is not int or type(row[1]) is not str for row in rows)):
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    try:
        expected = list(migration_catalog.expected_schema_versions(rows[-1][0]))
    except ServiceError:
        raise ServiceError(ErrorCode.VERSION_CONFLICT) from None
    expected[0] = (1, override_checksum)
    if rows != expected:
        raise ServiceError(ErrorCode.VERSION_CONFLICT)
    return rows[-1][0]


def apply_migration(conn, schema, *, script=None, target_version=1):
    """Own the top-level transaction. Return False for same-checksum repeat.

    `script` is a trusted local migration/test fault-injection input, never HTTP.
    Must use a validated migrator connection from onboarding.storage.
    """
    _validate_connection(conn, schema)
    migration_catalog.expected_schema_versions(target_version)
    if script is not None and target_version != 1:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    source = migration_text(target_version) if script is None else script
    if not isinstance(source, str) or not source.strip():
        raise ServiceError(ErrorCode.INVALID_INPUT)
    checksum = hashlib.sha256(source.encode('utf-8')).hexdigest()
    committing = False
    try:
        with conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
            row = conn.execute(
                "SELECT current_database(),current_user,inet_server_addr(),"
                "(SELECT pg_catalog.pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace WHERE nspname=%s)",
                (schema,)).fetchone()
            if row != ('rf_onboarding_test', 'rf_onboarding_migrator', None, 'rf_onboarding_migrator'):
                raise ServiceError(ErrorCode.INVALID_INPUT)
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ('rf_onboarding.migration.' + schema,))
            conn.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
            relation = conn.execute('SELECT to_regclass(%s)',
                                    (sql.Identifier(schema, 'schema_migrations').as_string(conn),)).fetchone()[0]
            installed = 0
            if relation is not None:
                versions = conn.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
                installed = _installed_version(versions, checksum if script is not None else None)
            if installed >= target_version:
                committing = True
                return False
            if installed != target_version - 1:
                raise ServiceError(ErrorCode.VERSION_CONFLICT)
            conn.execute(source)
            conn.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)', (target_version, checksum))
            if target_version == 1:
                _grant_application(conn, schema)
            else:
                _grant_pool_application(conn, schema)
            committing = True
        return True
    except ServiceError:
        raise
    except psycopg.Error:
        code = ErrorCode.COMMIT_UNKNOWN if committing else ErrorCode.DEPENDENCY_UNAVAILABLE
        raise ServiceError(code) from None


def apply_all(conn, schema, *, target_version=2):
    """Apply each reviewed version in its own transaction; no automatic retry.

    A failed 002 leaves a committed 001 intact and the failure visible. Calling
    the core default on a reviewed newer prefix is a no-op, never a downgrade.
    """
    _validate_connection(conn, schema)
    versions = migration_catalog.expected_schema_versions(target_version)
    changed = False
    for version, _ in versions:
        applied = apply_migration(conn, schema, target_version=version)
        changed = applied or changed
    return changed


def _grant_application(conn, schema):
    ident = sql.Identifier(schema)
    conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(ident))
    conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO rf_onboarding_app').format(ident))
    conn.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC').format(ident))
    conn.execute(sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC').format(ident))
    # Explicit object list, not ALL TABLES: do not grant app access to fixture markers.
    readonly = ('schema_migrations',)
    appendonly = ('global_configs', 'audit_events')
    mutable = ('operators', 'operator_sessions', 'auth_throttles', 'onboarding_batches',
               'onboarding_tasks', 'task_steps', 'resource_leases', 'operation_receipts',
               'secret_objects', 'approvals', 'download_grants')
    for tables, grants in ((readonly, 'SELECT'), (appendonly, 'SELECT, INSERT'),
                           (mutable, 'SELECT, INSERT, UPDATE')):
        for table in tables:
            name = sql.Identifier(schema, table)
            conn.execute(sql.SQL('REVOKE ALL ON {} FROM rf_onboarding_app').format(name))
            conn.execute(sql.SQL('GRANT ' + grants + ' ON {} TO rf_onboarding_app').format(name))
    conn.execute(sql.SQL('GRANT USAGE ON SEQUENCE {} TO rf_onboarding_app').format(
        sql.Identifier(schema, 'audit_events_id_seq')))


def _grant_pool_application(conn, schema):
    mutable = ('mailbox_registry', 'mailbox_platform_states',
               'payment_cards', 'card_reservations')
    appendonly = ('billing_identities', 'card_account_links')
    for tables, grants in ((mutable, 'SELECT, INSERT, UPDATE'),
                           (appendonly, 'SELECT, INSERT')):
        for table in tables:
            name = sql.Identifier(schema, table)
            conn.execute(sql.SQL('REVOKE ALL ON {} FROM PUBLIC, rf_onboarding_app').format(name))
            conn.execute(sql.SQL('GRANT ' + grants + ' ON {} TO rf_onboarding_app').format(name))
    conn.execute(sql.SQL('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {} FROM PUBLIC, rf_onboarding_app').format(sql.Identifier(schema)))
    # Only pure CHECK helpers are directly callable; trigger functions are not
    # public capabilities and all functions run with invoker permissions.
    for name, signature in (('valid_platform_plan', '(text,jsonb)'),
                            ('valid_platform_credential_pins', '(jsonb,jsonb)')):
        conn.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}' + signature + ' TO rf_onboarding_app').format(sql.Identifier(schema, name)))


def main(argv=None):
    import argparse
    import sys
    from onboarding.settings import load_settings
    from onboarding.storage import open_migrator

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True, help='Owner-private migrator JSON under the dedicated test base')
    parser.add_argument('--target-version', type=int, choices=(1, 2), default=1,
                        help='Explicit reviewed target; default preserves the core-only setup')
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings, role='migrator')
        with open_migrator(settings) as conn:
            changed = apply_all(conn, settings.schema, target_version=args.target_version)
        target = f'{args.target_version:03d}'
        print(f'migration {target} applied' if changed else
              f'migration {target} already applied; checksum verified')
        return 0
    except ServiceError as exc:
        print('migration refused: ' + exc.code.value, file=sys.stderr)
        return 2
    except Exception:
        print('migration refused: DEPENDENCY_UNAVAILABLE', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
