import re

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings
from django.urls import path, reverse

from omron_data.context_processors import REPOSITORY_URL
from omron_data.urls import urlpatterns as project_urlpatterns
from omron_data.views import not_found, server_error

BROWSER = {
    "DEBUG": False,
    "ALLOWED_HOSTS": ["testserver", "localhost"],
    "ROOT_URLCONF": "tests.test_error_pages",
}
CALL_TO_ACTION = re.compile(r'<a class="btn btn-primary" href="([^"]+)">([^<]+)</a>')


def explode(request):
    raise RuntimeError("deliberate failure while rendering")


urlpatterns = [*project_urlpatterns, path("explode/", explode)]
handler404 = not_found
handler500 = server_error


def request_for(user=None):
    request = RequestFactory().get("/missing/")
    if user is not None:
        request.user = user
    return request


def actions(body: str) -> list[tuple[str, str]]:
    return CALL_TO_ACTION.findall(body)


@pytest.mark.django_db
def test_a_logged_in_visitor_sees_the_branded_404_with_a_readings_link():
    visitor = get_user_model().objects.create_user(username="alice")

    response = not_found(request_for(visitor), Exception("missing"))

    body = response.content.decode()
    assert response.status_code == 404
    assert "Not found — omron-data" in body
    assert "Page not found" in body
    assert actions(body) == [(reverse("readings"), "Go to readings")]
    assert "Log in" not in body


def test_an_anonymous_visitor_sees_the_404_with_a_login_link():
    response = not_found(request_for(), Exception("missing"))

    body = response.content.decode()
    assert response.status_code == 404
    assert "Page not found" in body
    assert actions(body) == [(reverse("login"), "Log in")]
    assert "Go to readings" not in body


@pytest.mark.django_db
def test_the_500_handler_shows_the_readings_call_to_action_for_a_logged_in_visitor():
    visitor = get_user_model().objects.create_user(username="alice")

    response = server_error(request_for(visitor))

    body = response.content.decode()
    assert response.status_code == 500
    assert "Server error — omron-data" in body
    assert "Something went wrong" in body
    assert actions(body) == [(reverse("readings"), "Go to readings")]
    assert "Log in" not in body


def test_the_500_handler_works_when_the_request_carries_no_user_attribute():
    request = request_for()

    assert not hasattr(request, "user")

    response = server_error(request)

    body = response.content.decode()
    assert response.status_code == 500
    assert actions(body) == [(reverse("login"), "Log in")]
    assert "Go to readings" not in body


@pytest.mark.django_db
def test_the_500_page_never_shows_traceback_text():
    visitor = get_user_model().objects.create_user(username="alice")

    with pytest.raises(RuntimeError):
        explode(request_for(visitor))

    body = server_error(request_for(visitor)).content.decode()
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    assert "deliberate failure" not in body


@pytest.mark.django_db
def test_a_logged_in_404_and_a_logged_in_500_agree_on_their_call_to_action():
    user = get_user_model().objects.create_user(username="alice")

    missing = actions(not_found(request_for(user), Exception("missing")).content.decode())
    broken = actions(server_error(request_for(user)).content.decode())

    assert missing == broken == [(reverse("readings"), "Go to readings")]


@pytest.mark.django_db
def test_neither_error_page_loads_an_external_script_or_stylesheet():
    visitor = get_user_model().objects.create_user(username="alice")

    for body in (
        not_found(request_for(visitor), Exception("missing")).content.decode(),
        server_error(request_for(visitor)).content.decode(),
        not_found(request_for(), Exception("missing")).content.decode(),
        server_error(request_for()).content.decode(),
    ):
        assert re.search(r'<script[^>]*src="https?://', body) is None
        assert re.search(r'<link[^>]*href="https?://', body) is None
        assert re.findall(r'(?:src|href)="(https?://[^"]+)"', body) == [REPOSITORY_URL]


@override_settings(**BROWSER)
def test_the_client_gets_the_404_page_with_status_404(client):
    response = client.get("/nowhere/")

    body = response.content.decode()
    assert response.status_code == 404
    assert "Page not found" in body
    assert actions(body) == [(reverse("login"), "Log in")]


@override_settings(**BROWSER)
@pytest.mark.django_db
def test_the_client_gets_the_500_page_with_status_500(client):
    visitor = get_user_model().objects.create_user(username="alice")
    client.force_login(visitor)
    client.raise_request_exception = False

    response = client.get("/explode/")

    body = response.content.decode()
    assert response.status_code == 500
    assert "Something went wrong" in body
    assert actions(body) == [(reverse("readings"), "Go to readings")]
    assert "Traceback" not in body


@override_settings(**BROWSER)
@pytest.mark.django_db
def test_the_client_sees_the_branded_404_for_a_logged_in_visitor(client):
    visitor = get_user_model().objects.create_user(username="alice")
    client.force_login(visitor)

    response = client.get("/nowhere/")

    body = response.content.decode()
    assert response.status_code == 404
    assert "Page not found" in body
    assert "omron-data" in body
    assert actions(body) == [(reverse("readings"), "Go to readings")]
