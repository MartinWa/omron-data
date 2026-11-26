"""Fail when private data is present in the working tree, by name or by content.

A line that legitimately needs to look like private data can opt out of the content
scan by carrying the marker ``hygiene: allow``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

BAD_SUFFIXES = (
    ".7z",
    ".bak",
    ".backup",
    ".csv",
    ".db",
    ".dump",
    ".gz",
    ".key",
    ".pem",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".zip",
)
SIDECAR_MARKERS = (".db-", ".sqlite-", ".sqlite3-")
PRIVATE_DIRECTORIES = frozenset({"planning", "reference"})
TOKEN_FILE_NAME = re.compile(r"^(?:[A-Za-z0-9_.-]*[-_.])?tokens?\.(?:json|txt|ya?ml|key|pem)$")
SKIPPED_DIRECTORIES = frozenset(
    {
        ".data",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "data",
        "node_modules",
        "planning",
        "reference",
        "staticfiles",
    }
)
MAX_SCAN_BYTES = 2 * 1024 * 1024
ALLOW_MARKER = "hygiene: allow"

CONTENT_PATTERNS = (
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "email address",
        re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    (
        "absolute host path",
        re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/[A-Za-z]|/home/(?!developer\b)[A-Za-z])"),
    ),
)


def is_violation(path: str) -> bool:
    candidate = PurePosixPath(path)
    if PRIVATE_DIRECTORIES.intersection(candidate.parts):
        return True
    name = candidate.name.lower()
    if name.startswith(".env") and name != ".env.example":
        return True
    if any(marker in name for marker in SIDECAR_MARKERS):
        return True
    if TOKEN_FILE_NAME.match(name):
        return True
    return name.endswith(BAD_SUFFIXES)


def find_violations(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if is_violation(path))


def content_violations(paths: Iterable[str]) -> list[str]:
    findings = []
    for path in paths:
        candidate = Path(path)
        try:
            if candidate.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = candidate.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            for label, pattern in CONTENT_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path}:{line_number}: {label}")
    return findings


def walk(directory: Path, root: Path) -> Iterator[str]:
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            if entry.name in SKIPPED_DIRECTORIES:
                continue
            yield from walk(entry, root)
        elif entry.is_file():
            yield entry.relative_to(root).as_posix()


def candidate_files(root: Path | None = None) -> list[str]:
    base = Path.cwd() if root is None else root
    return list(walk(base, base))


def main() -> int:
    files = candidate_files()
    violations = find_violations(files)
    findings = content_violations(files)
    if violations:
        print("Private-data files found in the working tree:")
        for path in violations:
            print(f"  {path}")
    if findings:
        print("Private data found in file contents:")
        for finding in findings:
            print(f"  {finding}")
    if violations or findings:
        print(f"Scanned {len(files)} files. Remove or ignore the entries above.")
        return 1
    print(f"Hygiene check passed: {len(files)} files scanned, no private data found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
