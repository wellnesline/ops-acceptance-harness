"""Adapter placed as ops_harness/pinned_entry.py inside the frozen legacy runtime pin.

Jobs started before verifier pinning have no ``verifier_pin`` in their state.
They keep running on the exact pre-pinning workflow code, and their original
fingerprint is recomputed from the frozen copies instead of the repository, so
the cutover itself does not stale them. Software-development jobs are out of
scope: the cutover requires that none of them is in flight.
"""
from pathlib import Path

FILES = Path(__file__).resolve().parents[1]


def legacy_runtime(root):
    from ops_harness.workflow import WorkflowRuntime

    class LegacyRuntime(WorkflowRuntime):
        def __init__(self, root):
            super().__init__(root)
            # A legacy job has no pin of its own; never let pin-aware code adopt this frozen copy.
            self.pin = None
            self.pin_dir = None
            self.verifier_root = FILES

        def fingerprint(self):
            # The pre-pinning fingerprint read registry and dev_harness files from the workspace.
            workspace = self.root
            self.root = FILES
            try:
                return super().fingerprint()
            finally:
                self.root = workspace

    return LegacyRuntime(Path(root))


def main(argv):
    import json
    import sqlite3
    import sys
    from ops_harness.workflow_cli import summary, _read_json

    root, command, job_id, *rest = argv
    try:
        runtime = legacy_runtime(root)
        if command == "status":
            state = runtime.inspect(job_id)
        elif command == "submit":
            state = runtime.submit(job_id, _read_json(rest[1]))
        elif command == "review":
            state = runtime.review(job_id, _read_json(rest[1]))
        elif command == "continue":
            state = runtime.continue_job(job_id)
        elif command == "cancel":
            state = runtime.cancel(job_id)
        else:
            raise ValueError("Unsupported command for a legacy job")
        print(json.dumps(summary(state), ensure_ascii=False, indent=2))
        if state["status"] == "completed":
            return 0
        return 3 if state["status"] in ("awaiting_work", "awaiting_review") else 2
    except (ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": "Local workflow storage or input unavailable"}),
              file=sys.stderr)
        return 2
