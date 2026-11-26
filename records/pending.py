import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from hashlib import sha256
from pathlib import Path

PENDING_TTL_SECONDS = 3600
STAGING_DIR_NAME = "omron-data-pending"
MARKER_NAME = ".omron-data-pending"
MARKER_TEXT = "omron-data staging directory, created by the app; safe to delete when it is idle\n"
IMPORT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
SUMMARY_NAME = "summary.json"
FALLBACK_NAME = "export"


def pending_base() -> Path:
    configured = os.environ.get("OMRON_PENDING_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir())


def pending_root() -> Path:
    return pending_base() / STAGING_DIR_NAME


def new_import_id() -> str:
    return secrets.token_urlsafe(16)


def fingerprint(content: bytes) -> str:
    return sha256(content).hexdigest()


def safe_name(name: str) -> str:
    base = Path(name or "").name
    if base.lower().endswith(".csv"):
        base = base[:-4]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("._-")
    return f"{cleaned[:100] or FALLBACK_NAME}.csv"


def _owned(folder: Path) -> bool:
    return (folder / MARKER_NAME).is_file()


def _folder(import_id: str) -> Path | None:
    if not IMPORT_ID_PATTERN.fullmatch(import_id):
        return None
    return pending_root() / import_id


def _csv_path(import_id: str, name: str) -> Path | None:
    folder = _folder(import_id)
    if folder is None or not NAME_PATTERN.fullmatch(name):
        return None
    return folder / name


def _ensure_root() -> Path:
    root = pending_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


def save(import_id: str, name: str, content: bytes) -> Path:
    path = _csv_path(import_id, safe_name(name))
    if path is None:
        raise ValueError("invalid staging target")
    _ensure_root()
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    marker = folder / MARKER_NAME
    marker.write_text(MARKER_TEXT, encoding="utf-8")
    with contextlib.suppress(OSError):
        marker.chmod(0o600)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def staged_path(import_id: str) -> Path | None:
    folder = _folder(import_id)
    if folder is None:
        return None
    try:
        found = sorted(
            child for child in folder.iterdir() if child.is_file() and child.suffix == ".csv"
        )
    except OSError:
        return None
    return found[0] if found else None


def read_staged(import_id: str) -> bytes | None:
    path = staged_path(import_id)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def save_summary(import_id: str, summary: dict) -> None:
    folder = _folder(import_id)
    if folder is None:
        raise ValueError("invalid staging target")
    path = folder / SUMMARY_NAME
    path.write_text(json.dumps(summary), encoding="utf-8")
    path.chmod(0o600)


def load_summary(import_id: str) -> dict | None:
    folder = _folder(import_id)
    if folder is None:
        return None
    try:
        summary = json.loads((folder / SUMMARY_NAME).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return summary if isinstance(summary, dict) else None


def discard(import_id: str) -> None:
    folder = _folder(import_id)
    if folder is None or not _owned(folder):
        return
    shutil.rmtree(folder, ignore_errors=True)


def purge_stale(ttl_seconds: int = PENDING_TTL_SECONDS) -> int:
    try:
        entries = list(pending_root().iterdir())
    except OSError:
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for entry in entries:
        if entry.is_symlink() or not IMPORT_ID_PATTERN.fullmatch(entry.name):
            continue
        try:
            if not entry.is_dir() or not _owned(entry):
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed
