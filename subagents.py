"""Declarative sub-agents: Markdown files + YAML frontmatter, discoverable at runtime.

A sub-agent is a small agent with its own system prompt, its own tool allowlist,
and its own iteration budget, spawned by the main agent (or by another
sub-agent) to do one bounded job. The point of making them declarative is that
adding one never means touching Python: drop a `.md` file in the agents
directory and it is discoverable and spawnable on the next turn.

This is the pattern Claude Code uses for its `agents/*.md` files. The frontmatter
is deliberately small and mirrors it:

    ---
    name: researcher
    description: Find and validate primary-source facts on a question.
    tools: [research, read_file, grep]     # omit to inherit all main-agent tools
    model: deepseek-chat                    # optional; defaults to the app model
    max_iterations: 24
    ---
    You are a research sub-agent. ...

Discovery order (later wins): the package `agents/` directory next to this file,
then `AGENTS_DIR` (env) or `<app>/agents`. A malformed file is skipped and
reported, never fatal, so one bad file cannot take down the roster.

Three invocation paths, all hitting the same `run_agent` seam:
  - `spawn(name, task)` in Python (used by tools.py's `manage_subagent` handler).
  - `manage_subagent` tool, so the model can spawn one directly by name.
  - the legacy `research` tool, which is just the built-in `researcher` agent.

A sub-agent gets a bare Hooks() unless `subagent_hooks` is passed, so a spawned
agent does not inherit the parent's Stop gate or permission set — it enforces its
own contract (see `research.py` for the canonical example of a sub-agent that
verifies its own output).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

AGENTS_ENV = "AGENTS_DIR"

# Frontmatter keys that carry meaning. Anything else is ignored, not an error,
# so a file can carry operator notes without breaking the loader.
_KNOWN_KEYS = {"name", "description", "tools", "model", "max_iterations", "system_prompt"}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class SubAgent:
    """One declarative sub-agent, fully resolved and ready to spawn."""

    name: str
    description: str
    system_prompt: str
    tools: list[str] | None = None          # None = inherit every main-agent tool
    model: str | None = None
    max_iterations: int = 24
    source: str | None = None               # path it was loaded from, for errors

    def allows(self, tool_name: str) -> bool:
        return self.tools is None or tool_name in self.tools


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal YAML frontmatter parser.

    Deliberately hand-rolled: the schema is flat scalars and one inline list, and
    depending on PyYAML for that would be the only third-party import in the
    loader. Supports `key: value`, `key: [a, b]`, and `key: a, b`.
    """
    match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not match:
        return {}, text
    raw, body = match.group(1), match.group(2)
    data: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
            data[key] = [v for v in items if v]
        else:
            data[key] = value.strip().strip("'\"")
    return data, body


def parse_subagent(text: str, source: str | None = None) -> SubAgent:
    """Parse one Markdown sub-agent definition. Raises ValueError if unusable."""
    data, body = _parse_frontmatter(text)
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError(f"{source or 'sub-agent'}: frontmatter is missing 'name'")

    raw_tools = data.get("tools")
    if raw_tools in (None, "", []):
        tools = None
    elif isinstance(raw_tools, list):
        tools = list(raw_tools)
    else:
        tools = [t.strip() for t in str(raw_tools).split(",") if t.strip()]

    max_iter = data.get("max_iterations")
    try:
        max_iterations = int(max_iter) if max_iter not in (None, "") else 24
    except (TypeError, ValueError):
        max_iterations = 24

    system_prompt = str(data.get("system_prompt") or "").strip() or body.strip()
    if not system_prompt:
        raise ValueError(f"{source or name}: sub-agent has no system prompt body")

    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        # Not fatal, but surface it: a typo like `tool:` silently inherits all.
        pass

    return SubAgent(
        name=name,
        description=str(data.get("description") or "").strip(),
        system_prompt=system_prompt,
        tools=tools,
        model=str(data.get("model") or "").strip() or None,
        max_iterations=max_iterations,
        source=source,
    )


def _search_dirs() -> list[Path]:
    dirs: list[Path] = [Path(__file__).resolve().parent / "agents"]
    env = os.getenv(AGENTS_ENV)
    if env:
        dirs.append(Path(env))
    else:
        try:
            from config import APP_DIR  # local import: subagents.py is importable standalone
            dirs.append(Path(APP_DIR) / "agents")
        except Exception:
            pass
    seen: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def discover(directory: str | Path | None = None) -> tuple[dict[str, SubAgent], list[str]]:
    """Load every `*.md` sub-agent. Returns (roster, problems).

    `problems` are human-readable strings for files that failed to parse, so a
    caller can surface them without the whole roster going dark.
    """
    directories = [Path(directory)] if directory else _search_dirs()
    roster: dict[str, SubAgent] = {}
    problems: list[str] = []
    for folder in directories:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                agent = parse_subagent(path.read_text(encoding="utf-8"), str(path))
            except Exception as exc:
                problems.append(f"{path}: {exc}")
                continue
            # Later directories override earlier ones by name.
            roster[agent.name] = agent
    return roster, problems


class SubAgents:
    """A spawned-sub-agent runner bound to a tool registry.

    Holds the main agent's tool definitions and handlers so a sub-agent can
    inherit them (filtered by its own allowlist), and the roster of declarative
    agents. Construct once per session; `spawn` is cheap to call repeatedly.
    """

    def __init__(
        self,
        tools: list[dict[str, Any]],
        handlers: dict[str, Callable[..., str]],
        directory: str | Path | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.definitions = tools
        self.handlers = handlers
        self.event_callback = event_callback
        self.roster, self.problems = discover(directory)

    # -- registry -----------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self.roster)

    def get(self, name: str) -> SubAgent | None:
        return self.roster.get(name)

    def describe(self) -> str:
        """Roster rendered for the model, so it knows what it can spawn."""
        if not self.roster:
            return "(no sub-agents are defined)"
        lines = []
        for name in self.names():
            agent = self.roster[name]
            tools = "all" if agent.tools is None else ", ".join(agent.tools)
            lines.append(f"- {name}: {agent.description or '(no description)'} "
                         f"[tools: {tools}; max_iterations: {agent.max_iterations}]")
        if self.problems:
            lines.append("")
            lines.append("Skipped (unparseable): " + "; ".join(self.problems))
        return "\n".join(lines)

    # -- spawn --------------------------------------------------------------

    def _scoped_tools(self, agent: SubAgent) -> tuple[list[dict[str, Any]], dict[str, Callable[..., str]]]:
        if agent.tools is None:
            return self.definitions, self.handlers
        defs = [d for d in self.definitions if d["function"]["name"] in agent.tools]
        handlers = {k: v for k, v in self.handlers.items() if k in agent.tools}
        missing = set(agent.tools) - set(handlers)
        if missing:
            raise ValueError(
                f"sub-agent {agent.name!r} names unknown tools: {sorted(missing)}"
            )
        return defs, handlers

    def spawn(
        self,
        name: str,
        task: str,
        max_iterations: int | None = None,
        hooks: Any | None = None,
    ) -> str:
        """Run one sub-agent to completion on `task` and return its answer.

        The sub-agent runs a bare loop (empty Hooks) unless `hooks` is passed,
        because inheriting the parent's Stop gate would make a sub-agent block on
        a plan it does not own.
        """
        agent = self.roster.get(name)
        if agent is None:
            known = ", ".join(self.names()) or "(none)"
            return f"Unknown sub-agent {name!r}. Available: {known}."

        try:
            defs, handlers = self._scoped_tools(agent)
        except ValueError as exc:
            return f"Cannot spawn {name!r}: {exc}"

        from hooks import Hooks
        from llm import LLM
        from agent import run_agent

        iterations = int(max_iterations or agent.max_iterations)
        llm = LLM(model=agent.model) if agent.model else LLM()
        messages = [
            {"role": "system", "content": agent.system_prompt},
            {"role": "user", "content": task},
        ]

        if self.event_callback:
            self.event_callback("subagent_start", {"agent": name, "task": task})

        answer = run_agent(
            messages,
            defs,
            iterations,
            handlers,
            event_callback=self.event_callback,
            llm=llm,
            hooks=hooks if hooks is not None else Hooks(),
        )

        if self.event_callback:
            self.event_callback("subagent_end", {"agent": name, "answer": answer})

        label = agent.description or agent.name
        header = f"[sub-agent: {name} — {label}]\n"
        return header + answer


def load_default_agents(directory: str | Path | None = None) -> tuple[dict[str, SubAgent], list[str]]:
    """Convenience for callers that only want the roster."""
    return discover(directory)
