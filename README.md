# Local Coding Agent — read-only references, secret filtering, research, and Excel

`roots.txt` contains writable directories. They appear as `/workspace/write/rootN`.

`readonly_roots.txt` contains reference directories. Docker mounts them `readonly`, and they appear as `/workspace/read/rootN`.

The agent can list/read reference files, but Docker itself prevents writes to those mounts.

## Architecture

- `agent.py` — the generic reasoning loop `run_agent(messages, tools, max_iterations, handlers, ...)`, shared by the main agent and every sub-agent. `Agent` is the stateful per-session wrapper.
- `tools.py` — assembles the whole tool surface. Sandbox tools (`list_files`, `read_file`, `write_file`, `apply_patch`, `run_command`), the host tool (`research`), the Excel workbook tools, and the convention-file readers.
- `research.py` — a research sub-agent exposed to the coding agent as the `research` tool. It runs a nested `run_agent` loop with host-side web tools and returns a concise, source-cited summary.
- `web.py` — host-side `web_search` / `fetch_page`. The sandbox runs with `--network none`, so web access happens in the trusted host process. Uses the optional `crw` binary; degrades gracefully if absent.
- `excel_tools.py` — host-side `excel_*` tools. Ships the workbook engine into the running sandbox and drives it with a JSON request per call.
- `excel/xlsx.py` — a standard-library-only OOXML reader/writer. This is why Excel works in a network-disabled container: no `pip install openpyxl`, no image rebuild.
- `excel/engine.py` — the sandbox-side driver. All workbook file I/O happens here, inside the container, so the `/workspace/write` policy still applies.
- `excel/knowledge_tools.py` — `read_excel_guide`, `read_research_guide`, `read_research_notes`, `append_research_notes`. These read the editable Markdown in `knowledge/`.
- `excel/excel_test.py` — 40 self-checks for the engine. Runs in the sandbox with stock Python.
- `knowledge/` — the convention files you edit. `EXCEL_GUIDE.md`, `RESEARCH_GUIDE.md`, `RESEARCH_NOTES.md`.
- `bootstrap_excel.py` — verifies the knowledge files and engine are in place. `--check` reports only.
- `app.py` — Flask UI with Chat / Agent / Tools / Knowledge tabs and an SSE event stream.

A sub-agent is just another call to `run_agent` with its own `messages`, `tools`, and `handlers`. That is what lets `research` run nested inside the coding agent without any special casing.

## Excel

The agent builds workbooks with five tools:

| Tool | Purpose |
| --- | --- |
| `excel_create` | New `.xlsx` with a populated Cover sheet |
| `excel_write_sheet` | Write a table (headers, rows, formats, widths) to a sheet |
| `excel_edit_cells` | Write individual cells; corrections and totals rows |
| `excel_read` | Inspect headers, dimensions, number formats, row preview |
| `excel_list_sheets` | Sheet names, row counts, headers |

Number formats are passed as aliases — `currency`, `usd2`, `percent`, `percent2`,
`multiple`, `date`, `datetime`, `int`, `number`, `number2`, `signed_pct`,
`signed_num`, `text` — or as a raw Excel format code. Numeric-looking strings
(`"1,250"`, `"31.8%"`, `"$24,560"`) are coerced to real numbers, because an LLM
will otherwise write `"1,250"` and silently destroy the column's type.

The engine is copied into the container at `/tmp/.excel_engine` on first use and
overwritten on every call, so editing `excel/xlsx.py` takes effect immediately
with no restart.

**Why not openpyxl.** The sandbox runs `--network none`, so `pip install` fails.
Vendoring openpyxl would mean shipping roughly a megabyte of dependency tree into
the context-adjacent state and rebuilding an image to change it. The native writer
is ~700 lines of standard library, covers typed cells, per-column number formats,
frozen panes, column widths, shared strings and formulas, and is identical to edit
and to review. The tradeoff is honest: no charts, no pivot tables, no formula
evaluation (formulas are stored; `excel_read` reports the cached value).

## Knowledge files

`knowledge/EXCEL_GUIDE.md` and `knowledge/RESEARCH_GUIDE.md` are plain Markdown
that you maintain. The agent never loads them automatically. Its system prompt
names the tools and the moment to call them, so the conventions cost nothing until
an Excel or research task begins:

- `read_excel_guide` — before the first `excel_*` call in a task. Sheet layout,
  number formats, naming, reported-versus-estimated colouring, verification list.
- `read_research_guide` — before a research or data-compilation task. Source
  hierarchy (SEC EDGAR, XBRL company facts, filings), fetch rules and rate limits,
  build order, known traps.
- `read_research_notes` — your standing context: coverage universe, house metric
  definitions, prior conclusions.
- `append_research_notes` — the agent can record a durable finding. Keep in mind it
  writes to the same file you edit.

Point `EXCEL_KNOWLEDGE_DIR` at a different directory if you would rather keep these
outside the app directory. `KNOWLEDGE_MAX_CHARS` caps how much of a file is returned.

## Secret protection

Known secret-bearing files are blocked by `access.py`, including `.env*`, `.pem`, `.key`, `.p12`, `.pfx`, `.npmrc`, `.pypirc`, `.netrc`, common credential files, and sensitive directories such as `.ssh` and `.aws`.

Returned file/command output also passes through regex-based secret redaction.

Important: embedded secrets in arbitrary source files cannot be detected perfectly. Regex redaction is defense in depth, not a proof of safety. The strongest protection is not placing secrets in files exposed to the agent at all.

To prevent the generic command tool from bypassing filtering with `cat`, Python, etc., `run_command` is prohibited from directly referencing `/workspace/read`; reference files must go through `read_file`/`list_files`.

The `excel_*` tools route every write through `AccessPolicy.assert_write`, so a
workbook cannot be created outside `/workspace/write`.

## Configuration

Environment variables (see `config.py`; put them in `.env`):

- `DEEPSEEK_API_KEY` (required), `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL`
- `MAX_ITERATIONS` (main agent), `RESEARCH_MAX_ITERATIONS` (research sub-agent)
- `MAX_HISTORY`, `MAX_TOOL_OUTPUT`
- `AGENT_IMAGE`, `HOST`, `PORT`
- `EXCEL_KNOWLEDGE_DIR`, `KNOWLEDGE_MAX_CHARS`, `KNOWLEDGE_MAX_NOTE_CHARS`

Web research requires the `crw` binary on the host PATH (or at `/usr/local/bin/crw`, `/home/*/.local/bin/crw`). Without it, `research` returns a clear "unavailable" message and the rest of the agent still works. Excel does not need `crw` and works with no network at all.

## Setup

1. Put writable/output folders in `roots.txt`.
2. Put reference folders in `readonly_roots.txt`.
3. Keep your actual `.env` outside all exposed roots where practical.
4. Optionally edit `knowledge/EXCEL_GUIDE.md` to your house conventions.
5. Run `python3 bootstrap_excel.py --check` to confirm the guides and engine are present.
6. Run `python3 app.py`.

The Docker sandbox remains network-disabled and capability-restricted.

## Verification

There is no Docker in a bare checkout, so the checks are layered. Run them after
any change to the Excel stack:

```bash
cd excel        && python3 excel_test.py      # 40 engine round-trip checks
cd excel        && python3 -S -E excel_test.py  # same, with no site-packages
```

With Docker running and the app started, the live path is exercised by asking the
agent for a workbook and confirming `excel_read` reports the expected sheets and
formats. `bootstrap_excel.py --check` covers the file layout.
