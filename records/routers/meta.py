import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from ninja import Router

from ..contract import STATUS_CODES, ApiError, envelope
from ..params import NO_VALUE, NoQuery
from ..schemas import HealthOut, RootOut

logger = logging.getLogger("django")

NAME = "omron-data"
ENDPOINTS = (
    "/api/v1/healthz",
    "/api/v1/readyz",
    "/api/v1/metrics",
    "/api/v1/measurements",
    "/api/v1/measurements/{id}",
    "/api/v1/summary",
    "/api/v1/imports",
)

router = Router()


@router.get("/healthz", auth=None, response=HealthOut, tags=["meta"], summary="Liveness probe")
def healthz(request, query: NoQuery = NO_VALUE):
    return {**envelope(), "status": "ok"}


def _pending_problem(pending: list[str]) -> str | None:
    if not pending:
        return None
    problem = f"{len(pending)} migration(s) pending ({', '.join(pending[:5])})"
    logger.error("Readiness check failed: %s", problem)
    return problem


def _sqlite_file_problem(name) -> str | None:
    uri = f"file:{Path(name).as_posix()}?mode=rw"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as probe:
            probe.execute("SELECT 1")
            try:
                applied = {
                    f"{app}.{migration}"
                    for app, migration in probe.execute("SELECT app, name FROM django_migrations")
                }
            except sqlite3.OperationalError:
                applied = set()
    except sqlite3.Error:
        logger.exception("Readiness check failed")
        return "database probe raised"
    loader = MigrationLoader(None)
    pending = [
        f"{app}.{migration}"
        for app, migration in loader.graph.leaf_nodes()
        if f"{app}.{migration}" not in applied
    ]
    return _pending_problem(pending)


def _connection_problem() -> str | None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:
        logger.exception("Readiness check failed")
        return "database check raised"
    return _pending_problem([f"{migration.app_label}.{migration.name}" for migration, _ in pending])


def _readiness_problem() -> str | None:
    settings_dict = connection.settings_dict
    if (
        settings_dict.get("ENGINE") == "django.db.backends.sqlite3"
        and not connection.is_in_memory_db()
    ):
        return _sqlite_file_problem(settings_dict["NAME"])
    return _connection_problem()


@router.get("/readyz", auth=None, response=HealthOut, tags=["meta"], summary="Readiness probe")
def readyz(request, query: NoQuery = NO_VALUE):
    if _readiness_problem():
        raise ApiError(503, "service_unavailable", STATUS_CODES[503][1])
    return {**envelope(), "status": "ok"}


@router.get("", response=RootOut, tags=["meta"], summary="API root")
def root(request, query: NoQuery = NO_VALUE):
    return {**envelope(), "name": NAME, "endpoints": list(ENDPOINTS)}
