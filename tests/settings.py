import os

SETTINGS_ENVIRONMENT = (
    "OMRON_ALLOWED_HOSTS",
    "OMRON_CSRF_TRUSTED_ORIGINS",
    "OMRON_DEBUG",
    "OMRON_DB_PATH",
    "OMRON_PENDING_DIR",
    "OMRON_PORT",
    "OMRON_SECRET_KEY",
    "OMRON_SECRET_KEY_FILE",
    "OMRON_STATIC_ROOT",
    "OMRON_THREADS",
    "OMRON_TIME_ZONE",
    "OMRON_TRUSTED_PROXY",
    "OMRON_VERSION",
)

for name in SETTINGS_ENVIRONMENT:
    os.environ.pop(name, None)

os.environ["OMRON_SECRET_KEY"] = "test-only-secret-key-never-for-runtime-000000000000"

from omron_data.settings import *  # noqa: E402,F403

AXES_ENABLED = False
