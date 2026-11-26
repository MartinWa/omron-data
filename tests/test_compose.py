"""Compose wiring: the web service hands the operator's settings to the container."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSTHROUGH = ("OMRON_SECRET_KEY", "OMRON_ALLOWED_HOSTS", "OMRON_TIME_ZONE")
ENTRY = re.compile(r"^\s+-\s+(\S+)\s*$")


def entries() -> list[str]:
    found: list[str] = []
    inside = False
    for line in (ROOT / "compose.yaml").read_text(encoding="utf-8").splitlines():
        if line.strip() == "environment:":
            inside = True
            continue
        if not inside:
            continue
        match = ENTRY.match(line)
        if match is None:
            break
        found.append(match.group(1))
    return found


def documented() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = {line.split("=", 1)[0].strip() for line in text.splitlines() if line.strip()}
    return {name for name in names if name.startswith("OMRON_")}


def test_compose_passes_the_time_zone_to_the_container():
    assert "OMRON_TIME_ZONE" in entries()


def test_compose_passes_through_every_documented_setting():
    names = {entry.split("=", 1)[0] for entry in entries()}

    assert documented() <= names
    assert set(PASSTHROUGH) <= names


def test_compose_lists_the_passthrough_settings_as_bare_names():
    listed = entries()

    for name in PASSTHROUGH:
        assert name in listed
