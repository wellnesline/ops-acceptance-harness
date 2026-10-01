"""OpenSpec acceptance adapter; the upstream CLI and fixed tests run afresh."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def contract(artifact: dict) -> dict:
    values = {}
    for key in ("change", "profile"):
        value = artifact.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
            raise ValueError("OpenSpec artifacts require a safe change and profile")
        values[key] = value
    return values


def run_gate(root: Path, artifact: dict, verifier_root: Path | None = None) -> tuple[int, str]:
    """Run the fixed gate of this job: the pinned copy when the job has a verifier pin."""
    expected = contract(artifact)
    script = Path(verifier_root or root) / "dev_harness/gate.py"
    if not script.is_file():
        raise ValueError("OpenSpec gate is missing")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(script), "--root", str(root), "--change", expected["change"],
         "--profile", expected["profile"]], cwd=root, env=env,
        capture_output=True, encoding="utf-8", timeout=1800, check=False,
    )
    return result.returncode, result.stdout


def verify(root: Path, artifact: dict, submitted_text: str, verifier_root: Path | None = None,
           pin: str | None = None) -> tuple[bool, str]:
    """Never accept a worker's success report without rerunning its fixed gate.

    Source hashes bind the independent review to the actual implementation,
    including when LangGraph reruns this verifier after review. A pinned job
    reruns the gate from its pin, and the fresh evidence must name that pin.
    """
    expected = contract(artifact)
    report = json.loads(submitted_text)
    if not isinstance(report, dict):
        return False, "OpenSpec report must be an object"
    try:
        code, stdout = run_gate(root, expected, verifier_root)
    except ValueError as exc:
        return False, str(exc)
    if code != 0:
        return False, "OpenSpec gate failed (exit " + str(code) + ")"
    fresh = json.loads(stdout)
    if pin is not None and (not isinstance(fresh, dict) or not isinstance(fresh.get("artifact_hashes"), dict)
                            or fresh["artifact_hashes"].get("verifier-pin") != pin):
        return False, "Fresh evidence was not produced by this job's verifier pin"
    if not isinstance(fresh, dict):
        return False, "OpenSpec gate returned an invalid report"
    for value in (report, fresh):
        verification = value.get("verification")
        if (value.get("status") != "passed" or value.get("change") != expected["change"]
                or not isinstance(verification, dict)
                or verification.get("profile") != expected["profile"]
                or type(verification.get("exit_code")) is not int
                or verification["exit_code"] != 0
                or not isinstance(value.get("artifact_hashes"), dict)
                or not value["artifact_hashes"]):
            return False, "OpenSpec report does not match the fixed acceptance contract"
    if fresh["artifact_hashes"] != report["artifact_hashes"]:
        return False, "Implementation or specification changed since submitted evidence"
    for key in ("schema_version", "workspace", "upstream_version", "spec_valid", "planning_complete",
                "tasks", "verification", "inputs_unchanged"):
        if fresh.get(key) != report.get(key):
            return False, "Submitted acceptance fields differ from fresh OpenSpec evidence"
    return True, "Official OpenSpec validation and fixed tests passed; source hashes match"
