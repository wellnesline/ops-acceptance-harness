"""Portable entrypoint: python scripts/ops/harness.py [--setup | job ...]."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / ".harness" / "venv"
PYTHON = ENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def main():
    if sys.argv[1:] == ["--setup"]:
        if not PYTHON.exists():
            subprocess.run([sys.executable, "-m", "venv", str(ENV)], check=True)
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements-ops.lock.txt"),
                        "--disable-pip-version-check"], check=True)
        return subprocess.call([str(PYTHON), "-m", "pip", "check"])
    if not PYTHON.is_file():
        print("Run: python scripts/ops/harness.py --setup", file=sys.stderr)
        return 2
    return subprocess.call([str(PYTHON), "-m", "ops_harness", *sys.argv[1:]], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
