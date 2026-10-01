"""Real LangGraph/SqliteSaver contracts; no model, network or production actions."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from ops_harness.workflow import WorkflowRuntime


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/ops_harness.json").write_text(
        json.dumps({"schema_version": 1, "services": [], "workflows": []}), encoding="utf-8"
    )
    return tmp_path


def request(max_attempts=2):
    return {
        "workflow": "content-production", "objective": "Write hello for the reader",
        "criteria": [{"id": "c1", "description": "The output addresses the requested reader"}],
        "artifacts": [{"path": "out/result.txt", "kind": "file", "min_bytes": 1, "contains": ["hello"]}],
        "authorization_scope": "local only", "max_attempts": max_attempts,
        "checks": ["registry.paths"],
    }


def write_artifact(root, content="hello reader"):
    path = root / "out/result.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": "out/result.txt", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def submission(state, artifact):
    return {"token": state["token"], "worker_id": "worker", "artifacts": [artifact]}


def review(state, *, reviewer="reviewer", decision="approve", passed=True):
    return {"token": state["token"], "reviewer_id": reviewer, "decision": decision,
            "criteria": [{"id": criterion["id"], "passed": passed,
                          "note": "Independently checked this fixed criterion against the result"}
                         for criterion in state["request"]["criteria"]]}


def test_real_graph_waits_for_work_and_independent_review(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("job", request())
    assert state["status"] == "awaiting_work"
    assert state["attempt"] == 1
    assert state["interrupts"]
    state = runtime.submit("job", submission(state, write_artifact(workspace)))
    assert state["status"] == "awaiting_review", "file checks alone cannot certify prose quality"
    required = {"__scope_complete", "__evidence_verified", "__authorization_respected"}
    assert required.issubset({c["id"] for c in state["request"]["criteria"]})
    incomplete = review(state)
    incomplete["criteria"] = [c for c in incomplete["criteria"] if c["id"] not in required]
    with pytest.raises((ValueError, RuntimeError)):
        runtime.review("job", incomplete)
    with pytest.raises((ValueError, RuntimeError)):
        runtime.review("job", review(state, reviewer="worker"))
    state = runtime.review("job", review(state))
    assert state["status"] == "completed"
    assert WorkflowRuntime(workspace).inspect("job")["status"] == "completed"
    with pytest.raises((ValueError, RuntimeError)):
        runtime.start("job", request())


def test_worker_claim_does_not_override_independent_artifact_check(workspace):
    runtime = WorkflowRuntime(workspace)
    initial = runtime.start("job", request())
    payload = submission(initial, write_artifact(workspace, "unrelated output"))
    state = runtime.submit("job", payload)
    assert state["status"] == "awaiting_work"
    assert state["attempt"] == 2
    assert state["token"] != initial["token"]
    with pytest.raises((ValueError, RuntimeError)):
        runtime.submit("job", payload)
    state = runtime.submit("job", submission(state, write_artifact(workspace, "still incorrect")))
    assert state["status"] == "failed"
    assert state["attempt"] == 2


def test_review_rejection_consumes_bounded_retry(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("job", request(max_attempts=1))
    state = runtime.submit("job", submission(state, write_artifact(workspace)))
    state = runtime.review("job", review(state, decision="reject", passed=False))
    assert state["status"] == "failed"
    assert state["attempt"] == 1


def test_artifact_changed_while_waiting_for_review_cannot_complete(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("job", request(max_attempts=1))
    state = runtime.submit("job", submission(state, write_artifact(workspace)))
    assert state["status"] == "awaiting_review"
    write_artifact(workspace, "content changed after initial validation")
    try:
        state = runtime.review("job", review(state))
    except (ValueError, RuntimeError):
        state = runtime.inspect("job")
    assert state["status"] != "completed"


def test_request_cannot_select_outside_workspace_or_arbitrary_command(workspace):
    runtime = WorkflowRuntime(workspace)
    bad = request()
    bad["artifacts"][0]["path"] = "../outside.txt"
    with pytest.raises((ValueError, RuntimeError)):
        runtime.start("outside", bad)
    bad = request()
    bad["checks"] = ["arbitrary-shell"]
    with pytest.raises((ValueError, RuntimeError)):
        runtime.start("command", bad)


def test_digest_mismatch_never_completes(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("job", request(max_attempts=1))
    artifact = write_artifact(workspace)
    artifact["sha256"] = "0" * 64
    try:
        result = runtime.submit("job", submission(state, artifact))
    except (ValueError, RuntimeError):
        result = runtime.inspect("job")
    assert result["status"] != "completed"
    assert result["status"] != "awaiting_review"


def test_cancel_does_not_report_completed(workspace):
    runtime = WorkflowRuntime(workspace)
    runtime.start("job", request())
    assert runtime.cancel("job")["status"] == "cancelled"
    assert WorkflowRuntime(workspace).inspect("job")["status"] == "cancelled"
    with pytest.raises((ValueError, RuntimeError)):
        runtime.continue_job("job")


def test_changed_registry_blocks_resume_but_allows_inspect_and_cancel(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("job", request())
    registry = workspace / "config/ops_harness.json"
    registry.write_text(registry.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = WorkflowRuntime(workspace)
    with pytest.raises((ValueError, RuntimeError)):
        changed.submit("job", submission(state, write_artifact(workspace)))
    assert changed.inspect("job")["job_id"] == "job"
    assert changed.cancel("job")["status"] == "cancelled"


def test_daily_report_is_executed_and_verified_by_graph(workspace):
    state = WorkflowRuntime(workspace).start("daily", {
        "workflow": "daily-report", "objective": "Create today's diagnostic report",
        "checks": ["registry.paths"],
    })
    assert state["status"] == "completed"
    assert state.get("artifact"), "automatic graph action must produce an artifact"
    assert state.get("verification"), "completion needs independent verification evidence"


def test_workflow_resumes_across_three_real_processes(workspace):
    driver = """
import json, sys
from pathlib import Path
from ops_harness.workflow import WorkflowRuntime
root, operation, payload = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
r = WorkflowRuntime(root)
if operation == 'start': result = r.start('process-job', payload)
elif operation == 'submit': result = r.submit('process-job', payload)
else: result = r.review('process-job', payload)
print(json.dumps({k: result.get(k) for k in ['job_id','status','token','attempt','request']}))
"""
    def invoke(operation, payload):
        proc = subprocess.run(
            [sys.executable, "-c", driver, str(workspace), operation, json.dumps(payload)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    state = invoke("start", request())
    assert state["status"] == "awaiting_work"
    state = invoke("submit", submission(state, write_artifact(workspace)))
    assert state["status"] == "awaiting_review"
    state = invoke("review", review(state))
    assert state["status"] == "completed"
    # A genuine SQLite LangGraph saver persists the framework's checkpoint table.
    databases = list(workspace.rglob("*.sqlite*")) + list(workspace.rglob("*.db"))
    found = False
    for database in databases:
        with sqlite3.connect(database) as connection:
            names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            found = found or "checkpoints" in names
    assert found, "expected LangGraph SqliteSaver checkpoint schema"


def test_concurrent_duplicate_submission_is_consumed_once(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("race", request())
    payload = submission(state, write_artifact(workspace))
    driver = """
import json, sys
from pathlib import Path
from ops_harness.workflow import WorkflowRuntime
try:
    state = WorkflowRuntime(Path(sys.argv[1])).submit('race', json.loads(sys.argv[2]))
    print(json.dumps({'accepted': True, 'status': state['status']}))
except (ValueError, RuntimeError):
    print(json.dumps({'accepted': False}))
"""
    children = [subprocess.Popen(
        [sys.executable, "-c", driver, str(workspace), json.dumps(payload)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    ) for _ in range(2)]
    try:
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
        assert sum(o["accepted"] for o in outcomes) == 1
        assert runtime.inspect("race")["status"] == "awaiting_review"
        assert runtime.inspect("race")["attempt"] == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
