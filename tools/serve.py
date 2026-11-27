"""Serve the project with waitress, the same WSGI app the image's CMD runs.

waitress keeps its own SIGTERM and SIGINT handlers: on either signal it stops
accepting connections and drains the requests in flight, which Compose bounds with
stop_grace_period.
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from waitress import serve

from omron_data.wsgi import application
from records.limits import MAX_REQUEST_BODY_BYTES

DEFAULT_PORT = 8000
DEFAULT_THREADS = 8
CHANNEL_TIMEOUT_SECONDS = 120


def positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit(f"{name} must be a whole number, not {raw!r}.") from error
    if value < 1:
        raise SystemExit(f"{name} must be at least 1, not {value}.")
    return value


def proxy_options() -> dict:
    trusted = settings.OMRON_TRUSTED_PROXY
    if not trusted:
        return {}
    return {
        "trusted_proxy": trusted,
        "trusted_proxy_count": 1,
        "trusted_proxy_headers": {"x-forwarded-proto", "x-forwarded-for"},
        "clear_untrusted_proxy_headers": True,
    }


def verify_proxy_configuration() -> None:
    if settings.DEBUG or settings.OMRON_TRUSTED_PROXY:
        return
    raise SystemExit(
        "OMRON_TRUSTED_PROXY is empty while OMRON_DEBUG is false. The session and CSRF "
        "cookies are Secure then, so the app must sit behind a TLS proxy, and waitress "
        "discards X-Forwarded-Proto from any peer it has not been told to trust — every "
        "browser login would fail the CSRF origin check. Set OMRON_TRUSTED_PROXY (for "
        "example * or the proxy's address), or OMRON_DEBUG=true for local direct access."
    )


def database_path() -> Path:
    return Path(settings.DATABASES["default"]["NAME"])


def verify_data_directory() -> None:
    directory = database_path().parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.touch()
        probe.unlink()
    except OSError as error:
        raise SystemExit(
            f"Cannot write to {directory}: {error}. The host directory behind the data mount "
            "must be owned by the UID and GID in PUID and PGID, for example "
            "sudo chown -R 1000:1000 data."
        ) from error


def main() -> int:
    verify_proxy_configuration()
    verify_data_directory()
    call_command("migrate", interactive=False)
    listen = f"*:{positive_int('OMRON_PORT', DEFAULT_PORT)}"
    threads = positive_int("OMRON_THREADS", DEFAULT_THREADS)
    serve(
        application,
        listen=listen,
        threads=threads,
        channel_timeout=CHANNEL_TIMEOUT_SECONDS,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        ident="omron-data",
        **proxy_options(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
