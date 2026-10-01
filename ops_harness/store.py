"""Transactional diagnostic evidence ledger. This is not a business job runner."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import time


STATUSES = {"pass", "warn", "fail", "unknown"}


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Invalid PID means dead; access denied or other errors are uncertain.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 characters")


def _result_json(result: dict) -> str:
    if not isinstance(result, dict) or result.get("status") not in STATUSES:
        raise ValueError("result requires status pass, warn, fail, or unknown")
    def validate(value):
        if value is None or type(value) in (str, bool, int, float):
            return
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict and all(type(k) is str for k in value):
            for item in value.values():
                validate(item)
            return
        raise ValueError("result must contain only JSON values")
    validate(result)
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("result must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("result exceeds 64 KiB; persist structured summaries only")
    return encoded


class Store:
    """One active owner per diagnostic check, including across processes.

    Callers supply sanitized structured results; stdout, credentials and raw logs
    must never be passed here. SQLite cannot roll back external operations.
    Recovery is therefore restricted to the registered repeatable diagnostics.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, config_hash TEXT NOT NULL,
                    status TEXT NOT NULL, owner_pid INTEGER NOT NULL,
                    check_ids TEXT NOT NULL, verdict TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    check_id TEXT PRIMARY KEY, run_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS results (
                    run_id TEXT NOT NULL, check_id TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(run_id, check_id)
                );
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _get(db, run_id):
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("unknown run_id")
        result = dict(row)
        result["check_ids"] = json.loads(result["check_ids"])
        result["results"] = {
            r["check_id"]: json.loads(r["result"])
            for r in db.execute("SELECT check_id,result FROM results WHERE run_id=?", (run_id,))
        }
        result["pending_checks"] = [c for c in result["check_ids"] if c not in result["results"]]
        result["business_completion"] = "not_assessed"
        result["evidence_note"] = "Use each result's observed_at (Unix seconds). Resume preserves prior evidence; use a new run for current health. Older records may lack observation timestamps."
        return result

    @staticmethod
    def _owned(run):
        if run["status"] != "running" or run["owner_pid"] != os.getpid():
            raise RuntimeError("run is not running under this process")

    def start(self, run_id: str, config_hash: str, check_ids: list[str]) -> dict:
        _text(run_id, "run_id")
        _text(config_hash, "config_hash")
        if not isinstance(check_ids, list) or not check_ids:
            raise ValueError("check_ids must be a nonempty list")
        for check in check_ids:
            _text(check, "check_id")
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("duplicate check_id")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                raise RuntimeError("run_id already exists; inspect or resume it")
            for check in check_ids:
                if db.execute("SELECT 1 FROM claims WHERE check_id=?", (check,)).fetchone():
                    raise RuntimeError(f"diagnostic already claimed: {check}")
            now = time.time()
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                       (run_id, config_hash, "running", os.getpid(), json.dumps(check_ids), None, now, now))
            db.executemany("INSERT INTO claims VALUES (?,?)", [(c, run_id) for c in check_ids])
            return self._get(db, run_id)

    def record(self, run_id: str, check_id: str, result: dict):
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        result = {**result, "observed_at": time.time()}
        encoded = _result_json(result)
        with self._transaction() as db:
            run = self._get(db, run_id)
            self._owned(run)
            if check_id not in run["check_ids"]:
                raise ValueError("check_id is not part of this run")
            if check_id in run["results"]:
                raise RuntimeError("evidence already recorded; it cannot be overwritten")
            db.execute("INSERT INTO results VALUES (?,?,?)", (run_id, check_id, encoded))
            db.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (time.time(), run_id))

    def finish(self, run_id: str) -> dict:
        with self._transaction() as db:
            run = self._get(db, run_id)
            self._owned(run)
            if not run["check_ids"] or run["pending_checks"]:
                raise RuntimeError("all requested diagnostics require evidence before finish")
            ranking = {"pass": 0, "warn": 1, "unknown": 2, "fail": 3}
            verdict = max((r["status"] for r in run["results"].values()), key=ranking.get)
            db.execute("UPDATE runs SET status='diagnosed',verdict=?,updated_at=? WHERE run_id=?",
                       (verdict, time.time(), run_id))
            db.execute("DELETE FROM claims WHERE run_id=?", (run_id,))
            return self._get(db, run_id)

    def get(self, run_id: str) -> dict:
        with self._connection() as db:
            # Hold a read snapshot across the run and evidence queries.
            db.execute("BEGIN")
            return self._get(db, run_id)

    def resume(self, run_id: str, config_hash: str) -> dict:
        with self._transaction() as db:
            run = self._get(db, run_id)
            if config_hash != run["config_hash"]:
                raise ValueError("configuration changed; cannot resume")
            if run["status"] != "running":
                raise RuntimeError("diagnosed run cannot resume")
            if _pid_alive(run["owner_pid"]):
                raise RuntimeError("run owner is alive; concurrent resume refused")
            db.execute("UPDATE runs SET owner_pid=?,updated_at=? WHERE run_id=?",
                       (os.getpid(), time.time(), run_id))
            return self._get(db, run_id)

    def abandon(self, run_id: str) -> dict:
        """Release an interrupted diagnostic without declaring it successful.

        No config match is required: this is the recovery path after registry
        changes. Evidence remains immutable and the original run cannot resume.
        """
        with self._transaction() as db:
            run = self._get(db, run_id)
            if run["status"] != "running":
                raise RuntimeError("only an interrupted running run can be abandoned")
            if _pid_alive(run["owner_pid"]):
                raise RuntimeError("run owner is alive; abandon refused")
            db.execute("UPDATE runs SET status='abandoned',verdict='unknown',updated_at=? WHERE run_id=?",
                       (time.time(), run_id))
            db.execute("DELETE FROM claims WHERE run_id=?", (run_id,))
            return self._get(db, run_id)
