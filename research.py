# research.py
"""Research sub-agent, exposed to the coding agent as the `research` tool.

Stateless: it does not touch the coding session's history, sandbox, or files.
It runs a nested `run_agent` loop with host-side web tools and returns a
concise, validated fact summary.

WHAT CHANGED AND WHY
--------------------
The previous version verified the answer by regex over the rendered prose: every
line had to end in "[source: <url>; as-of: <date>]". Two things were wrong with
that, and both are visible in the failing transcripts:

  1. The web tools never emitted that string, so a sub-agent that fetched a page
     correctly and quoted it accurately still failed the check on every line. It
     was measuring a format, not a fact.
  2. The check never asked whether anything had been retrieved. An answer with
     zero real fetches passed if the model typed the tag by hand; an answer built
     on two real fetches failed. That is the inverse of the goal.

The rewrite moves verification onto an EvidenceLog (evidence.py) that the tool
results populate as the agent works. The answer is now judged on COVERAGE:

  - Did the sub-agent retrieve at least one source, not just search snippets?
  - Does every URL the answer cites correspond to a source it actually retrieved?

The output contract (every factual line carries a source and an as-of date) is
kept, because a downstream reader needs it, but it is enforced as a NARROW check
on lines the model chose to present as factual, not as a blanket rule over every
line of prose. Lines that report a gap ("could not confirm") stay exempt and are
still a positive result.

Termination is explicit. If the loop runs out of iterations or is cancelled, the
result is returned as a budget failure, not gate-checked and not dressed up as a
content verdict.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from config import RESEARCH_MAX_ITERATIONS, RESEARCH_VERIFY_PASSES
from agent import run_agent
from evidence import EvidenceLog
from web import HANDLERS as WEB_HANDLERS, TOOLS

_SYSTEM = """You are a research sub-agent. A coding agent has delegated a
question to you. Find primary-source facts and return a concise summary.

Priorities: intellectual honesty, objectivity, and finding ALL relevant
primary-source information. Not everything on the internet is true — validate
each load-bearing fact against a source you actually retrieved.

Your web tools are instrumented. Read the status on every result:
- [SNIPPET] is a truncated search snippet, NOT the page. Its "Published" date
  may be from a PRIOR period. Never state a fact from a snippet as current
  without fetching the source URL.
- [EMPTY] and [ERROR] are RETRIEVAL FAILURES, not empty pages. Switch method
  (fetch_json for API/EDGAR endpoints, alternate URL, primary filing) and retry.
- Report which of the three states you reached for each thing you looked for:
  (a) found and retrieved, (b) exists but not retrievable, (c) no evidence
  found after trying distinct angles. Never collapse (b) or (c) into "does not
  exist".

Search iteratively:
  1. Decide what information you need.
  2. Search for it.
  3. Validate it (open the source, do not trust the snippet).
  4. Assess whether you have a holistic picture. If not, return to step 1.

Completeness:
- A task is done only when you can name the concrete evidence you inspected
  (a document, source, file, or result). If you cannot name what you looked
  at, keep going.
- Never conclude anything — positive or negative — from a secondhand source
  when a primary source is reachable. Read the original first.
- Prefer primary sources over summaries, aggregators, or restatements.

Empty results and negatives:
- Absence of evidence is not evidence of absence. When something should exist
  or is expected to occur (a record, event, filing, file, scheduled
  occurrence), an inconclusive search means "I could not confirm this, here is
  where it would be" — never "this does not exist" or "this is not happening."
- Treat the requester's observation that something exists or occurred as a
  signal your search was wrong, not as something to explain away.
- Before any answer containing "no", "none", "not found", or "does not
  exist", run this gate: What did I actually inspect? Which distinct
  approaches did I try? Am I reporting that I found nothing, or that nothing
  exists? If you cannot name what you inspected, keep searching. State the
  sources, queries, or methods you tried.

Scheduled and future events:
- An artifact that only exists after an event cannot confirm the event is
  scheduled. Search for the scheduling announcement — the press release that
  names a date, the events calendar, the docket entry, the appointment notice —
  not for what has not been created yet.
- Keep three states distinct and never collapse them: (a) event occurred and
  output is available; (b) event scheduled but not yet occurred; (c) no
  evidence found after trying distinct angles. State (b) is a positive
  confirmation, not a null.
- Undated questions resolve against today's date. "The earnings call", "the
  filing", "the launch" means the one at or near today, not the most recent on
  file. If the figure you are about to give is from a prior period, say so or
  fail loudly — never present a stale artifact as the answer to a current
  question. A prior period's event (e.g. last year's call on the same weekday)
  is the single most dangerous source of a plausible-but-wrong date or time:
  if you derive a schedule from an analogous prior period, label it explicitly
  as an inference, not a fact.

When a search comes up empty, change one variable and retry — do not repeat
the same query. Cycle through: wrong input, wrong source, wrong method, wrong
framing, wrong scope or time. Log which variable you changed each pass.

Output contract (enforced — an answer that breaks it is rejected and you are
asked again):
- Every line that states a fact MUST end with a source in the form
  "[source: <url or document>; as-of: YYYY-MM-DD]".
- Every fact is date-stamped. If you cannot date it, mark it "as-of: unknown"
  and label it unverified.
- If a fact is derived from a prior-period analogue rather than a current
  primary source, write "INFERENCE:" at the start of the line.
- Rules:
  - Prefer primary sources over marketing, speculation, or opinion. Never treat
    analyst estimates, consensus, or forecasts as evidence.
  - Reply with a concise list of facts and sources. No internal reasoning.
"""


# A citation tag: [source: ...; as-of: ...]. Tolerant of spacing and case.
_CITATION = re.compile(r"\[source:\s*([^;\]]+);\s*as-of:\s*([^\]]+)\]", re.IGNORECASE)

# A URL anywhere in a citation's source slot.
_URL_IN_CITATION = re.compile(r"https?://[^\s\]>,;]+")

# Lines that are structural, not factual claims: headings, blanks, the
# three-state status labels, and explicit non-findings. They are exempt.
_EXEMPT = re.compile(
    r"^\s*$"
    r"|^\s*#"
    r"|^\s*[-*]?\s*(no evidence found|could not confirm|not found|unknown)",
    re.IGNORECASE,
)


def _line_claims_fact(line: str) -> bool:
    """A line is a factual claim if it has substance it isn't exempting itself from."""
    stripped = line.strip()
    if len(stripped) < 3:
        return False
    return not _EXEMPT.match(line)


def _verify_format(answer: str) -> str | None:
    """Narrow format check: factual lines must carry a citation.

    This is the ORIGINAL check, kept but demoted. It is no longer the thing that
    decides pass/fail on its own — `_verify` below combines it with the evidence
    ledger so a well-sourced answer that paraphrases its citation is not rejected
    for a missing bracket.
    """
    if not answer or not answer.strip():
        return "The answer was empty."

    uncited = [ln.strip()[:160] for ln in answer.splitlines()
               if _line_claims_fact(ln) and not _CITATION.search(ln)]
    if uncited:
        shown = "\n".join(f"  - {u}" for u in uncited[:8])
        more = f"\n  (+{len(uncited) - 8} more)" if len(uncited) > 8 else ""
        return (
            "These factual lines carry no citation. Every factual line must end "
            "with [source: <url or document>; as-of: YYYY-MM-DD]. If a line has "
            "no source, either source it or move it to an explicit "
            "'could not confirm' statement:\n" + shown + more
        )
    return None


def _verify(answer: str, log: EvidenceLog) -> str | None:
    """Return None if the answer is acceptable, else the reason to retry.

    Order matters: coverage first (did the agent actually look at anything?),
    then format (are the presented facts cited?), because the transcripts show
    the failure mode is an answer that cites nothing because it retrieved nothing
    usable — and the fix for that is more retrieval, not more brackets.
    """
    if not answer or not answer.strip():
        return "The answer was empty."

    # 1. Coverage: at least one source must have been fetched and returned a
    #    body. Search snippets alone are pointers, not sources.
    if not log.has_any_retrieval():
        if not log.has_urls():
            return (
                "No source was retrieved. You did not fetch any page, so there "
                "is nothing to base a fact on. Use web_search to find candidate "
                "URLs, then fetch_page or fetch_json to open them. If retrieval "
                "genuinely failed, say what you tried and which sources remain "
                "unretrievable — but do not answer as if a search succeeded."
            )
        return (
            "You have only search snippets, no fetched source. A snippet is a "
            "pointer, not the source. Fetch at least one of the URLs you found "
            "(fetch_page, or fetch_json for an API/EDGAR endpoint) before "
            "stating a fact from it."
        )

    # 2. Citation ↔ evidence: every cited URL must be one the agent really
    #    retrieved. This is the check that makes a fabricated citation fail and
    #    a paraphrased one pass.
    retrieved = log.retrieved_urls
    unknown = []
    for line in answer.splitlines():
        citation = _CITATION.search(line)
        if not citation:
            continue
        for url in _URL_IN_CITATION.findall(citation.group(1)):
            if url not in retrieved and not any(url in r or r in url for r in retrieved):
                unknown.append(url)
    if unknown:
        shown = "\n".join(f"  - {u}" for u in unknown[:5])
        return (
            "These cited URLs were never retrieved by any tool call. A citation "
            "must point to a source you actually opened:\n" + shown +
            "\nFetch each one, or drop the claim."
        )

    # 3. Format: what the model presented as fact must be cited.
    return _verify_format(answer)


def research(
    query: str,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> str:
    log = EvidenceLog()

    # Wrap the web handlers so every tool result feeds the ledger. This is the
    # collection step: evidence is recorded as the agent works, not reconstructed
    # from the prose afterwards.
    def recording_handlers() -> dict[str, Callable[..., str]]:
        wrapped: dict[str, Callable[..., str]] = {}
        for name, handler in WEB_HANDLERS.items():
            def make(fn: Callable[..., str], tool: str) -> Callable[..., str]:
                def call(*args: Any, **kwargs: Any) -> str:
                    result = fn(*args, **kwargs)
                    log.record(tool, result)
                    return result
                return call
            wrapped[name] = make(handler, name)
        return wrapped

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": query},
    ]

    answer = run_agent(
        messages,
        TOOLS,
        RESEARCH_MAX_ITERATIONS,
        recording_handlers(),
        event_callback=event_callback,
    )

    # Budget failures are not content failures. Return them as such.
    if answer.strip() in (
        "Agent exceeded maximum iterations.",
        "Agent cancelled.",
    ):
        return (
            f"[BUDGET EXHAUSTED — the sub-agent stopped before producing an "
            f"answer: {answer.strip()}]\n\n"
            f"Evidence collected before stopping:\n{log.render()}"
        )

    # Mechanical verification gate. If the answer breaks the contract, feed the
    # violation AND the evidence ledger back so the sub-agent can see what it
    # actually has, then let it fix the answer or go get more.
    for _ in range(RESEARCH_VERIFY_PASSES):
        problem = _verify(answer, log)
        if problem is None:
            break
        messages.append({"role": "assistant", "content": answer})
        messages.append({
            "role": "user",
            "content": (
                "Your previous answer was rejected by the output-contract check.\n\n"
                f"{problem}\n\n"
                "Here is the evidence you actually collected so far:\n"
                f"{log.render()}\n\n"
                "Revise. Cite the sources you retrieved; if a fact needs a source "
                "you have not fetched, fetch it now. For anything you cannot "
                "source, state it explicitly as 'could not confirm' rather than "
                "asserting it. Do not invent sources."
            ),
        })
        answer = run_agent(
            messages,
            TOOLS,
            RESEARCH_MAX_ITERATIONS,
            recording_handlers(),
            event_callback=event_callback,
        )
        if answer.strip() in (
            "Agent exceeded maximum iterations.",
            "Agent cancelled.",
        ):
            return (
                f"[BUDGET EXHAUSTED — the sub-agent stopped before producing a "
                f"revised answer: {answer.strip()}]\n\n"
                f"Evidence collected:\n{log.render()}"
            )
    else:
        # Loop exhausted without passing: return the answer with a visible flag
        # that names the actual failure, plus the ledger, so the downstream agent
        # can judge for itself instead of guessing.
        answer = (
            "[UNVERIFIED — output contract not satisfied after "
            f"{RESEARCH_VERIFY_PASSES} revision passes; treat with caution]\n"
            f"Evidence collected:\n{log.render()}\n\n"
            + answer
        )

    return answer
