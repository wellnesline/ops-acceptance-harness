import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from ops_harness import probes
from ops_harness import __main__ as cli
from ops_harness.store import Store

# synthetic sample declaration (all names are examples)
REGISTRY = {"scheduled_tasks": [{"name": "example-health", "path": "\\example\\", "action_contains": ""}]}


def mock_http(monkeypatch, body, content_type="application/json"):
    response = MagicMock()
    response.status = 200
    response.read.return_value = body
    response.headers.get_content_type.return_value = content_type
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(probes.urllib.request, "build_opener", lambda *a: opener)
    return opener


@pytest.mark.parametrize("body,expected", [
    (b'{"status":"ok","secret":"never-copy-me"}', "pass"),
    (b'{"status":"degraded"}', "fail"), (b'{"new_schema":true}', "unknown"),
    (b'{}', "fail"), (b'<html>login</html>', "fail"), (b'[]', "fail"),
    (b'x' * 65537, "fail"),
], ids=["healthy", "degraded", "unknown-schema", "empty", "html", "array", "oversized"])
def test_health_requires_valid_schema_and_does_not_dump_body(monkeypatch, body, expected):
    mock_http(monkeypatch, body)
    output = probes.http_probe("http://app.example.invalid/health", "app")
    assert output["status"] == expected
    assert "never-copy-me" not in json.dumps(output)


def test_timeout_is_evidence_not_success(monkeypatch):
    opener = mock_http(monkeypatch, b'{}')
    opener.open.side_effect = TimeoutError("secret must not appear")
    output = probes.http_probe("http://app.example.invalid/health", "app")
    assert output["status"] == "fail"
    assert "secret" not in json.dumps(output)


def test_url_probes_are_skipped_without_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("OPS_APP_HEALTH_URL", raising=False)
    monkeypatch.delenv("OPS_FRONTEND_URL", raising=False)
    for name in ("local.app_api", "local.frontend"):
        output = probes.PROBES[name](tmp_path, {})
        assert output["status"] == "unknown"
        assert "not set" in output["summary"]


def test_redirects_cannot_expand_probe_scope():
    assert probes.NoRedirect().redirect_request(None, None, 302, "", {}, "https://external.test") is None


def test_registry_paths_cannot_escape(tmp_path):
    with pytest.raises(ValueError):
        probes.within(tmp_path, "../private.env")


def test_missing_and_stale_marker_are_not_green(tmp_path):
    assert probes.export_evidence(tmp_path, {})["status"] == "unknown"
    path = tmp_path / "scripts/example_job/logs/last_success.txt"
    path.parent.mkdir(parents=True)
    path.write_text("contents deliberately never read")
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    assert probes.export_evidence(tmp_path, {})["status"] == "warn"


def test_task_query_failure_does_not_leak_stdout(monkeypatch, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-specific scheduler probe")
    monkeypatch.setattr(probes.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "secret", "secret"))
    output = probes.windows_tasks(tmp_path, REGISTRY)
    assert output["status"] == "unknown"
    assert "secret" not in json.dumps(output)


def test_never_run_task_warns_and_nested_path_is_queried(monkeypatch, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-specific scheduler probe")
    def run(argv, **kwargs):
        assert "OPS_TASK_SPECS" in argv[-1] and "\\example\\" in kwargs["env"]["OPS_TASK_SPECS"]
        return subprocess.CompletedProcess([], 0, json.dumps([
            {"name": "example-health", "state": "Ready", "last_result": 267011, "action_match": True}
        ]), "")
    monkeypatch.setattr(probes.subprocess, "run", run)
    assert probes.windows_tasks(tmp_path, REGISTRY)["status"] == "warn"


def test_show_works_when_registry_is_broken(monkeypatch, tmp_path, capsys):
    db = tmp_path / "runs.sqlite3"
    store = Store(db)
    store.start("r", "h", ["c"])
    store.record("r", "c", {"status": "pass"})
    store.finish("r")
    monkeypatch.setattr(cli, "DB", db)
    def broken():
        raise ValueError("registry broken")
    monkeypatch.setattr(cli, "load_registry", broken)
    assert cli.main(["show", "r"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "diagnosed"


def test_incomplete_snapshot_is_not_success():
    assert cli.diagnostic_exit({"status": "running", "verdict": None}) == 2
    assert cli.diagnostic_exit({"status": "abandoned", "verdict": "unknown"}) == 2
    assert cli.diagnostic_exit({"status": "diagnosed", "verdict": "warn"}) == 1


def test_observation_time_is_recorded_by_store(tmp_path):
    store = Store(tmp_path / "db")
    store.start("r", "h", ["c"])
    before = time.time()
    store.record("r", "c", {"status": "pass", "observed_at": 0})
    assert before <= store.get("r")["results"]["c"]["observed_at"] <= time.time()


def test_route_subprocess_emits_utf8_even_with_legacy_environment():
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    proc = subprocess.run([sys.executable, "-m", "ops_harness", "route", "daily-ops"],
                          cwd=cli.ROOT, env=env, capture_output=True, timeout=10)
    assert proc.returncode == 0
    data = json.loads(proc.stdout.decode("utf-8"))
    assert data["workflow"]["label"] == "Daily health check"


def test_scheduled_task_declaration_is_validated_not_executed(tmp_path):
    bad = {"scheduled_tasks": [{"name": "x; calc", "path": "\\", "action_contains": ""}]}
    with pytest.raises(ValueError):
        probes._scheduled_task_specs(bad)


def test_registry_references_and_workflow_routes_are_real():
    registry, digest = cli.load_registry()
    assert len(digest) == 64
    assert probes.registry_paths(cli.ROOT, registry)["status"] == "pass"
    for workflow in registry["workflows"]:
        routed = cli.route(registry, workflow["id"])
        assert routed["services"]
        assert routed["business_completion"] == "not_assessed"
    with pytest.raises(ValueError):
        cli.route(registry, "arbitrary shell command")


def test_execute_records_exception_and_continues(monkeypatch, tmp_path):
    def boom(*args):
        raise RuntimeError("confidential exception")
    monkeypatch.setattr(cli, "PROBES", {"bad": boom, "good": lambda *a: probes.result("pass", "ok")})
    store = Store(tmp_path / "runs.db")
    run = store.start("example", "hash", ["bad", "good"])
    output = cli.execute(store, run, {})
    assert output["status"] == "diagnosed"
    assert output["verdict"] == "unknown"
    assert len(output["results"]) == 2
    assert "confidential" not in json.dumps(output)


def test_cli_rejects_arbitrary_probe():
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "--run-id", "x", "--check", "shell:publish"])
    assert exc.value.code == 2
