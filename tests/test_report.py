"""Parser tests over synthetic reports shaped like an OMRON connect export."""

from datetime import datetime
from decimal import Decimal

from parsing.report import parse_files, parse_text

BLOOD_PRESSURE = """\ufeffDatum,Tid,Systoliskt tryck (mmHg),Diastoliskt tryck (mmHg),Puls (slag/min),Anmärkningar,Måläge\r
04 juli 2024,10:31,145,81,68,,AFib\r
26 juni 2024,11:33,133,79,72,,\r
"""

WEIGHTS = """Mätdatum,Tid,Lokalt mätdatum,Viktenhet,Manuell inmatning,Vikt (kg),BMI,Kroppsfett (%),Skelettmuskulatur %,Visceral fettnivå,Ämnesomsättning vid vila,Impedans,Fettfri massa (kg),Kroppsvatten (%)\r
25 nov. 2026,07:43,25 nov. 2026,kg,0,"82,4",27.1,"21,3","31,2",9,1720,"402,5","64,85",-\r
"""


def test_blood_pressure_rows():
    readings = parse_text(BLOOD_PRESSURE)
    first = [reading for reading in readings if reading.at.day == 4]
    assert {reading.metric for reading in first} == {"systolic_bp", "diastolic_bp", "pulse"}
    systolic = next(reading for reading in first if reading.metric == "systolic_bp")
    assert systolic.at == datetime(2024, 7, 4, 10, 31)
    assert systolic.value == Decimal("145")
    assert systolic.note == "AFib"


def test_weight_rows_use_decimal_comma():
    readings = parse_text(WEIGHTS)
    values = {reading.metric: reading.value for reading in readings}
    assert values["body_weight"] == Decimal("82.4")
    assert values["body_fat_pct"] == Decimal("21.3")
    assert values["impedance"] == Decimal("402.5")
    assert values["visceral_fat"] == Decimal("9")
    assert "fat_free_mass" in values


def test_skips_missing_and_ignores_headers():
    readings = parse_text(WEIGHTS)
    assert all(reading.value is not None for reading in readings)
    assert len(parse_text("Datum,Tid,Vikt (kg)\r\n")) == 0


def test_overlapping_ranges_deduplicate(tmp_path):
    early = tmp_path / "a.csv"
    late = tmp_path / "b.csv"
    early.write_text(BLOOD_PRESSURE, encoding="utf-8")
    late.write_text(BLOOD_PRESSURE, encoding="utf-8")

    once = parse_files([early])
    twice = parse_files([early, late])
    assert len(once) == len(twice)
