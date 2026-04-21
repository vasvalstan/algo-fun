"""
Private trading endpoints (require HMAC signature).

These are the three core actions of any trading bot:
  1. Place an order   — tell the exchange you want to buy or sell.
  2. List open orders — see what's still waiting to be filled.
  3. Cancel an order  — pull an order back before it fills.

All functions here call `signed_request` from auth.py, which handles the
timestamp and HMAC signing automatically.
"""

from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING

from auth import signed_request
import config

if TYPE_CHECKING:
    from api.exchange_context import BinanceContext


def place_limit_order(
    side: str,
    quantity: str,
    price: str,
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Place a LIMIT order.

    Args:
        side:     "BUY" or "SELL"
        quantity: how much to buy/sell (e.g. "0.001" for 0.001 BTC)
        price:    the price you want (e.g. "50000.00")
        symbol:   trading pair, defaults to config.SYMBOL
    """
    return signed_request("POST", "/api/v3/order", {
        "symbol": symbol,
        "side": side.upper(),
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": quantity,
        "price": price,
    }, ctx=ctx)


def place_maker_order(
    side: str,
    quantity: str,
    price: str,
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Place a LIMIT_MAKER (Post Only) order.

    Identical to a limit order except Binance rejects it immediately if
    it would match and execute as a taker.  This guarantees maker fees.
    """
    return signed_request("POST", "/api/v3/order", {
        "symbol": symbol,
        "side": side.upper(),
        "type": "LIMIT_MAKER",
        "quantity": quantity,
        "price": price,
    }, ctx=ctx)


def place_market_order(
    side: str,
    quantity: str,
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Place a MARKET order.

    Used by the LIFO runner's startup orphan-BTC sweep to convert
    untracked base asset back to USDT immediately. Pays taker fees, so
    use sparingly — only for self-healing cleanup, never for normal flow.
    """
    return signed_request("POST", "/api/v3/order", {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": quantity,
    }, ctx=ctx)


def place_market_quote_order(
    side: str,
    quote_order_qty: str,
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Place a MARKET order denominated in QUOTE asset (USDT / USDC).

    Binance's `quoteOrderQty` parameter spends an exact USDT amount and
    lets the venue size the base qty itself, so we never have to worry
    about LOT_SIZE rounding when the user clicks "Buy $10 of BTC at
    market" from the dashboard.
    """
    return signed_request("POST", "/api/v3/order", {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quoteOrderQty": quote_order_qty,
    }, ctx=ctx)


def get_open_orders(
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> List[Dict]:
    """List all open (unfilled) orders for a symbol."""
    return signed_request("GET", "/api/v3/openOrders", {"symbol": symbol}, ctx=ctx)


def cancel_order(
    order_id: int,
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """Cancel a specific order by its orderId."""
    return signed_request("DELETE", "/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    }, ctx=ctx)


def get_order(
    order_id: int,
    symbol: str = config.SYMBOL,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Look up a single order by orderId — works for OPEN, FILLED, or CANCELED.

    Used for boot-time recovery: if a tracked order is no longer in
    /openOrders, this tells us whether it filled or was cancelled.
    """
    return signed_request("GET", "/api/v3/order", {
        "symbol": symbol,
        "orderId": order_id,
    }, ctx=ctx)


def get_account(*, ctx: Optional[BinanceContext] = None) -> dict:
    """Get account information including all balances."""
    return signed_request("GET", "/api/v3/account", ctx=ctx)
