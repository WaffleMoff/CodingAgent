"""Context compaction with session-scoped state fidelity.

Compaction replaces the middle of the history with a summary. The failure mode
is not the summary being wrong — it is the agent forgetting state that lived
only in the dropped turns: what it was doing (the plan), what it had already
written, where the repo stood, and which files it had been touching. A modern
agent re-injects that state from disk after compaction, because disk is the
source of truth and survives any context loss.

The state that gets re-injected MUST belong to the session being compacted. A
global plan file re-injected into a different session is how an agent came to
believe it was working on an unrelated task (a stale plan from a prior session,
read verbatim into a fresh one). So `fidelity_messages` is bound to a
`session_id` and every source it reads is filtered by that id:

  - the session's own plan file (.agent/plans/<session_id>.json),
  - the writes log entries tagged with this session,
  - the most recently modified files, which are only names — safe to show, but
    shown as a hint rather than as the session's own work.

Re-injection is best-effort: any source that is missing or errors is skipped, so
compaction can never fail *because* state could not be reloaded. That keeps the
core (summarize) independent of the fidelity add-ons.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from config import (
    COMPACT_AT_TOKENS,
    COMPACT_ENABLED,
    COMPACT_KEEP_RECENT,
    COMPACT_MAX_SUMMARY_TOKENS,
)
from plan import plan_path

SUMMARY_PREFIX = "[SUMMARY OF EARLIER CONVERSATION]"
FIDELITY_PREFIX = "[STATE RE-INJECTED AFTER COMPACTION]"

WRITE_ROOT = Path(os.getenv("AGENT_WRITE_ROOT", "/workspace/write"))

# Running log of files written this session; appended by the post_tool_use hook
# in hooks_wiring.py, read back here. Lines are prefixed with the session id so
# a later session only sees its own writes.
WRITES_LOG = Path(os.getenv("AGENT_WRITES_LOG", "/tmp/agent_writes.log"))

_SUMMARY_INSTRUCTION = (
    "Summarize the conversation so far for another agent that will continue the "
    "work. Preserve: what was asked, decisions made and why, files changed and "
    "their current state, commands run and their results, what is done, what "
    "remains, and any constraints. Drop pleasantries. Be specific and factual. "
    "Do not invent anything that did not happen."
)


def enabled() -> bool:
    return COMPACT_ENABLED


def _rendered_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content:
            total += len(str(content))
        for call in m.get("tool_calls") or []:
            total += len(str(call))
    return total // 4


def needs_compaction(messages: list[dict[str, Any]]) -> bool:
    if not enabled():
        return False
    return len(messages) > COMPACT_KEEP_RECENT + 1 and _rendered_tokens(messages) > COMPACT_AT_TOKENS


def _summarize(llm: Any, block: list[dict[str, Any]]) -> str | None:
    convo = []
    for m in block:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            convo.append(f"{role}: {content}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            convo.append(f"{role} called tool {fn.get('name')}({fn.get('arguments')})")
    if not convo:
        return None
    try:
        prompt = [
            {"role": "system", "content": _SUMMARY_INSTRUCTION},
            {"role": "user", "content": "\n\n".join(convo)},
        ]
        completion = llm.complete(prompt, [])
        text = (getattr(completion, "content", None) or "").strip()
        if text:
            if len(text) // 4 > COMPACT_MAX_SUMMARY_TOKENS:
                text = text[: COMPACT_MAX_SUMMARY_TOKENS * 4]
            return text
    except Exception:
        return None
    return None


# --- fidelity: re-inject durable state from disk --------------------------

def _plan_state(session_id: str) -> str:
    try:
        raw = Path(plan_path(session_id)).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return ""
    # Defensive: a plan written by another session must never be re-injected.
    if str(data.get("session_id") or "") != session_id:
        return ""
    task = str(data.get("task") or "").strip()
    steps = data.get("steps") or []
    notes = data.get("notes") or []
    if not task and not steps and not notes:
        return ""
    lines = ["PLAN:"]
    if task:
        lines.append(f"  task: {task}")
    mark = {"todo": " ", "doing": "~", "done": "x"}
    for s in steps:
        lines.append(f"  [{mark.get(s.get('status', ' '), ' ')}] {s.get('id')}. {s.get('text')}")
    for n in notes[-5:]:
        lines.append(f"  note: {n}")
    return "\n".join(lines)


def _writes_log(session_id: str, limit: int = 20) -> str:
    try:
        lines = WRITES_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    own = [l for l in lines if l.strip() and l.split(" ", 1)[0] == session_id]
    recent = own[-limit:]
    if not recent:
        return ""
    return "FILES WRITTEN THIS SESSION (most recent last):\n" + "\n".join(f"  {l}" for l in recent)


def _git_status() -> str:
    try:
        if not (WRITE_ROOT / ".git").exists():
            return ""
        import subprocess
        out = subprocess.run(
            ["git", "status", "--short"], cwd=str(WRITE_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        text = (out.stdout or "").strip()
        if not text:
            return "GIT STATUS: clean"
        return "GIT STATUS (working tree):\n" + "\n".join(f"  {l}" for l in text.splitlines()[:30])
    except Exception:
        return ""


def _recent_files(limit: int = 12) -> str:
    try:
        if not WRITE_ROOT.exists():
            return ""
        entries = []
        for p in WRITE_ROOT.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                try:
                    entries.append((p.stat().st_mtime, p))
                except Exception:
                    continue
        entries.sort(reverse=True)
        recent = entries[:limit]
        if not recent:
            return ""
        lines = ["RECENTLY MODIFIED FILES (any session):"]
        for mtime, p in recent:
            rel = p.relative_to(WRITE_ROOT)
            lines.append(f"  {rel}  ({time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))})")
        return "\n".join(lines)
    except Exception:
        return ""


def make_fidelity_hook(session_id: str):
    """Return a post_compact hook bound to `session_id`.

    The hook renders only this session's plan and writes; the recent-files list
    is labelled as workspace-wide so it is not mistaken for the session's work.
    """
    def fidelity_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts = [
            p for p in (
                _plan_state(session_id),
                _writes_log(session_id),
                _git_status(),
                _recent_files(),
            ) if p
        ]
        if not parts:
            return []
        body = f"{FIDELITY_PREFIX}\nSESSION: {session_id}\n" + "\n\n".join(parts)
        return [{"role": "system", "content": body}]

    return fidelity_messages


def fidelity_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Default post_compact hook for callers with no session id.

    Falls back to the 'default' session so nothing is re-injected from another
    session's plan. Callers that own a session should use `make_fidelity_hook`.
    """
    return make_fidelity_hook("default")(messages)


# --- compaction -----------------------------------------------------------

def _safe_split(messages: list[dict[str, Any]], tail_len: int) -> int:
    """Return the index at which the kept tail may start.

    A `tool` message is only valid immediately after the assistant message that
    issued its `tool_calls`. Slicing by a fixed count can land the tail on a
    bare `tool` message, orphaning it and making every later request fail with
    "Messages with role 'tool' must be a response to a preceding message with
    'tool_calls'". So walk the split point backwards: if the message at the
    boundary is a `tool` reply, move the boundary up to its assistant parent so
    the whole tool exchange is kept together in the tail.
    """
    split = max(1, len(messages) - tail_len)
    while split > 1 and messages[split].get("role") == "tool":
        split -= 1
    return split


def compact(messages: list[dict[str, Any]], llm: Any, hooks: Any | None = None) -> bool:
    """Compact `messages` in place. Returns True if anything was collapsed.

    Fires pre_compact (extra text folded into the summary prompt) and
    post_compact (messages re-injected after the summary) when `hooks` is given.
    """
    if not enabled() or len(messages) <= COMPACT_KEEP_RECENT + 1:
        return False

    head = messages[0]
    split = _safe_split(messages, COMPACT_KEEP_RECENT)
    tail = messages[split:]
    middle = messages[1:split]
    if not middle:
        return False

    extra = ""
    if hooks is not None:
        extra = hooks.fire_pre_compact(messages)

    summary = _summarize(llm, middle)
    if summary is None:
        marker = (
            f"{SUMMARY_PREFIX}\n(Summary unavailable: older turns were dropped "
            "without being summarized. Do not assume the earlier work did not "
            "happen; re-check files and results if a fact is needed.)"
        )
    else:
        marker = f"{SUMMARY_PREFIX}\n{summary}"
    if extra:
        marker = marker.rstrip() + f"\n\n{extra.strip()}"

    rebuilt: list[dict[str, Any]] = [head, {"role": "system", "content": marker}]
    if hooks is not None:
        rebuilt.extend(hooks.fire_post_compact(messages))
    rebuilt.extend(tail)

    messages[:] = rebuilt
    return True


def maybe_compact(messages: list[dict[str, Any]], llm: Any, hooks: Any | None = None) -> bool:
    """Called before each model call. Compacts only when the threshold is crossed."""
    if needs_compaction(messages):
        return compact(messages, llm, hooks=hooks)
    return False
