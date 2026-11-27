import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.paginator import Paginator
from django.template.backends.django import get_installed_libraries
from django.test import override_settings
from django.urls import reverse

from omron_data.context_processors import REPOSITORY_URL
from records.templatetags.assets import asset
from records.templatetags.formatting import axis_unit, elided_pages, isodate, isominute, num

TEMPLATES = Path(settings.BASE_DIR) / "templates"
APP_CSS = Path(finders.find("records/app.css"))
STATIC = APP_CSS.parent
LOAD = re.compile(r"{%\s*load\s+([^%]*?)\s*%}")
ASSET_TAG = re.compile(r"{%\s*(?:asset|static)\s+(['\"])([^'\"]+)\1\s*%}")
URL = re.compile(r"https?://[^\s\"'<>)]+")
XMLNS = re.compile(r"xmlns(?::\w+)?=(?:\"[^\"]*\"|'[^']*')")


def template_files() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def static_files() -> list[Path]:
    return sorted(path for path in STATIC.rglob("*") if path.is_file())


def base_source() -> str:
    return (TEMPLATES / "base.html").read_text(encoding="utf-8")


def static_source(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def css_block(source: str, opener: str) -> str:
    start = source.index(opener) + len(opener)
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    raise AssertionError(f"unbalanced block after {opener!r}")


def declarations(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines() if line.strip()]


def test_base_links_the_skip_link_to_the_main_region():
    assert '<a class="skip-link" href="#main">Skip to content</a>' in base_source()


def test_main_is_focusable_so_the_skip_link_lands():
    assert re.search(r'<main[^>]*\bid="main"[^>]*\btabindex="-1"', base_source())


def test_the_navigation_is_labelled_main():
    assert re.search(r'<nav[^>]*\baria-label="Main"', base_source())


def test_the_messages_list_is_a_polite_status_region():
    assert '<ul class="messages" role="status" aria-live="polite">' in base_source()


@pytest.mark.django_db
def test_the_navigation_marks_the_current_page_and_nothing_else(client):
    visitor = get_user_model().objects.create_user(username="alice")
    client.force_login(visitor)

    for name, title in (("readings", "Readings"), ("imports", "Imports"), ("tokens", "Tokens")):
        body = client.get(reverse(name)).content.decode()
        marked = re.findall(
            r'<a href="([^"]+)" class="active" aria-current="page">([^<]+)</a>', body
        )
        assert marked == [(reverse(name), title)]
        assert body.count('aria-current="page"') == 1


def test_the_site_nav_covers_every_dashboards_route_in_the_base_template():
    anchor = re.search(
        r'<a href="\{% url \'dashboards\' %\}"(.*?)>Dashboards</a>', base_source(), re.S
    )

    assert anchor
    for name in ("dashboards", "dashboard_metric", "dashboard_weight", "dashboard_consistency"):
        assert f"url_name == '{name}'" in anchor.group(1)


@pytest.mark.django_db
def test_the_site_nav_marks_dashboards_active_on_the_dedicated_pages(client):
    visitor = get_user_model().objects.create_user(username="navdash")
    client.force_login(visitor)

    for name in ("dashboards", "dashboard_weight", "dashboard_consistency"):
        body = client.get(reverse(name)).content.decode()
        marked = re.findall(
            r'<a href="([^"]+)" class="active" aria-current="page">([^<]+)</a>', body
        )
        assert marked == [("/dashboards/", "Dashboards")], name


def _hex_luminance(value: str) -> float:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    low, high = sorted((_hex_luminance(first), _hex_luminance(second)))
    return (high + 0.05) / (low + 0.05)


def test_the_category_dot_colours_keep_a_3_to_1_contrast_on_the_card_surface():
    css = APP_CSS.read_text(encoding="utf-8")
    blocks = {
        "light": css_block(css, ":root {"),
        "dark-prefers": css_block(
            css_block(css, "@media (prefers-color-scheme: dark) {"),
            ':root:not([data-theme="light"]) {',
        ),
        "dark-pinned": css_block(css, ':root[data-theme="dark"] {'),
        "print-dark": css_block(css_block(css, "@media print {"), ':root[data-theme="dark"] {'),
    }
    for name, block in blocks.items():
        variables = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{3,6});", block))
        for cat_key in ("success", "cat-elevated", "cat-stage1", "danger"):
            ratio = _contrast(variables[cat_key], variables["surface"])
            assert ratio >= 3, (name, cat_key, round(ratio, 2))
    for name, block in blocks.items():
        variables = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{3,6});", block))
        ratio = _contrast(variables["avg-line"], variables["surface"])
        assert ratio >= 3, (name, "avg-line", round(ratio, 2))


def test_the_chart_series_colours_keep_a_3_to_1_contrast_on_the_card_surface():
    css = APP_CSS.read_text(encoding="utf-8")
    blocks = {
        "light": css_block(css, ":root {"),
        "dark-prefers": css_block(
            css_block(css, "@media (prefers-color-scheme: dark) {"),
            ':root:not([data-theme="light"]) {',
        ),
        "dark-pinned": css_block(css, ':root[data-theme="dark"] {'),
        "print-dark": css_block(css_block(css, "@media print {"), ':root[data-theme="dark"] {'),
    }
    for name, block in blocks.items():
        variables = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{3,6});", block))
        for series_key in ("accent", "success", "warning", "series-alt", "avg-line"):
            ratio = _contrast(variables[series_key], variables["surface"])
            assert ratio >= 3, (name, series_key, round(ratio, 2))


def test_every_series_class_has_line_dot_and_legend_rules_with_its_dash_pattern():
    css = APP_CSS.read_text(encoding="utf-8")

    assert ".chart-line.series-4 {\n  stroke-dasharray: 7 4;\n}" in css
    for selector, paint in (
        (".chart-dot.series-4", "fill"),
        (".series-4", "stroke"),
        (".legend-swatch.series-4", "background"),
    ):
        assert re.search(
            rf"^{re.escape(selector)} \{{\n  {paint}: var\(--series-alt\);\n\}}$", css, re.M
        ), selector


def test_the_avg_line_dash_is_pronounced_enough_to_read_at_small_widths():
    css = APP_CSS.read_text(encoding="utf-8")

    avg = css_block(css, ".chart-line.avg {")
    assert "stroke-dasharray: 10 6;" in avg
    assert "stroke-width: 1.5;" in avg
    assert "opacity: 0.85;" in avg


def test_the_toolbar_stays_on_one_scrollable_pills_row_on_narrow_screens():
    css = APP_CSS.read_text(encoding="utf-8")
    mobile = css_block(css, "@media (max-width: 640px) {")

    toolbar = css_block(mobile, ".report-toolbar {")
    assert "flex-wrap: nowrap;" in toolbar
    pills_range = css_block(mobile, ".report-toolbar-range {")
    assert "min-width: 0;" in pills_range
    assert "overflow-x: auto;" in pills_range
    assert "scrollbar-width: none;" in pills_range
    assert "scroll-snap-type: x proximity;" in pills_range
    segmented = css_block(mobile, ".report-toolbar-range .segmented {")
    assert "flex-wrap: nowrap;" in segmented
    actions = css_block(mobile, ".report-toolbar .filters-actions {")
    assert "flex-shrink: 0;" in actions
    assert "margin-left: auto;" in actions


def test_the_heatmap_cells_grow_to_a_cap_and_center_but_never_shrink_below_the_minimum():
    css = APP_CSS.read_text(encoding="utf-8")

    scroll = css_block(css, ".heatmap-scroll {")
    assert "overflow-x: auto;" in scroll
    grid = css_block(css, ".heatmap-grid {")
    assert "display: flex;" in grid
    assert "flex: 1 1 auto;" in grid
    assert "justify-content: safe center;" in grid
    col = css_block(css, ".heatmap-col {")
    assert "flex: 1 1 14px;" in col
    assert "min-width: 14px;" in col
    assert "max-width: 26px;" in col
    cell = css_block(css, ".heatmap-grid .heat-cell {")
    assert "width: 100%;" in cell
    assert "aspect-ratio: 1;" in cell
    weeks = css_block(css, ".heatmap-weeks {")
    assert "display: flex;" in weeks
    assert "justify-content: safe center;" in weeks
    assert "flex: 1 1 14px;" in css_block(css, ".heatmap-weeks span {")
    assert "max-width: 26px;" in css_block(css, ".heatmap-weeks span {")


def test_the_avg_line_and_tip_have_their_own_styles():
    css = APP_CSS.read_text(encoding="utf-8")

    avg = css_block(css, ".chart-line.avg {")
    assert "stroke: var(--avg-line);" in avg
    assert "stroke-dasharray: 10 6;" in avg
    assert "stroke-width: 1.5;" in avg
    assert "opacity: 0.85;" in avg
    assert "background: var(--avg-line);" in css_block(css, ".legend-swatch.avg {")
    assert "pointer-events: none;" in css_block(css, ".chart-tip {")
    assert "fill: var(--surface);" in css_block(css, ".chart-tip-box {")
    assert "fill: var(--text);" in css_block(css, ".chart-tip-text {")
    assert "stroke: var(--surface);" in css_block(css, ".chart-dot {")
    assert "stroke-width: 1;" in css_block(css, ".chart-dot {")
    assert "pointer-events: all;" in css_block(css, ".chart-dot {")
    assert "pointer-events: none;" in css_block(css, ".chart-dot.avg {")
    active = css_block(css, ".chart-dot.chart-point-active {")
    assert "stroke: var(--text);" in active
    assert "stroke-width: 2;" in active
    outlier = css_block(css, ".chart-dot.outlier {")
    assert "fill: none;" in outlier
    assert "pointer-events: none;" in outlier
    assert "stroke: var(--danger);" in outlier


def test_the_custom_range_popover_is_a_toolbar_wide_card_hidden_in_print():
    css = APP_CSS.read_text(encoding="utf-8")

    card = css_block(css, ".range-popover {")
    assert "background: var(--surface);" in card
    assert "border: 1px solid var(--border);" in card
    assert "box-shadow: var(--shadow), var(--shadow-pop);" in card
    assert "padding:" in card
    assert "display:" not in card

    light = css_block(css, ":root {")
    assert (
        "--shadow-pop: 0 8px 24px rgba(16, 24, 40, 0.18), 0 2px 6px rgba(16, 24, 40, 0.12);"
        in light
    )
    dark = css_block(css, ':root[data-theme="dark"] {')
    assert "--shadow-pop: 0 8px 24px rgba(0, 0, 0, 0.55), 0 2px 6px rgba(0, 0, 0, 0.45);" in dark
    prefers = css_block(
        css_block(css, "@media (prefers-color-scheme: dark) {"),
        ':root:not([data-theme="light"]) {',
    )
    assert "--shadow-pop: 0 8px 24px rgba(0, 0, 0, 0.55), 0 2px 6px rgba(0, 0, 0, 0.45);" in prefers
    print_block = css[css.index("@media print {") :]
    assert "--shadow-pop: none;" in css_block(print_block, ":root,")

    popover_open = css_block(
        css,
        'body:has(#custom-range-popover:popover-open) button[popovertarget="custom-range-popover"] {',
    )
    assert "background: var(--surface);" in popover_open
    assert "color: var(--text);" in popover_open
    assert "font-weight: 600;" in popover_open
    assert "box-shadow: var(--shadow);" in popover_open

    toolbar = css_block(css, ".report-toolbar {")
    assert "anchor-name: --dash-toolbar;" in toolbar
    assert "position: relative;" in toolbar

    form = css_block(css, ".range-popover form {")
    assert "display: flex;" in form
    assert "flex-wrap: wrap;" in form
    assert "align-items: flex-end;" in form
    field = css_block(css, ".range-popover .range-field {")
    assert "display: flex;" in field
    assert "flex-direction: column;" in field

    anchored = css_block(css_block(css, "@supports (top: anchor(bottom)) {"), ".range-popover {")
    assert "position-anchor: --dash-toolbar;" in anchored
    assert "inset: auto;" in anchored
    assert "margin: 0;" in anchored
    assert "top: anchor(bottom);" in anchored
    assert "left: anchor(left);" in anchored
    assert "inline-size: anchor-size(inline);" in anchored
    assert "position-try-fallbacks: flip-block;" in anchored
    assert "right:" not in anchored
    assert "position-area" not in css
    assert "@supports not (top: anchor(bottom))" not in css
    assert "position: absolute" not in card

    print_block = css[css.index("@media print {") :]
    popover_print = css_block(print_block, "[popover] {")
    assert "display: none !important;" in popover_print


def test_the_category_dot_and_legend_styles_exist_for_every_category_class():
    css = APP_CSS.read_text(encoding="utf-8")

    assert "chart-band" not in css
    for cat, variable in (
        ("cat-ok", "success"),
        ("cat-elevated", "cat-elevated"),
        ("cat-stage1", "cat-stage1"),
        ("cat-danger", "danger"),
    ):
        assert re.search(
            rf"^\.chart-dot\.{cat} \{{\n  fill: var\(--{variable}\);\n\}}$", css, re.M
        ), cat
        assert re.search(
            rf"^\.legend-swatch\.{cat} \{{\n  background: var\(--{variable}\);\n\}}$", css, re.M
        ), cat


def test_the_theme_bootstrap_runs_before_the_stylesheet_is_requested():
    source = base_source()

    assert source.index("omron-data:theme") < source.index('rel="stylesheet"')


def test_the_rendered_page_puts_the_theme_bootstrap_before_the_stylesheet(client):
    body: str = client.get(reverse("login")).content.decode()

    assert body.index("omron-data:theme") < body.index("records/app.css")


def test_the_two_dark_palettes_declare_the_same_values():
    css = APP_CSS.read_text(encoding="utf-8")
    prefers = declarations(
        css_block(
            css_block(css, "@media (prefers-color-scheme: dark) {"),
            ':root:not([data-theme="light"]) {',
        )
    )
    pinned = declarations(css_block(css, ':root[data-theme="dark"] {'))

    assert "--bg: #12151a;" in pinned
    assert prefers == pinned


def test_the_theme_and_filter_storage_keys_keep_the_omron_data_namespace():
    theme = static_source("theme.js")
    filters = static_source("filters.js")

    assert '"omron-data:theme"' in base_source()
    assert '"omron-data:theme"' in theme
    assert '"omron-data:filter-focus"' in filters
    assert theme.count("window.localStorage.getItem(KEY)") == 1
    assert theme.count("window.localStorage.setItem(KEY, value)") == 1
    assert "sessionStorage" not in theme
    assert re.findall(r"sessionStorage\.(?:getItem|setItem|removeItem)\(storageKey\)", filters)


def test_every_loaded_template_library_exists_in_an_installed_app():
    available = set(get_installed_libraries())
    loaded = set()
    unknown = {}

    for path in template_files():
        for match in LOAD.finditer(path.read_text(encoding="utf-8")):
            for name in match.group(1).split():
                loaded.add(name)
                if name not in available:
                    unknown.setdefault(path.name, set()).add(name)

    assert loaded
    assert loaded <= available
    assert unknown == {}


def test_no_template_or_static_file_reaches_out_to_an_external_url():
    offenders = {}

    for path in [*template_files(), *static_files()]:
        if path.suffix not in {".html", ".css", ".js", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8")
        found = URL.findall(XMLNS.sub("", text))
        if found:
            offenders[path.name] = found

    assert offenders == {}


def test_the_only_external_link_a_rendered_page_carries_is_the_repository(client):
    body: str = client.get(reverse("login")).content.decode()

    assert f'<a href="{REPOSITORY_URL}" rel="noreferrer">GitHub</a>' in body
    assert set(URL.findall(body)) == {REPOSITORY_URL}


@override_settings(DEBUG=False)
def test_the_asset_tag_returns_a_plain_url_when_debug_is_off():
    url = asset("records/app.css")

    assert url == "/static/records/app.css"
    assert "?" not in url


@override_settings(DEBUG=True)
def test_the_asset_tag_stamps_the_modification_time_when_debug_is_on():
    found = finders.find("records/app.css")

    assert found
    assert asset("records/app.css") == f"/static/records/app.css?v={os.stat(found).st_mtime_ns}"


def test_every_static_file_a_template_references_exists():
    referenced = {}

    for path in template_files():
        for match in ASSET_TAG.finditer(path.read_text(encoding="utf-8")):
            referenced.setdefault(match.group(2), set()).add(path.name)

    assert referenced
    assert {name: where for name, where in referenced.items() if finders.find(name) is None} == {}


def test_num_renders_an_em_dash_for_a_missing_value():
    assert num(None) == "—"
    assert num("") == "—"
    assert num("not a number") == "—"


def test_a_decimal_comma_is_not_silently_misread():
    assert num("72,4") == "—"
    assert num("1 234,5") == "—"


def test_num_survives_nan_and_infinity():
    assert num(Decimal("NaN")) == "—"
    assert num(float("nan")) == "—"
    assert num(Decimal("Infinity")) == "—"
    assert num(float("-inf")) == "—"
    assert num("Infinity", 2) == "—"


def test_num_groups_thousands_and_honours_the_asked_for_decimals():
    assert num(Decimal("1234.5")) == "1,234.5"
    assert num(Decimal("1234.500")) == "1,234.5"
    assert num(Decimal("1234.5"), 2) == "1,234.50"
    assert num(Decimal("0.005"), 2) == "0.01"
    assert num(Decimal("100")) == "100"


def test_axis_unit_keeps_the_percent_tight_and_spaces_every_other_unit():
    assert axis_unit("") == ""
    assert axis_unit(None) == ""
    assert axis_unit("%") == "%"
    assert axis_unit("kg") == " kg"
    assert axis_unit("mmHg") == " mmHg"
    assert axis_unit("/min") == " /min"


def test_isodate_renders_the_day_or_the_minute_and_passes_other_values_through():
    moment = datetime(2024, 6, 10, 8, 30, 15, tzinfo=UTC)

    assert isodate(moment) == "2024-06-10 08:30"
    assert isodate(moment, "day") == "2024-06-10"
    assert isodate(date(2024, 6, 10)) == "2024-06-10"
    assert isodate("10 June 2024") == "10 June 2024"
    assert isodate(None) == "—"


def test_isominute_renders_the_minute_and_falls_back_to_the_raw_text():
    moment = datetime(2024, 6, 10, 8, 30, 15, tzinfo=UTC)

    assert isominute(moment) == "2024-06-10 08:30"
    assert isominute("2024-06-10T08:30:15") == "2024-06-10 08:30"
    assert isominute("whenever") == "whenever"
    assert isominute(None) == "—"


def test_elided_pages_keeps_the_ends_and_elides_the_middle():
    pages = Paginator(range(100), 10)

    assert list(elided_pages(pages.page(5))) == [1, pages.ELLIPSIS, 4, 5, 6, pages.ELLIPSIS, 10]
    assert list(elided_pages(pages.page(1))) == [1, 2, pages.ELLIPSIS, 10]
