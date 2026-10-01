"""Entry point executed inside a verifier pin: ``<pin>/files`` is first on sys.path.

argv: <workspace root> <job command...>. The runtime derives its pin from the
location of this code, so the caller cannot claim a different pin.
"""
from pathlib import Path


def main(argv):
    from ops_harness import verifier_pins
    from ops_harness.workflow_cli import run_local
    if verifier_pins.owning_pin(Path(__file__)) is None:
        raise SystemExit("pinned_entry must run from a verifier pin")
    return run_local(Path(argv[0]), argv[1:])
