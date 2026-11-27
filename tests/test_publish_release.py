"""Release publishing: the tag must be stable, tested on main, and never promoted downwards."""

import sys
from pathlib import Path

import pytest

from tools import publish_release

ROOT = Path(__file__).resolve().parents[1]
CHECK_STEP = "        id: release\n"
RELEASE_STEP = "        id: github-release\n"


def workflow() -> str:
    return (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")


def step(text: str, identifier: str) -> str:
    marker = text.index(identifier)
    start = text.rindex("      - name:", 0, marker)
    end = text.find("\n      - name:", marker)
    return text[start:] if end == -1 else text[start:end]


def release(tag, *, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


class FakeResult:
    def __init__(self, stdout="[]", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class FakeClock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("0.10.14", (0, 10, 14)),
        ("v1.2.3", None),
        ("1.2", None),
        ("1.2.3-rc1", None),
        ("01.2.3", None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_version_number_accepts_only_a_stable_tag(value, expected):
    assert publish_release.version_number(value) == expected


def test_wait_for_ci_run_returns_when_ci_concludes(monkeypatch):
    runs = [
        {"conclusion": None},
        {"conclusion": None},
        {"conclusion": "success"},
    ]
    monkeypatch.setattr(publish_release, "read_ci_run", lambda commit: runs.pop(0))
    slept = []

    run = publish_release.wait_for_ci_run(
        "abc123",
        wait_seconds=60,
        poll_seconds=5,
        sleep=slept.append,
        monotonic=FakeClock(0, 5, 10),
    )

    assert run == {"conclusion": "success"}
    assert slept == [5, 5]


def test_wait_for_ci_run_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(publish_release, "read_ci_run", lambda commit: {"conclusion": None})
    slept = []

    run = publish_release.wait_for_ci_run(
        "abc123",
        wait_seconds=60,
        poll_seconds=5,
        sleep=slept.append,
        monotonic=FakeClock(0, 60),
    )

    assert run == {"conclusion": None}
    assert slept == []


def test_wait_for_ci_run_tolerates_a_run_that_does_not_exist_yet(monkeypatch):
    runs = [None, {"conclusion": "success"}]
    monkeypatch.setattr(publish_release, "read_ci_run", lambda commit: runs.pop(0))

    run = publish_release.wait_for_ci_run(
        "abc123",
        wait_seconds=60,
        poll_seconds=5,
        sleep=lambda seconds: None,
        monotonic=FakeClock(0, 5),
    )

    assert run == {"conclusion": "success"}


def test_validate_source_rejects_a_commit_whose_ci_did_not_pass(monkeypatch):
    monkeypatch.setattr(publish_release, "read_command", lambda *arguments: "abc123")
    monkeypatch.setattr(
        publish_release.subprocess, "run", lambda *arguments, **options: FakeResult()
    )
    monkeypatch.setattr(
        publish_release,
        "wait_for_ci_run",
        lambda commit: {
            "conclusion": "failure",
            "headSha": "abc123",
            "headBranch": "main",
            "event": "push",
        },
    )

    with pytest.raises(ValueError, match="successful CI push run on main"):
        publish_release.validate_source("1.0.0", tag_created="true")


def test_validate_source_rejects_a_tag_that_is_not_new():
    with pytest.raises(ValueError, match="must be new"):
        publish_release.validate_source("1.0.0", tag_created="false")


def test_read_ci_run_asks_for_the_push_run_on_main(monkeypatch):
    calls = []

    def capture(*arguments):
        calls.append(arguments)
        return "[]"

    monkeypatch.setattr(publish_release, "read_command", capture)

    assert publish_release.read_ci_run("abc123") is None
    arguments = calls[0]

    assert arguments[:2] == ("gh", "run")
    for required in ("--workflow", "ci.yml", "--branch", "main", "--event", "push"):
        assert required in arguments
    assert "abc123" in arguments


def test_check_never_writes_a_release(monkeypatch, tmp_path):
    output = tmp_path / "github-output"
    output.write_text("", encoding="utf-8")
    recorded = []

    def record(*arguments, **options):
        recorded.append(arguments)
        return FakeResult()

    monkeypatch.setattr(publish_release, "validate_source", lambda tag, *, tag_created: "abc123")
    monkeypatch.setattr(publish_release.subprocess, "run", record)
    monkeypatch.setattr(sys, "argv", ["publish_release.py", "--check"])
    monkeypatch.setenv("RELEASE_TAG", "1.2.3")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/omron-data")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    publish_release.main()

    written = output.read_text(encoding="utf-8")

    assert "version=1.2.3\n" in written
    assert "commit=abc123\n" in written
    assert not [call for call in recorded if call[:2] == ("gh", "release")]


def test_release_plan_creates_the_first_release_with_generated_notes():
    arguments, latest = publish_release.release_plan("1.2.3", [])

    assert arguments == [
        "create",
        "1.2.3",
        "--verify-tag",
        "--title",
        "1.2.3",
        "--generate-notes",
        "--latest=true",
    ]
    assert latest is True


def test_release_plan_does_not_promote_a_lower_version():
    arguments, latest = publish_release.release_plan("1.2.2", [release("1.2.3")])

    assert "--generate-notes" in arguments
    assert arguments[-1] == "--latest=false"
    assert latest is False


def test_release_plan_completes_an_existing_draft():
    arguments, latest = publish_release.release_plan("1.2.3", [release("1.2.3", draft=True)])

    assert arguments == ["edit", "1.2.3", "--draft=false", "--verify-tag", "--latest=true"]
    assert latest is True


def test_release_plan_leaves_an_already_published_release_alone():
    arguments, latest = publish_release.release_plan("1.2.3", [release("1.2.3")])

    assert arguments == []
    assert latest is True


def test_the_workflow_creates_the_release_for_the_tag_it_published():
    text = workflow()
    check = step(text, CHECK_STEP)

    assert "run: python3 tools/publish_release.py --check" in check
    assert "TAG_CREATED: ${{ github.event.created }}" in check
    assert "run: python3 tools/publish_release.py\n" in step(text, RELEASE_STEP)
    assert text.index(CHECK_STEP) < text.index(RELEASE_STEP)
    assert text.find("- name:", text.index(RELEASE_STEP)) == -1
    assert "if: ${{ !github.event.deleted }}" in text
    assert "contents: write" in text
    assert "actions: read" in text
    assert "fetch-depth: 0" in text
