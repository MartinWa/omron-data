"""Fail unless this installation is configured and migrated for production."""

import os
import sys
from pathlib import Path

PLACEHOLDER_SECRET_KEYS = frozenset(
    {
        "django-insecure-local-development-only",
        "replace-with-a-long-random-value",
        "dev-only-insecure-key-0000000000000000",
    }
)
DEFAULT_SETTINGS_MODULE = "omron_data.settings"


def add_repository_root_to_path() -> None:
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def check_debug(settings) -> list[str]:
    if settings.DEBUG:
        return ["OMRON_DEBUG is on; production must serve with DEBUG off."]
    return []


def check_trusted_proxy(settings) -> list[str]:
    if settings.DEBUG or settings.OMRON_TRUSTED_PROXY:
        return []
    return [
        "OMRON_TRUSTED_PROXY is empty while OMRON_DEBUG is false; the Secure session and "
        "CSRF cookies then require a TLS proxy, and without a declared peer every browser "
        "login fails the CSRF origin check."
    ]


def check_secret(settings) -> list[str]:
    secret = settings.SECRET_KEY or ""
    if not secret.strip() or secret in PLACEHOLDER_SECRET_KEYS:
        return [
            "OMRON_SECRET_KEY is empty or a published placeholder; set a unique random value "
            'with python -c "import secrets; print(secrets.token_urlsafe(48))".'
        ]
    return []


def check_database(settings) -> list[str]:
    from django.db import connections

    from records.db import probe_database

    path = Path(settings.DATABASES["default"]["NAME"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write-probe"
        probe.touch()
        probe.unlink()
    except OSError as error:
        return [f"{path.parent} is not writable: {error}"]
    try:
        pending = probe_database(connections["default"])
    except Exception as error:
        return [f"Database check failed for {path}: {error}"]
    if pending:
        listed = ", ".join(pending[:5])
        return [f"{len(pending)} migration(s) pending ({listed}). Run manage.py migrate."]
    return []


def collect() -> list[str]:
    from django.conf import settings

    return (
        check_debug(settings)
        + check_trusted_proxy(settings)
        + check_secret(settings)
        + check_database(settings)
    )


def main() -> int:
    add_repository_root_to_path()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", DEFAULT_SETTINGS_MODULE)
    import django
    from django.core.exceptions import ImproperlyConfigured

    try:
        django.setup()
        problems = collect()
    except ImproperlyConfigured as error:
        print(f"Production check failed: {error}", file=sys.stderr)
        return 1
    if problems:
        print("Production check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(
        "Production check passed: DEBUG off, a declared trusted proxy, a private secret, a "
        "writable database and a complete, consistent migration history."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
