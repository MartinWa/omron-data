from datetime import UTC

from django.db.models import Count, F, Max, Min, Prefetch

from .contract import DEFAULT_LIMIT, ApiError
from .models import COMPONENTS, METRICS, PANEL, ImportRun, Measurement
from .params import build_page, parse_sort

SORT_KEYS = ("at", "metric", "value", "id")
LEVELS = ("panel", "component", "all")


def timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def optional_timestamp(value):
    return None if value is None else timestamp(value)


def _component_prefetch(owner) -> Prefetch:
    return Prefetch(
        "components",
        queryset=Measurement.objects.filter(owner=owner).order_by("id"),
        to_attr="sorted_components",
    )


def measurement_record(row) -> dict:
    return {
        "id": row.id,
        "metric": row.metric,
        "value": float(row.value) if row.value is not None else None,
        "unit": row.unit,
        "at": timestamp(row.at),
        "note": row.note,
        "parent_id": row.parent_id,
    }


def _components(row) -> list[dict]:
    return [measurement_record(component) for component in row.sorted_components]


def _order_by(key, descending):
    column = F(key)
    primary = column.desc(nulls_last=True) if descending else column.asc(nulls_first=True)
    return (primary, "id")


def _filtered(owner, metric, since, until, level):
    if level not in LEVELS:
        raise ApiError(422, "validation_error", f"Unsupported level: {level}")
    if metric == PANEL and level == "component":
        raise ApiError(
            422, "validation_error", f"{metric} is a panel; use level=panel or level=all"
        )
    if metric in COMPONENTS and level == "panel":
        raise ApiError(
            422, "validation_error", f"{metric} is a component; use level=component or level=all"
        )
    if metric is not None and metric not in METRICS:
        raise ApiError(422, "validation_error", f"Unsupported metric: {metric}")
    rows = Measurement.objects.filter(owner=owner)
    if metric:
        rows = rows.filter(metric=metric)
    if since:
        rows = rows.filter(at__gte=since)
    if until:
        rows = rows.filter(at__lte=until)
    if level == "panel":
        rows = rows.filter(parent__isnull=True)
    if level == "component":
        rows = rows.filter(parent__isnull=False)
    return rows


def measurement_page(
    owner,
    *,
    metric=None,
    since=None,
    until=None,
    level="panel",
    include_components=False,
    sort="-at",
    limit=DEFAULT_LIMIT,
    offset=0,
    request=None,
) -> dict:
    key, descending = parse_sort(sort, SORT_KEYS, "-at")
    rows = _filtered(owner, metric, since, until, level)
    if include_components and level != "component":
        rows = rows.prefetch_related(_component_prefetch(owner))
    ordered = rows.order_by(*_order_by(key, descending))
    count = ordered.count()
    items = []
    for row in ordered[offset : offset + limit]:
        item = measurement_record(row)
        if include_components and row.parent_id is None:
            item["components"] = _components(row)
        items.append(item)
    return build_page(request, items, count, limit, offset)


def measurement_detail(owner, measurement_id) -> dict | None:
    row = (
        Measurement.objects.filter(owner=owner, pk=measurement_id)
        .prefetch_related(_component_prefetch(owner))
        .first()
    )
    if row is None:
        return None
    return {**measurement_record(row), "components": _components(row)}


def summary_data(owner) -> list[dict]:
    rows = (
        Measurement.objects.filter(owner=owner)
        .values("metric")
        .annotate(count=Count("id"), first=Min("at"), last=Max("at"))
        .order_by("metric")
    )
    return [
        {
            "metric": row["metric"],
            "count": row["count"],
            "first": timestamp(row["first"]),
            "last": timestamp(row["last"]),
        }
        for row in rows
    ]


def metrics_data() -> list[dict]:
    kinds = {PANEL: "panel", **dict.fromkeys(COMPONENTS, "component")}
    return [
        {"code": code, "unit": metric.unit, "kind": kinds.get(code, "reading")}
        for code, metric in METRICS.items()
    ]


def _import_record(run) -> dict:
    return {
        "id": run.id,
        "file_name": run.file_name,
        "content_sha256": run.content_sha256,
        "readings": run.readings,
        "created": run.created,
        "skipped": run.skipped,
        "conflicts": run.conflicts,
        "overwritten": run.overwritten,
        "panels": run.panels,
        "first_at": optional_timestamp(run.first_at),
        "last_at": optional_timestamp(run.last_at),
        "imported_at": timestamp(run.imported_at),
    }


def imports_data(owner, *, limit=DEFAULT_LIMIT, offset=0, request=None) -> dict:
    runs = ImportRun.objects.filter(owner=owner)
    count = runs.count()
    bounds = Measurement.objects.filter(owner=owner).aggregate(first=Min("at"), last=Max("at"))
    newest = runs.order_by("-imported_at", "-id")
    items = [_import_record(run) for run in newest[offset : offset + limit]]
    page = build_page(request, items, count, limit, offset)
    page["first_at"] = optional_timestamp(bounds["first"])
    page["last_at"] = optional_timestamp(bounds["last"])
    return page
