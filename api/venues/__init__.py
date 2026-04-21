"""
Venue protocol for LIFO grid runners.

A Venue wraps a single exchange/account behind a small, uniform interface.
The LIFO engine is venue-free; the runner composes Engine + Venue + state.json
+ WebSocket channel per deployment target.

Fee model:
  * "bnb_subsidized"  → the buyer receives the full requested BTC qty
                        (Binance with "Use BNB for fees" ON).
  * "deducted"        → fee comes out of the received base asset; buyer
                        ends up with qty * (1 - fee_rate) BTC
                        (Revolut X today).
  * "paper_free"      → for paper simulation with fee_rate=0 (we fake it).

All Venues treat order IDs as strings (Binance int → str). The engine does
too, so Binance ids are canonically stringified at the venue boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, runtime_checkable


FeeModel = Literal["bnb_subsidized", "deducted", "paper_free"]

# Canonical statuses returned by Venue.get_order_status() — superset of
# Binance/Revolut native enums normalised to one vocabulary.
OrderStatus = Literal["OPEN", "FILLED", "PARTIALLY_FILLED", "CANCELED", "UNKNOWN"]


@dataclass(frozen=True)
class VenueSpec:
    """Declarative metadata every Venue exposes."""

    name: str                          # "binance-live" | "binance-testnet" | …
    platform: Literal["binance", "revolut"]
    account_mode: Literal["live", "paper"]
    symbol: str                        # e.g. "BTCUSDT" or "BTC-USD"
    quote_asset: str                   # "USDT" | "USD"
    base_asset: str                    # "BTC"
    price_prec: int
    qty_prec: int
    min_notional: float                # lowest allowed price * qty
    fee_model: FeeModel
    fee_rate: float                    # per-leg, as a fraction (0.0015 = 0.15%)
    ws_channel: str                    # WS channel the runner broadcasts on


@dataclass(frozen=True)
class PlacedOrder:
    """Return value from place_limit_buy / place_limit_sell."""

    order_id: str
    price: float
    requested_qty: float


@runtime_checkable
class Venue(Protocol):
    """All runners interact with their exchange strictly through this API."""

    @property
    def spec(self) -> VenueSpec: ...

    # ── Price / account ───────────────────────────────────────────

    def get_price(self) -> float: ...

    def get_balances(self) -> dict[str, float]:
        """Return {asset: free+locked}. Must include at least base and quote."""
        ...

    def get_detailed_balances(self) -> dict[str, dict[str, float]]:
        """
        Return {asset: {"free": float, "locked": float}}.

        Optional — venues that don't override this fall back to treating
        get_balances() as fully-free funds (locked = 0).
        """
        ...

    def starting_equity_usdt(self) -> float:
        """Total account value denominated in USDT/USD at bootstrap."""
        ...

    # ── Orders ────────────────────────────────────────────────────

    def get_open_order_ids(self) -> set[str]: ...

    def get_open_orders_detail(self) -> list[dict]:
        """
        Return open orders as a list of plain dicts:
            {"order_id": str, "side": "BUY"|"SELL", "price": float, "qty": float}

        Used by runners to enrich the Telegram start/stop notifications.
        Implementations should never raise — return [] on any failure.
        """
        ...

    def place_limit_buy(self, price: float, qty: float) -> PlacedOrder:
        """Place a post-only (maker) limit BUY. Raise on failure."""
        ...

    def place_limit_sell(self, price: float, qty: float) -> PlacedOrder:
        """Place a post-only (maker) limit SELL. Raise on failure."""
        ...

    def place_market_buy(self, quote_amount: float) -> PlacedOrder:
        """
        Spend `quote_amount` of the quote asset (USDT/USDC) to buy base at
        market. Used by the dashboard "Buy Now" button to force-open a
        fresh LIFO bag on demand. Pays taker fees.

        Returns PlacedOrder where:
          * price          = volume-weighted average fill price
          * requested_qty  = base qty actually executed (gross of fees)
        """
        ...

    def cancel(self, order_id: str) -> None: ...

    def cancel_all(self) -> None: ...

    def get_order_status(self, order_id: str) -> tuple[OrderStatus, float]:
        """
        Look up a SINGLE order by id — used at boot to disambiguate orders
        that aren't in /openOrders. Returns (status, executed_qty).

        executed_qty is the actual filled base qty (already net of fees on
        venues whose fee_model='deducted').
        """
        ...

    # ── Fee math (venue-specific) ─────────────────────────────────

    def filled_qty_after_fees(self, requested_qty: float) -> float:
        """
        What actually lands in the wallet for a filled BUY of `requested_qty`.

        Binance + BNB → requested_qty (no deduction).
        Revolut       → requested_qty * (1 - fee_rate), floored to qty_prec.
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────────

    def is_ready(self) -> bool:
        """False if credentials/missing config prevents use; runner will self-disable."""
        ...


# ── Shared fee helpers used by every executor ───────────────────────

import math


def _floor(value: float, decimals: int) -> float:
    if decimals <= 0:
        return float(math.floor(value))
    step = 10.0 ** decimals
    return math.floor(value * step) / step


def apply_fee_model(
    requested_qty: float,
    spec: VenueSpec,
) -> float:
    """Translate venue fee model into the qty that actually lands in wallet."""
    if spec.fee_model in ("bnb_subsidized", "paper_free"):
        return requested_qty
    # deducted
    net = requested_qty * (1.0 - spec.fee_rate)
    return _floor(net, spec.qty_prec)
