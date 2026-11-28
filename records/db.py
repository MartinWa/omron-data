import logging
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from django.db import OperationalError, connections
from django.db.backends.signals import connection_created
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger("django")

_PROBE_ALIAS = "_omron_probe"
_PROBE_TIMEOUT_SECONDS = 1.0


def apply_pragmas(cursor):
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")


def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor != "sqlite" or connection.alias == _PROBE_ALIAS:
        return
    if connection.settings_dict.get("NAME") in (None, "", ":memory:"):
        return
    with connection.cursor() as cursor:
        apply_pragmas(cursor)


def connect(dispatch_uid="records.configure_sqlite"):
    connection_created.connect(configure_sqlite, dispatch_uid=dispatch_uid)


def _sqlite_probe_uri(database) -> str | None:
    if database.is_in_memory_db():
        return None
    return Path(database.settings_dict["NAME"]).absolute().as_uri() + "?mode=rw"


@contextmanager
def _probe_connection(database):
    uri = _sqlite_probe_uri(database) if database.vendor == "sqlite" else None
    if uri is None:
        yield database
        return
    with closing(database.copy(alias=_PROBE_ALIAS)) as probe:
        probe.settings_dict["NAME"] = uri
        probe.settings_dict["OPTIONS"]["timeout"] = _PROBE_TIMEOUT_SECONDS
        connections[probe.alias] = probe
        try:
            yield probe
        finally:
            del connections[probe.alias]


def _is_lock_contention(error) -> bool:
    code = getattr(error.__cause__, "sqlite_errorcode", 0)
    return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _check_sqlite_write_access(probe):
    with probe.cursor() as cursor:
        try:
            cursor.execute("BEGIN IMMEDIATE")
        except OperationalError as error:
            if not _is_lock_contention(error):
                raise
            logger.info("The SQLite write lock is held elsewhere; write access was not checked.")
            return
        try:
            cursor.execute("PRAGMA user_version")
            version = cursor.fetchone()[0]
            cursor.execute(f"PRAGMA user_version={version}")
        finally:
            cursor.execute("ROLLBACK")


def probe_database(database) -> list[str]:
    with _probe_connection(database) as probe:
        with probe.cursor() as cursor:
            cursor.execute("SELECT 1")
        executor = MigrationExecutor(probe)
        executor.loader.check_consistent_history(probe)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if probe.alias == _PROBE_ALIAS and not pending:
            _check_sqlite_write_access(probe)
        return [f"{migration.app_label}.{migration.name}" for migration, _ in pending]
