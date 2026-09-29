# Equity research and SEC-filing data compilation

Domain playbook for tasks covering **public companies and their filings**. This is the
authority on the equity-specific layer: which sources rank where, how to fetch from EDGAR
and government endpoints, and the traps that only appear when you read filings.

It **augments** general research discipline, it does not replace it. The general rules —
name your evidence or keep going, absence of evidence is not evidence of absence, change
one variable on an empty search, prefer primary to secondhand, resolve undated questions
against today — are always in force from the system prompt. Read this file when the task
concerns a public company or its filings; there is no reason to read it for other work.

If anything here contradicts your instinct, this file wins; if it contradicts a source you
have actually retrieved, the source wins and you should say so.

---

## 1. What the deliverable is

A workbook someone else can reproduce, with every load-bearing number traceable to a
primary source and its derivation shown.

It is **not** a narrative, **not** a recommendation, and **not** a view. If you find
yourself writing a paragraph that argues a position, stop: that is not the output, and
producing it is a failure mode, not a bonus.

---

## 2. Source hierarchy

Prefer sources in this order, and say which tier a figure came from when it matters:

1. **Regulatory filings** — 10-K, 10-Q, 8-K, S-1, DEF 14A, and their exhibits. The
   audited or filed record. Highest tier.
2. **XBRL company facts** — structured figures as filed with the SEC. Machine-readable
   and directly traceable to a filing. Use this over a PDF when both exist.
3. **Company disclosures** — earnings releases, investor decks, IR pages. Filed or
   published by the company but not always audited.
4. **Government statistics** — BLS, BEA, Census, FRED, EIA. Primary for macro.
5. **Everything else** — treat as a pointer to a primary source, never as the source
   itself.

**Never** present analyst estimates, price targets or consensus as evidence. They are
opinion, not fact, and a workbook built on them is not reproducible. If a task appears
to require consensus figures, say that the consensus is not a primary source and report
what you can source instead.

---

## 3. Every number gets a source and an as-of date

On the `Sources` sheet: what the figure is, where it came from, and the date it was
retrieved or the period it covers.

If a figure cannot be sourced primary, **report the gap**. Do not fill it with a
plausible number, do not carry one over from a secondary summary, and do not estimate
silently. A stated gap is a useful result; a fabricated figure poisons everything
downstream of it.

Where you do compute something — a growth rate, a margin, a multiple — the arithmetic
belongs on a `Calcs_` sheet, not just its result on `Summary`.

---

## 4. Fetching

EDGAR and most government endpoints are rate-limited and expect a declared identity.
Respect the published limits, space requests, and do not hammer an endpoint because a
first attempt returned something unexpected.

Where `research` is available, it runs on the host with web access and returns a
cited summary. Delegate rather than guess, and prefer it over reconstructing a fact
from memory. When it returns a figure, the citation is part of the figure — carry the
citation into the `Sources` sheet, do not drop it.

The sandbox itself has no network. Anything requiring a fetch happens either through
`research` or through a host-side tool.

---

## 5. Build order

1. Establish what the question actually is, and what would answer it. If the request is
   ambiguous in a way that changes the data you would pull, ask before pulling.
2. Inventory the primary sources that exist for this subject. Note what does **not**
   exist as early as what does.
3. Pull and stage the raw figures into `Data_<topic>` sheets, with the source recorded
   as you go rather than reconstructed at the end.
4. Derive. Put the working on `Calcs_<topic>`.
5. Assemble `Summary` last, from figures that are already sourced and already derived.

Building `Summary` first and back-filling sources is how a workbook ends up with a
number nobody can trace.

---

## 6. Known traps

- **Fiscal versus calendar periods.** A company's FY2025 is usually not calendar 2025.
  Label which convention every period column uses.
- **Restated figures.** Use the latest filing's figure, and if a prior figure was
  restated, say so rather than silently switching.
- **Adjusted versus GAAP.** These are different numbers with the same name. Label
  which one a column is. Never mix them in one column.
- **Units.** Thousands, millions, billions. State it in the header, not in a note.
- **Share counts and per-share figures.** Basic versus diluted changes the answer;
  label it.
- **XBRL tagging differences.** The same economic concept can sit under different tags
  across filers, and across years for one filer. Check the tag, not just the label.
- **Deriving from a prior derivation.** If a figure came from another workbook of
  yours, it is not sourced. Go back to the filing.

---

## 7. Reporting

- State what you compiled, from which sources, as of which dates.
- Quote figures as they appear in the source; do not round silently in a way that
  changes meaning.
- Distinguish "the source states X" from "I derived X".
- Report what you could not find. Gaps are findings.
- Do not state or imply a recommendation, a price target, or a view on whether any
  security is attractive.
