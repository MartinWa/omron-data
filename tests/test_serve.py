import http.client
import threading

import pytest
from django.test import RequestFactory, override_settings
from waitress.server import create_server

from tools.serve import listen_address, proxy_options, verify_proxy_configuration


def echo_app(environ, start_response):
    body = (
        f"{environ.get('HTTP_X_FORWARDED_PROTO', '')}|{environ.get('HTTP_X_FORWARDED_FOR', '')}"
    ).encode()
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
    return [body]


class Server:
    def __init__(self, **options):
        self.server = create_server(echo_app, host="127.0.0.1", port=0, **options)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.server.close()
        self.thread.join(timeout=5)

    def forwarded(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.effective_port, timeout=5)
        try:
            connection.request(
                "GET",
                "/",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"},
            )
            return connection.getresponse().read().decode()
        finally:
            connection.close()


def options_for(trusted):
    with override_settings(OMRON_TRUSTED_PROXY=trusted):
        return proxy_options()


def test_no_scheme_header_survives_without_a_trusted_proxy():
    with Server() as server:
        assert server.forwarded() == "|"


def test_a_trusted_proxy_passes_the_scheme_and_forwarding_headers_through():
    with Server(**options_for("*")) as server:
        assert server.forwarded() == "https|203.0.113.7"


def test_the_proxy_options_are_empty_until_a_proxy_is_declared(settings):
    assert settings.OMRON_TRUSTED_PROXY == ""
    assert proxy_options() == {}


def test_a_declared_proxy_trusts_the_scheme_and_forwarding_headers(settings):
    settings.OMRON_TRUSTED_PROXY = "*"

    options = proxy_options()

    assert options["trusted_proxy"] == "*"
    assert options["trusted_proxy_count"] == 1
    assert options["trusted_proxy_headers"] == {"x-forwarded-proto", "x-forwarded-for"}
    assert options["clear_untrusted_proxy_headers"] is True


def test_django_takes_the_scheme_from_the_forwarded_header():
    request = RequestFactory().get("/accounts/login/", headers={"x-forwarded-proto": "https"})

    assert request.is_secure() is True


def test_production_refuses_to_start_without_a_trusted_proxy(settings):
    settings.DEBUG = False
    settings.OMRON_TRUSTED_PROXY = ""

    with pytest.raises(SystemExit, match="OMRON_TRUSTED_PROXY is empty"):
        verify_proxy_configuration()


def test_production_starts_with_a_declared_trusted_proxy(settings):
    settings.DEBUG = False
    settings.OMRON_TRUSTED_PROXY = "*"

    verify_proxy_configuration()


def test_debug_starts_without_a_trusted_proxy(settings):
    settings.DEBUG = True
    settings.OMRON_TRUSTED_PROXY = ""

    verify_proxy_configuration()


def test_debug_binds_the_server_to_loopback_only(settings):
    settings.DEBUG = True

    assert listen_address(8000) == "127.0.0.1:8000"


def test_production_binds_all_interfaces(settings):
    settings.DEBUG = False

    assert listen_address(8000) == "*:8000"
