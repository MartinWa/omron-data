from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from parsing.report import Reading, parse_text
from records.importer import import_readings
from records.models import Measurement
from records.tokens import issue

HEADER = (
    "Datum,Tid,Systoliskt tryck (mmHg),Diastoliskt tryck (mmHg),Puls (slag/min),Anmärkningar,Måläge"
)


def report(*rows: str) -> str:
    return "\n".join([HEADER, *rows, ""])


def window(*rows: str) -> list[Reading]:
    return parse_text(report(*rows))


REPORT = report("04 juli 2024,10:31,145,81,68,,AFib")


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(username="alice")


def test_import_creates_panel_and_components(owner):
    counts = import_readings(owner, parse_text(REPORT))

    assert counts == {"created": 3, "skipped": 0, "conflicts": 0, "panels": 1, "overwritten": 0}
    panel = Measurement.objects.get(owner=owner, metric="blood_pressure")
    assert panel.note == "AFib"
    assert panel.components.count() == 3
    assert set(panel.components.values_list("metric", flat=True)) == {
        "systolic_bp",
        "diastolic_bp",
        "pulse",
    }


def test_import_is_idempotent(owner):
    readings = parse_text(REPORT)
    import_readings(owner, readings)
    counts = import_readings(owner, readings)

    assert counts == {"created": 0, "skipped": 3, "conflicts": 0, "panels": 0, "overwritten": 0}
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_identical_reimport_writes_nothing(owner):
    readings = parse_text(REPORT)
    import_readings(owner, readings)

    with CaptureQueriesContext(connection) as captured:
        import_readings(owner, readings)

    writes = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes == []


def test_sliding_window_adds_only_the_new_readings(owner):
    early = window(
        "03 juni 2024,08:00,130,85,70,,",
        "10 juni 2024,08:00,131,86,71,,",
        "20 juni 2024,08:00,132,87,72,,",
        "26 juni 2024,08:00,133,88,73,,",
    )
    late = window(
        "20 juni 2024,08:00,132,87,72,,",
        "25 juni 2024,08:00,134,89,74,,",
        "01 juli 2024,08:00,135,90,75,,",
    )

    first = import_readings(owner, early)
    second = import_readings(owner, late)

    assert first == {"created": 12, "skipped": 0, "conflicts": 0, "panels": 4, "overwritten": 0}
    assert second == {"created": 6, "skipped": 3, "conflicts": 0, "panels": 2, "overwritten": 0}
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 6
    assert Measurement.objects.filter(owner=owner).count() == 24
    assert (
        Measurement.objects.filter(owner=owner, parent__isnull=True)
        .exclude(metric="blood_pressure")
        .count()
        == 0
    )


def test_numerically_equal_value_is_skipped_regardless_of_formatting(owner):
    at = datetime(2024, 6, 10, 8, 0)
    import_readings(owner, [Reading(at, "body_weight", Decimal("131"))])

    counts = import_readings(owner, [Reading(at, "body_weight", Decimal("131.0"))])

    assert counts == {"created": 0, "skipped": 1, "conflicts": 0, "panels": 0, "overwritten": 0}
    assert Measurement.objects.get(owner=owner, metric="body_weight").value == Decimal("131")


def test_conflict_keeps_the_stored_value(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))

    counts = import_readings(owner, window("10 juni 2024,08:00,999,85,70,,"))

    assert counts == {"created": 0, "skipped": 2, "conflicts": 1, "panels": 0, "overwritten": 0}
    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == Decimal("130")


def test_overwrite_applies_the_conflicting_value(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))

    counts = import_readings(owner, window("10 juni 2024,08:00,999,85,70,,"), overwrite=True)

    assert counts == {"created": 0, "skipped": 2, "conflicts": 0, "panels": 0, "overwritten": 1}
    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == Decimal("999")


def test_duplicate_reading_in_one_call_keeps_the_first(owner):
    at = datetime(2024, 6, 10, 8, 0)

    counts = import_readings(
        owner,
        [Reading(at, "pulse", Decimal("70")), Reading(at, "pulse", Decimal("90"))],
    )

    assert counts == {"created": 1, "skipped": 1, "conflicts": 0, "panels": 1, "overwritten": 0}
    assert Measurement.objects.get(owner=owner, metric="pulse").value == Decimal("70")


def test_partial_reading_completed_later_keeps_one_panel(owner):
    first = import_readings(owner, window("10 juni 2024,08:00,130,85,,,"))
    second = import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))

    assert first == {"created": 2, "skipped": 0, "conflicts": 0, "panels": 1, "overwritten": 0}
    assert second == {"created": 1, "skipped": 2, "conflicts": 0, "panels": 0, "overwritten": 0}
    panel = Measurement.objects.get(owner=owner, metric="blood_pressure")
    assert panel.components.count() == 3
    assert Measurement.objects.filter(owner=owner, parent__isnull=True).count() == 1


def test_panel_note_is_refreshed_when_a_component_carries_it(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))
    panel = Measurement.objects.get(owner=owner, metric="blood_pressure")
    assert panel.note == ""

    counts = import_readings(owner, window("10 juni 2024,08:00,130,85,70,,AFib"))

    assert counts["skipped"] == 3
    panel.refresh_from_db()
    assert panel.note == "AFib"


def test_dst_fold_times_deduplicate(owner):
    autumn = window("26 oktober 2025,02:30,130,85,70,,")
    spring = window("30 mars 2025,02:30,130,85,70,,")

    import_readings(owner, autumn)
    import_readings(owner, spring)
    counts = import_readings(owner, autumn + spring)

    assert counts == {"created": 0, "skipped": 6, "conflicts": 0, "panels": 0, "overwritten": 0}
    systolic = Measurement.objects.filter(owner=owner, metric="systolic_bp")
    assert systolic.count() == 2
    assert {row.at.astimezone(UTC) for row in systolic} == {
        datetime(2025, 10, 26, 0, 30, tzinfo=UTC),
        datetime(2025, 3, 30, 1, 30, tzinfo=UTC),
    }
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 2


def test_command_imports_every_csv_in_a_directory(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    (tmp_path / "b.csv").write_text(report("11 juni 2024,08:00,131,86,71,,"), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    assert Measurement.objects.filter(owner=owner, metric="systolic_bp").count() == 2
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 2
    text = out.getvalue()
    assert (
        "a.csv: 3 readings, 3 created, 0 skipped, 0 conflicts, 0 overwritten, 1 new panels" in text
    )
    assert (
        "b.csv: 3 readings, 3 created, 0 skipped, 0 conflicts, 0 overwritten, 1 new panels" in text
    )
    assert "total: 2 files, 6 readings, 6 created" in text


def test_command_accepts_a_glob(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    (tmp_path / "b.csv").write_text(report("11 juni 2024,08:00,131,86,71,,"), encoding="utf-8")

    call_command("import_omron", str(tmp_path / "*.csv"), "--owner", "alice", stdout=StringIO())

    assert Measurement.objects.filter(owner=owner, metric="systolic_bp").count() == 2


def test_command_dry_run_writes_nothing(owner, tmp_path):
    (tmp_path / "export.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", "--dry-run", stdout=out)

    assert Measurement.objects.filter(owner=owner).count() == 0
    assert (
        "export.csv: 3 readings, 3 created, 0 skipped, 0 conflicts, 0 overwritten, 1 new panels"
        in out.getvalue()
    )


def test_owners_are_separate(owner):
    other = get_user_model().objects.create_user(username="bob")
    import_readings(owner, parse_text(REPORT))
    import_readings(other, parse_text(REPORT))

    assert Measurement.objects.filter(owner=owner).count() == 4
    assert Measurement.objects.filter(owner=other).count() == 4


def test_api_requires_a_token(db):
    response = Client().get("/api/v1/measurements")
    assert response.status_code == 401


def test_api_returns_only_owned_rows(owner):
    other = get_user_model().objects.create_user(username="bob")
    import_readings(owner, parse_text(REPORT))
    import_readings(other, parse_text(REPORT))
    token = issue(owner, "test")

    response = Client().get("/api/v1/measurements", headers={"authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = response.json()
    assert rows["count"] == 4
    assert {row["metric"] for row in rows["items"]} == {
        "blood_pressure",
        "systolic_bp",
        "diastolic_bp",
        "pulse",
    }
