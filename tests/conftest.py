import pytest
from django.contrib.auth import get_user_model
from django.db.models import Max, Min
from django.test import Client

from records.importer import import_readings
from records.models import ImportRun, Measurement
from records.tokens import issue
from tests.test_import import REPORT, parse_text, report

ALICE_REPORT = report(
    "03 juni 2024,08:00,130,85,70,,",
    "10 juni 2024,08:00,131,86,71,,",
    "20 juni 2024,08:00,132,87,72,,",
    "01 juli 2024,08:00,133,88,73,,",
)
BOB_REPORT = REPORT


def make_owner(username: str, text: str):
    owner = get_user_model().objects.create_user(username=username)
    counts = import_readings(owner, parse_text(text))
    bounds = Measurement.objects.filter(owner=owner).aggregate(first=Min("at"), last=Max("at"))
    ImportRun.objects.create(
        owner=owner,
        file_name=f"{username}.csv",
        content_sha256=f"{username:0<64}",
        readings=counts["created"],
        created=counts["created"],
        panels=counts["panels"],
        first_at=bounds["first"],
        last_at=bounds["last"],
    )
    owner.token = issue(owner, "tests")
    return owner


def client_for(owner) -> Client:
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {owner.token}"
    return client


@pytest.fixture
def alice(db):
    return make_owner("alice", ALICE_REPORT)


@pytest.fixture
def bob(db):
    return make_owner("bob", BOB_REPORT)


@pytest.fixture
def auth(alice):
    return client_for(alice)


@pytest.fixture
def bob_auth(bob):
    return client_for(bob)


@pytest.fixture
def panel_ids(alice):
    rows = Measurement.objects.filter(owner=alice, parent__isnull=True).order_by("-at")
    return list(rows.values_list("id", flat=True))
