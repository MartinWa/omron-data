"""Parser tests over synthetic reports shaped like an OMRON connect export."""

from datetime import datetime
from decimal import Decimal

import pytest

from parsing.report import Reading, parse_file, parse_text

BLOOD_PRESSURE = """\ufeffDatum,Tid,Systoliskt tryck (mmHg),Diastoliskt tryck (mmHg),Puls (slag/min),Anmärkningar,Mätläge\r
04 juli 2024,10:31,145,81,68,,AFib\r
26 juni 2024,11:33,133,79,72,,\r
"""

SUMMARY = """Datum,Tid,Vikt (kg),BMI,Kategori,Ämnesomsättning vid vila,Skelettmuskulaturprocent,Kroppsfett–procent,Visceral fettnivå,Källa\r
25 nov. 2026,07:43,"82,4",27.1,12,1720,"31,2%","21,3%",9,0\r
"""

DETAIL = """Mätdatum,Tid,Lokalt mätdatum,Viktenhet,Manuell inmatning,Vikt (kg),Vikt (lbs),Genomsnittlig vikt (kg),BMI,Kroppsfett (%),Skelettmuskulatur %,Visceral fettnivå,Ämnesomsättning vid vila,Kroppsvatten (%),Fettfri massa (kg),Fettfri massa (lbs),Fettfri massa (kg),Genomsnittlig fettfri massa (kg),Impedans\r
25 nov. 2026,07:43,24 nov. 2026,kg,0,"82,4","181,0","82,3",27.1,"21,3","31,2",9,1720,-,-,-,"64,85","64,9","402,5"\r
"""

TEMPERATURE = """Datum,Tid,Temperatur ()\r
25 nov. 2026,07:43,"36,5"\r
"""

WEIGHTS = """Mätdatum,Tid,Lokalt mätdatum,Viktenhet,Manuell inmatning,Vikt (kg),BMI,Kroppsfett (%),Skelettmuskulatur %,Visceral fettnivå,Ämnesomsättning vid vila,Impedans,Fettfri massa (kg),Kroppsvatten (%)\r
25 nov. 2026,07:43,25 nov. 2026,kg,0,"82,4",27.1,"21,3","31,2",9,1720,"402,5","64,85",-\r
"""


def metrics(text):
    return {reading.metric for reading in parse_text(text)}


def values(text):
    return {reading.metric: reading.value for reading in parse_text(text)}


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
    found = {reading.metric: reading.value for reading in readings}
    assert found["body_weight"] == Decimal("82.4")
    assert found["body_fat_pct"] == Decimal("21.3")
    assert found["impedance"] == Decimal("402.5")
    assert found["visceral_fat"] == Decimal("9")
    assert "fat_free_mass" in found


def test_skips_missing_and_ignores_headers():
    readings = parse_text(WEIGHTS)
    assert all(reading.value is not None for reading in readings)
    assert len(parse_text("Datum,Tid,Vikt (kg)\r\n")) == 0


def test_a_duplicate_note_column_falls_through_to_its_second_occurrence():
    header = (
        "Datum,Tid,Systoliskt tryck (mmHg),Diastoliskt tryck (mmHg),Puls (slag/min),"
        "Anmärkningar,Mätläge,Anmärkningar"
    )
    report = "\n".join([header, "4 juli 2024,10:31,145,81,68,,AFib,Second comment", ""])

    readings = parse_text(report)

    assert next(r for r in readings if r.metric == "systolic_bp").note == "Second comment"


def test_blood_pressure_section_metrics():
    assert metrics(BLOOD_PRESSURE) == {"systolic_bp", "diastolic_bp", "pulse"}


def test_temperature_section_metrics():
    assert parse_text(TEMPERATURE) == [
        Reading(datetime(2026, 11, 25, 7, 43), "body_temperature", Decimal("36.5"))
    ]


def test_summary_section_metrics():
    assert metrics(SUMMARY) == {
        "body_weight",
        "bmi",
        "resting_metabolism",
        "skeletal_muscle_pct",
        "body_fat_pct",
        "visceral_fat",
    }


def test_detail_section_metrics():
    assert metrics(DETAIL) == {
        "body_weight",
        "average_weight",
        "bmi",
        "body_fat_pct",
        "skeletal_muscle_pct",
        "visceral_fat",
        "resting_metabolism",
        "fat_free_mass",
        "average_fat_free_mass",
        "impedance",
    }


def test_body_sections_yield_one_reading_per_metric():
    readings = parse_text(SUMMARY + DETAIL)
    keys = [(reading.at, reading.metric) for reading in readings]
    assert len(keys) == len(set(keys))
    assert sum(1 for reading in readings if reading.metric == "body_weight") == 1


def test_131_and_131_0_are_the_same_reading():
    summary = "Datum,Tid,Vikt (kg)\r\n25 nov. 2026,07:43,131\r\n"
    detail = "Mätdatum,Tid,Vikt (kg)\r\n25 nov. 2026,07:43,131.0\r\n"

    readings = parse_text(summary + detail)

    assert len(readings) == 1
    assert readings[0].value == Decimal("131") == Decimal("131.0")


def test_explicit_kg_in_the_detail_outranks_a_converted_pounds_summary():
    summary = 'Datum,Tid,Vikt (lbs)\r\n25 nov. 2026,07:43,"200,0"\r\n'
    detail = 'Mätdatum,Tid,Vikt (kg)\r\n25 nov. 2026,07:43,"81,9"\r\n'

    readings = parse_text(summary + detail)

    assert len(readings) == 1
    assert readings[0].value == Decimal("81.9")


def test_explicit_kg_still_wins_when_it_comes_first():
    detail = 'Mätdatum,Tid,Vikt (kg)\r\n25 nov. 2026,07:43,"81,9"\r\n'
    summary = 'Datum,Tid,Vikt (lbs)\r\n25 nov. 2026,07:43,"200,0"\r\n'

    readings = parse_text(detail + summary)

    assert len(readings) == 1
    assert readings[0].value == Decimal("81.9")


def test_duplicated_header_reads_the_first_populated_column():
    text = (
        "Mätdatum,Tid,Fettfri massa (kg),Fettfri massa (lbs),Fettfri massa (kg)\r\n"
        '25 nov. 2026,07:43,"64,8","142,8","99,9"\r\n'
    )

    assert values(text)["fat_free_mass"] == Decimal("64.8")


def test_duplicated_header_falls_back_to_the_second_column():
    text = (
        "Mätdatum,Tid,Fettfri massa (kg),Fettfri massa (lbs),Fettfri massa (kg)\r\n"
        '25 nov. 2026,07:43,-,-,"64,85"\r\n'
    )

    assert values(text)["fat_free_mass"] == Decimal("64.85")


def test_body_fat_and_skeletal_muscle_aliases():
    found = values(SUMMARY)

    assert found["body_fat_pct"] == Decimal("21.3")
    assert found["skeletal_muscle_pct"] == Decimal("31.2")


def test_pounds_only_row_converts_to_kg():
    text = 'Mätdatum,Tid,Vikt (kg),Vikt (lbs)\r\n25 nov. 2026,07:43,-,"200,0"\r\n'

    found = values(text)

    assert found == {"body_weight": Decimal("90.718")}


def test_kg_column_wins_when_both_present():
    text = 'Mätdatum,Tid,Vikt (kg),Vikt (lbs)\r\n25 nov. 2026,07:43,"82,4","200,0"\r\n'

    assert values(text) == {"body_weight": Decimal("82.4")}


def test_note_comes_from_anmarkningar():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Anmärkningar,Mätläge\r\n"
        "04 juli 2024,10:31,145,AFib,hemma\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == "AFib"


def test_note_falls_back_to_the_last_column():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Anmärkningar,Mätläge\r\n04 juli 2024,10:31,145,,AFib\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == "AFib"


def test_note_reads_matlage_by_name_when_it_precedes_puls():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Mätläge,Puls (slag/min)\r\n"
        "04 juli 2024,10:31,145,AFib,68\r\n"
    )

    readings = {reading.metric: reading for reading in parse_text(text)}

    assert readings["systolic_bp"].note == "AFib"
    assert readings["pulse"].value == Decimal("68")


def test_note_reads_older_malage_spelling():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Måläge,Puls (slag/min)\r\n"
        "04 juli 2024,10:31,145,hemma,68\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == "hemma"


def test_note_priority_ignores_where_the_columns_sit():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Puls (slag/min),Mätläge,Anmärkningar\r\n"
        "04 juli 2024,10:31,145,68,hemma,AFib\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == "AFib"


def test_note_priority_holds_when_anmarkningar_comes_first():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Anmärkningar,Mätläge,Puls (slag/min)\r\n"
        "04 juli 2024,10:31,145,AFib,hemma,68\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == "AFib"


def test_note_uses_matlage_when_it_is_the_only_column():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Puls (slag/min),Mätläge\r\n"
        "04 juli 2024,10:31,145,68,hemma\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == "hemma"


def test_a_date_header_without_a_measurement_column_raises():
    with pytest.raises(ValueError, match="report has header rows"):
        parse_text("Mätdatum,Tid,Unrelated\r\n")


def test_a_date_header_with_an_empty_measurement_column_is_an_empty_export():
    assert parse_text("Mätdatum,Tid,Vikt (kg)\r\n") == []


def test_no_note_column_stores_no_note():
    text = (
        "Datum,Tid,Systoliskt tryck (mmHg),Diastoliskt tryck (mmHg),Puls (slag/min)\r\n"
        "04 juli 2024,10:31,145,81,68\r\n"
    )

    systolic = next(reading for reading in parse_text(text) if reading.metric == "systolic_bp")

    assert systolic.note == ""


def test_note_is_only_set_on_the_systolic_component():
    notes = {
        reading.metric: reading.note
        for reading in parse_text(BLOOD_PRESSURE)
        if reading.at.day == 4
    }

    assert notes == {"systolic_bp": "AFib", "diastolic_bp": "", "pulse": ""}


def test_unknown_section_is_skipped_without_dropping_recognised_rows():
    unknown = "Datum,Tid,Okänd kolumn\r\n04 juli 2024,10:31,7\r\n"

    readings = parse_text(BLOOD_PRESSURE + unknown)

    assert {reading.metric for reading in readings} == {"systolic_bp", "diastolic_bp", "pulse"}
    assert len(readings) == 6


def test_all_unknown_sections_raise():
    text = "Datum,Tid,Okänd kolumn\r\n04 juli 2024,10:31,7\r\n"

    with pytest.raises(ValueError, match="no recognised section"):
        parse_text(text)


def test_unrelated_columns_raise_instead_of_yielding_a_silent_run():
    text = "alfa,beta,gamma\r\n1,2,3\r\n"

    with pytest.raises(ValueError, match="no recognised section"):
        parse_text(text)


def test_empty_and_blank_reports_return_no_readings():
    assert parse_text("") == []
    assert parse_text("\r\n \r\n\r\n") == []


def test_parse_file_propagates_unknown_sections(tmp_path):
    path = tmp_path / "unknown.csv"
    path.write_text("Datum,Tid,Okänd kolumn\r\n04 juli 2024,10:31,7\r\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no recognised section"):
        parse_file(path)


def test_empty_report_has_no_readings():
    assert parse_text("") == []


def test_header_only_report_has_no_readings():
    assert parse_text("Datum,Tid,Vikt (kg)\r\n") == []


def test_local_measure_date_does_not_shift_the_timestamp():
    reading = next(reading for reading in parse_text(DETAIL) if reading.metric == "bmi")

    assert reading.at == datetime(2026, 11, 25, 7, 43)


@pytest.mark.parametrize(
    "spelling",
    ["25 nov. 2026", "25 nov 2026", "25 november 2026", "25 Nov 2026", "25 November 2026"],
)
def test_month_names_normalise(spelling):
    text = f'Datum,Tid,Vikt (kg)\r\n{spelling},07:43,"82,4"\r\n'

    assert parse_text(text)[0].at == datetime(2026, 11, 25, 7, 43)


@pytest.mark.parametrize(
    "spelling", ["31 okt. 2024", "31 okt 2024", "31 oktober 2024", "31 Oct. 2024"]
)
def test_swedish_and_english_october(spelling):
    text = f'Datum,Tid,Vikt (kg)\r\n{spelling},07:43,"82,4"\r\n'

    assert parse_text(text)[0].at == datetime(2024, 10, 31, 7, 43)


def test_dst_wall_clock_is_kept_as_written():
    reading = parse_text('Datum,Tid,Vikt (kg)\r\n27 okt. 2024,02:30,"82,4"\r\n')[0]

    assert reading.at == datetime(2024, 10, 27, 2, 30)
    assert reading.at.tzinfo is None


def test_body_temperature_and_water_are_stored():
    water = 'Mätdatum,Tid,Kroppsvatten (%)\r\n25 nov. 2026,07:43,"55,4"\r\n'

    assert values(water) == {"body_water_pct": Decimal("55.4")}
    assert values(TEMPERATURE) == {"body_temperature": Decimal("36.5")}


def test_average_metrics_are_stored():
    found = values(DETAIL)

    assert found["average_weight"] == Decimal("82.3")
    assert found["average_fat_free_mass"] == Decimal("64.9")
