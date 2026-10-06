"""Opening-quote placement and tick capture.

Two resting limit orders at the open of a contract: a YES bid at 30c and a NO
bid at 30c. The only orders this file ever sends.

Wire format: the V2 event-orders endpoint. Schema is CreateOrderV2Request
{ticker, side, count, price, time_in_force, self_trade_prevention_type,
client_order_id?} with side in {bid, ask} referring to the YES leg OF THE BOOK:
    side="bid" -> buy YES  at price
    side="ask" -> sell YES at price  ==  buy NO at (1 - price)
There is no yes_price / no_price / action / outcome_side on this request, so a
NO bid MUST go as an "ask" carrying the complementary YES-leg price:
    bid NO @30c  ->  side="ask", price="0.7000"
Sending side="bid", price="0.3000" twice would post the same YES bid twice and
buy no NO at all. The old V1 body (action/yes_price/no_price to
/portfolio/orders) is rejected with HTTP 410. Both legs rest; supplying
`ticker` routes to the market's shard, so exchange_index is omitted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from config import (
    OPENING_QUOTE_WINDOW_SEC, OPENING_QUOTE_YES_PRICE, OPENING_QUOTE_NO_PRICE,
    OPENING_QUOTE_COUNT, CAPTURE_TICK_HZ, CAPTURE_TICK_FIELDS, EVENT_BASE_URL, log,
)
from auth import create_headers
from state import capture, opening_quoted, opening_tasks, window_open_time
import state as st


def _leg(side: str, price_cents: int) -> tuple:
    """Map a desired leg + that leg's cents price to the V2 (side, price).

    `side` is the leg we want to BUY: "yes" or "no". `price_cents` is that
    leg's price in whole cents. The V2 request quotes everything from the YES
    leg, so buying NO at p is selling YES at (100 - p).
    """
    if side == "yes":
        return "bid", f"{price_cents / 100:.4f}"
    if side == "no":
        return "ask", f"{(100 - price_cents) / 100:.4f}"
    raise ValueError(f"unknown side {side!r}")


async def _post_order(session, ticker, side, price_cents, count):
    """POST one resting limit order via the V2 event-orders endpoint.

    Returns (status, body). See _leg() for the leg/price translation.
    """
    book_side, price = _leg(side, price_cents)
    order = {
        "ticker": ticker,
        "side": book_side,
        "count": str(count),
        "price": price,
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4()),
    }
    url = f"{EVENT_BASE_URL}/portfolio/events/orders"
    path = urlparse(url).path
    headers = create_headers("POST", path)
    async with session.post(url, headers=headers, json=order) as resp:
        body = await resp.json() if resp.status in (200, 201) else await resp.text()
        return resp.status, body


async def place_opening_quotes(ticker):
    """Place the contract's two opening quotes. Idempotent per ticker.

    Guarded by `opening_quoted` so a re-entrant call (a second tick arriving
    before this finishes) is a no-op; the ticker is marked before the awaits so
    the guard is airtight even under interleaving.
    """
    if ticker in opening_quoted:
        return
    opening_quoted.add(ticker)

    async with aiohttp.ClientSession() as session:
        for side, price_cents in (("yes", OPENING_QUOTE_YES_PRICE),
                                  ("no", OPENING_QUOTE_NO_PRICE)):
            try:
                status, body = await _post_order(
                    session, ticker, side, price_cents, OPENING_QUOTE_COUNT)
            except Exception as e:
                log("opening_quote_error", ticker=ticker, side=side,
                    error=f"{type(e).__name__}: {e}")
                continue
            ok = status in (200, 201)
            log("opening_quote", ticker=ticker, side=side, price_cents=price_cents,
                count=OPENING_QUOTE_COUNT, status=status, ok=ok,
                order_id=(body.get("order_id") if isinstance(body, dict) else None),
                result=(None if ok else body))
            capture.emit("quote", ticker=ticker, side=side,
                         price_cents=price_cents, count=OPENING_QUOTE_COUNT,
                         status=status, ok=ok,
                         order_id=(body.get("order_id") if isinstance(body, dict) else None),
                         result=(None if ok else body))
    log("opening_quotes_done", ticker=ticker)


def _maybe_place_opening_quotes(book) -> None:
    """Dispatch opening quotes if the contract is in its first minute.

    Called from the hot path, so it only inspects the clock and, at most once
    per contract, schedules a task. It never awaits. Cheap after the first
    call: `opening_quoted` membership short-circuits everything.
    """
    ticker = book.market_ticker
    if ticker in opening_quoted:
        return
    seconds_in = (datetime.now() - window_open_time()).total_seconds()
    if seconds_in > OPENING_QUOTE_WINDOW_SEC:
        # Past the first minute: remember it, so we stop re-checking every tick.
        opening_quoted.add(ticker)
        return
    task = asyncio.create_task(place_opening_quotes(ticker))
    opening_tasks.add(task)
    task.add_done_callback(opening_tasks.discard)
    log("opening_quotes_scheduled", ticker=ticker,
        seconds_into_window=round(seconds_in, 3))


def _capture_tick(row: dict, now: datetime) -> None:
    """Queue at most one tick row per (1 / CAPTURE_TICK_HZ) seconds.

    Runs on the hot path, so it does nothing but compare a clock reading and,
    at most once per interval, append one pre-built dict to a deque. The row is
    the dict price_tick() already produced, so capturing costs no extra
    pricing. The two datetime objects are formatted here rather than left to
    json.dumps so downstream readers see stable ISO strings.
    """
    if not capture.path or CAPTURE_TICK_HZ <= 0:
        return
    ts = now.timestamp()
    if ts - st.capture_last_tick_ts < 1.0 / CAPTURE_TICK_HZ:
        return
    st.capture_last_tick_ts = ts
    rec = {k: row.get(k) for k in CAPTURE_TICK_FIELDS}
    rec["clock"] = now.isoformat()
    rec["ts_iso"] = datetime.now(timezone.utc).isoformat()
    te, ce = row.get("ticker_expiry"), row.get("computed_expiry")
    rec["ticker_expiry"] = te.isoformat() if te else None
    rec["computed_expiry"] = ce.isoformat() if ce else None
    capture.emit("tick", **rec)
