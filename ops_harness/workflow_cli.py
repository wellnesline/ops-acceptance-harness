"""Command surface for LangGraph jobs. No arbitrary commands or state edits.

Every job runs from the verifier pin recorded when it started: ``start`` pins
the current shared verifier and workflow code, and later commands execute that
read-only copy (see ``ops_harness.verifier_pins``). Updating the repository's
shared files therefore affects only jobs started afterwards.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
# Digest of a frozen pre-pinning runtime, only for migrating jobs created before verifier pinning existed.
# Not needed in a fresh environment: leave empty (the legacy path is then skipped).
LEGACY_RUNTIME_PIN = ""
JOB_COMMANDS = ("status", "submit", "review", "continue", "cancel", "gate", "pin")
BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "from ops_harness.pinned_entry import main; raise SystemExit(main(sys.argv[2:]))")


def _read_json(path):
    file = Path(path)
    if file.stat().st_size > 262144:
        raise ValueError("Input JSON exceeds 256 KiB")
    data = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    return data


def summary(state):
    request = state["request"]
    return {"job_id": state["job_id"], "status": state["status"], "framework": state["framework"],
            "workflow": request["workflow"], "objective": request["objective"],
            "authorization_scope": request["authorization_scope"],
            "verifier_pin": state.get("verifier_pin"),
            "attempt": state.get("attempt", 0), "token": state.get("token"),
            "criteria": request["criteria"],
            "artifacts": [{k: v for k, v in a.items() if not k.startswith("baseline_")} for a in request["artifacts"]],
            "artifact": state.get("artifact"), "verification": state.get("verification"),
            "review": state.get("review"), "completion_scope": state.get("completion_scope"),
            "next": state["next"], "history": state["history"],
            "diagnostics": {k: {"status": v["status"], "observed_at": v["observed_at"]} for k, v in state.get("diagnostics", {}).items()},
            "handoff": state["interrupts"][0]["kind"] if state["interrupts"] else None}


def _parser():
    parser = argparse.ArgumentParser(description="Real LangGraph workflow execution and checkpoint recovery")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    start = sub.add_parser("start")
    start.add_argument("--job-id", required=True)
    start.add_argument("--request", required=True)
    for name in ("status", "continue", "cancel"):
        sub.add_parser(name).add_argument("job_id")
    for name, field in (("submit", "submission"), ("review", "review")):
        command = sub.add_parser(name)
        command.add_argument("job_id")
        command.add_argument("--" + field, required=True)
    gate = sub.add_parser("gate", help="Run this job's pinned OpenSpec gate and save its JSON to the artifact path")
    gate.add_argument("job_id")
    sub.add_parser("pin", help="Show and fully verify this job's verifier pin").add_argument("job_id")
    return parser


def _error(exc, generic=False):
    message = "Local workflow storage or input unavailable" if generic else str(exc)
    print(json.dumps({"error_type": type(exc).__name__, "message": message}, ensure_ascii=False), file=sys.stderr)
    return 2


def run_local(root, argv):
    """Execute one command with the code of this process (a pin, or the repository when unpinned)."""
    args = _parser().parse_args(argv)
    try:
        from .workflow import WorkflowRuntime
        if args.command == "list":
            from .workflow_actions import WORKFLOWS
            from importlib.metadata import version
            print(json.dumps({"framework": "langgraph", "version": version("langgraph"),
                              "workflows": sorted(WORKFLOWS)}, ensure_ascii=False, indent=2))
            return 0
        runtime = WorkflowRuntime(Path(root))
        if args.command == "pin":
            return _pin_report(runtime, args.job_id)
        if args.command == "gate":
            return _gate(runtime, args.job_id)
        if args.command == "start":
            state = runtime.start(args.job_id, _read_json(args.request))
        elif args.command == "status":
            state = runtime.inspect(args.job_id)
        elif args.command == "submit":
            state = runtime.submit(args.job_id, _read_json(args.submission))
        elif args.command == "review":
            state = runtime.review(args.job_id, _read_json(args.review))
        elif args.command == "continue":
            state = runtime.continue_job(args.job_id)
        else:
            state = runtime.cancel(args.job_id)
        print(json.dumps(summary(state), ensure_ascii=False, indent=2))
        if state["status"] == "completed":
            return 0
        return 3 if state["status"] in ("awaiting_work", "awaiting_review") else 2
    except ModuleNotFoundError:
        print("LangGraph runtime unavailable. Run python scripts/ops/harness.py --setup", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, KeyError) as exc:
        return _error(exc)
    except (OSError, sqlite3.Error) as exc:
        return _error(exc, generic=True)


def _pin_report(runtime, job_id):
    from . import verifier_pins
    state = runtime.inspect(job_id)
    pin = state.get("verifier_pin")
    report = {"job_id": job_id, "verifier_pin": pin, "running_pin": runtime.pin}
    if pin is None:
        report["intact"] = None
        report["detail"] = "Job started before verifier pinning; it uses the frozen legacy runtime or the repository"
    else:
        try:
            files = verifier_pins.verify(verifier_pins.pin_path(runtime.state_dir, pin))
            report.update(intact=True, files=len(files),
                          location=verifier_pins.pin_path(runtime.state_dir, pin).relative_to(runtime.root).as_posix())
        except verifier_pins.PinError as exc:
            report.update(intact=False, detail=str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["intact"] is not False else 2


def _print_pin(job_id, pin, pin_dir, root, files=None, error=None):
    """Pin report produced by the repository dispatcher, without running any pinned code."""
    report = {"job_id": job_id, "verifier_pin": pin, "intact": error is None,
              "location": pin_dir.relative_to(root).as_posix() if pin_dir.is_relative_to(root) else str(pin_dir)}
    if error is None:
        report["files"] = len(files)
    else:
        report["detail"] = str(error)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if error is None else 2


def _gate(runtime, job_id):
    """Produce the evidence file a worker submits, from the job's own pinned gate."""
    from .development import run_gate
    from .workflow_actions import _path, hash_file
    state = runtime.inspect(job_id)
    runtime._check_current(state)
    targets = [a for a in state["request"]["artifacts"] if a["kind"] == "openspec"]
    if not targets:
        raise ValueError("This job has no openspec artifact")
    results = []
    for artifact in targets:
        code, stdout = run_gate(runtime.root, artifact, runtime.verifier_root)
        report = json.loads(stdout)
        if not isinstance(report, dict):
            raise ValueError("Gate returned an invalid report")
        _, destination = _path(runtime.root, artifact["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results.append({"path": artifact["path"], "sha256": hash_file(destination),
                        "status": report.get("status"), "exit_code": code})
    print(json.dumps({"job_id": job_id, "verifier_pin": state.get("verifier_pin"), "artifacts": results},
                     ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "passed" for r in results) else 2


def _job_pin(state_dir, job_id):
    """Read the pin recorded in the job's LangGraph checkpoint (read-only)."""
    database = Path(state_dir) / "langgraph.sqlite3"
    if not database.is_file():
        return False, None
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        saver = SqliteSaver(connection, serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[],
                                                                 allowed_json_modules=[]))
        found = saver.get_tuple({"configurable": {"thread_id": job_id}})
    finally:
        connection.close()
    if found is None:
        return False, None
    return True, found.checkpoint.get("channel_values", {}).get("verifier_pin")


def _exec_pin(pin_dir, root, argv):
    command = [sys.executable, "-I", "-B", "-X", "utf8", "-c", BOOTSTRAP, str(pin_dir / "files"), str(root), *argv]
    return subprocess.call(command, cwd=root)


def main(argv=None, root=None, legacy_pin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(root or ROOT).resolve()
    legacy_pin = LEGACY_RUNTIME_PIN if legacy_pin is None else legacy_pin
    args = _parser().parse_args(argv)
    if args.command == "list":
        return run_local(root, argv)
    try:
        from . import verifier_pins
        from .workflow_lock import validate_job_id
        state_dir = root / ".harness"
        if args.command == "start":
            validate_job_id(args.job_id)
            request = Path(args.request).resolve()
            pin = verifier_pins.create(root, state_dir)
            pin_dir = verifier_pins.pin_path(state_dir, pin)
            verifier_pins.verify(pin_dir)
            return _exec_pin(pin_dir, root, ["start", "--job-id", args.job_id, "--request", str(request)])
        validate_job_id(args.job_id)
        known, pin = _job_pin(state_dir, args.job_id)
        forwarded = [args.command, args.job_id]
        for field in ("submission", "review"):
            if getattr(args, field, None):
                forwarded += ["--" + field, str(Path(getattr(args, field)).resolve())]
        if known and pin is not None:
            # Verify the whole pin here, in repository code, before any pinned code runs: <pin>/files
            # is first on the child's sys.path, so any planted module would execute before checks.
            pin_dir = verifier_pins.pin_path(state_dir, pin)
            try:
                files = verifier_pins.verify(pin_dir)
            except verifier_pins.PinError as exc:
                if args.command == "pin":
                    return _print_pin(args.job_id, pin, pin_dir, root, error=exc)
                if args.command in ("status", "cancel"):
                    # Inspect and cancel never execute pinned code, so a modified job can still be closed.
                    return run_local(root, argv)
                raise RuntimeError("Verifier pin of this job was modified (" + str(exc)
                                   + "); it cannot pass. Cancel the job and start a new one") from exc
            if args.command == "pin":
                return _print_pin(args.job_id, pin, pin_dir, root, files=files)
            return _exec_pin(pin_dir, root, forwarded)
        if known and legacy_pin and args.command not in ("gate", "pin"):
            pin_dir = verifier_pins.pin_path(state_dir, legacy_pin)
            if pin_dir.is_dir():
                verifier_pins.verify(pin_dir)
                return _exec_pin(pin_dir, root, forwarded)
        return run_local(root, argv)
    except ModuleNotFoundError:
        print("LangGraph runtime unavailable. Run python scripts/ops/harness.py --setup", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, KeyError) as exc:
        return _error(exc)
    except (OSError, sqlite3.Error) as exc:
        return _error(exc, generic=True)
