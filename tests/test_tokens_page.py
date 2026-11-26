import hashlib
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from records.models import ApiToken

STATUS_BOX = '<div class="secret" role="status" aria-live="polite" tabindex="-1">'


def alice_token(owner) -> ApiToken:
    return ApiToken.objects.get(owner=owner, name="tests")


@pytest.mark.django_db
def test_anonymous_visitors_are_sent_to_the_login_page(client):
    response = client.get(reverse("tokens"))

    assert response.status_code == 302
    assert reverse("login") in response.url


def test_creating_a_token_stores_only_a_hash_and_shows_the_secret_once(client, alice):
    client.force_login(alice)

    response = client.post(reverse("tokens"), {"name": "reader"})

    secret = response.context["secret"]
    token = ApiToken.objects.get(owner=alice, name="reader")
    assert token.digest == hashlib.sha256(secret.encode()).hexdigest()
    assert token.prefix == secret[:8]
    assert len(token.prefix) == 8
    content = response.content.decode()
    assert content.count(secret) == 1
    assert 'id="token-secret"' in content
    assert "Created reader. Copy the secret now; it is shown only once." in content


def test_the_secret_box_announces_itself_and_offers_a_copy_button(client, alice):
    client.force_login(alice)

    content = client.post(reverse("tokens"), {"name": "reader"}).content.decode()

    assert STATUS_BOX in content
    assert 'data-copy="#token-secret"' in content
    assert "shown only once" in content


def test_a_duplicate_name_is_a_form_error_and_creates_nothing(client, alice):
    client.force_login(alice)
    client.post(reverse("tokens"), {"name": "reader"})

    response = client.post(reverse("tokens"), {"name": "reader"})

    assert response.status_code == 200
    assert "A token with this name already exists." in response.content.decode()
    assert ApiToken.objects.filter(owner=alice, name="reader").count() == 1


def test_a_name_at_the_limit_is_accepted_and_one_over_it_is_refused(client, alice):
    client.force_login(alice)
    longest = "a" * 64

    created = client.post(reverse("tokens"), {"name": longest})
    refused = client.post(reverse("tokens"), {"name": longest + "a"})

    assert created.context["secret"]
    assert ApiToken.objects.filter(owner=alice, name=longest).exists()
    assert refused.status_code == 200
    assert "at most 64 characters" in refused.content.decode()
    assert ApiToken.objects.filter(owner=alice).count() == 2


def test_rows_show_the_prefix_and_a_status_badge(client, alice):
    client.force_login(alice)

    content = client.get(reverse("tokens")).content.decode()

    assert f"<code>{alice_token(alice).prefix}…</code>" in content
    assert '<span class="badge badge-success">active</span>' in content


def test_revoking_a_token_stops_its_clients_and_says_so(client, alice):
    client.force_login(alice)
    token = alice_token(alice)

    response = client.post(reverse("revoke_token", args=[token.pk]), follow=True)

    token.refresh_from_db()
    assert token.revoked_at is not None
    assert "Revoked tests. Clients using it stop working now." in response.content.decode()
    assert '<span class="badge badge-danger">revoked</span>' in response.content.decode()


def test_revoking_a_revoked_token_says_it_was_already_revoked(client, alice):
    client.force_login(alice)
    token = alice_token(alice)
    client.post(reverse("revoke_token", args=[token.pk]))

    response = client.post(reverse("revoke_token", args=[token.pk]), follow=True)

    assert "tests was already revoked." in response.content.decode()


def test_a_working_token_cannot_be_deleted(client, alice):
    client.force_login(alice)
    token = alice_token(alice)

    response = client.post(reverse("delete_token", args=[token.pk]), follow=True)

    assert "Revoke tests first; a working token cannot be deleted." in response.content.decode()
    assert ApiToken.objects.filter(pk=token.pk).exists()


def test_a_revoked_token_can_be_deleted_and_its_name_used_again(client, alice):
    client.force_login(alice)
    token = alice_token(alice)
    client.post(reverse("revoke_token", args=[token.pk]))

    response = client.post(reverse("delete_token", args=[token.pk]), follow=True)

    assert "Deleted tests. That name can be used again." in response.content.decode()
    assert not ApiToken.objects.filter(pk=token.pk).exists()
    client.post(reverse("tokens"), {"name": "tests"})
    assert ApiToken.objects.filter(owner=alice, name="tests").count() == 1


def test_token_actions_reject_get(client, alice):
    client.force_login(alice)
    token = alice_token(alice)

    assert client.get(reverse("revoke_token", args=[token.pk])).status_code == 405
    assert client.get(reverse("delete_token", args=[token.pk])).status_code == 405


def test_another_owner_cannot_revoke_or_delete_a_token(client, alice, bob):
    token = alice_token(alice)
    client.force_login(bob)

    revoked = client.post(reverse("revoke_token", args=[token.pk]), follow=True)
    deleted = client.post(reverse("delete_token", args=[token.pk]), follow=True)

    assert "That token was not found." in revoked.content.decode()
    assert "That token was not found." in deleted.content.decode()
    token.refresh_from_db()
    assert token.revoked_at is None


def test_the_revoke_confirmation_stays_hidden_until_the_row_asks_for_it(client, alice):
    client.force_login(alice)
    token = alice_token(alice)
    action = f'action="{reverse("revoke_token", args=[token.pk])}"'

    hidden = client.get(reverse("tokens")).content.decode()
    asking = client.get(reverse("tokens"), {"confirm": token.pk}).content.decode()

    assert f"{action} data-row-form hidden>" in hidden
    assert f"{action} data-row-form hidden>" not in asking
    assert f"{action} data-row-form>" in asking
    assert f'id="revoke-warning-{token.pk}"' in asking
    assert f'aria-describedby="revoke-warning-{token.pk}"' in asking
    assert f'href="?confirm={token.pk}"' in hidden


def test_the_delete_confirmation_carries_its_own_warning(client, alice):
    client.force_login(alice)
    token = alice_token(alice)
    client.post(reverse("revoke_token", args=[token.pk]))

    content = client.get(reverse("tokens"), {"confirm": token.pk}).content.decode()

    assert f'id="delete-warning-{token.pk}"' in content
    assert f'aria-describedby="delete-warning-{token.pk}"' in content
    assert f'action="{reverse("delete_token", args=[token.pk])}" data-row-form>' in content


def test_another_owners_tokens_are_not_listed(client, alice, bob):
    client.force_login(alice)

    content = client.get(reverse("tokens")).content.decode()

    assert ApiToken.objects.filter(owner=bob).count() == 1
    assert content.count("<code>") == 1


@pytest.mark.django_db
def test_an_account_without_tokens_is_told_where_to_start(client):
    owner = get_user_model().objects.create_user(username="carol")
    client.force_login(owner)

    content = client.get(reverse("tokens")).content.decode()

    assert "No tokens yet." in content
    assert "Create a token" in content


def test_a_token_can_be_created_with_an_expiry(client, alice):
    client.force_login(alice)

    response = client.post(reverse("tokens"), {"name": "reader", "expires_in_days": 30})

    token = ApiToken.objects.get(owner=alice, name="reader")
    assert response.context["secret"]
    assert token.status == "active"
    assert token.expires_at is not None
    left = token.expires_at - timezone.now()
    assert timedelta(days=29, hours=23) < left < timedelta(days=30, minutes=1)
    content = response.content.decode()
    assert timezone.localtime(token.expires_at).strftime("%Y-%m-%d") in content
    assert '<span class="badge badge-success">active</span>' in content


def test_an_expired_token_is_shown_as_expired_and_can_be_deleted(client, alice):
    token = alice_token(alice)
    token.expires_at = timezone.now() - timedelta(days=1)
    token.save(update_fields=["expires_at"])
    client.force_login(alice)
    delete_url = reverse("delete_token", args=[token.pk])

    content = client.get(reverse("tokens")).content.decode()

    assert '<span class="badge badge-warning">expired</span>' in content
    assert f'action="{delete_url}"' in content
    response = client.post(delete_url, follow=True)
    assert "Deleted tests. That name can be used again." in response.content.decode()
    assert not ApiToken.objects.filter(pk=token.pk).exists()


def test_the_create_form_offers_the_optional_expiry(client, alice):
    client.force_login(alice)

    content = client.get(reverse("tokens")).content.decode()

    assert 'name="expires_in_days"' in content
    assert "Leave empty for a token that does not expire on its own." in content


def test_a_zero_day_expiry_is_a_visible_form_error_and_creates_nothing(client, alice):
    client.force_login(alice)

    response = client.post(reverse("tokens"), {"name": "reader", "expires_in_days": 0})

    assert response.status_code == 200
    assert not ApiToken.objects.filter(owner=alice, name="reader").exists()
    assert "Ensure this value is greater than or equal to 1." in response.content.decode()


@pytest.mark.django_db
def test_make_token_can_set_an_expiry():
    owner = get_user_model().objects.create_user(username="carol")
    out = StringIO()

    call_command("make_token", owner="carol", name="laptop", expires_days=7, stdout=out)

    token = ApiToken.objects.get(owner=owner, name="laptop")
    assert token.expires_at is not None
    assert timezone.localtime(token.expires_at).strftime("%Y-%m-%d") in out.getvalue()


@pytest.mark.django_db
def test_make_token_refuses_a_non_positive_expiry():
    get_user_model().objects.create_user(username="carol")

    with pytest.raises(CommandError):
        call_command("make_token", owner="carol", name="laptop", expires_days=0, stdout=StringIO())


def test_a_racing_duplicate_name_becomes_a_form_error_not_a_crash(client, alice, monkeypatch):
    client.force_login(alice)

    def raises(*args, **kwargs):
        raise IntegrityError("UNIQUE constraint failed: records_apitoken.digest")

    monkeypatch.setattr("records.views.issue", raises)

    response = client.post(reverse("tokens"), {"name": "reader"})

    assert response.status_code == 200
    assert "A token with this name already exists." in response.content.decode()
    assert not ApiToken.objects.filter(owner=alice, name="reader").exists()
