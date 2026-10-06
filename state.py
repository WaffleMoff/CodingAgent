"""Shared mutable state, capture, rollover trace, and time helpers.

Single owner of every module-level mutable global so that a rebind in one
module is visible everywhere (see the note on globals below). Producers are
single-writer per field, so no locks are used anywhere.

Globals are attributes of this module (`state.target_price`, `state.active_tickers`,
...). Consumers must import this module and read/write the attribute rather than
`from state import target_price`, because `from ... import` copies the binding
and a later rebind elsewhere would not be seen.
"""

from __future__ import annotations

import os
import time
import json
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

from config import (
    CAPTURE_PATH, CAPTURE_QUEUE_MAXLEN, ROLLOVER_LOG_PATH, WINDOW_TICK_INTERVAL,
    BRTI_BUFFER_MAXLEN, VENUE_PANEL, FALLBACK_SIGMA, log,
)
from forward import ForwardEngine


# --------------------------------------------------------------------------
# Data capture
#
# A single append-only JSONL writer shared by every producer. Producers call
# capture.emit(...), which is a deque.append: no I/O, no await, no lock, safe
# from the hot path. capture_task() drains the deque to disk on a timer, so a
# slow disk can never stall the feed loop. The sampler keeps only the newest
# priced row, so a recorded tick is the current price rather than a stale
# queued one.
# --------------------------------------------------------------------------
class Capture:
    def __init__(self, path, queue_maxlen):
        self.path = path
        self._queue = deque(maxlen=queue_maxlen)
        self._fh = None
        self.written = 0
        self.dropped = 0

    def _open(self):
        if self._fh is None and self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._fh = open(self.path, "a", buffering=1)

    def emit(self, rec_type, **fields):
        """Queue one record. Never blocks and never raises into the caller."""
        if not self.path:
            return
        try:
            rec = {"ts": time.time(), "rec": rec_type}
            rec.update(fields)
            if len(self._queue) == self._queue.maxlen:
                self.dropped += 1
            self._queue.append(rec)
        except Exception:
            pass

    def drain(self):
        """Write queued records. Called only from capture_task."""
        if not self.path or not self._queue:
            return
        try:
            self._open()
            while self._queue:
                rec = self._queue.popleft()
                try:
                    self._fh.write(json.dumps(rec, default=str) + "\n")
                    self.written += 1
                except Exception:
                    self.dropped += 1
        except Exception as e:
            import sys
            sys.stderr.write(json.dumps(
                {"ts": time.time(), "event": "capture_write_error",
                 "error": f"{type(e).__name__}: {e}"}) + "\n")

    def close(self):
        try:
            self.drain()
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass


capture = Capture(CAPTURE_PATH, CAPTURE_QUEUE_MAXLEN)


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
        "subscribed_tickers": list(active_tickers),
        "subscribed_expiries": [_ticker_stamp(t) for t in active_tickers],
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
# Shared state objects (single writer per field, no locks)
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
                 "brti_buffer_n", "calc_us", "iv_sigma_mid", "iv_sigma_bid",
                 "iv_sigma_ask", "iv_sigma_gap")

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
        self.iv_sigma_mid = None
        self.iv_sigma_bid = None
        self.iv_sigma_ask = None
        self.iv_sigma_gap = None


state = FeedState()
sigma_state = SigmaState()
prices = PriceState()

# Vol history and venue panel. Written by the BRTI feed and the venue publisher,
# read by the pricer. Single writer per container, so no locks are needed.
brti_buffer = deque(maxlen=BRTI_BUFFER_MAXLEN)
venue_quotes = {}
forward_engine = ForwardEngine(venues=VENUE_PANEL)

# --- mutable scalars / small containers, single owner -----------------------
target_price = None
_last_withheld_log = 0.0

active_tickers = []
_refresh_event = None
# Clock offset vs the exchange, in seconds (server_ts - local_ts). None until
# the first discovery response carries a Date header.
_clock_offset = None
# The window the socket is currently subscribed to, as a filter string.
subscribed_window = None
# Set on resubscribe so the next priced tick logs the switch latency.
_awaiting_first_tick = False
_switch_started_ts = None

# Opening-quote bookkeeping. A ticker lands here the first time we attempt to
# post its opening quotes, so re-snapshots, reconnects and (critically) a
# rollover resubscribe to the SAME ticker can never re-post. Keyed by ticker,
# so a genuinely new contract is a new key and therefore a fresh opportunity.
opening_quoted = set()
# Strong references to in-flight placement tasks so the event loop does not
# garbage-collect them mid-flight (a bare create_task result is only weakly
# held). Each task removes itself on completion.
opening_tasks = set()

# Capture sampling clock (module-level, written only by the hot path).
capture_last_tick_ts = 0.0


def get_refresh_event() -> asyncio.Event:
    global _refresh_event
    if _refresh_event is None:
        _refresh_event = asyncio.Event()
    return _refresh_event
