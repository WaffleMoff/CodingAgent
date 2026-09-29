"""Finish gate and hooks for the agent loop.

Claude Code does this with `Stop` hooks: when the agent finishes responding, a
shell command runs; if it exits non-zero (2), the stop is blocked and the output
is fed back to the model, which must keep going. This module is the local
equivalent. It is a HOOK, not a phase machine — the operator defines checks the
agent must pass before its final answer is accepted, and the loop enforces them
generically.

The loop calls `gate.check(messages)` when the model emits a turn with no tool
calls (a candidate finish). If any check fails, the loop appends the reasons as
a user turn and continues, up to `max_finish_retries`; past that the answer is
accepted so a misconfigured gate can never hang a session forever.

Checks are plain callables `(messages) -> str`, returning "" to pass or a
human-readable reason to block. Registering one is a one-liner, so adding a
project rule (tests must pass, a specific file must exist, a plan must be
complete) is a list edit, not a rewrite. Default set lives in `default_checks`;
override with FINISH_CHECKS env or by passing checks into `FinishGate`.
"""

from __future__ import annotations

from typing import Any, Callable

from config import FINISH_RETRIES, FINISH_GATE_ENABLED

Check = Callable[[list[dict[str, Any]]], str]

DEFAULT_NUDGE = (
    "You are not finished. Address each point above, then give your final answer. "
    "If a point is genuinely already handled, say where."
)


class FinishGate:
    """Collection of checks that must pass before a final answer is accepted."""

    def __init__(
        self,
        checks: list[Check] | None = None,
        enabled: bool | None = None,
        max_retries: int | None = None,
        nudge: str | None = None,
    ):
        self.checks = checks if checks is not None else default_checks()
        self._enabled = FINISH_GATE_ENABLED if enabled is None else enabled
        self.max_retries = FINISH_RETRIES if max_retries is None else max_retries
        self.nudge = nudge or DEFAULT_NUDGE

    def enabled(self) -> bool:
        return self._enabled and bool(self.checks)

    def check(self, messages: list[dict[str, Any]]) -> tuple[bool, str]:
        """Return (allowed, reason). reason is "" when allowed."""
        if not self.enabled():
            return True, ""
        reasons: list[str] = []
        for check in self.checks:
            try:
                reason = check(messages) or ""
            except Exception as exc:
                reason = f"A finish check raised {exc!r}; treat as not passed."
            if reason:
                reasons.append(reason)
        if reasons:
            return False, "\n".join(f"- {r}" for r in reasons)
        return True, ""


# ---------------------------------------------------------------------------
# Default checks. These are deliberately conservative and generic: they encode
# the lessons that cost the most (don't stop mid-task, don't hand back a plan
# you never executed, don't answer a research question without citations), not
# domain rules. Domain rules belong in the operator's own registered checks.
# ---------------------------------------------------------------------------

_PLAN_TOOL_NAME = "manage_plan"


def check_no_open_plan_items(messages: list[dict[str, Any]]) -> str:
    """Block finishing while a plan exists with unfinished steps.

    Reads the plan state from the last tool result of the plan tool. If the agent
    never used the plan tool, this check is silent — planning is optional.
    """
    latest = None
    for m in reversed(messages):
        if m.get("role") == "tool" and m.get("name") == _PLAN_TOOL_NAME:
            latest = m.get("content")
            break
    if not isinstance(latest, str):
        return ""
    if "CAN FINISH: no" in latest:
        return (
            "Your plan still has open steps (see CAN FINISH: no in the last "
            "manage_plan result). Complete them or mark them not-applicable "
            "before finishing."
        )
    return ""


def check_research_citations(messages: list[dict[str, Any]]) -> str:
    """If the turn produced research output, require a source citation in it.

    Only fires when a `research` tool result is among the most recent messages,
    so a pure coding turn is never asked for citations.
    """
    recent = messages[-8:]
    researched = any(
        m.get("role") == "tool" and m.get("name") == "research" for m in recent
    )
    if not researched:
        return ""
    last_assistant = next(
        (m for m in reversed(messages) if m.get("role") == "assistant"
         and isinstance(m.get("content"), str) and m["content"].strip()),
        None,
    )
    if last_assistant is None:
        return ""
    import re
    if re.search(r"\[source:", last_assistant["content"], re.IGNORECASE) or re.search(
        r"(https?://|as-of:)", last_assistant["content"], re.IGNORECASE
    ):
        return ""
    return (
        "You used the research tool but your answer carries no source citation. "
        "Cite where each load-bearing fact came from, or state explicitly that it "
        "could not be confirmed."
    )


def default_checks() -> list[Check]:
    return [check_no_open_plan_items, check_research_citations]
