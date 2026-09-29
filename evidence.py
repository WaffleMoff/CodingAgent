# evidence.py
"""The research evidence ledger.

The failure this module fixes: the old harness verified a research answer by
regex over the *rendered prose* — every line had to end in
"[source: ...; as-of: ...]". But the web tools never emitted that string, so a
sub-agent could fetch a page correctly and still be rejected forever, and an
answer with zero real fetches could pass by writing the tag by hand. The check
measured formatting, not truth.

The fix is to separate collection from presentation:

  - While the sub-agent works, every web tool call is recorded as an Evidence
    record with a status (ok / empty / error), the URL, the retrieval date, and
    the body digest. This is what the agent ACTUALLY inspected.
  - The final answer is judged against that ledger: does every cited URL
    correspond to a source the agent really retrieved? did it reach at least one
    source at all? That is a coverage check, not a string check.

An EvidenceLog is a plain list of dataclasses with no dependency on the LLM, the
agent loop, or the web layer, so it can be unit-tested and reused by any future
verifier (a second model, a citation parser, an audit report) without change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tool-result status markers emitted by web.py. These are the machine-readable
# contract between the web tools and the verifier.
OK = "ok"
EMPTY = "empty"
ERROR = "error"

_STATUS_RE = re.compile(r"^\[(OK|SNIPPET|EMPTY|ERROR)\]", re.IGNORECASE)
_URL_RE = re.compile(r"^(?:Source|URL):\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class Evidence:
    """One thing the sub-agent actually looked at."""

    tool: str                 # web_search | fetch_page | fetch_json
    status: str               # OK | EMPTY | ERROR
    url: str = ""             # set for fetch_* and for the snippets in a search
    retrieved: str = ""       # YYYY-MM-DD the tool ran
    summary: str = ""         # first non-empty line of the body, for the log

    @property
    def retrieved_ok(self) -> bool:
        return self.status == OK


@dataclass
class EvidenceLog:
    """Accumulates evidence across the sub-agent's tool calls."""

    records: list[Evidence] = field(default_factory=list)

    def record(self, tool: str, result: str) -> Evidence | None:
        """Parse one tool result and append what it actually contains.

        A web_search yields one record per snippet URL (all status OK if the
        search itself succeeded); the fetch tools yield a single record. A
        result that carries no recognized marker is ignored rather than
        guessed at.
        """
        if not isinstance(result, str) or not result:
            return None

        head = _STATUS_RE.match(result.lstrip())
        if head is None:
            return None
        marker = head.group(1).upper()

        if marker == "SNIPPET":
            # One search result may bundle several snippets; record each URL.
            last = None
            for block in result.split("[SNIPPET]"):
                url_match = _URL_RE.search(block)
                if not url_match:
                    continue
                summary = next(
                    (ln.strip() for ln in block.splitlines()
                     if ln.strip() and not ln.startswith(("Source:", "Published:", "Retrieved:"))),
                    "",
                )
                last = Evidence(tool=tool, status=OK, url=url_match.group(1),
                                summary=summary[:200])
                self.records.append(last)
            return last

        status = {"OK": OK, "EMPTY": EMPTY, "ERROR": ERROR}[marker]
        url_match = _URL_RE.search(result)
        summary = next(
            (ln.strip() for ln in result.splitlines()
             if ln.strip() and not ln.startswith(("[", "Source:", "URL:"))),
            "",
        )
        record = Evidence(
            tool=tool,
            status=status,
            url=url_match.group(1) if url_match else "",
            summary=summary[:200],
        )
        self.records.append(record)
        return record

    # -- queries the verifier asks -------------------------------------------

    @property
    def retrieved(self) -> list[Evidence]:
        return [r for r in self.records if r.retrieved_ok and r.url]

    @property
    def retrieved_urls(self) -> set[str]:
        return {r.url for r in self.retrieved}

    def has_any_retrieval(self) -> bool:
        """True if a fetch returned a body, as opposed to only search snippets."""
        return any(r.retrieved_ok and r.tool in ("fetch_page", "fetch_json")
                   for r in self.records)

    def has_urls(self) -> bool:
        return bool(self.retrieved_urls)

    def render(self) -> str:
        """Compact ledger for feeding back to the model as context."""
        if not self.records:
            return "(no evidence collected: no web tool call returned a recognized status)"
        lines = []
        for r in self.records:
            mark = {OK: "RETRIEVED", EMPTY: "EMPTY", ERROR: "ERROR"}[r.status]
            where = r.url or "(no url)"
            lines.append(f"- [{mark}] {r.tool} {where}")
        return "\n".join(lines)
