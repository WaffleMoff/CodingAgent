"""Fair-value pricing: quote gate, implied sigma, cached sigma refresh, tick.

Per-tick cost is one digital_prob plus a small dict build. Sigma comes from the
cached `state.sigma_state`; nothing here scans the tick buffer on the hot path.
"""

from __future__ import annotations

import math
import time
from datetime import datetime

from scipy.stats import norm

from config import (
    WINDOW_SEC, VOL_LOOKBACK_SEC, FALLBACK_SIGMA, FORWARD_MAX_AGE_SEC,
    MIN_LIVE_VENUES, SIGMA_REFRESH_SEC, log,
)
from state import capture, sigma_state, state, brti_buffer, venue_quotes, forward_engine
import state as st
from settlement import (
    digital_prob, realized_vol, sigma_for_pricing, realized_vol_grid,
    effective_time, _grid_return_count, _fit_grid,
)


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


def implied_sigma(price: float, forward: float, strike: float, t_eff_years: float):
    """Annualized sigma that reproduces an observed digital price. Closed form.

    Inverting the log-normal digital  p = Phi(z),  z = (a - w^2/2) / w  with
    a = ln(F/K) and w = sigma*sqrt(T), is NOT transcendental: multiplying
    through by w gives the quadratic  w^2 + 2*z*w - 2*a = 0, whose positive
    root is  w = -z + sqrt(z^2 + 2*a)  and hence sigma = w / sqrt(T). One sqrt,
    no iteration (bisection would cost ~33 norm.cdf calls per solve).

    Meaningful only in the pre-window regime. Inside the averaging window the
    price embeds the realized average rather than sigma, so the caller must not
    feed an in-window price here.

    Returns None when no sigma >= 0 reproduces `price`: that is exactly the
    deep-OTM region where `price` sits below what any attainable sigma can
    produce (z^2 + 2a < 0). Returning None keeps that case honest instead of
    clamping to a fake number.
    """
    if price is None or forward is None or strike is None or t_eff_years is None:
        return None
    if not (0.0 < price < 1.0) or not (forward > 0) or not (strike > 0):
        return None
    if t_eff_years <= 0:
        return None
    a = math.log(forward / strike)
    z = norm.ppf(price)
    disc = z * z + 2.0 * a
    if disc < 0:
        return None
    w = -z + math.sqrt(disc)
    if w <= 0:
        return None
    return w / math.sqrt(t_eff_years)


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
    capture.emit("sigma", sigma=sigma_state.sigma, branch=sigma_state.branch,
                 legacy_realized_vol=legacy, robust_sigma_for_pricing=robust,
                 grid_estimate=grid_est, grid_returns=n_rets,
                 brti_buffer_n=len(brti_buffer), lookback_sec=VOL_LOOKBACK_SEC)


async def sigma_task():
    import asyncio
    while True:
        refresh_sigma(time.time())
        await asyncio.sleep(SIGMA_REFRESH_SEC)


def price_tick(book, spot, now: datetime, now_ts: float) -> dict:
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

    expiry = st.get_expiry_datetime(now)
    ticker_expiry = st.parse_ticker_expiry(book.market_ticker)
    seconds_to_expiry = expiry.timestamp() - now_ts

    n_venues = len(venue_quotes)
    if spot is not None and venue_quotes:
        cached_forward = forward_engine.compute(venue_quotes, spot, now_ts)
    else:
        cached_forward = spot
    fwd_diag = dict(forward_engine.last_diag)

    sigma_used = sigma_state.sigma
    sigma_branch = sigma_state.branch
    delta_spot = (spot - st.target_price) if (spot is not None and st.target_price is not None) else None

    p_raw = None
    t_eff_years = None
    denom = None
    z = None
    if st.target_price is not None and cached_forward is not None:
        p_raw = digital_prob(now, expiry, float(st.target_price), float(cached_forward),
                             float(sigma_used), window_sec=WINDOW_SEC, realized=brti_buffer)
        t_eff_years = effective_time(seconds_to_expiry, WINDOW_SEC)
        denom = sigma_used * math.sqrt(t_eff_years) if t_eff_years > 0 else 0.0
        if denom > 0 and cached_forward > 0 and st.target_price > 0:
            z = (math.log(cached_forward / st.target_price)
                 - 0.5 * sigma_used ** 2 * t_eff_years) / denom

    p = p_raw if (p_raw is not None and math.isfinite(p_raw)) else None

    # Market-implied sigma: back out the sigma the *book* is pricing, to compare
    # against sigma_used. Pre-window only -- see implied_sigma(). Solved off
    # best bid, best ask and the mid, so the [bid, ask] band shows whether a gap
    # to sigma_used is real or just 1-cent tick noise. Three closed-form solves,
    # a few flops; negligible next to the digital_prob call above.
    window_start_ts = expiry.timestamp() - WINDOW_SEC
    pre_window = now_ts <= window_start_ts
    iv_bid = iv_ask = iv_mid = None
    if (pre_window and t_eff_years is not None and st.target_price is not None
            and cached_forward is not None):
        _args = (float(cached_forward), float(st.target_price), float(t_eff_years))
        iv_mid = implied_sigma(market_p, *_args)
        iv_bid = implied_sigma(best_bid, *_args)
        iv_ask = implied_sigma(best_ask, *_args)

    iv_gap = None
    if iv_mid is not None and sigma_branch != "fallback":
        iv_gap = iv_mid - sigma_used

    forward_ts = state.brti_ts if state.brti_ts else None
    quotable, withhold_reason = _quote_gate(
        p, cached_forward, forward_ts, sigma_branch, n_venues)

    window_start = window_start_ts
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
        "target": st.target_price,
        "spot_minus_target": delta_spot,
        "sigma": sigma_used,
        "sigma_branch": sigma_branch,
        "sigma_raw_grid": sigma_state.grid_estimate,
        "sigma_n_returns": sigma_state.n_returns,
        "t_eff_years": t_eff_years,
        "denom_sigma_sqrtT": denom,
        "z_score": z,
        "iv_sigma_mid": iv_mid,
        "iv_sigma_bid": iv_bid,
        "iv_sigma_ask": iv_ask,
        "iv_sigma_gap": iv_gap,
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
    from state import prices
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
    prices.iv_sigma_mid = row["iv_sigma_mid"]
    prices.iv_sigma_bid = row["iv_sigma_bid"]
    prices.iv_sigma_ask = row["iv_sigma_ask"]
    prices.iv_sigma_gap = row["iv_sigma_gap"]


def _note_switch_tick(book, row: dict) -> None:
    """Emit the first-tick-after-switch record so the rollover gap is visible."""
    if not st._awaiting_first_tick:
        return
    st._awaiting_first_tick = False
    now = time.time()
    st.trace.emit(
        "first_tick_after_switch",
        ticker=book.market_ticker,
        ticker_expiry=st._ticker_stamp(book.market_ticker),
        subscribed_window=st.subscribed_window,
        seconds_since_resubscribe=(round(now - st._switch_started_ts, 3)
                                   if st._switch_started_ts else None),
        seconds_into_window=st._window_state()["seconds_into_window"],
        prob=row["prob"],
        best_yes_bid=row["best_yes_bid"],
        best_yes_ask=row["best_yes_ask"],
    )
