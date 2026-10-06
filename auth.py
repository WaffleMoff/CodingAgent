"""Kalshi auth headers, websocket connect helper, and market discovery."""

from __future__ import annotations

import base64
import inspect
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from config import (
    KEY_ID, PRIVATE_KEY, MAX_FRAME_BYTES, EVENT_BASE_URL, log,
)


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
