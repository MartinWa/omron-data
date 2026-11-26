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

from records.models import METRICS, PANEL, ImportRun, Measurement
from records.reports import RANGE_KEYS, build_dashboards, build_metric_dashboard

SPAN = 60
STEP = 3
COORDINATES = re.compile(r'(?:x|y|width|height|cx|cy|dx|dy|points)="([^"]*)"')
SVG_OPEN = re.compile(r"<svg[^>]*>")
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
    assert body.count('class="chart chart-lines"') == 2
    assert body.count('class="chart chart-bars"') == 2
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
    assert 'href="/imports/upload/"' in body
    assert "Import a CSV export" in body
    assert "report-kpis" not in body


def test_every_svg_carries_a_role_and_a_label(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    charts = [tag for tag in SVG_OPEN.findall(body) if 'class="chart chart-' in tag]

    assert len(charts) == 4
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
    assert "series-3" in body
    assert not re.search(r'(?:fill|stroke)="(#|rgb|hsl)', body)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", body)


def test_every_kpi_delta_pairs_an_arrow_with_hidden_words(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    blocks = delta_blocks(body)

    assert len(blocks) >= 3
    assert body.count('class="delta delta-') == len(blocks)
    for kind, arrow, words in blocks:
        assert kind in {"up", "down", "flat"}
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


def test_the_blood_pressure_card_states_the_shared_axis_and_names_the_range(signed_in):
    response = signed_in.get(reverse("dashboards"), {"range": "90d"})
    body = content_of(response)
    chart = response.context["blood_pressure"]["chart"]

    assert "All three series share one axis" in body
    assert response.context["period_label"] in chart["aria_label"]
    assert [line["label"] for line in chart["lines"]] == ["Systolic", "Diastolic", "Pulse"]
    assert [line["class"] for line in chart["lines"]] == ["series-1", "series-2", "series-3"]
    assert [item["unit"] for item in response.context["blood_pressure"]["legend"]] == [
        "mmHg",
        "mmHg",
        "/min",
    ]


def test_the_weight_card_shades_the_week_in_progress(signed_in):
    response = signed_in.get(reverse("dashboards"), {"range": "90d"})
    body = content_of(response)

    assert 'class="chart-bar bar-partial"' in body
    assert "a week still in progress" in body
    assert response.context["weight"]["has_line"] is True


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
    assert "72.5" in body
    assert '<th scope="col">Unit</th>' in body
    assert "Highest" in body
    assert "Lowest" in body
    assert "Latest" in body
    back = body.split('class="report-back"', 1)[1].split("</p>", 1)[0]
    assert "range=90d" in back


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


def test_the_print_script_opens_the_data_tables():
    script = Path(finders.find("records/print.js")).read_text(encoding="utf-8")

    assert "[data-print-open]" in script
    assert "beforeprint" in script
    assert "afterprint" in script


def test_the_builders_return_the_whole_page_without_a_request(owner):
    today = timezone.localdate()
    context = build_dashboards(owner, {}, today)

    assert context["range_key"] == "30d"
    assert context["period_label"].endswith("2026")
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
