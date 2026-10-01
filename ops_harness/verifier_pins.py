"""Content-addressed, read-only copies of the shared verifier for one job.

A job records the pin digest at start. Its gate runs, submission checks and
post-review re-verification all execute the pinned copy, so later edits to the
shared verifier files neither stale nor silently change in-flight jobs. The
digest is the SHA256 of the canonical manifest, and the manifest lists the
SHA256 of every pinned file, so any edit to a pin is detected.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

SCHEMA_VERSION = 1
DIGEST = re.compile(r"[0-9a-f]{64}")
PINS_DIRNAME = "verifier-pins"

# Shared verifier and workflow runtime. Job-specific checks belong in each
# profile's test_paths, never in this list.
FILES = (
    "dev_harness/gate.py",
    "dev_harness/unittest_runner.py",
    "dev_harness/workflow_delivery_policy.json",
    "dev_harness/tools/package.json",
    "dev_harness/tools/package-lock.json",
    "integrations/visual_workflow/policy.py",
    "integrations/visual_workflow/viewer.py",
)
OPTIONAL_FILES = ("config/ops_harness.json", "scripts/qa_memory_md_lint.py")
PACKAGES = ("ops_harness",)  # top-level *.py modules only
TREES = ("dev_harness/tools/node_modules",)
MAX_FILES = 5000
MAX_BYTES = 64_000_000


class PinError(ValueError):
    """The pin is missing, malformed or was modified after creation."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def members(root: Path) -> dict[str, Path]:
    """Repository-relative paths of the shared verifier set under root."""
    root = Path(root).resolve()
    found: dict[str, Path] = {}
    for relative in FILES:
        path = root / relative
        if not path.is_file():
            raise PinError("Shared verifier file is missing: " + relative)
        found[relative] = path
    for relative in OPTIONAL_FILES:
        if (root / relative).is_file():
            found[relative] = root / relative
    for package in PACKAGES:
        for path in sorted((root / package).glob("*.py")):
            found[path.relative_to(root).as_posix()] = path
    for tree in TREES:
        base = root / tree
        if not base.is_dir():
            raise PinError("Shared verifier tree is missing: " + tree)
        for current, dirs, files in os.walk(base):
            current_path = Path(current)
            for name in dirs:
                if _linked(current_path / name):
                    raise PinError("Linked directory in shared verifier tree")
            for name in files:
                path = current_path / name
                if _linked(path):
                    raise PinError("Linked file in shared verifier tree")
                found[path.relative_to(root).as_posix()] = path
    if len(found) > MAX_FILES:
        raise PinError("Shared verifier set exceeds the file bound")
    return dict(sorted(found.items()))


def _manifest_bytes(files: dict[str, str]) -> bytes:
    document = {"schema_version": SCHEMA_VERSION, "files": dict(sorted(files.items()))}
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def manifest(root: Path) -> tuple[str, dict[str, str]]:
    total = 0
    files = {}
    for relative, path in members(root).items():
        total += path.stat().st_size
        if total > MAX_BYTES:
            raise PinError("Shared verifier set exceeds the size bound")
        files[relative] = _hash(path)
    return hashlib.sha256(_manifest_bytes(files)).hexdigest(), files


def pins_dir(state_dir: Path) -> Path:
    return Path(state_dir).resolve() / PINS_DIRNAME


def pin_path(state_dir: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise PinError("Invalid verifier pin id")
    return pins_dir(state_dir) / digest


def _read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _writable_tree(path: Path) -> None:
    for current, _dirs, files in os.walk(path):
        for name in files:
            os.chmod(Path(current) / name, stat.S_IREAD | stat.S_IWRITE)


def create(root: Path, state_dir: Path, extra: dict[str, bytes] | None = None) -> str:
    """Copy the current shared verifier set into an immutable pin and return its id.

    ``extra`` adds generated files (used only for the legacy migration adapter).
    Identical sets share one pin; an existing pin is re-verified, never trusted.
    """
    root = Path(root).resolve()
    sources = members(root)
    base = pins_dir(state_dir)
    base.mkdir(parents=True, exist_ok=True)
    staging = base / (".tmp-" + uuid.uuid4().hex)
    try:
        files = {}
        for relative, source in sources.items():
            target = staging / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files[relative] = _hash(target)
        for relative, content in (extra or {}).items():
            target = staging / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            files[relative] = _hash(target)
        # The copy is what the job will execute: re-read the sources so a file
        # edited during copying cannot produce a pin that never existed.
        if any(relative not in (extra or {}) and _hash(source) != files[relative]
               for relative, source in sources.items()):
            raise PinError("Shared verifier changed while it was being pinned; retry")
        body = _manifest_bytes(files)
        digest = hashlib.sha256(body).hexdigest()
        (staging / "manifest.json").write_bytes(body)
        for current, _dirs, names in os.walk(staging):
            for name in names:
                _read_only(Path(current) / name)
        final = base / digest
        for attempt in range(20):
            if final.exists():
                verify(final)
                break
            try:
                os.rename(staging, final)
                break
            except PermissionError:
                # Windows refuses to rename a directory while a scanner still holds a fresh file.
                if attempt == 19:
                    raise
                time.sleep(0.25 * (attempt + 1))
            except OSError:
                if not final.exists():
                    raise
        return digest
    finally:
        if staging.exists():
            _writable_tree(staging)
            shutil.rmtree(staging, ignore_errors=True)


def verify(pin: Path, only: tuple[str, ...] | None = None) -> dict[str, str]:
    """Raise PinError unless the pin is byte-identical to its recorded manifest.

    ``only`` restricts the walk to these top-level directories of the pin (used
    before executing the pinned runtime code); the default checks everything.
    """
    pin = Path(pin)
    digest = pin.name
    if not DIGEST.fullmatch(digest) or not pin.is_dir() or _linked(pin):
        raise PinError("Verifier pin is missing or malformed")
    try:
        body = (pin / "manifest.json").read_bytes()
    except OSError as exc:
        raise PinError("Verifier pin manifest is missing") from exc
    if hashlib.sha256(body).hexdigest() != digest:
        raise PinError("Verifier pin manifest was modified")
    document = json.loads(body)
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, dict) or not files or document.get("schema_version") != SCHEMA_VERSION:
        raise PinError("Verifier pin manifest has an unsupported format")
    extra = {p.name for p in pin.iterdir()} - {"manifest.json", "files"}
    if extra:
        raise PinError("Unexpected content beside the verifier pin")
    base = pin / "files"
    seen = {}
    roots = [base / prefix for prefix in only] if only else [base]
    if only:
        files = {k: v for k, v in files.items() if k.split("/", 1)[0] in only}
    for current, dirs, names in (entry for top in roots for entry in os.walk(top)):
        current_path = Path(current)
        for name in dirs:
            if _linked(current_path / name):
                raise PinError("Linked directory inside the verifier pin")
        for name in names:
            path = current_path / name
            if _linked(path):
                raise PinError("Linked file inside the verifier pin")
            seen[path.relative_to(base).as_posix()] = path
    if set(seen) != set(files):
        raise PinError("Files were added to or removed from the verifier pin")
    for relative, path in seen.items():
        if _hash(path) != files[relative]:
            raise PinError("Verifier pin file was modified: " + relative)
    return files


def owning_pin(path: Path) -> Path | None:
    """Return the pin directory when ``path`` lies inside <pins>/<digest>/files/."""
    path = Path(path).resolve()
    for parent in path.parents:
        if parent.name == "files" and DIGEST.fullmatch(parent.parent.name) and parent.parent.parent.name == PINS_DIRNAME:
            return parent.parent
    return None
