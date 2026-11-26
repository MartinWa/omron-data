import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("OMRON_SECRET_KEY", "dev-only-insecure-key-0000000000000000")
DEBUG = os.environ.get("OMRON_DEBUG") == "1"
_hosts = os.environ.get("OMRON_ALLOWED_HOSTS", "127.0.0.1,localhost")
ALLOWED_HOSTS = [host for host in _hosts.split(",") if host]
OMRON_TIME_ZONE = os.environ.get("OMRON_TIME_ZONE", "Europe/Stockholm")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ninja",
    "records",
]

MIDDLEWARE = [
    "records.middleware.ApiResponseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "omron_data.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "omron_data.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("OMRON_DB_PATH", BASE_DIR / ".data" / "db.sqlite3"),
    }
}

Path(DATABASES["default"]["NAME"]).parent.mkdir(parents=True, exist_ok=True)

AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = OMRON_TIME_ZONE
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000
