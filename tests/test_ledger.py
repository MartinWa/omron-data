from datetime import datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from parsing.report import Reading
from records.importer import import_readings, import_report, local_zone
from records.management.commands.import_omron import stamp
from records.models import ImportRun, Measurement
from tests.test_import import report, window


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(username="alice")


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_successful_run_records_counts_and_span(owner, tmp_path):
    path = write(
        tmp_path / "export.csv",
        report(
            "10 juni 2024,08:00,130,85,70,,",
            "12 juni 2024,09:30,131,86,71,,",
        ),
    )

    result = import_report(owner, path)

    run = ImportRun.objects.get(owner=owner)
    assert result["run"] == run
    assert run.file_name == "export.csv"
    assert len(run.content_sha256) == 64
    assert (run.readings, run.created, run.skipped) == (6, 6, 0)
    assert (run.conflicts, run.overwritten, run.panels) == (0, 0, 2)
    local = local_zone()
    assert run.first_at.astimezone(local) == datetime(2024, 6, 10, 8, 0, tzinfo=local)
    assert run.last_at.astimezone(local) == datetime(2024, 6, 12, 9, 30, tzinfo=local)
    assert Measurement.objects.filter(owner=owner, import_run=run).count() == 8


def test_empty_file_records_a_run_without_a_span(owner, tmp_path):
    path = write(tmp_path / "empty.csv", "")

    result = import_report(owner, path)

    run = result["run"]
    assert (run.readings, run.created) == (0, 0)
    assert (run.first_at, run.last_at) == (None, None)


def test_identical_file_is_short_circuited(owner, tmp_path):
    path = write(tmp_path / "export.csv", report("10 juni 2024,08:00,130,85,70,,"))
    first = import_report(owner, path)

    second = import_report(owner, path)

    assert second["already_imported"] == first["run"].imported_at
    assert second["readings"] == 0
    assert second["run"] is None
    assert second["created"] == second["skipped"] == second["panels"] == 0
    assert ImportRun.objects.filter(owner=owner).count() == 1
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_overwrite_records_a_second_run(owner, tmp_path):
    path = write(tmp_path / "export.csv", report("10 juni 2024,08:00,130,85,70,,"))
    import_report(owner, path)

    result = import_report(owner, path, overwrite=True)

    assert result["already_imported"] is None
    assert result["created"] == 0
    assert result["skipped"] == 3
    assert result["panels"] == 0
    assert ImportRun.objects.filter(owner=owner).count() == 2
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_replay_records_a_second_run_without_overwriting(owner, tmp_path):
    path = write(tmp_path / "export.csv", report("10 juni 2024,08:00,130,85,70,,"))
    import_report(owner, path)

    result = import_report(owner, path, replay=True)

    assert result["already_imported"] is None
    assert (result["created"], result["skipped"], result["panels"]) == (0, 3, 0)
    assert result["run"] == ImportRun.objects.filter(owner=owner).latest("imported_at")
    assert ImportRun.objects.filter(owner=owner).count() == 2
    assert Measurement.objects.filter(owner=owner).count() == 4


def test_import_readings_stamps_only_the_rows_it_creates(owner):
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"))
    run = ImportRun.objects.create(owner=owner, file_name="later.csv", content_sha256="0" * 64)

    import_readings(
        owner, window("10 juni 2024,08:00,130,85,70,,", "11 juni 2024,08:00,130,85,70,,"), run=run
    )

    assert Measurement.objects.filter(owner=owner, import_run__isnull=True).count() == 4
    assert set(Measurement.objects.filter(import_run=run).values_list("metric", flat=True)) == {
        "systolic_bp",
        "diastolic_bp",
        "pulse",
        "blood_pressure",
    }


def test_purge_removes_only_that_runs_rows(owner):
    early = window(
        "03 juni 2024,08:00,130,85,70,,",
        "20 juni 2024,08:00,132,87,72,,",
    )
    late = window(
        "20 juni 2024,08:00,132,87,72,,",
        "25 juni 2024,08:00,134,89,74,,",
    )
    first = ImportRun.objects.create(owner=owner, file_name="early.csv", content_sha256="1" * 64)
    second = ImportRun.objects.create(owner=owner, file_name="late.csv", content_sha256="2" * 64)
    import_readings(owner, early, run=first)
    import_readings(owner, late, run=second)
    kept = set(Measurement.objects.filter(import_run=second).values_list("pk", flat=True))
    assert len(kept) == 4

    out = StringIO()
    call_command("purge_import", "--id", str(first.pk), "--owner", "alice", stdout=out)

    assert f"run {first.pk} (early.csv): deleted 8 measurements" in out.getvalue()
    assert not ImportRun.objects.filter(pk=first.pk).exists()
    assert set(Measurement.objects.filter(owner=owner).values_list("pk", flat=True)) == kept
    assert Measurement.objects.filter(owner=owner, metric="blood_pressure").count() == 1


def test_purge_keeps_a_later_runs_component_of_an_earlier_panel(owner):
    first = ImportRun.objects.create(owner=owner, file_name="partial.csv", content_sha256="1" * 64)
    second = ImportRun.objects.create(
        owner=owner, file_name="complete.csv", content_sha256="2" * 64
    )
    import_readings(owner, window("10 juni 2024,08:00,130,85,,,"), run=first)
    import_readings(owner, window("10 juni 2024,08:00,130,85,70,,"), run=second)

    call_command("purge_import", "--id", str(first.pk), "--owner", "alice", stdout=StringIO())

    pulse = Measurement.objects.get(import_run=second, metric="pulse")
    assert pulse.parent_id is None
    assert Measurement.objects.filter(owner=owner).count() == 1


def test_purge_dry_run_removes_nothing(owner):
    run = ImportRun.objects.create(owner=owner, file_name="early.csv", content_sha256="1" * 64)
    import_readings(owner, window("03 juni 2024,08:00,130,85,70,,"), run=run)
    out = StringIO()

    call_command("purge_import", "--id", str(run.pk), "--owner", "alice", "--dry-run", stdout=out)

    assert "would delete 4 measurements" in out.getvalue()
    assert Measurement.objects.filter(owner=owner).count() == 4
    assert ImportRun.objects.filter(pk=run.pk).exists()


def test_purge_refuses_an_unknown_run(owner):
    with pytest.raises(CommandError, match="unknown import run: 999"):
        call_command("purge_import", "--id", "999", "--owner", "alice", stdout=StringIO())


def test_purge_refuses_another_owners_run(owner):
    get_user_model().objects.create_user(username="bob")
    run = ImportRun.objects.create(owner=owner, file_name="early.csv", content_sha256="1" * 64)

    with pytest.raises(CommandError, match="belongs to alice, not bob"):
        call_command("purge_import", "--id", str(run.pk), "--owner", "bob", stdout=StringIO())

    assert ImportRun.objects.filter(pk=run.pk).exists()


def test_coverage_reports_a_gap_longer_than_a_week(owner):
    import_readings(
        owner,
        [
            Reading(datetime(2024, 6, 10, 8, 0), "body_weight", Decimal("137.25")),
            Reading(datetime(2024, 6, 11, 8, 0), "body_weight", Decimal("138.5")),
            Reading(datetime(2024, 6, 30, 8, 0), "body_weight", Decimal("139.75")),
        ],
    )
    out = StringIO()

    call_command("coverage", "--owner", "alice", stdout=out)

    text = out.getvalue()
    assert "body_weight" in text
    assert "2024-06-10 08:00  2024-06-30 08:00" in text
    assert "gap 2024-06-11 08:00 -> 2024-06-30 08:00 (19d 0h)" in text
    assert text.count("  gap ") == 1
    assert "137" not in text
    assert "139" not in text


def test_coverage_finds_no_gap_between_consecutive_days(owner):
    import_readings(
        owner,
        [
            Reading(datetime(2024, 6, 10, 8, 0), "body_weight", Decimal("70")),
            Reading(datetime(2024, 6, 11, 8, 0), "body_weight", Decimal("71")),
        ],
    )
    out = StringIO()

    call_command("coverage", "--owner", "alice", stdout=out)

    text = out.getvalue()
    assert "  gap " not in text
    assert "body_weight" in text
    assert "2024-06-10 08:00" in text
    assert "2024-06-11 08:00" in text


def test_coverage_refuses_an_unknown_owner(db):
    with pytest.raises(CommandError, match="unknown owner: nobody"):
        call_command("coverage", "--owner", "nobody", stdout=StringIO())


def test_stamp_shows_local_time():
    assert stamp(datetime(2024, 6, 10, 8, 0, tzinfo=local_zone())) == "2024-06-10 08:00"
