"""The standard hook set, assembled in one place.

`default_hooks()` is the single registration surface the app calls. It binds the
harness features onto the hook bus so that `app.py` never has to know which
module implements which event:

  session_start  excel guidance            tool routing     (change 9)
  post_tool_use  verify.make_verifier()    linter loop      (change 7)
  post_tool_use  append_writes_log()       writes log       (change 5)
  pre_tool_use   PermissionPolicy.check    permissions      (change 8)
  post_compact   session fidelity hook     state re-inject  (change 5)
  post_compact   excel guidance            tool routing     (change 9)
  stop           LegacyStopAdapter         finish gate      (existing)

Order within `post_tool_use` matters: the write must be logged before it is
verified, so the log records the intent even when the check fails. Registration
order is preserved by the bus, so the sequence below is the contract.

State re-injection is SESSION-SCOPED: `default_hooks(session_id=...)` registers
`compaction.make_fidelity_hook(session_id)`, so a session only ever re-reads its
own plan and its own writes. The writes log lines are tagged with the session id
so a later session cannot see an earlier session's writes.

Excel guidance is injected as a system message at `session_start` (seeded once,
before the first model call) and re-seeded at `post_compact` so it survives
context compaction. This is how the agent learns to use the `excel_*` tools
without a static-prompt edit.

Everything is a plain callable, so a caller that wants a different set builds its
own Hooks and registers what it needs; nothing here is required by the loop.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from hooks import Hooks
from compaction import make_fidelity_hook
from excel_guidance import make_excel_guidance_hook
from permissions import PermissionPolicy
from verify import make_verifier

WRITES_LOG = Path(os.getenv("AGENT_WRITES_LOG", "/tmp/agent_writes.log"))
WRITE_ROOT = os.getenv("AGENT_WRITE_ROOT", "/workspace/write")


def append_writes_log(
    log_path: Path = WRITES_LOG,
    session_id: str = "default",
) -> Any:
    """post_tool_use hook: record every successful file mutation.

    Each line is prefixed with the session id. Compaction reads this back and
    keeps only the current session's lines, so "what have I already written"
    survives compaction without leaking one session's writes into another.
    Best-effort: a failure to log must never break the tool call, so it is
    swallowed.
    """

    def hook(name: str, args: dict[str, Any], result: str,
             messages: list[dict[str, Any]]) -> str | None:
        if name not in {"write_file", "apply_patch"}:
            return None
        if isinstance(result, str) and result.lower().startswith("tool error"):
            return None
        try:
            if name == "write_file":
                target = str(args.get("path", "?"))
            else:
                targets = _patch_targets(result)
                if not targets:
                    return None
                target = ", ".join(targets)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{session_id} {time.strftime('%H:%M:%S')} {name}: {target}\n")
        except Exception:
            return None
        return None

    return hook


def _patch_targets(result: str) -> list[str]:
    for line in result.splitlines():
        if line.startswith("ok: applied patch to "):
            return [p.strip() for p in line[len("ok: applied patch to "):].split(",")
                    if p.strip() and p.strip() != "no files"]
    return []


class LegacyStopAdapter:
    """Adapts a `gate.FinishGate` onto the `stop` hook signature.

    The gate's contract is `check(messages) -> (allowed, reason)`; the hook
    contract is `stop(messages) -> reason-or-""`. Keeping the adapter here means
    the loop only ever sees hooks.
    """

    def __init__(self, gate: Any):
        self.gate = gate

    def __call__(self, messages: list[dict[str, Any]]) -> str:
        try:
            allowed, reason = self.gate.check(messages)
        except Exception as exc:
            return f"Finish gate raised {exc!r}; treat as not passed."
        return "" if allowed else (reason or "Finish gate not passed.")


def default_hooks(
    gate: Any | None = None,
    permission_mode: str | None = None,
    approver: Any | None = None,
    verify: bool | None = None,
    write_root: str = WRITE_ROOT,
    writes_log: Path = WRITES_LOG,
    session_id: str = "default",
) -> Hooks:
    """Build the standard hook set for a session.

    `gate` is the legacy FinishGate; pass None to disable the finish check.
    `permission_mode`/`approver` configure change 8. `verify=False` disables the
    linter loop for a session without unregistering it. `session_id` scopes state
    re-injection and the writes log so nothing leaks across sessions.
    """
    hooks = Hooks()

    # Excel tool-routing guidance: seeded once at session start, re-seeded after
    # compaction so it stays in context for the whole session.
    excel_guidance = make_excel_guidance_hook()
    hooks.register("session_start", excel_guidance)
    hooks.register("post_compact", excel_guidance)

    policy = PermissionPolicy(mode=permission_mode, approver=approver)
    hooks.register("pre_tool_use", policy.check)

    logger = append_writes_log(writes_log, session_id=session_id)
    hooks.register("post_tool_use", logger)
    hooks.register("post_tool_use", make_verifier(write_root=write_root, enabled=verify))

    hooks.register("post_compact", make_fidelity_hook(session_id))

    if gate is not None:
        hooks.register("stop", LegacyStopAdapter(gate))

    return hooks
