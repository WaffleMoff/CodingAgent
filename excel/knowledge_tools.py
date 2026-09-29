"""Excel knowledge files: conventions and research playbooks the agent reads on demand.

The agent is never handed these automatically. The system prompt names the tools
and the moment to call them, so a task that never touches Excel never pays for the
context. Both files live in `knowledge/` next to the app and are writable, so the
user edits conventions in Markdown without touching code.

Resolution order for a file:
    1. $EXCEL_KNOWLEDGE_DIR (absolute or relative to the app dir)
    2. <app>/knowledge/<name>
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

# This module lives in <app>/excel/, so the knowledge directory is one level up.
APP_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = Path(
    os.getenv("EXCEL_KNOWLEDGE_DIR", APP_DIR / "knowledge")
).expanduser()

EXCEL_GUIDE = "EXCEL_GUIDE.md"
RESEARCH_GUIDE = "RESEARCH_GUIDE.md"
RESEARCH_NOTES = "RESEARCH_NOTES.md"

MAX_READ_CHARS = int(os.getenv("KNOWLEDGE_MAX_CHARS", "40000"))
MAX_NOTE_CHARS = int(os.getenv("KNOWLEDGE_MAX_NOTE_CHARS", "4000"))

# Names the agent may address by keyword, so the model can ask for "research" or
# "research_notes" instead of a filename it has to remember.
ALIASES = {
    "excel": EXCEL_GUIDE,
    "excel_guide": EXCEL_GUIDE,
    "conventions": EXCEL_GUIDE,
    "research": RESEARCH_GUIDE,
    "research_guide": RESEARCH_GUIDE,
    "equity": RESEARCH_GUIDE,
    "notes": RESEARCH_NOTES,
    "research_notes": RESEARCH_NOTES,
    "scratch": RESEARCH_NOTES,
}

DEFAULT_MISSING = (
    "File not found: {path}\n"
    "The knowledge directory is {dir}. Create the file with write_file, or set "
    "EXCEL_KNOWLEDGE_DIR to point at the directory that holds it."
)


def _resolve(name: str) -> Path:
    key = (name or "").strip()
    if not key:
        raise ValueError("document name is required")
    key = ALIASES.get(key.lower(), key)
    candidate = Path(key)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (KNOWLEDGE_DIR / candidate).resolve()
    root = KNOWLEDGE_DIR.resolve()
    if root not in resolved.parents and resolved != root:
        raise PermissionError(
            f"{resolved} is outside the knowledge directory {root}; "
            "only files beneath it can be read"
        )
    return resolved


def _read(name: str, limit: int = MAX_READ_CHARS) -> str:
    path = _resolve(name)
    if not path.exists():
        return DEFAULT_MISSING.format(path=path, dir=KNOWLEDGE_DIR)
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        text = text[:limit] + f"\n...[truncated at {limit} characters; full file is {path}]"
    return f"# {path.name}\n\n{text}"


def read_excel_guide() -> str:
    """House conventions for building workbooks. Read before the first excel_* call."""
    return _read(EXCEL_GUIDE)


def read_research_guide() -> str:
    """How to do public equity research with this toolset. Read before starting a deck."""
    return _read(RESEARCH_GUIDE)


def read_research_notes() -> str:
    """Free-form working notes (standing context plus dated session entries)."""
    return _read(RESEARCH_NOTES)


def append_research_notes(note: str, tag: str = "note") -> str:
    """Append a dated entry to the research notes file."""
    text = (note or "").strip()
    if not text:
        return "Empty note; nothing written."
    if len(text) > MAX_NOTE_CHARS:
        return f"Note is {len(text)} characters; keep it under {MAX_NOTE_CHARS}."
    path = _resolve(RESEARCH_NOTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Research scratch notes\n", encoding="utf-8")
    import datetime as _dt

    stamp = _dt.date.today().isoformat()
    entry = f"\n\n### {stamp} — {tag.strip() or 'note'}\n{text}\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry)
    return f"Appended {len(text)} characters to {path}"


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_excel_guide",
            "description": (
                "Read the house Excel conventions (sheet layout, number formats, naming, "
                "reported-vs-estimated colouring, verification checklist). Call this once "
                "before the first excel_* tool call in a task; the file is maintained by the "
                "user and is the authority on formatting."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_research_guide",
            "description": (
                "Read the equity-research playbook: source hierarchy (SEC EDGAR, XBRL, "
                "filings), fetch rules and rate limits, build order, known traps, and what "
                "to hand back. Call this when starting a research or data-compilation task."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_research_notes",
            "description": (
                "Read the user's free-form research notes: standing context, house metric "
                "definitions, coverage universe, prior conclusions."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_research_notes",
            "description": (
                "Append a dated entry to the research notes file. Use sparingly, for "
                "durable findings the user would want next session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string"},
                    "tag": {"type": "string", "description": "Short label, e.g. 'LYV' or 'source'."},
                },
                "required": ["note"],
            },
        },
    },
]


def handlers() -> dict[str, Callable[..., str]]:
    return {
        "read_excel_guide": read_excel_guide,
        "read_research_guide": read_research_guide,
        "read_research_notes": read_research_notes,
        "append_research_notes": append_research_notes,
    }
