"""Business workflow execution using actual LangGraph and its SQLite saver.

The conversation orchestrator is an external worker, not a second state machine.
Only deterministic local report creation is automatically executed here.
A job started through the job CLI runs this module from its own verifier pin
(.harness/verifier-pins/<digest>/files), never from the repository copy.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import TypedDict
import uuid

from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
try:  # optional: only used to keep tracing off while a job runs
    from langsmith import tracing_context
except ImportError:  # pragma: no cover
    @contextmanager
    def tracing_context(**_kwargs):
        yield

from . import verifier_pins
from .probes import PROBES, result
from .workflow_actions import validate_request, verify_artifacts, hash_file
from .workflow_lock import job_lock, validate_job_id


class JobState(TypedDict, total=False):
    job_id: str
    request: dict
    fingerprint: str
    verifier_pin: str
    status: str
    diagnostics: dict
    attempt: int
    token: str
    submission: dict
    verification: dict
    review: dict
    history: list
    artifact: dict
    completion_scope: str


def _event(state, name, **extra):
    return state.get("history", []) + [{"event": name, "at": time.time(), **extra}]


def _atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class WorkflowRuntime:
    def __init__(self, root: Path, state_dir: Path | None = None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / ".harness").resolve()
        if not self.state_dir.is_relative_to(self.root):
            raise ValueError("state_dir must be inside the workspace")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # The pin is derived from where this code runs, never from a caller's claim: the job CLI
        # executes every operation of a pinned job from <state_dir>/verifier-pins/<digest>/files.
        pin_dir = verifier_pins.owning_pin(Path(__file__))
        if pin_dir is not None and pin_dir.parent != verifier_pins.pins_dir(self.state_dir):
            raise ValueError("Pinned runtime belongs to a different state directory")
        self.pin_dir = pin_dir
        self.pin = pin_dir.name if pin_dir is not None else None
        self.verifier_root = pin_dir / "files" if pin_dir is not None else self.root

    def fingerprint(self):
        if self.pin is not None:
            # Every pinned file is bound by the pin id itself; repository edits to the shared
            # verifier or workflow code therefore do not stale this job.
            digest = hashlib.sha256(("verifier-pin:" + self.pin).encode())
            for name in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite"):
                digest.update((name + version(name)).encode())
            return digest.hexdigest()
        digest = hashlib.sha256()
        for name in ("workflow.py", "workflow_actions.py", "workflow_lock.py", "probes.py", "development.py"):
            digest.update((Path(__file__).parent / name).read_bytes())
        for name in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite"):
            digest.update((name + version(name)).encode())
        registry = self.root / "config/ops_harness.json"
        digest.update(registry.read_bytes() if registry.exists() else b"no-registry")
        validator = self.root / "scripts/qa_memory_md_lint.py"
        digest.update(validator.read_bytes() if validator.exists() else b"no-memory-validator")
        # dev_harness/profiles.json is deliberately excluded: it is shared by all sessions, and each
        # development job's own profile entry is bound by the gate evidence that is re-run on submit/review.
        for relative in ("dev_harness/gate.py", "dev_harness/unittest_runner.py",
                         "dev_harness/tools/package-lock.json", "openspec/config.yaml"):
            path = self.root / relative
            digest.update(relative.encode())
            digest.update(path.read_bytes() if path.exists() else b"absent")
        return digest.hexdigest()

    def _intake(self, state):
        return {"status": "diagnosing", "history": _event(state, "intake")}

    def _diagnose(self, state):
        path = self.verifier_root / "config/ops_harness.json"
        registry = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"services": [], "workflows": []}
        observations = {}
        for check_id in state["request"]["checks"]:
            try:
                observation = PROBES[check_id](self.root, registry)
            except Exception as exc:
                observation = result("unknown", "Probe unavailable", error_type=type(exc).__name__)
            observations[check_id] = {**observation, "observed_at": time.time()}
        return {"diagnostics": observations, "status": "diagnosed", "history": _event(state, "diagnose")}

    def _dispatch(self, state):
        attempt = state.get("attempt", 0) + 1
        return {"attempt": attempt, "token": uuid.uuid4().hex, "status": "dispatched",
                "history": _event(state, "dispatch", attempt=attempt)}

    def _work(self, state):
        # Nothing side-effecting belongs before interrupt: LangGraph restarts this node.
        payload = interrupt({"kind": "work", "job_id": state["job_id"], "token": state["token"],
                             "attempt": state["attempt"], "request": state["request"],
                             "prior_verification": state.get("verification"),
                             "prior_review": state.get("review"),
                             "instruction": "The authorized conversation worker performs the task, then submits artifact hashes. This is a worker handoff, not a request to ask the user again."})
        return {"submission": payload, "status": "verifying", "history": _event(state, "submitted")}

    def _verified_evidence(self, state):
        if self.pin_dir is not None:
            try:
                verifier_pins.verify(self.pin_dir)
            except verifier_pins.PinError as exc:
                # A modified pin is recorded as a failed check, never silently re-pinned.
                return {"passed": False, "checks": [{"id": "verifier-pin", "passed": False, "detail": str(exc)}]}
        evidence = verify_artifacts(self.root, state["request"], state["submission"], self.verifier_root, self.pin)
        if self.pin_dir is not None:
            evidence["checks"].insert(0, {"id": "verifier-pin", "passed": True,
                                          "detail": "Pinned verifier " + self.pin + " matches its manifest"})
        return evidence

    def _verify(self, state):
        evidence = self._verified_evidence(state)
        return {"verification": evidence, "status": "verified" if evidence["passed"] else "verification_failed",
                "history": _event(state, "verify", passed=evidence["passed"])}

    def _review(self, state):
        payload = interrupt({"kind": "review", "job_id": state["job_id"], "token": state["token"],
                             "criteria": state["request"]["criteria"], "submission": state["submission"],
                             "verification": state["verification"],
                             "instruction": "An independent reviewer checks every acceptance criterion and cites evidence. Do not ask the user again unless the task actually requires their decision."})
        return {"review": payload, "status": "reviewed", "history": _event(state, "reviewed", decision=payload["decision"])}

    def _final_verify(self, state):
        # A file can change while a reviewer is working. Recheck before accepting.
        evidence = self._verified_evidence(state)
        return {"verification": evidence, "history": _event(state, "final_verify", passed=evidence["passed"])}

    def _complete(self, state):
        return {"status": "completed", "completion_scope": "Submitted artifacts satisfy the fixed structural checks and all independently reviewed acceptance criteria; external effects are not inferred.",
                "history": _event(state, "completed")}

    def _failed(self, state):
        return {"status": "failed", "history": _event(state, "attempt_limit_reached")}

    def _cancel(self, state):
        return {"status": "cancelled", "history": _event(state, "cancelled")}

    def _report_document(self, state):
        return {"schema_version": 1, "job_id": state["job_id"], "objective": state["request"]["objective"],
                "completion_scope": "diagnostic_report_only", "observations": state["diagnostics"],
                "unresolved": [k for k, v in state["diagnostics"].items() if v["status"] != "pass"],
                "business_health": "not_assessed"}

    def _report(self, state):
        destination = self.state_dir / "jobs" / state["job_id"] / "report.json"
        # Same job always writes the same deterministic content after diagnosis checkpoint.
        _atomic_json(destination, self._report_document(state))
        return {"artifact": {"path": destination.relative_to(self.root).as_posix(), "sha256": hash_file(destination)},
                "status": "verifying", "history": _event(state, "report_written")}

    def _verify_report(self, state):
        destination = self.root / state["artifact"]["path"]
        valid = (hash_file(destination) == state["artifact"]["sha256"] and
                 json.loads(destination.read_text(encoding="utf-8")) == self._report_document(state))
        return {"verification": {"passed": valid, "checks": [{"id": "report-content", "passed": valid}]},
                "status": "completed" if valid else "failed", "completion_scope": "diagnostic_report_only",
                "history": _event(state, "report_verified", passed=valid)}

    @staticmethod
    def _retry_route(state):
        return "dispatch" if state["attempt"] < state["request"]["max_attempts"] else "failed"

    def build_graph(self, saver):
        graph = StateGraph(JobState)
        for name, action in (("intake", self._intake), ("diagnose", self._diagnose),
                             ("dispatch", self._dispatch), ("work", self._work), ("verify", self._verify),
                             ("review", self._review), ("final_verify", self._final_verify),
                             ("complete", self._complete), ("failed", self._failed), ("cancel", self._cancel),
                             ("report", self._report), ("verify_report", self._verify_report)):
            graph.add_node(name, action)
        graph.add_edge(START, "intake")
        graph.add_edge("intake", "diagnose")
        graph.add_conditional_edges("diagnose", lambda s: "report" if s["request"]["workflow"] == "daily-report" else "dispatch")
        graph.add_edge("dispatch", "work")
        graph.add_edge("work", "verify")
        graph.add_conditional_edges("verify", lambda s: "review" if s["verification"]["passed"] else self._retry_route(s))
        graph.add_conditional_edges("review", lambda s: "final_verify" if s["review"]["decision"] == "approve" else self._retry_route(s))
        graph.add_conditional_edges("final_verify", lambda s: "complete" if s["verification"]["passed"] else self._retry_route(s))
        graph.add_edge("report", "verify_report")
        for name in ("complete", "failed", "cancel", "verify_report"):
            graph.add_edge(name, END)
        return graph.compile(checkpointer=saver)

    @contextmanager
    def _session(self, job_id):
        validate_job_id(job_id)
        with job_lock(self.state_dir, job_id), tracing_context(enabled=False):
            connection = sqlite3.connect(self.state_dir / "langgraph.sqlite3", check_same_thread=False, timeout=30)
            try:
                saver = SqliteSaver(connection, serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[], allowed_json_modules=[]))
                graph = self.build_graph(saver)
                yield graph, {"configurable": {"thread_id": job_id}, "recursion_limit": 100}
            finally:
                connection.close()

    def _snapshot(self, graph, config):
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise ValueError("Unknown job_id")
        values = dict(snapshot.values)
        interruptions = [i.value for task in snapshot.tasks for i in task.interrupts]
        values["interrupts"] = interruptions
        values["next"] = list(snapshot.next)
        if interruptions:
            values["status"] = "awaiting_" + interruptions[0]["kind"]
        elif snapshot.next:
            values["status"] = "interrupted_execution"
        values["framework"] = {"name": "langgraph", "version": version("langgraph"), "checkpointer": "SqliteSaver"}
        return values

    def _check_current(self, state):
        if state.get("verifier_pin") != self.pin:
            raise RuntimeError("This job is bound to verifier pin " + str(state.get("verifier_pin"))
                               + "; operate it through the job CLI, which runs that pin")
        if self.pin_dir is not None:
            try:
                verifier_pins.verify(self.pin_dir, only=("ops_harness",))
            except verifier_pins.PinError as exc:
                raise RuntimeError("Pinned workflow runtime was modified: " + str(exc)) from exc
        if state["fingerprint"] != self.fingerprint():
            raise RuntimeError("Workflow implementation or registry changed; inspect/cancel this job and start a new one")

    def start(self, job_id: str, request: dict):
        with self._session(job_id) as (graph, config):
            if graph.get_state(config).values:
                raise RuntimeError("job_id already exists")
            normalized = validate_request(self.root, request)
            checks = normalized.get("checks", list(PROBES))
            if not isinstance(checks, list) or not checks or any(c not in PROBES for c in checks) or len(set(checks)) != len(checks):
                raise ValueError("Select a nonempty set of registered diagnostic checks")
            attempts = normalized.get("max_attempts", 3)
            if type(attempts) is not int or not 1 <= attempts <= 5:
                raise ValueError("max_attempts must be 1..5")
            scope = normalized.get("authorization_scope", "Only local work within this request; external actions require existing explicit authorization")
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError("authorization_scope must be a nonempty description")
            normalized.update(checks=checks, max_attempts=attempts, authorization_scope=scope)
            if normalized["workflow"] == "daily-report":
                # This automatic workflow cannot be repurposed as a general acceptance bypass.
                if request.get("artifacts") or request.get("criteria"):
                    raise ValueError("daily-report has fixed report-only acceptance; use daily-ops for repair work")
            initial = {"job_id": job_id, "request": normalized, "fingerprint": self.fingerprint(),
                       "attempt": 0, "history": [], "status": "created"}
            if self.pin is not None:
                initial["verifier_pin"] = self.pin
            graph.invoke(initial, config, durability="sync")
            return self._snapshot(graph, config)

    def inspect(self, job_id):
        with self._session(job_id) as (graph, config):
            return self._snapshot(graph, config)

    def _pending(self, graph, config, kind, payload):
        state = self._snapshot(graph, config)
        self._check_current(state)
        if not state["interrupts"] or state["interrupts"][0]["kind"] != kind:
            raise RuntimeError("Job is not waiting for this operation")
        if not isinstance(payload, dict) or payload.get("token") != state["token"]:
            raise ValueError("Submission has a missing or stale dispatch token")
        return state

    def submit(self, job_id, submission):
        with self._session(job_id) as (graph, config):
            self._pending(graph, config, "work", submission)
            if set(submission) != {"token", "worker_id", "artifacts"}:
                raise ValueError("submit accepts only token, worker_id and artifact references")
            if not isinstance(submission["worker_id"], str) or not submission["worker_id"].strip():
                raise ValueError("worker_id required")
            artifacts = submission["artifacts"]
            if not isinstance(artifacts, list) or not artifacts or any(
                not isinstance(a, dict) or set(a) != {"path", "sha256"} or not isinstance(a["path"], str)
                or not isinstance(a["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) for a in artifacts
            ):
                raise ValueError("artifacts require path and lowercase sha256")
            graph.invoke(Command(resume=submission), config, durability="sync")
            return self._snapshot(graph, config)

    def review(self, job_id, review):
        with self._session(job_id) as (graph, config):
            state = self._pending(graph, config, "review", review)
            if set(review) != {"token", "reviewer_id", "decision", "criteria"}:
                raise ValueError("Invalid review fields")
            reviewer = review["reviewer_id"]
            if not isinstance(reviewer, str) or not reviewer.strip() or reviewer == state["submission"]["worker_id"]:
                raise ValueError("Independent reviewer_id required")
            rows = review["criteria"]
            expected = {c["id"] for c in state["request"]["criteria"]}
            if not isinstance(rows, list) or len(rows) != len(expected) or any(
                not isinstance(c, dict) or set(c) != {"id", "passed", "note"}
                or not isinstance(c["id"], str) or type(c["passed"]) is not bool
                or not isinstance(c["note"], str) or not c["note"].strip() for c in rows
            ) or {c["id"] for c in rows} != expected:
                raise ValueError("Review must assess every fixed criterion with an evidence note")
            if review["decision"] not in ("approve", "reject") or (review["decision"] == "approve" and not all(c["passed"] for c in rows)):
                raise ValueError("Approval requires every criterion to pass")
            graph.invoke(Command(resume=review), config, durability="sync")
            return self._snapshot(graph, config)

    def continue_job(self, job_id):
        with self._session(job_id) as (graph, config):
            state = self._snapshot(graph, config)
            self._check_current(state)
            if state["interrupts"] or not state["next"]:
                raise RuntimeError("Use submit/review for pending work; terminal jobs cannot continue")
            graph.invoke(None, config, durability="sync")
            return self._snapshot(graph, config)

    def cancel(self, job_id):
        with self._session(job_id) as (graph, config):
            state = self._snapshot(graph, config)
            if state["status"] in ("completed", "failed", "cancelled"):
                raise RuntimeError("Terminal job cannot be cancelled")
            # Fixed administrative transition via LangGraph, never arbitrary user state/goto.
            graph.update_state(config, self._cancel(state), as_node="cancel")
            return self._snapshot(graph, config)
