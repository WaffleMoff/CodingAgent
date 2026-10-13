"""Excel guidance injected into the conversation by the hook bus.

Why this exists: the model has the `excel_*` tools, but the static system prompt
does not name them, so on a spreadsheet task it reaches for `run_command` /
`python3` and hand-writes workbook XML - which produces workbooks missing the
number formats, widths, and coercion the engine applies automatically. That is
the defect this module prevents.

The instruction is delivered as a system message via the `session_start` hook
(seeded once, before the first model call) and re-seeded via `post_compact`
(so it survives context compaction). Nothing here depends on `prompts.py`, so it
can evolve alongside the excel tools without touching the prompt.

Keep this text next to `excel_tools.py`, which defines the tools it references,
so the two stay in step.
"""

from __future__ import annotations

from typing import Any

EXCEL_GUIDANCE = """## Excel workbooks

When a task involves producing a spreadsheet, a workbook, a `.xlsx` file, or a
tabular deliverable, build it with the dedicated `excel_*` tools — `excel_create`,
`excel_write_sheet`, `excel_edit_cells`, `excel_read`, `excel_list_sheets`. They are
the supported path and the only one that produces a correctly formatted workbook.

Before your first `excel_*` call in a task, call `read_excel_guide`. It is the
authority on sheet layout, number formats, naming, and what "done" means; read it
rather than guessing. Once per task, not once per sheet.

Do not hand-write workbook XML and do not drive `python3`, `openpyxl`, or
`run_command` to produce a workbook the `excel_*` tools already produce. The sandbox
has no network, so `openpyxl` cannot be installed and a `pip install` will fail. A
workbook written by hand will be missing the number formats, widths, and coercion the
tools apply automatically — that is the defect this rule exists to prevent.
`run_command` is an escape hatch for the rare case the tools genuinely cannot
express, never the default.

Two rules that catch most formatting failures:

- Every numeric column gets a format, passed per column via the `formats` argument
  (`currency`, `percent`, `multiple`, `date`, `int`, `number`, `text`, and the rest of
  the aliases in the guide). A column of bare, unformatted numbers is unfinished work.
- Numeric-looking strings are coerced to numbers on write. Pass identifier columns
  (tickers, account codes) as `text` explicitly so they are not mangled.

After writing, run `excel_read` on the finished file and confirm the sheets, headers,
row counts, and per-column formats before reporting done. "Done" without a read-back
is a claim, not a result.
"""


def excel_guidance_message() -> dict[str, Any]:
    """The guidance as a single system message."""
    return {"role": "system", "content": EXCEL_GUIDANCE}


def make_excel_guidance_hook() -> Any:
    """Hook callable for `session_start` / `post_compact`.

    Both events expect a list of messages to inject, so one callable serves both.
    """

    def hook(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [excel_guidance_message()]

    return hook
