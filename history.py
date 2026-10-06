"""History hygiene for the chat-completions API.

The API enforces one invariant on the message list: for every assistant message
carrying `tool_calls`, exactly one `tool` message per call must follow it,
matched by `tool_call_id`, before the next assistant message. Any violation is a
hard 400 that rejects the *entire* request - and because the session history is
the same object reused every turn, one violation poisons every later turn.

Rather than trust each producer (the loop, compaction, a resumed session) to get
the grouping right, the invariant is enforced here, at the single point where a
history leaves the process. `sanitize_history` mutates in place and returns the
number of repairs it made, so a caller can log drift without handling it.

Two defects are covered:

  - a `tool` message with no matching preceding `tool_calls` (an orphan, e.g. a
    compaction cut that split a group) -> dropped,
  - an assistant `tool_calls` entry with no `tool` reply -> answered with a
    placeholder so the call is not silently lost.

Ordering is preserved; only invalid messages are removed or inserted.
"""

from __future__ import annotations

from typing import Any

MISSING_RESULT = (
    "No result was recorded for this tool call (it was not executed, or its "
    "result was dropped during context compaction). Re-issue it if still needed."
)


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for call in message.get("tool_calls") or []:
        cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
        if cid:
            ids.append(cid)
    return ids


def sanitize_history(messages: list[dict[str, Any]]) -> int:
    """Repair the tool-call grouping invariant in place.

    Returns the number of messages dropped or inserted. Safe to call before
    every request; a well-formed history is left untouched.
    """
    repairs = 0

    # Pass 1: drop `tool` messages that answer no preceding tool_calls, and
    # record any tool_calls that go unanswered within their own group.
    cleaned: list[dict[str, Any]] = []
    open_calls: dict[str, int] = {}  # tool_call_id -> index in `cleaned` of owner

    def close_group() -> None:
        nonlocal repairs
        if not open_calls:
            return
        # Answer the unanswered calls so their assistant message stays valid.
        insert_at = len(cleaned)
        filler = [
            {"role": "tool", "tool_call_id": cid, "content": MISSING_RESULT}
            for cid in open_calls
        ]
        cleaned[insert_at:insert_at] = filler
        repairs += len(filler)
        open_calls.clear()

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            close_group()
            cleaned.append(message)
            for cid in _tool_call_ids(message):
                open_calls[cid] = len(cleaned) - 1
        elif role == "tool":
            cid = message.get("tool_call_id")
            if cid in open_calls:
                del open_calls[cid]
                cleaned.append(message)
            else:
                repairs += 1
        else:
            close_group()
            cleaned.append(message)

    close_group()

    if repairs:
        messages[:] = cleaned
    return repairs
