import pytest
from axes.utils import reset
from django.test import override_settings
from django.urls import reverse

PASSWORD = "correct-horse-0001"  # noqa: S105
WRONG_PASSWORD = "wrong-horse-0001"  # noqa: S105
CLIENT_ADDRESS = "203.0.113.7"
FAILURES_TO_LOCK = 3


@pytest.fixture(autouse=True)
def cleared_attempts(db):
    reset()
    yield
    reset()


@pytest.fixture
def account(alice):
    alice.set_password(PASSWORD)
    alice.is_staff = True
    alice.save()
    return alice


@pytest.fixture
def other_account(bob):
    bob.set_password(PASSWORD)
    bob.save()
    return bob


def sign_in(client, route, username, password):
    return client.post(
        reverse(route),
        {"username": username, "password": password},
        headers={"x-forwarded-for": CLIENT_ADDRESS},
    )


def fail_until_locked(client, route, account):
    for _ in range(FAILURES_TO_LOCK):
        sign_in(client, route, account.username, WRONG_PASSWORD)


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=FAILURES_TO_LOCK)
def test_repeated_wrong_passwords_lock_the_login_route(client, account):
    assert sign_in(client, "login", account.username, PASSWORD).status_code == 302
    client.logout()

    fail_until_locked(client, "login", account)

    assert sign_in(client, "login", account.username, PASSWORD).status_code == 429


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=FAILURES_TO_LOCK)
def test_repeated_wrong_passwords_lock_the_admin_login_route(client, account):
    assert sign_in(client, "admin:login", account.username, PASSWORD).status_code == 302
    client.logout()

    fail_until_locked(client, "admin:login", account)

    assert sign_in(client, "admin:login", account.username, PASSWORD).status_code == 429


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=FAILURES_TO_LOCK)
def test_a_lockout_leaves_another_account_at_the_same_address_signed_in(
    client, account, other_account
):
    assert sign_in(client, "login", account.username, PASSWORD).status_code == 302
    client.logout()

    fail_until_locked(client, "login", account)

    assert sign_in(client, "login", account.username, PASSWORD).status_code == 429
    assert sign_in(client, "login", other_account.username, PASSWORD).status_code == 302


@pytest.mark.django_db
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=FAILURES_TO_LOCK)
def test_a_lockout_is_confined_to_the_address_it_came_from(client, account):
    assert sign_in(client, "login", account.username, PASSWORD).status_code == 302
    client.logout()

    fail_until_locked(client, "login", account)

    assert sign_in(client, "login", account.username, PASSWORD).status_code == 429
    elsewhere = client.post(
        reverse("login"),
        {"username": account.username, "password": PASSWORD},
        headers={"x-forwarded-for": "198.51.100.9"},
    )
    assert elsewhere.status_code == 302
