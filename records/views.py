from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .models import COMPONENTS, METRICS, PANEL, ApiToken, Measurement
from .templatetags.formatting import num
from .tokens import issue

PAGE_SIZE = 25

METRIC_LABELS = {
    "blood_pressure": "Blood pressure",
    "systolic_bp": "Systolic",
    "diastolic_bp": "Diastolic",
    "pulse": "Pulse",
    "body_weight": "Weight",
    "bmi": "BMI",
    "body_fat_pct": "Body fat",
    "skeletal_muscle_pct": "Skeletal muscle",
    "body_water_pct": "Body water",
    "visceral_fat": "Visceral fat",
    "resting_metabolism": "Resting metabolism",
    "impedance": "Impedance",
    "fat_free_mass": "Fat-free mass",
    "average_weight": "Average weight",
    "average_fat_free_mass": "Average fat-free mass",
    "body_temperature": "Body temperature",
}

COMPONENT_ORDER = {code: index for index, code in enumerate(COMPONENTS)}


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.OMRON_TIME_ZONE)


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=_zone())


def _optional_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric)


def _unit(metric: str) -> str:
    registered = METRICS.get(metric)
    return registered.unit if registered else ""


def _component_rows(owner) -> Prefetch:
    return Prefetch(
        "components",
        queryset=Measurement.objects.filter(owner=owner),
        to_attr="event_components",
    )


def _sorted_components(row) -> list[Measurement]:
    return sorted(
        row.event_components,
        key=lambda component: COMPONENT_ORDER.get(component.metric, len(COMPONENT_ORDER)),
    )


def _table_rows(rows, event_note: str) -> list[dict]:
    return [
        {
            "label": _label(row.metric),
            "value": row.value,
            "unit": _unit(row.metric),
            "note": "" if row.note == event_note else row.note,
        }
        for row in rows
    ]


def _badge(value, unit: str) -> str:
    return f"{num(value)} {unit}".strip()


def _panel_badge(components) -> str:
    values = {component.metric: component.value for component in components}
    systolic = num(values.get("systolic_bp"))
    diastolic = num(values.get("diastolic_bp"))
    return f"{systolic}/{diastolic} {_unit(COMPONENTS[0])}".strip()


def _event_title(rows, groups) -> str:
    if any(row.metric == PANEL for row in rows):
        return _label(PANEL)
    if len(groups) == 1:
        return groups[0]["label"]
    return f"{len(groups)} readings"


def _event(at: datetime, rows) -> dict:
    rows = sorted(rows, key=lambda row: (row.metric != PANEL, _label(row.metric)))
    note = next((row.note for row in rows if row.note), "")
    groups: list[dict] = []
    badges: list[str] = []
    for row in rows:
        if row.metric == PANEL:
            components = _sorted_components(row)
            badges.append(_panel_badge(components))
            groups.append(
                {
                    "label": _label(PANEL),
                    "count": len(components),
                    "rows": _table_rows(components, note),
                }
            )
            continue
        badges.append(_badge(row.value, _unit(row.metric)))
        groups.append({"label": _label(row.metric), "count": 1, "rows": _table_rows([row], note)})
    return {
        "at": at,
        "title": _event_title(rows, groups),
        "note": note,
        "badges": badges,
        "groups": groups,
        "count": sum(group["count"] for group in groups),
    }


def _events(rows) -> list[dict]:
    grouped: dict[datetime, list[Measurement]] = {}
    for row in rows:
        grouped.setdefault(row.at, []).append(row)
    return [_event(at, members) for at, members in grouped.items()]


def _metric_options() -> list[dict]:
    return [{"code": code, "label": _label(code)} for code in METRICS]


@login_required
def readings(request):
    metric = request.GET.get("metric", "").strip()
    selected_metric = metric if metric in METRICS else ""
    query = request.GET.get("q", "").strip()
    date_from = _optional_date(request.GET.get("from", ""))
    date_to = _optional_date(request.GET.get("to", ""))
    filtered = bool(selected_metric or query or date_from or date_to)

    stored = Measurement.objects.filter(owner=request.user)
    has_data = stored.exists()
    total_readings = stored.exclude(metric=PANEL).count()
    total_events = stored.filter(parent__isnull=True).values("at").distinct().count()

    rows = stored.filter(parent__isnull=True)
    if selected_metric:
        rows = rows.filter(Q(metric=selected_metric) | Q(components__metric=selected_metric))
    if date_from:
        rows = rows.filter(at__gte=_day_start(date_from))
    if date_to:
        rows = rows.filter(at__lt=_day_start(date_to + timedelta(days=1)))
    if query:
        rows = rows.filter(note__icontains=query)
    times = rows.order_by("-at").values_list("at", flat=True).distinct()
    page = Paginator(times, PAGE_SIZE).get_page(request.GET.get("page"))
    events = _events(
        rows.filter(at__in=list(page.object_list)).prefetch_related(_component_rows(request.user))
    )
    return render(
        request,
        "records/readings.html",
        {
            "page": page,
            "events": events,
            "has_data": has_data,
            "filtered": filtered,
            "total_readings": total_readings,
            "total_events": total_events,
            "metrics": _metric_options(),
            "selected_metric": selected_metric,
            "query": query,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
        },
    )


@login_required
def imports(request):
    messages.info(request, "Import history is not built yet.")
    return redirect("readings")


class TokenForm(forms.Form):
    name = forms.CharField(
        max_length=64,
        help_text="A name you will recognise, such as the device or script that uses it.",
    )

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.owner is not None and ApiToken.objects.filter(owner=self.owner, name=name).exists():
            raise forms.ValidationError("A token with this name already exists.")
        return name


@login_required
def tokens(request):
    secret = None
    created_name = ""
    if request.method == "POST":
        form = TokenForm(request.POST, owner=request.user)
        if form.is_valid():
            created_name = form.cleaned_data["name"]
            secret = issue(request.user, created_name)
            messages.success(
                request,
                f"Created {created_name}. Copy the secret now; it is shown only once.",
            )
            form = TokenForm(owner=request.user)
    else:
        form = TokenForm(owner=request.user)
    return render(
        request,
        "records/tokens.html",
        {
            "tokens": ApiToken.objects.filter(owner=request.user),
            "form": form,
            "secret": secret,
            "created_name": created_name,
        },
    )


@login_required
@require_POST
def revoke_token(request, pk):
    token = ApiToken.objects.filter(owner=request.user, pk=pk).first()
    if token is None:
        messages.error(request, "That token was not found.")
        return redirect("tokens")
    if token.revoked:
        messages.info(request, f"{token.name} was already revoked.")
        return redirect("tokens")
    token.revoked = True
    token.save(update_fields=["revoked"])
    messages.success(request, f"Revoked {token.name}. Clients using it stop working now.")
    return redirect("tokens")


@login_required
@require_POST
def delete_token(request, pk):
    token = ApiToken.objects.filter(owner=request.user, pk=pk).first()
    if token is None:
        messages.error(request, "That token was not found.")
        return redirect("tokens")
    if not token.revoked:
        messages.error(request, f"Revoke {token.name} first; a working token cannot be deleted.")
        return redirect("tokens")
    name = token.name
    token.delete()
    messages.success(request, f"Deleted {name}. That name can be used again.")
    return redirect("tokens")
