"""Parse OMRON connect CSV reports into flat readings."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
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
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
}

BP_MARKER = "Systoliskt tryck (mmHg)"
WEIGHT_MARKER = "Vikt (kg)"

BP_COLUMNS = {
    "Systoliskt tryck (mmHg)": "systolic_bp",
    "Diastoliskt tryck (mmHg)": "diastolic_bp",
    "Puls (slag/min)": "pulse",
}

WEIGHT_COLUMNS = {
    "Vikt (kg)": "body_weight",
    "BMI": "bmi",
    "Kroppsfett (%)": "body_fat_pct",
    "Skelettmuskulatur %": "skeletal_muscle_pct",
    "Visceral fettniv\u00e5": "visceral_fat",
    "\u00c4mnesoms\u00e4ttning vid vila": "resting_metabolism",
    "Impedans": "impedance",
    "Fettfri massa (kg)": "fat_free_mass",
}

MISSING = {"", "-", "\u2013"}
DATE_HEADS = ("Datum", "M\u00e4tdatum")


@dataclass(frozen=True)
class Reading:
    at: datetime
    metric: str
    value: Decimal
    note: str = ""


def _number(text: str) -> Decimal | None:
    cleaned = text.replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    if cleaned in MISSING:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _moment(date_text: str, time_text: str) -> datetime | None:
    parts = date_text.replace(".", " ").split()
    clock = time_text.split(":")
    if len(parts) != 3 or len(clock) < 2:
        return None
    month = MONTHS.get(parts[1].lower())
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


def _cells(
    row: list[str], index: dict[str, int], columns: dict[str, str]
) -> list[tuple[str, Decimal]]:
    out = []
    for header, metric in columns.items():
        position = index.get(header)
        if position is None or position >= len(row):
            continue
        value = _number(row[position])
        if value is not None:
            out.append((metric, value))
    return out


def parse_text(text: str) -> list[Reading]:
    readings: list[Reading] = []
    header: list[str] | None = None
    index: dict[str, int] = {}
    note_position = 0
    date_column = "Datum"

    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if _is_header(row):
            header = [cell.strip() for cell in row]
            index = {name: position for position, name in enumerate(header) if name}
            note_position = len(header) - 1
            date_column = next(name for name in header if name.startswith(DATE_HEADS))
            continue
        if header is None or not _is_data(row):
            continue

        at = _moment(row[index[date_column]], row[index["Tid"]])
        if at is None:
            continue
        joined = " ".join(header)
        if BP_MARKER in joined:
            columns = BP_COLUMNS
        elif WEIGHT_MARKER in joined:
            columns = WEIGHT_COLUMNS
        else:
            columns = {}
        note = row[note_position].strip() if note_position < len(row) else ""

        for metric, value in _cells(row, index, columns):
            readings.append(Reading(at, metric, value, note if metric == "systolic_bp" else ""))
    return readings


def parse_file(path: Path) -> list[Reading]:
    return parse_text(path.read_text(encoding="utf-8-sig"))


def parse_files(paths: list[Path]) -> list[Reading]:
    seen: dict[tuple[datetime, str], Reading] = {}
    for path in sorted(paths):
        for reading in parse_file(path):
            seen.setdefault((reading.at, reading.metric), reading)
    return sorted(seen.values(), key=lambda r: (r.at, r.metric))
