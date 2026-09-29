"""Lifecycle hooks for the agent loop.

Claude Code and Codex both expose a fixed set of lifecycle events that an
operator can attach behaviour to. The point is that the harness itself stays
generic: you register plain callables and the loop fires them at known moments.
This module is the local equivalent, and it generalizes the one hook we already
had — `gate.py` was the Stop hook ("the local form of Claude Code's Stop hook",
in its own words). `stop` now lives here alongside the rest, so there is one
registration surface instead of two.

Events, in firing order within a run:

  session_start     once, before the first model call. Hooks may append messages
                    (e.g. load an AGENTS.md, seed a plan). Message hooks win.
  user_prompt_submit
                    once per user turn, before it reaches the model. A hook may
                    rewrite the prompt (return a string) or leave it (return None).
  pre_tool_use      before each tool executes. A hook may BLOCK the call by
                    returning a reason string; the call is skipped and the reason
                    is handed back to the model as the tool result. This is where
                    a permission policy lives (see permissions.py), but any check
                    can be registered here.
  post_tool_use     after each tool returns. Return a string to APPEND to the
                    tool result (e.g. a linter verdict from verify.py), or None.
  stop              when the model emits a no-tool-call turn (a candidate finish).
                    A hook may block the finish by returning a reason; the loop
                    feeds the reasons back and continues. This is the old
                    FinishGate; it is preserved verbatim in behaviour.
  pre_compact /     around context compaction (compaction.py). Message hooks may
  post_compact      inject state that must survive compaction (plan, writes log).

A hook is any callable matching its event's signature. Registration is a list
edit, so adding a project rule never means touching the loop. All hooks are
opt-in: an empty registry is a bare loop, which is what a sub-agent gets.

Failure policy is intentional and uniform: a hook that raises is caught and
surfaced as a block (for guard events) or dropped with an emitted error (for
advisory events). A misconfigured hook must never take down a session, but it
also must never silently pass a check it was supposed to enforce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Event names. Kept as constants so a typo is an import error, not a dead hook.
SESSION_START = "session_start"
USER_PROMPT_SUBMIT = "user_prompt_submit"
PRE_TOOL_USE = "pre_tool_use"
POST_TOOL_USE = "post_tool_use"
STOP = "stop"
PRE_COMPACT = "pre_compact"
POST_COMPACT = "post_compact"

ALL_EVENTS = (
    SESSION_START,
    USER_PROMPT_SUBMIT,
    PRE_TOOL_USE,
    POST_TOOL_USE,
    STOP,
    PRE_COMPACT,
    POST_COMPACT,
)

# --- hook signatures -------------------------------------------------------
#
# Guard hooks return "" to allow or a human-readable reason to block.
# Advisory hooks return a string to contribute, or None to contribute nothing.
#
#   session_start(messages) -> list[dict] | None      extra messages to seed
#   user_prompt_submit(messages, prompt) -> str | None rewritten prompt
#   pre_tool_use(name, args, messages) -> str          "" allows, else blocks
#   post_tool_use(name, args, result, messages) -> str | None  appended text
#   stop(messages) -> str                              "" allows, else blocks
#   pre_compact(messages) -> str | None                extra text into summary
#   post_compact(messages) -> list[dict] | None        messages to re-inject

GuardHook = Callable[..., str]
MessageHook = Callable[..., Any]


@dataclass
class Hooks:
    """A registry of lifecycle hooks, fired by the agent loop.

    Construct empty for a bare loop, or use `default_hooks()` for the standard
    set. `enabled` is the master switch so an operator can silence every hook
    without unregistering them.
    """

    session_start: list[MessageHook] = field(default_factory=list)
    user_prompt_submit: list[MessageHook] = field(default_factory=list)
    pre_tool_use: list[GuardHook] = field(default_factory=list)
    post_tool_use: list[MessageHook] = field(default_factory=list)
    stop: list[GuardHook] = field(default_factory=list)
    pre_compact: list[MessageHook] = field(default_factory=list)
    post_compact: list[MessageHook] = field(default_factory=list)

    enabled: bool = True
    max_stop_retries: int = 3
    stop_nudge: str = (
        "You are not finished. Address each point above, then give your final "
        "answer. If a point is genuinely already handled, say where."
    )

    def register(self, event: str, hook: Callable[..., Any]) -> None:
        if event not in ALL_EVENTS:
            raise ValueError(f"unknown hook event {event!r}; known: {ALL_EVENTS}")
        getattr(self, event).append(hook)

    # -- firing -----------------------------------------------------------
    #
    # Each `fire_*` returns the aggregate effect for the loop to apply. They
    # never raise: a broken hook degrades to a block (guards) or a no-op
    # (advisories), and the caller is told which happened via the events dict.

    def fire_session_start(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        extra: list[dict[str, Any]] = []
        for hook in self.session_start:
            try:
                produced = hook(messages)
            except Exception:
                continue
            if produced:
                extra.extend(produced if isinstance(produced, list) else [produced])
        return extra

    def fire_user_prompt_submit(
        self, messages: list[dict[str, Any]], prompt: str
    ) -> str:
        if not self.enabled:
            return prompt
        current = prompt
        for hook in self.user_prompt_submit:
            try:
                rewritten = hook(messages, current)
            except Exception:
                continue
            if isinstance(rewritten, str) and rewritten:
                current = rewritten
        return current

    def fire_pre_tool_use(
        self, name: str, args: dict[str, Any], messages: list[dict[str, Any]]
    ) -> str:
        """Return "" to allow, else the combined reason to block."""
        if not self.enabled:
            return ""
        reasons: list[str] = []
        for hook in self.pre_tool_use:
            try:
                reason = hook(name, args, messages) or ""
            except Exception as exc:
                reason = f"pre_tool_use hook raised {exc!r}; blocking to be safe."
            if reason:
                reasons.append(reason)
        return "\n".join(f"- {r}" for r in reasons)

    def fire_post_tool_use(
        self, name: str, args: dict[str, Any], result: str, messages: list[dict[str, Any]]
    ) -> str:
        """Return text to append to the tool result ("" appends nothing)."""
        if not self.enabled:
            return ""
        parts: list[str] = []
        for hook in self.post_tool_use:
            try:
                addition = hook(name, args, result, messages)
            except Exception:
                continue
            if addition:
                parts.append(str(addition))
        return "\n".join(parts)

    def fire_stop(self, messages: list[dict[str, Any]]) -> str:
        """Return "" to allow the finish, else the combined reasons to continue."""
        if not self.enabled:
            return ""
        reasons: list[str] = []
        for hook in self.stop:
            try:
                reason = hook(messages) or ""
            except Exception as exc:
                reason = f"A stop hook raised {exc!r}; treat as not passed."
            if reason:
                reasons.append(reason)
        return "\n".join(f"- {r}" for r in reasons)

    def fire_pre_compact(self, messages: list[dict[str, Any]]) -> str:
        if not self.enabled:
            return ""
        parts: list[str] = []
        for hook in self.pre_compact:
            try:
                text = hook(messages)
            except Exception:
                continue
            if text:
                parts.append(str(text))
        return "\n".join(parts)

    def fire_post_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        extra: list[dict[str, Any]] = []
        for hook in self.post_compact:
            try:
                produced = hook(messages)
            except Exception:
                continue
            if produced:
                extra.extend(produced if isinstance(produced, list) else [produced])
        return extra
