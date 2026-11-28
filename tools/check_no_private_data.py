"""Fail when private data is present in the working tree, by name or by content.

A line that legitimately needs to look like private data can opt out of the content
scan by carrying the marker ``hygiene: allow``.
"""

from __future__ import annotations

import argparse
import io
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
MAX_INDEX_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
ALLOW_MARKER = "hygiene: allow"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = frozenset({b"tEXt", b"iTXt", b"zTXt", b"eXIf"})
JPEG_EXIF_MARKER = b"Exif\x00\x00"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")

CONTENT_PATTERNS = (
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}")),
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


def png_metadata_chunks(stream) -> list[str]:
    if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
        raise ValueError("the file does not start with the PNG signature")
    chunks = []
    while True:
        header = stream.read(8)
        if len(header) < 8:
            raise ValueError("the file ends before its IEND chunk")
        length = int.from_bytes(header[:4], "big")
        chunk_type = header[4:8]
        if chunk_type in PNG_METADATA_CHUNKS:
            chunks.append(chunk_type.decode("latin-1"))
        if chunk_type == b"IEND":
            return chunks
        stream.seek(length + 4, io.SEEK_CUR)


def jpeg_has_exif(stream) -> bool:
    overlap = len(JPEG_EXIF_MARKER) - 1
    carried = b""
    while True:
        block = stream.read(READ_CHUNK_BYTES)
        if not block:
            return False
        if JPEG_EXIF_MARKER in carried + block:
            return True
        carried = (carried + block)[-overlap:]


def image_violations(path: str, name: str, stream) -> list[str]:
    try:
        if name.endswith(".png"):
            return [f"{path}: PNG {chunk} metadata chunk" for chunk in png_metadata_chunks(stream)]
        if jpeg_has_exif(stream):
            return [f"{path}: JPEG Exif metadata"]
        return []
    except OSError, ValueError:
        return [f"{path}: image metadata not inspectable"]


def text_violations(path: str, data: bytes) -> list[str]:
    findings = []
    text = data.decode("utf-8", errors="ignore")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in CONTENT_PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{line_number}: {label}")
    return findings


def path_violations(path: str) -> list[str]:
    candidate = Path(path)
    if candidate.is_symlink():
        return [f"{path}: symlink not followed"]
    name = candidate.name.lower()
    try:
        with candidate.open("rb") as stream:
            if name.endswith(IMAGE_SUFFIXES):
                return image_violations(path, name, stream)
            if candidate.stat().st_size > MAX_SCAN_BYTES:
                return []
            return text_violations(path, stream.read(MAX_SCAN_BYTES))
    except OSError:
        return [f"{path}: file not readable"]


def staged_violations(path: str, data: bytes) -> list[str]:
    name = PurePosixPath(path).name.lower()
    if name.endswith(IMAGE_SUFFIXES):
        return image_violations(path, name, io.BytesIO(data))
    if len(data) > MAX_SCAN_BYTES:
        return []
    return text_violations(path, data)


def index_content(paths: list[str]) -> tuple[dict[str, bytes], list[str]]:
    if not paths:
        return {}, []
    result = subprocess.run(
        ["git", "cat-file", "--batch"],  # noqa: S607
        input="".join(f":{path}\n" for path in paths).encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {}, []
    blobs: dict[str, bytes] = {}
    oversized: list[str] = []
    stream = io.BytesIO(result.stdout)
    for path in paths:
        header = stream.readline()
        if not header or header.rstrip().endswith(b" missing"):
            continue
        size = int(header.split()[2])
        if size > MAX_INDEX_BYTES:
            oversized.append(path)
            stream.seek(size, io.SEEK_CUR)
        else:
            blobs[path] = stream.read(size)
        stream.read(1)
    return blobs, oversized


def content_violations(paths: Iterable[str]) -> list[str]:
    listed = list(paths)
    blobs, oversized = index_content([path for path in listed if not is_violation(path)])
    findings: list[str] = []
    for path in listed:
        if path in blobs:
            findings.extend(staged_violations(path, blobs[path]))
        elif path in oversized:
            findings.append(f"{path}: staged content over {MAX_INDEX_BYTES} bytes, not inspected")
        else:
            findings.extend(path_violations(path))
    return findings


def walk(directory: Path, root: Path) -> Iterator[str]:
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            yield entry.relative_to(root).as_posix()
        elif entry.is_dir():
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
