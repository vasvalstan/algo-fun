"""
Public market-data endpoints.

These do NOT require authentication — anyone can read prices.
We use them to understand the current state of the market before trading.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from auth import public_request
import config

if TYPE_CHECKING:
    from api.exchange_context import BinanceContext


def get_price(
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Get the latest price for a symbol.

    Returns something like:
        {"symbol": "BTCUSDT", "price": "67432.10000000"}
    """
    return public_request("GET", "/api/v3/ticker/price", {"symbol": symbol}, ctx=ctx)


def get_orderbook(
    symbol: str = config.SYMBOL,
    limit: int = 5,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Get the top `limit` levels of the order book.

    The order book shows:
      - bids: people willing to BUY  (sorted highest price first)
      - asks: people willing to SELL (sorted lowest price first)

    The gap between the best bid and best ask is called the "spread".
    """
    return public_request(
        "GET", "/api/v3/depth", {"symbol": symbol, "limit": limit}, ctx=ctx,
    )


def get_klines(
    symbol: str = config.SYMBOL,
    interval: str = "5m",
    limit: int = 200,
    *,
    ctx: Optional[BinanceContext] = None,
) -> list:
    """
    Fetch OHLCV klines (candlestick data) for a given interval.

    Each element is a list:
      [open_time, open, high, low, close, volume, close_time,
       quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]

    Supported intervals: 1m, 5m, 15m, 1h, 4h, 1d, 1w, 1M.
    """
    return public_request(
        "GET",
        "/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
        ctx=ctx,
    )


def get_exchange_info(
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Get trading rules for a symbol: min order size, price tick, etc.
    """
    data = public_request("GET", "/api/v3/exchangeInfo", {"symbol": symbol}, ctx=ctx)
    for s in data.get("symbols", []):
        if s["symbol"] == symbol:
            return s
    return {}
