from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import subprocess
import sys

import pytest

from ops_harness.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ledger.sqlite3")


def test_evidence_required_and_business_completion_not_assessed(store):
    store.start("a", "hash", ["first", "second"])
    with pytest.raises(RuntimeError):
        store.finish("a")
    store.record("a", "first", {"status": "pass", "details": {"count": 2}})
    with pytest.raises(RuntimeError):
        store.finish("a")
    store.record("a", "second", {"status": "warn"})
    result = store.finish("a")
    assert result["status"] == "diagnosed"
    assert result["verdict"] == "warn"
    assert result["business_completion"] == "not_assessed"
    store.start("b", "hash", ["first"])


@pytest.mark.parametrize("statuses,expected", [
    (["pass", "pass"], "pass"), (["warn", "unknown"], "unknown"),
    (["unknown", "fail"], "fail"),
])
def test_worst_verdict(store, statuses, expected):
    store.start("a", "h", ["x", "y"])
    for check, status in zip(["x", "y"], statuses):
        store.record("a", check, {"status": status})
    assert store.finish("a")["verdict"] == expected


def test_invalid_and_duplicate_evidence(store):
    with pytest.raises(ValueError):
        store.start("empty", "h", [])
    with pytest.raises(ValueError):
        store.start("dup", "h", ["x", "x"])
    store.start("a", "h", ["x"])
    for result in [{"status": "success"}, {"status": "pass", "value": float("nan")},
                   {"status": "pass", "value": object()}]:
        with pytest.raises(ValueError):
            store.record("a", "x", result)
    with pytest.raises(ValueError):
        store.record("a", "outside", {"status": "pass"})
    store.record("a", "x", {"status": "pass"})
    with pytest.raises(RuntimeError):
        store.record("a", "x", {"status": "fail"})


def test_collision_rolls_back_all_claims(store):
    store.start("a", "h", ["x"])
    with pytest.raises(RuntimeError):
        store.start("b", "h", ["y", "x"])
    store.start("c", "h", ["y"])
    with pytest.raises(RuntimeError):
        store.start("a", "h", ["z"])


def test_simultaneous_start_has_exactly_one_winner(store):
    def begin(name):
        try:
            store.start(name, "h", ["same-resource"])
            return True
        except RuntimeError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(begin, ["a", "b", "c", "d"])) == 1


def test_live_owner_and_config_change_cannot_resume(store):
    store.start("a", "h", ["x"])
    with pytest.raises(RuntimeError, match="alive"):
        store.resume("a", "h")
    with pytest.raises(ValueError, match="configuration"):
        store.resume("a", "other")


def test_process_exit_resume_retains_completed_evidence(store):
    source = (
        "from ops_harness.store import Store; import sys; "
        "s=Store(sys.argv[1]); s.start('child','h',['x','y']); "
        "s.record('child','x',{'status':'pass'})"
    )
    subprocess.run([sys.executable, "-c", source, str(store.path)], check=True)
    run = store.resume("child", "h")
    assert run["owner_pid"] == os.getpid()
    assert run["pending_checks"] == ["y"]
    assert run["results"]["x"]["status"] == "pass"
    store.record("child", "y", {"status": "unknown"})
    assert store.finish("child")["verdict"] == "unknown"
    with pytest.raises(RuntimeError):
        store.resume("child", "h")


def test_foreign_owner_cannot_record_or_finish(store):
    store.start("a", "h", ["x"])
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE runs SET owner_pid=? WHERE run_id='a'", (os.getpid() + 1000000,))
    with pytest.raises(RuntimeError):
        store.record("a", "x", {"status": "pass"})
    with pytest.raises(RuntimeError):
        store.finish("a")


def test_abandon_dead_owner_after_config_change_preserves_evidence(store):
    source = (
        "from ops_harness.store import Store; import sys; "
        "s=Store(sys.argv[1]); s.start('old','old-config',['x','y']); "
        "s.record('old','x',{'status':'pass'})"
    )
    subprocess.run([sys.executable, "-c", source, str(store.path)], check=True)
    with pytest.raises(ValueError):
        store.resume("old", "new-config")
    result = store.abandon("old")
    assert result["status"] == "abandoned"
    assert result["verdict"] == "unknown"
    assert result["results"]["x"]["status"] == "pass"
    assert result["business_completion"] == "not_assessed"
    store.start("new", "new-config", ["x", "y"])
    with pytest.raises(RuntimeError):
        store.resume("old", "old-config")
    with pytest.raises(RuntimeError):
        store.abandon("old")


def test_abandon_rejects_live_and_diagnosed_runs(store):
    store.start("a", "h", ["x"])
    with pytest.raises(RuntimeError, match="alive"):
        store.abandon("a")
    store.record("a", "x", {"status": "pass"})
    store.finish("a")
    with pytest.raises(RuntimeError):
        store.abandon("a")
