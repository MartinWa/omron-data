import importlib
import os
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

import omron_data.settings as settings_module

ENVIRONMENT_KEYS = (
    "OMRON_DEBUG",
    "OMRON_SECRET_KEY",
    "OMRON_SECRET_KEY_FILE",
    "OMRON_ALLOWED_HOSTS",
    "OMRON_CSRF_TRUSTED_ORIGINS",
    "OMRON_TIME_ZONE",
    "OMRON_STATIC_ROOT",
    "OMRON_VERSION",
    "OMRON_DB_PATH",
)
DEVELOPMENT_KEY = settings_module.DEVELOPMENT_SECRET_KEY
PLACEHOLDERS = sorted(settings_module.PLACEHOLDER_SECRET_KEYS)
REAL_KEY = "0f9c1d2b3a4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddee"
ENV_EXAMPLE = Path(settings_module.BASE_DIR) / ".env.example"


def env_example_secret() -> str:
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("OMRON_SECRET_KEY="):
            return line.partition("=")[2]
    raise AssertionError("OMRON_SECRET_KEY is missing from .env.example")


@pytest.fixture(autouse=True)
def restored_settings(monkeypatch, tmp_path):
    snapshot = {name: os.environ.get(name) for name in ENVIRONMENT_KEYS}
    monkeypatch.setenv("OMRON_DB_PATH", str(tmp_path / "db.sqlite3"))
    yield
    for name, value in snapshot.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    importlib.reload(settings_module)


def reload_settings(monkeypatch, environment=None):
    for name in ENVIRONMENT_KEYS:
        if name != "OMRON_DB_PATH":
            monkeypatch.delenv(name, raising=False)
    for name, value in (environment or {}).items():
        monkeypatch.setenv(name, value)
    return importlib.reload(settings_module)


def test_omron_debug_accepts_one_true_yes_and_on(monkeypatch):
    for word in ("1", "true", "yes", "on", "TRUE", "On"):
        assert reload_settings(monkeypatch, {"OMRON_DEBUG": word}).DEBUG is True


def test_omron_debug_accepts_zero_false_no_and_off(monkeypatch):
    for word in ("0", "false", "no", "off", "FALSE", "No"):
        module = reload_settings(monkeypatch, {"OMRON_DEBUG": word, "OMRON_SECRET_KEY": REAL_KEY})
        assert module.DEBUG is False


def test_omron_debug_refuses_any_other_word_and_names_the_variable(monkeypatch):
    for word in ("maybe", "2", "truthy", "enabled"):
        with pytest.raises(
            ImproperlyConfigured, match=f"OMRON_DEBUG must be true or false, not {word!r}"
        ):
            reload_settings(monkeypatch, {"OMRON_DEBUG": word})


def test_omron_debug_defaults_to_false_when_it_is_unset(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY})

    assert module.DEBUG is False


def test_a_missing_secret_with_debug_off_is_refused(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="OMRON_SECRET_KEY must be set"):
        reload_settings(monkeypatch, {"OMRON_DEBUG": "0"})


def test_every_known_placeholder_with_debug_off_is_refused(monkeypatch):
    for placeholder in PLACEHOLDERS:
        with pytest.raises(ImproperlyConfigured, match="OMRON_SECRET_KEY"):
            reload_settings(monkeypatch, {"OMRON_DEBUG": "0", "OMRON_SECRET_KEY": placeholder})


def test_the_published_env_example_placeholder_is_refused(monkeypatch):
    placeholder = "dev-only-insecure-key-0000000000000000"

    assert placeholder in PLACEHOLDERS
    with pytest.raises(ImproperlyConfigured, match="OMRON_SECRET_KEY"):
        reload_settings(monkeypatch, {"OMRON_SECRET_KEY": placeholder})


def test_env_example_never_ships_a_secret_that_would_be_accepted(monkeypatch):
    shipped = env_example_secret()

    assert shipped == "" or shipped in PLACEHOLDERS
    if shipped:
        with pytest.raises(ImproperlyConfigured, match="OMRON_SECRET_KEY"):
            reload_settings(monkeypatch, {"OMRON_SECRET_KEY": shipped})


def test_the_reference_project_placeholder_is_refused(monkeypatch):
    placeholder = "replace-with-a-long-random-value"

    assert placeholder in PLACEHOLDERS
    with pytest.raises(ImproperlyConfigured, match="OMRON_SECRET_KEY"):
        reload_settings(monkeypatch, {"OMRON_SECRET_KEY": placeholder})


def test_a_placeholder_falls_back_to_the_development_key_when_debug_is_on(monkeypatch):
    for placeholder in PLACEHOLDERS:
        module = reload_settings(monkeypatch, {"OMRON_DEBUG": "1", "OMRON_SECRET_KEY": placeholder})
        assert module.SECRET_KEY == DEVELOPMENT_KEY


def test_an_unset_secret_falls_back_to_the_development_key_when_debug_is_on(monkeypatch):
    assert reload_settings(monkeypatch, {"OMRON_DEBUG": "true"}).SECRET_KEY == DEVELOPMENT_KEY


def test_a_real_secret_is_used_verbatim(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY})

    assert module.SECRET_KEY == REAL_KEY


def test_the_secret_is_read_from_a_file(monkeypatch, tmp_path):
    path = tmp_path / "secret.txt"
    path.write_text(f"  {REAL_KEY}\n", encoding="utf-8")

    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY_FILE": str(path)})

    assert module.SECRET_KEY == REAL_KEY


def test_the_session_and_csrf_cookies_are_secure_when_debug_is_off(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY})

    assert module.SESSION_COOKIE_SECURE is True
    assert module.CSRF_COOKIE_SECURE is True


def test_the_session_and_csrf_cookies_are_plain_when_debug_is_on(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_DEBUG": "1"})

    assert module.SESSION_COOKIE_SECURE is False
    assert module.CSRF_COOKIE_SECURE is False


def test_allowed_hosts_defaults_to_loopback(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY})

    assert module.ALLOWED_HOSTS == ["127.0.0.1", "localhost"]


def test_allowed_hosts_splits_a_comma_list(monkeypatch):
    module = reload_settings(
        monkeypatch,
        {"OMRON_SECRET_KEY": REAL_KEY, "OMRON_ALLOWED_HOSTS": "example.test,10.1.2.3,,"},
    )

    assert module.ALLOWED_HOSTS == ["example.test", "10.1.2.3"]


def test_csrf_trusted_origins_default_to_nothing(monkeypatch):
    assert reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY}).CSRF_TRUSTED_ORIGINS == []


def test_csrf_trusted_origins_split_a_comma_list(monkeypatch):
    module = reload_settings(
        monkeypatch,
        {
            "OMRON_SECRET_KEY": REAL_KEY,
            "OMRON_CSRF_TRUSTED_ORIGINS": "https://one.test, https://two.test ,",
        },
    )

    assert module.CSRF_TRUSTED_ORIGINS == ["https://one.test", "https://two.test"]


def test_a_good_iana_zone_is_accepted_and_time_zone_follows_it(monkeypatch):
    module = reload_settings(
        monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY, "OMRON_TIME_ZONE": "America/New_York"}
    )

    assert module.OMRON_TIME_ZONE == "America/New_York"
    assert module.TIME_ZONE == "America/New_York"


def test_the_zone_defaults_to_stockholm(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY})

    assert module.OMRON_TIME_ZONE == "Europe/Stockholm"
    assert module.TIME_ZONE == module.OMRON_TIME_ZONE


def test_an_unknown_zone_is_refused_with_the_documented_message(monkeypatch):
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"OMRON_TIME_ZONE must be an IANA zone name such as Europe/Stockholm, "
            r"not 'Europe/Nowhere'\."
        ),
    ):
        reload_settings(
            monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY, "OMRON_TIME_ZONE": "Europe/Nowhere"}
        )


def test_a_nonsense_zone_is_refused_with_the_documented_message(monkeypatch):
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"OMRON_TIME_ZONE must be an IANA zone name such as Europe/Stockholm, "
            r"not 'not a time zone!!'\."
        ),
    ):
        reload_settings(
            monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY, "OMRON_TIME_ZONE": "not a time zone!!"}
        )


def test_static_root_comes_from_the_environment(monkeypatch, tmp_path):
    module = reload_settings(
        monkeypatch,
        {"OMRON_SECRET_KEY": REAL_KEY, "OMRON_STATIC_ROOT": str(tmp_path / "collected")},
    )

    assert Path(module.STATIC_ROOT).name == "collected"
    assert Path(module.STATIC_ROOT).parent == tmp_path


def test_the_default_static_root_ends_with_staticfiles(monkeypatch):
    module = reload_settings(monkeypatch, {"OMRON_SECRET_KEY": REAL_KEY})

    assert Path(module.STATIC_ROOT).name == "staticfiles"


def test_the_module_promises_the_login_urls_and_timezone_support():
    assert settings_module.LOGIN_URL == "login"
    assert settings_module.LOGIN_REDIRECT_URL == "readings"
    assert settings_module.USE_TZ is True


def test_the_module_installs_the_admin():
    assert "django.contrib.admin" in settings_module.INSTALLED_APPS


def test_the_api_middleware_runs_first():
    assert settings_module.MIDDLEWARE[0] == "records.middleware.ApiResponseMiddleware"


def test_whitenoise_runs_right_after_security_and_before_common():
    stack = settings_module.MIDDLEWARE
    security = stack.index("django.middleware.security.SecurityMiddleware")
    whitenoise = stack.index("whitenoise.middleware.WhiteNoiseMiddleware")
    common = stack.index("django.middleware.common.CommonMiddleware")

    assert stack[security + 1] == "whitenoise.middleware.WhiteNoiseMiddleware"
    assert whitenoise < common
