from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client

from records.importer import import_readings
from records.models import ApiToken
from records.tokens import issue
from tests.test_import import REPORT, parse_text


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(username="alice")


def get(token: str, path: str = "/api/v1/summary"):
    return Client().get(path, headers={"authorization": f"Bearer {token}"})


def test_an_active_owners_token_is_accepted(owner):
    import_readings(owner, parse_text(REPORT))
    token = issue(owner, "laptop")

    assert get(token).status_code == 200
    assert get(token, "/api/v1/measurements").status_code == 200


def test_a_deactivated_owners_token_is_refused(owner):
    import_readings(owner, parse_text(REPORT))
    token = issue(owner, "laptop")
    owner.is_active = False
    owner.save()

    assert get(token).status_code == 401
    assert get(token, "/api/v1/measurements").status_code == 401


def test_a_revoked_token_is_refused(owner):
    token = issue(owner, "laptop")
    ApiToken.objects.filter(owner=owner).update(revoked=True)

    assert get(token).status_code == 401
    assert get(token, "/api/v1/measurements").status_code == 401


def test_reactivating_the_owner_reinstates_the_existing_token(owner):
    token = issue(owner, "laptop")
    owner.is_active = False
    owner.save()
    assert get(token).status_code == 401

    owner.is_active = True
    owner.save()

    assert get(token).status_code == 200
    assert get(token, "/api/v1/measurements").status_code == 200


def test_the_inactive_owner_can_still_be_imported_for(owner, tmp_path):
    (tmp_path / "export.csv").write_text(REPORT, encoding="utf-8")
    owner.is_active = False
    owner.save()

    out = StringIO()
    call_command("import_omron", str(tmp_path), "--owner", "alice", stdout=out)

    assert "3 created" in out.getvalue()
    assert get(issue(owner, "laptop")).status_code == 401
