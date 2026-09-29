#!/usr/bin/env python3
"""Self-check for the sandbox-side Excel stack.

Runs inside the agent image (and anywhere else with a stock python3) and asserts
the OOXML round-trip properties the `excel_*` tools depend on. No third-party
imports, deliberately: the sandbox has no network.

    python3 excel_test.py            # all checks
    python3 excel_test.py -v         # show each case
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402
import xlsx  # noqa: E402

_FAILURES: list[str] = []
_PASSES = 0
_VERBOSE = False


def check(label: str, condition: bool, detail: str = "") -> None:
    global _PASSES
    if condition:
        _PASSES += 1
        if _VERBOSE:
            print(f"  ok   {label}")
    else:
        _FAILURES.append(f"{label}{(' -> ' + detail) if detail else ''}")
        print(f"  FAIL {label} {detail}")


def call(req: dict) -> dict:
    """Run a request through the same entry point the host tool uses."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(req, fh)
        path = fh.name
    try:
        out = engine.main(["--request", path])
        return {"_rc": out}
    except SystemExit as exc:
        return {"_rc": exc.code}


def run_op(req: dict) -> dict:
    """Dispatch in-process; equivalent to the subprocess path but inspectable."""
    return engine.OPS[req["op"]](req)


def call_op(req: dict) -> dict:
    """Full path: write a request file, run engine.main, parse stdout."""
    import io
    import contextlib

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(req, fh)
        request_path = fh.name
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            engine.main(["--request", request_path])
    finally:
        os.unlink(request_path)
    text = buffer.getvalue().strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"non-JSON output: {text[:200]}"}


def test_number_roundtrip(tmp: str) -> None:
    path = os.path.join(tmp, "numbers.xlsx")
    run_op({"op": "write", "path": path, "sheet": "N",
            "headers": ["Int", "Float", "Money", "Pct", "Text"],
            "rows": [[23180, 0.3187, "1,250", "12.5%", "FY2025"]],
            "formats": {"Int": "int", "Float": "number2", "Money": "currency",
                        "Pct": "percent", "Text": "text"}})
    sheet = xlsx.load_book(path).get("N")
    row = sheet.rows[1]
    check("int stays integral", row[0] == 23180 and isinstance(row[0], int), repr(row[0]))
    check("float preserved", abs(row[1] - 0.3187) < 1e-12, repr(row[1]))
    check("comma string becomes number", row[2] == 1250, repr(row[2]))
    check("percent string becomes fraction", abs(row[3] - 0.125) < 1e-12, repr(row[3]))
    check("plain text untouched", row[4] == "FY2025", repr(row[4]))
    check("currency format persisted",
          sheet.formats.get(2, "").replace('"', "") == "$#,##0", repr(sheet.formats))
    check("percent format persisted", sheet.formats.get(3) == "0.0%", repr(sheet.formats))


def test_large_and_precise(tmp: str) -> None:
    path = os.path.join(tmp, "big.xlsx")
    big = 480_000_000
    precise = 1.2345678901234
    run_op({"op": "write", "path": path, "sheet": "B",
            "headers": ["Tickets", "Ratio"],
            "rows": [[big, precise]],
            "formats": {"Tickets": "int", "Ratio": "number2"}})
    row = xlsx.load_book(path).get("B").rows[1]
    check("large int exact", row[0] == big, repr(row[0]))
    check("precision survives, not truncated to int",
          abs(row[1] - precise) < 1e-12, repr(row[1]))


def test_date_roundtrip(tmp: str) -> None:
    path = os.path.join(tmp, "dates.xlsx")
    as_of = _dt.date(2026, 1, 16)
    moment = _dt.datetime(2026, 1, 16, 9, 30, 0)
    run_op({"op": "write", "path": path, "sheet": "D",
            "headers": ["As Of", "Stamp"],
            "rows": [[as_of, moment]],
            "formats": {"As Of": "date", "Stamp": "datetime"}})
    sheet = xlsx.load_book(path).get("D")
    got_date, got_stamp = sheet.rows[1]
    check("date round-trips", getattr(got_date, "date", lambda: got_date)() == as_of,
          repr(got_date))
    check("datetime round-trips",
          isinstance(got_stamp, _dt.datetime) and got_stamp == moment, repr(got_stamp))


def test_multisheet_and_edit(tmp: str) -> None:
    path = os.path.join(tmp, "multi.xlsx")
    run_op({"op": "write", "path": path, "sheet": "Summary",
            "headers": ["Metric", "Value"], "rows": [["Revenue", 1]]})
    run_op({"op": "write", "path": path, "sheet": "Sources",
            "headers": ["fact", "url"], "rows": [["10-K", "https://x"]]})
    book = xlsx.load_book(path)
    check("two sheets persisted", book.names() == ["Summary", "Sources"], repr(book.names()))

    run_op({"op": "edit", "path": path, "sheet": "Summary",
            "edits": [{"cell": "B2", "value": 24560, "format": "currency"},
                      {"cell": "C1", "value": "Note"}]})
    sheet = xlsx.load_book(path).get("Summary")
    check("edit writes a value", sheet.rows[1][1] == 24560, repr(sheet.rows[1]))
    check("edit extends the row", sheet.rows[0][2] == "Note", repr(sheet.rows[0]))
    check("edit applies a format", sheet.formats.get(1, "").endswith("#,##0"),
          repr(sheet.formats))
    check("other sheet untouched", xlsx.load_book(path).get("Sources").rows[1][0] == "10-K")


def test_single_row_formats(tmp: str) -> None:
    """A header-only sheet must still carry its column formats."""
    path = os.path.join(tmp, "header_only.xlsx")
    run_op({"op": "write", "path": path, "sheet": "H",
            "headers": ["A", "B"], "rows": [], "formats": {"A": "currency"}})
    sheet = xlsx.load_book(path).get("H")
    check("header-only sheet keeps formats",
          sheet.formats.get(0, "").replace('"', "") == "$#,##0", repr(sheet.formats))


def test_sheet_overwrite(tmp: str) -> None:
    path = os.path.join(tmp, "overwrite.xlsx")
    run_op({"op": "write", "path": path, "sheet": "S", "headers": ["A"], "rows": [[1]]})
    run_op({"op": "write", "path": path, "sheet": "S", "headers": ["B"], "rows": [[2]]})
    sheet = xlsx.load_book(path).get("S")
    check("overwrite replaces the sheet", sheet.header == ["B"] and sheet.rows[1] == [2],
          repr(sheet.rows))
    check("overwrite leaves one sheet", xlsx.load_book(path).names() == ["S"])


def test_errors_are_reported_not_raised(tmp: str) -> None:
    """Through `main` (the real entry point) a bad request must not crash."""
    bad = call_op({"op": "read", "path": os.path.join(tmp, "nope.xlsx")})
    check("missing file returns ok=false", bad.get("ok") is False, repr(bad))
    check("missing file error is readable", "not found" in str(bad.get("error", "")).lower(),
          repr(bad))

    unknown = call_op({"op": "teleport", "path": "x.xlsx"})
    check("unknown op is rejected cleanly", unknown.get("ok") is False, repr(unknown))

    try:
        engine.parse_formats({"NotAColumn": "int"}, ["A", "B"])
    except ValueError as exc:
        check("bad format key raises ValueError", True, str(exc))
    except Exception as exc:  # noqa: BLE001
        check("bad format key raises ValueError", False, f"{type(exc).__name__}: {exc}")
    else:
        check("bad format key raises ValueError", False, "no error raised")


def test_zip_integrity(tmp: str) -> None:
    path = os.path.join(tmp, "zip.xlsx")
    run_op({"op": "create", "path": path, "title": "Probe",
            "metadata": [{"Ticker": "LYV"}]})
    with zipfile.ZipFile(path) as zf:
        check("zip has no corrupt entries", zf.testzip() is None)
        names = zf.namelist()
        for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                         "xl/styles.xml", "xl/sharedStrings.xml",
                         "xl/worksheets/sheet1.xml"):
            check(f"part present: {required}", required in names, repr(names))


def test_format_aliases(tmp: str) -> None:
    cases = {
        "currency": '"$"#,##0',
        "percent": "0.0%",
        "multiple": '0.0"x"',
        "date": "yyyy-mm-dd",
        "signed_pct": "+0.0%;-0.0%",
        "0.000": "0.000",
        "Custom Code": "Custom Code",
    }
    for alias, expected in cases.items():
        got = engine.resolve_format(alias)
        check(f"alias {alias!r}", got == expected, f"got {got!r}")


def test_negative_and_zero(tmp: str) -> None:
    path = os.path.join(tmp, "neg.xlsx")
    run_op({"op": "write", "path": path, "sheet": "N",
            "headers": ["Delta", "Zero", "Blank"],
            "rows": [[-0.0421, 0, None]],
            "formats": {"Delta": "signed_pct", "Zero": "int"}})
    row = xlsx.load_book(path).get("N").rows[1]
    check("negative preserved", abs(row[0] + 0.0421) < 1e-12, repr(row[0]))
    check("zero is zero, not blank", row[1] == 0, repr(row[1]))
    check("blank stays None", row[2] is None, repr(row[2]))


def main(argv=None) -> int:
    global _VERBOSE
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _VERBOSE = args.verbose

    tmp = tempfile.mkdtemp(prefix="xlsx-selftest-")
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                if _VERBOSE:
                    print(name)
                try:
                    fn(tmp)
                except Exception as exc:  # noqa: BLE001
                    _FAILURES.append(f"{name} raised {type(exc).__name__}: {exc}")
                    print(f"  FAIL {name} raised {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_PASSES} checks passed, {len(_FAILURES)} failed")
    for failure in _FAILURES:
        print(f"  - {failure}")
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
