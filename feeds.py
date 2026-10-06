"""Feeds, the hot-path tick handler, and the per-window market refresh.

The hot path is event_orderbook_websocket -> OrderBook.apply_* -> on_tick ->
price_tick. It is synchronous, does no await, no network, no disk I/O.
Everything expensive (sigma estimation, disk writes, rendering, order POSTs)
runs on its own timer/task.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import websockets

from config import (
    CFB_WS_URL, CFB_WS_PATH, EVENT_WS_URL, EVENT_WS_PATH, BRTI_INDEX_ID,
    BRTI_STALE_SEC, CFB_BACKOFF_BASE, CFB_BACKOFF_MAX, EVENT_BACKOFF_BASE,
    EVENT_BACKOFF_MAX, REFRESH_POLL_SEC, REFRESH_RETRY_SEC,
    REFRESH_RETRY_INTERVAL, WINDOW_TICK_INTERVAL, WITHHELD_LOG_INTERVAL_SEC,
    VENUE_PANEL, log,
)
from auth import _ws_connect, create_headers, fetch_event_markets_async
from book import new_event_book
from venues import VenueHub, publish_snapshots
from pricing import price_tick, _publish_prices, _note_switch_tick
from orders import _maybe_place_opening_quotes, _capture_tick
import state as st
from state import state, brti_buffer, venue_quotes, capture


def on_tick(book) -> None:
    """Priced once per applied delta, synchronously in the feed loop.

    Cheap by construction: the book has already cached its top of book, sigma
    is cached by sigma_task, and the rest is in-memory arithmetic. The only
    I/O is a stderr heartbeat, rate-limited, plus the one-shot rollover trace.
    """
    now = datetime.now()
    row = price_tick(book, state.brti_spot, now, now.timestamp())
    _publish_prices(row)
    _note_switch_tick(book, row)
    _capture_tick(row, now)
    _maybe_place_opening_quotes(book)

    if row["quotable"]:
        pass
    elif time.time() - st._last_withheld_log >= WITHHELD_LOG_INTERVAL_SEC:
        st._last_withheld_log = time.time()
        log("quote_withheld", ticker=book.market_ticker,
            reason=row["withhold_reason"], prob=row["prob"])


async def cfb_brti_websocket():
    """BRTI spot feed. Owns state.brti_spot / state.brti_ts / brti_buffer."""
    backoff = CFB_BACKOFF_BASE
    while True:
        headers = create_headers("GET", CFB_WS_PATH, for_websocket=True)
        connected_at = time.time()
        n_msgs = 0
        try:
            async with _ws_connect(CFB_WS_URL, headers) as websocket:
                state.cfb_connected = True
                backoff = CFB_BACKOFF_BASE
                await websocket.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["cfbenchmarks_value"], "index_ids": [BRTI_INDEX_ID]},
                }))
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("type") != "cfbenchmarks_value":
                        continue
                    m = data["msg"]
                    if m.get("index_id") != BRTI_INDEX_ID:
                        continue
                    try:
                        spot = float(json.loads(m["data"])["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    ts = time.time()
                    state.brti_spot = spot
                    state.brti_ts = ts
                    brti_buffer.append((ts, spot))
                    n_msgs += 1
        except websockets.ConnectionClosed as e:
            log("cfb_ws_closed", code=e.code, reason=e.reason,
                uptime_sec=round(time.time() - connected_at, 1), n_msgs=n_msgs)
        except Exception as e:
            log("cfb_ws_error", error=f"{type(e).__name__}: {e}",
                uptime_sec=round(time.time() - connected_at, 1), n_msgs=n_msgs)
        finally:
            state.cfb_connected = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, CFB_BACKOFF_MAX)


async def cfb_watchdog():
    """Log feed staleness so a dead-but-open socket is visible."""
    while True:
        await asyncio.sleep(1.0)
        if not state.brti_ts:
            continue
        age = time.time() - state.brti_ts
        if age > BRTI_STALE_SEC:
            log("cfb_stale", age_sec=round(age, 1), connected=state.cfb_connected)


async def venues_task():
    """Run the venue panel; the pricer reads `venue_quotes` directly."""
    hub = VenueHub(list(VENUE_PANEL))
    asyncio.create_task(publish_snapshots(hub, venue_quotes, interval=0.25))
    asyncio.create_task(_venue_health(hub))
    await hub.run()


async def _venue_health(hub: VenueHub, interval: float = 5.0):
    """Log up/down transitions per venue. A venue is up only when connected
    AND two-sided, so connected-but-quoteless is not reported as down."""
    seen = set()
    stale_logged = set()
    while True:
        await asyncio.sleep(interval)
        snap = hub.snapshot()
        for name in VENUE_PANEL:
            row = snap.get(name) or {}
            connected = bool(row.get("connected"))
            has_quote = row.get("bid") is not None and row.get("ask") is not None

            if connected and has_quote:
                if name not in seen:
                    seen.add(name)
                    log("venue_up", venue=name, bid=row["bid"], ask=row["ask"])
                stale_logged.discard(name)
            elif name not in stale_logged:
                stale_logged.add(name)
                log("venue_down", venue=name, connected=connected,
                    has_quote=has_quote, age=row.get("age"))
        log("venue_panel", n_live=len(venue_quotes),
            connected=sorted(n for n, r in snap.items() if r.get("connected")))


async def event_orderbook_websocket(tickers):
    """Kalshi event-book feed. Hot path: parse -> apply_delta -> cached top of
    book -> price. No await and no disk I/O on this path.

    Rollover: the recv loop polls the refresh signal every REFRESH_POLL_SEC
    instead of blocking forever on the socket. On signal it drops the
    connection and rebuilds the subscription from `active_tickers`, which
    `market_refresh_task` repopulates each window. `subscribed` is re-read from
    `active_tickers` on every reconnect too, so a dropped socket also recovers
    onto the live window.

    Every subscribe is recorded in the rollover trace with the ticker's own
    embedded expiry and the clock's view of the open window, so a mismatch
    between the two (the usual failure) is visible after the fact.
    """
    backoff = EVENT_BACKOFF_BASE
    books = {}
    subscribed = list(tickers) or list(st.active_tickers)
    while True:
        if not subscribed:
            await st.get_refresh_event().wait()
            st.get_refresh_event().clear()
            subscribed = list(st.active_tickers)
            continue

        connected_at = time.time()
        n_msgs = 0
        headers = create_headers("GET", EVENT_WS_PATH, for_websocket=True)
        resubscribe = False
        try:
            async with _ws_connect(EVENT_WS_URL, headers) as websocket:
                state.event_connected = True
                backoff = EVENT_BACKOFF_BASE
                for i, ticker in enumerate(subscribed):
                    await websocket.send(json.dumps({
                        "id": i + 1, "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_ticker": ticker},
                    }))
                log("event_subscribed", tickers=subscribed)
                st.subscribed_window = (st._ticker_stamp(subscribed[0]) if subscribed else None)
                st.trace.emit(
                    "socket_subscribe",
                    tickers=subscribed,
                    ticker_expiries=[st._ticker_stamp(t) for t in subscribed],
                    subscribed_window=st.subscribed_window,
                    reason="connect",
                    **st._window_state(),
                )

                while True:
                    if st.get_refresh_event().is_set():
                        resubscribe = True
                        break
                    try:
                        message = await asyncio.wait_for(websocket.recv(),
                                                         timeout=REFRESH_POLL_SEC)
                    except asyncio.TimeoutError:
                        continue
                    data = json.loads(message)
                    msg_type = data.get("type")
                    if msg_type == "orderbook_snapshot":
                        ticker = data["msg"]["market_ticker"]
                        ob = books.get(ticker)
                        if ob is None:
                            ob = new_event_book(ticker)
                            books[ticker] = ob
                        ob.apply_snapshot(data)
                        state.book = ob
                        on_tick(ob)
                    elif msg_type == "orderbook_delta":
                        ob = books.get(data["msg"]["market_ticker"])
                        if ob is not None:
                            ob.apply_delta(data)
                            state.deltas_since_frame += 1
                            on_tick(ob)
                    elif msg_type == "error":
                        log("event_ws_error", data=data)
                    n_msgs += 1
        except websockets.ConnectionClosed as e:
            log("event_ws_closed", code=e.code, reason=e.reason,
                uptime_sec=round(time.time() - connected_at, 1), n_msgs=n_msgs)
        except Exception as e:
            log("event_ws_disconnect", error=f"{type(e).__name__}: {e}",
                uptime_sec=round(time.time() - connected_at, 1), n_msgs=n_msgs)
        finally:
            state.event_connected = False

        if resubscribe:
            st.get_refresh_event().clear()
            subscribed = list(st.active_tickers)
            books.clear()
            st.subscribed_window = (st._ticker_stamp(subscribed[0]) if subscribed else None)
            st._awaiting_first_tick = bool(subscribed)
            st._switch_started_ts = time.time()
            log("event_resubscribe", tickers=subscribed)
            st.trace.emit(
                "socket_resubscribe",
                tickers=subscribed,
                ticker_expiries=[st._ticker_stamp(t) for t in subscribed],
                subscribed_window=st.subscribed_window,
                **st._window_state(),
            )
            continue

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, EVENT_BACKOFF_MAX)
        new_subscribed = list(st.active_tickers) or subscribed
        if new_subscribed != subscribed:
            st.trace.emit(
                "socket_reconnect_new_tickers",
                old_tickers=subscribed,
                old_expiries=[st._ticker_stamp(t) for t in subscribed],
                new_tickers=new_subscribed,
                new_expiries=[st._ticker_stamp(t) for t in new_subscribed],
                **st._window_state(),
            )
            subscribed = new_subscribed
            st.subscribed_window = (st._ticker_stamp(subscribed[0]) if subscribed else None)


async def market_refresh_task(series_ticker: str, date_filter: str):
    """Re-discover the active markets every 15 min and signal a resubscribe.

    The filter is derived from `open_window_expiry()`, which targets the window
    that just OPENED (the floor of `now`, plus 15). Discovery does not fire once
    at the boundary: the exchange does not swap its `status=open` set exactly at
    :00/:15/:30/:45, so the poll retries until the open window's ticker appears
    (or REFRESH_RETRY_SEC elapses).

    Every attempt is recorded in the rollover trace with its filter, result,
    attempt number and the server-vs-local clock offset.
    """
    while True:
        now = datetime.now()
        minutes = (now.minute // 15) * 15
        next_run = now.replace(minute=minutes, second=0, microsecond=0) + timedelta(minutes=15)
        if next_run <= now:
            next_run += timedelta(minutes=15)
        sleep_for = (next_run - now).total_seconds()
        st.trace.emit("scheduler_sleep", next_run=next_run.strftime("%H:%M:%S"),
                      sleep_sec=round(sleep_for, 3), **st._window_state())
        await asyncio.sleep(sleep_for)

        new_filter = st.format_date_filter(st.open_window_expiry())
        st.trace.emit("refresh_start", filter=new_filter, **st._window_state())

        tickers, target = None, None
        attempt = 0
        started = time.time()
        deadline = started + REFRESH_RETRY_SEC
        while time.time() < deadline:
            attempt += 1
            attempt_filter = st.format_date_filter(st.open_window_expiry())
            tickers, target, server_ts = await fetch_event_markets_async(
                series_ticker, attempt_filter)
            if server_ts is not None:
                st._clock_offset = server_ts - time.time()
            st.trace.emit(
                "refresh_attempt",
                attempt=attempt,
                filter=attempt_filter,
                n_tickers=len(tickers or []),
                tickers=tickers or [],
                ticker_expiries=[st._ticker_stamp(t) for t in (tickers or [])],
                target_price=target,
                server_ts=server_ts,
                clock_offset=(round(st._clock_offset, 3) if st._clock_offset is not None else None),
                elapsed=round(time.time() - started, 3),
                **st._window_state(),
            )
            if tickers:
                break
            await asyncio.sleep(REFRESH_RETRY_INTERVAL)

        st.active_tickers = tickers or []
        if target is not None:
            st.target_price = target
        log("event_markets_refreshed", date_filter=new_filter,
            target_price=st.target_price, tickers=st.active_tickers)
        capture.emit("window", filter=new_filter, tickers=st.active_tickers,
                     ticker_expiries=[st._ticker_stamp(t) for t in st.active_tickers],
                     target_price=st.target_price, attempts=attempt,
                     elapsed=round(time.time() - started, 3),
                     empty=(not st.active_tickers), **st._window_state())
        st.trace.emit(
            "refresh_result",
            filter=new_filter,
            attempts=attempt,
            elapsed=round(time.time() - started, 3),
            tickers=st.active_tickers,
            ticker_expiries=[st._ticker_stamp(t) for t in st.active_tickers],
            target_price=st.target_price,
            empty=(not st.active_tickers),
            **st._window_state(),
        )
        st.get_refresh_event().set()
