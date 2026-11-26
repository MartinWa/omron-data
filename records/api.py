from datetime import datetime

from django.db.models import Count, Max, Min
from ninja import NinjaAPI, Schema
from ninja.pagination import LimitOffsetPagination, paginate

from .auth import ApiTokenAuth
from .models import Measurement

api = NinjaAPI(urls_namespace="omron", auth=ApiTokenAuth(), docs_url="/docs")


class MeasurementOut(Schema):
    id: int
    metric: str
    value: float | None
    unit: str
    at: datetime
    note: str
    parent_id: int | None

    @staticmethod
    def resolve_value(obj: Measurement) -> float | None:
        return float(obj.value) if obj.value is not None else None


class SummaryOut(Schema):
    metric: str
    count: int
    first: datetime
    last: datetime


@api.get("/measurements", response=list[MeasurementOut])
@paginate(LimitOffsetPagination)
def measurements(
    request,
    metric: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
):
    rows = Measurement.objects.filter(owner=request.auth)
    if metric:
        rows = rows.filter(metric=metric)
    if since:
        rows = rows.filter(at__gte=since)
    if until:
        rows = rows.filter(at__lte=until)
    return rows


@api.get("/summary", response=list[SummaryOut])
def summary(request):
    rows = (
        Measurement.objects.filter(owner=request.auth)
        .values("metric")
        .annotate(count=Count("id"), first=Min("at"), last=Max("at"))
        .order_by("metric")
    )
    return list(rows)


@api.get("/health")
def health(request):
    return {"status": "ok"}
