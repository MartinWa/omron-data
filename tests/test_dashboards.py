import html
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from records.models import METRICS, PANEL, REFERENCE_BANDS, ImportRun, Measurement
from records.reports import (
    CARD_ROW_LIMIT,
    METRIC_DOMAINS,
    RANGE_KEYS,
    _bp_category,
    _fetch_window,
    _history,
    _rolling_average,
    build_dashboards,
    build_metric_dashboard,
)

SPAN = 60
STEP = 3
CSRF = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')
COORDINATES = re.compile(r'(?:x|y|width|height|cx|cy|dx|dy|points)="([^"]*)"')
SVG_OPEN = re.compile(r"<svg[^>]*>")
RAW_FLOAT = re.compile(r"\d+\.\d{3,}")
DELTA = re.compile(
    r'<span class="delta delta-(?P<kind>\w+)">\s*'
    r'<span aria-hidden="true">(?P<arrow>[^<]+)</span>\s*'
    r'<span class="visually-hidden">(?P<words>[^<]+)</span>',
    re.S,
)


def zone() -> ZoneInfo:
    return ZoneInfo(settings.OMRON_TIME_ZONE)


def moment(day: date) -> datetime:
    return datetime.combine(day, time(8, 0), tzinfo=zone())


def measurement(owner, metric: str, at: datetime, value, note: str = "") -> Measurement:
    return Measurement(
        owner=owner,
        metric=metric,
        at=at,
        value=Decimal(str(value)),
        unit=METRICS[metric].unit,
        note=note,
    )


def history(owner, today: date) -> None:
    rows = []
    for index, offset in enumerate(range(0, SPAN, STEP)):
        day = today - timedelta(days=offset)
        at = moment(day)
        panel = Measurement(owner=owner, metric=PANEL, at=at, unit="", note="resting")
        rows.append(panel)
        for metric, value in (
            ("systolic_bp", 126 + index % 5),
            ("diastolic_bp", 78 + index % 4),
            ("pulse", 64 + index % 6),
        ):
            rows.append(measurement(owner, metric, at, value, "resting"))
        for metric, value in (
            ("body_weight", round(72.5 + (index % 7) * 0.2, 1)),
            ("body_fat_pct", round(22.0 + (index % 3) * 0.3, 1)),
            ("skeletal_muscle_pct", round(38.4 - (index % 4) * 0.1, 1)),
            ("visceral_fat", 7 + index % 2),
            ("resting_metabolism", 1620 + index),
            ("impedance", 486 + index % 9),
        ):
            rows.append(measurement(owner, metric, at, value))
    Measurement.objects.bulk_create(rows)
    for row in rows:
        if row.metric in {"systolic_bp", "diastolic_bp", "pulse"}:
            row.parent = Measurement.objects.get(owner=owner, metric=PANEL, at=row.at)
    run = ImportRun.objects.create(
        owner=owner,
        file_name="history.csv",
        content_sha256="h" * 64,
        readings=len(rows),
    )
    ImportRun.objects.filter(pk=run.pk).update(imported_at=moment(today))


@pytest.fixture
def owner(db):
    user = get_user_model().objects.create_user(username="dashboards")
    history(user, timezone.localdate())
    return user


@pytest.fixture
def signed_in(client, owner):
    client.force_login(owner)
    return client


def content_of(response) -> str:
    return response.content.decode()


def hrefs(body: str) -> list[str]:
    return [html.unescape(match) for match in re.findall(r'href="([^"]+)"', body)]


def query_of(link: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(link).query)


def links_with(body: str, key: str) -> list[dict[str, list[str]]]:
    return [params for params in map(query_of, hrefs(body)) if key in params]


def group(body: str, label: str) -> str:
    start = body.index(f'aria-label="{label}">')
    return body[start : body.index("</span>", start)]


def heatmap_grid(body: str) -> str:
    return body.split('class="heatmap-grid"', 1)[1].split("</div>", 1)[0]


def delta_blocks(body: str) -> list[tuple[str, str, str]]:
    return [match.groups() for match in DELTA.finditer(body)]


def weigh_ins(owner, today: date, offsets: list[int]) -> None:
    Measurement.objects.bulk_create(
        [
            measurement(owner, "body_weight", moment(today - timedelta(days=offset)), 72.0)
            for offset in offsets
        ]
    )


def centre(bar) -> float:
    return bar["x"] + bar["width"] / 2


@pytest.mark.django_db
def test_the_first_visit_sends_anonymous_visitors_to_the_login_page(client):
    response = client.get(reverse("dashboards"))

    assert response.status_code == 302
    assert reverse("login") in response.url


def test_the_nav_marks_dashboards_active_on_both_routes(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    assert 'href="/dashboards/" class="active" aria-current="page"' in body
    assert body.count('aria-current="page"') == 1

    drill = content_of(signed_in.get(reverse("dashboard_metric", kwargs={"metric": "body_weight"})))
    assert 'href="/dashboards/" class="active" aria-current="page"' in drill
    assert drill.count('aria-current="page"') == 1


def test_every_card_renders_for_a_two_month_history(signed_in):
    response = signed_in.get(reverse("dashboards"))
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["has_data"] is True
    for heading in ("Blood pressure", "Weight", "Body composition", "Consistency"):
        assert heading in body
    assert "Readings per week" in body
    assert "Extremes" in body
    assert 'class="stat-grid report-kpis"' in body
    assert body.count('class="chart chart-lines"') == 4
    assert body.count('class="chart chart-bars"') == 1
    assert 'class="heatmap-grid"' in body
    assert 'class="segmented" role="group" aria-label="Date range"' in body
    assert 'role="group" aria-label="Body composition metric"' in body


def test_the_default_range_is_thirty_days(signed_in):
    context = signed_in.get(reverse("dashboards")).context

    assert context["range_key"] == "30d"
    assert context["range"]["start"] == timezone.localdate() - timedelta(days=29)
    assert context["range"]["end"] == timezone.localdate()
    assert context["range"]["previous"]["end"] == timezone.localdate() - timedelta(days=30)


@pytest.mark.parametrize("key", RANGE_KEYS)
def test_each_range_key_renders_and_marks_the_current_option(signed_in, key):
    response = signed_in.get(reverse("dashboards"), {"range": key})
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["range_key"] == key
    assert f'class="segmented-item is-current" href="?range={key}" aria-current="true">' in body
    current = re.findall(
        r'class="segmented-item is-current" href="\?range=([^"]+)"', group(body, "Date range")
    )
    assert current == [key]


def test_an_unknown_range_key_falls_back_to_the_default(signed_in):
    response = signed_in.get(reverse("dashboards"), {"range": "forever"})

    assert response.status_code == 200
    assert response.context["range_key"] == "30d"


def test_range_and_metric_links_preserve_each_other(signed_in):
    body = content_of(signed_in.get(reverse("dashboards"), {"range": "1y", "metric": "impedance"}))

    range_links = links_with(group(body, "Date range"), "range")
    metric_links = links_with(group(body, "Body composition metric"), "metric")
    assert range_links
    assert metric_links
    assert {params["metric"][0] for params in range_links} == {"impedance"}
    assert {params["range"][0] for params in metric_links} == {"1y"}


def test_a_one_point_series_still_draws_a_marker(client, db):
    user = get_user_model().objects.create_user(username="single")
    at = moment(timezone.localdate())
    panel = Measurement.objects.create(owner=user, metric=PANEL, at=at, unit="", note="")
    for metric, value in (("systolic_bp", 128), ("diastolic_bp", 81), ("pulse", 66)):
        Measurement.objects.create(
            owner=user,
            metric=metric,
            at=at,
            value=Decimal(str(value)),
            unit=METRICS[metric].unit,
            note="",
            parent=panel,
        )
    client.force_login(user)

    body = content_of(client.get(reverse("dashboards"), {"range": "all"}))

    assert '<circle class="chart-dot series-1"' in body
    assert 'class="chart-dot series-2"' in body
    assert body.count("<circle") >= 2


def test_an_empty_range_shows_the_notice_without_a_traceback(client, db):
    user = get_user_model().objects.create_user(username="stale")
    old = moment(timezone.localdate() - timedelta(days=400))
    Measurement.objects.create(
        owner=user,
        metric="body_weight",
        at=old,
        value=Decimal("80.5"),
        unit="kg",
        note="",
    )
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "30d"})
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["range_empty"] is True
    assert "No readings in" in body
    assert "Show all time" in body
    assert 'class="stat-grid report-kpis"' not in body


def test_an_empty_database_shows_the_import_call_to_action(client, db):
    user = get_user_model().objects.create_user(username="nothing")
    client.force_login(user)

    body = content_of(client.get(reverse("dashboards")))

    assert "No readings yet." in body
    assert 'href="/imports/"' in body
    assert "Import a CSV export" in body
    assert "report-kpis" not in body


def test_every_svg_carries_a_role_and_a_label(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    charts = [tag for tag in SVG_OPEN.findall(body) if 'class="chart chart-' in tag]

    assert len(charts) == 5
    for tag in charts:
        assert 'role="img"' in tag
        label = tag.split('aria-label="', 1)[1].split('"', 1)[0]
        assert label.strip()
        assert "nan" not in label.lower()
        assert "none" not in label.lower()


def test_no_chart_attribute_carries_nan_inf_or_none(signed_in):
    for key in RANGE_KEYS:
        body = content_of(signed_in.get(reverse("dashboards"), {"range": key}))
        for match in COORDINATES.finditer(body):
            value = match.group(1).lower()
            assert "nan" not in value
            assert "inf" not in value
            assert "none" not in value


def test_every_chart_coordinate_is_numeric(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    checked = 0

    for match in COORDINATES.finditer(body):
        for token in match.group(1).replace(",", " ").split():
            assert float(token) == float(token)
            checked += 1
    assert checked > 100


def test_heatmap_cells_are_whole_weeks_and_at_most_fifty_three(signed_in):
    body = content_of(signed_in.get(reverse("dashboards"), {"range": "all"}))
    grid = heatmap_grid(body)

    assert grid.count("heat-cell") % 7 == 0
    assert grid.count("heat-cell") // 7 <= 53
    assert "is-outside" in grid
    assert "Mon" in body
    assert "Sun" in body
    assert "Less" in body
    assert "More" in body


def test_the_heatmap_table_fallback_holds_the_same_counts(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    grid = heatmap_grid(body)
    titles = re.findall(r'title="[^"]*: (\d+) reading', grid)
    cells_with_readings = len(re.findall(r"heat-cell level-[123]", grid))

    table = body.split("Readings per day from", 1)[1].split("</table>", 1)[0]
    rows = re.findall(
        r"<tr><td>[^<]+</td><td>[A-Z][a-z]{2}</td><td class=\"num\">(\d+)</td>", table
    )

    assert titles
    assert sum(int(value) for value in titles) == sum(int(value) for value in rows)
    assert len(rows) == cells_with_readings


def test_series_colours_come_only_from_css_classes(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))

    assert "series-1" in body
    assert "series-2" in body
    assert "series-1 avg" in body
    assert not re.search(r'(?:fill|stroke)="(#|rgb|hsl)', body)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", body)


def test_the_rendered_page_carries_no_raw_floats(signed_in):
    for key in RANGE_KEYS:
        body = content_of(signed_in.get(reverse("dashboards"), {"range": key}))

        assert not RAW_FLOAT.search(body), key


def test_every_kpi_delta_pairs_an_arrow_with_hidden_words(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    blocks = delta_blocks(body)

    assert len(blocks) >= 3
    assert body.count('class="delta delta-') == len(blocks)
    for kind, arrow, words in blocks:
        assert kind in {"worse", "better", "neutral"}
        assert arrow.strip()
        assert words.startswith(("up ", "down ", "unchanged", "no reading"))


def test_the_kpis_cover_the_six_numbers_the_page_promises(signed_in):
    context = signed_in.get(reverse("dashboards")).context

    assert [card["key"] for card in context["kpis"]] == [
        "blood-pressure",
        "seven-day",
        "weight",
        "weight-change",
        "readings",
        "last-import",
    ]
    assert context["kpis"][5]["value"] == timezone.localdate().isoformat()


def test_the_blood_pressure_card_draws_two_panels_with_bands_and_a_category(signed_in):
    response = signed_in.get(reverse("dashboards"), {"range": "90d"})
    body = content_of(response)
    card = response.context["blood_pressure"]
    chart = card["chart"]

    assert [line["label"] for line in chart["lines"]] == [
        "Systolic",
        "Diastolic",
        "Systolic 7-day average",
        "Diastolic 7-day average",
    ]
    assert [line["class"] for line in chart["lines"]] == [
        "series-1",
        "series-2",
        "series-1 avg",
        "series-2 avg",
    ]
    assert [item["unit"] for item in card["legend"]] == ["mmHg"] * 4
    assert card["pulse_chart"]["lines"][0]["label"] == "Pulse"
    assert [item["unit"] for item in card["pulse_legend"]] == ["/min"]
    assert card["pulse_chart"]["xlabels"] == chart["xlabels"]
    assert response.context["period_label"] in chart["aria_label"]
    assert "All three series share one axis" not in body
    assert "135/85" in body
    assert "AHA category:" in body
    assert card["category"]["text"] in body
    assert '<th scope="col">Note</th>' in body


@pytest.mark.django_db
def test_the_blood_pressure_card_renders_when_only_diastolic_exists(client, db):
    user = get_user_model().objects.create_user(username="dia-only")
    today = timezone.localdate()
    Measurement.objects.bulk_create(
        [
            measurement(user, metric, moment(today - timedelta(days=offset)), value)
            for offset in range(8)
            for metric, value in (("diastolic_bp", 78), ("pulse", 64))
        ]
    )
    client.force_login(user)

    response = client.get(reverse("dashboards"))
    body = content_of(response)
    card = response.context["blood_pressure"]

    assert response.status_code == 200
    assert card["has_points"] is True
    assert [line["label"] for line in card["chart"]["lines"]] == [
        "Diastolic",
        "Diastolic 7-day average",
    ]
    assert card["pulse_chart"]["lines"][0]["label"] == "Pulse"
    assert "Diastolic" in body


def test_the_blood_pressure_bands_come_from_the_reference_bands(client, db):
    user = get_user_model().objects.create_user(username="bands")
    today = timezone.localdate()
    rows = []
    for offset, systolic in ((20, 100), (10, 160)):
        at = moment(today - timedelta(days=offset))
        panel = Measurement.objects.create(owner=user, metric=PANEL, at=at, unit="", note="")
        for metric, value in (("systolic_bp", systolic), ("diastolic_bp", 70)):
            row = measurement(user, metric, at, value)
            row.parent = panel
            rows.append(row)
    Measurement.objects.bulk_create(rows)
    client.force_login(user)

    chart = client.get(reverse("dashboards"), {"range": "all"}).context["blood_pressure"]["chart"]

    assert [(band["class"], band["label"]) for band in chart["bands"]] == [
        (band["class"], band["label"]) for band in REFERENCE_BANDS["blood_pressure"]
    ]
    plot_height = 240 - 14 - 34
    span = chart["y_max"] - chart["y_min"]
    for band, reference in zip(chart["bands"], REFERENCE_BANDS["blood_pressure"], strict=True):
        low = max(reference["from"], chart["y_min"])
        high = chart["y_max"] if reference["to"] is None else min(reference["to"], chart["y_max"])
        assert band["height"] == pytest.approx(plot_height * (high - low) / span, abs=0.01)
        expected_y = 14 + plot_height - plot_height * (high - chart["y_min"]) / span
        assert band["y"] == pytest.approx(expected_y, abs=0.01)


def test_high_readings_get_outlier_marks_and_a_note_on_the_blood_pressure_chart(client, db):
    user = get_user_model().objects.create_user(username="outliers")
    today = timezone.localdate()
    at = moment(today)
    panel = Measurement.objects.create(owner=user, metric=PANEL, at=at, unit="", note="afib")
    rows = []
    for metric, value in (("systolic_bp", 141), ("diastolic_bp", 92), ("pulse", 70)):
        row = measurement(user, metric, at, value, "afib")
        row.parent = panel
        rows.append(row)
    at2 = moment(today - timedelta(days=1))
    panel2 = Measurement.objects.create(owner=user, metric=PANEL, at=at2, unit="", note="")
    for metric, value in (("systolic_bp", 139), ("diastolic_bp", 89), ("pulse", 68)):
        row = measurement(user, metric, at2, value)
        row.parent = panel2
        rows.append(row)
    Measurement.objects.bulk_create(rows)
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "all"})
    body = content_of(response)
    chart = response.context["blood_pressure"]["chart"]

    marks = {mark["title"]: mark["class"] for line in chart["lines"] for mark in line["marks"]}
    assert marks == {
        f"Systolic 141 mmHg on {today:%d %b %Y} 08:00": "series-1",
        f"Diastolic 92 mmHg on {today:%d %b %Y} 08:00": "series-2",
    }
    assert 'class="chart-dot series-1 outlier"' in body
    assert 'class="chart-dot series-2 outlier"' in body
    assert "afib" in body


def test_the_latest_reading_pair_is_classified_into_an_aha_category():
    assert _bp_category(118, 79) == {"text": "Normal", "class": "band-ok"}
    assert _bp_category(125, 79) == {"text": "Elevated", "class": "band-warn"}
    assert _bp_category(129, 85) == {"text": "Stage 1", "class": "band-warn"}
    assert _bp_category(139, 79) == {"text": "Stage 1", "class": "band-warn"}
    assert _bp_category(140, 79) == {"text": "Stage 2", "class": "band-danger"}
    assert _bp_category(120, 90) == {"text": "Stage 2", "class": "band-danger"}
    assert _bp_category(181, 110) == {"text": "Hypertensive crisis", "class": "band-danger"}
    assert _bp_category(130, 121) == {"text": "Hypertensive crisis", "class": "band-danger"}


def test_the_weight_card_draws_daily_points_a_seven_day_average_and_a_latest_dot(signed_in):
    response = signed_in.get(reverse("dashboards"), {"range": "90d"})
    body = content_of(response)
    weight = response.context["weight"]

    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    assert [line["class"] for line in weight["chart"]["lines"]] == ["series-1", "series-2 avg"]
    assert len(weight["chart"]["lines"][0]["dots"]) == weight["readings"]
    latest = [dot for dot in weight["chart"]["lines"][0]["dots"] if dot["class"] == "dot-latest"]
    assert len(latest) == 1
    assert latest[0]["title"].startswith("Latest ")
    assert "kg" in latest[0]["title"]
    assert weight["chart"]["y_min"] > 0
    assert "Line: 7-day rolling average. The line breaks across gaps longer than 7 days." in body
    weight_section = body.split("Weight</h2>", 1)[1].split("</section>", 1)[0]
    assert "bar-partial" not in weight_section
    assert weight["rows"][0]["value"].endswith(" kg")
    assert "average" in weight["rows"][0]


def test_the_seven_day_average_is_absent_until_seven_days_of_history_exist(client, db):
    user = get_user_model().objects.create_user(username="six-days")
    today = timezone.localdate()
    weigh_ins(user, today, list(range(6)))
    client.force_login(user)

    weight = client.get(reverse("dashboards"), {"range": "all"}).context["weight"]
    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight"]
    assert all(row["average"] == "—" for row in weight["rows"])

    weigh_ins(user, today, [6])
    weight = client.get(reverse("dashboards"), {"range": "all"}).context["weight"]
    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    average_line = weight["chart"]["lines"][1]
    assert len(average_line["segments"][0].split()) == 1
    assert weight["rows"][-1]["average"] == "72.0 kg"
    assert all(row["average"] == "—" for row in weight["rows"][:-1])


def test_the_seven_day_window_arithmetic_crosses_a_month_boundary():
    today = date(2026, 3, 2)
    points = [
        {"date": today - timedelta(days=offset), "value": float(70 + offset)} for offset in range(7)
    ]
    averages = _rolling_average(points)

    assert list(averages) == [date(2026, 3, 2)]
    assert averages[date(2026, 3, 2)] == pytest.approx(sum(range(70, 77)) / 7)

    sparse = [{"date": date(2026, 2, 20), "value": 80.0}, {"date": date(2026, 3, 2), "value": 72.0}]
    assert _rolling_average(sparse) == {}


def test_a_single_weigh_in_still_renders_a_dot_and_a_row(client, db):
    user = get_user_model().objects.create_user(username="once")
    weigh_ins(user, timezone.localdate(), [0])
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "all"})
    weight = response.context["weight"]
    body = content_of(response)

    assert weight["has_points"] is True
    assert weight["chart"]["y_max"] > weight["chart"]["y_min"]
    line = weight["chart"]["lines"][0]
    assert len(line["segments"]) == 1
    assert len(line["dots"]) == 1
    assert len(weight["rows"]) == 1
    assert weight["rows"][0]["value"] == "72.0 kg"
    assert weight["rows"][0]["average"] == "—"
    assert "72.0 kg" in body


def test_weigh_ins_more_than_a_week_apart_break_the_line_into_segments(client, db):
    user = get_user_model().objects.create_user(username="gappy")
    today = timezone.localdate()
    weigh_ins(user, today, [0, 8, 16, 24, 32, 40, 48])
    client.force_login(user)

    weight = client.get(reverse("dashboards"), {"range": "all"}).context["weight"]
    line = weight["chart"]["lines"][0]

    assert len(line["segments"]) == 7
    assert all(len(segment.split()) == 1 for segment in line["segments"])
    assert len(line["dots"]) == 7


def test_the_weight_table_caps_at_two_hundred_rows_with_a_note_and_a_link(client, db):
    user = get_user_model().objects.create_user(username="daily")
    today = timezone.localdate()
    weigh_ins(user, today, list(range(CARD_ROW_LIMIT + 50)))
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "all"})
    weight = response.context["weight"]
    body = content_of(response)

    assert len(weight["rows"]) == CARD_ROW_LIMIT
    assert weight["rows_total"] == CARD_ROW_LIMIT + 50
    assert weight["rows_url"] == "/readings/?metric=body_weight"
    assert f"Showing the {CARD_ROW_LIMIT} most recent of {CARD_ROW_LIMIT + 50} readings" in body
    assert 'href="/readings/?metric=body_weight"' in body


def test_the_metric_switch_draws_one_metric_at_a_time(signed_in):
    for metric in ("body_fat_pct", "resting_metabolism"):
        response = signed_in.get(reverse("dashboards"), {"metric": metric})
        composition = response.context["composition"]

        assert composition["metric"] == metric
        assert composition["label"] in composition["chart"]["aria_label"]
        assert len(composition["chart"]["lines"]) == 1
        assert METRICS[metric].unit == composition["unit"]

    body = content_of(signed_in.get(reverse("dashboards"), {"metric": "resting_metabolism"}))
    section = body.split("Body composition", 1)[1].split("</section>", 1)[0]
    assert section.count("<polyline") == 1
    assert "kcal" in section


def test_the_extremes_card_links_to_the_drill_down(signed_in):
    body = content_of(signed_in.get(reverse("dashboards"), {"range": "90d"}))

    assert 'class="record-list"' in body
    assert 'href="/dashboards/body_weight/?range=90d"' in body
    assert 'href="/dashboards/systolic_bp/?range=90d"' in body
    assert "High " in body
    assert "Low " in body


def test_the_drill_down_lists_every_reading_in_the_range(signed_in):
    response = signed_in.get(
        reverse("dashboard_metric", kwargs={"metric": "body_weight"}), {"range": "90d"}
    )
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["readings_total"] == len(response.context["rows"])
    assert response.context["readings_total"] >= 19
    assert "72.5 kg" in body
    assert '<th scope="col">Note</th>' in body
    assert '<th scope="col">Unit</th>' not in body
    assert "Highest" in body
    assert "Lowest" in body
    assert "Latest" in body
    back = body.split('class="report-back"', 1)[1].split("</p>", 1)[0]
    assert "range=90d" in back
    extremes = response.context["extremes"]
    assert extremes["latest"]["value"].endswith(" kg")
    assert extremes["high"]["value"].endswith(" kg")


def test_the_drill_down_marks_the_current_range(signed_in):
    for key in RANGE_KEYS:
        response = signed_in.get(
            reverse("dashboard_metric", kwargs={"metric": "pulse"}), {"range": key}
        )
        body = content_of(response)

        assert response.status_code == 200
        assert f'class="segmented-item is-current" href="?range={key}" aria-current="true">' in body
        assert body.count('aria-current="true"') == 1


def test_an_unknown_metric_slug_is_not_found(signed_in):
    response = signed_in.get(reverse("dashboard_metric", kwargs={"metric": "blood_sugar"}))

    assert response.status_code == 404


def test_another_owners_data_never_appears(client, owner):
    other = get_user_model().objects.create_user(username="other")
    Measurement.objects.create(
        owner=other,
        metric="pulse",
        at=moment(timezone.localdate()),
        value=Decimal("61"),
        unit="/min",
        note="",
    )
    client.force_login(other)

    body_response = client.get(reverse("dashboards"))
    drill = client.get(reverse("dashboard_metric", kwargs={"metric": "body_weight"}))
    body = content_of(body_response)

    assert "No weight readings in" in body
    assert "No weight readings in" in content_of(drill)
    assert "72.5" not in body
    assert "72.5" not in content_of(drill)
    assert body_response.context["readings_total"] == 1
    assert [row["metric"] for row in body_response.context["extremes"]] == ["pulse"]


@pytest.mark.django_db
def test_readings_outside_the_range_do_not_add_a_single_query(signed_in, owner):
    with CaptureQueriesContext(connection) as captured:
        before = signed_in.get(reverse("dashboards"), {"range": "30d"})
    queries = len(captured.captured_queries)

    old = moment(timezone.localdate() - timedelta(days=1500))
    Measurement.objects.bulk_create(
        [
            measurement(owner, metric, old + timedelta(days=day, hours=hour), value)
            for day in range(100)
            for hour, (metric, value) in enumerate(
                (
                    ("body_weight", 90.5),
                    ("body_fat_pct", 25.5),
                    ("systolic_bp", 150),
                    ("diastolic_bp", 95),
                )
            )
        ]
    )

    with CaptureQueriesContext(connection) as captured:
        after = signed_in.get(reverse("dashboards"), {"range": "30d"})

    assert len(captured.captured_queries) == queries
    assert after.context["readings_total"] == before.context["readings_total"]
    assert after.context["kpis"] == before.context["kpis"]
    assert after.context["range"]["start"] == before.context["range"]["start"]
    assert CSRF.sub(b"", after.content) == CSRF.sub(b"", before.content)

    series = _history(owner, _fetch_window(after.context["range"]))
    assert sum(len(points) for points in series.values()) < 400


def test_the_page_stays_under_forty_queries(signed_in):
    with CaptureQueriesContext(connection) as captured:
        response = signed_in.get(reverse("dashboards"), {"range": "90d"})

    assert response.status_code == 200
    assert len(captured.captured_queries) < 40


def test_the_printed_page_hides_the_nav_and_keeps_tables_open(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    css = Path(finders.find("records/app.css")).read_text(encoding="utf-8")
    print_block = css[css.index("@media print {") :]

    assert 'class="data-table" data-print-open' in body
    assert body.count("data-print-open") >= 5
    assert "records/print.js" in body
    assert 'onclick="window.print()">Print</button>' in body
    assert ".site-header," in print_block
    assert print_block.index(".site-header,") < print_block.index("display: none")
    assert ".segmented," in print_block
    assert "details[data-print-open]::details-content" in print_block
    assert "content-visibility: visible" in print_block


def test_the_print_script_opens_the_data_tables():
    script = Path(finders.find("records/print.js")).read_text(encoding="utf-8")

    assert "[data-print-open]" in script
    assert "beforeprint" in script
    assert "afterprint" in script


def test_the_builders_return_the_whole_page_without_a_request(owner):
    today = timezone.localdate()
    context = build_dashboards(owner, {}, today)

    assert context["range_key"] == "30d"
    assert context["period_label"].endswith(str(today.year))
    assert set(context) >= {
        "has_data",
        "range",
        "range_options",
        "metric",
        "metric_options",
        "kpis",
        "blood_pressure",
        "weight",
        "composition",
        "consistency",
        "weekly",
        "extremes",
    }

    drill = build_metric_dashboard(owner, "body_weight", {"range": "all"}, today)
    assert drill["metric_label"] == "Weight"
    assert drill["rows"]
    assert drill["dashboards_url"] == "/dashboards/?range=all&metric=body_fat_pct"


def test_the_builders_return_none_for_an_unknown_metric(owner):
    assert build_metric_dashboard(owner, "blood_sugar", {}, timezone.localdate()) is None


@pytest.mark.django_db
def test_the_kpi_texts_use_whole_mmhg_and_one_decimal_kg(signed_in):
    kpis = signed_in.get(reverse("dashboards")).context["kpis"]
    by_key = {card["key"]: card for card in kpis}

    assert by_key["blood-pressure"]["value"] == "126/78 mmHg"
    assert by_key["blood-pressure"]["delta"] is None
    assert by_key["seven-day"]["value"] == "127/79 mmHg"
    assert by_key["seven-day"]["sub"] == "based on 3 readings"
    assert by_key["weight"]["value"] == "72.5 kg"
    assert by_key["weight-change"]["value"] == "72.9 kg → 72.5 kg"
    assert by_key["weight-change"]["delta"]["text"] == "−0.4 kg"
    assert by_key["weight-change"]["delta"]["tone"] == "neutral"
    assert by_key["readings"]["value"] == "90"
    assert by_key["last-import"]["value"] == timezone.localdate().isoformat()

    body = content_of(signed_in.get(reverse("dashboards")))
    assert "127/79 mmHg" in body
    assert "based on 3 readings" in body
    assert 'class="stat-sub muted small"' in body


@pytest.mark.django_db
def test_the_seven_day_average_delta_is_absolute_mmhg_and_worse_when_it_rises(client, db):
    user = get_user_model().objects.create_user(username="rising")
    today = timezone.localdate()
    rows = []
    for offset in range(60):
        at = moment(today - timedelta(days=offset))
        rows.append(Measurement(owner=user, metric=PANEL, at=at, unit="", note=""))
        systolic = 140 if offset < 30 else 120
        for metric, value in (("systolic_bp", systolic), ("diastolic_bp", 80), ("pulse", 70)):
            rows.append(measurement(user, metric, at, value))
    Measurement.objects.bulk_create(rows)
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "30d"})
    body = content_of(response)
    seven = {card["key"]: card for card in response.context["kpis"]}["seven-day"]

    assert seven["value"] == "140/80 mmHg"
    assert seven["sub"] == "based on 7 readings"
    assert seven["delta"]["text"] == "+20 mmHg"
    assert seven["delta"]["direction"] == "up"
    assert seven["delta"]["tone"] == "worse"
    assert seven["delta"]["aria"] == "up 20 mmHg versus the previous 30 days"
    assert 'class="delta delta-worse"' in body
    assert "+20 mmHg" in body


@pytest.mark.django_db
def test_the_composition_card_honours_each_metric_domain(signed_in):
    for metric, (low, high) in METRIC_DOMAINS.items():
        chart = signed_in.get(reverse("dashboards"), {"metric": metric}).context["composition"][
            "chart"
        ]

        assert chart["y_min"] <= low, metric
        assert chart["y_max"] >= high, metric


@pytest.mark.django_db
def test_the_composition_domain_widens_instead_of_clipping_data_outside_it(client, db):
    user = get_user_model().objects.create_user(username="outside")
    today = timezone.localdate()
    Measurement.objects.bulk_create(
        [
            measurement(user, "body_fat_pct", moment(today - timedelta(days=offset)), value)
            for offset, value in ((0, 10.0), (5, 50.0))
        ]
    )
    client.force_login(user)

    chart = client.get(reverse("dashboards"), {"metric": "body_fat_pct", "range": "all"}).context[
        "composition"
    ]["chart"]

    assert chart["y_min"] <= 10
    assert chart["y_max"] >= 50


@pytest.mark.django_db
def test_the_composition_table_caps_at_two_hundred_rows_with_a_note_and_a_link(client, db):
    user = get_user_model().objects.create_user(username="fat-daily")
    today = timezone.localdate()
    Measurement.objects.bulk_create(
        [
            measurement(user, "body_fat_pct", moment(today - timedelta(days=offset)), 22.0)
            for offset in range(CARD_ROW_LIMIT + 50)
        ]
    )
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"metric": "body_fat_pct", "range": "all"})
    composition = response.context["composition"]
    body = content_of(response)

    assert len(composition["rows"]) == CARD_ROW_LIMIT
    assert composition["rows_total"] == CARD_ROW_LIMIT + 50
    assert composition["rows_url"] == "/dashboards/body_fat_pct/?range=all"
    assert f"Showing the {CARD_ROW_LIMIT} most recent of {CARD_ROW_LIMIT + 50} readings" in body
    assert 'href="/dashboards/body_fat_pct/?range=all"' in body


@pytest.mark.django_db
def test_the_blood_pressure_table_caps_at_two_hundred_rows_with_a_note_and_a_link(client, db):
    user = get_user_model().objects.create_user(username="bp-daily")
    today = timezone.localdate()
    rows = []
    for offset in range(CARD_ROW_LIMIT + 10):
        at = moment(today - timedelta(days=offset))
        rows.append(Measurement(owner=user, metric=PANEL, at=at, unit="", note=""))
        for metric, value in (("systolic_bp", 128), ("diastolic_bp", 80), ("pulse", 70)):
            rows.append(measurement(user, metric, at, value))
    Measurement.objects.bulk_create(rows)
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "all"})
    card = response.context["blood_pressure"]
    body = content_of(response)

    assert len(card["rows"]) == CARD_ROW_LIMIT
    assert card["rows_total"] == CARD_ROW_LIMIT + 10
    assert card["rows_url"] == "/readings/?metric=blood_pressure"
    assert f"Showing the {CARD_ROW_LIMIT} most recent of {CARD_ROW_LIMIT + 10} readings" in body
    assert 'href="/readings/?metric=blood_pressure"' in body
    assert card["rows"][0]["note"] == ""


@pytest.mark.django_db
def test_the_weekly_table_caps_at_two_hundred_weeks_with_a_note_and_a_link(client, db):
    user = get_user_model().objects.create_user(username="weekly-years")
    today = timezone.localdate()
    Measurement.objects.bulk_create(
        [
            measurement(user, "body_weight", moment(today - timedelta(days=7 * week)), 72.0)
            for week in range(CARD_ROW_LIMIT + 10)
        ]
    )
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "all"})
    weekly = response.context["weekly"]
    body = content_of(response)

    assert len(weekly["rows"]) == CARD_ROW_LIMIT
    assert weekly["rows_total"] == CARD_ROW_LIMIT + 10
    assert weekly["rows_url"] == "/readings/"
    assert f"Showing the {CARD_ROW_LIMIT} most recent of {CARD_ROW_LIMIT + 10} weeks" in body
    assert 'href="/readings/"' in body


def test_the_extremes_row_carries_the_unit_and_a_single_reading_is_not_repeated(
    signed_in, client, db
):
    body = content_of(signed_in.get(reverse("dashboards"), {"range": "90d"}))

    assert " mmHg" in body
    assert " kg" in body

    user = get_user_model().objects.create_user(username="one-pulse")
    Measurement.objects.create(
        owner=user,
        metric="pulse",
        at=moment(timezone.localdate()),
        value=Decimal("61"),
        unit="/min",
        note="",
    )
    client.force_login(user)
    response = client.get(reverse("dashboards"), {"range": "all"})
    body = content_of(response)
    row = next(
        row
        for row in re.findall(r'<li class="record-row">(.*?)</li>', body, re.S)
        if "Pulse" in row
    )

    assert response.context["extremes"][0]["count"] == 1
    assert "61 /min" in row
    assert "Latest on" in row
    assert "1 reading" in row
    assert "High" not in row
    assert "Low" not in row


def test_the_lede_states_how_recent_the_last_reading_is(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))

    assert "last reading today" in body


@pytest.mark.django_db
def test_the_lede_counts_the_days_since_the_last_reading(client, db):
    user = get_user_model().objects.create_user(username="stale-lede")
    old = moment(timezone.localdate() - timedelta(days=400))
    Measurement.objects.create(
        owner=user, metric="body_weight", at=old, value=Decimal("80.5"), unit="kg", note=""
    )
    client.force_login(user)

    response = client.get(reverse("dashboards"), {"range": "all"})
    body = content_of(response)

    assert response.context["last_reading_days"] == 400
    assert "last reading 400 days ago" in body


def test_the_heatmap_carries_a_visually_hidden_summary(signed_in):
    response = signed_in.get(reverse("dashboards"))
    body = content_of(response)
    consistency = response.context["consistency"]

    summary = re.search(r'<p class="visually-hidden">(Heatmap of [^<]+)</p>', body)

    assert summary
    assert consistency["window_label"] in summary.group(1)
    assert f"{consistency['days']} days with readings" in summary.group(1)
    assert f"{consistency['readings']} readings in total" in summary.group(1)
    assert "The table below lists each day." in summary.group(1)


@pytest.mark.django_db
def test_the_page_shows_the_year_on_january_day_ticks_across_a_year_boundary(client, db):
    user = get_user_model().objects.create_user(username="yearcross")
    for day, weight in ((date(2025, 12, 20), "72.0"), (date(2026, 1, 10), "71.5")):
        Measurement.objects.create(
            owner=user,
            metric="body_weight",
            at=moment(day),
            value=Decimal(weight),
            unit="kg",
            note="",
        )
    client.force_login(user)

    across = build_dashboards(user, {"range": "all"}, date(2026, 1, 10))
    labels = [label["label"] for label in across["weight"]["chart"]["xlabels"]]
    assert "3 Jan 2026" in labels
    assert "20 Dec" in labels
    assert all("2025" not in label for label in labels if "Jan" not in label)

    single_year_user = get_user_model().objects.create_user(username="january-only")
    for day in (date(2026, 1, 2), date(2026, 1, 9)):
        Measurement.objects.create(
            owner=single_year_user,
            metric="body_weight",
            at=moment(day),
            value=Decimal("72.0"),
            unit="kg",
            note="",
        )
    single = build_dashboards(single_year_user, {"range": "all"}, date(2026, 1, 9))
    labels = [label["label"] for label in single["weight"]["chart"]["xlabels"]]
    assert labels == ["2 Jan", "4 Jan", "7 Jan", "9 Jan"]
