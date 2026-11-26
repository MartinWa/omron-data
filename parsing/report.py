"""Parse OMRON connect CSV reports into flat readings.

A report is a sequence of unnamed sections, each introduced by its own header row, so a
section is identified from that header before its columns are mapped by name:

- ``Systoliskt tryck (mmHg)`` — blood pressure, the only section that carries a note, read
  from the first populated of ``Anmärkningar``, ``Mätläge`` and ``Måläge``. That order is
  the priority, wherever the columns sit in the header. A header naming none of those
  carries no note, because nothing is read by position.
- ``Temperatur ()`` — body temperature.
- ``Mätdatum`` — body composition, detail shape (53 columns).
- ``Vikt (kg)`` or ``Vikt (lbs)`` without ``Mätdatum`` — body composition, summary shape.

Columns are mapped through :data:`ALIASES`, one entry per meaning, because the two body
sections spell the same measurement differently (``Kroppsfett (%)`` against
``Kroppsfett–procent``). A header that appears twice resolves to its first populated
occurrence: the real detail shape has ``Fettfri massa (kg)`` blank at position 28 and
populated at position 30. Percent columns may render the sign, so a trailing ``%`` is dropped
before the number is read (the summary shape writes ``34,3%`` where the detail writes
``34,3``).

Both body sections of a file describe the same readings, so :func:`parse_text` returns one
reading per ``(at, metric)``. Values are compared as :class:`~decimal.Decimal`, never as
strings: the same reading renders as ``131`` in one section and ``131.0`` in the other. A value
read from a column outranks a unit-converted one, so a pound-only summary row cannot beat a
kilogram detail row for the same moment; between two equally direct, or two converted,
candidates the first section still wins. Weight in pounds is a fallback only, converted to
kilograms when the kilogram column of that row is empty.

Left unmapped, because they are ``-`` or empty in every row of every export measured so far,
so mapping them now would be guessing rather than reading: ``Kategori``, ``Källa``,
``BMI-kategori``, ``Kroppsfettnivå``, ``Skelettmuskulatur nivå``, ``Nivåklassificering``,
``Kroppsvattennivå``, ``Benmassanivå``, ``Idrottarläge``, ``Beräkningsalgoritm``, ``Kön``,
``Källa för kroppsfett``, ``Källa för skelettmuskulatur``, ``Kroppsfettmassa``,
``Skelettmuskelmassa``, ``Benmassa``, ``Subkutant fett``, ``Kroppsålder`` and the segmental
``* - armar``, ``* - överkropp``, ``* - ben`` pairs. ``Lokalt mätdatum``, ``Viktenhet``,
``Viktdataenhet`` and ``Manuell inmatning`` are metadata, not measurements.

A report holding no content at all — an empty file, or one of blank lines only — yields no
readings, because an empty export is legitimate. Any other report that names no section this
parser recognises raises :class:`UnknownReportError` instead of pretending it was parsed, and
the import command turns that into a ``CommandError``. A header names a section only when it
also carries a measurement column, so a bare date header is not a supported export, while the
same header with an empty measurement column under it is an export that happens to be empty.
That also covers a row of unrelated columns.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

MONTHS = {
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
}

POUNDS = Decimal("0.45359237")
KG_STEP = Decimal("0.001")

ALIASES: dict[str, tuple[str, ...]] = {
    "at": ("Datum", "Mätdatum"),
    "time": ("Tid",),
    "note": ("Anmärkningar", "Mätläge", "Måläge"),
    "systolic_bp": ("Systoliskt tryck (mmHg)",),
    "diastolic_bp": ("Diastoliskt tryck (mmHg)",),
    "pulse": ("Puls (slag/min)",),
    "body_temperature": ("Temperatur ()", "Temperatur (°C)"),
    "body_weight": ("Vikt (kg)",),
    "body_weight_lb": ("Vikt (lbs)",),
    "average_weight_reported": ("Genomsnittlig vikt (kg)",),
    "average_fat_mass": ("Genomsnittlig fettfri massa (kg)",),
    "bmi": ("BMI",),
    "body_fat_pct": ("Kroppsfett (%)", "Kroppsfett–procent"),
    "skeletal_muscle_pct": ("Skelettmuskulatur %", "Skelettmuskulaturprocent"),
    "visceral_fat": ("Visceral fettnivå",),
    "resting_metabolism": ("Ämnesomsättning vid vila",),
    "impedance": ("Impedans",),
    "fat_mass": ("Fettfri massa (kg)",),
    "body_water_pct": ("Kroppsvatten (%)",),
}

SPELLINGS = {spelling: metric for metric, spellings in ALIASES.items() for spelling in spellings}
RESERVED = frozenset({"at", "time", "note"})
MISSING = {"", "-", "\u2013"}
DATE_HEADS = ALIASES["at"]
TEMPERATURE_HEAD = "Temperatur"
EXPECTED_SHAPE = (
    "expected an OMRON connect export whose header row starts with 'Datum' or 'Mätdatum' and "
    "names a section such as 'Systoliskt tryck (mmHg)', 'Temperatur', 'Vikt (kg)' or 'Mätdatum'"
)


@dataclass(frozen=True)
class Reading:
    at: datetime
    metric: str
    value: Decimal
    note: str = ""


class UnknownReportError(ValueError):
    """Raised when a report has header rows but no section this parser recognises."""


def _text(cell: str) -> str:
    stripped = cell.strip()
    return "" if stripped in MISSING else stripped


def _number(text: str) -> Decimal | None:
    cleaned = text.replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    cleaned = cleaned.removesuffix("%")
    if cleaned in MISSING:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _month(token: str) -> int | None:
    cleaned = token.strip().rstrip(".").lower()
    if not cleaned:
        return None
    if cleaned in MONTHS:
        return MONTHS[cleaned]
    found = {value for name, value in MONTHS.items() if name.startswith(cleaned)}
    return found.pop() if len(found) == 1 else None


def _moment(date_text: str, time_text: str) -> datetime | None:
    parts = date_text.replace(".", " ").replace(",", " ").split()
    clock = time_text.split(":")
    if len(parts) != 3 or len(clock) < 2:
        return None
    month = _month(parts[1])
    if month is None:
        return None
    try:
        return datetime(int(parts[2]), month, int(parts[0]), int(clock[0]), int(clock[1]))
    except ValueError:
        return None


def _is_header(row: list[str]) -> bool:
    return bool(row) and row[0].strip().startswith(DATE_HEADS) and "Tid" in row


def _is_data(row: list[str]) -> bool:
    return bool(row) and row[0].strip()[:1].isdigit()


def _measurement(name: str) -> bool:
    metric = _metric(name)
    return metric is not None and metric not in RESERVED


def _section(header: list[str]) -> str | None:
    if not any(_measurement(name) for name in header):
        return None
    if "Systoliskt tryck (mmHg)" in header:
        return "blood_pressure"
    if any(name.startswith(TEMPERATURE_HEAD) for name in header):
        return "temperature"
    if "Mätdatum" in header:
        return "body_detail"
    if "Vikt (kg)" in header or "Vikt (lbs)" in header:
        return "body_summary"
    return None


def _metric(name: str) -> str | None:
    if name in SPELLINGS:
        return SPELLINGS[name]
    if name.startswith(TEMPERATURE_HEAD):
        return "body_temperature"
    return None


def _index(header: list[str]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for position, name in enumerate(header):
        metric = _metric(name)
        if metric is not None:
            index.setdefault(metric, []).append(position)
    return index


def _first(row: list[str], positions: list[int] | None) -> str:
    if not positions or positions[0] >= len(row):
        return ""
    return row[positions[0]]


def _value(row: list[str], positions: list[int]) -> Decimal | None:
    for position in positions:
        if position >= len(row):
            continue
        value = _number(row[position])
        if value is not None:
            return value
    return None


def _values(row: list[str], index: dict[str, list[int]]) -> tuple[dict[str, Decimal], set[str]]:
    values: dict[str, Decimal] = {}
    for metric, positions in index.items():
        if metric in RESERVED:
            continue
        value = _value(row, positions)
        if value is not None:
            values[metric] = value
    converted: set[str] = set()
    pounds = values.pop("body_weight_lb", None)
    if pounds is not None and "body_weight" not in values:
        values["body_weight"] = (pounds * POUNDS).quantize(KG_STEP, rounding=ROUND_HALF_UP)
        converted.add("body_weight")
    return values, converted


def _note(row: list[str], header: list[str]) -> str:
    for spelling in ALIASES["note"]:
        for position, name in enumerate(header):
            if name != spelling or position >= len(row):
                continue
            text = _text(row[position])
            if text:
                return text
    return ""


def _reading(at: datetime, metric: str, value: Decimal, note: str) -> Reading:
    return Reading(at, metric, value, note if metric == "systolic_bp" else "")


def parse_text(text: str) -> list[Reading]:
    readings: dict[tuple[datetime, str], Reading] = {}
    converted_keys: set[tuple[datetime, str]] = set()
    header: list[str] = []
    index: dict[str, list[int]] = {}
    section: str | None = None
    seen_header = False
    recognised = False
    has_content = False

    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        has_content = has_content or any(cell.strip() for cell in row)
        if _is_header(row):
            header = [cell.strip() for cell in row]
            index = _index(header)
            section = _section(header)
            seen_header = True
            recognised = recognised or section is not None
            continue
        if section is None or not _is_data(row):
            continue

        at = _moment(_first(row, index.get("at")), _first(row, index.get("time")))
        if at is None:
            continue
        note = _note(row, header) if "systolic_bp" in index else ""
        values, converted = _values(row, index)
        for metric, value in values.items():
            key = (at, metric)
            if key in readings and (key not in converted_keys or metric in converted):
                continue
            readings[key] = _reading(at, metric, value, note)
            if metric in converted:
                converted_keys.add(key)
            else:
                converted_keys.discard(key)

    if not recognised and has_content:
        shape = "header rows and" if seen_header else "no header row, and"
        raise UnknownReportError(f"report has {shape} no recognised section: {EXPECTED_SHAPE}")
    return list(readings.values())


def parse_file(path: Path) -> list[Reading]:
    return parse_text(path.read_text(encoding="utf-8-sig"))
