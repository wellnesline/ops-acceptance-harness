"""Nonblocking local-host job locks, independent of graph checkpoint state.

Lock files are never removed or truncated. Ownership lives in the OS and is
released when the process exits, including a crash. Use a local filesystem.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import threading


_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z", re.ASCII)
_GUARD = threading.Lock()
_ACTIVE: set[str] = set()


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("job_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,79}")
    return job_id


def _acquire(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def job_lock(base_dir: Path, job_id: str):
    """Hold exclusive job ownership only for the surrounding graph operation.

    A process-local guard closes platform-specific same-process lock loopholes.
    Prefixing filenames also permits IDs such as CON on Windows without using
    reserved device filenames. No checkpoint or execution state is stored here.
    """
    validate_job_id(job_id)
    lock_dir = Path(base_dir) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"job-{job_id}.lock"
    identity = os.path.normcase(str(path.resolve()))
    with _GUARD:
        if identity in _ACTIVE:
            raise RuntimeError("job is busy in this process")
        _ACTIVE.add(identity)
    fd = None
    acquired = False
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire(fd)
        except OSError as exc:
            raise RuntimeError("job is busy or its OS lock is unavailable") from exc
        acquired = True
        yield
    finally:
        try:
            if fd is not None:
                try:
                    if acquired:
                        _release(fd)
                finally:
                    os.close(fd)
        finally:
            with _GUARD:
                _ACTIVE.discard(identity)
