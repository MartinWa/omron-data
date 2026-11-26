from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from django.db import transaction

from parsing.report import Reading, parse_text

from .models import COMPONENTS, METRICS, PANEL, ImportRun, Measurement

LOCAL = ZoneInfo("Europe/Stockholm")
VALUE_STEP = Decimal(1).scaleb(-Measurement._meta.get_field("value").decimal_places)
COUNTS = ("created", "skipped", "conflicts", "overwritten", "panels")


def _local(at: datetime) -> datetime:
    return at.replace(tzinfo=LOCAL)


def _instant(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(tzinfo=None)


def _stored(owner, first: datetime, last: datetime) -> dict[tuple[str, datetime], Measurement]:
    rows = Measurement.objects.filter(owner=owner, at__gte=first, at__lte=last)
    return {(row.metric, _instant(row.at)): row for row in rows}


@transaction.atomic
def import_readings(
    owner, readings: list[Reading], *, overwrite: bool = False, run: ImportRun | None = None
) -> dict[str, int]:
    counts = {"created": 0, "skipped": 0, "conflicts": 0, "panels": 0, "overwritten": 0}

    unique: dict[tuple[str, datetime], Reading] = {}
    for reading in readings:
        key = (reading.metric, _instant(_local(reading.at)))
        if key in unique:
            counts["skipped"] += 1
            continue
        unique[key] = reading
    if not unique:
        return counts

    moments = [instant for _, instant in unique]
    first = min(moments).replace(tzinfo=UTC)
    last = max(moments).replace(tzinfo=UTC)
    existing = _stored(owner, first, last)

    rows: list[Measurement] = []
    changed: list[Measurement] = []
    for (metric, instant), reading in unique.items():
        value = reading.value.quantize(VALUE_STEP)
        row = existing.get((metric, instant))
        if row is None:
            rows.append(
                Measurement(
                    owner=owner,
                    metric=metric,
                    at=_local(reading.at),
                    value=value,
                    unit=METRICS[metric].unit,
                    note=reading.note,
                    import_run=run,
                )
            )
            counts["created"] += 1
        elif row.value == value:
            counts["skipped"] += 1
        elif overwrite:
            row.value = value
            row.note = reading.note
            changed.append(row)
            counts["overwritten"] += 1
        else:
            counts["conflicts"] += 1

    notes: dict[datetime, str] = {}
    panel_at: dict[datetime, datetime] = {}
    for (metric, instant), reading in unique.items():
        if metric not in COMPONENTS:
            continue
        panel_at.setdefault(instant, _local(reading.at))
        if reading.note:
            notes.setdefault(instant, reading.note)

    panels: list[Measurement] = []
    for instant, at in panel_at.items():
        if (PANEL, instant) in existing:
            continue
        panels.append(
            Measurement(
                owner=owner,
                metric=PANEL,
                at=at,
                unit="",
                note=notes.get(instant, ""),
                import_run=run,
            )
        )
        counts["panels"] += 1

    if changed:
        Measurement.objects.bulk_update(changed, ["value", "note"])
    if panels:
        Measurement.objects.bulk_create(panels)
    if rows:
        Measurement.objects.bulk_create(rows, ignore_conflicts=True)

    stored = _stored(owner, first, last) if rows or panels else existing

    linked: list[Measurement] = []
    refreshed: list[Measurement] = []
    for instant in panel_at:
        panel = stored.get((PANEL, instant))
        if panel is None:
            continue
        for metric in COMPONENTS:
            component = stored.get((metric, instant))
            if component is None or component.parent_id == panel.pk:
                continue
            component.parent = panel
            linked.append(component)
        note = notes.get(instant, "")
        if note and not panel.note:
            panel.note = note
            refreshed.append(panel)

    if linked:
        Measurement.objects.bulk_update(linked, ["parent"])
    if refreshed:
        Measurement.objects.bulk_update(refreshed, ["note"])

    return counts


def _span(readings: list[Reading]) -> tuple[datetime | None, datetime | None]:
    if not readings:
        return (None, None)
    moments = [_local(reading.at) for reading in readings]
    return (min(moments), max(moments))


@transaction.atomic
def import_report(
    owner, path: Path, *, overwrite: bool = False, dry_run: bool = False
) -> dict[str, object]:
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    result: dict[str, object] = {
        "file_name": path.name,
        "readings": 0,
        **dict.fromkeys(COUNTS, 0),
        "run": None,
        "already_imported": None,
    }

    if not overwrite:
        prior = (
            ImportRun.objects.filter(owner=owner, content_sha256=digest)
            .order_by("-imported_at")
            .first()
        )
        if prior is not None:
            result["already_imported"] = prior.imported_at
            return result

    readings = parse_text(content.decode("utf-8-sig"))
    result["readings"] = len(readings)

    if dry_run:
        with transaction.atomic():
            result.update(import_readings(owner, readings, overwrite=overwrite))
            transaction.set_rollback(True)
        return result

    first_at, last_at = _span(readings)
    run = ImportRun.objects.create(
        owner=owner,
        file_name=path.name,
        content_sha256=digest,
        readings=len(readings),
        first_at=first_at,
        last_at=last_at,
    )
    counts = import_readings(owner, readings, overwrite=overwrite, run=run)
    result.update(counts)
    for field, value in counts.items():
        setattr(run, field, value)
    run.save(update_fields=list(counts))
    result["run"] = run
    return result
