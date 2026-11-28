import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DEVELOPMENT_SECRET_KEY = "django-insecure-local-development-only"
PLACEHOLDER_SECRET_KEYS = frozenset(
    {
        DEVELOPMENT_SECRET_KEY,
        "replace-with-a-long-random-value",
        "dev-only-insecure-key-0000000000000000",
    }
)


def read_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return ""


TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def boolean_setting(name: str, default: bool = False) -> bool:
    raw = read_setting(name).lower()
    if not raw:
        return default
    if raw in TRUE_WORDS:
        return True
    if raw in FALSE_WORDS:
        return False
    raise ImproperlyConfigured(f"{name} must be true or false, not {raw!r}.")


def secret_key(debug: bool) -> str:
    value = read_setting("OMRON_SECRET_KEY")
    if value and value not in PLACEHOLDER_SECRET_KEYS:
        return value
    if debug:
        return DEVELOPMENT_SECRET_KEY
    raise ImproperlyConfigured(
        "OMRON_SECRET_KEY must be set to a unique random value when OMRON_DEBUG is false."
    )


DEBUG = boolean_setting("OMRON_DEBUG")
SECRET_KEY = secret_key(DEBUG)
OMRON_TRUSTED_PROXY = read_setting("OMRON_TRUSTED_PROXY")

_hosts = read_setting("OMRON_ALLOWED_HOSTS") or "127.0.0.1,localhost"
ALLOWED_HOSTS = [host.strip() for host in _hosts.split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in read_setting("OMRON_CSRF_TRUSTED_ORIGINS").split(",")
    if origin.strip()
]


def verify_origins(hosts: list[str], origins: list[str]) -> None:
    for host in hosts:
        if host.strip() == "*":
            raise ImproperlyConfigured(
                "OMRON_ALLOWED_HOSTS must name every hostname the proxy forwards; "
                "'*' would accept any Host header."
            )
    for origin in origins:
        if not origin.strip().startswith("https://"):
            raise ImproperlyConfigured(
                f"OMRON_CSRF_TRUSTED_ORIGINS entries must start with https://, not {origin!r}."
            )


verify_origins(ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS)


def proxy_ssl_header(trusted_proxy: str) -> tuple[str, str] | None:
    return ("HTTP_X_FORWARDED_PROTO", "https") if trusted_proxy.strip() else None


SECURE_PROXY_SSL_HEADER = proxy_ssl_header(OMRON_TRUSTED_PROXY)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG


def default_time_zone() -> str:
    name = read_setting("OMRON_TIME_ZONE") or "Europe/Stockholm"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ImproperlyConfigured(
            f"OMRON_TIME_ZONE must be an IANA zone name such as Europe/Stockholm, not {name!r}."
        ) from exc
    return name


OMRON_TIME_ZONE = default_time_zone()
OMRON_VERSION = read_setting("OMRON_VERSION")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ninja",
    "records",
    "axes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "omron_data.middleware.ContentSecurityPolicyMiddleware",
    "records.middleware.ApiResponseMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "omron_data.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "omron_data.context_processors.app_info",
            ]
        },
    }
]

WSGI_APPLICATION = "omron_data.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": read_setting("OMRON_DB_PATH") or BASE_DIR / ".data" / "db.sqlite3",
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    }
}

Path(DATABASES["default"]["NAME"]).parent.mkdir(parents=True, exist_ok=True)

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AXES_ENABLED = True
AXES_FAILURE_LIMIT = 5
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_COOLOFF_TIME = 1
AXES_RESET_ON_SUCCESS = True
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False
AXES_ENABLE_ACCESS_FAILURE_LOG = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"axes": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = OMRON_TIME_ZONE
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(read_setting("OMRON_STATIC_ROOT") or BASE_DIR / "staticfiles")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "readings"

DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000
