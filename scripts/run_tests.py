from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_FILES = [
    ROOT / "mysynth",
    ROOT / "tests",
    ROOT / "scripts",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all MySynthesizer checks.")
    parser.add_argument("--skip-syntax", action="store_true", help="Skip Python syntax checks.")
    parser.add_argument("--skip-unit", action="store_true", help="Skip unittest discovery.")
    args = parser.parse_args()

    steps: list[tuple[str, callable[[], int]]] = []
    if not args.skip_syntax:
        steps.append(("syntax", run_syntax_check))
    if not args.skip_unit:
        steps.append(("unit", run_unittest))

    for name, step in steps:
        print(f"[run-tests] {name}...", flush=True)
        code = step()
        if code != 0:
            print(f"[run-tests] {name} failed with exit code {code}", flush=True)
            return code

    print("[run-tests] all checks passed", flush=True)
    return 0


def run_syntax_check() -> int:
    for base in PYTHON_FILES:
        if not base.exists():
            continue
        paths = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in paths:
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                return 1
    return 0


def run_unittest() -> int:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ]
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
