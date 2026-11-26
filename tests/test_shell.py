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
