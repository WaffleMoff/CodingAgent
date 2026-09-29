#!/usr/bin/env python3
"""Install the Excel engine and knowledge files into a running agent sandbox.

The `excel_*` tools install the engine on first use, but a sandbox that will do
heavy Excel work is better pre-warmed, and the knowledge files need to exist
before the agent asks for them. Run this once after the app starts, or from the
UI's Tools tab.

    python3 bootstrap_excel.py                # uses config defaults
    python3 bootstrap_excel.py --check        # report only, change nothing
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# Deliberately avoids importing config.py: that module loads .env and pulls in
# python-dotenv, and this script must run even when dependencies are missing.
from excel.knowledge_tools import (  # noqa: E402
    EXCEL_GUIDE, KNOWLEDGE_DIR as RESOLVED_DIR, RESEARCH_GUIDE, RESEARCH_NOTES,
)

KNOWLEDGE_DIR = Path(os.getenv("EXCEL_KNOWLEDGE_DIR", APP_DIR / "knowledge"))

REQUIRED_FILES = (EXCEL_GUIDE, RESEARCH_GUIDE, RESEARCH_NOTES)
ENGINE_FILES = ("xlsx.py", "engine.py", "excel_test.py")


def check() -> int:
    app_dir = APP_DIR
    problems = 0

    print(f"knowledge dir: {RESOLVED_DIR}")
    if not RESOLVED_DIR.exists():
        print("  MISSING - create it and add the guide files")
        problems += 1
    else:
        for name in REQUIRED_FILES:
            path = RESOLVED_DIR / name
            if path.exists():
                print(f"  ok   {name} ({path.stat().st_size} bytes)")
            else:
                print(f"  MISSING {name} - the agent will be told it does not exist")
                problems += 1

    print(f"engine dir: {app_dir / 'excel'}")
    for name in ENGINE_FILES:
        path = app_dir / "excel" / name
        print(f"  {'ok  ' if path.exists() else 'MISSING'} {name}")
        problems += 0 if path.exists() else 1

    print(f"app files: {app_dir}")
    for name in ("excel_tools.py", "tools.py", "prompts.py"):
        path = app_dir / name
        print(f"  {'ok  ' if path.exists() else 'MISSING'} {name}")
        problems += 0 if path.exists() else 1

    if str(KNOWLEDGE_DIR) != str(RESOLVED_DIR):
        print(f"note: EXCEL_KNOWLEDGE_DIR={KNOWLEDGE_DIR} overrides the default")

    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report only")
    args = parser.parse_args()
    if args.check:
        return check()
    status = check()
    if status:
        print("\nFix the problems above, then restart the app.")
        return status
    print("\nEverything present. The excel_* tools self-install the engine on first use.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
