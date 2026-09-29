# Excel conventions

This is the authority on how workbooks are built with the `excel_*` tools: sheet
layout, number formats, naming, and what "done" means. Read it once before your first
`excel_*` call in a task. If anything here contradicts your instinct, this file wins;
if it contradicts the tool output, the tool output wins and you should say so.

---

## 1. Use the tools, not openpyxl

`excel_create`, `excel_write_sheet`, `excel_edit_cells`, `excel_read`,
`excel_list_sheets`. That is the supported path.

The sandbox has no network, so openpyxl cannot be installed and a `pip install` will
fail. `run_command` with a `python3` one-liner is an escape hatch for the rare case the
tools genuinely cannot express, not a default. Reaching for it to write a workbook the
tools already write is a defect.

The engine ships into the container at call time, so the tools and the file format stay
in step. A workbook written by hand is not.

---

## 2. Sheet layout

| sheet | contents |
| --- | --- |
| `Cover` | title, metadata, created date. `excel_create` writes it; do not rewrite it. |
| `Summary` | the answer: headline figures, the few numbers a reader should take away. |
| `Data_<topic>` | one sheet per source or topic of raw compiled data. |
| `Calcs_<topic>` | derivations, intermediate steps, anything a reader must be able to retrace. |
| `Sources` | one row per source: what it is, where it came from, as-of date. |

Not every workbook needs all of them. `Data_` and `Sources` are the ones that are
almost always required, because without them the numbers are not traceable.

`Calcs_` exists because **derivations must be shown, not asserted**. If a figure on
`Summary` is computed from two columns on `Data_`, the arithmetic belongs on a `Calcs_`
sheet where a reader can follow it. A number with no visible derivation is not finished
work, however correct it is.

---

## 3. Number formats

**Every numeric column gets a format. A column of bare numbers is unfinished work.**

Pass them explicitly per column, keyed by header, letter, or 0-based index:

| alias | use for |
| --- | --- |
| `currency` | dollar amounts |
| `usd2` | dollar amounts needing cents shown |
| `percent` / `percent2` | rates, growth, margins |
| `signed_pct` | changes where the sign matters |
| `multiple` | valuation multiples like 12.4x |
| `number` / `number2` | plain counts and ratios |
| `int` | whole-number counts |
| `signed_num` | deltas where the sign matters |
| `date` | dates |
| `text` | identifiers that must not be parsed as numbers |

Numeric-looking strings (`'1,250'`, `'31.8%'`, `'$12'`) are coerced to numbers on write,
which is convenient and also a trap: a ticker or an account code that looks numeric will
be coerced too. Pass those columns as `text` explicitly.

---

## 4. Naming

Sheets: the table in §2. Names are stable and are how a reader navigates; do not rename
a sheet on a later write without a reason.

Files: name a workbook for its subject and period (`LYV_Q3_2026_metrics.xlsx`), not for
its version (`dashboard_v4_final.xlsx`).

Columns: lead with the identifier, then the measure, then the period
(`Ticker`, `Revenue_Growth_YoY`, `FY2025`). A reader scanning a header row should not
have to open a cell to learn what it is.

---

## 5. Reported versus estimated

Where a number is a forecast, an assumption, or your own derivation rather than a
reported figure, it must be visibly distinguishable from reported data. Use the
house colouring and label it; never let a modelled number sit in the same visual
register as a filed one.

The distinction is not cosmetic. A reader must be able to tell, at a glance and without
reading a caveat, which cells are facts and which are someone's model.

---

## 6. Sources

Every load-bearing number needs a source and an as-of date on the `Sources` sheet.

- Prefer primary sources: regulatory filings, company disclosures, government
  statistics.
- **Never** present analyst estimates, price targets or consensus as evidence.
- **Never** infer a recommendation or a price target.
- If a figure cannot be sourced primary, report the gap. Do not fill it with a
  plausible number.

A workbook someone else can reproduce is the deliverable. Not a narrative, not a
recommendation, not a view.

---

## 7. Before reporting done

Run `excel_read` on the finished file and confirm:

- the sheets you intended exist, and no others
- headers match what you meant to write
- row counts are what you expect
- every numeric column carries a format (an unformatted column is the most common defect)
- the `Sources` sheet covers every load-bearing figure

Report the path and what you verified. "Done" without a read-back is a claim, not a
result.

---

## 8. Where files go

Workbooks go under `/workspace/write/rootN`. **Never** write a workbook into the
knowledge directory, and never write into a read-only root.
