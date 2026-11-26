import json
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from parsing.report import Reading
from records import pending
from records.importer import import_readings, import_report
from records.limits import MAX_UPLOAD_BYTES, MAX_UPLOAD_MEGABYTES, MULTIPART_OVERHEAD_BYTES
from records.models import ImportRun, Measurement
from records.views import (
    NO_ROWS_MESSAGE,
    NOT_UTF8_MESSAGE,
    PREVIEW_EXPIRED_MESSAGE,
    PREVIEW_MISSING_MESSAGE,
    PREVIEW_STALE_MESSAGE,
    PURGE_UNCONFIRMED_MESSAGE,
    RUN_NOT_FOUND_MESSAGE,
    TOO_LARGE_MESSAGE,
)
from tests.test_import import report, window

FRESH = report(
    "02 september 2024,08:00,120,80,60,,",
    "03 september 2024,08:00,121,81,61,,",
)
CLASH = report("03 juni 2024,08:00,140,90,75,,")


@pytest.fixture(autouse=True)
def pending_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OMRON_PENDING_DIR", str(tmp_path / "pending"))


def csv_file(text: str, name: str = "export.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")


def upload(client, text: str = FRESH, name: str = "export.csv"):
    return client.post(reverse("import_upload"), {"csv_file": csv_file(text, name)})


def staged(tmp_path) -> list[Path]:
    root = tmp_path / "pending" / "omron-data-pending"
    return sorted(root.iterdir()) if root.exists() else []


def confirm(client, **extra):
    stored = client.session["pending_import"]
    return client.post(
        reverse("import_preview"),
        {
            "import_id": stored["id"],
            "fingerprint": stored["fingerprint"],
            "action": "confirm",
            **extra,
        },
    )


def zone() -> ZoneInfo:
    return ZoneInfo(settings.OMRON_TIME_ZONE)


def stored_value(owner, metric: str, moment: datetime):
    return Measurement.objects.get(owner=owner, metric=metric, at=moment).value


@pytest.mark.django_db
def test_the_coverage_table_counts_every_gap_longer_than_a_week(client, db):
    owner = get_user_model().objects.create_user(username="coverage")
    import_readings(owner, window("02 januar 2024,08:00,120,80,60,,"))
    import_readings(
        owner,
        [
            Reading(datetime(2024, 1, 1, 8, 0), "body_weight", Decimal("70")),
            Reading(datetime(2024, 1, 2, 8, 0), "body_weight", Decimal("71")),
            Reading(datetime(2024, 1, 30, 8, 0), "body_weight", Decimal("72")),
            Reading(datetime(2024, 1, 31, 8, 0), "body_weight", Decimal("73")),
            Reading(datetime(2024, 3, 15, 8, 0), "body_weight", Decimal("74")),
        ],
    )
    ImportRun.objects.create(
        owner=owner, file_name="coverage.csv", content_sha256="c" * 64, readings=8
    )
    client.force_login(owner)

    response = client.get(reverse("imports"))

    coverage = response.context["coverage"]
    assert coverage["gaps"] == 2
    assert coverage["readings"] == 8
    assert coverage["events"] == 5
    assert [row["metric"] for row in coverage["metrics"]] == [
        "blood_pressure",
        "body_weight",
        "diastolic_bp",
        "pulse",
        "systolic_bp",
    ]
    weight = coverage["metrics"][1]
    assert weight["count"] == 5
    assert weight["first"] == datetime(2024, 1, 1, 8, 0, tzinfo=zone())
    assert weight["last"] == datetime(2024, 3, 15, 8, 0, tzinfo=zone())
    assert [gap["span"] for gap in weight["gaps"]] == ["28d 0h", "44d 0h"]
    assert [gap["before"] for gap in weight["gaps"]] == [
        datetime(2024, 1, 2, 8, 0, tzinfo=zone()),
        datetime(2024, 1, 31, 8, 0, tzinfo=zone()),
    ]
    body = response.content.decode()
    assert "2 gaps" in body
    assert "2024-01-02 08:00 → 2024-01-30 08:00 (28d 0h)" in body


@pytest.mark.django_db
def test_the_coverage_table_counts_no_gap_for_readings_within_a_week(client, db):
    owner = get_user_model().objects.create_user(username="dense")
    import_readings(
        owner,
        [
            Reading(datetime(2024, 1, day, 8, 0), "body_weight", Decimal("70") + day)
            for day in (1, 2, 3, 10, 11, 18)
        ],
    )
    client.force_login(owner)

    coverage = client.get(reverse("imports")).context["coverage"]

    assert coverage["gaps"] == 0
    assert coverage["metrics"][0]["gaps"] == []


@pytest.mark.django_db
def test_a_long_export_keeps_only_the_newest_five_hundred_readings(client, alice):
    client.force_login(alice)
    start = datetime(2024, 1, 1, 0, 0)
    lines = [
        f"{(start + timedelta(hours=index)).day:02d} januari 2024,"
        f"{start + timedelta(hours=index):%H:%M},120,80,60,,"
        for index in range(400)
    ]
    upload(client, report(*lines))

    import_id = client.session["pending_import"]["id"]
    summary = pending.load_summary(import_id)
    content = client.get(reverse("import_preview")).content.decode()

    assert summary["readings_total"] == 1200
    assert len(summary["readings"]) == 500
    assert summary["readings"][0]["at"] == "2024-01-17 15:00"
    assert summary["readings"][-1]["at"] == "2024-01-10 17:00"
    assert "Showing the first 10 of 1200 readings." in content
    assert "<summary>Show all 1200 readings</summary>" in content
    assert content.count('<td class="muted nowrap">') == 500


@pytest.mark.django_db
def test_a_capped_summary_stays_far_below_the_rendered_page_size(client, alice, tmp_path):
    client.force_login(alice)
    start = datetime(2024, 1, 1, 0, 0)
    lines = [
        f"{(start + timedelta(hours=index)).day:02d} januari 2024,"
        f"{start + timedelta(hours=index):%H:%M},120,80,60,,"
        for index in range(400)
    ]
    upload(client, report(*lines))
    import_id = client.session["pending_import"]["id"]
    stored = pending.pending_root() / import_id / "summary.json"

    summary = json.loads(stored.read_text(encoding="utf-8"))

    assert stored.stat().st_size < 60000
    assert len(summary["readings"]) == 500


@pytest.mark.django_db
def test_the_import_pages_send_anonymous_visitors_to_the_login_page(client):
    for name in ("imports", "import_upload", "import_preview"):
        response = client.get(reverse(name))

        assert response.status_code == 302
        assert reverse("login") in response.url


@pytest.mark.django_db
def test_the_upload_page_states_the_size_limit_and_offers_a_submit_without_javascript(
    client, alice
):
    client.force_login(alice)

    content = client.get(reverse("import_upload")).content.decode()

    assert f"Up to {MAX_UPLOAD_MEGABYTES} MB" in content
    assert 'accept=".csv,text/csv"' in content
    assert "<noscript>" in content
    assert 'type="submit">Preview<' in content
    assert "records/upload.js" in content


@pytest.mark.django_db
def test_an_oversized_request_body_is_refused_before_the_upload_is_read(client, alice, tmp_path):
    client.force_login(alice)
    before = ImportRun.objects.filter(owner=alice).count()

    response = client.post(
        reverse("import_upload"),
        {"csv_file": csv_file(FRESH)},
        CONTENT_LENGTH=str(MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES + 1),
    )

    assert response.status_code == 200
    assert TOO_LARGE_MESSAGE in response.content.decode()
    assert "pending_import" not in client.session
    assert ImportRun.objects.filter(owner=alice).count() == before
    assert staged(tmp_path) == []


@pytest.mark.django_db
def test_a_file_that_names_no_omron_section_reports_the_parser_message(client, alice, tmp_path):
    client.force_login(alice)
    before = ImportRun.objects.filter(owner=alice).count()

    response = client.post(
        reverse("import_upload"),
        {"csv_file": csv_file("Datum,Tid\n2024-06-03,08:00\n")},
    )

    assert response.status_code == 200
    assert "no recognised section" in response.content.decode()
    assert ImportRun.objects.filter(owner=alice).count() == before
    assert staged(tmp_path) == []
    assert "pending_import" not in client.session


@pytest.mark.django_db
def test_a_file_that_is_not_utf8_is_reported_as_text_the_parser_cannot_read(
    client, alice, tmp_path
):
    client.force_login(alice)

    response = client.post(
        reverse("import_upload"),
        {
            "csv_file": SimpleUploadedFile(
                "export.csv", b"\xff\xfe\x00Datum", content_type="text/csv"
            )
        },
    )

    assert response.status_code == 200
    assert NOT_UTF8_MESSAGE in response.content.decode()
    assert staged(tmp_path) == []
    assert "pending_import" not in client.session


@pytest.mark.django_db
def test_an_export_with_no_data_rows_is_refused_before_anything_is_staged(client, alice, tmp_path):
    client.force_login(alice)
    before = ImportRun.objects.filter(owner=alice).count()

    response = client.post(reverse("import_upload"), {"csv_file": csv_file(report())})

    assert response.status_code == 200
    assert NO_ROWS_MESSAGE in response.content.decode()
    assert ImportRun.objects.filter(owner=alice).count() == before
    assert staged(tmp_path) == []
    assert "pending_import" not in client.session


@pytest.mark.django_db
def test_a_valid_export_is_staged_outside_the_repository_and_redirects_to_the_preview(
    client, alice, tmp_path
):
    client.force_login(alice)
    before_rows = Measurement.objects.filter(owner=alice).count()
    before_runs = ImportRun.objects.filter(owner=alice).count()

    response = upload(client, FRESH, name="../../nested/export.csv")

    assert response.status_code == 302
    assert response.url == reverse("import_preview")
    stored = client.session["pending_import"]
    folder = tmp_path / "pending" / "omron-data-pending" / stored["id"]
    assert folder.is_dir()
    assert sorted(child.name for child in folder.iterdir()) == [
        ".omron-data-pending",
        "export.csv",
        "summary.json",
    ]
    assert stored["fingerprint"] == sha256(FRESH.encode()).hexdigest()
    assert stored["created_at"] <= time.time()
    assert str(settings.BASE_DIR) not in str(folder)
    assert pending.pending_root() == tmp_path / "pending" / "omron-data-pending"
    assert Measurement.objects.filter(owner=alice).count() == before_rows
    assert ImportRun.objects.filter(owner=alice).count() == before_runs


def test_the_staging_area_is_an_application_subdirectory_of_the_configured_base(
    tmp_path, monkeypatch
):
    base = tmp_path / "data"
    monkeypatch.setenv("OMRON_PENDING_DIR", str(base))

    import_id = pending.new_import_id()
    pending.save(import_id, "export.csv", b"Datum\n")

    root = base / "omron-data-pending"
    assert pending.pending_root() == root
    assert (root / import_id / "export.csv").is_file()
    assert (root / import_id / ".omron-data-pending").is_file()


def test_purge_stale_leaves_a_foreign_database_and_directory_in_the_configured_base(
    tmp_path, monkeypatch
):
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setenv("OMRON_PENDING_DIR", str(base))
    database = base / "omron.sqlite3"
    database.write_bytes(b"SQLite format 3\0")
    backups = base / "backups"
    backups.mkdir()
    (backups / "old.csv").write_text("Datum\n", encoding="utf-8")
    old = time.time() - pending.PENDING_TTL_SECONDS - 60
    os.utime(database, (old, old))
    os.utime(backups, (old, old))

    removed = pending.purge_stale()

    assert removed == 0
    assert database.read_bytes() == b"SQLite format 3\0"
    assert (backups / "old.csv").is_file()


def test_purge_stale_removes_a_staged_import_older_than_the_ttl(tmp_path):
    import_id = pending.new_import_id()
    path = pending.save(import_id, "export.csv", b"Datum\n")
    old = time.time() - pending.PENDING_TTL_SECONDS - 60
    os.utime(path.parent, (old, old))

    removed = pending.purge_stale()

    assert removed == 1
    assert not (pending.pending_root() / import_id).exists()


def test_purge_stale_keeps_a_fresh_staged_import():
    import_id = pending.new_import_id()
    pending.save(import_id, "export.csv", b"Datum\n")

    removed = pending.purge_stale()

    assert removed == 0
    assert (pending.pending_root() / import_id / "export.csv").is_file()


def test_purge_stale_keeps_a_pattern_shaped_directory_the_application_did_not_create():
    root = pending.pending_root()
    root.mkdir(parents=True, exist_ok=True)
    impostor = root / ("a" * 22)
    impostor.mkdir()
    (impostor / "export.csv").write_text("Datum\n", encoding="utf-8")
    old = time.time() - pending.PENDING_TTL_SECONDS - 60
    os.utime(impostor, (old, old))

    removed = pending.purge_stale()

    assert removed == 0
    assert (impostor / "export.csv").is_file()


def test_cancel_removes_a_staged_import():
    import_id = pending.new_import_id()
    pending.save(import_id, "export.csv", b"Datum\n")

    pending.discard(import_id)

    assert not (pending.pending_root() / import_id).exists()


def test_cancel_keeps_a_pattern_shaped_directory_the_application_did_not_create():
    root = pending.pending_root()
    root.mkdir(parents=True, exist_ok=True)
    impostor = root / pending.new_import_id()
    impostor.mkdir()
    (impostor / "export.csv").write_text("Datum\n", encoding="utf-8")

    pending.discard(impostor.name)

    assert (impostor / "export.csv").is_file()


@pytest.mark.django_db
def test_the_preview_shows_the_counts_the_import_would_write(client, alice):
    client.force_login(alice)
    upload(client)

    response = client.get(reverse("import_preview"))

    summary = response.context["summary"]
    assert summary["readings_total"] == 6
    assert summary["counts"] == {
        "created": 6,
        "skipped": 0,
        "conflicts": 0,
        "overwritten": 0,
        "panels": 2,
        "invalid": 0,
    }
    assert summary["conflict_total"] == 0
    assert summary["replay"] is False
    content = response.content.decode()
    assert 'name="replay"' not in content
    assert "<strong>Confirming stores 6 new readings and 2 new panels.</strong>" in content


@pytest.mark.django_db
def test_the_preview_get_writes_nothing(client, alice, tmp_path):
    client.force_login(alice)
    upload(client)
    stored = client.session["pending_import"]
    folder = tmp_path / "pending" / "omron-data-pending" / stored["id"]
    before_files = {child.name: child.read_bytes() for child in folder.iterdir()}
    before_rows = Measurement.objects.count()
    before_runs = ImportRun.objects.count()

    client.get(reverse("import_preview"))

    assert {child.name: child.read_bytes() for child in folder.iterdir()} == before_files
    assert Measurement.objects.count() == before_rows
    assert ImportRun.objects.count() == before_runs


@pytest.mark.django_db
def test_an_export_that_is_already_stored_offers_the_replay_checkbox(client, alice):
    client.force_login(alice)
    upload(client)
    confirm(client)

    response = upload(client)

    assert response.url == reverse("import_preview")
    content = client.get(reverse("import_preview")).content.decode()
    assert "This export was already imported." in content
    assert 'name="replay"' in content
    assert "fill gaps without replacing stored readings" in content


@pytest.mark.django_db
def test_skipped_rows_alone_also_offer_the_replay_checkbox(client, alice):
    client.force_login(alice)
    upload(client, CLASH)

    response = client.get(reverse("import_preview"))

    assert response.context["summary"]["counts"]["conflicts"] == 3
    assert 'name="replay"' in response.content.decode()


@pytest.mark.django_db
def test_confirming_creates_exactly_one_run_and_reports_the_counts(client, alice):
    client.force_login(alice)
    before = ImportRun.objects.filter(owner=alice).count()
    upload(client)

    response = confirm(client)

    assert response.status_code == 302
    assert response.url == reverse("imports")
    assert "pending_import" not in client.session
    assert ImportRun.objects.filter(owner=alice).count() == before + 1
    run = ImportRun.objects.filter(owner=alice).latest("imported_at")
    assert run.file_name == "export.csv"
    assert (run.readings, run.created, run.skipped, run.conflicts, run.panels) == (6, 6, 0, 0, 2)
    ledger = client.get(reverse("imports")).content.decode()
    assert "Imported export.csv: 6 readings, 6 created, 0 skipped" in ledger


@pytest.mark.django_db
def test_confirming_with_replay_keeps_stored_values_and_reports_the_conflict(client, alice):
    client.force_login(alice)
    moment = datetime(2024, 6, 3, 8, 0, tzinfo=zone())
    upload(client, CLASH)

    response = confirm(client, replay="1")

    assert response.url == reverse("imports")
    assert stored_value(alice, "systolic_bp", moment) == Decimal("130")
    assert stored_value(alice, "diastolic_bp", moment) == Decimal("85")
    assert stored_value(alice, "pulse", moment) == Decimal("70")
    run = ImportRun.objects.filter(owner=alice).latest("imported_at")
    assert run.conflicts == 3
    assert run.created == 0
    assert run.overwritten == 0
    assert "3 conflicts" in client.get(reverse("imports")).content.decode()


@pytest.mark.django_db
def test_cancelling_discards_the_staged_file_and_imports_nothing(client, alice, tmp_path):
    client.force_login(alice)
    before = ImportRun.objects.filter(owner=alice).count()
    upload(client)
    stored = client.session["pending_import"]

    response = client.post(
        reverse("import_preview"),
        {
            "import_id": stored["id"],
            "fingerprint": stored["fingerprint"],
            "action": "cancel",
        },
    )

    assert response.status_code == 302
    assert response.url == reverse("import_upload")
    assert "pending_import" not in client.session
    assert staged(tmp_path) == []
    assert ImportRun.objects.filter(owner=alice).count() == before
    assert (
        "Preview discarded. Nothing was imported."
        in client.get(reverse("import_upload")).content.decode()
    )


@pytest.mark.django_db
def test_a_preview_without_a_staged_upload_asks_for_the_csv_again(client, alice):
    client.force_login(alice)

    response = client.get(reverse("import_preview"), follow=True)

    assert response.redirect_chain[-1][0] == reverse("import_upload")
    assert PREVIEW_MISSING_MESSAGE in response.content.decode()


@pytest.mark.django_db
def test_a_preview_whose_staged_file_changed_is_called_stale(client, alice, tmp_path):
    client.force_login(alice)
    upload(client)
    stored = client.session["pending_import"]
    session = client.session
    session["pending_import"] = {**stored, "fingerprint": "0" * 64}
    session.save()

    response = client.get(reverse("import_preview"), follow=True)

    assert response.redirect_chain[-1][0] == reverse("import_upload")
    assert PREVIEW_STALE_MESSAGE in response.content.decode()
    assert staged(tmp_path) == []
    assert "pending_import" not in client.session


@pytest.mark.django_db
def test_a_preview_older_than_an_hour_is_called_expired(client, alice, tmp_path):
    client.force_login(alice)
    upload(client)
    stored = client.session["pending_import"]
    session = client.session
    session["pending_import"] = {
        **stored,
        "created_at": time.time() - pending.PENDING_TTL_SECONDS - 60,
    }
    session.save()

    response = client.get(reverse("import_preview"), follow=True)

    assert response.redirect_chain[-1][0] == reverse("import_upload")
    assert PREVIEW_EXPIRED_MESSAGE in response.content.decode()
    assert staged(tmp_path) == []


@pytest.mark.django_db
def test_a_preview_posted_from_an_earlier_tab_cannot_import_the_newer_file(client, alice):
    client.force_login(alice)
    upload(client)
    earlier = client.session["pending_import"]
    upload(client, CLASH)

    response = client.post(
        reverse("import_preview"),
        {
            "import_id": earlier["id"],
            "fingerprint": earlier["fingerprint"],
            "action": "confirm",
        },
        follow=True,
    )

    assert response.redirect_chain[-1][0] == reverse("import_upload")
    assert PREVIEW_STALE_MESSAGE in response.content.decode()
    assert ImportRun.objects.filter(owner=alice, file_name="export.csv").count() == 0


@pytest.mark.django_db
def test_the_ledger_lists_the_newest_run_first_and_badges_a_conflict(client, alice):
    ImportRun.objects.create(
        owner=alice,
        file_name="later.csv",
        content_sha256="b" * 64,
        readings=3,
        created=3,
        conflicts=2,
        panels=1,
    )
    client.force_login(alice)

    content = client.get(reverse("imports")).content.decode()

    assert content.index("later.csv") < content.index("alice.csv")
    assert '<span class="badge badge-warning">2 conflicts</span>' in content
    assert "Coverage" in content
    assert "Blood pressure" in content


@pytest.mark.django_db
def test_an_account_with_no_imports_is_told_so_and_pointed_at_the_upload_page(client):
    alice = get_user_model().objects.create_user(username="carol")
    client.force_login(alice)

    content = client.get(reverse("imports")).content.decode()

    assert "No imports yet." in content
    assert f'href="{reverse("import_upload")}"' in content


@pytest.mark.django_db
def test_purging_another_owners_run_reports_it_missing_and_deletes_nothing(client, alice, bob):
    run = ImportRun.objects.get(owner=bob)
    before = Measurement.objects.filter(import_run=run).count()
    client.force_login(alice)

    response = client.post(
        reverse("purge_import_run", args=[run.pk]), {"confirm": "1"}, follow=True
    )

    assert RUN_NOT_FOUND_MESSAGE in response.content.decode()
    assert ImportRun.objects.filter(pk=run.pk).exists()
    assert Measurement.objects.filter(import_run=run).count() == before


@pytest.mark.django_db
def test_purging_without_ticking_the_box_deletes_nothing(client, alice):
    run = ImportRun.objects.get(owner=alice)
    before = Measurement.objects.count()
    client.force_login(alice)

    response = client.post(reverse("purge_import_run", args=[run.pk]), follow=True)

    assert PURGE_UNCONFIRMED_MESSAGE in response.content.decode()
    assert ImportRun.objects.filter(pk=run.pk).exists()
    assert Measurement.objects.count() == before


@pytest.mark.django_db
def test_purging_one_run_keeps_a_later_overlap_and_leaves_no_orphaned_component(
    client, alice, tmp_path
):
    early = tmp_path / "early.csv"
    early.write_text(report("02 augusti 2024,09:00,,,64,,"), encoding="utf-8")
    late = tmp_path / "late.csv"
    late.write_text(report("02 augusti 2024,09:00,125,82,,,"), encoding="utf-8")
    first = import_report(alice, early)["run"]
    second = import_report(alice, late)["run"]
    moment = datetime(2024, 8, 2, 9, 0, tzinfo=zone())
    panel = Measurement.objects.get(owner=alice, metric="blood_pressure", at=moment)
    assert Measurement.objects.get(owner=alice, metric="systolic_bp", at=moment).parent_id == (
        panel.pk
    )
    client.force_login(alice)

    response = client.post(
        reverse("purge_import_run", args=[first.pk]), {"confirm": "1"}, follow=True
    )

    assert "Purged early.csv: 2 measurements deleted." in response.content.decode()
    assert not ImportRun.objects.filter(pk=first.pk).exists()
    assert ImportRun.objects.filter(pk=second.pk).exists()
    assert not Measurement.objects.filter(import_run=first).exists()
    assert not Measurement.objects.filter(owner=alice, metric="blood_pressure", at=moment).exists()
    kept = Measurement.objects.filter(import_run=second, at=moment).order_by("metric")
    assert [row.metric for row in kept] == ["diastolic_bp", "systolic_bp"]
    assert [row.parent_id for row in kept] == [None, None]


@pytest.mark.django_db
def test_a_get_on_the_purge_url_is_not_allowed(client, alice):
    run = ImportRun.objects.get(owner=alice)
    client.force_login(alice)

    response = client.get(reverse("purge_import_run", args=[run.pk]))

    assert response.status_code == 405
    assert ImportRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_the_conflict_table_shows_the_stored_and_the_file_value(client, alice):
    client.force_login(alice)
    upload(client, CLASH)

    content = client.get(reverse("import_preview")).content.decode()

    assert "The file disagrees with a reading you already have" in content
    assert '<td class="num">130</td>' in content
    assert '<td class="num">140</td>' in content


@pytest.mark.django_db
def test_a_timestamp_that_does_not_exist_names_the_configured_zone(client, alice):
    client.force_login(alice)
    upload(client, report("31 mars 2024,02:30,130,85,70,,", "02 september 2024,08:00,120,80,60,,"))

    response = client.get(reverse("import_preview"))

    content = response.content.decode()
    assert response.context["summary"]["counts"]["invalid"] == 1
    assert "2024-03-31 02:30" in content
    assert settings.OMRON_TIME_ZONE in content
    assert "OMRON_TIME_ZONE" in content


@pytest.mark.django_db
def test_more_than_ten_readings_hide_behind_a_show_all_details(client, alice):
    client.force_login(alice)
    upload(client, report(*[f"{day:02d} maj 2024,08:00,120,80,60,," for day in range(1, 13)]))

    content = client.get(reverse("import_preview")).content.decode()

    assert "Showing the first 10 of 36 readings." in content
    assert "<summary>Show all 36 readings</summary>" in content
    first_table = content.split("Show all 36 readings")[0]
    assert first_table.count('<td class="muted nowrap">') == 10
    assert content.count('<td class="muted nowrap">') == 36


@pytest.mark.django_db
def test_twenty_five_runs_fill_the_first_page_and_the_pager_finds_the_rest(client, alice):
    ImportRun.objects.bulk_create(
        [
            ImportRun(owner=alice, file_name=f"run-{day:02d}.csv", content_sha256=f"{day:0<64}")
            for day in range(26)
        ]
    )
    client.force_login(alice)

    first = client.get(reverse("imports")).content.decode()
    second = client.get(reverse("imports"), {"page": 2}).content.decode()

    assert first.count('data-label="File"') == 25
    assert 'rel="next"' in first
    assert second.count('data-label="File"') == 2


@pytest.mark.django_db
def test_the_purge_form_stays_closed_until_that_run_is_asked_for(client, alice):
    run = ImportRun.objects.get(owner=alice)
    client.force_login(alice)

    closed = client.get(reverse("imports")).content.decode()
    opened = client.get(reverse("imports"), {"confirm": run.pk}).content.decode()

    assert " data-delete-form hidden>" in closed
    assert " data-delete-form hidden>" not in opened
    assert 'name="confirm" value="1" required' in opened
    assert "records/delete.js" in opened
