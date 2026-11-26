from django.http import Http404
from ninja import Router

from ..contract import envelope
from ..params import (
    LIMIT_QUERY,
    NO_VALUE,
    OFFSET_QUERY,
    IntParam,
    NoQuery,
    OptDateTimeParam,
    OptFilterParam,
    OptFlagParam,
    StrictQuery,
    metric_query,
    sort_query,
    validate_pagination,
    validate_range,
)
from ..queries import (
    SORT_KEYS,
    measurement_detail,
    measurement_page,
    metrics_data,
    summary_data,
)
from ..schemas import MeasurementDetailOut, MeasurementPageOut, MetricsOut, SummaryOut

LEVEL_QUERY = StrictQuery(
    "panel",
    description=(
        "Which rows to return: panel (top-level events), component, or all. "
        "A component metric requires level=component or level=all; "
        "the blood_pressure panel requires level=panel or level=all."
    ),
)
INCLUDE_QUERY = StrictQuery(
    None,
    description="Pass components (or true) to inline each panel's components in the item.",
)
SORT_QUERY = sort_query(SORT_KEYS, "-at")
METRIC_QUERY = metric_query()

router = Router()


@router.get("/metrics", response=MetricsOut, tags=["measurements"], summary="Metric registry")
def metrics(request, query: NoQuery = NO_VALUE):
    items = metrics_data()
    return {**envelope(), "count": len(items), "items": items}


@router.get(
    "/measurements",
    response=MeasurementPageOut,
    tags=["measurements"],
    summary="Paginated measurements",
)
def measurements(
    request,
    metric: OptFilterParam = METRIC_QUERY,
    since: OptDateTimeParam = NO_VALUE,
    until: OptDateTimeParam = NO_VALUE,
    level: str = LEVEL_QUERY,
    include: OptFlagParam = INCLUDE_QUERY,
    sort: str | None = SORT_QUERY,
    limit: IntParam = LIMIT_QUERY,
    offset: IntParam = OFFSET_QUERY,
):
    validate_range(since, until)
    validate_pagination(limit, offset)
    page = measurement_page(
        request.auth,
        metric=metric,
        since=since,
        until=until,
        level=level,
        include_components=bool(include),
        sort=sort,
        limit=limit,
        offset=offset,
        request=request,
    )
    return {**envelope(), **page}


@router.get(
    "/measurements/{measurement_id}",
    response=MeasurementDetailOut,
    tags=["measurements"],
    summary="Measurement detail",
)
def measurement(request, measurement_id: IntParam, query: NoQuery = NO_VALUE):
    item = measurement_detail(request.auth, measurement_id)
    if item is None:
        raise Http404("Measurement not found")
    return {**envelope(), **item}


@router.get("/summary", response=SummaryOut, tags=["measurements"], summary="Per-metric coverage")
def summary(request, query: NoQuery = NO_VALUE):
    items = summary_data(request.auth)
    return {**envelope(), "count": len(items), "items": items}
