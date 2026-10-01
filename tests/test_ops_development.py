import json
from pathlib import Path

import pytest

from ops_harness.development import contract, verify


def report():
    return {"status": "passed", "change": "feature-one",
            "verification": {"profile": "unit", "exit_code": 0},
            "artifact_hashes": {"app.py": "a" * 64}}


def gate(root, value, exit_code=0):
    script = root / "dev_harness/gate.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("import sys\nprint(" + repr(json.dumps(value)) + ")\nsys.exit(" + str(exit_code) + ")\n", encoding="utf-8")


ARTIFACT = {"change": "feature-one", "profile": "unit"}


def test_fresh_gate_required_and_source_bound(tmp_path):
    value = report()
    gate(tmp_path, value)
    assert verify(tmp_path, ARTIFACT, json.dumps(value))[0]
    changed = report()
    changed["artifact_hashes"]["app.py"] = "b" * 64
    gate(tmp_path, changed)
    assert not verify(tmp_path, ARTIFACT, json.dumps(value))[0]


def test_worker_success_cannot_mask_failed_gate(tmp_path):
    gate(tmp_path, report(), 2)
    assert not verify(tmp_path, ARTIFACT, json.dumps(report()))[0]


@pytest.mark.parametrize("key,value", [("change", "../escape"), ("profile", "--sample"), ("profile", None)])
def test_unsafe_contract_rejected(key, value):
    with pytest.raises(ValueError):
        contract({**ARTIFACT, key: value})


@pytest.mark.parametrize("mutation", ["profile", "hashes", "status", "acceptance"])
def test_inconsistent_self_report_rejected(tmp_path, mutation):
    gate(tmp_path, report())
    forged = report()
    if mutation == "profile":
        forged["verification"]["profile"] = "other"
    elif mutation == "hashes":
        forged["artifact_hashes"] = {}
    elif mutation == "status":
        forged["status"] = "incomplete"
    else:
        forged["spec_valid"] = False
    assert not verify(tmp_path, ARTIFACT, json.dumps(forged))[0]
