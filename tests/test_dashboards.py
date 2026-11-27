import html
import math
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
from records.reports import (
    CARD_ROW_LIMIT,
    METRIC_DOMAINS,
    RANGE_KEYS,
    TALL_CHART_HEIGHT,
    WEEKDAYS,
    _bp_category,
    _fetch_window,
    _history,
    _rolling_average,
    _tick_bucket_label,
    build_dashboard_consistency,
    build_dashboard_weight,
    build_dashboards,
    build_metric_dashboard,
)

SPAN = 60
STEP = 3
CSRF = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')
COORDINATES = re.compile(r'\b(?:x|y|width|height|cx|cy|dx|dy|points)="([^"]*)"')
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
    start = body.index('class="heatmap-grid"')
    end = body.index('class="heat-legend"')
    return body[start:end]


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


def bp_url() -> str:
    return reverse("dashboard_metric", kwargs={"metric": PANEL})


def chart_pages() -> list[str]:
    return [
        reverse("dashboard_weight"),
        reverse("dashboard_consistency"),
        bp_url(),
        reverse("dashboard_metric", kwargs={"metric": "body_weight"}),
    ]


@pytest.mark.django_db
def test_the_first_visit_sends_anonymous_visitors_to_the_login_page(client):
    response = client.get(reverse("dashboards"))

    assert response.status_code == 302
    assert reverse("login") in response.url


def test_the_nav_marks_dashboards_active_on_every_dashboard_page(signed_in):
    pages = [
        reverse("dashboards"),
        reverse("dashboard_weight"),
        reverse("dashboard_consistency"),
        bp_url(),
        reverse("dashboard_metric", kwargs={"metric": "body_weight"}),
        reverse("dashboard_metric", kwargs={"metric": "pulse"}),
    ]
    for page in pages:
        body = content_of(signed_in.get(page))
        assert 'href="/dashboards/" class="active" aria-current="page"' in body

    body = content_of(signed_in.get(reverse("dashboards")))
    assert body.count('aria-current="page"') == 2
    drill = content_of(signed_in.get(reverse("dashboard_metric", kwargs={"metric": "pulse"})))
    assert drill.count('aria-current="page"') == 1


def test_the_sub_nav_lists_the_sections_and_marks_the_active_one(signed_in):
    cases = [
        (reverse("dashboards"), "Overview"),
        (bp_url(), "Blood pressure"),
        (reverse("dashboard_weight"), "Weight &amp; composition"),
        (reverse("dashboard_consistency"), "Consistency"),
        (reverse("dashboard_metric", kwargs={"metric": "body_weight"}), "Weight &amp; composition"),
        (
            reverse("dashboard_metric", kwargs={"metric": "body_fat_pct"}),
            "Weight &amp; composition",
        ),
        (reverse("dashboard_metric", kwargs={"metric": "pulse"}), None),
    ]
    for page, active in cases:
        body = content_of(signed_in.get(page, {"range": "90d", "metric": "impedance"}))
        nav = body.split('aria-label="Dashboard sections">', 1)[1].split("</nav>", 1)[0]
        for label in ("Overview", "Blood pressure", "Weight &amp; composition", "Consistency"):
            assert f">{label}</a>" in nav
        marked = re.findall(
            r'<a class="segmented-item is-current" href="([^"]+)" aria-current="page">([^<]+)</a>',
            nav,
        )
        if active is None:
            assert marked == []
        else:
            assert len(marked) == 1
            assert marked[0][1] == active
        for href in re.findall(r'href="([^"]+)"', nav):
            assert "range=90d" in href
            assert "metric=impedance" in href


def test_the_dedicated_paths_beat_the_metric_slug(signed_in):
    for name in ("dashboard_weight", "dashboard_consistency"):
        response = signed_in.get(reverse(name))

        assert response.status_code == 200
        assert response.resolver_match.url_name == name


def test_the_new_pages_return_200_with_their_cards(signed_in):
    weight = signed_in.get(reverse("dashboard_weight"), {"range": "90d"})
    weight_body = content_of(weight)
    assert weight.status_code == 200
    assert weight.context["weight"]["has_points"] is True
    assert weight.context["composition"]["has_points"] is True
    assert weight_body.count('class="chart chart-lines"') == 2
    assert 'role="group" aria-label="Body composition metric"' in weight_body
    assert "Line: 7-day rolling average." in weight_body
    assert 'class="heatmap-grid"' not in weight_body

    consistency = signed_in.get(reverse("dashboard_consistency"), {"range": "90d"})
    consistency_body = content_of(consistency)
    assert consistency.status_code == 200
    assert consistency.context["consistency"]["has_heatmap"] is True
    assert consistency.context["weekly"]["has_points"] is True
    assert 'class="heatmap-grid"' in consistency_body
    assert consistency.context["weekday"]["has_points"] is True
    assert consistency_body.count('class="chart chart-bars"') == 2
    assert consistency_body.count('class="chart chart-lines"') == 0


def test_the_weight_page_stacks_its_two_cards_full_width_like_the_blood_pressure_page(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_weight"), {"range": "90d"}))

    assert 'class="report-grid"' not in body
    assert body.count('class="card report-card"') == 2
    assert body.index("<h2>Weight</h2>") < body.index("<h2>Body composition</h2>")
    assert body.count('viewBox="0 0 720 380"') == 2


def test_the_blood_pressure_metric_page_renders_the_real_panel(signed_in):
    response = signed_in.get(bp_url(), {"range": "90d"})
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["has_points"] is True
    assert response.context["range_empty"] is False
    assert "No blood pressure readings" not in body
    assert body.count('class="chart chart-lines"') == 2
    assert "chart-band" not in body
    assert 'class="chart-dot series-1 cat-elevated"' in body
    assert 'class="chart-dot series-1 cat-stage1"' in body
    assert 'class="chart-dot series-2 cat-ok"' in body
    assert '<th scope="col" class="num">Systolic</th>' in body
    assert "126/78 mmHg" in body
    assert 'class="segmented-item is-current" href="?range=90d" aria-current="true">' in body
    card = response.context["blood_pressure"]
    assert [line["label"] for line in card["chart"]["lines"]] == [
        "Systolic",
        "Diastolic",
        "Systolic 7-day average",
        "Diastolic 7-day average",
    ]


@pytest.mark.django_db
def test_the_blood_pressure_page_shows_the_notice_when_the_range_has_none(client, db):
    user = get_user_model().objects.create_user(username="bp-stale")
    today = timezone.localdate()
    at = moment(today - timedelta(days=400))
    panel = Measurement.objects.create(owner=user, metric=PANEL, at=at, unit="", note="")
    for metric, value in (("systolic_bp", 128), ("diastolic_bp", 80), ("pulse", 70)):
        row = measurement(user, metric, at, value)
        row.parent = panel
        Measurement.objects.create(
            owner=user, metric=metric, at=at, value=Decimal(str(value)), unit=METRICS[metric].unit
        )
    Measurement.objects.create(
        owner=user,
        metric="body_weight",
        at=moment(today),
        value=Decimal("72.0"),
        unit="kg",
        note="",
    )
    client.force_login(user)

    response = client.get(bp_url(), {"range": "30d"})
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["range_empty"] is True
    assert "No blood pressure readings in" in body
    assert "Show all time" in body


def test_the_overview_keeps_the_kpis_and_links_the_three_section_pages(signed_in):
    response = signed_in.get(reverse("dashboards"))
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["has_data"] is True
    assert 'class="stat-grid report-kpis"' in body
    assert "Extremes" in body
    assert 'class="dashboard-nav"' in body
    for label in ("Blood pressure", "Weight &amp; composition", "Consistency"):
        assert label in body
    assert 'href="/dashboards/blood_pressure/?range=30d&amp;metric=body_fat_pct"' in body
    assert 'href="/dashboards/weight/?range=30d&amp;metric=body_fat_pct"' in body
    assert 'href="/dashboards/consistency/?range=30d&amp;metric=body_fat_pct"' in body
    assert body.count('class="chart chart-') == 0
    assert 'class="heatmap-grid"' not in body
    sections = response.context["sections"]
    assert [section["key"] for section in sections] == ["blood_pressure", "weight", "consistency"]
    assert sections[0]["summary"] == "Latest 126/78 mmHg"
    assert sections[0]["extra"] == "AHA category: Elevated"
    assert sections[1]["summary"] == "Latest 72.5 kg"
    assert sections[2]["summary"].startswith("90 readings in")


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
    options = group(body, "Date range")

    assert response.status_code == 200
    assert response.context["range_key"] == key
    if key == "custom":
        button = re.search(
            r'<button type="button" popovertarget="custom-range-popover" aria-expanded="false" '
            r'class="segmented-item is-current" aria-current="true">([^<]+)</button>',
            options,
        )
        assert button
        assert button.group(1).startswith("Custom")
    else:
        assert (
            f'class="segmented-item is-current" href="?range={key}" aria-current="true">' in options
        )
        current = re.findall(r'class="segmented-item is-current" href="\?range=([^"&]+)', options)
        assert current == [key]


def test_an_unknown_range_key_falls_back_to_the_default(signed_in):
    response = signed_in.get(reverse("dashboards"), {"range": "forever"})

    assert response.status_code == 200
    assert response.context["range_key"] == "30d"


def custom_params(today, back=13, to_back=4):
    return {
        "range": "custom",
        "from": (today - timedelta(days=back)).isoformat(),
        "to": (today - timedelta(days=to_back)).isoformat(),
    }


def _custom(from_day, to_day):
    return {"range": "custom", "from": from_day.isoformat(), "to": to_day.isoformat()}


def test_the_custom_button_and_popover_are_on_every_dashboard_page(signed_in):
    today = timezone.localdate()
    params = custom_params(today)
    for page in [reverse("dashboards"), *chart_pages()]:
        response = signed_in.get(page, params)
        body = content_of(response)

        assert response.status_code == 200
        assert response.context["range_key"] == "custom"
        options = group(body, "Date range")
        button = re.search(
            r'<button type="button" popovertarget="custom-range-popover" aria-expanded="false" '
            r'class="segmented-item is-current" aria-current="true">([^<]+)</button>',
            options,
        )
        assert button
        assert button.group(1) == f"Custom · {response.context['range']['label']}"
        assert '<div id="custom-range-popover" popover class="range-popover">' in body
        popover = body.split('id="custom-range-popover"', 1)[1].split("</form>", 1)[0]
        assert 'method="get"' in popover
        assert '<input type="hidden" name="range" value="custom">' in popover
        assert (
            f'<input id="range-from" type="date" name="from" value="{params["from"]}">' in popover
        )
        assert f'<input id="range-to" type="date" name="to" value="{params["to"]}">' in popover
        assert ">Apply</button>" in popover
        assert popover.count('<div class="range-field">') == 2
        assert '<button class="btn btn-primary btn-sm" type="submit">Apply</button>' in popover
        bar = body.split('class="report-toolbar filters"', 1)[1].split(
            'id="custom-range-popover"', 1
        )[0]
        assert 'type="date"' not in bar


def test_submitting_the_popover_form_applies_the_chosen_window(signed_in):
    today = timezone.localdate()
    page = signed_in.get(reverse("dashboards"), {"range": "90d"})
    popover = content_of(page).split('id="custom-range-popover"', 1)[1].split("</form>", 1)[0]
    fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', popover))
    assert fields["range"] == "custom"
    date_from = today - timedelta(days=20)
    date_to = today - timedelta(days=6)
    fields.update({"from": date_from.isoformat(), "to": date_to.isoformat()})

    response = signed_in.get(reverse("dashboards"), fields)

    assert response.status_code == 200
    assert response.context["range_key"] == "custom"
    assert response.context["range"]["key"] == "custom"
    assert response.context["range"]["start"] == date_from
    assert response.context["range"]["end"] == date_to


def test_the_custom_form_keeps_the_metric_param_only_when_present(signed_in):
    body = content_of(
        signed_in.get(reverse("dashboard_weight"), {"range": "30d", "metric": "impedance"})
    )
    form = body.split('id="custom-range-popover"', 1)[1].split("</form>", 1)[0]
    assert '<input type="hidden" name="metric" value="impedance">' in form

    body = content_of(signed_in.get(reverse("dashboards"), {"range": "30d"}))
    form = body.split('id="custom-range-popover"', 1)[1].split("</form>", 1)[0]
    assert 'name="metric"' not in form

    drill = content_of(
        signed_in.get(reverse("dashboard_metric", kwargs={"metric": "pulse"}), {"range": "90d"})
    )
    form = drill.split('id="custom-range-popover"', 1)[1].split("</form>", 1)[0]
    assert 'name="metric"' not in form


def test_the_popover_dates_default_to_the_current_window_when_switching_from_another_range(
    signed_in,
):
    today = timezone.localdate()
    body = content_of(signed_in.get(reverse("dashboards"), {"range": "90d"}))
    popover = body.split('id="custom-range-popover"', 1)[1].split("</form>", 1)[0]

    assert (
        f'<input id="range-from" type="date" name="from" value="{(today - timedelta(days=89)).isoformat()}">'
        in popover
    )
    assert f'<input id="range-to" type="date" name="to" value="{today.isoformat()}">' in popover


def test_the_other_pills_drop_from_and_to(signed_in):
    today = timezone.localdate()
    body = content_of(signed_in.get(reverse("dashboards"), custom_params(today)))
    options = group(body, "Date range")

    assert re.search(
        r'<button type="button" popovertarget="custom-range-popover" aria-expanded="false" class="segmented-item',
        options,
    )
    hrefs_found = re.findall(r'href="([^"]+)"', options)
    assert len(hrefs_found) == len(RANGE_KEYS) - 1
    for href in hrefs_found:
        params = query_of(html.unescape(href))
        assert params["range"] != ["custom"]
        assert "from" not in params
        assert "to" not in params


def test_the_custom_range_resolves_on_the_page_and_names_the_previous_period(signed_in):
    today = timezone.localdate()
    date_from = today - timedelta(days=13)
    date_to = today - timedelta(days=4)
    response = signed_in.get(reverse("dashboards"), custom_params(today))
    context = response.context

    assert context["range_key"] == "custom"
    assert context["range"]["key"] == "custom"
    assert context["range"]["start"] == date_from
    assert context["range"]["end"] == date_to
    assert context["range"]["previous"] == {
        "start": date_from - timedelta(days=10),
        "end": date_from - timedelta(days=1),
    }
    assert context["period_label"].endswith(str(date_to.year))
    assert "versus the previous period" in content_of(response)


def test_a_custom_end_in_the_future_is_clamped_to_today(signed_in):
    today = timezone.localdate()
    params = {
        "range": "custom",
        "from": (today - timedelta(days=9)).isoformat(),
        "to": (today + timedelta(days=30)).isoformat(),
    }
    response = signed_in.get(reverse("dashboards"), params)

    assert response.context["range"]["key"] == "custom"
    assert response.context["range"]["end"] == today
    assert f'<input id="range-to" type="date" name="to" value="{today.isoformat()}">' in content_of(
        response
    )


def test_a_broken_custom_range_falls_back_to_the_default_window(signed_in):
    today = timezone.localdate()
    cases = [
        {"range": "custom"},
        {"range": "custom", "from": "not-a-date", "to": (today - timedelta(days=5)).isoformat()},
        {
            "range": "custom",
            "from": (today - timedelta(days=2)).isoformat(),
            "to": (today - timedelta(days=5)).isoformat(),
        },
        {
            "range": "custom",
            "from": (today + timedelta(days=3)).isoformat(),
            "to": (today + timedelta(days=9)).isoformat(),
        },
    ]
    for params in cases:
        response = signed_in.get(reverse("dashboards"), params)

        assert response.context["range_key"] == "custom", params
        assert response.context["range"]["key"] == "30d", params
        assert response.context["range"]["start"] == today - timedelta(days=29), params
        assert response.context["range"]["end"] == today, params


def test_the_consistency_cards_work_for_a_custom_window(signed_in):
    today = timezone.localdate()
    date_from = today - timedelta(days=40)
    response = signed_in.get(
        reverse("dashboard_consistency"),
        {"range": "custom", "from": date_from.isoformat(), "to": today.isoformat()},
    )
    body = content_of(response)

    assert response.status_code == 200
    assert response.context["range"]["key"] == "custom"
    assert response.context["weekly"]["has_points"] is True
    assert response.context["consistency"]["has_heatmap"] is True
    assert 'class="heatmap-grid"' in body
    assert 'class="chart chart-bars"' in body
    bars = response.context["weekly"]["chart"]["bars"]
    assert bars[-1]["in_progress"] is True
    assert any(bar["partial"] for bar in bars)


def test_a_custom_range_across_a_year_boundary_labels_january_ticks_with_the_year(client, db):
    user = get_user_model().objects.create_user(username="custom-years")
    for day, weight in ((date(2025, 12, 20), "72.0"), (date(2026, 1, 10), "71.5")):
        Measurement.objects.create(
            owner=user,
            metric="body_weight",
            at=moment(day),
            value=Decimal(weight),
            unit="kg",
            note="",
        )

    across = build_dashboards(
        user, {"range": "custom", "from": "2025-12-20", "to": "2026-01-10"}, date(2026, 1, 10)
    )
    labels = [label["label"] for label in across["weight"]["chart"]["xlabels"]]

    assert "3 Jan 2026" in labels
    assert "20 Dec" in labels
    assert all("2025" not in label for label in labels if "Jan" not in label)


def test_the_last_seven_days_chip_is_offered_first_and_custom_last(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))
    options_group = group(body, "Date range")
    options = re.findall(r'href="\?range=([^"&]+)[^"]*"[^>]*>([^<]+)</a>', options_group)

    assert [key for key, _ in options] == list(RANGE_KEYS)[:-1]
    assert options[0] == ("7d", "Last 7 days")
    assert options[-1] == ("all", "All time")
    assert re.search(
        r'<button type="button" popovertarget="custom-range-popover"[^>]*>Custom</button>',
        options_group,
    )

    response = signed_in.get(reverse("dashboards"), {"range": "7d"})
    today = timezone.localdate()

    assert response.status_code == 200
    assert response.context["range_key"] == "7d"
    assert response.context["range"]["start"] == today - timedelta(days=6)
    assert response.context["range"]["end"] == today
    assert response.context["range"]["previous"] == {
        "start": today - timedelta(days=13),
        "end": today - timedelta(days=7),
    }
    assert "versus the previous 7 days" in content_of(response)


def test_the_dedicated_pages_render_the_taller_chart_frame(signed_in):
    for page in chart_pages():
        body = content_of(signed_in.get(page))

        assert 'viewBox="0 0 720 380"' in body
        assert 'viewBox="0 0 720 240"' not in body

    overview = content_of(signed_in.get(reverse("dashboards")))
    assert 'class="chart chart-' not in overview


def test_range_and_metric_links_preserve_each_other(signed_in):
    body = content_of(
        signed_in.get(reverse("dashboard_weight"), {"range": "1y", "metric": "impedance"})
    )

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

    body = content_of(client.get(bp_url(), {"range": "all"}))

    assert '<circle class="chart-dot series-1 cat-' in body
    assert '<rect class="chart-dot series-2 cat-' in body
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
    charts = []
    for page in chart_pages():
        body = content_of(signed_in.get(page))
        charts += [tag for tag in SVG_OPEN.findall(body) if 'class="chart chart-' in tag]

    assert len(charts) == 7
    for tag in charts:
        assert 'role="img"' in tag
        label = tag.split('aria-label="', 1)[1].split('"', 1)[0]
        assert label.strip()
        assert "nan" not in label.lower()
        assert "none" not in label.lower()


def range_params(today):
    for key in RANGE_KEYS:
        if key == "custom":
            yield _custom(today - timedelta(days=13), today - timedelta(days=4))
        else:
            yield {"range": key}


def test_no_chart_attribute_carries_nan_inf_or_none(signed_in):
    today = timezone.localdate()
    for page in [reverse("dashboards"), *chart_pages()]:
        for params in range_params(today):
            body = content_of(signed_in.get(page, params))
            for match in COORDINATES.finditer(body):
                value = match.group(1).lower()
                assert "nan" not in value
                assert "inf" not in value
                assert "none" not in value


def test_every_chart_coordinate_is_numeric(signed_in):
    checked = 0
    for page in chart_pages():
        body = content_of(signed_in.get(page))
        for match in COORDINATES.finditer(body):
            for token in match.group(1).replace(",", " ").split():
                assert float(token) == float(token)
                checked += 1
    assert checked > 100


def test_heatmap_cells_are_whole_weeks_of_the_resolved_range(signed_in):
    response = signed_in.get(reverse("dashboard_consistency"), {"range": "all"})
    body = content_of(response)
    grid = heatmap_grid(body)
    weeks = response.context["consistency"]["weeks"]

    assert grid.count("heat-cell") == 7 * len(weeks)
    assert "is-outside" in grid
    assert "Mon" in body
    assert "Sun" in body
    assert "Less" in body
    assert "More" in body


def test_the_heatmap_follows_the_resolved_range(signed_in):
    today = timezone.localdate()

    week = signed_in.get(reverse("dashboard_consistency"), {"range": "7d"})
    consistency = week.context["consistency"]
    assert 1 <= len(consistency["weeks"]) <= 2
    assert consistency["window_label"] == week.context["range"]["label"]
    assert consistency["readings"] == week.context["readings_total"]
    weekly = week.context["weekly"]
    assert len(weekly["chart"]["bars"]) == len(week.context["range"]["partial"])
    assert weekly["chart"]["bars"][-1]["in_progress"] is True
    body = content_of(week)
    assert f"{consistency['window_label']} · {consistency['days']} days with readings" in body

    month = signed_in.get(reverse("dashboard_consistency"), {"range": "30d"})
    consistency = month.context["consistency"]
    assert 4 <= len(consistency["weeks"]) <= 6
    assert consistency["window_label"] == month.context["range"]["label"]
    assert consistency["readings"] == month.context["readings_total"]

    everything = signed_in.get(reverse("dashboard_consistency"), {"range": "all"})
    consistency = everything.context["consistency"]
    assert consistency["window_label"] == everything.context["range"]["label"]
    assert consistency["days"] == SPAN // STEP
    assert consistency["readings"] == everything.context["readings_total"]
    first = min(week["cells"][0]["date"] for week in consistency["weeks"])
    last = max(week["cells"][-1]["date"] for week in consistency["weeks"])
    assert first <= today - timedelta(days=SPAN - 1)
    assert last >= today


def test_the_heatmap_week_headings_never_collide_on_small_grids(signed_in):
    def labeled_columns(response):
        weeks = response.context["consistency"]["weeks"]
        step = max(1, math.ceil(len(weeks) / 8))
        if len(weeks) <= 6:
            step = max(step, 2)
        labeled = [column for column, week in enumerate(weeks) if week["label"]]
        assert labeled == list(range(0, len(weeks), step))
        return weeks, labeled

    week = signed_in.get(reverse("dashboard_consistency"), {"range": "7d"})
    weeks, labeled = labeled_columns(week)
    assert 1 <= len(weeks) <= 2
    if len(weeks) == 2:
        assert labeled == [0]
    assert all(later - earlier >= 2 for earlier, later in zip(labeled, labeled[1:], strict=False))

    month = signed_in.get(reverse("dashboard_consistency"), {"range": "30d"})
    weeks, labeled = labeled_columns(month)
    assert 4 <= len(weeks) <= 6
    assert all(later - earlier >= 2 for earlier, later in zip(labeled, labeled[1:], strict=False))


def test_the_heatmap_grid_wraps_each_week_in_a_stretchable_column(signed_in):
    for params in ({"range": "30d"}, {"range": "all"}):
        response = signed_in.get(reverse("dashboard_consistency"), params)
        body = content_of(response)
        weeks = response.context["consistency"]["weeks"]

        grid = heatmap_grid(body)
        assert grid.count('<div class="heatmap-col">') == len(weeks)
        assert grid.count("heat-cell") == 7 * len(weeks)
        if params["range"] == "30d":
            assert 4 <= len(weeks) <= 6
        else:
            assert len(weeks) > 6


def test_the_pulse_axis_uses_integer_ticks(signed_in):
    response = signed_in.get(bp_url(), {"range": "7d"})
    ticks = response.context["blood_pressure"]["pulse_chart"]["ticks"]

    assert ticks
    assert all(re.fullmatch(r"\d+ /min", tick["label"]) for tick in ticks)


def test_the_custom_button_carries_aria_expanded(signed_in):
    body = content_of(signed_in.get(reverse("dashboards")))

    assert (
        '<button type="button" popovertarget="custom-range-popover" aria-expanded="false" '
        'class="segmented-item">' in body
    )


def test_only_the_current_week_bar_is_shaded(signed_in):
    today = timezone.localdate()

    week = signed_in.get(reverse("dashboard_consistency"), {"range": "7d"})
    bars = week.context["weekly"]["chart"]["bars"]
    assert bars
    assert [bar["partial"] for bar in bars[:-1]] == [False] * (len(bars) - 1)
    assert bars[-1]["partial"] is True
    assert bars[-1]["in_progress"] is True
    weekly_section = content_of(week).split("Readings per week<", 1)[1].split("</section>", 1)[0]
    assert weekly_section.count("bar-partial") == 1
    assert "Shaded bar: the current week, still in progress." in weekly_section

    past = signed_in.get(
        reverse("dashboard_consistency"),
        {
            "range": "custom",
            "from": (today - timedelta(days=40)).isoformat(),
            "to": (today - timedelta(days=20)).isoformat(),
        },
    )
    bars = past.context["weekly"]["chart"]["bars"]
    assert bars
    assert all(bar["partial"] is False for bar in bars)
    assert all(bar["in_progress"] is False for bar in bars)
    past_section = content_of(past).split("Readings per week<", 1)[1].split("</section>", 1)[0]
    assert "bar-partial" not in past_section

    month = signed_in.get(reverse("dashboard_consistency"), {"range": "30d"})
    bars = month.context["weekly"]["chart"]["bars"]
    assert [bar["partial"] for bar in bars[:-1]] == [False] * (len(bars) - 1)
    assert bars[-1]["partial"] is True


def test_the_heatmap_card_is_full_width_with_thinned_week_headings_above_the_grid(signed_in):
    response = signed_in.get(reverse("dashboard_consistency"), {"range": "all"})
    body = content_of(response)
    weeks = response.context["consistency"]["weeks"]

    assert body.count('<section class="card report-card">') == 3
    heatmap_card = body.split('<section class="card report-card">', 1)[1].split("</section>", 1)[0]
    assert 'class="heatmap-scroll"' in heatmap_card
    assert 'class="report-grid"' not in heatmap_card
    assert body.index('class="heatmap-scroll"') < body.index('<div class="report-grid">')

    headings = re.search(r'<div class="heatmap-weeks"[^>]*>(.*?)</div>', body, re.S).group(1)
    assert headings.count("<span>") == len(weeks)
    labeled = [column for column, week in enumerate(weeks) if week["label"]]
    step = max(1, math.ceil(len(weeks) / 8))
    if len(weeks) <= 6:
        step = max(step, 2)
    assert labeled == list(range(0, len(weeks), step))
    multi_year = weeks[0]["cells"][0]["date"].year != weeks[-1]["cells"][-1]["date"].year
    for column in labeled:
        monday = weeks[column]["cells"][0]["date"]
        expected = _tick_bucket_label(monday, "day", multi_year)
        assert weeks[column]["label"] == expected
        assert f"<span>{expected}</span>" in headings


def test_the_weekly_and_weekday_cards_sit_side_by_side_in_one_grid(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_consistency"), {"range": "30d"}))
    grid = body.split('<div class="report-grid">', 1)[1]

    assert "Readings per week" in grid
    assert "Readings per weekday" in grid
    assert grid.count('<section class="card report-card">') == 2
    assert 'class="chart chart-bars"' in grid


def test_the_readings_per_weekday_card_charts_mon_to_sun_counts(signed_in, owner):
    response = signed_in.get(reverse("dashboard_consistency"), {"range": "30d"})
    weekday = response.context["weekday"]
    resolved = response.context["range"]
    body = content_of(response)

    assert weekday["has_points"] is True
    assert len(weekday["chart"]["bars"]) == 7
    assert [label["label"] for label in weekday["chart"]["xlabels"]] == list(WEEKDAYS)
    assert weekday["chart"]["aria_label"].startswith("Readings per weekday from ")
    titles = [bar["title"] for bar in weekday["chart"]["bars"]]
    counts = [int(re.fullmatch(r"\w+: (\d+) readings?", title).group(1)) for title in titles]
    assert [title.split(":")[0] for title in titles] == list(WEEKDAYS)

    start, end = resolved["start"], resolved["end"]
    series = _history(owner, _fetch_window(resolved))
    expected = [0] * len(WEEKDAYS)
    for metric, points in series.items():
        if metric == PANEL:
            continue
        for point in points:
            if start <= point["date"] <= end:
                expected[point["date"].weekday()] += 1
    assert counts == expected
    assert sum(counts) == response.context["readings_total"] == weekday["total"]
    assert weekday["rows_total"] == 7
    assert [row["date"] for row in weekday["rows"]] == list(WEEKDAYS)
    assert weekday["rows_url"] == "/readings/"

    section = body.split("Readings per weekday", 1)[1].split("</section>", 1)[0]
    assert 'aria-label="Readings per weekday from ' in section
    assert re.search(rf"<title>Mon: {counts[0]} readings?</title>", section)
    assert "Readings per weekday for " in section


def test_the_heatmap_table_fallback_holds_the_same_counts(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_consistency")))
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
    body = content_of(signed_in.get(bp_url()))

    assert "series-1" in body
    assert "series-2" in body
    assert "series-1 avg" in body
    assert not re.search(r'(?:fill|stroke)="(#|rgb|hsl)', body)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", body)


def test_the_rendered_pages_carry_no_raw_floats(signed_in):
    today = timezone.localdate()
    for page in [reverse("dashboards"), *chart_pages()]:
        for params in range_params(today):
            body = content_of(signed_in.get(page, params))

            assert not RAW_FLOAT.search(body), (page, params)


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
    assert [card["label"] for card in context["kpis"]] == [
        "Latest blood pressure",
        "7-day average",
        "Latest weight",
        "Weight change",
        "Readings",
        "Last import",
    ]
    assert "Readings in range" not in content_of(signed_in.get(reverse("dashboards")))


def test_the_blood_pressure_card_draws_two_panels_with_coloured_dots_and_a_category(signed_in):
    response = signed_in.get(bp_url(), {"range": "90d"})
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
    assert "bands" not in chart
    assert {dot["class"] for dot in chart["lines"][0]["dots"]} == {"cat-elevated", "cat-stage1"}
    assert all(dot["class"].startswith("cat-") for dot in chart["lines"][0]["dots"])
    assert all(dot["class"].startswith("cat-") for dot in chart["lines"][1]["dots"])
    assert [(item["class"], item["label"]) for item in card["category_legend"]] == [
        ("cat-ok", "Normal <120 mmHg"),
        ("cat-elevated", "Elevated 120–129 mmHg"),
        ("cat-stage1", "Stage 1 130–139 mmHg"),
        ("cat-danger", "Stage 2 ≥140 mmHg"),
    ]
    assert [item["unit"] for item in card["legend"]] == ["mmHg"] * 4
    assert card["pulse_chart"]["lines"][0]["label"] == "Pulse"
    assert [item["unit"] for item in card["pulse_legend"]] == ["/min"]
    assert card["pulse_chart"]["xlabels"] == chart["xlabels"]
    assert response.context["period_label"] in chart["aria_label"]
    assert "All three series share one axis" not in body
    assert "135/85" in body
    assert "coloured by the AHA/ACC 2017 category" in body
    assert '<span class="legend-swatch legend-dot cat-danger"></span>Stage 2 ≥140 mmHg' in body
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

    response = client.get(bp_url())
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


def test_the_blood_pressure_dots_carry_the_aha_tone_and_the_axis_is_not_stretched(client, db):
    user = get_user_model().objects.create_user(username="dot-cats")
    today = timezone.localdate()
    rows = []
    for offset, systolic, diastolic in ((20, 118, 82), (10, 141, 92), (5, 125, 85)):
        at = moment(today - timedelta(days=offset))
        panel = Measurement.objects.create(owner=user, metric=PANEL, at=at, unit="", note="")
        for metric, value in (("systolic_bp", systolic), ("diastolic_bp", diastolic)):
            row = measurement(user, metric, at, value)
            row.parent = panel
            rows.append(row)
    Measurement.objects.bulk_create(rows)
    client.force_login(user)

    response = client.get(bp_url(), {"range": "all"})
    chart = response.context["blood_pressure"]["chart"]
    body = content_of(response)

    assert "bands" not in chart
    assert [dot["class"] for dot in chart["lines"][0]["dots"]] == [
        "cat-ok",
        "cat-danger",
        "cat-elevated",
    ]
    assert [dot["class"] for dot in chart["lines"][1]["dots"]] == [
        "cat-stage1",
        "cat-danger",
        "cat-stage1",
    ]
    assert chart["y_min"] == 80.0
    assert chart["y_max"] == 160.0
    assert [tick["label"] for tick in chart["ticks"]] == ["80 mmHg", "120 mmHg", "160 mmHg"]
    assert '<circle class="chart-dot series-1 cat-danger"' in body
    assert '<rect class="chart-dot series-2 cat-danger"' in body
    assert '<circle class="chart-dot series-1 cat-ok"' in body
    assert "chart-band" not in body
    swatches = [
        (cls, html.unescape(label))
        for cls, label in re.findall(
            r'<span class="legend-swatch legend-dot ([^"]+)"></span>([^<]+)</li>', body
        )
    ]
    assert swatches == [
        ("cat-ok", "Normal <120 mmHg"),
        ("cat-elevated", "Elevated 120–129 mmHg"),
        ("cat-stage1", "Stage 1 130–139 mmHg"),
        ("cat-danger", "Stage 2 ≥140 mmHg"),
    ]


def test_a_single_stage_two_point_gets_a_danger_dot_and_a_data_only_axis(client, db):
    user = get_user_model().objects.create_user(username="one-high")
    today = timezone.localdate()
    at = moment(today)
    panel = Measurement.objects.create(owner=user, metric=PANEL, at=at, unit="", note="")
    rows = []
    for metric, value in (("systolic_bp", 141), ("diastolic_bp", 92)):
        row = measurement(user, metric, at, value)
        row.parent = panel
        rows.append(row)
    Measurement.objects.bulk_create(rows)
    client.force_login(user)

    chart = client.get(bp_url(), {"range": "all"}).context["blood_pressure"]["chart"]

    assert [dot["class"] for dot in chart["lines"][0]["dots"]] == ["cat-danger"]
    assert [dot["class"] for dot in chart["lines"][1]["dots"]] == ["cat-danger"]
    assert chart["y_max"] == 160.0
    assert chart["y_max"] < 180.0


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

    response = client.get(bp_url(), {"range": "all"})
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
    response = signed_in.get(reverse("dashboard_weight"), {"range": "90d"})
    body = content_of(response)
    weight = response.context["weight"]

    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    assert [line["class"] for line in weight["chart"]["lines"]] == ["series-1", "series-2 avg"]
    assert [(item["label"], item["class"]) for item in weight["legend"]] == [
        ("Daily weight", "series-1"),
        ("7-day average", "series-2 avg"),
    ]
    assert len(weight["chart"]["lines"][0]["dots"]) == weight["readings"]
    latest = [dot for dot in weight["chart"]["lines"][0]["dots"] if dot["class"] == "dot-latest"]
    assert len(latest) == 1
    assert latest[0]["title"].startswith("Latest ")
    assert "kg" in latest[0]["title"]
    assert weight["chart"]["y_min"] > 0
    assert "Line: 7-day rolling average. The line breaks across gaps longer than 7 days." in body
    assert "chart-legend" in body
    weight_section = body.split("Weight</h2>", 1)[1].split("</section>", 1)[0]
    assert "bar-partial" not in weight_section
    assert weight["rows"][0]["value"].endswith(" kg")
    assert "average" in weight["rows"][0]


def test_the_seven_day_average_starts_at_the_first_reading(client, db):
    user = get_user_model().objects.create_user(username="six-days")
    today = timezone.localdate()
    weigh_ins(user, today, list(range(6)))
    client.force_login(user)

    weight = client.get(reverse("dashboard_weight"), {"range": "all"}).context["weight"]
    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    average_line = weight["chart"]["lines"][1]
    assert len(average_line["segments"]) == 1
    assert len(average_line["segments"][0].split()) == 6
    assert all(row["average"] == "72.0 kg" for row in weight["rows"])

    weigh_ins(user, today, [6])
    weight = client.get(reverse("dashboard_weight"), {"range": "all"}).context["weight"]
    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    average_line = weight["chart"]["lines"][1]
    assert len(average_line["segments"][0].split()) == 7
    assert weight["rows"][-1]["average"] == "72.0 kg"


def test_the_weight_page_shows_the_average_caption_whenever_the_average_line_is_drawn(signed_in):
    response = signed_in.get(reverse("dashboard_weight"), {"range": "7d"})
    weight = response.context["weight"]
    assert weight["has_average"] is True
    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    assert [(item["label"], item["class"]) for item in weight["legend"]] == [
        ("Daily weight", "series-1"),
        ("7-day average", "series-2 avg"),
    ]
    assert (
        "Line: 7-day rolling average. The line breaks across gaps longer than 7 days."
        in content_of(response)
    )

    drill = signed_in.get(
        reverse("dashboard_metric", kwargs={"metric": "body_weight"}), {"range": "7d"}
    )
    assert [(item["label"], item["class"]) for item in drill.context["legend"]] == [
        ("Daily weight", "series-1"),
        ("7-day average", "series-2 avg"),
    ]
    assert "Line: 7-day rolling average." not in content_of(drill)

    response = signed_in.get(reverse("dashboard_weight"), {"range": "30d"})
    weight = response.context["weight"]
    assert weight["has_average"] is True
    assert [line["label"] for line in weight["chart"]["lines"]] == ["Daily weight", "7-day average"]
    assert (
        "Line: 7-day rolling average. The line breaks across gaps longer than 7 days."
        in content_of(response)
    )


def test_the_rolling_average_covers_every_reading_day_from_the_first_one():
    points = [
        {"date": date(2026, 3, 1), "value": 70.0},
        {"date": date(2026, 3, 3), "value": 74.0},
        {"date": date(2026, 3, 10), "value": 80.0},
    ]

    assert _rolling_average(points) == {
        date(2026, 3, 1): 70.0,
        date(2026, 3, 3): 72.0,
        date(2026, 3, 10): 80.0,
    }
    assert _rolling_average([{"date": date(2026, 3, 1), "value": 70.0}]) == {date(2026, 3, 1): 70.0}


def test_the_seven_day_window_arithmetic_crosses_a_month_boundary():
    today = date(2026, 3, 2)
    points = [
        {"date": today - timedelta(days=offset), "value": float(70 + offset)} for offset in range(7)
    ]
    averages = _rolling_average(points)

    assert set(averages) == {today - timedelta(days=offset) for offset in range(7)}
    assert averages[today] == pytest.approx(sum(range(70, 77)) / 7)
    assert averages[today - timedelta(days=6)] == pytest.approx(76.0)

    sparse = [{"date": date(2026, 2, 20), "value": 80.0}, {"date": date(2026, 3, 2), "value": 72.0}]
    assert _rolling_average(sparse) == {date(2026, 2, 20): 80.0, date(2026, 3, 2): 72.0}


def test_a_single_weigh_in_still_renders_a_dot_and_a_row(client, db):
    user = get_user_model().objects.create_user(username="once")
    weigh_ins(user, timezone.localdate(), [0])
    client.force_login(user)

    response = client.get(reverse("dashboard_weight"), {"range": "all"})
    weight = response.context["weight"]
    body = content_of(response)

    assert weight["has_points"] is True
    assert weight["chart"]["y_max"] > weight["chart"]["y_min"]
    line = weight["chart"]["lines"][0]
    assert len(line["segments"]) == 1
    assert len(line["dots"]) == 1
    assert len(weight["rows"]) == 1
    assert weight["rows"][0]["value"] == "72.0 kg"
    assert weight["rows"][0]["average"] == "72.0 kg"
    assert "72.0 kg" in body


def test_weigh_ins_more_than_a_week_apart_break_the_line_into_segments(client, db):
    user = get_user_model().objects.create_user(username="gappy")
    today = timezone.localdate()
    weigh_ins(user, today, [0, 8, 16, 24, 32, 40, 48])
    client.force_login(user)

    weight = client.get(reverse("dashboard_weight"), {"range": "all"}).context["weight"]
    line = weight["chart"]["lines"][0]

    assert len(line["segments"]) == 7
    assert all(len(segment.split()) == 1 for segment in line["segments"])
    assert len(line["dots"]) == 7


def test_the_weight_table_caps_at_two_hundred_rows_with_a_note_and_a_link(client, db):
    user = get_user_model().objects.create_user(username="daily")
    today = timezone.localdate()
    weigh_ins(user, today, list(range(CARD_ROW_LIMIT + 50)))
    client.force_login(user)

    response = client.get(reverse("dashboard_weight"), {"range": "all"})
    weight = response.context["weight"]
    body = content_of(response)

    assert len(weight["rows"]) == CARD_ROW_LIMIT
    assert weight["rows_total"] == CARD_ROW_LIMIT + 50
    assert weight["rows_url"] == "/readings/?metric=body_weight"
    assert f"Showing the {CARD_ROW_LIMIT} most recent of {CARD_ROW_LIMIT + 50} readings" in body
    assert 'href="/readings/?metric=body_weight"' in body


def test_the_metric_switch_draws_the_combined_chart_for_the_two_percentages(signed_in):
    for metric in ("body_fat_pct", "skeletal_muscle_pct"):
        response = signed_in.get(reverse("dashboard_weight"), {"metric": metric})
        composition = response.context["composition"]

        assert composition["metric"] == metric
        assert composition["combined"] is True
        assert [line["label"] for line in composition["chart"]["lines"]] == [
            "Body fat",
            "Skeletal muscle",
            "Body fat 7-day average",
            "Skeletal muscle 7-day average",
        ]
        assert [line["class"] for line in composition["chart"]["lines"]] == [
            "series-1",
            "series-4",
            "series-1 avg",
            "series-4 avg",
        ]
        assert composition["chart"]["aria_label"].startswith("Body fat and skeletal muscle")
        assert composition["unit"] == "%"
        assert [(item["label"], item["class"]) for item in composition["legend"]] == [
            ("Body fat", "series-1"),
            ("Skeletal muscle", "series-4"),
            ("Body fat 7-day average", "series-1 avg"),
            ("Skeletal muscle 7-day average", "series-4 avg"),
        ]
        assert [link["url"] for link in composition["links"]] == [
            "/dashboards/body_fat_pct/?range=30d",
            "/dashboards/skeletal_muscle_pct/?range=30d",
        ]

    body = content_of(signed_in.get(reverse("dashboard_weight")))
    section = body.split("Body composition", 1)[1].split("</section>", 1)[0]
    assert section.count("<polyline") == 4
    assert 'class="chart-line series-1"' in section
    assert 'class="chart-line series-4"' in section
    assert 'class="chart-line series-1 avg"' in section
    assert 'class="chart-line series-4 avg"' in section
    assert "chart-legend" in section
    assert "Body fat 7-day average (%)" in section
    assert "Skeletal muscle 7-day average (%)" in section
    assert '<th scope="col" class="num">Body fat (%)</th>' in section
    assert '<th scope="col" class="num">Skeletal muscle (%)</th>' in section
    assert 'href="/dashboards/body_fat_pct/?range=30d"' in section
    assert 'href="/dashboards/skeletal_muscle_pct/?range=30d"' in section


def test_the_composition_switcher_offers_one_pill_for_the_combined_chart(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_weight")))
    switcher = group(body, "Body composition metric")
    pills = re.findall(r'<a class="segmented-item[^"]*" href="([^"]+)"[^>]*>([^<]+)</a>', switcher)

    assert [(query_of(href)["metric"][0], label) for href, label in pills] == [
        ("body_fat_pct", "Body fat &amp; muscle"),
        ("visceral_fat", "Visceral fat"),
        ("resting_metabolism", "Resting metabolism"),
        ("impedance", "Impedance"),
    ]
    assert "skeletal_muscle_pct" not in switcher

    for metric in ("body_fat_pct", "skeletal_muscle_pct"):
        response = signed_in.get(reverse("dashboard_weight"), {"metric": metric})
        current = re.findall(
            r'class="segmented-item is-current" href="([^"]+)" aria-current="true">([^<]+)</a>',
            group(content_of(response), "Body composition metric"),
        )
        assert [(query_of(href)["metric"][0], label) for href, label in current] == [
            ("body_fat_pct", "Body fat &amp; muscle")
        ], metric
        assert [(o["key"], o["current"]) for o in response.context["metric_options"]] == [
            ("body_fat_pct", True),
            ("visceral_fat", False),
            ("resting_metabolism", False),
            ("impedance", False),
        ], metric

    for metric in ("visceral_fat", "resting_metabolism", "impedance"):
        response = signed_in.get(reverse("dashboard_weight"), {"metric": metric})
        current = re.findall(
            r'class="segmented-item is-current" href="\?metric=([^"]+)" aria-current="true">',
            group(content_of(response), "Body composition metric"),
        )
        assert current == [metric], metric


def test_the_weight_page_defaults_to_the_combined_fat_and_muscle_chart(signed_in):
    response = signed_in.get(reverse("dashboard_weight"))
    composition = response.context["composition"]

    assert composition["metric"] == "body_fat_pct"
    assert composition["combined"] is True
    assert len(composition["chart"]["lines"]) == 4
    assert composition["chart"]["height"] == TALL_CHART_HEIGHT
    assert all(row["body_fat_pct"] != "—" for row in composition["rows"])
    assert all(row["skeletal_muscle_pct"] != "—" for row in composition["rows"])
    assert composition["rows"][0]["body_fat_pct"].endswith("%")
    assert composition["rows"][0]["skeletal_muscle_pct"].endswith("%")
    assert composition["rows_url"] == "/dashboards/body_fat_pct/?range=30d"
    body = content_of(response)
    assert "Body fat and skeletal muscle are both percentages" in body


def test_the_metric_switch_draws_one_metric_at_a_time_for_the_other_metrics(signed_in):
    for metric in ("resting_metabolism", "visceral_fat", "impedance"):
        response = signed_in.get(reverse("dashboard_weight"), {"metric": metric})
        composition = response.context["composition"]

        assert composition["metric"] == metric
        assert composition["combined"] is False
        assert composition["label"] in composition["chart"]["aria_label"]
        assert [line["label"] for line in composition["chart"]["lines"]] == [
            composition["label"],
            f"{composition['label']} 7-day average",
        ]
        assert [line["class"] for line in composition["chart"]["lines"]] == [
            "series-1",
            "series-2 avg",
        ]
        assert [(item["label"], item["class"]) for item in composition["legend"]] == [
            (composition["label"], "series-1"),
            (f"{composition['label']} 7-day average", "series-2 avg"),
        ]
        assert METRICS[metric].unit == composition["unit"]

    body = content_of(signed_in.get(reverse("dashboard_weight"), {"metric": "resting_metabolism"}))
    section = body.split("Body composition", 1)[1].split("</section>", 1)[0]
    assert section.count("<polyline") == 2
    assert 'class="chart-line series-2 avg"' in section
    assert "Resting metabolism 7-day average (kcal)" in section
    assert "kcal" in section


def test_the_legend_omits_the_parens_when_the_unit_is_empty(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_weight"), {"metric": "visceral_fat"}))
    section = body.split("Body composition", 1)[1].split("</section>", 1)[0]

    assert '<span class="legend-swatch series-1"></span>Visceral fat</li>' in section
    assert (
        '<span class="legend-swatch series-2 avg"></span>Visceral fat 7-day average</li>' in section
    )
    assert "()" not in section

    weighted = content_of(signed_in.get(reverse("dashboard_weight")))
    weight_section = weighted.split("Weight</h2>", 1)[1].split("</section>", 1)[0]
    assert '<span class="legend-swatch series-1"></span>Daily weight (kg)</li>' in weight_section
    assert (
        '<span class="legend-swatch series-2 avg"></span>7-day average (kg)</li>' in weight_section
    )

    bp = content_of(signed_in.get(bp_url()))
    assert '<span class="legend-swatch series-1"></span>Systolic (mmHg)</li>' in bp
    assert (
        '<span class="legend-swatch series-1 avg"></span>Systolic 7-day average (mmHg)</li>' in bp
    )


def _projected_segment(chart, x_max, points):
    height = chart["plot_bottom"] - chart["top"]

    def project(x, y):
        px = chart["left"] + chart["plot_width"] * (x / x_max)
        py = (
            chart["top"]
            + height
            - height * (y - chart["y_min"]) / (chart["y_max"] - chart["y_min"])
        )
        return f"{round(px, 2)},{round(py, 2)}"

    return " ".join(project(x, y) for x, y in points)


def test_the_combined_composition_chart_draws_a_seven_day_average_per_series(signed_in, owner):
    response = signed_in.get(reverse("dashboard_weight"), {"range": "30d"})
    composition = response.context["composition"]
    resolved = response.context["range"]
    start, end = resolved["start"], resolved["end"]
    chart = composition["chart"]
    x_max = (end - start).days
    series = _history(owner, _fetch_window(resolved))

    for index, code in enumerate(("body_fat_pct", "skeletal_muscle_pct")):
        readings = [point for point in series[code] if start <= point["date"] <= end]
        averages = _rolling_average(readings)
        assert averages
        expected = [((day - start).days, value) for day, value in sorted(averages.items())]
        assert chart["lines"][2 + index]["segments"] == [_projected_segment(chart, x_max, expected)]
        assert chart["lines"][2 + index]["class"].endswith("avg")


def test_the_single_composition_card_draws_one_seven_day_average_line(signed_in, owner):
    response = signed_in.get(
        reverse("dashboard_weight"), {"range": "30d", "metric": "visceral_fat"}
    )
    composition = response.context["composition"]
    resolved = response.context["range"]
    start, end = resolved["start"], resolved["end"]
    chart = composition["chart"]
    x_max = (end - start).days
    series = _history(owner, _fetch_window(resolved))

    assert len(chart["lines"]) == 2
    readings = [point for point in series["visceral_fat"] if start <= point["date"] <= end]
    averages = _rolling_average(readings)
    assert averages
    expected = [((day - start).days, value) for day, value in sorted(averages.items())]
    assert chart["lines"][1]["segments"] == [_projected_segment(chart, x_max, expected)]
    assert chart["lines"][1]["class"] == "series-2 avg"


def test_the_chart_dots_and_lines_carry_hover_titles_and_are_focusable(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_weight"), {"range": "30d"}))

    weight_section = body.split("Weight</h2>", 1)[1].split("</section>", 1)[0]
    assert re.search(
        r'<polyline class="chart-line series-1"[^>]*fill="none"><title>Daily weight</title></polyline>',
        weight_section,
    )
    assert re.search(
        r'<polyline class="chart-line series-2 avg"[^>]*fill="none"><title>7-day average</title></polyline>',
        weight_section,
    )
    assert re.search(
        r'<circle class="chart-dot series-1 dot-latest"[^>]*tabindex="0">', weight_section
    )
    weight_titles = re.findall(r"<title>([^<]+)</title>", weight_section)
    assert any(title.endswith("kg") and " — " in title for title in weight_titles)
    assert "Latest " in weight_titles[-1] or any(t.startswith("Latest ") for t in weight_titles)

    composition_section = body.split("Body composition", 1)[1].split("</section>", 1)[0]
    assert re.search(
        r'<polyline class="chart-line series-4 avg"[^>]*fill="none">'
        r"<title>Skeletal muscle 7-day average</title></polyline>",
        composition_section,
    )
    composition_titles = re.findall(r"<title>([^<]+)</title>", composition_section)
    assert any("Body fat" in title and "%" in title for title in composition_titles)
    assert any("Skeletal muscle" in title and "%" in title for title in composition_titles)

    bp_body = content_of(signed_in.get(bp_url(), {"range": "30d"}))
    assert re.search(r"<title>\d+ \w+ \d+ \d+:\d+ — Systolic \d+ mmHg</title>", bp_body)
    assert re.search(r"<title>\d+ \w+ \d+ \d+:\d+ — Diastolic \d+ mmHg</title>", bp_body)
    assert re.search(r'<circle class="chart-dot series-1[^"]*"[^>]*tabindex="0">', bp_body)


def test_the_weekly_bars_carry_the_bucket_label_and_the_count_in_a_title(signed_in):
    body = content_of(signed_in.get(reverse("dashboard_consistency"), {"range": "30d"}))

    titles = re.findall(r"<title>(Week of [^<]+)</title>", body)
    assert titles
    assert all(
        re.fullmatch(r"Week of \d+ \w+ \d+: \d+ readings?(, .+)?", title) for title in titles
    )


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


def test_the_weight_drill_down_charts_daily_weight_and_the_seven_day_average(signed_in):
    response = signed_in.get(
        reverse("dashboard_metric", kwargs={"metric": "body_weight"}), {"range": "90d"}
    )
    body = content_of(response)
    chart = response.context["chart"]

    assert [line["label"] for line in chart["lines"]] == ["Daily weight", "7-day average"]
    assert [line["class"] for line in chart["lines"]] == ["series-1", "series-2 avg"]
    assert [(item["label"], item["class"]) for item in response.context["legend"]] == [
        ("Daily weight", "series-1"),
        ("7-day average", "series-2 avg"),
    ]
    assert 'class="chart-line series-2 avg"' in body
    assert "chart-legend" in body
    assert '<th scope="col" class="num">7-day average</th>' in body
    assert response.context["rows"][0]["average"] == "72.7 kg"
    assert '<td class="num">72.7 kg</td>' in body
    assert any(row["average"] != "—" for row in response.context["rows"])


def test_a_metric_drill_down_without_a_rolling_average_has_no_legend(signed_in):
    response = signed_in.get(
        reverse("dashboard_metric", kwargs={"metric": "pulse"}), {"range": "90d"}
    )

    assert response.context["legend"] is None
    assert [line["label"] for line in response.context["chart"]["lines"]] == ["Pulse"]
    assert "chart-legend" not in content_of(response)
    assert "7-day average" not in content_of(response)


def test_the_drill_down_marks_the_current_range(signed_in):
    for key in RANGE_KEYS:
        response = signed_in.get(
            reverse("dashboard_metric", kwargs={"metric": "pulse"}), {"range": key}
        )
        body = content_of(response)

        assert response.status_code == 200
        if key == "custom":
            assert re.search(
                r'<button type="button" popovertarget="custom-range-popover" aria-expanded="false" '
                r'class="segmented-item is-current" aria-current="true">',
                group(body, "Date range"),
            )
        else:
            assert (
                f'class="segmented-item is-current" href="?range={key}" aria-current="true">'
                in body
            )
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
    panel = client.get(bp_url())
    body = content_of(body_response)

    assert "No weight readings in" in body
    assert "No weight readings in" in content_of(drill)
    assert "72.5" not in body
    assert "72.5" not in content_of(drill)
    assert "72.5" not in content_of(panel)
    assert "61" in content_of(panel)
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
    today = timezone.localdate()
    with CaptureQueriesContext(connection) as captured:
        response = signed_in.get(reverse("dashboards"), {"range": "90d"})

    assert response.status_code == 200
    assert len(captured.captured_queries) < 40

    with CaptureQueriesContext(connection) as captured:
        custom = signed_in.get(reverse("dashboards"), custom_params(today))

    assert custom.status_code == 200
    assert custom.context["range"]["key"] == "custom"
    assert len(captured.captured_queries) < 40


def test_the_printed_pages_hide_the_nav_and_keep_tables_open(signed_in):
    bodies = [content_of(signed_in.get(page)) for page in [reverse("dashboards"), *chart_pages()]]
    body = "\n".join(bodies)
    css = Path(finders.find("records/app.css")).read_text(encoding="utf-8")
    print_block = css[css.index("@media print {") :]

    assert 'class="data-table" data-print-open' in body
    assert body.count("data-print-open") >= 5
    assert all("records/print.js" in page for page in bodies)
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


def test_the_scroll_script_is_loaded_on_every_dashboard_page(signed_in):
    bodies = [content_of(signed_in.get(page)) for page in [reverse("dashboards"), *chart_pages()]]

    assert len(bodies) == 5
    assert all("records/scroll.js" in page for page in bodies)


def test_the_scroll_script_saves_and_restores_the_scroll_position_per_path():
    script = Path(finders.find("records/scroll.js")).read_text(encoding="utf-8")

    assert '"omron-scroll:" + window.location.pathname' in script
    assert "window.scrollY" in script
    assert "sessionStorage.setItem(KEY" in script
    assert "sessionStorage.getItem(KEY)" in script
    assert "sessionStorage.removeItem(KEY)" in script
    assert "window.scrollTo(0, Number(saved) || 0)" in script
    assert 'addEventListener("DOMContentLoaded", restore)' in script
    assert 'addEventListener("pageshow", restore)' in script
    assert "url.pathname === window.location.pathname" in script
    assert 'getElementById("custom-range-popover")' in script
    assert 'addEventListener("toggle"' in script
    assert 'setAttribute("aria-expanded", String(event.newState === "open"))' in script


def test_the_chart_tip_script_is_loaded_on_every_dashboard_page(signed_in):
    bodies = [content_of(signed_in.get(page)) for page in [reverse("dashboards"), *chart_pages()]]

    assert len(bodies) == 5
    assert all("records/chart-tips.js" in page for page in bodies)
    consistency = content_of(signed_in.get(reverse("dashboard_consistency")))
    assert consistency.count('<g class="chart-tip"') == 2
    weight = content_of(signed_in.get(reverse("dashboard_weight")))
    assert weight.count('<g class="chart-tip"') == 2


def test_the_chart_tip_script_shows_and_hides_the_tip_from_the_shape_title():
    script = Path(finders.find("records/chart-tips.js")).read_text(encoding="utf-8")

    assert 'querySelectorAll("svg.chart")' in script
    assert 'querySelector(".chart-tip")' in script
    assert 'addEventListener("mouseover"' in script
    assert 'addEventListener("mouseout"' in script
    assert 'addEventListener("focusin"' in script
    assert 'addEventListener("focusout"' in script
    assert ".chart-dot, .chart-bar, .chart-line" in script
    assert 'querySelector("title")' in script
    assert 'setAttribute("visibility", "visible")' in script
    assert 'setAttribute("visibility", "hidden")' in script
    assert "getBBox()" in script


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
        "dashboard_nav",
        "dashboards_url",
        "kpis",
        "blood_pressure",
        "weight",
        "composition",
        "consistency",
        "weekly",
        "extremes",
        "sections",
    }
    assert [item["key"] for item in context["dashboard_nav"]] == [
        "overview",
        "blood_pressure",
        "weight",
        "consistency",
    ]
    assert context["dashboard_nav"][0]["current"] is True
    assert context["dashboard_nav"][0]["url"] == "/dashboards/?range=30d&metric=body_fat_pct"

    weight_page = build_dashboard_weight(owner, {"range": "90d", "metric": "impedance"}, today)
    assert set(weight_page) >= {"weight", "composition", "dashboard_nav", "dashboards_url"}
    assert weight_page["dashboard_nav"][2]["current"] is True
    assert weight_page["dashboard_nav"][2]["url"] == (
        "/dashboards/weight/?range=90d&metric=impedance"
    )
    assert weight_page["weight"]["has_points"] is True

    consistency_page = build_dashboard_consistency(owner, {}, today)
    assert set(consistency_page) >= {"consistency", "weekly", "dashboard_nav"}
    assert consistency_page["dashboard_nav"][3]["current"] is True
    assert consistency_page["consistency"]["has_heatmap"] is True

    panel_page = build_metric_dashboard(owner, PANEL, {"range": "90d"}, today)
    assert panel_page["blood_pressure"]["has_points"] is True
    assert panel_page["has_points"] is True
    assert panel_page["readings_total"] == panel_page["blood_pressure"]["rows_total"]
    assert panel_page["dashboard_nav"][1]["current"] is True

    drill = build_metric_dashboard(owner, "body_weight", {"range": "all"}, today)
    assert drill["metric_label"] == "Weight"
    assert drill["rows"]
    assert drill["dashboards_url"] == "/dashboards/?range=all&metric=body_fat_pct"
    assert drill["dashboard_nav"][2]["current"] is True
    assert [line["label"] for line in drill["chart"]["lines"]] == [
        "Daily weight",
        "7-day average",
    ]
    assert any(row["average"] != "—" for row in drill["rows"])


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

    response = client.get(reverse("dashboard_weight"), {"metric": "body_fat_pct", "range": "all"})
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

    response = client.get(bp_url(), {"range": "all"})
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

    response = client.get(reverse("dashboard_consistency"), {"range": "all"})
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
    response = signed_in.get(reverse("dashboard_consistency"))
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
