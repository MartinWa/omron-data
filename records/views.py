import hmac
import io
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Min, Prefetch, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from parsing.report import UnknownReportError, parse_text

from . import pending
from .importer import CONFLICT, COUNTS, INVALID, REPORTED, import_report
from .limits import MAX_REQUEST_BODY_BYTES, MAX_UPLOAD_BYTES, MAX_UPLOAD_MEGABYTES
from .management.commands.coverage import GAP
from .management.commands.import_omron import LABELS
from .models import COMPONENTS, METRICS, PANEL, ApiToken, ImportRun, Measurement
from .reports import METRIC_LABELS, build_dashboards, build_metric_dashboard
from .templatetags.formatting import num
from .tokens import issue

PAGE_SIZE = 25
IMPORTS_PAGE_SIZE = 25
WARNING_ROW_LIMIT = 10
PENDING_IMPORT_KEY = "pending_import"
TOO_LARGE_MESSAGE = (
    f"CSV export must be {MAX_UPLOAD_MEGABYTES} MB or smaller. Choose a smaller file."
)
NOT_UTF8_MESSAGE = "The file is not UTF-8 text."
NO_ROWS_MESSAGE = "CSV export contains no importable rows"
STORE_FAILED_MESSAGE = "The upload could not be stored. Try again."
PREVIEW_MISSING_MESSAGE = "Your preview expired or was not found. Upload the CSV again."
PREVIEW_STALE_MESSAGE = "This preview is stale. Upload the CSV again."
PREVIEW_EXPIRED_MESSAGE = "This preview expired. Upload the CSV again."
PREVIEW_CANCELLED_MESSAGE = "Preview discarded. Nothing was imported."
RUN_NOT_FOUND_MESSAGE = "That import run was not found."
PURGE_UNCONFIRMED_MESSAGE = "Tick the confirmation box to purge an import run."
UNKNOWN_METRIC_MESSAGE = "That metric is not available."

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
def dashboards(request):
    return render(
        request,
        "records/dashboards.html",
        build_dashboards(request.user, request.GET, timezone.localdate()),
    )


@login_required
def dashboard_metric(request, metric):
    context = build_metric_dashboard(request.user, metric, request.GET, timezone.localdate())
    if context is None:
        raise Http404(UNKNOWN_METRIC_MESSAGE)
    return render(request, "records/dashboard_metric.html", context)


def _stamp(at: datetime) -> str:
    if timezone.is_aware(at):
        return timezone.localtime(at).strftime("%Y-%m-%d %H:%M")
    return at.strftime("%Y-%m-%d %H:%M")


def _decimal_text(value) -> str:
    return "" if value is None else format(value.normalize(), "f")


def _counts_line(report: dict) -> str:
    counts = ", ".join(f"{report[field]} {LABELS[field]}" for field in COUNTS)
    return f"{report['readings']} readings, {counts}"


def _replay_offered(report: dict) -> bool:
    return any((report["already_imported"] is not None, report["skipped"], report["conflicts"]))


def _preview_summary(report: dict, readings, file_name: str) -> dict:
    rejected = report["rejected"]
    conflicts = [item for item in rejected if item.kind == CONFLICT]
    invalid = [item for item in rejected if item.kind == INVALID]
    ordered = sorted(readings, key=lambda reading: reading.at, reverse=True)
    return {
        "file_name": file_name,
        "counts": {field: report[field] for field in REPORTED},
        "readings_total": report["readings"],
        "already_imported": (
            None if report["already_imported"] is None else _stamp(report["already_imported"])
        ),
        "readings": [
            {
                "at": _stamp(reading.at),
                "label": _label(reading.metric),
                "unit": _unit(reading.metric),
                "value": _decimal_text(reading.value),
            }
            for reading in ordered
        ],
        "conflicts": [
            {
                "at": _stamp(item.at),
                "metric": _label(item.metric),
                "stored": _decimal_text(item.stored),
                "incoming": _decimal_text(item.incoming),
            }
            for item in conflicts[:WARNING_ROW_LIMIT]
        ],
        "conflict_total": len(conflicts),
        "conflict_hidden": len(conflicts) - len(conflicts[:WARNING_ROW_LIMIT]),
        "invalid": [_stamp(item.at) for item in invalid[:WARNING_ROW_LIMIT]],
        "invalid_total": len(invalid),
        "invalid_hidden": len(invalid) - len(invalid[:WARNING_ROW_LIMIT]),
        "replay": _replay_offered(report),
        "time_zone": settings.OMRON_TIME_ZONE,
    }


def _preview_report(owner, path: Path) -> dict:
    with transaction.atomic():
        report = import_report(owner, path)
        transaction.set_rollback(True)
    return report


def _render_upload(request, form, too_large=None):
    if too_large:
        form.fields["csv_file"].widget.attrs["aria-describedby"] = (
            "id_csv_file_helptext id_csv_file_error"
        )
    return render(
        request,
        "records/import_upload.html",
        {
            "form": form,
            "too_large": too_large,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_upload_megabytes": MAX_UPLOAD_MEGABYTES,
            "too_large_message": TOO_LARGE_MESSAGE,
        },
    )


def _pending_session(request) -> dict | None:
    stored = request.session.get(PENDING_IMPORT_KEY)
    return stored if isinstance(stored, dict) else None


def _discard_pending(request, import_id: str) -> None:
    pending.discard(import_id)
    request.session.pop(PENDING_IMPORT_KEY, None)


def _expired_preview(request, import_id: str):
    _discard_pending(request, import_id)
    messages.error(request, PREVIEW_EXPIRED_MESSAGE)
    return redirect("import_upload")


class UploadForm(forms.Form):
    csv_file = forms.FileField(
        label="OMRON connect CSV export",
        help_text=(
            f"Up to {MAX_UPLOAD_MEGABYTES} MB. "
            "Nothing is written until you confirm on the next page."
        ),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )


@login_required
def import_upload(request):
    pending.purge_stale()
    if request.method != "POST":
        return _render_upload(request, UploadForm())

    content_length = request.META.get("CONTENT_LENGTH", "")
    size = int(content_length) if content_length.isdigit() else 0
    if size > MAX_REQUEST_BODY_BYTES:
        return _render_upload(request, UploadForm(), too_large=TOO_LARGE_MESSAGE)

    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return _render_upload(request, form)
    upload_file = form.cleaned_data["csv_file"]
    if upload_file.size > MAX_UPLOAD_BYTES:
        form.add_error("csv_file", TOO_LARGE_MESSAGE)
        return _render_upload(request, form)

    content = upload_file.read()
    try:
        readings = parse_text(content.decode("utf-8-sig"))
    except UnicodeDecodeError:
        form.add_error("csv_file", NOT_UTF8_MESSAGE)
        return _render_upload(request, form)
    except UnknownReportError as error:
        form.add_error("csv_file", str(error))
        return _render_upload(request, form)
    if not readings:
        form.add_error("csv_file", NO_ROWS_MESSAGE)
        return _render_upload(request, form)

    previous = _pending_session(request)
    if previous:
        pending.discard(str(previous.get("id", "")))
    import_id = pending.new_import_id()
    try:
        staged = pending.save(import_id, upload_file.name, content)
        report = _preview_report(request.user, staged)
        pending.save_summary(import_id, _preview_summary(report, readings, upload_file.name))
    except UnknownReportError as error:
        pending.discard(import_id)
        form.add_error("csv_file", str(error))
        return _render_upload(request, form)
    except OSError, ValueError:
        pending.discard(import_id)
        form.add_error("csv_file", STORE_FAILED_MESSAGE)
        return _render_upload(request, form)

    request.session[PENDING_IMPORT_KEY] = {
        "id": import_id,
        "fingerprint": pending.fingerprint(content),
        "created_at": timezone.now().timestamp(),
    }
    return redirect("import_preview")


@login_required
def import_preview(request):
    pending.purge_stale()
    stored = _pending_session(request)
    if stored is None:
        messages.warning(request, PREVIEW_MISSING_MESSAGE)
        return redirect("import_upload")

    import_id = str(stored.get("id", ""))
    created_at = stored.get("created_at")
    fingerprint = str(stored.get("fingerprint", ""))
    if not isinstance(created_at, int | float):
        return _expired_preview(request, import_id)
    if timezone.now().timestamp() - created_at > pending.PENDING_TTL_SECONDS:
        return _expired_preview(request, import_id)

    summary = pending.load_summary(import_id)
    content = pending.read_staged(import_id)
    if summary is None or content is None:
        return _expired_preview(request, import_id)
    if not hmac.compare_digest(pending.fingerprint(content), fingerprint):
        _discard_pending(request, import_id)
        messages.error(request, PREVIEW_STALE_MESSAGE)
        return redirect("import_upload")

    if request.method == "POST":
        posted_id = request.POST.get("import_id", "")
        posted_fingerprint = request.POST.get("fingerprint", "")
        if posted_id != import_id or not hmac.compare_digest(posted_fingerprint, fingerprint):
            messages.error(request, PREVIEW_STALE_MESSAGE)
            return redirect("import_upload")
        if request.POST.get("action") == "cancel":
            _discard_pending(request, import_id)
            messages.info(request, PREVIEW_CANCELLED_MESSAGE)
            return redirect("import_upload")
        if request.POST.get("action") != "confirm":
            messages.error(request, PREVIEW_STALE_MESSAGE)
            return redirect("import_upload")
        staged = pending.staged_path(import_id)
        if staged is None:
            return _expired_preview(request, import_id)
        report = import_report(request.user, staged, replay=bool(request.POST.get("replay")))
        _discard_pending(request, import_id)
        messages.success(request, f"Imported {report['file_name']}: {_counts_line(report)}.")
        return redirect("imports")

    return render(
        request,
        "records/import_preview.html",
        {"summary": summary, "import_id": import_id, "fingerprint": fingerprint},
    )


def _span_text(delta: timedelta) -> str:
    return f"{delta.days}d {delta.seconds // 3600}h"


def _coverage(owner) -> dict:
    metrics: list[dict] = []
    for metric in (
        Measurement.objects.filter(owner=owner)
        .values_list("metric", flat=True)
        .distinct()
        .order_by("metric")
    ):
        moments = list(
            Measurement.objects.filter(owner=owner, metric=metric)
            .order_by("at")
            .values_list("at", flat=True)
        )
        metrics.append(
            {
                "label": _label(metric),
                "count": len(moments),
                "first": moments[0],
                "last": moments[-1],
                "gaps": [
                    {"before": before, "after": after, "span": _span_text(after - before)}
                    for before, after in pairwise(moments)
                    if after - before > GAP
                ],
            }
        )
    bounds = Measurement.objects.filter(owner=owner).aggregate(first=Min("at"), last=Max("at"))
    return {
        "readings": Measurement.objects.filter(owner=owner).exclude(metric=PANEL).count(),
        "events": Measurement.objects.filter(owner=owner, parent__isnull=True)
        .values("at")
        .distinct()
        .count(),
        "first": bounds["first"],
        "last": bounds["last"],
        "metrics": metrics,
        "gap_days": GAP.days,
        "gaps": sum(len(row["gaps"]) for row in metrics),
    }


@login_required
def imports(request):
    pending.purge_stale()
    runs = (
        ImportRun.objects.filter(owner=request.user)
        .annotate(rows=Count("measurements"))
        .order_by("-imported_at", "-id")
    )
    page = Paginator(runs, IMPORTS_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "records/imports.html",
        {
            "page": page,
            "has_runs": page.paginator.count > 0,
            "coverage": _coverage(request.user),
        },
    )


@login_required
@require_POST
def purge_import_run(request, pk):
    run = ImportRun.objects.filter(owner=request.user, pk=pk).first()
    if run is None:
        messages.error(request, RUN_NOT_FOUND_MESSAGE)
        return redirect("imports")
    if not request.POST.get("confirm"):
        messages.error(request, PURGE_UNCONFIRMED_MESSAGE)
        return redirect("imports")
    file_name = run.file_name
    rows = Measurement.objects.filter(import_run=run).count()
    try:
        call_command(
            "purge_import",
            id=run.pk,
            owner=request.user.get_username(),
            stdout=io.StringIO(),
        )
    except CommandError:
        messages.error(request, RUN_NOT_FOUND_MESSAGE)
        return redirect("imports")
    messages.success(request, f"Purged {file_name}: {rows} measurements deleted.")
    return redirect("imports")


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
