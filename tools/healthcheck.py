"""Probe the liveness endpoint the container health check watches."""

import http.client
import os
import sys

TIMEOUT_SECONDS = 3.0
HEALTH_PATH = "/api/v1/healthz"
DEFAULT_PORT = "8000"


def port() -> str:
    return os.environ.get("OMRON_PORT", "").strip() or DEFAULT_PORT


def target_url() -> str:
    return f"http://127.0.0.1:{port()}{HEALTH_PATH}"


def probe_host() -> str:
    hosts = [
        part.strip()
        for part in os.environ.get("OMRON_ALLOWED_HOSTS", "").split(",")
        if part.strip()
    ]
    if not hosts or hosts[0] in {"*", "."}:
        return "localhost"
    return hosts[0].lstrip(".")


def probe() -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port(), timeout=TIMEOUT_SECONDS)
    try:
        connection.request("GET", HEALTH_PATH, headers={"Host": probe_host()})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def main() -> int:
    try:
        status = probe()
    except OSError as error:
        print(f"healthcheck: {target_url()} failed: {error}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"healthcheck: {target_url()} answered {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
