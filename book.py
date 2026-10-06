"""Kalshi event order book. Hot path lives here; mirrors the exchange handling.

Stored identically to MarketMakingBase.OrderBook:
    self.bids : {rounded_price -> rounded_qty}  from yes_dollars_fp / 'yes'
    self.asks : {rounded_price -> rounded_qty}  from no_dollars_fp  / 'no'

Snapshot and delta handling, rounding, and the add/delete rule are copied from
the base class so a Python `NaN` sentinel drift cannot occur.

Derived top of book:
    best_yes_bid = max(self.bids)              (top of the YES book)
    best_no_bid  = max(self.asks)              (top of the NO  book, event)
    best_yes_ask = 1 - best_no_bid             (cheapest way to buy YES)
"""

from __future__ import annotations

import time

from config import EVENT_BOOK_CONFIG


class OrderBook:
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
