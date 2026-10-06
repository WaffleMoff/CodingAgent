"""Rich dashboard. Runs on its own task reading shared state at RENDER_FPS.

The feed loop never blocks on Rich: this module only reads state and repaints.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from rich.live import Live
from rich.panel import Panel
from rich.console import Console

from config import RENDER_FPS, RENDER_INTERVAL, BRTI_STALE_SEC
from state import prices, state, _clock_offset, _subscribed_window, _window_state


console = Console()
live = Live(console=console, refresh_per_second=int(RENDER_FPS), auto_refresh=False)


def _fmt_level(level):
    if level is None:
        return "  --  "
    return f"{level[0]:.2f}@{level[1]:.2f}"


def _fmt_prob(x):
    return "N/A" if x is None else f"{x:.4f}"


def _fmt_iv(x):
    return "  n/a " if x is None else f"{x:.3f}"


def _fmt_gap(x):
    return "N/A" if x is None else f"{x:+.3f}"


def render_screen():
    from state import get_expiry_datetime
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
        f"[bold]implied sigma[/] {_fmt_iv(prices.iv_sigma_mid)} "
        f"[{_fmt_iv(prices.iv_sigma_bid)} .. {_fmt_iv(prices.iv_sigma_ask)}]   "
        f"[bold]gap[/] {_fmt_gap(prices.iv_sigma_gap)}\n"
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


async def window_trace_task():
    """Once-per-second trace of the window the process believes is open.

    This is the record that shows, unambiguously, at what second the local
    clock flips which contract it thinks is live, what the socket is
    subscribed to at that moment, and how far the exchange clock is from the
    local one.
    """
    from state import trace
    from config import WINDOW_TICK_INTERVAL, log
    while True:
        try:
            trace.window_tick()
        except Exception as e:
            log("window_trace_error", error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(WINDOW_TICK_INTERVAL)
