"""
Binance HMAC-SHA256 authentication.

How Binance auth works (simplified):
  1. You build a query string with your parameters + a timestamp.
  2. You hash that string with your secret key using HMAC-SHA256.
  3. You append the resulting hex digest as &signature=... to the request.
  4. You send your API key in the X-MBX-APIKEY header.

Binance checks that the signature matches on their side.  If it does, the
request is yours.  If not, you get a 401.

This module wraps all of that into a single `signed_request` function so the
rest of the codebase never has to think about signing.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, Optional, TYPE_CHECKING
from urllib.parse import urlencode

import requests

import config

if TYPE_CHECKING:
    from api.exchange_context import BinanceContext


def _timestamp_ms() -> int:
    """Current time in milliseconds — what Binance expects."""
    return int(time.time() * 1000)


def _sign_qs(query_string: str, secret: str) -> str:
    return hmac.new(
        secret.encode(),
        query_string.encode(),
        hashlib.sha256,
    ).hexdigest()


def public_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Send a request to a PUBLIC endpoint (no signature needed).
    Used for things like fetching the current price.
    """
    base_url = ctx.base_url if ctx else config.BASE_URL
    url = f"{base_url}{path}"
    resp = requests.request(method, url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def signed_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Send a request to a PRIVATE endpoint (signature required).
    Used for placing orders, checking balances, etc.

    The function automatically adds timestamp, recvWindow, and signature.
    """
    api_key = ctx.api_key if ctx else config.API_KEY
    api_secret = ctx.api_secret if ctx else config.API_SECRET
    base_url = ctx.base_url if ctx else config.BASE_URL
    recv_window = ctx.recv_window if ctx else config.RECV_WINDOW

    if not api_key or not api_secret:
        raise RuntimeError(
            "API key or secret is missing. "
            "Copy .env.example to .env and fill in your credentials."
        )

    params = params or {}
    params["timestamp"] = _timestamp_ms()
    params["recvWindow"] = recv_window

    query_string = urlencode(params)
    params["signature"] = _sign_qs(query_string, api_secret)

    url = f"{base_url}{path}"
    resp = requests.request(
        method, url, params=params,
        headers={"X-MBX-APIKEY": api_key},
        timeout=10,
    )
    if not resp.ok:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(
            f"Binance {resp.status_code}: {body}"
        )
    return resp.json()
