import logging

from django.db import connection
from ninja import Router

from ..contract import STATUS_CODES, ApiError, envelope
from ..db import probe_database
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


@router.get("/readyz", auth=None, response=HealthOut, tags=["meta"], summary="Readiness probe")
def readyz(request, query: NoQuery = NO_VALUE):
    try:
        pending = probe_database(connection)
    except Exception:
        logger.exception("Readiness check failed")
    else:
        if not pending:
            return {**envelope(), "status": "ok"}
        logger.error(
            "Readiness check failed: %s migration(s) pending (%s)",
            len(pending),
            ", ".join(pending[:5]),
        )
    raise ApiError(503, "service_unavailable", STATUS_CODES[503][1])


@router.get("", response=RootOut, tags=["meta"], summary="API root")
def root(request, query: NoQuery = NO_VALUE):
    return {**envelope(), "name": NAME, "endpoints": list(ENDPOINTS)}
