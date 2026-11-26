from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction

from parsing.report import Reading

from .models import COMPONENTS, METRICS, PANEL, Measurement

LOCAL = ZoneInfo("Europe/Stockholm")
VALUE_STEP = Decimal(1).scaleb(-Measurement._meta.get_field("value").decimal_places)


def _local(at: datetime) -> datetime:
    return at.replace(tzinfo=LOCAL)


def _instant(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(tzinfo=None)


def _stored(owner, first: datetime, last: datetime) -> dict[tuple[str, datetime], Measurement]:
    rows = Measurement.objects.filter(owner=owner, at__gte=first, at__lte=last)
    return {(row.metric, _instant(row.at)): row for row in rows}


@transaction.atomic
def import_readings(owner, readings: list[Reading], *, overwrite: bool = False) -> dict[str, int]:
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
            Measurement(owner=owner, metric=PANEL, at=at, unit="", note=notes.get(instant, ""))
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
