"""Fail when private data is present in the working tree, by name or by content.

A line that legitimately needs to look like private data can opt out of the content
scan by carrying the marker ``hygiene: allow``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
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
    ".pdf",
    ".pem",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".zip",
)
SIDECAR_MARKERS = (".db-", ".sqlite-", ".sqlite3-")
BACKUP_MARKERS = (".backup", ".bak")
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
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = frozenset({b"tEXt", b"iTXt", b"zTXt", b"eXIf"})
JPEG_EXIF_MARKER = b"Exif\x00\x00"

CONTENT_PATTERNS = (
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(secret[_-]?key|api[_-]?key|token|password)\b\s*[:=]\s*[\"']"
            r"[A-Za-z0-9_\-/+]{32,}[\"']"
        ),
    ),
    (
        "email address",
        re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    (
        "absolute host path",
        re.compile(
            r"(?:[A-Za-z]:\\Users\\|/Users/[A-Za-z]|/home/(?!developer\b)[A-Za-z]|/(?:root|srv|mnt)/)"
        ),
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
    if any(marker in name for marker in BACKUP_MARKERS):
        return True
    if TOKEN_FILE_NAME.match(name):
        return True
    return name.endswith(BAD_SUFFIXES)


def find_violations(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if is_violation(path))


def png_metadata_chunks(data: bytes) -> list[str]:
    chunks = []
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        if chunk_type in PNG_METADATA_CHUNKS:
            chunks.append(chunk_type.decode("latin-1"))
        offset += 12 + length
        if chunk_type == b"IEND":
            break
    return chunks


def content_violations(paths: Iterable[str]) -> list[str]:
    findings = []
    for path in paths:
        candidate = Path(path)
        name = candidate.name.lower()
        try:
            if candidate.stat().st_size > MAX_SCAN_BYTES:
                continue
            if name.endswith(".png"):
                data = candidate.read_bytes()
                if data.startswith(PNG_SIGNATURE):
                    for chunk in png_metadata_chunks(data):
                        findings.append(f"{path}: PNG {chunk} metadata chunk")
                continue
            if name.endswith((".jpg", ".jpeg")):
                if JPEG_EXIF_MARKER in candidate.read_bytes():
                    findings.append(f"{path}: JPEG Exif metadata")
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


def select_files(parser: argparse.ArgumentParser, untracked: bool) -> list[str]:
    if not untracked:
        return candidate_files()
    try:
        return untracked_files()
    except FileNotFoundError:
        parser.exit(1, "--untracked needs git: no git executable was found on PATH.\n")
    except subprocess.CalledProcessError:
        parser.exit(1, "--untracked needs a git work tree: git ls-files --others failed.\n")


def untracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],  # noqa: S607
        capture_output=True,
        check=True,
    )
    return [entry.decode() for entry in result.stdout.split(b"\0") if entry]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--untracked",
        action="store_true",
        help="scan the files git reports as untracked (git ls-files --others --exclude-standard)",
    )
    options = parser.parse_args(argv)
    files = select_files(parser, options.untracked)
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
