from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone

# Host-side web access for research sub-agents.
#
# Design rule: a tool result must never look like a successful answer when the
# fetch actually failed. Every outcome carries an explicit [OK] / [EMPTY] /
# [ERROR] status line, plus retrieved/published dates, so the model can tell
# "the page is empty" from "the scraper failed" and act on the difference.
#
# Second rule, added for the evidence ledger (evidence.py): the status marker
# must be the FIRST thing in the result and the URL must appear on a line that
# starts with "Source:" or "URL:". The ledger parses those two things, so they
# are a contract, not decoration. Keep the markers and the URL lines stable.
#
# The sandbox runs with --network none, so web calls happen here in the trusted
# host process. `crw` is an optional local search/scrape binary; if it is absent
# the tools degrade to an explicit [ERROR] instead of crashing the app.

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _crw_path() -> str | None:
    path = shutil.which("crw")
    if path:
        return path
    candidates = glob.glob("/home/*/.local/bin/crw") + glob.glob("/usr/local/bin/crw")
    return candidates[0] if candidates else None


def _run(args: list[str], *, local: bool = False) -> str:
    path = _crw_path()
    if not path:
        raise RuntimeError("Web tools unavailable: `crw` binary not found on host.")
    env = {**os.environ, "CRW_LOCAL": "1"} if local else os.environ.copy()
    result = subprocess.run(
        [path, *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=env,
    )
    return result.stdout


def _looks_js_shell(text: str) -> bool:
    """Heuristic: a body with almost no prose but script/framework markers.

    An IR page that renders client-side returns a shell, not the content. Flag
    it so the model does not read 'few words' as 'nothing to report'.
    """
    lowered = text.lower()
    markers = ("<script", "window.__", "next_data", "__nuxt", "id=\"root\"",
               "id=\"app\"", "hybrid", "react", "vue")
    return len(text.split()) < 60 and any(m in lowered for m in markers)


def web_search(query: str) -> str:
    with _lock:
        try:
            data = json.loads(_run(["search", "--format", "json", query], local=True))
        except RuntimeError as exc:
            return f"[ERROR] {exc}"
        except subprocess.TimeoutExpired:
            return f"[ERROR] Search timed out for '{query}'."
        except subprocess.CalledProcessError as exc:
            return f"[ERROR] Search failed: {exc.stderr or exc}"
        except Exception as exc:
            return f"[ERROR] Search error: {exc}"

        if isinstance(data, dict) and "error" in data:
            return f"[ERROR] Search error: {data['error']}"

        results = data if isinstance(data, list) else data.get(
            "results", data.get("items", [])
        )
        results = results[:5]
        if not results:
            return (
                f"[EMPTY] No results for '{query}'. Absence of results is not "
                f"evidence of absence; rephrase or try a different angle."
            )

        retrieved = _now()
        blocks = []
        for r in results:
            url = r.get("url", "")
            body = str(r.get("markdown", r.get("content", "")))[:1000]
            published = (
                r.get("published") or r.get("published_date")
                or r.get("date") or "unknown"
            )
            blocks.append(
                f"[SNIPPET] This is a truncated snippet, not the page, and the "
                f"'Published' date may belong to a PRIOR period. Do not state a "
                f"fact from here as current without fetching the source.\n"
                f"Source: {url}\n"
                f"Published: {published}\n"
                f"Retrieved: {retrieved}\n"
                f"{body}"
            )
        return "\n\n".join(blocks)


def _scrape(url: str) -> dict:
    return json.loads(_run(["scrape", "--format", "json", url]))


def fetch_page(url: str) -> str:
    with _lock:
        try:
            data = _scrape(url)
        except Exception as exc:
            return (
                f"[ERROR] Fetch failed for {url}: {exc}\n"
                f"URL: {url}\n"
                f"A failed fetch is NOT an empty page. Switch method (JSON/API "
                f"endpoint, EDGAR exhibit, alternate URL) before concluding."
            )

        content = (
            data.get("markdown") or data.get("content")
            or data.get("text") or data.get("html") or ""
        )
        status = data.get("status") or data.get("status_code")
        header = f"[OK] {url} retrieved {_now()}" + (f" status={status}" if status else "")

        if not content:
            return (
                f"[EMPTY] {url} at {_now()}: fetch succeeded but returned no "
                f"readable content. Most likely JavaScript-rendered. This is a "
                f"RETRIEVAL FAILURE, not a statement that the page is empty. "
                f"Try the print/AMP/rss variant, the underlying JSON endpoint "
                f"(fetch_json), a primary filing, or an alternate URL.\n"
                f"URL: {url}"
            )

        note = ""
        if _looks_js_shell(content):
            note = ("\n[WARNING] Content looks like a JS app shell — treat as "
                    "NOT fully retrieved.")

        return f"{header}{note}\nURL: {url}\n{content[:8000]}"


def fetch_json(url: str) -> str:
    """Fetch a structured endpoint (EDGAR, XBRL company facts, APIs).

    Reuses the same scrape call but returns the raw body, because structured
    endpoints are the correct fallback when a rendered page returns an empty
    shell. Kept separate from fetch_page so the model reaches for it by name.
    """
    with _lock:
        if not _crw_path():
            return "[ERROR] Web tools unavailable: `crw` binary not found on host."
        try:
            data = _scrape(url)
        except Exception as exc:
            return f"[ERROR] Fetch failed for {url}: {exc}\nURL: {url}"
        body = (
            data.get("content") or data.get("text")
            or data.get("markdown") or data.get("html")
        )
        if not body:
            # Some crw builds return the raw body directly rather than a dict.
            body = json.dumps(data) if data else ""
        if not body.strip():
            return (f"[EMPTY] {url} at {_now()} returned no body; verify the "
                    f"endpoint exists and is not JS-gated.\nURL: {url}")
        return f"[OK] {url} retrieved {_now()}\nURL: {url}\n{body[:8000]}"


TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Results are dated SNIPPETS: a pointer to "
                       "the source, not the source. The 'Published' date may be a "
                       "PRIOR period, so never state a snippet's fact as current "
                       "without fetching the source. Results carry explicit "
                       "[SNIPPET]/[EMPTY]/[ERROR] status.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Fetch the markdown content of a webpage by URL. Returns an "
                       "explicit [OK]/[EMPTY]/[ERROR] status. An [EMPTY] or [ERROR] "
                       "result is a RETRIEVAL FAILURE, not an empty page — switch "
                       "method (fetch_json, EDGAR exhibit, alternate URL) rather "
                       "than concluding the page says nothing.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "fetch_json",
        "description": "Fetch a raw JSON/XML/API endpoint (EDGAR submissions, XBRL "
                       "company facts, gov endpoints). Use when a page fetch returns "
                       "an empty JS shell.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
]

HANDLERS = {
    "web_search": web_search,
    "fetch_page": fetch_page,
    "fetch_json": fetch_json,
}
