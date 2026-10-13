"""Plan / todo tool for the main agent.

Claude Code ships `TodoWrite`; Cursor ships Plan mode. Both let the model keep a
structured task list outside its context window so a long run does not lose the
thread. This is the local equivalent: one tool, `manage_plan`, that reads and
writes a small JSON plan file.

Plans are SESSION-SCOPED. Each session owns its own file under
`<app>/.agent/plans/<session_id>.json`, so one session's plan can never be read
back into another session. This is the fix for a real failure: a leftover plan in
a single global file was re-injected into an unrelated session by compaction's
state re-injection (`compaction.fidelity_messages`), making the agent believe it
was working on a task it had never been given.

It is a PLANNING aid, not a gate. It records steps and their status; whether an
unfinished plan blocks the final answer is a separate decision made by the
finish gate (see gate.py, `check_no_open_plan_items`), which reads this tool's
rendered output. Keeping the two separate means planning is always available and
never mandatory, and the stop condition stays configurable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATUS_MARK = {"todo": " ", "doing": "~", "done": "x"}

_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize(session_id: str) -> str:
    """Filesystem-safe session id. Empty or junk collapses to 'default'."""
    cleaned = _SESSION_RE.sub("_", (session_id or "").strip())
    return cleaned or "default"


def plan_path(session_id: str = "default") -> str:
    """Resolve the plan file for `session_id`.

    AGENT_PLAN_PATH, if set, is treated as a directory for per-session files so a
    single override still gives each session its own plan. A bare ``.json`` value
    is honoured verbatim, which preserves a fixed single-file override for
    tests/scripts. Without the env var, plans live in `<app>/.agent/plans/`.
    """
    env = os.getenv("AGENT_PLAN_PATH", "").strip()
    if env:
        p = Path(env)
        return str(p) if p.suffix == ".json" else str(p / f"{_sanitize(session_id)}.json")
    return str(Path(__file__).resolve().parent / ".agent" / "plans" / f"{_sanitize(session_id)}.json")


def default_plan_path(session_id: str = "default") -> str:
    return plan_path(session_id)


@dataclass
class Plan:
    path: str
    session_id: str = "default"
    task: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _next_id: int = 1

    @classmethod
    def for_session(cls, session_id: str) -> "Plan":
        return cls(path=plan_path(session_id), session_id=_sanitize(session_id))

    def add(self, text: str) -> dict[str, Any]:
        step = {"id": self._next_id, "text": text.strip(), "status": "todo", "note": ""}
        self._next_id += 1
        self.steps.append(step)
        return step

    def find(self, step_id: int) -> dict[str, Any] | None:
        return next((s for s in self.steps if s["id"] == step_id), None)

    def open_steps(self) -> list[dict[str, Any]]:
        return [s for s in self.steps if s["status"] != "done"]

    def can_finish(self) -> tuple[bool, str]:
        open_steps = self.open_steps()
        if open_steps:
            ids = ", ".join(f"{s['id']}:{s['text']}" for s in open_steps)
            return False, f"open steps: {ids}"
        return True, ""

    def save(self) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "session_id": self.session_id,
            "task": self.task,
            "steps": self.steps,
            "notes": self.notes,
            "next_id": self._next_id,
        }, indent=2), encoding="utf-8")

    def render(self) -> str:
        lines = [f"TASK: {self.task or '(unset)'}", ""]
        lines.append(f"STEPS ({len(self.steps)}):")
        if not self.steps:
            lines.append("  (none yet)")
        for s in self.steps:
            mark = STATUS_MARK.get(s["status"], " ")
            note = f"  <- {s['note']}" if s.get("note") else ""
            lines.append(f"  [{mark}] {s['id']}. {s['text']}{note}")
        if self.notes:
            lines.append("")
            lines.append("NOTES:")
            lines.extend(f"  - {n}" for n in self.notes)
        ok, reason = self.can_finish()
        lines.append("")
        lines.append("CAN FINISH: " + ("yes" if ok else f"no ({reason})"))
        return "\n".join(lines)


PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "manage_plan",
        "description": (
            "Maintain your task plan for long or multi-step work. Actions: "
            "'update' (set the task and/or replace the step list), 'add' (append "
            "steps), 'start' (mark a step doing), 'complete' (mark a step done), "
            "'note' (append a short note). Returns the current plan and whether "
            "you may finish. Use it when a task has more than a couple of steps so "
            "you do not lose the thread; it is not required for trivial requests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["update", "add", "start", "complete", "note"]},
                "task": {"type": "string", "description": "For 'update': the task restated."},
                "steps": {"type": "array", "items": {"type": "string"},
                          "description": "For 'update': concrete steps. Replaces the list."},
                "add_steps": {"type": "array", "items": {"type": "string"},
                              "description": "For 'add': steps to append."},
                "step_id": {"type": "integer", "description": "For 'start'/'complete'."},
                "note": {"type": "string", "description": "For 'note', or a per-step note."},
            },
            "required": ["action"],
        },
    },
}


class PlanTool:
    def __init__(self, plan: Plan):
        self.plan = plan

    def __call__(
        self,
        action: str,
        task: str | None = None,
        steps: list[str] | None = None,
        add_steps: list[str] | None = None,
        step_id: int | None = None,
        note: str | None = None,
    ) -> str:
        p = self.plan

        if action == "update":
            if task:
                p.task = task
            if steps is not None:
                p.steps = []
                p._next_id = 1
                for text in steps:
                    if text and text.strip():
                        p.add(text)
            if not p.steps:
                return "No steps recorded. Add at least one concrete step.\n\n" + p.render()

        elif action == "add":
            for text in (add_steps or []):
                if text and text.strip():
                    p.add(text)

        elif action in ("start", "complete"):
            if step_id is None:
                return f"step_id is required for '{action}'.\n\n" + p.render()
            step = p.find(int(step_id))
            if step is None:
                return f"No step {step_id}.\n\n" + p.render()
            step["status"] = "doing" if action == "start" else "done"
            if note:
                step["note"] = note

        elif action == "note":
            if note and note.strip():
                p.notes.append(note.strip())

        else:
            return f"Unknown action '{action}'.\n\n" + p.render()

        p.save()
        return p.render()
