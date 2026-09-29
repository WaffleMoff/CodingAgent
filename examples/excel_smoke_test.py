#!/usr/bin/env python3
"""Worked example: build an equity-research workbook through the excel_* tools.

Run this inside the agent sandbox to confirm the toolset works end to end before
asking the agent to do real work. It uses the same host tools the agent does, so a
pass here means the tools, the bridge, and the engine all agree.

    python3 examples/excel_smoke_test.py /workspace/write/root0

Expected output: a workbook written to the given root, plus a printed verification
report. Exit code 1 if any structural check fails.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)


def main(root: str) -> int:
    from excel import engine

    path = os.path.join(root, "LYV_smoke_2026-01-16.xlsx")
    print(f"target: {path}\n")

    steps = [
        ("create", {
            "op": "create",
            "path": path,
            "title": "LYV — venue economics smoke test",
            "metadata": [{"Ticker": "LYV"}, {"As Of": "2026-01-16"},
                         {"Source", "SEC EDGAR 10-K"}],
        }),
        ("write Summary", {
            "op": "write",
            "path": path,
            "sheet": "Summary",
            "headers": ["Metric", "FY2024", "FY2025", "% Change"],
            "rows": [
                ["Revenue ($M)", "23,180", "24,560", "5.95%"],
                ["Tickets Sold (M)", 480, 512, "6.67%"],
                ["Concerts", "44,341", "48,201", "8.71%"],
            ],
            "formats": {
                "FY2024": "number",
                "FY2025": "number",
                "% Change": "signed_pct",
            },
        }),
        ("write Sources", {
            "op": "write",
            "path": path,
            "sheet": "Sources",
            "headers": ["fact", "source_name", "source_url", "as_of_date"],
            "rows": [
                ["FY2025 consolidated revenue", "Form 10-K",
                 "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001335258",
                 "2026-02-20"],
                ["FY2025 tickets sold", "Form 10-K",
                 "https://data.sec.gov/api/xbrl/companyconcept/CIK0001335258/us-gaap/Revenues.json",
                 "2026-02-20"],
            ],
            "formats": {"as_of_date": "date"},
        }),
        ("write Calcs", {
            "op": "write",
            "path": path,
            "sheet": "Calcs_Growth",
            "headers": ["Input", "FY2024", "FY2025", "Value"],
            "rows": [
                ["Revenue ($M)", 23180, 24560, 0.0595],
                ["Tickets Sold (M)", 480, 512, 0.0667],
            ],
            "formats": {"FY2024": "int", "FY2025": "int", "Value": "signed_pct"},
        }),
        ("edit totals row", {
            "op": "edit",
            "path": path,
            "sheet": "Summary",
            "edits": [{"cell": "A5", "value": "Source: FY2025 Form 10-K",
                       "format": "text"}],
        }),
    ]

    for label, request in steps:
        reply = engine.OPS[request["op"]](request)
        status = "ok" if reply.get("ok") else f"FAILED {reply.get('error')}"
        print(f"  {label:22s} {status}")
        if not reply.get("ok"):
            return 1

    # verify the way the guide says to: read it back and check the structure
    from excel import xlsx

    book = xlsx.load_book(path)
    report = []
    report.append(("sheets present", book.names() == ["Cover", "Summary", "Sources", "Calcs_Growth"],
                   str(book.names())))

    summary = book.get("Summary")
    report.append(("header row correct",
                   summary.header == ["Metric", "FY2024", "FY2025", "% Change"],
                   str(summary.header)))
    metric_rows = [row for row in summary.data_rows()
                   if len(row) > 1 and isinstance(row[1], (int, float))]
    report.append(("no number stored as text",
                   len(metric_rows) == 3,
                   str([row[1] if len(row) > 1 else None
                        for row in summary.data_rows()])))
    report.append(("percent format applied",
                   summary.formats.get(3) == "+0.0%;-0.0%", str(summary.formats.get(3))))

    sources = book.get("Sources")
    report.append(("sources sheet populated", len(sources.data_rows()) >= 2,
                   f"{len(sources.data_rows())} rows"))
    report.append(("every source has a date",
                   len(sources.data_rows()) == 2
                   and all(len(row) > 3 and row[3] is not None
                           for row in sources.data_rows()),
                   str([row[3] if len(row) > 3 else None
                        for row in sources.data_rows()])))

    print()
    failures = 0
    for label, passed, detail in report:
        print(f"  {'ok  ' if passed else 'FAIL'} {label}: {detail}")
        failures += 0 if passed else 1

    print(f"\nworkbook at {path} ({os.path.getsize(path)} bytes)")
    print(f"{len(report) - failures}/{len(report)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
