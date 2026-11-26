from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from parsing.report import parse_text
from records.importer import import_readings
from records.models import METRICS, Measurement
from tests.test_import import report

DETAILS = '<details class="card session" data-print-open>'


def zone() -> ZoneInfo:
    return ZoneInfo(settings.OMRON_TIME_ZONE)


def owner_with(username: str, *rows: str):
    owner = get_user_model().objects.create_user(username=username)
    import_readings(owner, parse_text(report(*rows)))
    return owner


def stored(owner, metric: str, at: datetime, value=None, note: str = ""):
    return Measurement.objects.create(
        owner=owner,
        metric=metric,
        at=at,
        value=None if value is None else Decimal(str(value)),
        unit=METRICS[metric].unit,
        note=note,
    )


def panels(*days: int) -> list[str]:
    return [f"{day} juni 2024,08:00,130,85,70,," for day in days]


def events(content: str) -> list[str]:
    return content.split(DETAILS)[1:]


def pager_links(content: str) -> list[str]:
    pager = content.split('class="pager"')[1].split("</nav>")[0]
    return pager.split("<a ")[1:]


@pytest.mark.django_db
def test_anonymous_visitors_are_sent_to_the_login_page(client):
    response = client.get(reverse("readings"))

    assert response.status_code == 302
    assert reverse("login") in response.url


@pytest.mark.django_db
def test_twenty_five_events_fill_the_first_page_and_page_two_holds_the_rest(client):
    owner = owner_with("alice", *panels(*range(1, 27)))
    client.force_login(owner)

    first = client.get(reverse("readings")).content.decode()
    second = client.get(reverse("readings"), {"page": 2}).content.decode()

    assert first.count(DETAILS) == 25
    assert second.count(DETAILS) == 1


@pytest.mark.django_db
def test_two_metrics_at_one_moment_share_a_single_event(client):
    owner = get_user_model().objects.create_user(username="alice")
    at = datetime(2024, 6, 10, 8, 0, tzinfo=zone())
    stored(owner, "pulse", at, 70)
    stored(owner, "body_weight", at, "72.4")
    client.force_login(owner)

    content = client.get(reverse("readings")).content.decode()

    assert content.count(DETAILS) == 1
    assert content.count('scope="rowgroup"') == 2
    assert "2 readings" in content


@pytest.mark.django_db
def test_the_panel_components_sit_under_one_heading_in_pressure_order(client):
    owner = owner_with("alice", *panels(10))
    client.force_login(owner)

    event = events(client.get(reverse("readings")).content.decode())[0]

    assert event.count('scope="rowgroup"') == 1
    assert "Blood pressure" in event.split('scope="rowgroup"')[1]
    assert event.index("Systolic") < event.index("Diastolic") < event.index("Pulse")


@pytest.mark.django_db
def test_values_carry_their_unit_and_a_missing_value_is_an_em_dash(client):
    owner = owner_with("alice", *panels(10))
    stored(owner, "body_weight", datetime(2024, 6, 11, 8, 0, tzinfo=zone()), None)
    client.force_login(owner)

    content = client.get(reverse("readings")).content.decode()

    assert '<td class="num">130</td>' in content
    assert '<td class="muted">mmHg</td>' in content
    assert '<td class="num">—</td>' in content
    assert "None" not in content


@pytest.mark.django_db
def test_the_event_note_is_shown_once(client):
    owner = owner_with("alice", "10 juni 2024,08:00,130,85,70,,AFib")
    client.force_login(owner)

    content = client.get(reverse("readings")).content.decode()

    assert content.count("AFib") == 1


@pytest.mark.django_db
def test_the_metric_filter_narrows_the_list_and_stays_selected(client):
    owner = owner_with("alice", *panels(10))
    stored(owner, "body_weight", datetime(2024, 6, 11, 8, 0, tzinfo=zone()), "72.4")
    client.force_login(owner)

    content = client.get(reverse("readings"), {"metric": "body_weight"}).content.decode()

    assert content.count(DETAILS) == 1
    assert '<option value="body_weight" selected>Weight</option>' in content
    assert '<td class="muted">mmHg</td>' not in content


@pytest.mark.django_db
def test_a_component_metric_filter_still_finds_its_panel(client):
    owner = owner_with("alice", *panels(10, 11))
    client.force_login(owner)

    content = client.get(reverse("readings"), {"metric": "pulse"}).content.decode()

    assert content.count(DETAILS) == 2
    assert content.count('scope="rowgroup"') == 2


@pytest.mark.django_db
def test_the_date_filters_include_the_whole_boundary_day(client):
    owner = get_user_model().objects.create_user(username="alice")
    day = datetime(2024, 6, 10, tzinfo=zone())
    stored(owner, "body_weight", day.replace(hour=0, minute=1), "72.4")
    stored(owner, "body_weight", day.replace(hour=23, minute=59), "72.5")
    stored(owner, "body_weight", day - timedelta(minutes=1), "72.3")
    client.force_login(owner)

    content = client.get(
        reverse("readings"), {"from": "2024-06-10", "to": "2024-06-10"}
    ).content.decode()

    assert content.count(DETAILS) == 2
    assert "72.3" not in content


@pytest.mark.django_db
def test_the_search_matches_a_note_without_regard_to_case_and_is_echoed(client):
    owner = owner_with("alice", "10 juni 2024,08:00,130,85,70,,AFib")
    client.force_login(owner)

    content = client.get(reverse("readings"), {"q": "afib"}).content.decode()

    assert content.count(DETAILS) == 1
    assert 'value="afib"' in content


@pytest.mark.django_db
def test_a_filtered_empty_list_keeps_the_form_and_asks_for_other_filters(client, alice):
    client.force_login(alice)

    content = client.get(reverse("readings"), {"q": "nothing-matches"}).content.decode()

    assert "No readings match these filters." in content
    assert "No readings yet." not in content
    assert "data-filter-form" in content
    assert ">Clear filters<" in content


@pytest.mark.django_db
def test_an_account_with_nothing_stored_is_invited_to_import(client):
    owner = get_user_model().objects.create_user(username="alice")
    client.force_login(owner)

    content = client.get(reverse("readings")).content.decode()

    assert "No readings yet." in content
    assert "No readings match these filters." not in content
    assert f'href="{reverse("imports")}"' in content


@pytest.mark.django_db
def test_the_pager_keeps_the_metric_filter(client):
    owner = owner_with("alice", *panels(*range(1, 27)))
    client.force_login(owner)

    content = client.get(reverse("readings"), {"metric": "blood_pressure"}).content.decode()

    following = [link for link in pager_links(content) if 'rel="next"' in link][0]
    assert "metric=blood_pressure" in following
    assert "page=2" in following


@pytest.mark.django_db
def test_a_disabled_pager_link_has_no_target_to_activate(client):
    owner = owner_with("alice", *panels(*range(1, 27)))
    client.force_login(owner)

    content = client.get(reverse("readings"), {"page": 2}).content.decode()

    disabled = [link for link in pager_links(content) if 'aria-disabled="true"' in link]
    assert len(disabled) == 1
    assert "href=" not in disabled[0]


@pytest.mark.django_db
def test_each_event_table_is_captioned_for_screen_readers(client):
    owner = owner_with("alice", *panels(10))
    client.force_login(owner)

    content = client.get(reverse("readings")).content.decode()

    caption = '<caption class="visually-hidden">Blood pressure on 2024-06-10 08:00'
    assert caption in content
    assert "3 readings</caption>" in content


@pytest.mark.django_db
def test_the_page_count_matches_the_api_for_the_same_filters(client, auth, alice):
    client.force_login(alice)

    page = client.get(reverse("readings"), {"metric": "blood_pressure"})
    body = auth.get("/api/v1/measurements", {"metric": "blood_pressure"}).json()

    assert page.context["page"].paginator.count == body["count"]


@pytest.mark.django_db
def test_another_owners_readings_never_appear(client, alice, bob):
    client.force_login(alice)

    content = client.get(reverse("readings")).content.decode()

    assert content.count(DETAILS) == 4
    assert "AFib" not in content


@pytest.mark.django_db
def test_the_page_loads_the_filter_and_print_scripts(client):
    owner = owner_with("alice", *panels(10))
    client.force_login(owner)

    content = client.get(reverse("readings")).content.decode()

    assert "records/filters.js" in content
    assert "records/print.js" in content
    assert content.count("data-print-open") == 1
