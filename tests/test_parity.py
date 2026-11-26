"""Opt-in parity check against real OMRON connect exports.

Skipped unless ``OMRON_LOCAL_CSV`` names a directory of real exports, so the committed suite
stays wholly synthetic. Nothing here reads a fixed path.
"""

from __future__ import annotations

import csv
import io
import os
from collections import Counter
from pathlib import Path

import pytest

from parsing.report import parse_file
from records.models import METRICS

DIRECTORY = os.environ.get("OMRON_LOCAL_CSV", "")
MARKERS = ("Systoliskt tryck (mmHg)", "Temperatur", "Mätdatum", "Vikt (kg)", "Vikt (lbs)")

pytestmark = pytest.mark.skipif(not DIRECTORY, reason="OMRON_LOCAL_CSV is not set")


def exports() -> list[Path]:
    return sorted(Path(DIRECTORY).glob("*.csv"))


def rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]


def sections(path: Path):
    header: list[str] | None = None
    for row in rows(path):
        if row and row[0] in ("Datum", "Mätdatum") and "Tid" in row:
            header = row
            continue
        if header is not None:
            yield header, row


def test_exports_are_present():
    assert exports()


def test_every_section_is_recognised():
    for path in exports():
        for header, _ in sections(path):
            joined = " ".join(header)
            assert any(marker in joined for marker in MARKERS), (path.name, header[:3])


def test_no_duplicate_reading_survives():
    for path in exports():
        keys = [(reading.at, reading.metric) for reading in parse_file(path)]
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        assert duplicates == [], path.name


def test_no_data_row_is_silently_dropped():
    for path in exports():
        expected = 0
        for header, row in sections(path):
            if not row or not row[0][:1].isdigit():
                continue
            if "Temperatur" in " ".join(header):
                continue
            expected += 1
        assert len(parse_file(path)) >= expected, path.name


def test_every_metric_is_registered():
    for path in exports():
        for reading in parse_file(path):
            assert reading.metric in METRICS, (path.name, reading.metric)
