"""Deterministic structural verification; semantic review belongs to the graph."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

WORKFLOWS = {
    "daily-ops", "incident", "video-production", "content-production",
    "business-support", "distribution-maintenance", "memory-maintenance", "daily-report",
    "software-development",
}
REQUIRED_CRITERIA = (
    {"id": "__scope_complete", "description": "依頼目的全体を達成し、残作業を完了と取り違えていない"},
    {"id": "__evidence_verified", "description": "実成果の証跡を独立して確認した"},
    {"id": "__authorization_respected", "description": "既存の許可対象と範囲を遵守した"},
)


def _protected(parts) -> bool:
    return any(part == ".." or part in {".git", ".ssh"} or "secret" in part
               or part.startswith(".env") or "credential" in part
               or ("token" in part and part.endswith(".json"))
               or part.endswith((".pem", ".key", ".p12"))
               for part in (value.lower() for value in parts))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path(root: Path, value: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("artifact path must be nonempty")
    relative = Path(value)
    if relative.is_absolute() or re.match(r"^[A-Za-z]:", value) or "\\" in value:
        raise ValueError("artifact path must be repository-relative with forward slashes")
    if _protected(relative.parts):
        raise ValueError("artifact path is protected")
    destination = (root / relative).resolve()
    if not destination.is_relative_to(root.resolve()):
        raise ValueError("artifact path escapes repository")
    if _protected(destination.relative_to(root.resolve()).parts):
        raise ValueError("resolved artifact path is protected")
    if destination.exists() and not destination.is_file():
        raise ValueError("artifact must be a file")
    return relative.as_posix(), destination


def _links(text: str) -> list[str]:
    return sorted(set(re.findall(r"\]\(([^)]+)\)", text)))


def validate_request(root: Path, request: dict) -> dict:
    """Normalize once at intake. Never recompute these baselines on resume."""
    root = Path(root).resolve()
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    objective = request.get("objective")
    workflow = request.get("workflow")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective is required")
    if not isinstance(workflow, str) or workflow not in WORKFLOWS:
        raise ValueError("unknown workflow")
    criteria = request.get("criteria", [])
    if workflow == "daily-report" and not criteria:
        criteria = [{"id": "report", "description": "診断結果と未解決事項を報告する"}]
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("nonempty criteria are required")
    ids = set()
    for criterion in criteria:
        if not isinstance(criterion, dict) or any(
            not isinstance(criterion.get(key), str) or not criterion[key].strip()
            for key in ("id", "description")
        ) or criterion["id"] in ids:
            raise ValueError("criteria require unique ids and descriptions")
        ids.add(criterion["id"])
    if ids.intersection(item["id"] for item in REQUIRED_CRITERIA):
        raise ValueError("reserved criterion id")
    criteria = [dict(item) for item in criteria]
    if workflow != "daily-report":
        criteria.extend(dict(item) for item in REQUIRED_CRITERIA)
    artifacts = request.get("artifacts", [])
    if not isinstance(artifacts, list) or (not artifacts and workflow != "daily-report"):
        raise ValueError("nonempty artifact expectations are required")
    normalized = []
    paths = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("artifact must be an object")
        name, path = _path(root, artifact.get("path"))
        kind = artifact.get("kind")
        if not isinstance(kind, str) or kind not in {"file", "json", "memory", "openspec"}:
            raise ValueError("unknown artifact kind")
        if name in paths:
            raise ValueError("duplicate artifact path")
        paths.add(name)
        minimum = artifact.get("min_bytes", 1)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ValueError("min_bytes must be a positive integer")
        contains = artifact.get("contains", [])
        equals = artifact.get("json_equals", {})
        if not isinstance(contains, list) or any(not isinstance(x, str) or not x.strip() for x in contains):
            raise ValueError("contains must contain nonempty strings")
        if not isinstance(equals, dict) or any(
            not isinstance(key, str) or not key.strip() or isinstance(value, (dict, list))
            or not isinstance(value, (str, int, float, bool, type(None)))
            or (isinstance(value, float) and not math.isfinite(value))
            for key, value in equals.items()
        ):
            raise ValueError("json_equals must contain top-level scalar expectations")
        if kind == "file" and (not contains or equals):
            raise ValueError("file requires contains and cannot use json_equals")
        if kind == "json" and not equals:
            raise ValueError("json requires nonempty json_equals")
        if kind == "memory" and equals:
            raise ValueError("memory cannot use json_equals")
        development = {}
        if kind == "openspec":
            from .development import contract
            development = contract(artifact)
            if equals:
                raise ValueError("openspec uses its fixed gate, not json_equals")
        reuse = artifact.get("reuse", False)
        if not isinstance(reuse, bool):
            raise ValueError("reuse must be boolean")
        links = _links(path.read_text(encoding="utf-8")) if kind == "memory" and path.exists() else []
        if kind == "memory" and not links:
            raise ValueError("memory requires an existing nonempty linked index")
        normalized.append({
            "path": name, "kind": kind, "min_bytes": minimum, "contains": contains,
            "json_equals": equals, "reuse": reuse,
            "baseline_hash": hash_file(path) if path.exists() else None,
            "baseline_links": links,
            **development,
        })
    if workflow == "memory-maintenance" and not any(item["kind"] == "memory" for item in normalized):
        raise ValueError("memory-maintenance requires at least one memory artifact")
    if workflow == "software-development" and not any(item["kind"] == "openspec" for item in normalized):
        raise ValueError("software-development requires an openspec acceptance artifact")
    result = dict(request)
    result.update(objective=objective.strip(), workflow=workflow,
                  criteria=[dict(x) for x in criteria], artifacts=normalized)
    return result


def verify_artifacts(root: Path, request: dict, submission: dict, verifier_root: Path | None = None,
                     pin: str | None = None) -> dict:
    """Check submitted immutable hashes against current local artifacts.

    Fixed verifier programs (memory lint, OpenSpec gate) run from ``verifier_root``,
    which is the job's read-only pin when it has one.
    """
    root = Path(root).resolve()
    tools = Path(verifier_root).resolve() if verifier_root else root
    checks = []

    def record(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail})

    artifacts = request.get("artifacts", [])
    submitted = submission.get("artifacts") if isinstance(submission, dict) else None
    if not artifacts or not isinstance(submitted, list):
        record("artifact-set", False, "Nonempty artifact expectations and submission required")
        return {"passed": False, "checks": checks}
    try:
        by_path = {item["path"]: item["sha256"] for item in submitted}
        valid_set = len(by_path) == len(submitted) and set(by_path) == {x["path"] for x in artifacts}
    except (KeyError, TypeError):
        valid_set = False
        by_path = {}
    record("artifact-set", valid_set, "Submitted paths must exactly match expectations")
    if not valid_set:
        return {"passed": False, "checks": checks}
    for artifact in artifacts:
        name = artifact["path"]
        try:
            _, path = _path(root, name)
            if not path.is_file():
                raise ValueError("Artifact is missing")
            digest = hash_file(path)
            size = path.stat().st_size
            record(name + ":hash", digest == by_path[name], "Current hash must match submission")
            record(name + ":size", size >= artifact["min_bytes"], "Minimum byte size")
            record(name + ":changed", artifact["reuse"] or digest != artifact["baseline_hash"],
                   "Content must change unless reuse was explicitly requested")
            text = path.read_text(encoding="utf-8-sig")
            record(name + ":nonempty", bool(text.strip()), "Artifact must contain text")
            record(name + ":contains", all(x in text for x in artifact["contains"]), "Required text")
            if artifact["kind"] == "json":
                data = json.loads(text)
                expected = artifact["json_equals"]
                matches = isinstance(data, dict) and all(
                    key in data and type(data[key]) is type(value) and data[key] == value
                    for key, value in expected.items()
                )
                record(name + ":json", matches, "Top-level JSON scalar expectations")
            if artifact["kind"] == "memory":
                record(name + ":links", bool(artifact["baseline_links"]) and
                       set(artifact["baseline_links"]).issubset(_links(text)), "Original index links retained")
                script = tools / "scripts" / "qa_memory_md_lint.py"
                if not script.is_file():
                    raise ValueError("Memory verifier is missing")
                completed = subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", str(script), "--memory-file", str(path)], cwd=root,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                    check=False,
                )
                record(name + ":memory-lint", completed.returncode == 0,
                       "Existing memory CLI exit code: " + str(completed.returncode))
            if artifact["kind"] == "openspec":
                from .development import verify
                passed, detail = verify(root, artifact, text, tools, pin)
                record(name + ":openspec", passed, detail)
            record(name + ":stable", hash_file(path) == digest, "Artifact unchanged during verification")
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
            record(name + ":verification", False, type(exc).__name__)
    return {"passed": bool(checks) and all(x["passed"] for x in checks), "checks": checks}
