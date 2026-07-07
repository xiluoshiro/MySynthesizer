from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist" / "MySynthesizer"
ENTRY_SCRIPT = ROOT / "scripts" / "workbench_entry.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local MySynthesizer workbench package.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned package layout without building.")
    parser.add_argument("--name", default="MySynthesizer")
    args = parser.parse_args()

    plan = {
        "entry": str(ENTRY_SCRIPT),
        "dist": str(DIST_DIR),
        "ui": str(ROOT / "ui"),
        "data": str(ROOT / "data" / "engine" / "mysynth.db"),
        "requires_pyinstaller": True,
    }
    if args.dry_run:
        checks = {
            "entry_exists": ENTRY_SCRIPT.is_file(),
            "ui_exists": (ROOT / "ui" / "index.html").is_file(),
            "data_dir_exists": (ROOT / "data" / "engine").is_dir(),
        }
        print(json.dumps({"ok": all(checks.values()), "plan": plan, "checks": checks}, ensure_ascii=False, indent=2))
        return 0

    pyinstaller = shutil.which("pyinstaller")
    if pyinstaller is None:
        print("pyinstaller not found; install it or run with --dry-run", file=sys.stderr)
        return 2

    command = [
        pyinstaller,
        "--noconfirm",
        "--name",
        args.name,
        "--add-data",
        f"{ROOT / 'ui'}{os.pathsep}ui",
        "--add-data",
        f"{ROOT / 'data' / 'engine'}{os.pathsep}data/engine",
        str(ENTRY_SCRIPT),
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
