"""Verified local PostgreSQL connections and short explicit transactions."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

import psycopg
from psycopg import sql

from .errors import ErrorCode, ServiceError
from .settings import BASE, Settings, validate_settings


def _validate_socket(host: str) -> None:
    """Reject redirected paths before libpq is allowed to connect."""
    path = Path(host)
    try:
        for item in (*reversed(path.parents), path):
            info = os.lstat(item)
            if not stat.S_ISDIR(info.st_mode):
                raise ServiceError(ErrorCode.INVALID_INPUT)
            if item == BASE or item.is_relative_to(BASE):
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ServiceError(ErrorCode.INVALID_INPUT)
    except OSError:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def _schema_owner(conn, schema: str):
    return conn.execute(
        "SELECT r.rolname FROM pg_catalog.pg_namespace AS n "
        "JOIN pg_catalog.pg_roles AS r ON r.oid = n.nspowner WHERE n.nspname = %s",
        (schema,),
    ).fetchone()


def _close(conn):
    try:
        conn.close()
    except psycopg.Error:
        # Never replace a classified transaction outcome with a cleanup error.
        pass


def _open(settings: Settings, role: str):
    validate_settings(settings, role=role)
    # libpq service/hostaddr/options environment settings could bypass the
    # explicit Unix-socket contract or execute unexpected startup options.
    if any(os.environ.get(name) for name in
           ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")):
        raise ServiceError(ErrorCode.INVALID_INPUT)
    _validate_socket(settings.host)
    conn = None
    try:
        conn = psycopg.connect(
            host=settings.host, port=settings.port, dbname=settings.dbname,
            user=settings.user, password=settings.password,
            autocommit=True, connect_timeout=3,
            options="-c search_path=pg_catalog", application_name="rf-onboarding-p1b",
        )
        if conn.info.host != settings.host or conn.info.port != settings.port:
            raise ServiceError(ErrorCode.INVALID_INPUT)
        identity = conn.execute(
            "SELECT pg_catalog.current_database(), current_user, "
            "pg_catalog.shobj_description(d.oid, 'pg_database'), "
            "pg_catalog.inet_server_addr(), pg_catalog.current_setting('listen_addresses') "
            "FROM pg_catalog.pg_database AS d WHERE d.datname = pg_catalog.current_database()"
        ).fetchone()
        if identity != (settings.dbname, settings.user, settings.instance_marker, None, ""):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        owner = _schema_owner(conn, settings.schema)
        if owner is not None and owner != ("rf_onboarding_migrator",):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        conn.execute(sql.SQL("SET search_path TO {}, {}").format(
            sql.Identifier(settings.schema), sql.Identifier("pg_catalog")))
        return conn
    except ServiceError:
        if conn is not None:
            _close(conn)
        raise
    except psycopg.Error:
        if conn is not None:
            _close(conn)
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None


def open_app(settings: Settings):
    """Return an autocommit application connection after target verification."""
    return _open(settings, "app")


def open_migrator(settings: Settings):
    """Return a separately authorized migration connection, never elevate app."""
    return _open(settings, "migrator")


@contextmanager
def unit_of_work(settings: Settings):
    """One connection and short transaction; a lost COMMIT reply is not failure proof."""
    conn = open_app(settings)
    body_finished = False
    try:
        if _schema_owner(conn, settings.schema) != ("rf_onboarding_migrator",):
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
            yield conn
            body_finished = True
    except ServiceError:
        raise
    except (psycopg.OperationalError, psycopg.InterfaceError):
        code = ErrorCode.COMMIT_UNKNOWN if body_finished else ErrorCode.DEPENDENCY_UNAVAILABLE
        raise ServiceError(code) from None
    except Exception:
        raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
    finally:
        _close(conn)
