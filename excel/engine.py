#!/usr/bin/env python3
"""Sandbox-side driver for the `excel_*` tools.

The agent never imports this. The host tool layer copies this file (and
`xlsx.py`) into a temp directory inside the container and runs it with a JSON
request on argv. Keeping the file I/O inside the sandbox means every read and
write goes through exactly the same path the agent's own `write_file` uses, so
the write-root policy from `access.py` still holds.

Protocol: `python3 engine.py --request <json file>`, one JSON object in, one
JSON object out on stdout.

    {"op": "create",  "path": "...", "author": "..."}
    {"op": "write",   "path": "...", "sheet": "...", "rows": [...], ...}
    {"op": "read",    "path": "...", "sheet": null, "max_rows": 20}
    {"op": "edit",    "path": "...", "sheet": "...", "cell": "B2", "value": 5}
    {"op": "list",    "path": "..."}
    {"op": "describe","path": "..."}
    {"op": "append_notes", "path": "...", "note": "..."}

Every response is `{"ok": bool, ...}`. Errors are returned, never raised, so a
bad argument produces a readable tool result instead of a stack trace in the
model's context.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xlsx  # noqa: E402  (co-located module)

FORMAT_ALIASES = {
    "text": "@",
    "general": "General",
    "int": "#,##0",
    "integer": "#,##0",
    "number": "#,##0",
    "number2": "#,##0.00",
    "currency": '"$"#,##0',
    "currency2": '"$"#,##0.00',
    "usd": '"$"#,##0',
    "usd2": '"$"#,##0.00',
    "millions": '"$"#,##0',
    "percent": "0.0%",
    "percent2": "0.00%",
    "bps": '+0" bps";-0" bps"',
    "multiple": '0.0"x"',
    "x": '0.0"x"',
    "date": "yyyy-mm-dd",
    "datetime": "yyyy-mm-dd hh:mm",
    "signed_pct": "+0.0%;-0.0%",
    "signed_pct2": "+0.00%;-0.00%",
    "signed_num": "+#,##0;-#,##0",
}

_NUM_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d*\.?\d+$")


def resolve_format(code: str | None) -> str:
    if not code:
        return "General"
    key = str(code).strip()
    return FORMAT_ALIASES.get(key.lower(), key)


def coerce(value):
    """Turn tool-supplied scalars into workbook values.

    Strings that are unambiguously numbers become numbers, because an LLM will
    otherwise happily write "1,250" and silently destroy the column's type.
    Everything else is left alone.
    """
    if value is None or isinstance(value, (int, float, bool, _dt.date, _dt.datetime)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == "":
        return None
    iso = xlsx._DATE_RE.match(text)
    if iso:
        try:
            if iso.group(4):
                return _dt.datetime(
                    int(iso.group(1)), int(iso.group(2)), int(iso.group(3)),
                    int(iso.group(4)), int(iso.group(5)), int(iso.group(6) or 0),
                )
            return _dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return value
    if text.endswith("%"):
        try:
            return float(text[:-1].replace(",", "")) / 100.0
        except ValueError:
            return value
    money = text.replace("$", "").replace("\u00a3", "").replace("\u20ac", "").strip()
    if money != text and _NUM_RE.match(money):
        text = money
    if _NUM_RE.match(text):
        cleaned = text.replace(",", "")
        try:
            number = float(cleaned)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return value


def parse_formats(raw, columns: list[str] | None) -> dict[int, str]:
    """Accept {'0': 'currency'} or {'Revenue': 'currency'} or ['currency', ...]."""
    out: dict[int, str] = {}
    if raw is None:
        return out
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        for index, code in enumerate(raw):
            if code:
                out[index] = resolve_format(code)
        return out
    if isinstance(raw, dict):
        for key, code in raw.items():
            if code is None:
                continue
            text = str(key).strip()
            if text.isdigit():
                out[int(text)] = resolve_format(code)
                continue
            if columns and text in columns:
                out[columns.index(text)] = resolve_format(code)
                continue
            letters = re.fullmatch(r"[A-Za-z]{1,3}", text)
            if letters:
                out[xlsx._col_index(text)] = resolve_format(code)
                continue
            raise ValueError(
                f"column key {key!r} is neither a position, a column letter, nor a header"
            )
        return out
    raise ValueError("formats must be a list or an object")


def _date_columns(formats: dict[int, str]) -> set[int]:
    return {col for col, code in formats.items() if xlsx._is_date_format(code)}


def _row_width(row: list) -> int:
    return len(row)


def _normalise_rows(rows) -> list[list]:
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = json.loads(rows)
    if not isinstance(rows, list):
        raise ValueError("rows must be a list of lists")
    out = []
    for index, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            out.append(list(row.values()))
        elif isinstance(row, (list, tuple)):
            out.append([coerce(cell) for cell in row])
        else:
            raise ValueError(f"row {index} is not a list or object")
    return out


def _load_path(request) -> str:
    path = str(request.get("path", "")).strip()
    if not path:
        raise ValueError("path is required")
    return path


def op_create(request) -> dict:
    path = _load_path(request)
    sheet_name = request.get("sheet") or "Cover"
    book = xlsx.create_book()
    sheet = book.add(sheet_name)
    title = request.get("title") or os.path.basename(path)
    sheet.append(["Field", "Value"])
    sheet.append(["Title", title])
    for extra in request.get("metadata") or []:
        if isinstance(extra, dict):
            for key, value in extra.items():
                sheet.append([str(key), coerce(value)])
        elif isinstance(extra, (list, tuple)) and len(extra) == 2:
            sheet.append([coerce(extra[0]), coerce(extra[1])])
    sheet.append(["Created", _dt.date.today()], formats={1: "yyyy-mm-dd"})
    sheet.set_widths({0: 24, 1: 48})
    sheet.freeze("A2")
    book.save(path)
    return {"ok": True, "path": os.path.abspath(path), "sheets": book.names(),
            "rows": {"Cover": len(sheet)}}


def op_write(request) -> dict:
    path = _load_path(request)
    sheet_name = str(request.get("sheet") or "").strip()
    if not sheet_name:
        raise ValueError("sheet is required for write")
    if os.path.exists(path) and not request.get("overwrite_sheet", True):
        book = xlsx.load_book(path)
        if book.has(sheet_name):
            raise ValueError(f"sheet {sheet_name!r} exists; pass overwrite_sheet=true to replace")
    elif os.path.exists(path):
        book = xlsx.load_book(path)
    else:
        book = xlsx.create_book()

    if book.has(sheet_name) and request.get("overwrite_sheet", True):
        book.remove(sheet_name)
    sheet = book.ensure(sheet_name)

    headers = request.get("headers")
    if headers is not None and not isinstance(headers, list):
        headers = json.loads(headers)

    body = _normalise_rows(request.get("rows"))
    columns = [str(h) for h in headers] if headers else None
    formats = parse_formats(request.get("formats"), columns)

    start_row = request.get("start_row")
    if start_row:
        # Fill forward to the requested row, leaving earlier rows empty.
        while len(sheet.rows) < int(start_row) - 1:
            sheet.append([])

    if headers:
        sheet.append([coerce(h) for h in headers])
    for row in body:
        sheet.append(row)

    for col, code in formats.items():
        sheet.set_format_at(col + 1, code)

    widths = request.get("widths")
    if widths:
        if isinstance(widths, str):
            widths = json.loads(widths)
        sheet.set_widths({int(k): float(v) for k, v in widths.items()})
    elif headers:
        widths = {}
        for index, header in enumerate(headers):
            longest = len(str(header))
            for row in body[:200]:
                if index < len(row) and row[index] is not None:
                    longest = max(longest, len(str(_display(row[index]))))
            widths[index] = min(max(longest + 2, 10), 45)
        sheet.set_widths(widths)

    if request.get("freeze", True) and sheet.rows:
        sheet.freeze("A2")

    book.save(path)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "sheet": sheet.name,
        "sheets": book.names(),
        "rows_in_sheet": len(sheet.rows),
        "columns": len(headers) if headers else (max((len(r) for r in body), default=0)),
    }


def op_edit(request) -> dict:
    path = _load_path(request)
    book = xlsx.load_book(path)
    sheet_name = request.get("sheet")
    sheet = book.get(sheet_name) if sheet_name else book.sheets[0]

    edits = request.get("edits")
    if edits is None:
        if not request.get("cell"):
            raise ValueError("provide either cell+value or edits")
        edits = [{"cell": request["cell"], "value": request.get("value")}]
    if isinstance(edits, str):
        edits = json.loads(edits)

    applied = []
    for edit in edits:
        ref = str(edit.get("cell", "")).strip().upper()
        match = re.fullmatch(r"([A-Z]{1,3})(\d+)", ref)
        if not match:
            raise ValueError(f"bad cell reference {ref!r}; expected e.g. B7")
        col = xlsx._col_index(match.group(1))
        row = int(match.group(2))
        while len(sheet.rows) < row:
            sheet.append([])
        line = sheet.rows[row - 1]
        while len(line) <= col:
            line.append(None)
        line[col] = coerce(edit.get("value"))
        if edit.get("format"):
            sheet.set_format_at(col + 1, resolve_format(edit["format"]))
        applied.append(ref)

    if request.get("set_format") and request.get("column") is not None:
        key = request["column"]
        if isinstance(key, int):
            sheet.set_format_at(key, resolve_format(request["set_format"]))
        else:
            sheet.set_format(key, resolve_format(request["set_format"]))

    book.save(path)
    return {"ok": True, "path": os.path.abspath(path), "sheet": sheet.name,
            "cells_written": applied}


def op_read(request) -> dict:
    path = _load_path(request)
    book = xlsx.load_book(path)
    want = request.get("sheet")
    sheets = [book.get(want)] if want else list(book.sheets)
    max_rows = int(request.get("max_rows") or 20)
    out = []
    for sheet in sheets:
        info = xlsx.describe(sheet, max_rows=max_rows)
        info["preview"] = [
            [_display(cell) for cell in row] for row in info["preview"]
        ]
        if request.get("include_data") and sheet.name == (want or sheet.name):
            info["data"] = [
                [_display(cell) for cell in row] for row in sheet.rows[1:]
            ]
        out.append(info)
    return {"ok": True, "path": os.path.abspath(path), "sheets": out}


def op_list(request) -> dict:
    path = _load_path(request)
    book = xlsx.load_book(path)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "sheets": [
            {"name": s.name, "rows": len(s.rows), "headers": [str(h) for h in s.header]}
            for s in book.sheets
        ],
    }


def op_describe(request) -> dict:
    return op_read(request)


def op_append_notes(request) -> dict:
    path = _load_path(request)
    note = str(request.get("note") or "").strip()
    if not note:
        raise ValueError("note is required")
    stamp = _dt.date.today().isoformat()
    header = "" if os.path.exists(path) and os.path.getsize(path) > 0 else "# Research notes\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{header}\n### {stamp}\n{note}\n")
    return {"ok": True, "path": os.path.abspath(path)}


OPS = {
    "create": op_create,
    "write": op_write,
    "edit": op_edit,
    "read": op_read,
    "list": op_list,
    "describe": op_describe,
    "append_notes": op_append_notes,
}


def _display(value) -> object:
    if isinstance(value, _dt.datetime):
        if value.time() == _dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        with open(args.request, "r", encoding="utf-8") as fh:
            request = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"unreadable request: {exc}"}))
        return 2

    op = request.get("op")
    if op not in OPS:
        print(json.dumps({"ok": False, "error": f"unknown op {op!r}; known: {sorted(OPS)}"}))
        return 2
    try:
        result = OPS[op](request)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
