from ninja import Router

from ..contract import envelope
from ..params import NO_VALUE, NoQuery
from ..schemas import HealthOut, RootOut

NAME = "omron-data"
ENDPOINTS = (
    "/api/v1/healthz",
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


@router.get("", response=RootOut, tags=["meta"], summary="API root")
def root(request, query: NoQuery = NO_VALUE):
    return {**envelope(), "name": NAME, "endpoints": list(ENDPOINTS)}
