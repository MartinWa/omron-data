from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.urls import reverse

from records.models import ApiToken, ImportRun, Measurement

MODELS = (Measurement, ImportRun, ApiToken)
ADMIN_USERS = ("superuser", "staff")


def model_id(model):
    return model._meta.model_name


@pytest.fixture
def superuser(db):
    return get_user_model().objects.create_superuser(username="root")


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user(username="staff", is_staff=True)


@pytest.fixture
def admin_user(request, superuser, staff):
    return superuser if request.param == "superuser" else staff


@pytest.fixture
def rows(db):
    owner = get_user_model().objects.create_user(username="alice")
    return {
        Measurement: Measurement.objects.create(
            owner=owner,
            metric="pulse",
            value=Decimal("70"),
            unit="/min",
            at=datetime(2024, 6, 10, 8, 0, tzinfo=UTC),
        ),
        ImportRun: ImportRun.objects.create(
            owner=owner,
            file_name="export.csv",
            content_sha256="a" * 64,
            readings=3,
        ),
        ApiToken: ApiToken.objects.create(
            owner=owner,
            name="reader",
            prefix="abcd1234",
            digest="b" * 64,
        ),
    }


def changelist(model):
    return reverse(f"admin:records_{model_id(model)}_changelist")


def add(model):
    return reverse(f"admin:records_{model_id(model)}_add")


def change(model, row):
    return reverse(f"admin:records_{model_id(model)}_change", args=[row.pk])


def delete(model, row):
    return reverse(f"admin:records_{model_id(model)}_delete", args=[row.pk])


@pytest.mark.parametrize("model", MODELS, ids=model_id)
def test_every_record_model_is_registered_in_the_admin(model):
    assert admin.site.is_registered(model)


@pytest.mark.parametrize("model", MODELS, ids=model_id)
@pytest.mark.parametrize("admin_user", ADMIN_USERS, indirect=True)
def test_the_changelist_renders_for_admin_staff(client, admin_user, model, rows):
    client.force_login(admin_user)

    response = client.get(changelist(model))

    assert response.status_code == 200
    assert response.context["cl"].result_count == 1


@pytest.mark.parametrize("model", MODELS, ids=model_id)
@pytest.mark.parametrize("admin_user", ADMIN_USERS, indirect=True)
def test_adding_is_refused_for_admin_staff(client, admin_user, model, rows):
    client.force_login(admin_user)

    assert client.get(add(model)).status_code == 403
    assert client.post(add(model), {}).status_code == 403


@pytest.mark.parametrize("model", MODELS, ids=model_id)
@pytest.mark.parametrize("admin_user", ADMIN_USERS, indirect=True)
def test_changing_is_refused_for_admin_staff(client, admin_user, model, rows):
    row = rows[model]
    client.force_login(admin_user)

    assert client.post(change(model, row), {"metric": "bmi"}).status_code == 403
    assert model.objects.filter(pk=row.pk).exists()


@pytest.mark.parametrize("model", MODELS, ids=model_id)
@pytest.mark.parametrize("admin_user", ADMIN_USERS, indirect=True)
def test_deleting_is_refused_and_the_row_survives(client, admin_user, model, rows):
    row = rows[model]
    client.force_login(admin_user)

    assert client.get(delete(model, row)).status_code == 403
    assert client.post(delete(model, row), {"post": "yes"}).status_code == 403
    assert model.objects.filter(pk=row.pk).exists()


@pytest.mark.parametrize("model", MODELS, ids=model_id)
@pytest.mark.parametrize("admin_user", ADMIN_USERS, indirect=True)
def test_the_change_page_is_read_only_and_covers_every_field(client, admin_user, model, rows):
    row = rows[model]
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    model_admin = admin.site._registry[model]

    client.force_login(admin_user)
    response = client.get(change(model, row))

    body = response.content.decode()
    readonly = set(model_admin.get_readonly_fields(request))
    assert response.status_code == 200
    assert {field.name for field in model._meta.fields} <= readonly
    assert body.count('<div class="readonly">') == len(model._meta.fields)
    assert "Save" not in body
