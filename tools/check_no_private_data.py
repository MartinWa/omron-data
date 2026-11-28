"""Fail when private data is present in the working tree, by name or by content.

A line that legitimately needs to look like private data can opt out of the content
scan by carrying the marker ``hygiene: allow``.
"""

from __future__ import annotations

import argparse
import re
import string
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
READ_CHUNK_BYTES = 64 * 1024
ALLOW_MARKER = "hygiene: allow"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = frozenset({b"tEXt", b"iTXt", b"zTXt", b"eXIf"})
JPEG_EXIF_MARKER = b"Exif\x00\x00"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
SYMLINK_MODE = "120000"

LOCAL_PART_CHARACTERS = frozenset(string.ascii_letters + string.digits + "._%+-")
DOMAIN_CHARACTERS = frozenset(string.ascii_letters + string.digits + ".-")
DOMAIN_END = re.compile(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\Z")
PUBLIC_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
LOCAL_PART_LIMIT = 64
DOMAIN_LIMIT = 255


def has_email_address(line: str) -> bool:
    position = line.find("@")
    while position >= 0:
        start = position
        while (
            start > 0
            and position - start < LOCAL_PART_LIMIT
            and line[start - 1] in LOCAL_PART_CHARACTERS
        ):
            start -= 1
        end = position + 1
        while end < len(line) and end - position <= DOMAIN_LIMIT and line[end] in DOMAIN_CHARACTERS:
            end += 1
        domain = line[position + 1 : end]
        if (
            start < position
            and DOMAIN_END.match(domain)
            and not domain.startswith(PUBLIC_EXAMPLE_DOMAINS)
        ):
            return True
        position = line.find("@", position + 1)
    return False


def contains(pattern: re.Pattern, *needles: str):
    def detector(line: str) -> bool:
        return any(needle in line for needle in needles) and pattern.search(line) is not None

    return detector


CONTENT_PATTERNS = (
    ("bearer token", contains(re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}"), "earer")),
    (
        "private key block",
        contains(re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PRIVATE KEY"),
    ),
    ("AWS access key", contains(re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA")),
    (
        "GitHub token",
        contains(
            re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}"),
            "gh",
            "github_pat_",
        ),
    ),
    ("Slack token", contains(re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "xox")),
    (
        "assigned secret",
        contains(
            re.compile(
                r"(?i)\b(secret[_-]?key|api[_-]?key|token|password)\b\s*[:=]\s*[\"']"
                r"[A-Za-z0-9_\-/+]{32,}[\"']"
            ),
            "=",
            ":",
        ),
    ),
    ("email address", has_email_address),
    (
        "absolute host path",
        contains(
            re.compile(
                r"(?:[A-Za-z]:\\Users\\|/Users/[A-Za-z]|/home/(?!developer\b)[A-Za-z]|/(?:root|srv|mnt)/)"
            ),
            "/",
            "\\",
        ),
    ),
)


class ScanFailed(Exception):
    pass


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


class DeclaredReader:
    def __init__(self, stream, size: int) -> None:
        self.stream = stream
        self.remaining = size

    def read(self, count: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if count < 0 or count > self.remaining:
            count = self.remaining
        block = self.stream.read(count)
        if not block:
            raise ScanFailed(f"git ended {self.remaining} bytes early")
        self.remaining -= len(block)
        return block

    def drain(self) -> None:
        while self.read(READ_CHUNK_BYTES):
            pass


def skip(stream, count: int) -> None:
    while count > 0:
        block = stream.read(min(count, READ_CHUNK_BYTES))
        if not block:
            raise ValueError("the image ends early")
        count -= len(block)


def line_violations(path: str, number: int, raw: bytes) -> list[str]:
    if len(raw) > MAX_SCAN_BYTES:
        return [f"{path}:{number}: line longer than {MAX_SCAN_BYTES} bytes, not scanned"]
    line = raw.decode("utf-8", errors="ignore")
    if ALLOW_MARKER in line:
        return []
    return [f"{path}:{number}: {label}" for label, detector in CONTENT_PATTERNS if detector(line)]


def text_violations(path: str, stream) -> list[str]:
    findings: list[str] = []
    line_number = 0
    pending = b""
    dropping = False
    while True:
        block = stream.read(READ_CHUNK_BYTES)
        if not block:
            break
        if dropping:
            newline = block.find(b"\n")
            if newline < 0:
                continue
            block = block[newline + 1 :]
            dropping = False
        pending += block
        while True:
            end = pending.find(b"\n")
            if end < 0:
                break
            line_number += 1
            findings.extend(line_violations(path, line_number, pending[:end]))
            pending = pending[end + 1 :]
        if len(pending) > MAX_SCAN_BYTES:
            line_number += 1
            findings.append(
                f"{path}:{line_number}: line longer than {MAX_SCAN_BYTES} bytes, not scanned"
            )
            pending = b""
            dropping = True
    if pending:
        line_number += 1
        findings.extend(line_violations(path, line_number, pending))
    return findings


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
        skip(stream, length + 4)


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


def stream_violations(path: str, stream) -> list[str]:
    name = PurePosixPath(path).name.lower()
    if name.endswith(IMAGE_SUFFIXES):
        return image_violations(path, name, stream)
    return text_violations(path, stream)


def file_violations(path: str) -> list[str]:
    candidate = Path(path)
    if candidate.is_symlink():
        return [f"{path}: symbolic link not inspected"]
    try:
        with candidate.open("rb") as stream:
            return stream_violations(path, stream)
    except OSError:
        return [f"{path}: file not readable"]


def content_violations(paths: Iterable[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        findings.extend(file_violations(path))
    return findings


def present_in_worktree(candidate: Path) -> bool:
    try:
        candidate.lstat()
    except OSError:
        return False
    return True


def worktree_violations(paths: Iterable[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if not present_in_worktree(Path(path)):
            continue
        findings.extend(file_violations(path))
    return findings


def git_output(arguments: list[str]) -> bytes:
    result = subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ScanFailed("this mode needs a git work tree: git refused to list the tree")
    return result.stdout


def index_entries() -> list[tuple[str, str, str]]:
    entries = []
    for record in git_output(["ls-files", "--stage", "-z"]).split(b"\0"):
        if not record:
            continue
        header, _, path = record.partition(b"\t")
        mode, oid, _stage = header.split()
        entries.append((mode.decode(), oid.decode(), path.decode()))
    return entries


def untracked_paths() -> list[str]:
    output = git_output(["ls-files", "-z", "--others", "--exclude-standard"])
    return [entry.decode() for entry in output.split(b"\0") if entry]


def changed_paths() -> list[str]:
    paths = []
    fields = [field for field in git_output(["status", "--porcelain", "-z"]).split(b"\0")]
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2]
        path = record[3:]
        if b"R" in status or b"C" in status:
            index += 1
        if status == b"??":
            continue
        paths.append(path.decode())
    return paths


def blob_violations(blobs: list[tuple[str, str]]) -> list[str]:
    if not blobs:
        return []
    findings: list[str] = []
    process = subprocess.Popen(  # noqa: S603
        ["git", "cat-file", "--batch"],  # noqa: S607
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        for oid, path in blobs:
            process.stdin.write(f"{oid}\n".encode())
            process.stdin.flush()
            header = process.stdout.readline()
            if not header.endswith(b"\n") or len(header.split()) != 3:
                raise ScanFailed(f"git cat-file returned no blob for {path}")
            reader = DeclaredReader(process.stdout, int(header.split()[2]))
            findings.extend(stream_violations(path, reader))
            reader.drain()
            if process.stdout.read(1) != b"\n":
                raise ScanFailed(f"git cat-file ended the blob for {path} early")
        process.stdin.close()
        if process.wait() != 0:
            raise ScanFailed("git cat-file exited with an error")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return findings


def scan(untracked: bool, staged: bool) -> tuple[int, list[str], list[str]]:
    if staged:
        entries = index_entries()
        paths = [path for _mode, _oid, path in entries]
        contents = [
            f"{path}: symbolic link not inspected"
            for mode, _oid, path in entries
            if mode == SYMLINK_MODE
        ]
        contents.extend(
            blob_violations([(oid, path) for mode, oid, path in entries if mode != SYMLINK_MODE])
        )
        contents.extend(worktree_violations(changed_paths()))
        return len(paths), find_violations(paths), contents
    paths = untracked_paths() if untracked else candidate_files()
    return len(paths), find_violations(paths), content_violations(paths)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--untracked",
        action="store_true",
        help="scan the files git reports as untracked (git ls-files --others --exclude-standard)",
    )
    modes.add_argument(
        "--staged",
        action="store_true",
        help="scan the content git has staged (git ls-files --stage) instead of the working tree",
    )
    options = parser.parse_args(argv)
    try:
        scanned, violations, contents = scan(options.untracked, options.staged)
    except FileNotFoundError:
        parser.exit(1, "this mode needs git: no git executable was found on PATH.\n")
    except ScanFailed as error:
        parser.exit(1, f"The private-data scan could not finish: {error}\n")
    if violations:
        print("Private-data files found in the working tree:")
        for path in violations:
            print(f"  {path}")
    if contents:
        print("Private data found in file contents:")
        for finding in contents:
            print(f"  {finding}")
    if violations or contents:
        print(f"Scanned {scanned} files. Remove or ignore the entries above.")
        return 1
    print(f"Hygiene check passed: {scanned} files scanned, no private data found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
