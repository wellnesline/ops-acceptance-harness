"""Usage: python -m ops_harness {list,route,doctor,show,resume,abandon}."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

from .probes import PROBES, result, within
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config/ops_harness.json"
DB = ROOT / ".harness/runs.sqlite3"


def diagnostic_exit(payload: dict) -> int:
    if payload.get("status") != "diagnosed":
        return 2
    return {"pass": 0, "warn": 1, "unknown": 2, "fail": 2}.get(payload.get("verdict"), 2)


def load_registry(path: Path = REGISTRY) -> tuple[dict, str]:
    data = path.read_bytes()
    registry = json.loads(data.decode("utf-8-sig"))
    if registry.get("schema_version") != 1:
        raise ValueError("Unsupported registry schema")
    service_ids = set()
    for service in registry["services"]:
        if service["id"] in service_ids:
            raise ValueError("Duplicate service id")
        service_ids.add(service["id"])
        for relative in service["source_paths"] + service["entrypoints"] + [service["runbook"].split("#")[0]]:
            within(ROOT, relative)
        if set(service["checks"]) - PROBES.keys():
            raise ValueError("Unknown probe in registry")
    if not service_ids:
        raise ValueError("Empty service registry")
    workflow_ids = set()
    for workflow in registry["workflows"]:
        if workflow["id"] in workflow_ids or set(workflow["service_ids"]) - service_ids:
            raise ValueError("Invalid workflow references")
        workflow_ids.add(workflow["id"])
    digest = hashlib.sha256(data)
    # Do not resume old evidence after changing probe, state or orchestration semantics.
    for name in ("__main__.py", "probes.py", "store.py"):
        digest.update((Path(__file__).parent / name).read_bytes())
    return registry, digest.hexdigest()


def route(registry: dict, workflow_id: str) -> dict:
    workflow = next((w for w in registry["workflows"] if w["id"] == workflow_id), None)
    if workflow is None:
        raise ValueError("Unknown workflow; use list")
    services = [s for s in registry["services"] if s["id"] in workflow["service_ids"]]
    return {"workflow": workflow, "services": services, "business_completion": "not_assessed",
            "execution": "Route only. The conversation orchestrator executes existing runbooks within user authorization.",
            "handoff_contract": ["Record goal, target, expected evidence and existing authorization.",
                                 "Read the service source and runbook; do not edit distribution copies.",
                                 "Use specialist agents only as needed, with bounded scope.",
                                 "Check actual artifacts independently; diagnostic pass is not business completion.",
                                 "Do not replay uncertain external side effects; inspect before retrying."]}


def execute(store: Store, state: dict, registry: dict) -> dict:
    for check_id in state["pending_checks"]:
        start = time.monotonic()
        try:
            outcome = PROBES[check_id](ROOT, registry)
        except Exception as exc:
            # No exception message: it can contain paths, credentials or raw response data.
            outcome = result("unknown", "Probe failed unexpectedly", error_type=type(exc).__name__)
        outcome["duration_seconds"] = round(time.monotonic() - start, 3)
        store.record(state["run_id"], check_id, outcome)
    return store.finish(state["run_id"])


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["job"]:
        from .workflow_cli import main as job_main
        return job_main(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    route_parser = sub.add_parser("route")
    route_parser.add_argument("workflow")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--run-id", required=True)
    doctor.add_argument("--check", action="append", choices=sorted(PROBES))
    for name in ("show", "resume", "abandon"):
        sub.add_parser(name).add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        # Recovery and evidence inspection must survive a broken/changed registry.
        if args.command in ("show", "abandon"):
            store = Store(DB)
            payload = store.get(args.run_id) if args.command == "show" else store.abandon(args.run_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return diagnostic_exit(payload)
        registry, config_hash = load_registry()
        if args.command == "list":
            payload = {"services": [{"id": s["id"], "label": s["label"], "checks": s["checks"]} for s in registry["services"]],
                       "workflows": [{"id": w["id"], "label": w["label"]} for w in registry["workflows"]],
                       "checks": sorted(PROBES), "business_completion": "not_assessed"}
        elif args.command == "route":
            payload = route(registry, args.workflow)
        else:
            store = Store(DB)
            if args.command == "doctor":
                state = store.start(args.run_id, config_hash, args.check or list(PROBES))
                payload = execute(store, state, registry)
            else:
                payload = execute(store, store.resume(args.run_id, config_hash), registry)
            payload["coverage_note"] = "Only recorded checks were assessed. Unprobed services, production outputs and business completion remain unassessed."
            payload["evidence_note"] = "observed_at is each check's observation time (Unix seconds). Resume preserves old evidence; run a new diagnosis for current health."
            payload["service_coverage"] = {s["id"]: {
                "checked": [c for c in s["checks"] if c in payload["results"]],
                "not_checked": [c for c in s["checks"] if c not in payload["results"]],
                "business_status": "unknown",
            } for s in registry["services"]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if args.command in ("list", "route") else diagnostic_exit(payload)
    except (ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": "Local registry or evidence storage unavailable"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    # JSON is UTF-8 even when Windows redirects stdout through a CP932 pipe.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
