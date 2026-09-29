"""Verified-edit loop: a post_tool_use hook that checks what was just written.

A modern agent does not treat a successful `write_file` as proof the file is
valid. This module runs the cheapest available check on every file mutation and
appends the verdict to the tool result, so a syntax error surfaces in the same
turn it was introduced instead of three steps later as a mysterious failure.

Checks are chosen by file type and are all best-effort:
  - .py  -> python -m py_compile (syntax), plus ruff/black --check if present
  - .json-> json.loads
  - .md/.txt/.toml/.yaml/.yml -> no check (nothing cheap and meaningful)
  - anything else -> no check

The verdict is always appended, even when it passes: "no issues" is information
too, and it tells the model the check actually ran. If a checker is not
installed, that is reported once ("ruff not available") rather than silently
skipped, so a missing tool is never mistaken for a passing one.

`make_verifier()` returns a callable matching the post_tool_use hook signature:
    (name, args, result, messages) -> str | None
It returns the text to append, or None when the tool was not a mutation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any

# Tool name -> the argument that names the file being touched.
_MUTATING = {
    "write_file": "path",
    "apply_patch": None,   # multi-file; handled specially
}

# Seconds any single checker may run before we give up on it.
CHECK_TIMEOUT = 10

_CHECKED_SUFFIXES = {".py", ".json"}


def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=CHECK_TIMEOUT,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not available"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {CHECK_TIMEOUT}s"
    except Exception as exc:  # never let a checker crash the loop
        return 1, f"checker error: {exc!r}"


def _check_python(path: str) -> str:
    lines: list[str] = []
    code, out = _run([sys.executable, "-m", "py_compile", path])
    if code == 0:
        lines.append("py_compile: ok")
    else:
        lines.append("py_compile: SYNTAX ERROR\n" + out.strip())

    if shutil.which("ruff"):
        code, out = _run(["ruff", "check", path])
        lines.append("ruff: ok" if code == 0 else "ruff: issues\n" + out.strip())
    else:
        lines.append("ruff: not installed (skipped)")

    if shutil.which("black"):
        code, out = _run(["black", "--check", "-q", path])
        lines.append("black: formatted" if code == 0 else "black: would reformat")
    else:
        lines.append("black: not installed (skipped)")
    return "\n".join(lines)


def _check_json(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            json.load(fh)
        return "json: ok"
    except Exception as exc:
        return f"json: INVALID ({exc})"


def _check_file(path: str) -> str | None:
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in _CHECKED_SUFFIXES:
        return None
    if not os.path.exists(path):
        return f"verify: {path} does not exist after the write (write may have failed)"
    if suffix == ".py":
        return _check_python(path)
    if suffix == ".json":
        return _check_json(path)
    return None


def make_verifier(write_root: str = "/workspace/write", enabled: bool | None = None):
    """Build a post_tool_use hook that verifies file mutations.

    `enabled` defaults to VERIFY_EDITS env (on unless set to off).
    """
    if enabled is None:
        enabled = os.getenv("VERIFY_EDITS", "on").strip().lower() != "off"

    def verifier(name: str, args: dict[str, Any], result: str,
                 messages: list[dict[str, Any]]) -> str | None:
        if not enabled:
            return None
        if name == "apply_patch":
            # apply_patch reports the files it touched on its last line; verify
            # each. If we cannot parse them, skip rather than guess.
            files = _patch_targets(result)
            verdicts = [(f, _check_file(f)) for f in files]
        elif name == "write_file":
            path = args.get("path")
            if not isinstance(path, str):
                return None
            verdicts = [(path, _check_file(path))]
        else:
            return None

        lines = [f"[verify] {p}: {v}" for p, v in verdicts if v]
        if not lines:
            return None
        return "\n".join(lines)

    return verifier


def _patch_targets(result: str) -> list[str]:
    """Extract touched file paths from an apply_patch tool result."""
    targets: list[str] = []
    for line in result.splitlines():
        if line.startswith("ok: applied patch to "):
            for part in line[len("ok: applied patch to "):].split(","):
                part = part.strip()
                if part and part != "no files":
                    targets.append(part)
    return targets
