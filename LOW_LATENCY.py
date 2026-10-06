"""Low-latency top-of-book monitor for Kalshi KXBTC15M, with pricing.

Sibling of LOGGER.py: keeps the low-latency book/feed/render architecture of
the original LOW_LATENCY.py and adds LOGGER.py's fair-value pricing (settlement
digital probability, robust sigma, cross-venue forward, quote gate).

Order-book handling is IDENTICAL to the original LOW_LATENCY.py: Kalshi event
books publish YES and NO as two independent sides and are stored exactly as
before:
    self.bids  <- yes_dollars_fp snapshot levels / 'yes' deltas
    self.asks  <- no_dollars_fp  snapshot levels / 'no'  deltas
Prices and quantities are rounded with the same p_round/q_round (2/2), the
same dicts are used, and deltas apply the same add/delete/re-insert rule. The
YES-ask view stays derived the same way: best_yes_ask = 1 - best_no_bid.

What the pricing layer adds on top (all from LOGGER.py, unchanged math):
    * digital_prob over the BRTI 60s-average settlement window;
    * sigma_for_pricing (prior-blended, self-widening grid) with a fallback
      branch that blocks quoting;
    * ForwardEngine basis-adjusted forward from the venue panel;
    * the quote gate: quote only when every input is present and fresh.

Latency design (unchanged):
  * Hot path only applies a dict delta and recomputes cached top-of-book.
  * Pricing per tick is a couple of flops (digital_prob) against a *cached*
    sigma. The sigma estimate is expensive (several passes over the tick
    buffer) but changes on a second-scale, so it is refreshed by its own 1Hz
    task, never per delta. Recomputing it ~340x/s starved the render loop and
    is what made the display look choppy.
  * No await, no network, no disk I/O in the feed loop.
  * Rendering runs on its own task reading shared state at RENDER_FPS; the
    feed loop never blocks on Rich.
  * No disk writes in the hot path. Lifecycle/error events go to stderr, and
    the rollover trace goes to ROLLOVER_LOG_PATH as JSONL.

Opening-quote layer (this revision)
-----------------------------------
At the open of each 15-minute contract the maker rests two limit orders:
a YES bid at 30c and a NO bid at 30c (equivalently, a YES bid at 30 and a YES
ask at 70). They are the ONLY orders this file ever sends. The rules, exactly:

  * Placement window: only while the contract is in its first
    OPENING_QUOTE_WINDOW_SEC (60s) of trading, measured from the real window
    open boundary (the 15-min floor), NOT from when our socket connected. If
    the process starts 5 minutes into a window, nothing is sent.
  * Once per contract: a ticker is placed at most once, ever. Reconnects,
    re-snapshots and rollover re-subscribes to the SAME ticker do not re-post.
    A new ticker (next window) is a fresh posting opportunity.
  * Fixed size: OPENING_QUOTE_COUNT whole contracts on each side. Nothing
    scales with fair value, book state, or the quote gate.
  * No cancels: there is deliberately no order-takedown path. The orders rest
    until Kalshi clears, matches, or expires them at settlement.

The placement call is dispatched off the hot path (a fire-and-forget task), so
the feed loop never awaits the network; a slow POST cannot stall order-book
updates or the display.

Order wire format (V2, this revision)
-------------------------------------
`_post_order` previously posted the deprecated V1 shape to `/portfolio/orders`
(`action`/`yes_price`/`no_price`), which the exchange rejects with HTTP 410;
because failures are only logged, the file looked healthy while placing
nothing. It now uses the V2 event-orders path that the working create-order
probe exercises:

    POST /portfolio/events/orders
    {"ticker","side","count","price","time_in_force",
     "self_trade_prevention_type","client_order_id"}

`side` in {bid, ask} refers to the YES leg of the book, `count` and `price` are
strings, and `price` is a decimal-dollar string. There is no `exchange_index`:
with `ticker` supplied the order routes to the market's shard. Because the NO
leg has no field of its own, the NO bid is sent as side="ask" with the
complementary YES price (bid NO @30c == ask YES @70c).
"""

from __future__ import annotations

import os
import sys
import math
import time
import json
import uuid
import base64
import inspect
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import websockets
import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from dotenv import load_dotenv
from rich.live import Live
from rich.panel import Panel
from rich.console import Console

from settlement import (
    digital_prob,
    realized_vol,
    sigma_for_pricing,
    realized_vol_grid,
    effective_time,
    _grid_return_count,
    _fit_grid,
)
from forward import ForwardEngine
from venues import VenueHub, publish_snapshots
from ticklog import JsonlTickLogger


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SERIES_TICKER = "KXBTC15M".upper()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EVENT_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
EVENT_WS_PATH = "/trade-api/ws/v2"
EVENT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
CFB_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
CFB_WS_PATH = "/trade-api/ws/v2"

BRTI_INDEX_ID = "BRTI"
BRTI_STALE_SEC = 5.0

MAX_FRAME_BYTES = 16 * 1024 * 1024
EVENT_BACKOFF_BASE = 0.5
EVENT_BACKOFF_MAX = 10.0
CFB_BACKOFF_BASE = 1.0
CFB_BACKOFF_MAX = 30.0

RENDER_FPS = 20.0
RENDER_INTERVAL = 1.0 / RENDER_FPS

# Rollover: how long the recv loop waits before re-checking the refresh signal.
REFRESH_POLL_SEC = 1.0
# The discovery filter and the "expected filter" are derived from the window
# the clock says is open RIGHT NOW (no skew): floor(now) to the 15-min block is
# the open window's start, plus 15 is its expiry label. The old code skewed
# "now" back by REFRESH_SKEW_SEC to dodge get_expiry_datetime()'s boundary case,
# which landed it *inside the previous window* during the first seconds of every
# new window and made discovery target the expired contract there. Targeting the
# open window directly removes that failure; see open_window_expiry() below.
# How long a discovery poll keeps retrying when the API has not yet listed the
# newly opened window. The window swaps a variable number of seconds after the
# boundary, so a single shot at :00 is not enough.
REFRESH_RETRY_SEC = 90.0
REFRESH_RETRY_INTERVAL = 1.0
# Once-per-second wall-clock trace of which window the process believes is open.
WINDOW_TICK_INTERVAL = 1.0

# Rollover trace. JSONL, one record per event. Set to None to disable.
ROLLOVER_LOG_PATH = os.path.join(BASE_DIR, "rollover.jsonl")

# --- pricing configuration (from LOGGER.py) ---------------------------------
WINDOW_SEC = 60.0
VENUE_PANEL = ("coinbase", "kraken", "gemini", "bitstamp")
VOL_LOOKBACK_SEC = 600.0
FALLBACK_SIGMA = 0.45
FORWARD_MAX_AGE_SEC = 5.0
MIN_LIVE_VENUES = 2
WITHHELD_LOG_INTERVAL_SEC = 5.0
# Sigma is expensive and slow-moving; refresh it on a timer, not per tick.
SIGMA_REFRESH_SEC = 1.0

# --- opening quotes ---------------------------------------------------------
# Two resting limit orders per contract, opened in the first minute of trading.
# Prices are in CENTS, converted to the V2 decimal-dollar string at post time.
OPENING_QUOTE_WINDOW_SEC = 60.0        # only the first minute of the contract
OPENING_QUOTE_YES_PRICE = 30           # bid YES at 30c
OPENING_QUOTE_NO_PRICE = 30            # bid NO  at 30c
OPENING_QUOTE_COUNT = 1                # whole contracts per side

# --- tick logging ---------------------------------------------------------
# One JSONL record per priced tick: the full price_tick() dict, which is the
# source of every value the dashboard renders. Files rotate per UTC day under
# ticklog/YYYY-MM/. Writes are buffered; flush is driven by TICKLOG_FLUSH_SEC
# on its own task, so on_tick() still does no disk I/O. Set TICKLOG_DIR to
# None to disable. TICKLOG_SAMPLE_EVERY=1 logs every tick; raise it to thin.
TICKLOG_DIR = os.path.join(BASE_DIR, "ticklog")
TICKLOG_SAMPLE_EVERY = 1
TICKLOG_FLUSH_SEC = 1.0
TICKLOG_MAX_BUFFER_ROWS = 200_000
TICKLOG_STATS_INTERVAL_SEC = 60.0

# BRTI ring buffer for volatility. 4096 matches LOGGER.py; a 1Hz feed over the
# VOL_LOOKBACK_SEC window is ~600 rows, so this is generous headroom.
BRTI_BUFFER_MAXLEN = 4096

# Book config, byte-for-byte identical to MarketMakingBase.py event_ob_configs.
EVENT_BOOK_CONFIG = {
    "market_ticker": 0,
    "p_round": 2,
    "q_round": 2,
    "book_type": "event",
}

_ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(_ENV_PATH)

KEY_ID = os.getenv("KALSHI_API_KEY")
_PRIVATE_KEY_RAW = os.getenv("KALSHI_PRIVATE_KEY")
if not KEY_ID:
    raise RuntimeError(f"KALSHI_API_KEY missing from {_ENV_PATH}")
if not _PRIVATE_KEY_RAW:
    raise RuntimeError(f"KALSHI_PRIVATE_KEY missing from {_ENV_PATH}")

# Private key load, mirroring LOW_LATENCY_old.py. The previous revision called
# serialization.load_pem_private_key(...) without assigning the result, so the
# module-level PRIVATE_KEY used by create_signature() was never bound and every
# signing attempt raised NameError. Both branches assign PRIVATE_KEY.
_pk_candidate = _PRIVATE_KEY_RAW.strip().strip('"').strip("'")
if "BEGIN" in _pk_candidate:
    PRIVATE_KEY = serialization.load_pem_private_key(
        _pk_candidate.replace("\\n", "\n").encode("utf-8"),
        password=None,
        backend=default_backend(),
    )
else:
    _pk_path = os.path.expanduser(_pk_candidate)
    if not os.path.isabs(_pk_path):
        _pk_path = os.path.join(BASE_DIR, _pk_path)
    with open(_pk_path, "rb") as _f:
        PRIVATE_KEY = serialization.load_pem_private_key(
            _f.read(), password=None, backend=default_backend()
        )


def log(event: str, **fields) -> None:
    """Structured lifecycle event to stderr. Never writes to the hot path."""
    sys.stderr.write(json.dumps({"ts": time.time(), "event": event, **fields},
                                default=str) + "\n")


# --------------------------------------------------------------------------
# Rollover trace
#
# The rollover happens once per 15 minutes and the state that matters (which
# window the clock says is open, which filter was requested, what the API
# returned, what the socket subscribed to) is transient. This writes it to
# JSONL so the failure is inspectable after the fact instead of inferred from
# a live terminal. Writes are infrequent (a handful per boundary plus one per
# WINDOW_TICK_INTERVAL) so a synchronous append is fine.
# --------------------------------------------------------------------------
class RolloverTrace:
    def __init__(self, path):
        self.path = path
        self._fh = None
        self._window_tick_last = 0.0

    def _open(self):
        if self._fh is None and self.path:
            self._fh = open(self.path, "a", buffering=1)

    def emit(self, event, **fields):
        if not self.path:
            return
        try:
            self._open()
            rec = {"ts": time.time(), "ts_iso": _iso_now(), "event": event}
            rec.update(fields)
            self._fh.write(json.dumps(rec, default=str) + "\n")
        except Exception as e:
            log("rollover_log_error", error=f"{type(e).__name__}: {e}")

    def window_tick(self, force=False):
        """Once-per-second snapshot of the window the clock believes is open."""
        now = time.time()
        if not force and now - self._window_tick_last < WINDOW_TICK_INTERVAL:
            return
        self._window_tick_last = now
        self.emit("window_tick", **_window_state())


trace = RolloverTrace(ROLLOVER_LOG_PATH)

# Tick logger. Constructed once; the handle is opened lazily on first flush.
# `tick_log` is None when logging is disabled (TICKLOG_DIR is None), and the
# hot-path hook below becomes a no-op.
tick_log = (
    JsonlTickLogger(
        directory=TICKLOG_DIR,
        prefix="ticks",
        flush_interval_sec=TICKLOG_FLUSH_SEC,
        max_buffer_rows=TICKLOG_MAX_BUFFER_ROWS,
        sample_every=TICKLOG_SAMPLE_EVERY,
    )
    if TICKLOG_DIR
    else None
)

# Stable id for this process lifetime, so a restarted run's rows are
# separable from the previous one's when the files are concatenated.
RUN_ID = uuid.uuid4().hex[:12]
_run_started_ts = time.time()


def _tick_meta(row: dict, now_ts: float) -> dict:
    """Per-row context added at log time: the fields that make a flat
    dataframe groupable without reconstructing anything by subtraction.

    Kept out of price_tick() so the pricer's return dict (and the render /
    gate consumers that read it) is byte-for-byte unchanged.
    """
    dt = datetime.fromtimestamp(now_ts)
    open_base = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    return {
        "ts": now_ts,
        "ts_iso": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "run_id": RUN_ID,
        "run_uptime_sec": now_ts - _run_started_ts,
        "window_open": format_date_filter(open_base),
        "window_expiry": format_date_filter(open_base + timedelta(minutes=15)),
        "seconds_into_window": round((dt - open_base).total_seconds(), 3),
        "event_connected": state.event_connected,
        "cfb_connected": state.cfb_connected,
        "brti_age_sec": (now_ts - state.brti_ts) if state.brti_ts else None,
        "brti_stale": bool(state.brti_ts and (now_ts - state.brti_ts) > BRTI_STALE_SEC),
        "n_ticks_since_start": _n_ticks_logged,
    }


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _window_state():
    """The process's view of the currently-open window, for the trace."""
    dt = datetime.now()
    open_base = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    return {
        "clock_now": dt.strftime("%H:%M:%S.%f")[:-3],
        "clock_window_start": open_base.strftime("%H:%M"),
        "clock_window_expiry": format_date_filter(open_base + timedelta(minutes=15)),
        "clock_window_filter": format_date_filter(open_base),
        "seconds_into_window": round((dt - open_base).total_seconds(), 3),
        "subscribed_tickers": list(_active_tickers),
        "subscribed_expiries": [_ticker_stamp(t) for t in _active_tickers],
        "expected_filter": _expected_filter(),
    }


def _expected_filter():
    """Local-clock filter for the window that is open RIGHT NOW (no skew)."""
    dt = datetime.now()
    open_base = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    return format_date_filter(open_base + timedelta(minutes=15))


def _ticker_stamp(ticker):
    """The YYMMMDDHHMM stamp embedded in a ticker, or None."""
    exp = parse_ticker_expiry(ticker)
    return format_date_filter(exp) if exp else None


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def get_expiry_datetime(now: datetime) -> datetime:
    minutes_to_add = 15 if now.minute % 15 == 0 else (15 - now.minute % 15) % 15
    return (now + timedelta(minutes=minutes_to_add)).replace(second=0, microsecond=0)


def open_window_expiry(now: datetime = None) -> datetime:
    """Expiry label of the contract whose window is OPEN at `now`.

    The open window runs from the floor of `now` to the next 15-min boundary,
    so its expiry label is exactly that boundary. Derived by flooring rather
    than by skewing `now` back through get_expiry_datetime(): the skew approach
    returned the PREVIOUS window's label for the first seconds of every new
    window, which is what sent rollover discovery to the expired contract.
    """
    now = now or datetime.now()
    open_base = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return open_base + timedelta(minutes=15)


def window_open_time(now: datetime = None) -> datetime:
    """Wall-clock start of the window that is open at `now` (the 15-min floor).

    This is the reference the opening-quote window is measured against: a
    contract is "in its first minute" when now - window_open_time(now) <= 60s,
    independently of when our socket happened to connect.
    """
    now = now or datetime.now()
    return now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)


def format_date_filter(expiry: datetime) -> str:
    return (f"{expiry.strftime('%y')}{expiry.strftime('%b').upper()}"
            f"{expiry.strftime('%d')}{expiry.strftime('%H')}{expiry.strftime('%M')}")


def parse_ticker_expiry(ticker: str):
    """Best-effort parse of the expiry embedded in a Kalshi event ticker.

    KXBTC15M tickers look like KXBTC15M-26JUN201030-... i.e. a YYMMMDDHHMM
    stamp between the series and the strike suffix. Returns a datetime or None.
    """
    for part in ticker.split("-")[1:]:
        try:
            return datetime.strptime(part.upper(), "%y%b%d%H%M")
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Auth / connectivity
# --------------------------------------------------------------------------
def _ws_connect(url, headers):
    param = ("additional_headers"
             if "additional_headers" in inspect.signature(websockets.connect).parameters
             else "extra_headers")
    return websockets.connect(
        url,
        **{param: headers},
        ping_interval=20,
        ping_timeout=60,
        max_size=MAX_FRAME_BYTES,
    )


def create_signature(timestamp, method, path):
    message = f"{timestamp}{method}{path.split('?')[0]}".encode("utf-8")
    signature = PRIVATE_KEY.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def create_headers(method: str, path: str, for_websocket=False) -> dict:
    timestamp = str(int(time.time() * 1000))
    headers = {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": create_signature(timestamp, method, path),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
    if not for_websocket:
        headers["Content-Type"] = "application/json"
    return headers


# --------------------------------------------------------------------------
# Market discovery
# --------------------------------------------------------------------------
async def fetch_event_markets_async(series_ticker: str, date_filter: str):
    """Fetch open markets for `series_ticker` and keep those matching the
    filter. Returns (tickers, target_price, server_ts).

    `server_ts` is parsed from the response Date header (unix seconds) so the
    caller can compare the exchange's clock to the local one. It is None when
    the header is absent or unparseable.
    """
    url = f"{EVENT_BASE_URL}/markets?series_ticker={series_ticker}&status=open"
    server_ts = None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                date_hdr = resp.headers.get("Date")
                data = await resp.json()
                if date_hdr:
                    try:
                        server_ts = datetime.strptime(
                            date_hdr, "%a, %d %b %Y %H:%M:%S %Z"
                        ).replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        server_ts = None
        except Exception as e:
            log("fetch_event_markets_error", error=f"{type(e).__name__}: {e}")
            return None, None, None
    tickers = []
    target = None
    for m in data.get("markets", []):
        if date_filter not in m["ticker"]:
            continue
        tickers.append(m["ticker"])
        try:
            price_str = m["yes_sub_title"].split()[-1]
            if price_str[0] == "$":
                price_str = price_str[1:]
            target = float(price_str.replace(",", ""))
        except (KeyError, IndexError, ValueError):
            pass
    log("fetch_event_markets", date_filter=date_filter, n_tickers=len(tickers),
        target_price=target)
    return tickers, target, server_ts


# --------------------------------------------------------------------------
# Order book  (hot path lives here; mirrors MarketMakingBase.OrderBook exactly)
# --------------------------------------------------------------------------
class OrderBook:
    """Kalshi event order book, stored identically to MarketMakingBase.OrderBook.

        self.bids : {rounded_price -> rounded_qty}  from yes_dollars_fp / 'yes'
        self.asks : {rounded_price -> rounded_qty}  from no_dollars_fp  / 'no'

    Snapshot and delta handling, rounding, and the add/delete rule are copied
    from the base class so a Python `NaN` sentinel drift cannot occur.

    Derived top of book:
        best_yes_bid = max(self.bids)              (top of the YES book)
        best_no_bid  = max(self.asks)              (top of the NO  book, event)
        best_yes_ask = 1 - best_no_bid             (cheapest way to buy YES)

    `_refresh()` recomputes these once per applied message, so every consumer
    (render + pricer) reads one consistent pair from the same book state.
    """

    __slots__ = ("market_ticker", "p_round", "q_round", "book_type",
                 "snapshot_sides", "delta_sides", "delta_p_key", "delta_q_key",
                 "bids", "asks", "sequence_number", "last_update_time",
                 "n_deltas", "n_gaps", "best_yes_bid", "best_yes_ask",
                 "best_no_bid", "last_update")

    def __init__(self, ob_configs: dict):
        self.market_ticker = ob_configs["market_ticker"]
        self.p_round = ob_configs["p_round"]
        self.q_round = ob_configs["q_round"]
        self.book_type = ob_configs["book_type"]
        if self.book_type == "perp":
            self.snapshot_sides = ["bid", "ask"]
            self.delta_sides = ["bid", "ask"]
            self.delta_p_key = "price"
            self.delta_q_key = "delta"
        else:
            self.snapshot_sides = ["yes_dollars_fp", "no_dollars_fp"]
            self.delta_sides = ["yes", "no"]
            self.delta_p_key = "price_dollars"
            self.delta_q_key = "delta_fp"

        self.bids = {}
        self.asks = {}
        self.sequence_number = 0
        self.last_update_time = 0

        self.n_deltas = 0
        self.n_gaps = 0
        self.best_yes_bid = None
        self.best_yes_ask = None
        self.best_no_bid = None
        self.last_update = 0.0

    def _refresh(self):
        if self.bids:
            p = max(self.bids)
            self.best_yes_bid = (p, self.bids[p])
        else:
            self.best_yes_bid = None
        if self.asks:
            p = max(self.asks)
            self.best_no_bid = (p, self.asks[p])
            self.best_yes_ask = (round(1.0 - p, self.p_round), self.asks[p])
        else:
            self.best_no_bid = None
            self.best_yes_ask = None

    def apply_snapshot(self, snapshot_msg):
        msg = snapshot_msg.get("msg", {})
        self.bids.clear()
        self.asks.clear()
        for price, qty in msg.get(self.snapshot_sides[0], []):
            self.bids[round(float(price), self.p_round)] = round(float(qty), self.q_round)
        for price, qty in msg.get(self.snapshot_sides[1], []):
            self.asks[round(float(price), self.p_round)] = round(float(qty), self.q_round)
        self.sequence_number = snapshot_msg.get("seq", 0)
        self.last_update_time = time.time()
        self.n_deltas += 1
        self.last_update = self.last_update_time
        self._refresh()

    def apply_delta(self, delta_msg):
        msg = delta_msg.get("msg", {})
        side = msg["side"]
        price = round(float(msg[self.delta_p_key]), self.p_round)
        delta = round(float(msg[self.delta_q_key]), self.q_round)
        book = self.bids if side == self.delta_sides[0] else self.asks

        if price in book:
            new_q = book[price] + delta
            if new_q <= 0:
                del book[price]
            else:
                book[price] = round(new_q, self.q_round)
        elif delta > 0:
            book[price] = round(delta, self.q_round)

        seq = delta_msg.get("seq", 0)
        if seq and self.sequence_number and seq != self.sequence_number + 1:
            self.n_gaps += 1
        self.sequence_number = seq or self.sequence_number
        self.last_update_time = time.time()
        self.n_deltas += 1
        self.last_update = self.last_update_time
        self._refresh()


def new_event_book(ticker: str) -> OrderBook:
    cfg = EVENT_BOOK_CONFIG.copy()
    cfg["market_ticker"] = ticker
    return OrderBook(cfg)


# --------------------------------------------------------------------------
# Shared state (single writer per field, no locks)
# --------------------------------------------------------------------------
class FeedState:
    __slots__ = ("book", "brti_spot", "brti_ts", "event_connected",
                 "cfb_connected", "deltas_since_frame", "started_at")

    def __init__(self):
        self.book = None
        self.brti_spot = None
        self.brti_ts = 0.0
        self.event_connected = False
        self.cfb_connected = False
        self.deltas_since_frame = 0
        self.started_at = time.time()


class SigmaState:
    """Cached volatility estimate, refreshed on a timer by sigma_task()."""

    __slots__ = ("sigma", "branch", "grid_estimate", "n_returns", "ts")

    def __init__(self):
        self.sigma = FALLBACK_SIGMA
        self.branch = "fallback"
        self.grid_estimate = None
        self.n_returns = 0
        self.ts = 0.0


class PriceState:
    """Outputs of the pricer, written once per priced tick by the feed loop."""

    __slots__ = ("ticker", "prob", "market_p", "forward", "target", "sigma",
                 "sigma_branch", "forward_source", "forward_time", "window_left",
                 "quotable", "withhold_reason", "n_venues", "seconds_to_expiry",
                 "brti_buffer_n", "calc_us")

    def __init__(self):
        self.ticker = None
        self.prob = None
        self.market_p = None
        self.forward = None
        self.target = None
        self.sigma = FALLBACK_SIGMA
        self.sigma_branch = "init"
        self.forward_source = "none"
        self.forward_time = None
        self.window_left = 0.0
        self.quotable = False
        self.withhold_reason = "init"
        self.n_venues = 0
        self.seconds_to_expiry = 0.0
        self.brti_buffer_n = 0
        self.calc_us = 0.0


state = FeedState()
sigma_state = SigmaState()
prices = PriceState()

# Vol history and venue panel. Written by the BRTI feed and the venue publisher,
# read by the pricer. Single writer per container, so no locks are needed.
brti_buffer = deque(maxlen=BRTI_BUFFER_MAXLEN)
venue_quotes = {}
forward_engine = ForwardEngine(venues=VENUE_PANEL)
target_price = None
_last_withheld_log = 0.0
# Monotonic count of ticks offered to on_tick(), for the log's row index.
_n_ticks_logged = 0

_active_tickers = []
_refresh_event = None
# Clock offset vs the exchange, in seconds (server_ts - local_ts). None until
# the first discovery response carries a Date header.
_clock_offset = None
# The window the socket is currently subscribed to, as a filter string.
_subscribed_window = None
# Set on resubscribe so the next priced tick logs the switch latency.
_awaiting_first_tick = False
_switch_started_ts = None

# Opening-quote bookkeeping. A ticker lands here the first time we attempt to
# post its opening quotes, so re-snapshots, reconnects and (critically) a
# rollover resubscribe to the SAME ticker can never re-post. Keyed by ticker,
# so a genuinely new contract is a new key and therefore a fresh opportunity.
_opening_quoted = set()
# Strong references to in-flight placement tasks so the event loop does not
# garbage-collect them mid-flight (a bare create_task result is only weakly
# held). Each task removes itself on completion.
_opening_tasks = set()


def get_refresh_event() -> asyncio.Event:
    global _refresh_event
    if _refresh_event is None:
        _refresh_event = asyncio.Event()
    return _refresh_event


# --------------------------------------------------------------------------
# Pricing  (lifted from LOGGER.py; reads the cached top of book)
# --------------------------------------------------------------------------
def _quote_gate(prob, forward, forward_ts, sigma_branch, n_venues):
    """Return (quotable, reason). Any missing/stale input blocks quoting."""
    if prob is None:
        return False, "fair_none"
    if forward is None:
        return False, "no_forward"
    if forward_ts is None:
        return False, "no_forward_ts"
    age = time.time() - forward_ts
    if age > FORWARD_MAX_AGE_SEC:
        return False, f"forward_stale_{age:.1f}s"
    if sigma_branch == "fallback":
        return False, "sigma_fallback"
    if n_venues < MIN_LIVE_VENUES:
        return False, f"venues_{n_venues}"
    return True, "ok"


def refresh_sigma(now_ts: float) -> None:
    """Recompute the cached sigma estimate. Called on a timer, not per tick.

    Sigma is built from 30s grid buckets over a 600s lookback, so it changes on
    a second-scale. `realized_vol_grid`, `_fit_grid`, `_grid_return_count` and
    `sigma_for_pricing` each make a full pass over the tick buffer, so running
    this per delta (~340x/s on this book) starves the event loop and is what
    made the display choppy.
    """
    grid_est, n_rets = None, 0
    try:
        grid_est = realized_vol_grid(brti_buffer, VOL_LOOKBACK_SEC, now_ts)
        grid = _fit_grid(brti_buffer, VOL_LOOKBACK_SEC, now_ts, 30.0, 30)
        n_rets = _grid_return_count(brti_buffer, VOL_LOOKBACK_SEC, now_ts, grid)
    except Exception as e:
        log("sigma_diag_error", error=f"{type(e).__name__}: {e}")

    robust = None
    try:
        robust = sigma_for_pricing(brti_buffer, now_ts, lookback_sec=VOL_LOOKBACK_SEC)
    except Exception:
        robust = None

    legacy = None
    try:
        legacy = realized_vol(brti_buffer, VOL_LOOKBACK_SEC, now_ts)
    except Exception:
        legacy = None

    # Anything without >=1 usable return is a fallback and blocks quoting.
    if n_rets > 0 and robust is not None and math.isfinite(robust) and robust > 0:
        sigma_state.sigma, sigma_state.branch = robust, "robust"
    else:
        sigma_state.sigma, sigma_state.branch = FALLBACK_SIGMA, "fallback"
    sigma_state.grid_estimate = grid_est
    sigma_state.n_returns = n_rets
    sigma_state.ts = now_ts

    log("sigma", sigma_used=round(sigma_state.sigma, 6), branch=sigma_state.branch,
        legacy_realized_vol=legacy, robust_sigma_for_pricing=robust,
        grid_estimate=grid_est, grid_returns=n_rets,
        brti_buffer_n=len(brti_buffer), lookback_sec=VOL_LOOKBACK_SEC)


async def sigma_task():
    while True:
        refresh_sigma(time.time())
        await asyncio.sleep(SIGMA_REFRESH_SEC)


def price_tick(book: OrderBook, spot, now: datetime, now_ts: float) -> dict:
    """Compute the fair value for `book` from its cached top of book.

    Per-tick cost is one digital_prob plus a small dict build. Sigma comes from
    the cached `sigma_state`; nothing here scans the tick buffer.
    """
    t0 = time.perf_counter()

    bb = book.best_yes_bid
    ba = book.best_yes_ask
    best_bid = bb[0] if bb else None
    best_ask = ba[0] if ba else None
    market_p = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None

    expiry = get_expiry_datetime(now)
    ticker_expiry = parse_ticker_expiry(book.market_ticker)
    seconds_to_expiry = expiry.timestamp() - now_ts

    n_venues = len(venue_quotes)
    if spot is not None and venue_quotes:
        cached_forward = forward_engine.compute(venue_quotes, spot, now_ts)
    else:
        cached_forward = spot
    fwd_diag = dict(forward_engine.last_diag)

    sigma_used = sigma_state.sigma
    sigma_branch = sigma_state.branch
    delta_spot = (spot - target_price) if (spot is not None and target_price is not None) else None

    p_raw = None
    t_eff_years = None
    denom = None
    z = None
    if target_price is not None and cached_forward is not None:
        p_raw = digital_prob(now, expiry, float(target_price), float(cached_forward),
                             float(sigma_used), window_sec=WINDOW_SEC, realized=brti_buffer)
        t_eff_years = effective_time(seconds_to_expiry, WINDOW_SEC)
        denom = sigma_used * math.sqrt(t_eff_years) if t_eff_years > 0 else 0.0
        if denom > 0 and cached_forward > 0 and target_price > 0:
            z = (math.log(cached_forward / target_price)
                 - 0.5 * sigma_used ** 2 * t_eff_years) / denom

    p = p_raw if (p_raw is not None and math.isfinite(p_raw)) else None

    forward_ts = state.brti_ts if state.brti_ts else None
    quotable, withhold_reason = _quote_gate(
        p, cached_forward, forward_ts, sigma_branch, n_venues)

    window_start = expiry.timestamp() - WINDOW_SEC
    in_window = now_ts > window_start
    realized_avg, realized_n = None, 0
    if in_window:
        total, cnt = 0.0, 0
        for row in brti_buffer:
            if window_start <= row[0] <= now_ts and row[1] > 0:
                total += row[1]
                cnt += 1
        if cnt:
            realized_avg, realized_n = total / cnt, cnt

    return {
        "ticker": book.market_ticker,
        "ticker_expiry": ticker_expiry,
        "computed_expiry": expiry,
        "best_yes_bid": best_bid,
        "best_yes_ask": best_ask,
        "best_no_bid_raw": book.best_no_bid[0] if book.best_no_bid else None,
        "market_p": market_p,
        "forward": cached_forward,
        "forward_source": fwd_diag["source"],
        "fwd_panel_mid": fwd_diag["panel_mid"],
        "fwd_n_live": fwd_diag["n_live"],
        "fwd_raw_basis_bp": fwd_diag["raw_basis_bp"],
        "fwd_smoothed_basis_bp": fwd_diag["smoothed_basis_bp"],
        "fwd_reason": fwd_diag["reason"],
        "brti_spot": spot,
        "n_venues": n_venues,
        "target": target_price,
        "spot_minus_target": delta_spot,
        "sigma": sigma_used,
        "sigma_branch": sigma_branch,
        "sigma_raw_grid": sigma_state.grid_estimate,
        "sigma_n_returns": sigma_state.n_returns,
        "t_eff_years": t_eff_years,
        "denom_sigma_sqrtT": denom,
        "z_score": z,
        "prob": p,
        "prob_raw": p_raw,
        "prob_branch": "in_window" if in_window else "pre_window",
        "quotable": quotable,
        "withhold_reason": withhold_reason,
        "window_realized_avg": realized_avg,
        "window_realized_n": realized_n,
        "brti_buffer_n": len(brti_buffer),
        "seconds_to_expiry": seconds_to_expiry,
        "n_deltas": book.n_deltas,
        "n_seq_gaps": book.n_gaps,
        "calc_us": (time.perf_counter() - t0) * 1e6,
    }


def _publish_prices(row: dict) -> None:
    prices.ticker = row["ticker"]
    prices.prob = row["prob"]
    prices.market_p = row["market_p"]
    prices.forward = row["forward"]
    prices.target = row["target"]
    prices.sigma = row["sigma"]
    prices.sigma_branch = row["sigma_branch"]
    prices.forward_source = row["forward_source"]
    prices.forward_time = state.brti_ts or None
    prices.window_left = max(row["seconds_to_expiry"], 0.0)
    prices.quotable = row["quotable"]
    prices.withhold_reason = row["withhold_reason"]
    prices.n_venues = row["n_venues"]
    prices.seconds_to_expiry = row["seconds_to_expiry"]
    prices.brti_buffer_n = row["brti_buffer_n"]
    prices.calc_us = row["calc_us"]


def _note_switch_tick(book: OrderBook, row: dict) -> None:
    """Emit the first-tick-after-switch record so the rollover gap is visible."""
    global _awaiting_first_tick, _switch_started_ts
    if not _awaiting_first_tick:
        return
    _awaiting_first_tick = False
    now = time.time()
    trace.emit(
        "first_tick_after_switch",
        ticker=book.market_ticker,
        ticker_expiry=_ticker_stamp(book.market_ticker),
        subscribed_window=_subscribed_window,
        seconds_since_resubscribe=(round(now - _switch_started_ts, 3)
                                   if _switch_started_ts else None),
        seconds_into_window=_window_state()["seconds_into_window"],
        prob=row["prob"],
        best_yes_bid=row["best_yes_bid"],
        best_yes_ask=row["best_yes_ask"],
    )


# --------------------------------------------------------------------------
# Opening quotes
#
# Two resting limit orders at the open of a contract: a YES bid at 30c and a
# NO bid at 30c. The only orders this file ever sends.
#
# Wire format: the V2 event-orders endpoint. Schema is CreateOrderV2Request
# {ticker, side, count, price, time_in_force, self_trade_prevention_type,
# client_order_id?, exchange_index?} with side in {bid, ask} referring to the
# YES leg OF THE BOOK:
#     side="bid" -> buy YES  at price
#     side="ask" -> sell YES at price  ==  buy NO at (1 - price)
# There is no yes_price / no_price / action / outcome_side on this request, so
# a NO bid MUST go as an "ask" carrying the complementary YES-leg price:
#     bid NO @30c  ->  side="ask", price="0.7000"
# Sending side="bid", price="0.3000" twice would post the same YES bid twice
# and buy no NO at all.
#
# The old V1 body (action/yes_price/no_price to /portfolio/orders) is rejected
# with HTTP 410. Both legs rest; supplying `ticker` routes to the market's
# shard, so exchange_index is omitted.
# --------------------------------------------------------------------------
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

    Guarded by `_opening_quoted` so a re-entrant call (a second tick arriving
    before this finishes) is a no-op; the ticker is marked before the awaits so
    the guard is airtight even under interleaving.
    """
    if ticker in _opening_quoted:
        return
    _opening_quoted.add(ticker)

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
    log("opening_quotes_done", ticker=ticker)


def _maybe_place_opening_quotes(book: OrderBook) -> None:
    """Dispatch opening quotes if the contract is in its first minute.

    Called from the hot path, so it only inspects the clock and, at most once
    per contract, schedules a task. It never awaits. Cheap after the first
    call: `_opening_quoted` membership short-circuits everything.
    """
    ticker = book.market_ticker
    if ticker in _opening_quoted:
        return
    seconds_in = (datetime.now() - window_open_time()).total_seconds()
    if seconds_in > OPENING_QUOTE_WINDOW_SEC:
        # Past the first minute: remember it, so we stop re-checking every tick.
        _opening_quoted.add(ticker)
        return
    task = asyncio.create_task(place_opening_quotes(ticker))
    _opening_tasks.add(task)
    task.add_done_callback(_opening_tasks.discard)
    log("opening_quotes_scheduled", ticker=ticker,
        seconds_into_window=round(seconds_in, 3))


def on_tick(book: OrderBook) -> None:
    """Priced once per applied delta, synchronously in the feed loop.

    Cheap by construction: the book has already cached its top of book, sigma
    is cached by sigma_task, and the rest is in-memory arithmetic. The only
    I/O is a stderr heartbeat, rate-limited, plus the one-shot rollover trace.
    """
    global _last_withheld_log
    now = datetime.now()
    row = price_tick(book, state.brti_spot, now, now.timestamp())
    _publish_prices(row)
    _note_switch_tick(book, row)
    _maybe_place_opening_quotes(book)

    global _n_ticks_logged
    _n_ticks_logged += 1
    if tick_log is not None:
        # Buffered in-memory append; disk I/O happens on the flush task.
        tick_log.write({**row, **_tick_meta(row, now.timestamp())})

    if row["quotable"]:
        pass
    elif time.time() - _last_withheld_log >= WITHHELD_LOG_INTERVAL_SEC:
        _last_withheld_log = time.time()
        log("quote_withheld", ticker=book.market_ticker,
            reason=row["withhold_reason"], prob=row["prob"])


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
console = Console()
live = Live(console=console, refresh_per_second=int(RENDER_FPS), auto_refresh=False)


def _fmt_level(level):
    if level is None:
        return "  --  "
    return f"{level[0]:.2f}@{level[1]:.2f}"


def _fmt_prob(x):
    return "N/A" if x is None else f"{x:.4f}"


def render_screen():
    now_ts = time.time()
    book = state.book

    if book is None:
        ticker = "(waiting for book)"
        bid_s = ask_s = spread_s = "--"
        yes_raw = no_raw = "--"
        n_deltas = n_gaps = 0
    else:
        ticker = book.market_ticker
        bid_s = _fmt_level(book.best_yes_bid)
        ask_s = _fmt_level(book.best_yes_ask)
        yes_raw = _fmt_level(book.best_yes_bid)
        no_raw = _fmt_level(book.best_no_bid)
        if book.best_yes_bid and book.best_yes_ask:
            spread_s = f"{book.best_yes_ask[0] - book.best_yes_bid[0]:.3f}"
        else:
            spread_s = "--"
        n_deltas = book.n_deltas
        n_gaps = book.n_gaps

    secs_left = max(0.0, get_expiry_datetime(datetime.now()).timestamp() - now_ts)

    if state.brti_spot is None:
        brti_s = "  --  "
        brti_age = 0.0
    else:
        brti_s = f"{state.brti_spot:,.2f}"
        brti_age = now_ts - state.brti_ts if state.brti_ts else 0.0

    ev_up = state.event_connected
    cfb_up = state.cfb_connected and brti_age <= BRTI_STALE_SEC

    fair = _fmt_prob(prices.prob)
    edge = "N/A" if prices.prob is None else f"{prices.prob - (prices.market_p or 0.0):+.4f}"
    gate = "OK" if prices.quotable else f"WITHHELD ({prices.withhold_reason})"

    wins = _window_state()
    body = (
        f"[bold]TICKER[/] {ticker}\n"
        f"[bold]FAIR[/] {fair}   [bold]MARKET[/] {_fmt_prob(prices.market_p)}   "
        f"[bold]EDGE[/] {edge}\n"
        f"[bold]YES BID[/] {bid_s}   [bold]YES ASK[/] {ask_s}   [bold]SPREAD[/] {spread_s}\n"
        f"[bold]raw yes[/] {yes_raw}   [bold]raw no[/] {no_raw}   "
        f"[bold](ask = 1 - no)[/]\n"
        f"[bold]forward[/] {_fmt_prob(prices.forward)}   [bold]target[/] {_fmt_prob(prices.target)}   "
        f"[bold]sigma[/] {prices.sigma:.3f} ({prices.sigma_branch})\n"
        f"[bold]BRTI[/] {brti_s}   [bold]age[/] {brti_age:0.1f}s   "
        f"[bold]expiry in[/] {secs_left:0.0f}s\n"
        f"[bold]deltas[/] {n_deltas}   [bold]gaps[/] {n_gaps}   "
        f"[bold]rate[/] {state.deltas_since_frame * RENDER_FPS:0.0f}/s   "
        f"[bold]venues[/] {prices.n_venues}   [bold]sigma n[/] {prices.brti_buffer_n}\n"
        f"[bold]window[/] {wins['clock_window_filter']} "
        f"(+{wins['seconds_into_window']:.0f}s)   [bold]subscribed[/] {_subscribed_window}\n"
        f"[bold]clock offset[/] "
        f"{'n/a' if _clock_offset is None else f'{_clock_offset:+.1f}s'}\n"
        f"[bold]quote[/] {gate}\n"
        f"[bold]event[/] [{'green' if ev_up else 'red'}]{'UP' if ev_up else 'DOWN'}[/]   "
        f"[bold]brti[/] [{'green' if cfb_up else 'red'}]{'UP' if cfb_up else 'DOWN'}[/]"
    )

    live.update(Panel(body, title="[cyan]Top of Book + Price[/cyan]",
                      border_style="green" if (prices.quotable and ev_up and cfb_up) else "red"))
    live.refresh()
    state.deltas_since_frame = 0


async def render_loop():
    while True:
        await asyncio.sleep(RENDER_INTERVAL)
        render_screen()


async def ticklog_task():
    """Flush the tick log on a timer and emit a stats heartbeat.

    Disk I/O lives here, off the feed loop. Also flushes on each iteration so
    a long-running process never holds more than ~TICKLOG_FLUSH_SEC of rows in
    memory. The heartbeat makes a stalled or erroring sink visible in stderr.
    """
    if tick_log is None:
        return
    last_stats = time.time()
    while True:
        try:
            tick_log.flush()
        except Exception as e:
            log("ticklog_flush_error", error=f"{type(e).__name__}: {e}")
        now = time.time()
        if now - last_stats >= TICKLOG_STATS_INTERVAL_SEC:
            last_stats = now
            try:
                log("ticklog_stats", **tick_log.stats())
            except Exception:
                pass
        await asyncio.sleep(TICKLOG_FLUSH_SEC)


async def window_trace_task():
    """Once-per-second trace of the window the process believes is open.

    This is the record that shows, unambiguously, at what second the local
    clock flips which contract it thinks is live, what the socket is
    subscribed to at that moment, and how far the exchange clock is from the
    local one.
    """
    while True:
        try:
            trace.window_tick()
        except Exception as e:
            log("window_trace_error", error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(WINDOW_TICK_INTERVAL)


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------
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
    connection and rebuilds the subscription from `_active_tickers`, which
    `market_refresh_task` repopulates each window. Previously the only place
    the signal was read was inside `if not subscribed:`, a branch that never
    runs because `subscribed` is seeded at startup — so the freshly discovered
    next-window tickers never reached the socket and it idled on the expired
    contract. `subscribed` is re-read from `_active_tickers` on every reconnect
    too, so a dropped socket also recovers onto the live window.

    Every subscribe is recorded in the rollover trace with the ticker's own
    embedded expiry and the clock's view of the open window, so a mismatch
    between the two (the usual failure) is visible after the fact.
    """
    global target_price, _subscribed_window, _awaiting_first_tick, _switch_started_ts
    backoff = EVENT_BACKOFF_BASE
    books = {}
    subscribed = list(tickers) or list(_active_tickers)
    while True:
        if not subscribed:
            await get_refresh_event().wait()
            get_refresh_event().clear()
            subscribed = list(_active_tickers)
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
                _subscribed_window = (_ticker_stamp(subscribed[0]) if subscribed else None)
                trace.emit(
                    "socket_subscribe",
                    tickers=subscribed,
                    ticker_expiries=[_ticker_stamp(t) for t in subscribed],
                    subscribed_window=_subscribed_window,
                    reason="connect",
                    **_window_state(),
                )

                while True:
                    if get_refresh_event().is_set():
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
            get_refresh_event().clear()
            subscribed = list(_active_tickers)
            books.clear()
            _subscribed_window = (_ticker_stamp(subscribed[0]) if subscribed else None)
            _awaiting_first_tick = bool(subscribed)
            _switch_started_ts = time.time()
            log("event_resubscribe", tickers=subscribed)
            trace.emit(
                "socket_resubscribe",
                tickers=subscribed,
                ticker_expiries=[_ticker_stamp(t) for t in subscribed],
                subscribed_window=_subscribed_window,
                **_window_state(),
            )
            continue

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, EVENT_BACKOFF_MAX)
        new_subscribed = list(_active_tickers) or subscribed
        if new_subscribed != subscribed:
            trace.emit(
                "socket_reconnect_new_tickers",
                old_tickers=subscribed,
                old_expiries=[_ticker_stamp(t) for t in subscribed],
                new_tickers=new_subscribed,
                new_expiries=[_ticker_stamp(t) for t in new_subscribed],
                **_window_state(),
            )
            subscribed = new_subscribed
            _subscribed_window = (_ticker_stamp(subscribed[0]) if subscribed else None)


async def market_refresh_task(series_ticker: str, date_filter: str):
    """Re-discover the active markets every 15 min and signal a resubscribe.

    Two things this does that the original did not:

    * The filter is derived from `open_window_expiry()`, which targets the
      window that just OPENED (the floor of `now`, plus 15). The old code skewed
      `now` back by REFRESH_SKEW_SEC through `get_expiry_datetime`, which for
      the first seconds of a new window returned the PREVIOUS window and so
      pointed discovery at the contract that had just expired.
    * Discovery does not fire once at the boundary. The exchange does not swap
      its `status=open` set at :00/:15/:30/:45 exactly; in the observed run the
      newly opened window was not listed for ~29s after the boundary. So the
      poll retries until the open window's ticker appears (or
      REFRESH_RETRY_SEC elapses), instead of taking one empty response as final
      and leaving the socket on the expired contract.

    Every attempt is recorded in the rollover trace with its filter, result,
    attempt number and the server-vs-local clock offset.
    """
    global _active_tickers, target_price, _clock_offset
    while True:
        now = datetime.now()
        minutes = (now.minute // 15) * 15
        next_run = now.replace(minute=minutes, second=0, microsecond=0) + timedelta(minutes=15)
        if next_run <= now:
            next_run += timedelta(minutes=15)
        sleep_for = (next_run - now).total_seconds()
        trace.emit("scheduler_sleep", next_run=next_run.strftime("%H:%M:%S"),
                   sleep_sec=round(sleep_for, 3), **_window_state())
        await asyncio.sleep(sleep_for)

        new_filter = format_date_filter(open_window_expiry())
        trace.emit("refresh_start", filter=new_filter, **_window_state())

        tickers, target = None, None
        attempt = 0
        started = time.time()
        deadline = started + REFRESH_RETRY_SEC
        while time.time() < deadline:
            attempt += 1
            # Recompute the filter each pass: the open window's *filter* is
            # fixed, but recomputing keeps it correct if a retry straddles the
            # next boundary.
            attempt_filter = format_date_filter(open_window_expiry())
            tickers, target, server_ts = await fetch_event_markets_async(
                series_ticker, attempt_filter)
            if server_ts is not None:
                _clock_offset = server_ts - time.time()
            trace.emit(
                "refresh_attempt",
                attempt=attempt,
                filter=attempt_filter,
                n_tickers=len(tickers or []),
                tickers=tickers or [],
                ticker_expiries=[_ticker_stamp(t) for t in (tickers or [])],
                target_price=target,
                server_ts=server_ts,
                clock_offset=(round(_clock_offset, 3) if _clock_offset is not None else None),
                elapsed=round(time.time() - started, 3),
                **_window_state(),
            )
            if tickers:
                break
            await asyncio.sleep(REFRESH_RETRY_INTERVAL)

        _active_tickers = tickers or []
        if target is not None:
            target_price = target
        log("event_markets_refreshed", date_filter=new_filter,
            target_price=target_price, tickers=_active_tickers)
        trace.emit(
            "refresh_result",
            filter=new_filter,
            attempts=attempt,
            elapsed=round(time.time() - started, 3),
            tickers=_active_tickers,
            ticker_expiries=[_ticker_stamp(t) for t in _active_tickers],
            target_price=target_price,
            empty=(not _active_tickers),
            **_window_state(),
        )
        get_refresh_event().set()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
async def main():
    global _active_tickers, target_price
    live.start()

    # Seed from the window that is open RIGHT NOW (no skew), matching the
    # boundary-safe derivation used by market_refresh_task. The old startup used
    # get_expiry_datetime(now), which at an exact boundary returned the window
    # after next, so a process launched on :00/:15/:30/:45 seeded the wrong
    # contract for a full cycle.
    date_filter = format_date_filter(open_window_expiry())
    _active_tickers, target_price, server_ts = await fetch_event_markets_async(
        SERIES_TICKER, date_filter)
    _active_tickers = _active_tickers or []
    log("startup", series_ticker=SERIES_TICKER, date_filter=date_filter,
        target_price=target_price, tickers=_active_tickers)
    trace.emit(
        "startup",
        date_filter=date_filter,
        tickers=_active_tickers,
        ticker_expiries=[_ticker_stamp(t) for t in _active_tickers],
        target_price=target_price,
        server_ts=server_ts,
        clock_offset=(round(server_ts - time.time(), 3) if server_ts else None),
        rollover_log=ROLLOVER_LOG_PATH,
        **_window_state(),
    )
    trace.window_tick(force=True)

    asyncio.create_task(cfb_brti_websocket())
    asyncio.create_task(cfb_watchdog())
    asyncio.create_task(venues_task())
    asyncio.create_task(sigma_task())
    asyncio.create_task(render_loop())
    asyncio.create_task(ticklog_task())
    asyncio.create_task(window_trace_task())
    asyncio.create_task(market_refresh_task(SERIES_TICKER, date_filter))

    while True:
        try:
            await event_orderbook_websocket(_active_tickers)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("event_task_restart", error=f"{type(e).__name__}: {e}")
            trace.emit("event_task_restart", error=f"{type(e).__name__}: {e}",
                       **_window_state())
            await asyncio.sleep(EVENT_BACKOFF_BASE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        # Flush the tail so a multi-day buffer is not lost on shutdown.
        if tick_log is not None:
            tick_log.close()
            log("ticklog_closed", **tick_log.stats())
