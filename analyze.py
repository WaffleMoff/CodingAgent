"""Load the tick JSONL logs into a dataframe for analysis.

Handles the whole-run directory tree (ticklog/YYYY-MM/ticks_YYYYMMDD.jsonl),
so you point it at the ticklog folder and get one table back.

Backend choice, in order: DuckDB (best for the ~tens-of-millions-of-rows this
produces), Polars, pandas, then a dependency-free stdlib reader. duckdb/polars
stream from disk without loading everything into memory; pandas does not, and
will struggle past a few million rows. The stdlib reader is the guaranteed
floor -- it always works, in plain dicts, even with nothing installed.

Usage:
    from analyze import load_ticks
    df = load_ticks("ticklog")                       # everything
    df = load_ticks("ticklog", where="ticker='KXBTC15M-...'")
    df = load_ticks("ticklog", columns=["ts","prob","market_p","quotable"])

    # or run as a script for a quick look:
    python3 analyze.py ticklog
"""

from __future__ import annotations

import glob
import json
import os


def _files(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    pats = [
        os.path.join(root, "**", "*.jsonl"),
        os.path.join(root, "*.jsonl"),
    ]
    files = sorted({f for p in pats for f in glob.glob(p, recursive=True)})
    if not files:
        raise FileNotFoundError(f"no .jsonl files under {root!r}")
    return files


def _load_stdlib(files, columns):
    """Dependency-free reader: list of dicts. The guaranteed fallback."""
    rows = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if columns:
                    rec = {c: rec.get(c) for c in columns}
                rows.append(rec)
    return rows


def load_ticks(root: str = "ticklog", columns=None, where: str | None = None):
    """Return tick rows from `root` (dir or single file).

    Backend is chosen automatically; each is tried in order and the first that
    is installed wins. Returns whatever that backend's native table type is
    (duckdb -> DataFrame, polars -> DataFrame, pandas -> DataFrame, stdlib ->
    list of dicts).

    columns : sequence[str] | None  -- project only these columns
    where   : SQL predicate string   -- DuckDB backend only; ignored otherwise
    """
    files = _files(root)

    try:
        import duckdb
        sel = ", ".join(columns) if columns else "*"
        q = f"SELECT {sel} FROM read_json_auto(?, format='newline_delimited')"
        if where:
            q += f" WHERE {where}"
        return duckdb.connect().execute(q, [files]).df()
    except ImportError:
        pass

    try:
        import polars as pl
        df = pl.scan_ndjson(files, infer_schema_length=10_000)
        if columns:
            df = df.select(columns)
        if where:
            df = df.filter(where)
        return df.collect()
    except ImportError:
        pass

    try:
        import pandas as pd
        df = pd.concat([pd.read_json(f, lines=True) for f in files],
                       ignore_index=True)
        if columns:
            df = df[columns]
        return df
    except ImportError:
        return _load_stdlib(files, columns)


def _col(df, name):
    """Best-effort column extraction across backends for summarize()."""
    if isinstance(df, list):
        return [r.get(name) for r in df]
    return list(df[name])


def summarize(df) -> None:
    """Cheap sanity summary: shape, time span, gate reasons, runs, null rate."""
    n = len(df)
    backend = "stdlib/list" if isinstance(df, list) else type(df).__module__
    print(f"backend={backend}")
    print(f"rows={n:,}")
    if n == 0:
        return

    try:
        ts = [x for x in _col(df, "ts") if x is not None]
        if ts:
            print(f"span={max(ts) - min(ts):,.0f}s ({(max(ts) - min(ts)) / 3600:,.2f}h)")
    except Exception:
        pass

    for col in ("run_id", "ticker", "window_expiry", "withhold_reason",
                "sigma_branch", "prob_branch"):
        try:
            vals = _col(df, col)
        except Exception:
            continue
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
        print(f"\n{col}:")
        for k, v in top:
            print(f"  {k}: {v:,}")

    try:
        prob = _col(df, "prob")
        miss = sum(1 for p in prob if p is None)
        print(f"\nprob null: {miss:,} / {n:,} ({100 * miss / n:.2f}%)")
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "ticklog"
    summarize(load_ticks(root))
