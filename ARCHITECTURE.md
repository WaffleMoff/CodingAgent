# Architecture

How the agent is put together and why. For setup steps, see [README.md](README.md).

## The one idea

Everything the model can touch is a **tool**. The reasoning loop knows nothing
about sandboxes, files, or the network — it only calls tools by name. Features
are added by adding tools or hooking the loop's lifecycle, never by editing the
loop. That is what keeps the core small and the security boundaries in one place.

## Components

- `agent.py` — the generic reasoning loop `run_agent(messages, tools,
  max_iterations, handlers, ...)`, shared by the main agent and every sub-agent.
  `Agent` is the stateful per-session wrapper the UI drives.
- `llm.py` / `prompts.py` — the model client and the system prompt.
- `tools.py` — assembles the whole tool surface: sandbox tools, the host tools,
  the Excel tools, the knowledge readers, the plan tool, and `manage_subagent`.
- `hooks.py` / `hooks_wiring.py` — the lifecycle hook bus and the standard hook
  set (permissions, verification, compaction fidelity, the finish gate).
- `permissions.py` / `gate.py` / `verify.py` — the pre-tool-use policy, the
  finish gate, and the post-write linter loop.
- `subagents.py` / `research.py` / `web.py` — declarative sub-agents, the
  research sub-agent, and host-side web access.
- `excel_tools.py` / `excel/` — the Excel tool surface and a standard-library
  OOXML engine.
- `excel/knowledge_tools.py` / `knowledge/` — the editable convention files.
- `app.py` — the Flask UI (Chat / Agent / Tools / Roots tabs) and the SSE stream.

A sub-agent is just another `run_agent` call with its own `messages`, `tools`,
and `handlers`. That is why `research` can run nested inside the coding agent
with no special casing: the loop does not care who is calling it.

## The sandbox

`workspace.py` runs one Docker container per session group. The container is
locked down at `docker run`:

- `--network none` — no network at all.
- `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 512`,
  `--memory 4g`, `--cpus 4`.
- `python:3.12-slim` by default, pulled on first use.

Writable roots mount at `/workspace/write/rootN` (read/write). Read-only roots
mount at `/workspace/read/rootN` and are enforced read-only by the kernel.

**Why `WorkspaceProxy`.** Docker bind mounts are fixed when the container
starts, so changing the root set means building a new container. Every tool
handler closes over a `WorkspaceProxy`, not a concrete container. `RootManager`
swaps the proxy's target under a lock, so a rebuild is invisible to sessions
mid-run and no command is ever sent to a container being torn down.

**Secret shielding.** Docker bind mounts have no exclude directive, so
`workspace.py` overlays an empty read-only bind mount at the same path for any
file named `.env`, `.env.local`, `.env.production`, or `.env.development` found
at the top level of a mounted root. The name stays visible; the contents are
empty and unwritable. This is why your `.env` can sit in the project folder and
still be safe. It checks only the top level of each root — nested copies are not
covered, which is the placement rule the README gives.

## Model access to files

The sandbox filesystem is not enough on its own; the model reaches files through
tools, and those tools are the real boundary.

`access.py` is the chokepoint:

- `assert_read` / `assert_write` — path must be under `/workspace/read` or
  `/workspace/write`; writes only under `/workspace/write`.
- `assert_content_allowed` — rejects secret-bearing **filenames** (`.env*`,
  `.pem`, `.key`, `.p12`, `.pfx`, `.npmrc`, `.pypirc`, `.netrc`, credentials
  files) and sensitive **directories** (`.git`, `.ssh`, `.aws`, `.azure`,
  `.config/gcloud`) before the read.
- `assert_no_secrets` — runs on the bytes after the read and redacts credential
  shapes (`sk-...`, `ghp_...`, `AKIA...`, PEM blocks, dotenv-style lines). A
  file whose every substantive line is secret-shaped is refused whole rather
  than returned redacted.

Name checks are cheap but defeatable by renaming; the content check is what
covers the copy-to-an-innocuous-name route. Both run on every read-capable
handler.

`shell_policy.py` governs `run_command`. Programs are **allowlisted by
basename** (`SAFE_COMMANDS`), never blocklisted by substring — a substring test
is defeated by any quoting or splitting. Two rules close the historical leak:

1. Every path-looking token, on every pipeline stage, is resolved and rejected
   if it falls under `/workspace/read` or names a secret-shaped file. This runs
   regardless of the program, so `cat`, `sed`, `find -exec`, and `python3 -c`
   are covered equally.
2. Redirect targets get the same treatment, so nothing is written over a
   secret-shaped path or into the read-only root.

`run_command` output is the one read channel that never passed the content
filter, so results from read-capable commands are additionally redacted in
`tools.py`. That is why reference material must go through
`read_file`/`list_files`/`grep`, which enforce filtering — a design choice, not a
limitation to work around.

## The Excel engine

The sandbox has `--network none`, so `pip install openpyxl` is impossible.
`excel/xlsx.py` is a standard-library-only OOXML reader/writer (~700 lines) that
covers typed cells, per-column number formats, frozen panes, column widths,
shared strings, and formulas.

`excel_tools.py` ships `xlsx.py` and `excel/engine.py` into the running container
at `/tmp/.excel_engine` on first use, then drives it with one JSON request per
tool call. All workbook I/O happens inside the container, so the
`/workspace/write` policy still applies and `access.py` remains the single
chokepoint. The engine is re-shipped on every call, so editing it takes effect
with no restart.

The tradeoff is honest: no charts, no pivot tables, no formula evaluation.
Formulas are stored; `excel_read` reports the cached value.

## Hooks and the finish gate

`hooks.py` defines the lifecycle: `session_start`, `pre_tool_use`,
`post_tool_use`, `stop`, `pre_compact`, `post_compact`, `user_prompt_submit`.
An empty registry is a bare ReAct loop — that is what a sub-agent gets.

`hooks_wiring.default_hooks()` is the single registration point:
`pre_tool_use` → permission policy; `post_tool_use` → writes log, then the
linter; `post_compact` → state re-injection; `stop` → the finish gate. Order
matters: the write is logged before it is verified, so the log records intent
even when the check fails.

The finish gate (`gate.py`) fires when the model emits a no-tool-call turn.
Registered checks must pass or the reasons are fed back and the loop continues,
up to `FINISH_RETRIES` times.

## Context compaction

`compaction.py` keeps the head and a recent tail of the history and summarizes
the middle before a model call, once the rendered history crosses
`COMPACT_AT_TOKENS`. The writes log is read back and re-injected so "what have I
already written" survives the loss of the turns in which the writes happened.
Set `COMPACT_ENABLED=off` to send history unchanged.

## Configuration

All environment variables (see `config.py`; put them in `.env`):

- `DEEPSEEK_API_KEY` (required), `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL`
- `MAX_ITERATIONS` (main agent), `RESEARCH_MAX_ITERATIONS` (research sub-agent)
- `MAX_HISTORY`, `MAX_TOOL_OUTPUT`
- `AGENT_IMAGE`, `HOST`, `PORT`
- `COMPACT_ENABLED`, `COMPACT_AT_TOKENS`, `COMPACT_KEEP_RECENT`,
  `COMPACT_MAX_SUMMARY_TOKENS`
- `FINISH_GATE_ENABLED`, `FINISH_RETRIES`
- `EXCEL_KNOWLEDGE_DIR`, `KNOWLEDGE_MAX_CHARS`, `KNOWLEDGE_MAX_NOTE_CHARS`
- `AGENT_PLAN_PATH`, `AGENT_WRITES_LOG`, `AGENT_WRITE_ROOT`

Web research requires the `crw` binary on the host PATH (or at
`/usr/local/bin/crw`, `/home/*/.local/bin/crw`). Without it, `research` returns a
clear "unavailable" message and the rest of the agent still works. Excel needs
no `crw` and works with no network at all.

## Verifying a change to the Excel stack

There is no Docker dependency for the engine's own tests:

```bash
cd excel && python3 excel_test.py            # engine round-trip checks
cd excel && python3 -S -E excel_test.py      # same, with no site-packages
```

`python3 bootstrap_excel.py --check` covers file layout. With Docker running and
the app started, the live path is exercised by asking the agent for a workbook
and confirming `excel_read` reports the expected sheets and formats.

## Extending it

- **New tool** — add a schema to `definitions()` in `tools.py` and a handler to
  `sandbox_handlers()`. Nothing else changes.
- **New lifecycle behavior** — register a hook in
  `hooks_wiring.default_hooks()`.
- **New sub-agent** — drop a Markdown file in `agents/`; `subagents.py` picks it
  up and `manage_subagent` exposes it.
- **New shell program** — add it to `SAFE_COMMANDS` in `shell_policy.py`. Adding
  a program grants every capability it has; the path check, not the allowlist,
  is the read boundary.
