from __future__ import annotations

import base64
import json
import shlex
from pathlib import PurePosixPath
from typing import Any, Callable

from access import AccessPolicy, assert_no_secrets, redact_secrets
from config import AGENT_PLAN_PATH
from excel.knowledge_tools import TOOLS as KNOWLEDGE_TOOLS
from excel.knowledge_tools import handlers as knowledge_handlers
from excel_tools import ExcelBridge
from excel_tools import handlers as excel_handlers
from excel_tools import tools as excel_tool_schemas
from gate import FinishGate
from plan import PLAN_TOOL, Plan, PlanTool
from research import research
from shell_policy import check_command, is_read_command
from subagents import SubAgents
from workspace import DockerWorkspace, WorkspaceProxy

POLICY = AccessPolicy()

# Shell is intentionally constrained. Arbitrary shell access would let the model
# bypass file-content filtering by using cat/python/etc. SAFE_COMMANDS is now
# enforced in shell_policy.check_command, not merely defined.
from shell_policy import SAFE_COMMANDS  # re-exported for callers that imported it here

# Lines returned by read_file when the caller omits `limit`.
DEFAULT_READ_LINES = 500
MAX_READ_LINES = 2000
WHOLE_FILE_LINES = 1200

# Everything a patch may touch lives under this path, relative to /workspace.
PATCH_ROOT = "write"


def _clip(text: str, limit: int, path: str | None = None) -> str:
    """Redact, then truncate. `path` selects the redaction profile by extension.

    A missing/unknown path falls back to the strict profile, so an unfiltered
    caller is never silently relaxed.
    """
    text = redact_secrets(text, path)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} characters]"


def _render_window(body: str, limit: int, first_line: int, total_lines: int,
                   path: str | None = None) -> str:
    """Clip a line-addressed window, reporting position against the FILE."""
    body = redact_secrets(body, path)
    lines = body.splitlines(keepends=True)
    last_shown = first_line + len(lines) - 1 if lines else first_line

    if len(body) > limit:
        kept: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) > limit:
                break
            kept.append(line)
            used += len(line)
        if not kept:
            return (
                f"...[line {first_line} alone exceeds the {limit}-character read "
                f"budget; the file has {total_lines} lines and this window is not "
                f"displayable - request a narrower range]"
            )
        body = "".join(kept)
        last_shown = first_line + len(kept) - 1

    last_shown = max(last_shown, first_line)
    footer = f"[lines {first_line}-{last_shown} of {total_lines} total]"
    if last_shown < total_lines:
        footer += f" [more follows; call read_file with offset={last_shown}]"
    return f"{body}\n{footer}"


def _patch_target(line: str) -> str | None:
    """Extract a normalized, containment-checked path from a diff file header.

    Returns None for /dev/null (file creation/deletion). Raises for any target
    that escapes /workspace/write, including via `..` or `.` segments, so the
    check cannot be defeated by a path that only *looks* prefixed.
    """
    if not line.startswith(("+++ ", "--- ")) or line[4:] == "/dev/null":
        return None
    raw = line[4:].split("\t", 1)[0].strip()
    raw = raw[2:] if raw.startswith(("a/", "b/")) else raw

    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise PermissionError(f"Patch target may not contain '..': {raw}")
    normalized = str(PurePosixPath(*[p for p in parts if p not in (".", "")]))
    if not (normalized == PATCH_ROOT or normalized.startswith(PATCH_ROOT + "/")):
        raise PermissionError("Patch targets must be beneath /workspace/write")
    return normalized


def _code_hint(path: str, glob: str | None = None) -> bool | None:
    """Explicit profile override for a multi-file read, or None to use the path.

    `grep`/`run_command` return output blended across files, so there is no
    single extension to key on. When a `glob` pins the read to one file type we
    honor it; otherwise the caller decides. Returning None defers to the strict
    default rather than guessing, so an unknown mix is never relaxed.
    """
    if glob and "." in glob:
        from access import profile_for
        return profile_for(glob) == "code"
    return None


def definitions() -> list[dict[str, Any]]:
    """Tool schema advertised to the model."""
    return [
        {"type": "function", "function": {"name": "list_files", "description": "List files/directories in an allowed workspace path. Sensitive filenames may be visible but their contents cannot be read.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "max_depth": {"type": "integer", "minimum": 1, "maximum": 8}}, "required": ["path"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "grep", "description": "Search file contents with ripgrep and return matching lines with numbers. PREFER THIS over read_file to locate a symbol, function, or string: it returns the ~20 relevant lines instead of whole files.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Regex to search for"}, "path": {"type": "string", "description": "File or directory to search; defaults to /workspace/write"}, "glob": {"type": "string", "description": "Optional file glob, e.g. '*.py'"}, "ignore_case": {"type": "boolean"}}, "required": ["pattern"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "outline", "description": "Structural outline of a source file (classes, functions, methods with line numbers) without reading the bodies. Use to decide which lines to read. Python is parsed via AST; other languages fall back to a definition-pattern scan.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read a text file from writable or read-only workspace roots. Small files are returned whole. Large files return a line window starting at `offset` (0-based) and report exactly which lines were shown against the file's true length, so paging is reliable. Known secret files are blocked. Source-code files are returned verbatim; other files have embedded secrets redacted.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "description": "0-based first line to return. Default 0."}, "limit": {"type": "integer", "minimum": 0, "description": "Max lines to return. Default 500. Use 0 to request the whole file (still capped by the server)."}}, "required": ["path"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "write_file", "description": "Create or replace a text file. Only /workspace/write is writable. For files larger than a few thousand tokens, write in chunks: create the file with the first chunk, then append further chunks with run_command (e.g. cat >> path <<'EOF'), because a single oversized call can exceed the output token limit and be rejected.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "apply_patch", "description": "Apply a unified diff to a writable root. The patch must target paths beneath /workspace/write.", "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "run_command", "description": "Run a shell command with /workspace/write as the working area. Only allowlisted programs may be invoked (SAFE_COMMANDS); reads of /workspace/read are rejected. Returns JSON {exit_code, stdout, stderr}.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 600}}, "required": ["command"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "research", "description": "Spawn the built-in research sub-agent to find and validate primary-source information on a question. Runs on the host (the sandbox has no network) and returns a concise, source-cited summary. Use this for facts you cannot determine from the workspace alone.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "manage_subagent", "description": "List the declarative sub-agents available to spawn, or spawn one by name on a task. A sub-agent runs in its own loop with its own tool allowlist and returns its own answer. Use action='list' to see names and descriptions, action='spawn' to spawn one.", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "spawn"]}, "name": {"type": "string", "description": "Sub-agent name, required for action='spawn'."}, "task": {"type": "string", "description": "The task to hand the sub-agent, required for action='spawn'."}, "max_iterations": {"type": "integer", "minimum": 1, "description": "Optional override of the sub-agent's iteration budget."}}, "required": ["action"], "additionalProperties": False}}},
    ]


def roots_definitions() -> list[dict[str, Any]]:
    """Schemas for the runtime root-management tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_roots",
                "description": (
                    "Report the directories the sandbox can currently reach: writable "
                    "roots (host paths mounted at /workspace/write/rootN) and read-only "
                    "reference roots (mounted at /workspace/read/rootN)."
                ),
                "parameters": {"type": "object", "properties": {},
                               "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_roots",
                "description": (
                    "Replace the sandbox's root set at runtime. The container is rebuilt "
                    "with the new mounts and the change is written to roots.txt / "
                    "readonly_roots.txt, so it survives a restart. Paths are absolute host "
                    "directories that must already exist. Omit a list to leave it unchanged; "
                    "pass an empty list to clear it. Writable and read-only roots may not "
                    "overlap. The container restart drops in-container state (installed "
                    "packages, /tmp); files in writable roots persist."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "writable": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Absolute host paths. Omit to keep current.",
                        },
                        "readonly": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Absolute host paths. Omit to keep current.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
    ]


def roots_handlers(manager: Any) -> dict[str, Callable[..., str]]:
    """Handlers bound to a RootManager. Kept separate so any caller can reuse them.

    `set_roots` is deliberately permissive about arguments: an omitted list
    (`None`) means "leave alone", which is what a tool call that only adds a
    read-only reference needs.
    """

    def get_roots() -> str:
        cfg = manager.config()
        return json.dumps({
            "writable_roots": [
                {"host_path": r.host_path, "sandbox_path": f"/workspace/write/root{i}"}
                for i, r in enumerate(cfg.writable)
            ],
            "readonly_roots": [
                {"host_path": r.host_path, "sandbox_path": f"/workspace/read/root{i}"}
                for i, r in enumerate(cfg.readonly)
            ],
        }, indent=2)

    def set_roots(writable: list[str] | None = None,
                  readonly: list[str] | None = None) -> str:
        cfg = manager.config()
        new_writable = [r.host_path for r in cfg.writable] if writable is None else writable
        new_readonly = [r.host_path for r in cfg.readonly] if readonly is None else readonly
        try:
            updated = manager.apply(new_writable, new_readonly)
        except Exception as exc:
            return f"set_roots failed: {exc}"
        return "roots updated (container rebuilt)\n" + json.dumps(
            updated.to_json(), indent=2
        )

    return {"get_roots": get_roots, "set_roots": set_roots}


def sandbox_handlers(workspace: DockerWorkspace, max_output: int) -> dict[str, Callable[..., str]]:
    """Handlers for tools that execute inside the Docker sandbox.

    `workspace` may be a real DockerWorkspace or a WorkspaceProxy; both expose
    the same `execute`. Callers pass the proxy so this binding survives a
    container rebuild.

    Every handler that can return file bytes ends in a content check, not just a
    name check: `assert_content_allowed` rejects secret *filenames* before the
    read, and `assert_no_secrets` rejects secret *content* after it. The second
    check is what covers a secret file that was copied to an innocuous name.
    Both content checks are profile-aware: source-code extensions use the
    relaxed "code" profile (no assignment-shape false positives), everything
    else stays strict.
    """

    def list_files(path: str, max_depth: int = 3) -> str:
        POLICY.assert_read(path)
        cmd = f"find {shlex.quote(path)} -maxdepth {int(max_depth)} -mindepth 1 -print | sort"
        r = workspace.execute(cmd)
        return _clip(str(r["stdout"]) + str(r["stderr"]), max_output, path)

    def grep(pattern: str, path: str = "/workspace/write", glob: str | None = None,
             ignore_case: bool = False) -> str:
        POLICY.assert_read(path)
        args = ["rg", "-n", "--no-heading", "--color", "never"]
        if ignore_case:
            args.append("-i")
        if glob:
            args += ["-g", shlex.quote(glob)]
        args += ["--", shlex.quote(pattern), shlex.quote(path)]
        r = workspace.execute(" ".join(args))
        if r["exit_code"] not in (0, 1):  # 1 = no matches, not an error
            raise RuntimeError(str(r["stderr"]) or "rg failed")
        out = str(r["stdout"]) or "(no matches)"
        # Output spans files: honor an explicit code glob, else stay strict.
        assert_no_secrets(out, f"grep of {path}", code=_code_hint(path, glob))
        return _clip(out, max_output, path)

    def outline(path: str) -> str:
        POLICY.assert_read(path)
        POLICY.assert_content_allowed(path)
        script = (
            "import ast,sys,re\n"
            "p=sys.argv[1]\n"
            "src=open(p,encoding='utf-8',errors='replace').read()\n"
            "try:\n"
            "    t=ast.parse(src)\n"
            "except SyntaxError:\n"
            "    for i,l in enumerate(src.splitlines(),1):\n"
            "        if re.match(r'\\s*(def|class|function|func|fn|public|private|impl|struct|enum)\\b',l):\n"
            "            print(f'{i}: {l.strip()[:120]}')\n"
            "    raise SystemExit(0)\n"
            "def w(n,d=0):\n"
            "    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):\n"
            "        k='class' if isinstance(n,ast.ClassDef) else 'def'\n"
            "        print('  '*d+f'{n.lineno}: {k} {n.name}')\n"
            "        d+=1\n"
            "    for c in ast.iter_child_nodes(n):\n"
            "        w(c,d)\n"
            "w(t)\n"
        )
        payload = base64.b64encode(script.encode()).decode()
        cmd = (
            f"printf %s {shlex.quote(payload)} | base64 -d > /tmp/_outline.py && "
            f"python3 /tmp/_outline.py {shlex.quote(path)}"
        )
        r = workspace.execute(cmd)
        if r["exit_code"] != 0:
            raise RuntimeError(str(r["stderr"]))
        out = str(r["stdout"]).strip() or "(no definitions found)"
        assert_no_secrets(out, path)
        return _clip(f"{path}:\n{out}", max_output, path)

    def read_file(path: str, offset: int = 0, limit: int = DEFAULT_READ_LINES) -> str:
        POLICY.assert_read(path)
        POLICY.assert_content_allowed(path)

        quoted = shlex.quote(path)
        total_r = workspace.execute(f"wc -l < {quoted}")
        if total_r["exit_code"] != 0:
            raise RuntimeError(str(total_r["stderr"]))
        try:
            total_lines = int(str(total_r["stdout"]).strip())
        except ValueError:
            total_lines = 0

        start = max(int(offset), 0)
        want = int(limit)
        if want <= 0 or (total_lines and total_lines <= WHOLE_FILE_LINES and start == 0):
            end = start + min(total_lines or MAX_READ_LINES, MAX_READ_LINES)
        else:
            end = start + min(want, MAX_READ_LINES)

        cmd = f"sed -n {start + 1},{end}p -- {quoted}"
        r = workspace.execute(cmd)
        if r["exit_code"] != 0:
            raise RuntimeError(str(r["stderr"]))

        body = str(r["stdout"]).strip("\n")
        assert_no_secrets(body, path)
        return _render_window(body, max_output, start + 1, total_lines, path)

    def write_file(path: str, content: str) -> str:
        POLICY.assert_write(path)
        payload = base64.b64encode(content.encode()).decode()
        parent = path.rsplit("/", 1)[0]
        cmd = f"mkdir -p -- {shlex.quote(parent)} && printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)}"
        r = workspace.execute(cmd)
        if r["exit_code"] != 0:
            raise RuntimeError(str(r["stderr"]))
        return f"ok: wrote {len(content)} characters to {path}"

    def apply_patch(patch: str) -> str:
        files: list[str] = []
        for line in patch.splitlines():
            target = _patch_target(line)
            if target is not None:
                files.append(target)
        payload = base64.b64encode(patch.encode()).decode()
        r = workspace.execute(f"cd /workspace && printf %s {shlex.quote(payload)} | base64 -d | git apply --whitespace=nowarn -")
        if r["exit_code"] != 0:
            raise RuntimeError(str(r["stderr"]))
        return f"ok: applied patch to {', '.join(files) or 'no files'}"

    def run_command(command: str, timeout: int = 120) -> str:
        verdict = check_command(command)
        if verdict:
            raise PermissionError(verdict)
        r = workspace.execute(f"cd /workspace/write && {command}", int(timeout))
        out = json.dumps(r, ensure_ascii=False)
        # check_command is a token-level guard, not a proof: it cannot see
        # content assembled inside python3/node. Redact the result whenever the
        # command has a plausible read path, so this channel is filtered too.
        # No single file to key on, so this stays strict.
        if is_read_command(command):
            assert_no_secrets(str(r.get("stdout", "")), f"run_command: {command}")
            assert_no_secrets(str(r.get("stderr", "")), f"run_command: {command}")
        return _clip(out, max_output)

    return {
        "list_files": list_files,
        "grep": grep,
        "outline": outline,
        "read_file": read_file,
        "write_file": write_file,
        "apply_patch": apply_patch,
        "run_command": run_command,
    }


def knowledge_definitions() -> list[dict[str, Any]]:
    """Schemas for the Excel and research convention files."""
    return KNOWLEDGE_TOOLS


def host_handlers() -> dict[str, Callable[..., str]]:
    """Handlers for tools that run in the trusted host process."""
    return {"research": research, **knowledge_handlers()}


def _manage_subagent_handler(subagents: "SubAgents") -> Callable[..., str]:
    def manage_subagent(action: str, name: str = "", task: str = "",
                        max_iterations: int | None = None) -> str:
        if action == "list":
            return subagents.describe()
        if action != "spawn":
            return f"Unknown action {action!r}; use 'list' or 'spawn'."
        if not name or not task:
            return "action='spawn' requires both 'name' and 'task'."
        return subagents.spawn(name, task, max_iterations)

    return manage_subagent


def build_registry(
    workspace: DockerWorkspace | WorkspaceProxy,
    max_output: int,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    agents_dir: str | None = None,
    root_manager: Any | None = None,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Callable[..., str]],
    FinishGate,
    "SubAgents",
    ExcelBridge,
]:
    """Assemble the tool surface, finish gate, sub-agent runner, and Excel bridge.

    The sub-agent roster is built here (rather than by the caller) because the
    runner needs the finished tool surface to inherit from, and because the
    `manage_subagent` handler needs the runner. Returning it keeps the caller to
    a single call and one source of truth for the registry.

    Sandbox tools move files, host tools reach the network or the on-disk
    convention files, the Excel bridge installs the workbook engine into the
    running container on first use, the plan tool tracks multi-step work,
    manage_subagent spawns declarative sub-agents, and - when a RootManager is
    supplied - get_roots/set_roots let the model inspect and change the mounted
    directories without a restart.

    The gate is returned so the Stop hook can enforce it. The bridge is returned
    because it holds per-container state (its `_loaded` flag) that the caller must
    invalidate after a rebuild: without a handle to it, the "engine already
    installed" flag survives a container swap and the next Excel call skips
    shipping the engine into a container that no longer has it.
    """
    bridge = ExcelBridge(workspace, max_output)
    plan = Plan(path=AGENT_PLAN_PATH)
    plan_tool = PlanTool(plan)

    tool_definitions = (
        definitions()
        + excel_tool_schemas(bridge)
        + knowledge_definitions()
        + [PLAN_TOOL]
    )
    tool_handlers: dict[str, Callable[..., str]] = {
        **sandbox_handlers(workspace, max_output),
        **host_handlers(),
        **excel_handlers(bridge),
        "manage_plan": plan_tool,
    }

    if root_manager is not None:
        tool_definitions += roots_definitions()
        tool_handlers.update(roots_handlers(root_manager))

    subagents = SubAgents(
        tool_definitions, tool_handlers,
        directory=agents_dir, event_callback=event_callback,
    )
    tool_handlers["manage_subagent"] = _manage_subagent_handler(subagents)

    gate = FinishGate()
    return tool_definitions, tool_handlers, gate, subagents, bridge
