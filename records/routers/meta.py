from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from ninja import Router

from ..contract import ApiError, envelope
from ..params import NO_VALUE, NoQuery
from ..schemas import HealthOut, RootOut

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


@router.get("/readyz", auth=None, response=HealthOut, tags=["meta"], summary="Readiness probe")
def readyz(request, query: NoQuery = NO_VALUE):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as error:
        raise ApiError(503, "service_unavailable", f"Not ready: {error}") from error
    if pending:
        listed = ", ".join(
            f"{migration.app_label}.{migration.name}" for migration, _ in pending[:5]
        )
        raise ApiError(
            503,
            "service_unavailable",
            f"Not ready: {len(pending)} migration(s) pending ({listed})",
        )
    return {**envelope(), "status": "ok"}


@router.get("", response=RootOut, tags=["meta"], summary="API root")
def root(request, query: NoQuery = NO_VALUE):
    return {**envelope(), "name": NAME, "endpoints": list(ENDPOINTS)}
