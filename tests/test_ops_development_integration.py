import json

import pytest

from ops_harness.workflow import WorkflowRuntime
from ops_harness.workflow_actions import hash_file, validate_request


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/ops_harness.json").write_text('{"services":[],"workflows":[]}', encoding="utf-8")
    (tmp_path / "dev_harness").mkdir()
    (tmp_path / "app.py").write_text("initial implementation", encoding="utf-8")
    (tmp_path / "dev_harness/gate.py").write_text(
        "import hashlib,json\nfrom pathlib import Path\n"
        "print(json.dumps({'status':'passed','change':'feature-one',"
        "'verification':{'profile':'unit','exit_code':0},"
        "'artifact_hashes':{'app.py':hashlib.sha256(Path('app.py').read_bytes()).hexdigest()}}))\n",
        encoding="utf-8")
    return tmp_path


def request():
    return {"workflow": "software-development", "objective": "Implement specified behavior",
            "checks": ["registry.paths"], "criteria": [{"id": "behavior", "description": "Specification implemented"}],
            "artifacts": [{"path": "evidence.json", "kind": "openspec", "change": "feature-one", "profile": "unit"}]}


def evidence(root):
    value = {"status": "passed", "change": "feature-one", "verification": {"profile": "unit", "exit_code": 0},
             "artifact_hashes": {"app.py": hash_file(root / "app.py")}}
    (root / "evidence.json").write_text(json.dumps(value), encoding="utf-8")


def test_development_cannot_use_generic_success_json(workspace):
    value = request()
    value["artifacts"] = [{"path": "evidence.json", "kind": "json", "json_equals": {"status": "passed"}}]
    with pytest.raises(ValueError, match="openspec"):
        validate_request(workspace, value)


def test_source_change_during_review_returns_to_work(workspace):
    runtime = WorkflowRuntime(workspace)
    state = runtime.start("development-test", request())
    evidence(workspace)
    state = runtime.submit("development-test", {"token": state["token"], "worker_id": "builder",
        "artifacts": [{"path": "evidence.json", "sha256": hash_file(workspace / "evidence.json")}]})
    assert state["status"] == "awaiting_review"
    (workspace / "app.py").write_text("unreviewed implementation", encoding="utf-8")
    state = runtime.review("development-test", {"token": state["token"], "reviewer_id": "reviewer",
        "decision": "approve", "criteria": [{"id": c["id"], "passed": True, "note": "Verified prior implementation"}
            for c in state["request"]["criteria"]]})
    assert state["status"] == "awaiting_work"
    assert not state["verification"]["passed"]
    assert state["attempt"] == 2
