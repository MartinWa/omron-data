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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
