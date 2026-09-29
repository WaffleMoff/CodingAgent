"""Host-side `excel_*` tools.

The sandbox has no network, so openpyxl cannot be installed. Instead this module
ships the engine (`excel/xlsx.py`, `excel/engine.py`) into the running container
at call time and drives it with a JSON request. That means:

  * Workbook I/O happens inside the sandbox, under the same `/workspace/write`
    policy as every other write, so `access.py` remains the single chokepoint.
  * The engine is versioned with the app, not baked into an image, so upgrading
    Excel support is a file edit rather than a rebuild.
  * Nothing needs installing at run time.

Only `xlsx.py` and `engine.py` are copied. They import nothing outside the
standard library.
"""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path
from typing import Any, Callable

from access import AccessPolicy, redact_secrets
from workspace import DockerWorkspace

POLICY = AccessPolicy()

EXCEL_DIR = Path(__file__).resolve().parent / "excel"
ENGINE_FILES = ("xlsx.py", "engine.py")

# Ship into a scratch dir inside the container; overwritten on every call so an
# edited engine takes effect without restarting the sandbox.
REMOTE_DIR = "/tmp/.excel_engine"

# Guard rails on model-supplied payloads, to keep a runaway response from
# filling the context window or the container's disk.
MAX_ROWS_PER_CALL = 20_000
MAX_ROW_WIDTH = 512


class ExcelToolError(RuntimeError):
    """A failure the model should see as a tool result, not a crash."""


def _validate_payload(rows: Any) -> None:
    if rows is None:
        return
    if not isinstance(rows, list):
        raise ExcelToolError("rows must be a list of lists")
    if len(rows) > MAX_ROWS_PER_CALL:
        raise ExcelToolError(
            f"{len(rows)} rows exceeds the {MAX_ROWS_PER_CALL}-row limit per call; "
            "split the sheet across multiple write_sheet calls"
        )
    for index, row in enumerate(rows, start=1):
        if isinstance(row, (list, tuple)) and len(row) > MAX_ROW_WIDTH:
            raise ExcelToolError(
                f"row {index} has {len(row)} columns; the limit is {MAX_ROW_WIDTH}"
            )


class ExcelBridge:
    """Copies the engine into the sandbox and runs one request per tool call."""

    def __init__(self, workspace: DockerWorkspace, max_output: int):
        self.workspace = workspace
        self.max_output = max_output
        self._loaded = False

    # -- plumbing --------------------------------------------------------
    def _ensure_engine(self) -> None:
        if self._loaded:
            return
        missing = [name for name in ENGINE_FILES if not (EXCEL_DIR / name).exists()]
        if missing:
            raise ExcelToolError(
                f"Excel engine files missing from {EXCEL_DIR}: {missing}"
            )
        for name in ENGINE_FILES:
            source = (EXCEL_DIR / name).read_bytes()
            # Chunked base64 keeps the command line well under ARG_MAX.
            payload = __import__("base64").b64encode(source).decode()
            target = f"{REMOTE_DIR}/{name}"
            command = (
                f"mkdir -p {shlex.quote(REMOTE_DIR)} && "
                f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(target)}"
            )
            result = self.workspace.execute(command)
            if result["exit_code"] != 0:
                raise ExcelToolError(
                    f"failed to install {name} in the sandbox: {result['stderr']}"
                )
        self._loaded = True

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one engine request inside the sandbox and return its JSON reply."""
        path = request.get("path")
        if path:
            POLICY.assert_write(path)
        self._ensure_engine()

        token = uuid.uuid4().hex[:12]
        request_path = f"{REMOTE_DIR}/req-{token}.json"
        payload = json.dumps(request, default=str)
        encoded = __import__("base64").b64encode(payload.encode()).decode()
        result = self.workspace.execute(
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(request_path)} && "
            f"cd {shlex.quote(REMOTE_DIR)} && "
            f"python3 engine.py --request {shlex.quote(request_path)}; "
            f"rm -f {shlex.quote(request_path)}",
            timeout=180,
        )
        stdout = str(result["stdout"]).strip()
        stderr = str(result["stderr"]).strip()
        if not stdout:
            raise ExcelToolError(
                f"engine produced no output (exit {result['exit_code']}): {stderr[:400]}"
            )
        try:
            reply = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            raise ExcelToolError(
                f"engine returned unparseable output: {stdout[:400]}"
            ) from None
        if not reply.get("ok"):
            raise ExcelToolError(str(reply.get("error") or "unknown engine error"))
        return reply

    def _clip(self, text: str) -> str:
        text = redact_secrets(text)
        if len(text) <= self.max_output:
            return text
        return text[: self.max_output] + (
            f"\n...[truncated {len(text) - self.max_output} characters]"
        )

    # -- tools -----------------------------------------------------------
    def create_workbook(
        self,
        path: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        sheet: str = "Cover",
    ) -> str:
        """Create a new workbook with a populated Cover sheet."""
        meta = []
        if metadata:
            if not isinstance(metadata, dict):
                raise ExcelToolError("metadata must be an object of field: value")
            meta = [{str(k): v} for k, v in metadata.items()]
        reply = self.call({
            "op": "create",
            "path": path,
            "title": title or Path(path).stem,
            "metadata": meta,
            "sheet": sheet,
        })
        return json.dumps({
            "ok": True,
            "path": reply["path"],
            "sheets": reply["sheets"],
            "next": "use write_sheet to add data sheets",
        }, indent=2)

    def write_sheet(
        self,
        path: str,
        sheet: str,
        rows: list[list[Any]],
        headers: list[str] | None = None,
        formats: dict[str, str] | None = None,
        widths: dict[str, float] | None = None,
        start_row: int | None = None,
        overwrite_sheet: bool = True,
        freeze: bool = True,
    ) -> str:
        """Write a rectangular table to a sheet, creating the workbook if needed."""
        _validate_payload(rows)
        reply = self.call({
            "op": "write",
            "path": path,
            "sheet": sheet,
            "headers": headers,
            "rows": rows,
            "formats": formats,
            "widths": widths,
            "start_row": start_row,
            "overwrite_sheet": overwrite_sheet,
            "freeze": freeze,
        })
        return json.dumps({
            "ok": True,
            "path": reply["path"],
            "sheet": reply["sheet"],
            "sheets": reply["sheets"],
            "rows_in_sheet": reply["rows_in_sheet"],
            "columns": reply["columns"],
        }, indent=2)

    def edit_cells(
        self,
        path: str,
        edits: list[dict[str, Any]],
        sheet: str | None = None,
    ) -> str:
        """Write individual cells. Each edit is {cell, value, format?}."""
        if not edits:
            raise ExcelToolError("edits is required and must be non-empty")
        if isinstance(edits, str):
            edits = json.loads(edits)
        reply = self.call({"op": "edit", "path": path, "sheet": sheet, "edits": edits})
        return json.dumps(reply, indent=2)

    def read_workbook(
        self,
        path: str,
        sheet: str | None = None,
        max_rows: int = 20,
        include_data: bool = False,
    ) -> str:
        """Inspect structure, headers, formats and a preview of rows."""
        reply = self.call({
            "op": "read",
            "path": path,
            "sheet": sheet,
            "max_rows": int(max_rows),
            "include_data": include_data,
        })
        return self._clip(json.dumps(reply["sheets"], indent=2, default=str))

    def list_sheets(self, path: str) -> str:
        """List sheets with row counts and headers, without loading data."""
        reply = self.call({"op": "list", "path": path})
        return json.dumps(reply, indent=2)


def tools(bridge: ExcelBridge) -> list[dict[str, Any]]:
    """Schemas for the excel_* tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "excel_create",
                "description": (
                    "Create a new .xlsx workbook with a Cover sheet (title, metadata, "
                    "created date). Use this once per workbook, before write_sheet."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path under /workspace/write, ending in .xlsx",
                        },
                        "title": {"type": "string"},
                        "metadata": {
                            "type": "object",
                            "description": "Cover fields, e.g. {Ticker: LYV, As Of: 2026-01-16}",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "excel_write_sheet",
                "description": (
                    "Write a table to a sheet. Creates the workbook if it does not exist. "
                    "`formats` maps a column header, letter, or 0-based index to a format: "
                    "currency, usd2, percent, percent2, multiple, date, datetime, int, number, "
                    "number2, signed_pct, signed_num, text, or a raw Excel format code. "
                    "Numeric-looking strings ('1,250', '31.8%', '$12') are coerced to numbers."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sheet": {
                            "type": "string",
                            "description": "Sheet name; use Summary, Data_<topic>, Calcs_<topic>, Sources",
                        },
                        "headers": {"type": "array", "items": {"type": "string"}},
                        "rows": {
                            "type": "array",
                            "items": {"type": "array", "items": {}},
                            "description": "List of rows; each row is a list of cell values",
                        },
                        "formats": {
                            "type": "object",
                            "description": "Column header/letter/index -> format alias",
                        },
                        "widths": {
                            "type": "object",
                            "description": "0-based column index (as a string) -> width in characters",
                        },
                        "start_row": {
                            "type": "integer",
                            "description": "Row to begin at, for stacking two tables on a sheet",
                        },
                        "overwrite_sheet": {
                            "type": "boolean",
                            "description": "Replace an existing sheet of this name (default true)",
                        },
                        "freeze": {"type": "boolean", "description": "Freeze the header row"},
                    },
                    "required": ["path", "sheet", "rows"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "excel_edit_cells",
                "description": (
                    "Write individual cells in an existing workbook. Each edit is "
                    "{cell: 'B7', value: ..., format: 'currency'|null}. Use for corrections "
                    "and for adding a totals row."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sheet": {"type": "string", "description": "Defaults to the first sheet"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cell": {"type": "string"},
                                    "value": {},
                                    "format": {"type": "string"},
                                },
                                "required": ["cell"],
                            },
                        },
                    },
                    "required": ["path", "edits"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "excel_read",
                "description": (
                    "Inspect a workbook: sheet names, dimensions, headers, per-column number "
                    "formats, a row preview, and the count of formula cells. Run this after "
                    "writing to verify the file before reporting done."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sheet": {"type": "string", "description": "Omit to inspect every sheet"},
                        "max_rows": {"type": "integer"},
                        "include_data": {
                            "type": "boolean",
                            "description": "Include every row of the requested sheet, not just the preview",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "excel_list_sheets",
                "description": "List a workbook's sheets with row counts and headers.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
    ]


def handlers(bridge: ExcelBridge) -> dict[str, Callable[..., str]]:
    return {
        "excel_create": bridge.create_workbook,
        "excel_write_sheet": bridge.write_sheet,
        "excel_edit_cells": bridge.edit_cells,
        "excel_read": bridge.read_workbook,
        "excel_list_sheets": bridge.list_sheets,
    }
