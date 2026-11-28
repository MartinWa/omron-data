import re

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path, reverse

from omron_data.middleware import add_cache_control
from omron_data.urls import urlpatterns as project_urlpatterns
from omron_data.views import not_found, server_error

NONCE_ATTRIBUTE = re.compile(r'<script nonce="([^"]+)"')
NONCE_POLICY = re.compile(r"script-src 'self' 'nonce-([^']+)'")
SCRIPT_OPEN = re.compile(r"<script(?P<attributes>[^>]*)>")


def explode(request):
    raise RuntimeError("deliberate failure while rendering")


urlpatterns = [*project_urlpatterns, path("explode/", explode)]
handler404 = not_found
handler500 = server_error


def policy_shape(response) -> str:
    return NONCE_POLICY.sub(
        "script-src 'self' 'nonce-<value>'", response["Content-Security-Policy"]
    )


def test_a_page_policy_names_the_nonce_its_inline_script_carries(client, alice):
    client.force_login(alice)

    response = client.get(reverse("readings"))

    policy = response["Content-Security-Policy"]
    nonce = NONCE_POLICY.search(policy).group(1)
    assert NONCE_ATTRIBUTE.search(response.content.decode()).group(1) == nonce
    assert policy.split("; ") == [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ]


def test_the_anonymous_and_authenticated_pages_carry_the_same_policy(client, alice):
    anonymous = client.get(reverse("login"))

    client.force_login(alice)
    signed_in = client.get(reverse("readings"))

    assert policy_shape(anonymous) == policy_shape(signed_in)


def test_an_api_error_carries_the_policy_and_stays_uncached(client):
    response = client.get("/api/v1/measurements")

    assert response.status_code == 401
    assert NONCE_POLICY.search(response["Content-Security-Policy"]) is not None
    assert response["Cache-Control"] == "no-store"


def test_an_api_route_the_middleware_refuses_stays_uncached(client):
    response = client.post("/api/v1/measurements")

    assert response.status_code == 405
    assert NONCE_POLICY.search(response["Content-Security-Policy"]) is not None
    assert response["Cache-Control"] == "no-store"


def test_an_authenticated_page_is_never_stored(client, alice):
    client.force_login(alice)

    assert client.get(reverse("readings"))["Cache-Control"] == "no-store"


def test_a_response_that_already_sets_cache_control_keeps_it(db):
    request = RequestFactory().get("/readings/")
    request.user = get_user_model().objects.create_user(username="cache")
    response = HttpResponse("body", content_type="text/html")
    response["Cache-Control"] = "private, max-age=60"

    add_cache_control(request, response)

    assert response["Cache-Control"] == "private, max-age=60"


@override_settings(ROOT_URLCONF=__name__, DEBUG=False)
def test_the_error_pages_carry_the_policy():
    client = Client(raise_request_exception=False)

    missing = client.get("/no-such-page/")
    failed = client.get("/explode/")

    assert missing.status_code == 404
    assert failed.status_code == 500
    for response in (missing, failed):
        assert NONCE_POLICY.search(response["Content-Security-Policy"]) is not None


def test_the_api_docs_page_renders_under_the_nonce_policy(client):
    response = client.get("/api/v1/docs")

    assert response.status_code == 200
    body = response.content.decode()
    assert '<div id="swagger-ui"></div>' in body
    policy = response["Content-Security-Policy"]
    assert NONCE_POLICY.search(policy) is not None
    assert "unsafe-inline" not in policy
    inline = [
        script.group("attributes")
        for script in SCRIPT_OPEN.finditer(body)
        if "src=" not in script.group("attributes")
    ]
    assert len(inline) == 1
    assert 'type="application/json"' in inline[0]


ADMIN_INLINE_STYLE = re.compile(r"<style| style=\"")
ADMIN_PAGES = (
    "admin:index",
    "admin:records_measurement_changelist",
    "admin:records_importrun_changelist",
    "admin:records_apitoken_changelist",
)


def test_the_admin_pages_carry_no_inline_style(client, db):
    client.force_login(get_user_model().objects.create_superuser(username="admin"))

    for name in ADMIN_PAGES:
        response = client.get(reverse(name))

        assert response.status_code == 200, name
        assert ADMIN_INLINE_STYLE.search(response.content.decode()) is None, name
