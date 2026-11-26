from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from parsing.report import Reading, parse_text
from records import importer
from records.importer import (
    BUSY_ATTEMPTS,
    CONFLICT,
    Rejected,
    import_readings,
    import_report,
    local_zone,
    retry_when_busy,
)
from records.management.commands.import_omron import stamp
from records.models import ImportRun, Measurement
from records.tokens import issue

HEADER = "Datum,Tid,Systoliskt tryck (mmHg),Diastoliskt tryck (mmHg),Puls (slag/min),Anmärkningar,Mätläge"
NONE = {"created": 0, "skipped": 0, "conflicts": 0, "overwritten": 0, "panels": 0, "invalid": 0}


def report(*rows: str) -> str:
    return "\n".join([HEADER, *rows, ""])


def window(*rows: str) -> list[Reading]:
    return parse_text(report(*rows))


def counts(**overrides: int) -> dict[str, int]:
    return NONE | overrides


REPORT = report("04 juli 2024,10:31,145,81,68,,AFib")


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(username="alice")


def test_import_creates_panel_and_components(owner):
    result = import_readings(owner, parse_text(REPORT))

    assert result == counts(created=3, panels=1)
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
    result = import_readings(owner, readings)

    assert result == counts(skipped=3)
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

    assert first == counts(created=12, panels=4)
    assert second == counts(created=6, skipped=3, panels=2)
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

    result = import_readings(owner, [Reading(at, "body_weight", Decimal("131.0"))])

    assert result == counts(skipped=1)
    assert Measurement.objects.get(owner=owner, metric="body_weight").value == Decimal("131")


def test_conflict_keeps_the_stored_value(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))

    result = import_readings(owner, window("10 juni 2024,08:00,999,85,70,,"))

    assert result == counts(skipped=2, conflicts=1)
    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == Decimal("130")


def test_conflict_detail_names_the_moment_metric_and_both_values(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))
    rejected: list[Rejected] = []

    import_readings(owner, window("10 juni 2024,08:00,999,85,70,,"), rejected=rejected)

    assert rejected == [
        Rejected(
            CONFLICT,
            datetime(2024, 6, 10, 8, 0, tzinfo=local_zone()),
            "systolic_bp",
            Decimal("130"),
            Decimal("999"),
        )
    ]


def test_a_conflicting_note_never_reaches_the_panel(owner):
    import_readings(owner, window("04 juli 2024,10:31,145,81,68,,"))
    panel = Measurement.objects.get(owner=owner, metric="blood_pressure")
    assert panel.note == ""

    result = import_readings(owner, window("04 juli 2024,10:31,999,81,68,,AFib"))

    assert result == counts(skipped=2, conflicts=1)
    panel.refresh_from_db()
    assert panel.note == ""


def test_overwrite_applies_the_conflicting_value(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))

    result = import_readings(owner, window("10 juni 2024,08:00,999,85,70,,"), overwrite=True)

    assert result == counts(skipped=2, overwritten=1)
    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == Decimal("999")


def test_duplicate_reading_in_one_call_keeps_the_first(owner):
    at = datetime(2024, 6, 10, 8, 0)

    result = import_readings(
        owner,
        [Reading(at, "pulse", Decimal("70")), Reading(at, "pulse", Decimal("90"))],
    )

    assert result == counts(created=1, skipped=1, panels=1)
    assert Measurement.objects.get(owner=owner, metric="pulse").value == Decimal("70")


def test_partial_reading_completed_later_keeps_one_panel(owner):
    first = import_readings(owner, window("10 juni 2024,08:00,130,85,,,"))
    second = import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))

    assert first == counts(created=2, panels=1)
    assert second == counts(created=1, skipped=2)
    panel = Measurement.objects.get(owner=owner, metric="blood_pressure")
    assert panel.components.count() == 3
    assert Measurement.objects.filter(owner=owner, parent__isnull=True).count() == 1


def test_panel_note_is_refreshed_when_a_component_carries_it(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))
    panel = Measurement.objects.get(owner=owner, metric="blood_pressure")
    assert panel.note == ""

    result = import_readings(owner, window("10 juni 2024,08:00,130,85,70,,AFib"))

    assert result == counts(skipped=3)
    panel.refresh_from_db()
    assert panel.note == "AFib"


def test_ambiguous_autumn_hour_still_deduplicates(owner):
    autumn = window("26 oktober 2025,02:30,130,85,70,,")

    first = import_readings(owner, autumn)
    second = import_readings(owner, autumn)

    assert first == counts(created=3, panels=1)
    assert second == counts(skipped=3)
    systolic = Measurement.objects.filter(owner=owner, metric="systolic_bp")
    assert systolic.count() == 1
    assert systolic.get().at.astimezone(UTC) == datetime(2025, 10, 26, 0, 30, tzinfo=UTC)
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 1


@pytest.mark.parametrize(
    "rows",
    [
        ("29 mars 2026,02:30,130,85,70,,", "29 mars 2026,03:30,131,86,71,,"),
        ("29 mars 2026,03:30,131,86,71,,", "29 mars 2026,02:30,130,85,70,,"),
    ],
)
def test_a_wall_clock_that_never_existed_never_displaces_a_real_reading(owner, rows):
    results = [import_readings(owner, window(row)) for row in rows]

    assert sum(result["invalid"] for result in results) == 1
    systolic = Measurement.objects.filter(owner=owner, metric="systolic_bp")
    assert systolic.count() == 1
    assert systolic.get().value == Decimal("131")
    assert systolic.get().at.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 1


@override_settings(OMRON_TIME_ZONE="UTC")
def test_the_configured_zone_decides_which_wall_clocks_exist(owner):
    result = import_readings(owner, window("29 mars 2026,02:30,130,85,70,,"))

    assert result == counts(created=3, panels=1)
    systolic = Measurement.objects.get(owner=owner, metric="systolic_bp")
    assert systolic.at.astimezone(UTC) == datetime(2026, 3, 29, 2, 30, tzinfo=UTC)


def test_command_reports_a_wall_clock_that_never_existed(owner, tmp_path):
    (tmp_path / "export.csv").write_text(report("29 mars 2026,02:30,130,85,70,,"), encoding="utf-8")

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    text = out.getvalue()
    assert (
        "export.csv: 3 readings, 0 created, 0 skipped, 0 conflicts, 0 overwritten, 0 new panels"
        in text
    )
    assert (
        "  2026-03-29 02:30 does not exist in Europe/Stockholm, so it was not imported; "
        "check OMRON_TIME_ZONE if the zone is wrong\n" in text
    )
    assert "total: 1 files, 3 readings, 0 created, 0 skipped, 0 conflicts, 0 overwritten" in text
    assert "1 invalid" in text
    assert not Measurement.objects.filter(owner=owner).exists()


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
    assert ImportRun.objects.filter(owner=owner).count() == 0
    assert (
        "export.csv: 3 readings, 3 created, 0 skipped, 0 conflicts, 0 overwritten, 1 new panels"
        in out.getvalue()
    )


def test_directory_dry_run_previews_exactly_what_the_import_then_does(owner, tmp_path):
    (tmp_path / "a.csv").write_text(
        report("03 juni 2024,08:00,130,85,70,,", "20 juni 2024,08:00,132,87,72,,"),
        encoding="utf-8",
    )
    (tmp_path / "b.csv").write_text(
        report("20 juni 2024,08:00,132,87,72,,", "25 juni 2024,08:00,134,89,74,,"),
        encoding="utf-8",
    )

    preview = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", "--dry-run", stdout=preview)
    real = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=real)

    assert preview.getvalue().splitlines() == real.getvalue().splitlines()
    assert (
        "b.csv: 6 readings, 3 created, 3 skipped, 0 conflicts, 0 overwritten, 1 new panels"
        in real.getvalue()
    )
    assert "total: 2 files, 12 readings, 9 created, 3 skipped, 0 conflicts, 0 overwritten" in (
        real.getvalue()
    )
    assert Measurement.objects.filter(owner=owner).count() == 12


def test_directory_dry_run_rolls_back_the_owner_and_every_file(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    (tmp_path / "b.csv").write_text(report("11 juni 2024,08:00,131,86,71,,"), encoding="utf-8")

    call_command(
        "import_omron",
        str(tmp_path),
        "--owner",
        "carol",
        "--create-user",
        "--dry-run",
        stdout=StringIO(),
    )

    assert not get_user_model().objects.filter(username="carol").exists()
    assert not ImportRun.objects.exists()
    assert not Measurement.objects.exists()


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

    response = Client().get(
        "/api/v1/measurements?level=all", headers={"authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    rows = response.json()
    assert rows["count"] == 4
    assert {row["metric"] for row in rows["items"]} == {
        "blood_pressure",
        "systolic_bp",
        "diastolic_bp",
        "pulse",
    }


def test_command_names_the_earlier_import_and_points_at_replay(owner, tmp_path):
    (tmp_path / "export.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())
    run = ImportRun.objects.get(owner=owner)

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    text = out.getvalue()
    assert f"export.csv: already imported at {stamp(run.imported_at)}" in text
    assert "pass --replay to import it again without overwriting" in text
    assert "0 readings, 0 created, 0 skipped, 0 conflicts, 0 overwritten, 0 new panels" in text
    assert "total: 1 files, 0 readings, 0 created" in text
    assert ImportRun.objects.filter(owner=owner).count() == 1
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_replay_imports_a_known_file_and_records_a_new_run(owner, tmp_path):
    (tmp_path / "export.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", "--replay", stdout=out)

    text = out.getvalue()
    assert "already imported" not in text
    assert (
        "export.csv: 3 readings, 0 created, 3 skipped, 0 conflicts, 0 overwritten, 0 new panels"
        in text
    )
    assert ImportRun.objects.filter(owner=owner).count() == 2
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_replay_restores_readings_a_purge_removed_from_an_overlapping_window(owner, tmp_path):
    (tmp_path / "early.csv").write_text(
        report("03 juni 2024,08:00,130,85,70,,", "20 juni 2024,08:00,132,87,72,,"),
        encoding="utf-8",
    )
    (tmp_path / "late.csv").write_text(
        report("20 juni 2024,08:00,132,87,72,,", "25 juni 2024,08:00,134,89,74,,"),
        encoding="utf-8",
    )
    call_command("import_omron", str(tmp_path / "early.csv"), "--owner", "alice", stdout=StringIO())
    call_command("import_omron", str(tmp_path / "late.csv"), "--owner", "alice", stdout=StringIO())
    early = ImportRun.objects.get(file_name="early.csv")

    call_command("purge_import", "--id", str(early.pk), "--owner", "alice", stdout=StringIO())
    assert Measurement.objects.filter(owner=owner, metric="systolic_bp").count() == 1

    out = StringIO()
    call_command(
        "import_omron", str(tmp_path / "late.csv"), "--owner", "alice", "--replay", stdout=out
    )

    assert "already imported" not in out.getvalue()
    assert Measurement.objects.filter(owner=owner, metric="systolic_bp").count() == 2
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 2
    assert (
        Measurement.objects.filter(owner=owner, parent__isnull=True)
        .exclude(metric="blood_pressure")
        .count()
        == 0
    )
    assert ImportRun.objects.filter(file_name="late.csv").count() == 2


def test_replay_never_overwrites_a_stored_value(owner, tmp_path):
    (tmp_path / "original.csv").write_text(
        report("04 juli 2024,10:31,145,81,68,,"), encoding="utf-8"
    )
    (tmp_path / "later.csv").write_text(report("04 juli 2024,10:31,999,81,68,,"), encoding="utf-8")
    call_command(
        "import_omron", str(tmp_path / "original.csv"), "--owner", "alice", stdout=StringIO()
    )
    call_command(
        "import_omron",
        str(tmp_path / "later.csv"),
        "--owner",
        "alice",
        "--overwrite",
        stdout=StringIO(),
    )

    out = StringIO()
    call_command(
        "import_omron", str(tmp_path / "original.csv"), "--owner", "alice", "--replay", stdout=out
    )

    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == Decimal("999")
    assert (
        "original.csv: 3 readings, 0 created, 2 skipped, 1 conflicts, 0 overwritten, 0 new panels"
        in (out.getvalue())
    )
    assert "  2024-07-04 10:31 systolic_bp: stored 999, file 145\n" in out.getvalue()
    assert ImportRun.objects.filter(owner=owner).count() == 3


def test_command_prints_one_indented_line_per_conflict(owner, tmp_path):
    (tmp_path / "first.csv").write_text(report("04 juli 2024,10:31,145,81,68,,"), encoding="utf-8")
    (tmp_path / "second.csv").write_text(report("04 juli 2024,10:31,999,81,68,,"), encoding="utf-8")
    call_command("import_omron", str(tmp_path / "first.csv"), "--owner", "alice", stdout=StringIO())

    out = StringIO()
    call_command("import_omron", str(tmp_path / "second.csv"), "--owner", "alice", stdout=out)

    text = out.getvalue()
    assert "  2024-07-04 10:31 systolic_bp: stored 145, file 999\n" in text
    assert (
        "second.csv: 3 readings, 0 created, 2 skipped, 1 conflicts, 0 overwritten, 0 new panels"
        in text
    )
    assert "total: 1 files, 3 readings, 0 created, 2 skipped, 1 conflicts, 0 overwritten" in text
    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == Decimal("145")


def test_command_overwrite_reimports_a_checksum_duplicate(owner, tmp_path):
    (tmp_path / "export.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", "--overwrite", stdout=out)

    text = out.getvalue()
    assert "already imported" not in text
    assert "export.csv: 3 readings, 0 created, 3 skipped" in text
    assert ImportRun.objects.filter(owner=owner).count() == 2
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_command_parse_failure_leaves_no_run_and_no_readings(owner, tmp_path):
    (tmp_path / "export.csv").write_bytes("not a report \u00e9".encode("latin-1"))

    with pytest.raises(UnicodeDecodeError):
        call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())

    assert ImportRun.objects.filter(owner=owner).count() == 0
    assert Measurement.objects.filter(owner=owner).count() == 0


def test_a_decode_failure_later_in_a_batch_still_reports_earlier_files(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("03 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    (tmp_path / "b.csv").write_bytes("not a report é".encode("latin-1"))
    out = StringIO()

    with pytest.raises(UnicodeDecodeError):
        call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    assert "a.csv: 3 readings, 3 created" in out.getvalue()
    assert "stopped at b.csv" in out.getvalue()
    assert (
        "1 file imported before the error; 0 files already imported; 0 files not attempted"
        in out.getvalue()
    )
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_a_failed_dry_run_says_nothing_was_written(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("03 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    (tmp_path / "b.csv").write_bytes("not a report é".encode("latin-1"))
    out = StringIO()

    with pytest.raises(UnicodeDecodeError) as raised:
        call_command("import_omron", str(tmp_path), "--owner", "alice", "--dry-run", stdout=out)

    assert "nothing was written" in out.getvalue()
    assert "was kept" not in str(raised.value)
    assert "was kept" not in out.getvalue()
    assert Measurement.objects.count() == 0
    assert ImportRun.objects.count() == 0


def test_a_checksum_skipped_file_is_not_counted_as_imported(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("03 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    (tmp_path / "b.csv").write_text(report("04 juni 2024,08:00,131,86,71,,"), encoding="utf-8")
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())

    out = StringIO()

    (tmp_path / "c.csv").write_bytes("not a report é".encode("latin-1"))
    with pytest.raises(UnicodeDecodeError):
        call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    assert "0 files imported before the error; 2 files already imported; 0 files not attempted" in (
        out.getvalue()
    )
    assert "imported before the error was kept" not in out.getvalue()


def test_a_programmatic_call_cannot_combine_replay_and_overwrite(owner, tmp_path):
    (tmp_path / "a.csv").write_text(report("03 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())
    (tmp_path / "a.csv").write_text(report("03 juni 2024,08:00,999,85,70,,"), encoding="utf-8")
    before = Measurement.objects.get(owner=owner, metric="systolic_bp").value

    with pytest.raises(CommandError):
        call_command("import_omron", str(tmp_path), "--owner", "alice", replay=True, overwrite=True)

    assert Measurement.objects.get(owner=owner, metric="systolic_bp").value == before
    assert ImportRun.objects.filter(owner=owner).count() == 1


def test_command_reports_an_unrecognised_export(owner, tmp_path):
    header = "Datum,Tid,Ok\u00e4nd kolumn"
    (tmp_path / "export.csv").write_text(f"{header}\r\n04 juli 2024,10:31,7\r\n", encoding="utf-8")

    with pytest.raises(CommandError, match="export.csv: report has header rows"):
        call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=StringIO())

    assert Measurement.objects.filter(owner=owner).count() == 0


def test_a_failed_file_keeps_the_files_already_imported(owner, tmp_path):
    (tmp_path / "first.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    header = "Datum,Tid,Ok\u00e4nd kolumn"
    (tmp_path / "second.csv").write_text(f"{header}\r\n04 juli 2024,10:31,7\r\n", encoding="utf-8")

    out = StringIO()
    with pytest.raises(CommandError) as failure:
        call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    text = out.getvalue()
    assert (
        "first.csv: 3 readings, 3 created, 0 skipped, 0 conflicts, 0 overwritten, 1 new panels"
        in text
    )
    assert "stopped at second.csv: report has header rows" in text
    assert (
        "1 file imported before the error; 0 files already imported; 0 files not attempted" in text
    )
    assert "second.csv: report has header rows" in str(failure.value)
    assert "1 file imported before the error was kept" in str(failure.value)
    assert Measurement.objects.filter(owner=owner, metric="systolic_bp").count() == 1


def test_replay_and_overwrite_are_refused_together(owner, tmp_path):
    (tmp_path / "export.csv").write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")

    with pytest.raises(CommandError, match="--overwrite"):
        call_command(
            "import_omron",
            str(tmp_path),
            "--owner",
            "alice",
            "--replay",
            "--overwrite",
            stdout=StringIO(),
        )

    assert not Measurement.objects.filter(owner=owner).exists()
    assert not ImportRun.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_an_import_takes_the_write_lock_before_it_reads_the_ledger_checksum(tmp_path):
    owner = get_user_model().objects.create_user(username="alice")
    path = tmp_path / "export.csv"
    path.write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")

    with CaptureQueriesContext(connection) as captured:
        import_report(owner, path)

    statements = [query["sql"].lstrip().upper() for query in captured.captured_queries]
    checksum_read = next(
        index for index, sql in enumerate(statements) if "CONTENT_SHA256" in sql and "SELECT" in sql
    )
    assert statements[0] == "BEGIN IMMEDIATE"
    assert checksum_read > 0
    assert not any(sql == "BEGIN" for sql in statements)


@pytest.mark.parametrize(
    "message",
    [
        "database is locked",
        "database table is locked",
        "database is locked: SQLITE_BUSY_SNAPSHOT",
    ],
)
def test_a_busy_database_message_is_retried_instead_of_failing(message):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError(message)
        return "imported"

    assert retry_when_busy(flaky, backoff=0) == "imported"
    assert len(attempts) == 2


def test_a_busy_database_that_never_clears_gives_up_after_a_few_attempts():
    attempts = []

    def always_busy():
        attempts.append(1)
        raise OperationalError("database is locked")

    with pytest.raises(OperationalError, match="database is locked"):
        retry_when_busy(always_busy, backoff=0)

    assert len(attempts) == BUSY_ATTEMPTS


def test_a_broken_statement_is_not_retried():
    attempts = []

    def broken():
        attempts.append(1)
        raise OperationalError("no such table: records_measurement")

    with pytest.raises(OperationalError, match="no such table"):
        retry_when_busy(broken, backoff=0)

    assert len(attempts) == 1


def test_a_programming_error_is_not_retried():
    attempts = []

    def rejects():
        attempts.append(1)
        raise ValueError("invalid staging target")

    with pytest.raises(ValueError, match="invalid staging target"):
        retry_when_busy(rejects, backoff=0)

    assert len(attempts) == 1


def test_an_import_interrupted_by_a_busy_database_is_retried_without_a_duplicate_run(
    owner, tmp_path, monkeypatch
):
    path = tmp_path / "export.csv"
    path.write_text(report("10 juni 2024,08:00,130,85,70,,"), encoding="utf-8")
    stored = importer._stored
    lookups = []

    def busy_after_the_first_write(*args, **kwargs):
        lookups.append(1)
        if len(lookups) == 2:
            raise OperationalError("database is locked")
        return stored(*args, **kwargs)

    monkeypatch.setattr(importer, "_stored", busy_after_the_first_write)

    result = import_report(owner, path)

    assert result["created"] == 3
    assert len(lookups) == 4
    assert ImportRun.objects.filter(owner=owner).count() == 1
    assert Measurement.objects.filter(owner=owner).count() == 4
