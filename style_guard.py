"""Style re-assertion for long agentic runs.

The system prompt states the tone once, at position 0. Between that statement and the
final answer sit tens of tool results full of dense tabular output, and the model drifts
toward their register. Re-appending the block as a system message immediately before
each completion puts it in the last position the model reads before it writes, and
stripping it immediately after keeps it from accumulating across iterations.

The block is the SYSTEM_PROMPT text verbatim, imported rather than duplicated, so the
two can never drift: editing prompts.py changes both the opening statement and the
per-completion re-assertion.

Injection is applied around every model call in agent.py, so it fires on direct-answer
turns and tool-using turns alike. Controlled by STYLE_GUARD_MODE:

  on   (default) inject the block before each completion, strip it after.
  off            no injection; the system prompt alone governs tone.
"""

from __future__ import annotations

import os
from typing import Any

from prompts import SYSTEM_PROMPT

SEPARATOR = "\n\n---\n"


def mode() -> str:
    return os.getenv("STYLE_GUARD_MODE", "on").strip().lower()


def style_block() -> str:
    """The block text, or empty when the guard is switched off."""
    if mode() == "off":
        return ""
    return SYSTEM_PROMPT.strip()


def guard_message() -> dict[str, Any] | None:
    """The trailing system message injected before each completion, or None."""
    block = style_block()
    if not block:
        return None
    return {"role": "system", "content": block}


def apply_to_user_message(content: str) -> str:
    """Attach the block to the user's turn.

    Retained for callers that persist a combined user turn and want the tone stated
    inline. The per-completion injection in agent.py is the mechanism that actually
    holds; this is available but off the default path.
    """
    block = style_block()
    if not block or mode() != "turn":
        return content
    return f"{content}{SEPARATOR}{block}"


def strip_guards(messages: list[dict[str, Any]]) -> None:
    """Remove injected blocks so they never accumulate in stored history.

    Guards are stripped in two places: before injection at the top of each loop
    iteration (defensive, in case a caller left one behind) and immediately after
    each completion. Without stripping, a 40-iteration run would stack 40 copies of
    the block into the context and into whatever the session persists.
    """
    block = style_block()
    if not block:
        return
    suffix = f"{SEPARATOR}{block}"
    kept: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system" and message.get("content") == block:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.endswith(suffix):
            message = {**message, "content": content[: -len(suffix)]}
        kept.append(message)
    messages[:] = kept
