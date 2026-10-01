#!/usr/bin/env python3
"""synthetic stub / 合成スタブ: minimal Markdown index lint used by the memory-maintenance workflow.

Usage: python scripts/qa_memory_md_lint.py --memory-file notes/INDEX.md
Exit 1 when any line is longer than MAX_LINE_CHARS (link URLs are not counted). Replace with your own linter.
"""
import argparse
import re
import sys
from pathlib import Path

MAX_LINE_CHARS = 150


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-file", required=True)
    args = parser.parse_args(argv)
    failures = 0
    for number, line in enumerate(Path(args.memory_file).read_text(encoding="utf-8").splitlines(), 1):
        visible = re.sub(r"\]\([^)]+\)", "]", line)
        if len(visible) > MAX_LINE_CHARS:
            failures += 1
            print(f"line {number}: {len(visible)} chars", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())