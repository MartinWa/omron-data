import os
import re
import subprocess
import sys
from pathlib import Path

from tools.check_no_private_data import candidate_files, content_violations, find_violations

ROOT = Path(__file__).resolve().parents[1]

IMAGE_PROVIDED = frozenset({"OMRON_DEBUG", "OMRON_DB_PATH", "OMRON_STATIC_ROOT", "OMRON_VERSION"})
COMPOSE_ONLY = frozenset({"OMRON_IMAGE", "OMRON_HOST_PORT", "OMRON_NETWORK"})
FILE_SETTINGS = frozenset({"OMRON_SECRET_KEY_FILE"})
UNUSABLE_SECRET_KEYS = frozenset(
    {
        "django-insecure-local-development-only",
        "replace-with-a-long-random-value",
        "dev-only-insecure-key-0000000000000000",
    }
)
ENV_LINE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
COMPOSE_ENTRY = re.compile(r"^\s+-\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*$")
TOP_LEVEL = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")
YAML_FILES = (
    "compose.yaml",
    "compose.host-port.yaml",
    "compose.proxy-network.yaml",
    "compose.local-build.yaml",
    "compose.development.yaml",
)


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def ignore_patterns(name: str) -> list[str]:
    return [line.strip() for line in read(name).splitlines() if line.strip()]


def example_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in read(".env.example").splitlines():
        match = ENV_LINE.match(line.strip())
        if match is not None:
            entries[match.group("name")] = match.group("value").strip()
    return entries


def documented_names() -> set[str]:
    return {name for name in example_entries() if name.startswith("OMRON_")}


def settings_names() -> set[str]:
    return set(re.findall(r"OMRON_[A-Z_]+", read("omron_data/settings.py")))


def compose_environment() -> list[str]:
    found: list[str] = []
    inside = False
    for line in read("compose.yaml").splitlines():
        if line.strip() == "environment:":
            inside = True
            continue
        if not inside:
            continue
        match = COMPOSE_ENTRY.match(line)
        if match is None:
            break
        found.append(match.group("name"))
    return found


def production_stage() -> str:
    dockerfile = read("Dockerfile")
    return dockerfile[dockerfile.index("FROM runtime-base AS production") :]


def yaml_problems(text: str) -> list[str]:
    problems: list[str] = []
    if "\t" in text:
        problems.append("tab character")
    if not text.endswith("\n"):
        problems.append("missing final newline")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            problems.append(f"line {number}: odd indentation")
        if not stripped.startswith("- ") and TOP_LEVEL.match(stripped) is None:
            problems.append(f"line {number}: neither a mapping nor a sequence entry")
    return problems


def test_gitignore_starts_with_the_private_directories():
    patterns = ignore_patterns(".gitignore")

    assert patterns[:2] == ["planning/", "reference/"]
    for required in (
        "data/",
        ".data/",
        "*.sqlite3-*",
        "*.backup*",
        "*.bak",
        "*.csv",
        ".venv/",
        "staticfiles/",
    ):
        assert required in patterns


def test_dockerignore_excludes_the_same_private_paths():
    patterns = ignore_patterns(".dockerignore")

    assert patterns[:2] == [".git", ".gitignore"]
    for required in (
        "planning/",
        "reference/",
        ".data/",
        "data/",
        ".venv/",
        "staticfiles/",
        "*.csv",
        "*.sqlite3",
        "*.sqlite3-*",
        ".env",
        ".env.*",
    ):
        assert required in patterns
    assert "!.env.example" in patterns


def test_example_environment_lists_only_variables_the_code_reads():
    names = documented_names()

    assert "OMRON_IMAGE" in names
    assert names - COMPOSE_ONLY - FILE_SETTINGS <= settings_names()
    assert names & {"OMRON_DEBUG", "OMRON_DB_PATH", "OMRON_PORT"} == set()


def test_every_operator_setting_of_the_settings_module_is_documented():
    missing = settings_names() - IMAGE_PROVIDED - documented_names()

    assert missing == set()


def test_example_environment_starts_with_the_values_that_have_no_default():
    listed = [line.split("=", 1)[0] for line in read(".env.example").splitlines() if "=" in line]

    assert listed[:3] == ["OMRON_SECRET_KEY", "OMRON_ALLOWED_HOSTS", "OMRON_IMAGE"]


def test_example_secret_key_is_empty_or_a_rejected_placeholder():
    value = example_entries()["OMRON_SECRET_KEY"]

    assert value == "" or value in UNUSABLE_SECRET_KEYS


def test_example_secret_key_cannot_start_a_production_container():
    value = example_entries()["OMRON_SECRET_KEY"]
    environment = {key: item for key, item in os.environ.items() if not key.startswith("OMRON_")}
    environment.update(
        {
            "PYTHONPATH": str(ROOT),
            "OMRON_SECRET_KEY": value,
            "DJANGO_SETTINGS_MODULE": "omron_data.settings",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import omron_data.settings"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout
    assert b"OMRON_SECRET_KEY" in result.stderr, result.stderr


def test_production_compose_passes_environment_names_without_values():
    entries = compose_environment()

    assert "OMRON_SECRET_KEY" in entries
    assert "OMRON_TIME_ZONE" in entries
    for entry in entries:
        assert "${" not in entry
        assert "=" not in entry


def test_production_compose_takes_the_database_path_from_the_image():
    text = read("compose.yaml")

    assert "OMRON_DB_PATH" not in text
    assert "OMRON_STATIC_ROOT" not in text
    assert "OMRON_DEBUG" not in text
    assert "OMRON_PORT" not in text
    assert "env_file" not in text
    assert "build:" not in text


def test_production_compose_hardens_the_container():
    text = read("compose.yaml")

    for hardening in (
        "read_only: true",
        "cap_drop",
        "no-new-privileges:true",
        "init: true",
        "stop_grace_period: 30s",
        "tmpfs:",
        "max-size",
        'user: "${PUID:-1000}:${PGID:-1000}"',
        'name: "${OMRON_NETWORK:-omron-data}"',
        "- ./data:/app/data",
    ):
        assert hardening in text


def test_overlays_cover_the_documented_patterns():
    host_port = read("compose.host-port.yaml")
    proxy_network = read("compose.proxy-network.yaml")
    local_build = read("compose.local-build.yaml")
    development = read("compose.development.yaml")

    assert '"127.0.0.1:${OMRON_HOST_PORT:-8000}:8000"' in host_port
    assert "external: true" in proxy_network
    assert 'name: "${PROXY_NETWORK:?Set the existing private proxy network}"' in proxy_network
    assert "image: omron-data:local" in local_build
    assert "pull_policy: build" in local_build
    assert "target: production" in local_build
    assert "OMRON_IMAGE" not in local_build
    assert "target: development" in development
    assert "type: bind" in development
    assert "env_file" not in development


def test_compose_files_are_well_formed():
    for name in YAML_FILES:
        text = read(name)

        assert yaml_problems(text) == [], name
        top_level = {
            match.group("key")
            for line in text.splitlines()
            if (match := TOP_LEVEL.match(line)) is not None
        }
        assert top_level <= {"name", "networks", "secrets", "services", "volumes"}, name


def test_production_image_is_unprivileged_and_has_a_healthcheck():
    stage = production_stage()

    assert "USER 1000:1000" in stage
    assert "HEALTHCHECK" in stage
    assert "tools/healthcheck.py" in stage
    assert "/app/data" in stage
    assert "collectstatic" in stage


def test_production_image_copies_the_directories_the_settings_need():
    stage = production_stage()

    for needed in (
        "COPY manage.py /app/manage.py",
        "COPY omron_data /app/omron_data",
        "COPY parsing /app/parsing",
        "COPY records /app/records",
        "COPY templates /app/templates",
    ):
        assert needed in stage
    assert "pyproject.toml" in stage
    assert "OMRON_STATIC_ROOT=/app/staticfiles" in stage


def test_image_is_multi_stage_and_needs_no_pip_at_runtime():
    dockerfile = read("Dockerfile")

    assert "AS uv-tool" in dockerfile
    assert "AS os-base" in dockerfile
    assert "AS development" in dockerfile
    assert "AS production" in dockerfile
    assert "uv sync --locked" in dockerfile
    assert "pip install" not in dockerfile
    assert "ARG OMRON_VERSION" in dockerfile
    assert "ENV OMRON_VERSION=${OMRON_VERSION}" in dockerfile
    assert dockerfile.rstrip().endswith('CMD ["python", "-m", "tools.serve"]')


def test_operational_tools_use_the_standard_library_only():
    healthcheck = read("tools/healthcheck.py")
    serve = read("tools/serve.py")
    production = read("tools/check_production.py")

    assert "/api/v1/readyz" in healthcheck
    assert 'os.environ.get("OMRON_PORT"' in healthcheck
    assert "TIMEOUT_SECONDS = 3.0" in healthcheck
    assert "status != 200" in healthcheck
    assert "from omron_data.wsgi import application" in serve
    assert 'positive_int("OMRON_THREADS"' in serve
    assert "threads=" in serve
    assert "verify_proxy_configuration()" in serve
    for gate in (
        "settings.DEBUG",
        "check_trusted_proxy",
        "PLACEHOLDER_SECRET_KEYS",
        "mode=rw",
        "migration_plan",
    ):
        assert gate in production


def test_private_data_scan_allows_source_files():
    paths = ["README.md", "pyproject.toml", "uv.lock", "omron_data/settings.py", ".env.example"]

    assert find_violations(paths) == []


def test_a_database_backup_is_ignored_and_refused_by_name():
    patterns = ignore_patterns(".gitignore")
    docker = ignore_patterns(".dockerignore")
    name = "db.sqlite3.backup-20261126-135446"

    assert "*.backup*" in patterns
    assert "*.backup*" in docker
    assert find_violations([name]) == [name]
    assert find_violations(["docs/backup-and-recovery.md"]) == []


def test_private_data_scan_rejects_private_files():
    paths = [
        "export.csv",
        "state.db",
        "state.db-wal",
        "db.sqlite3-wal",
        ".env",
        ".env.local",
        "token.txt",
        "api-token.json",
        "planning/plan.md",
        "reference/init.sql",
        "db.sqlite3.backup-20261126-135446",
        "omron.sqlite3.bak",
        "history.db.backup",
    ]

    assert find_violations(paths) == sorted(paths)


def test_private_data_scan_flags_private_content(tmp_path):
    sample = tmp_path / "notes.md"
    email = "real.person" + "@" + "corp.example"
    host_path = "C:" + "\\Users\\someone\\export.csv"
    bearer = "Bearer " + "sk-0123456789abcdefghij"
    sample.write_text(f"contact {email}\npath {host_path}\nauth {bearer}\n", encoding="utf-8")

    assert content_violations([str(sample)]) == [
        f"{sample}:1: email address",
        f"{sample}:2: absolute host path",
        f"{sample}:3: bearer token",
    ]


def test_private_data_scan_allows_synthetic_examples(tmp_path):
    sample = tmp_path / "notes.md"
    sample.write_text(
        "user@example.com\nhttps://omron.example.com/api/v1/healthz\neditor:/home/developer\n",
        encoding="utf-8",
    )

    assert content_violations([str(sample)]) == []


def test_private_data_scan_honours_the_allow_marker(tmp_path):
    sample = tmp_path / "notes.md"
    email = "real.person" + "@" + "corp.example"
    sample.write_text(f"contact {email}  <!-- hygiene: allow -->\n", encoding="utf-8")

    assert content_violations([str(sample)]) == []


def test_private_data_scan_passes_on_this_tree():
    files = candidate_files(ROOT)

    assert find_violations(files) == []
    assert content_violations(files) == []
