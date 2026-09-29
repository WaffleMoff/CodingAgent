"""Permission policy: the pre_tool_use hook that decides what the agent may do.

Two modes, chosen by PERMISSION_MODE:

  auto   (default) allow everything except the hard denials below.
  ask             any tool whose effect is not provably safe is routed to a
                  human approval callback. With no callback attached, "ask"
                  degrades to "deny with a reason", never to silent allow.

Hard denials apply in every mode and are not overridable by the model:
  - run_command must pass shell_policy.check_command (program allowlist + path).
  - writes must resolve beneath the writable root.
  - reads of blocked basenames/suffixes are denied by access.py already; this
    layer does not duplicate that, it sits in front of it.

The policy is a plain callable so it drops straight onto Hooks.pre_tool_use:

    hooks.register("pre_tool_use", PermissionPolicy(approver=my_ask).check)

Returning "" allows the call; returning a reason blocks it and the reason is
handed back to the model as the tool result. That is the whole contract.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from shell_policy import check_command

# Tools that only read. Under "ask", these are allowed without a human in the
# loop because they cannot change state or leak a secret (access.py filters
# content on the way out).
READ_ONLY_TOOLS = {
    "read_file", "list_files", "grep", "outline",
    "read_excel_guide", "read_research_guide", "read_research_notes",
}

# Tools that always require approval under "ask".
MUTATING_TOOLS = {"write_file", "apply_patch", "run_command"}

WRITE_ROOT = "/workspace/write"
READ_ROOT = "/workspace/read"

Approver = Callable[[str, dict[str, Any]], bool]


class PermissionPolicy:
    def __init__(self, mode: str | None = None, approver: Approver | None = None):
        self.mode = (mode or os.getenv("PERMISSION_MODE", "auto")).strip().lower()
        self.approver = approver

    # Called by the hook bus: (name, args, messages) -> reason-or-""
    def check(self, name: str, args: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        hard = self._hard_deny(name, args)
        if hard:
            return hard
        if self.mode == "auto":
            return ""
        if name in READ_ONLY_TOOLS:
            return ""
        if name in MUTATING_TOOLS:
            return self._ask(name, args)
        # Unknown tool: be conservative under "ask".
        return self._ask(name, args)

    # -- internals ----------------------------------------------------------

    def _hard_deny(self, name: str, args: dict[str, Any]) -> str:
        if name == "run_command":
            return check_command(str(args.get("command", "")))
        if name in {"write_file", "apply_patch"}:
            path = args.get("path")
            if isinstance(path, str) and path and not _under(path, WRITE_ROOT):
                return (
                    f"{name} may only target {WRITE_ROOT}. '{path}' is outside it. "
                    f"All finished files belong in {WRITE_ROOT}."
                )
        return ""

    def _ask(self, name: str, args: dict[str, Any]) -> str:
        if self.approver is None:
            return (
                f"{name} needs approval and no approval channel is attached "
                f"(PERMISSION_MODE=ask). Propose the change and wait, or set "
                f"PERMISSION_MODE=auto."
            )
        try:
            granted = bool(self.approver(name, args))
        except Exception as exc:
            return f"Approval callback errored ({exc!r}); denying to be safe."
        return "" if granted else f"{name} was denied by the approval channel."


def _under(child: str, parent: str) -> bool:
    child = os.path.normpath(child)
    parent = os.path.normpath(parent)
    return child == parent or child.startswith(parent + os.sep)
