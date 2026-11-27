import logging
from pathlib import Path

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
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


def _is_memory_database(name: str) -> bool:
    return name == ":memory:" or "mode=memory" in name


def _readiness_problem() -> str | None:
    settings_dict = connection.settings_dict
    name = settings_dict["NAME"]
    if (
        settings_dict.get("ENGINE") == "django.db.backends.sqlite3"
        and not _is_memory_database(name)
        and not Path(name).is_file()
    ):
        problem = f"database file {name} does not exist"
        logger.error("Readiness check failed: %s", problem)
        return problem
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:
        logger.exception("Readiness check failed")
        return "database check raised"
    if pending:
        listed = ", ".join(
            f"{migration.app_label}.{migration.name}" for migration, _ in pending[:5]
        )
        problem = f"{len(pending)} migration(s) pending ({listed})"
        logger.error("Readiness check failed: %s", problem)
        return problem
    return None


@router.get("/readyz", auth=None, response=HealthOut, tags=["meta"], summary="Readiness probe")
def readyz(request, query: NoQuery = NO_VALUE):
    if _readiness_problem():
        raise ApiError(503, "service_unavailable", STATUS_CODES[503][1])
    return {**envelope(), "status": "ok"}


@router.get("", response=RootOut, tags=["meta"], summary="API root")
def root(request, query: NoQuery = NO_VALUE):
    return {**envelope(), "name": NAME, "endpoints": list(ENDPOINTS)}
