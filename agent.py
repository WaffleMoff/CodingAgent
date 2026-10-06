from __future__ import annotations

import json
import threading
from typing import Any, Callable

from llm import LLM
from prompts import SYSTEM_PROMPT
from style_guard import apply_to_user_message, guard_message, strip_guards

EventCallback = Callable[[str, dict[str, Any]], None]

# Returned to the model when its tool-call JSON is cut off by the output cap.
TRUNCATION_HINT = (
    "Your tool call was cut off before the arguments finished (the response hit "
    "the output token limit). The call was not executed. Do not retry the same "
    "call unchanged. Instead: for a large file, write it in several smaller "
    "write_file calls (create the file with the first chunk, then append with "
    "run_command or successive writes), or use apply_patch for a targeted edit."
)

BLOCKED_HINT = (
    "The call was blocked before it ran. Do not retry it unchanged; either adjust "
    "the arguments to satisfy the stated reason, or take a different approach."
)

# Appended as the tool result for a call the loop aborted before running, so the
# history keeps one tool message per tool_call and stays a valid request.
ABORTED_RESULT = (
    "The run was interrupted before this tool call was executed. It was not run. "
    "Re-issue it (or a corrected form) if it is still needed."
)


def _legacy_hooks(gate: Any, compact: bool) -> Any:
    """Adapt the old `gate` + `compact` arguments onto the Hooks registry.

    `gate` was the Stop hook and `compact` was an on/off flag. Both are folded
    into Hooks so callers written against the previous signature keep working
    while the loop only knows about one mechanism.
    """
    from hooks import Hooks

    hooks = Hooks()
    if gate is not None:
        hooks.max_stop_retries = getattr(gate, "max_retries", 3)
        hooks.stop_nudge = getattr(gate, "nudge", hooks.stop_nudge)
        if hasattr(gate, "check"):
            def _stop(messages: list[dict[str, Any]]) -> str:
                allowed, reason = gate.check(messages)
                return "" if allowed else reason
            hooks.register("stop", _stop)
        elif callable(gate):
            hooks.register("stop", gate)
    return hooks


def _drain_tool_calls(
    remaining: list[Any],
    messages: list[dict[str, Any]],
    emit: Callable[[str, dict[str, Any]], None],
) -> None:
    """Answer every not-yet-executed tool call with a placeholder tool message.

    The chat API requires one `tool` message per entry in a preceding assistant
    message's `tool_calls`. When the loop stops early (cancellation, or an
    exception escaping the per-call handler) the assistant message is already in
    `messages`, so the remaining calls must still be answered or the whole
    history becomes an invalid request and every later turn is rejected with
    "Messages with role 'tool' must be a response to a preceding message with
    'tool_calls'". This closes that gap.
    """
    for call in remaining:
        emit("tool_result", {"name": call.function.name, "result": ABORTED_RESULT})
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": ABORTED_RESULT,
        })


def run_agent(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_iterations: int,
    handlers: dict[str, Callable[..., str]] | None = None,
    event_callback: EventCallback | None = None,
    cancel_event: threading.Event | None = None,
    llm: LLM | None = None,
    gate: Any | None = None,
    compact: bool = False,
    hooks: Any | None = None,
) -> str:
    """Generic reasoning loop shared by the main agent and every sub-agent.

    `hooks` fires at known lifecycle points (see hooks.py): session_start once,
    pre/post_tool_use around every call, stop when the model tries to finish, and
    pre/post_compact around context compaction. An empty registry is a bare ReAct
    loop, which is what a sub-agent gets by default.

    `gate` is accepted for backward compatibility and adapted onto the Stop hook
    of `hooks`; if both are given, `hooks` wins and `gate` is ignored.

    Style re-assertion is applied around every completion via `complete`, so it
    fires on direct-answer turns as well as tool-using ones.
    """
    handlers = handlers or {}
    llm = llm or LLM()
    if hooks is None:
        hooks = _legacy_hooks(gate, compact)

    def emit(event_type: str, data: dict[str, Any]) -> None:
        if event_callback:
            event_callback(event_type, data)

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def complete() -> Any:
        """Single choke point: compact if due, inject the guard, call, strip."""
        if compact:
            from compaction import maybe_compact
            try:
                if maybe_compact(messages, llm, hooks=hooks):
                    emit("compaction", {"messages": len(messages)})
            except Exception as exc:
                emit("error", {"message": f"Compaction failed: {exc}"})
        strip_guards(messages)
        guard = guard_message()
        if guard is not None:
            messages.append(guard)
        try:
            return llm.complete(messages, tools)
        finally:
            strip_guards(messages)

    # session_start: seed the history once, before the first model call.
    for seeded in hooks.fire_session_start(messages):
        messages.append(seeded)

    stop_retries = 0

    for _ in range(max_iterations):
        if cancelled():
            emit("error", {"message": "Agent cancelled"})
            return "Agent cancelled."

        response = complete()
        truncated = getattr(response, "truncated", False)

        assistant: dict[str, Any] = {"role": "assistant", "content": response.content}
        if response.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in response.tool_calls
            ]
        messages.append(assistant)

        emit("assistant", {"content": response.content})
        if truncated:
            emit("error", {"message": "Completion hit the output token limit; strategy change required"})

        if not response.tool_calls:
            reason = hooks.fire_stop(messages)
            if reason and stop_retries < hooks.max_stop_retries:
                stop_retries += 1
                emit("gate", {"reason": reason, "attempt": stop_retries})
                messages.append({"role": "user", "content": (
                    (reason or "You are not finished.") + "\n\n" + (hooks.stop_nudge or "")
                )})
                continue
            if reason:
                emit("error", {"message": f"Stop hook not passed after "
                                           f"{stop_retries} retries: {reason}"})
            emit("final", {"content": response.content or ""})
            return response.content or ""

        # Execute the calls in order. Every exit from this block must leave one
        # `tool` message per call in the assistant message, or the history is
        # invalid for the next request; that is what the `finally` guarantees.
        aborted = False
        try:
            for index, call in enumerate(response.tool_calls):
                if cancelled():
                    emit("error", {"message": "Agent cancelled"})
                    aborted = True
                    return "Agent cancelled."

                name = call.function.name
                raw_args = call.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    args = {}
                    if truncated:
                        result = f"{TRUNCATION_HINT}\n\n(json error: {exc})"
                    else:
                        result = (
                            f"Invalid JSON tool arguments: {exc}. The call was not "
                            "executed. Re-send the call with well-formed JSON, and "
                            "split large payloads across multiple calls."
                        )
                    emit("tool_call", {"name": name, "arguments": args})
                else:
                    emit("tool_call", {"name": name, "arguments": args})

                    blocked = hooks.fire_pre_tool_use(name, args, messages)
                    if blocked:
                        emit("hook_block", {"name": name, "reason": blocked})
                        result = f"{blocked}\n\n{BLOCKED_HINT}"
                    else:
                        handler = handlers.get(name)
                        try:
                            result = handler(**args) if handler else f"Unknown tool: {name}"
                        except Exception as exc:
                            result = f"Tool error: {exc}"
                        addition = hooks.fire_post_tool_use(name, args, result, messages)
                        if addition:
                            result = f"{result}\n\n{addition}"

                emit("tool_result", {"name": name, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
                answered = index + 1
        finally:
            # On an early exit (cancellation here, or an uncaught exception from
            # a hook/handler) the assistant's tool_calls are only partly
            # answered. Answer the remainder with a placeholder so the next
            # request stays valid.
            answered = locals().get("answered", 0)
            if answered < len(response.tool_calls):
                _drain_tool_calls(response.tool_calls[answered:], messages, emit)

        if aborted:
            return "Agent cancelled."

    emit("error", {"message": f"Agent exceeded {max_iterations} iterations"})
    return "Agent exceeded maximum iterations."


class Agent:
    """Stateful session wrapper around `run_agent` for the chat UI.

    Holds the running message history and the tool registry for one session.
    `hooks` carries the harness-level features (permissions, verification,
    compaction fidelity, sub-agents); an empty Hooks is a plain chat loop.
    """

    def __init__(
        self,
        tools: list[dict[str, Any]],
        handlers: dict[str, Callable[..., str]],
        callback: EventCallback,
        max_iterations: int,
        system_prompt: str = SYSTEM_PROMPT,
        gate: Any | None = None,
        compact: bool = False,
        hooks: Any | None = None,
    ):
        self.tools = tools
        self.handlers = handlers
        self.callback = callback
        self.max_iterations = max_iterations
        self.llm = LLM()
        self.hooks = hooks if hooks is not None else _legacy_hooks(gate, compact)
        self.compact = compact
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self._cancelled = threading.Event()
        self._run_lock = threading.Lock()

    def interrupt(self) -> None:
        self._cancelled.set()

    def run(self, user_message: str) -> str:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Agent is already running")
        try:
            self._cancelled.clear()
            strip_guards(self.messages)
            submitted = self.hooks.fire_user_prompt_submit(self.messages, user_message)
            self.messages.append(
                {"role": "user", "content": apply_to_user_message(submitted)}
            )
            return run_agent(
                self.messages,
                self.tools,
                self.max_iterations,
                self.handlers,
                event_callback=self.callback,
                cancel_event=self._cancelled,
                llm=self.llm,
                hooks=self.hooks,
                compact=self.compact,
            )
        finally:
            self._run_lock.release()
