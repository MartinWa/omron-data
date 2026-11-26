from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import OperationalError, transaction

from parsing.report import Reading, parse_text

from .models import COMPONENTS, METRICS, PANEL, ImportRun, Measurement

VALUE_STEP = Decimal(1).scaleb(-Measurement._meta.get_field("value").decimal_places)
COUNTS = ("created", "skipped", "conflicts", "overwritten", "panels")
REPORTED = (*COUNTS, "invalid")
CONFLICT = "conflict"
INVALID = "invalid"
BUSY_ATTEMPTS = 4
BUSY_BACKOFF_SECONDS = 0.05
BUSY_MESSAGES = ("database is locked", "database table is locked", "SQLITE_BUSY_SNAPSHOT")


def busy_database(error: Exception) -> bool:
    if not isinstance(error, OperationalError):
        return False
    message = str(error)
    return any(text in message for text in BUSY_MESSAGES)


def retry_when_busy(operation, *, attempts: int | None = None, backoff: float | None = None):
    limit = BUSY_ATTEMPTS if attempts is None else attempts
    pause = BUSY_BACKOFF_SECONDS if backoff is None else backoff
    for attempt in range(1, limit + 1):
        try:
            return operation()
        except OperationalError as error:
            if attempt >= limit or not busy_database(error):
                raise
            time.sleep(pause * attempt)


@dataclass(frozen=True)
class Rejected:
    kind: str
    at: datetime
    metric: str = ""
    stored: Decimal | None = None
    incoming: Decimal | None = None


def local_zone() -> ZoneInfo:
    return ZoneInfo(settings.OMRON_TIME_ZONE)


def _local(at: datetime, tz: ZoneInfo) -> datetime:
    return at.replace(tzinfo=tz)


def _instant(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(tzinfo=None)


def _exists(at: datetime, tz: ZoneInfo) -> bool:
    return at.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None) == at


def _stored(owner, first: datetime, last: datetime) -> dict[tuple[str, datetime], Measurement]:
    rows = Measurement.objects.filter(owner=owner, at__gte=first, at__lte=last)
    return {(row.metric, _instant(row.at)): row for row in rows}


@transaction.atomic
def import_readings(
    owner,
    readings: list[Reading],
    *,
    overwrite: bool = False,
    run: ImportRun | None = None,
    rejected: list[Rejected] | None = None,
) -> dict[str, int]:
    tz = local_zone()
    counts: dict[str, int] = dict.fromkeys(REPORTED, 0)
    missing: set[datetime] = set()

    unique: dict[tuple[str, datetime], Reading] = {}
    for reading in readings:
        if not _exists(reading.at, tz):
            if reading.at not in missing:
                missing.add(reading.at)
                counts[INVALID] += 1
                if rejected is not None:
                    rejected.append(Rejected(INVALID, reading.at))
            continue
        key = (reading.metric, _instant(_local(reading.at, tz)))
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
    conflicted: set[tuple[str, datetime]] = set()
    for (metric, instant), reading in unique.items():
        value = reading.value.quantize(VALUE_STEP)
        row = existing.get((metric, instant))
        if row is None:
            rows.append(
                Measurement(
                    owner=owner,
                    metric=metric,
                    at=_local(reading.at, tz),
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
            conflicted.add((metric, instant))
            if rejected is not None:
                rejected.append(
                    Rejected(CONFLICT, _local(reading.at, tz), metric, row.value, value)
                )

    notes: dict[datetime, str] = {}
    panel_at: dict[datetime, datetime] = {}
    for (metric, instant), reading in unique.items():
        if metric not in COMPONENTS:
            continue
        panel_at.setdefault(instant, _local(reading.at, tz))
        if reading.note and (metric, instant) not in conflicted:
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


def _span(readings: list[Reading], tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    moments = [_local(reading.at, tz) for reading in readings if _exists(reading.at, tz)]
    if not moments:
        return (None, None)
    return (min(moments), max(moments))


def import_report(
    owner, path: Path, *, overwrite: bool = False, replay: bool = False
) -> dict[str, object]:
    return retry_when_busy(lambda: _import_report(owner, path, overwrite=overwrite, replay=replay))


@transaction.atomic
def _import_report(
    owner, path: Path, *, overwrite: bool = False, replay: bool = False
) -> dict[str, object]:
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    result: dict[str, object] = {
        "file_name": path.name,
        "readings": 0,
        **dict.fromkeys(REPORTED, 0),
        "rejected": [],
        "run": None,
        "already_imported": None,
    }

    if not (overwrite or replay):
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

    first_at, last_at = _span(readings, local_zone())
    run = ImportRun.objects.create(
        owner=owner,
        file_name=path.name,
        content_sha256=digest,
        readings=len(readings),
        first_at=first_at,
        last_at=last_at,
    )
    rejected: list[Rejected] = []
    counts = import_readings(owner, readings, overwrite=overwrite, run=run, rejected=rejected)
    result.update(counts)
    result["rejected"] = rejected
    for field in COUNTS:
        setattr(run, field, counts[field])
    run.save(update_fields=list(COUNTS))
    result["run"] = run
    return result
