"""
Revolut X venue — live (real money) and paper (in-memory simulation).

Revolut X has no testnet, so "paper" here is a local fill simulator that
honours Revolut precision + fee semantics so param tuning is meaningful
before promoting a config to live.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
import uuid
from typing import Any, Optional

from revolut_x import revx_request
from api.venues import PlacedOrder, VenueSpec, apply_fee_model

log = logging.getLogger(__name__)


_TICKERS_ENDPOINT = "/tickers"


def _revolut_symbol() -> str:
    # Default to BTC-USDC: USDC is what the LIFO bot is funded in on Revolut X
    # (no FX leg, USDC ≈ USD pricing). Override with REVOLUT_X_SYMBOL env var
    # if you ever want to trade BTC-USD or another pair.
    return os.getenv("REVOLUT_X_SYMBOL", "BTC-USDC").strip()


# ── Live venue ─────────────────────────────────────────────────────


class RevolutLiveVenue:
    """Real Revolut X account — post-only limit orders, deducted fees."""

    def __init__(
        self,
        *,
        symbol: Optional[str] = None,
        price_prec: int = 2,
        qty_prec: int = 8,
        min_notional: float = 5.0,
        fee_rate: float = 0.0015,  # 0.15% maker default (user pick)
        ws_channel: str = "revolut_live",
    ) -> None:
        sym = (symbol or _revolut_symbol()).upper()
        base, quote = (sym.split("-") + ["USD"])[:2]
        self._spec = VenueSpec(
            name="revolut-live",
            platform="revolut",
            account_mode="live",
            symbol=sym,
            base_asset=base,
            quote_asset=quote,
            price_prec=price_prec,
            qty_prec=qty_prec,
            min_notional=min_notional,
            fee_model="deducted",
            fee_rate=fee_rate,
            ws_channel=ws_channel,
        )

    # ── Protocol ──────────────────────────────────────────────────

    @property
    def spec(self) -> VenueSpec:
        return self._spec

    def is_ready(self) -> bool:
        try:
            # Lightweight ping — /tickers is public for authenticated keys.
            revx_request("GET", _TICKERS_ENDPOINT, params={"symbols": self._spec.symbol})
            return True
        except Exception as exc:
            log.warning("Revolut X not ready: %s", exc)
            return False

    def get_price(self) -> float:
        data = revx_request("GET", _TICKERS_ENDPOINT, params={"symbols": self._spec.symbol})
        rows = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(rows, list):
            for t in rows:
                if t.get("symbol") == self._spec.symbol:
                    return float(t["last_price"])
            if rows:
                return float(rows[0].get("last_price", 0))
        if isinstance(rows, dict):
            return float(rows.get("last_price", 0))
        return 0.0

    def get_balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for asset, bal in self.get_detailed_balances().items():
            out[asset] = bal["free"] + bal["locked"]
        return out

    def get_detailed_balances(self) -> dict[str, dict[str, float]]:
        """
        Return {asset: {free, locked}} for the trading pair's base + quote.

        Filtered to only the active pair (matches Binance behaviour) so
        unrelated funds in the Revolut wallet (GBP, EUR, USD) don't clutter
        the dashboard. Always includes the base + quote even if zero so the
        UI shows "0 USDC" / "0 BTC" for a flat account.
        """
        try:
            data = revx_request("GET", "/balances")
        except Exception as exc:
            log.warning("Revolut detailed balances fetch failed: %s", exc)
            return {}
        rows = data.get("data", data) if isinstance(data, dict) else data
        wanted = {self._spec.base_asset, self._spec.quote_asset}
        out: dict[str, dict[str, float]] = {a: {"free": 0.0, "locked": 0.0} for a in wanted}
        if isinstance(rows, list):
            for b in rows:
                asset = b.get("currency", b.get("asset", ""))
                if not asset or asset not in wanted:
                    continue
                free = float(b.get("available", b.get("free", 0.0)) or 0.0)
                locked = float(b.get("reserved", b.get("locked", 0.0)) or 0.0)
                out[asset] = {"free": free, "locked": locked}
        return out

    def starting_equity_usdt(self) -> float:
        bals = self.get_balances()
        quote = bals.get(self._spec.quote_asset, 0.0)
        base = bals.get(self._spec.base_asset, 0.0)
        price = self.get_price() if base > 0 else 0.0
        return quote + base * price

    def get_open_order_ids(self) -> set[str]:
        return {o["order_id"] for o in self.get_open_orders_detail()}

    def get_open_orders_detail(self) -> list[dict]:
        """
        Active orders from Revolut X.

        Revolut's /orders/active payload is FLAT (top-level `price`,
        `quantity`, `side`, `created_date` in epoch ms). The earlier
        `order_configuration.limit.{price,base_size}` shape we used to
        look at applies only to the request body when *placing* orders —
        responses don't echo it back. Without this fix, qty + time both
        rendered as zero in the dashboard.
        """
        try:
            data = revx_request("GET", "/orders/active", params={"symbols": self._spec.symbol})
            rows = data.get("data", data) if isinstance(data, dict) else data
        except Exception as exc:
            log.warning("Revolut open orders fetch failed: %s", exc)
            return []
        out: list[dict] = []
        if not isinstance(rows, list):
            return out
        for o in rows:
            oid = str(o.get("id") or o.get("order_id") or o.get("venue_order_id") or "")
            if not oid:
                continue
            # Read top-level fields first; fall back to legacy nested shape.
            cfg = (o.get("order_configuration") or {}).get("limit") or {}
            try:
                price = float(o.get("price") or cfg.get("price") or 0.0)
                qty = float(
                    o.get("quantity")
                    or o.get("leaves_quantity")
                    or cfg.get("base_size")
                    or o.get("base_size")
                    or o.get("qty")
                    or 0.0
                )
                created_ms = int(o.get("created_date") or o.get("created_at") or 0)
            except (TypeError, ValueError):
                price, qty, created_ms = 0.0, 0.0, 0
            out.append({
                "order_id": oid,
                "side": str(o.get("side", "")).upper(),
                "price": price,
                "qty": qty,
                "time": created_ms,
            })
        return out

    def _place(self, side: str, price: float, qty: float) -> PlacedOrder:
        body = {
            "client_order_id": str(uuid.uuid4()),
            "symbol": self._spec.symbol,
            "side": side.lower(),
            "order_configuration": {
                "limit": {
                    "base_size": f"{qty:.{self._spec.qty_prec}f}",
                    "price": f"{price:.{self._spec.price_prec}f}",
                    "execution_instructions": ["post_only"],
                },
            },
        }
        resp = revx_request("POST", "/orders", json_body=body)
        order = resp.get("data", resp) if isinstance(resp, dict) else resp
        oid = str(order.get("venue_order_id", order.get("id", order.get("order_id", ""))))
        if not oid:
            raise RuntimeError(f"Revolut X did not return an order id: {resp!r}")
        return PlacedOrder(order_id=oid, price=price, requested_qty=qty)

    def place_limit_buy(self, price: float, qty: float) -> PlacedOrder:
        return self._place("buy", price, qty)

    def place_limit_sell(self, price: float, qty: float) -> PlacedOrder:
        return self._place("sell", price, qty)

    def place_market_buy(self, quote_amount: float) -> PlacedOrder:
        """
        Spend ~`quote_amount` USDC on a MARKET BUY. Revolut X requires
        the request body in BASE units, so we size against the current
        spot price with a small (~0.5%) headroom haircut to avoid
        exceeding `quote_amount` if the book ticks up between the
        quote we read and the order landing on the venue.

        Pays taker fees. Returns the venue-reported fill price + qty
        when present, falling back to the requested values otherwise.
        """
        spot = self.get_price()
        if spot <= 0:
            raise RuntimeError("Revolut market buy: no live price available")
        # 0.5% headroom protects against the price drifting up between
        # quote and execution. The user asked for "approx $X", not "≥ $X".
        target_quote = float(quote_amount) * 0.995
        raw_qty = target_quote / spot
        # Floor to qty_prec so Revolut accepts the size.
        step = 10.0 ** self._spec.qty_prec
        qty = math.floor(raw_qty * step) / step
        if qty <= 0:
            raise RuntimeError(
                f"Revolut market buy: derived qty {raw_qty:.10f} rounds to 0 "
                f"at qty_prec={self._spec.qty_prec}; spend more than "
                f"{(1.0 / step) * spot:.2f} {self._spec.quote_asset}"
            )
        body = {
            "client_order_id": str(uuid.uuid4()),
            "symbol": self._spec.symbol,
            "side": "buy",
            "order_configuration": {
                "market": {"base_size": f"{qty:.{self._spec.qty_prec}f}"},
            },
        }
        resp = revx_request("POST", "/orders", json_body=body)
        order = resp.get("data", resp) if isinstance(resp, dict) else resp
        oid = str(order.get("venue_order_id", order.get("id", order.get("order_id", ""))))
        if not oid:
            raise RuntimeError(f"Revolut X did not return an order id: {resp!r}")
        # Revolut may not echo the fill price on the placement response;
        # fall back to the spot we used for sizing. The runner stores
        # this as the bag's entry price for TP math.
        try:
            fill_price = float(order.get("price") or order.get("average_price") or spot)
        except (TypeError, ValueError):
            fill_price = spot
        try:
            executed = float(order.get("filled_size") or order.get("executed_qty") or qty)
        except (TypeError, ValueError):
            executed = qty
        return PlacedOrder(order_id=oid, price=fill_price, requested_qty=executed)

    def place_market_sell(self, qty: float) -> PlacedOrder:
        """
        Convert `qty` base asset → quote at market on Revolut X.

        Mirrors `place_market_buy` shape but with side="sell". Used by:
          * the reset / liquidation script (full BTC dump → USDC)
          * the runner's startup orphan-BTC sweep — without this method
            `_sweep_orphan_btc_if_any` short-circuits on Revolut and any
            untracked base-asset dust stays stranded across redeploys.

        Pays taker fees. Floors `qty` to qty_prec so the request body is
        always within precision; raises if the floored size rounds to zero.
        """
        step = 10.0 ** self._spec.qty_prec
        sized = math.floor(float(qty) * step) / step
        if sized <= 0:
            raise RuntimeError(
                f"Revolut market sell: qty {qty:.10f} rounds to 0 at "
                f"qty_prec={self._spec.qty_prec}"
            )
        body = {
            "client_order_id": str(uuid.uuid4()),
            "symbol": self._spec.symbol,
            "side": "sell",
            "order_configuration": {
                "market": {"base_size": f"{sized:.{self._spec.qty_prec}f}"},
            },
        }
        resp = revx_request("POST", "/orders", json_body=body)
        order = resp.get("data", resp) if isinstance(resp, dict) else resp
        oid = str(order.get("venue_order_id", order.get("id", order.get("order_id", ""))))
        if not oid:
            raise RuntimeError(f"Revolut X did not return an order id: {resp!r}")
        spot = self.get_price() or 0.0
        try:
            fill_price = float(order.get("price") or order.get("average_price") or spot)
        except (TypeError, ValueError):
            fill_price = spot
        try:
            executed = float(order.get("filled_size") or order.get("executed_qty") or sized)
        except (TypeError, ValueError):
            executed = sized
        return PlacedOrder(order_id=oid, price=fill_price, requested_qty=executed)

    def cancel(self, order_id: str) -> None:
        try:
            revx_request("DELETE", f"/orders/{order_id}")
        except Exception as exc:
            log.warning("Revolut cancel %s failed: %s", order_id, exc)

    def cancel_all(self) -> None:
        for oid in self.get_open_order_ids():
            self.cancel(oid)

    def get_order_status(self, order_id: str) -> tuple[str, float]:
        """
        Map Revolut order state → canonical OrderStatus.

        Revolut uses lowercase strings. Common: pending, accepted, working,
        filled, partially_filled, canceled, rejected, expired.
        """
        try:
            data = revx_request("GET", f"/orders/{order_id}")
        except Exception as exc:
            log.warning("Revolut get_order %s failed: %s", order_id, exc)
            return ("UNKNOWN", 0.0)
        order = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(order, dict):
            return ("UNKNOWN", 0.0)
        st = str(order.get("state", order.get("status", ""))).lower()
        executed = float(order.get("filled_size", order.get("executed_qty", 0.0)) or 0.0)
        if st in ("filled", "completed", "done"):
            return ("FILLED", executed)
        if st in ("partially_filled", "partial"):
            return ("PARTIALLY_FILLED", executed)
        if st in ("pending", "accepted", "working", "open", "new"):
            return ("OPEN", executed)
        if st in ("canceled", "cancelled", "rejected", "expired", "failed"):
            return ("CANCELED", executed)
        return ("UNKNOWN", executed)

    def filled_qty_after_fees(self, requested_qty: float) -> float:
        return apply_fee_model(requested_qty, self._spec)


# ── Paper venue (in-memory) ────────────────────────────────────────


class _PaperOrder:
    __slots__ = ("order_id", "side", "price", "qty", "placed_at")

    def __init__(self, order_id: str, side: str, price: float, qty: float) -> None:
        self.order_id = order_id
        self.side = side  # "buy" | "sell"
        self.price = price
        self.qty = qty
        self.placed_at = time.time()


class RevolutPaperVenue:
    """
    In-memory Revolut paper venue.

    Fills happen when the cross-tick price range ([min(prev,cur), max(prev,cur)])
    crosses the resting order's price. Fees applied per VenueSpec.

    Price source: delegates to the live Revolut tickers endpoint so paper trades
    on real market data without spending quota on writes. If unauthenticated,
    caller may inject a custom price_source() callback.
    """

    def __init__(
        self,
        *,
        symbol: Optional[str] = None,
        starting_usdt: float = 1000.0,
        fee_rate: float = 0.0015,
        price_source: Optional[Any] = None,  # callable() -> float
        ws_channel: str = "revolut_paper",
    ) -> None:
        sym = (symbol or _revolut_symbol()).upper()
        base, quote = (sym.split("-") + ["USD"])[:2]
        self._spec = VenueSpec(
            name="revolut-paper",
            platform="revolut",
            account_mode="paper",
            symbol=sym,
            base_asset=base,
            quote_asset=quote,
            price_prec=2,
            qty_prec=8,
            min_notional=5.0,
            fee_model="deducted",
            fee_rate=fee_rate,
            ws_channel=ws_channel,
        )

        self._wallet = {quote: float(starting_usdt), base: 0.0}
        self._open: dict[str, _PaperOrder] = {}
        self._filled_this_tick: set[str] = set()
        self._lock = threading.Lock()
        self._order_seq = 0

        self._price_source = price_source
        self._prev_price: float = 0.0
        self._last_price: float = 0.0

    # ── Tick driver (runner calls this before each evaluate pass) ──

    def _resolve_price(self) -> float:
        if self._price_source is not None:
            try:
                return float(self._price_source())
            except Exception as exc:
                log.debug("Revolut paper price_source failed: %s", exc)
        try:
            data = revx_request("GET", _TICKERS_ENDPOINT, params={"symbols": self._spec.symbol})
            rows = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(rows, list):
                for t in rows:
                    if t.get("symbol") == self._spec.symbol:
                        return float(t["last_price"])
                if rows:
                    return float(rows[0].get("last_price", 0) or 0)
            if isinstance(rows, dict):
                return float(rows.get("last_price", 0) or 0)
        except Exception as exc:
            log.debug("Revolut paper price fetch fallback: %s", exc)
        return self._last_price or 0.0

    def advance_tick(self) -> None:
        """Evaluate fills against the latest price. Runner calls this each tick."""
        price = self._resolve_price()
        if price <= 0:
            return
        prev = self._prev_price if self._prev_price > 0 else price
        lo, hi = min(prev, price), max(prev, price)

        with self._lock:
            self._filled_this_tick.clear()
            for oid, o in list(self._open.items()):
                if o.side == "buy" and lo <= o.price:
                    self._fill_buy(o)
                    del self._open[oid]
                    self._filled_this_tick.add(oid)
                elif o.side == "sell" and hi >= o.price:
                    self._fill_sell(o)
                    del self._open[oid]
                    self._filled_this_tick.add(oid)

        self._prev_price = price
        self._last_price = price

    def _fill_buy(self, o: _PaperOrder) -> None:
        cost = o.qty * o.price
        filled_qty = apply_fee_model(o.qty, self._spec)
        self._wallet[self._spec.quote_asset] -= cost
        self._wallet[self._spec.base_asset] = self._wallet.get(self._spec.base_asset, 0.0) + filled_qty

    def _fill_sell(self, o: _PaperOrder) -> None:
        gross = o.qty * o.price
        fee = gross * self._spec.fee_rate
        self._wallet[self._spec.base_asset] = max(0.0, self._wallet.get(self._spec.base_asset, 0.0) - o.qty)
        self._wallet[self._spec.quote_asset] = self._wallet.get(self._spec.quote_asset, 0.0) + gross - fee

    # ── Protocol ──────────────────────────────────────────────────

    @property
    def spec(self) -> VenueSpec:
        return self._spec

    def is_ready(self) -> bool:
        return True

    def get_price(self) -> float:
        if self._last_price <= 0:
            self._last_price = self._resolve_price()
        return self._last_price

    def get_balances(self) -> dict[str, float]:
        with self._lock:
            return dict(self._wallet)

    def starting_equity_usdt(self) -> float:
        price = self.get_price()
        bals = self.get_balances()
        return bals.get(self._spec.quote_asset, 0.0) + bals.get(self._spec.base_asset, 0.0) * price

    def get_open_order_ids(self) -> set[str]:
        with self._lock:
            return set(self._open.keys())

    def get_open_orders_detail(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "order_id": o.order_id,
                    "side": o.side.upper(),
                    "price": o.price,
                    "qty": o.qty,
                }
                for o in self._open.values()
            ]

    def _new_oid(self) -> str:
        self._order_seq += 1
        return f"paper-{self._order_seq}"

    def place_limit_buy(self, price: float, qty: float) -> PlacedOrder:
        oid = self._new_oid()
        with self._lock:
            self._open[oid] = _PaperOrder(oid, "buy", price, qty)
        return PlacedOrder(order_id=oid, price=price, requested_qty=qty)

    def place_limit_sell(self, price: float, qty: float) -> PlacedOrder:
        oid = self._new_oid()
        with self._lock:
            self._open[oid] = _PaperOrder(oid, "sell", price, qty)
        return PlacedOrder(order_id=oid, price=price, requested_qty=qty)

    def place_market_buy(self, quote_amount: float) -> PlacedOrder:
        """In-memory market buy: fill instantly at the latest tick."""
        price = self.get_price()
        if price <= 0:
            raise RuntimeError("Revolut paper market buy: no price available")
        step = 10.0 ** self._spec.qty_prec
        qty = math.floor((float(quote_amount) / price) * step) / step
        if qty <= 0:
            raise RuntimeError(f"Revolut paper market buy: qty rounds to 0 at price {price}")
        oid = self._new_oid()
        with self._lock:
            self._wallet[self._spec.quote_asset] -= qty * price
            self._wallet[self._spec.base_asset] = self._wallet.get(self._spec.base_asset, 0.0) + apply_fee_model(qty, self._spec)
            self._filled_this_tick.add(oid)
        return PlacedOrder(order_id=oid, price=price, requested_qty=qty)

    def place_market_sell(self, qty: float) -> PlacedOrder:
        """In-memory market sell: fill instantly at the latest tick."""
        price = self.get_price()
        if price <= 0:
            raise RuntimeError("Revolut paper market sell: no price available")
        step = 10.0 ** self._spec.qty_prec
        sized = math.floor(float(qty) * step) / step
        if sized <= 0:
            raise RuntimeError(f"Revolut paper market sell: qty rounds to 0 at price {price}")
        oid = self._new_oid()
        with self._lock:
            base_held = self._wallet.get(self._spec.base_asset, 0.0)
            actual = min(sized, base_held)
            gross = actual * price
            fee = gross * self._spec.fee_rate
            self._wallet[self._spec.base_asset] = max(0.0, base_held - actual)
            self._wallet[self._spec.quote_asset] = self._wallet.get(self._spec.quote_asset, 0.0) + gross - fee
            self._filled_this_tick.add(oid)
        return PlacedOrder(order_id=oid, price=price, requested_qty=actual)

    def cancel(self, order_id: str) -> None:
        with self._lock:
            self._open.pop(order_id, None)

    def cancel_all(self) -> None:
        with self._lock:
            self._open.clear()

    def get_order_status(self, order_id: str) -> tuple[str, float]:
        """
        Trivial in-memory lookup. Anything not currently open is considered
        FILLED if it disappeared this tick, otherwise UNKNOWN (e.g. across a
        process restart the in-memory map is empty).
        """
        with self._lock:
            if order_id in self._open:
                return ("OPEN", 0.0)
            if order_id in self._filled_this_tick:
                return ("FILLED", 0.0)
        return ("UNKNOWN", 0.0)

    def filled_qty_after_fees(self, requested_qty: float) -> float:
        return apply_fee_model(requested_qty, self._spec)


# ── Factories ──────────────────────────────────────────────────────


def revolut_live_venue(fee_rate: float = 0.0015) -> RevolutLiveVenue:
    return RevolutLiveVenue(fee_rate=fee_rate)


def revolut_paper_venue(*, starting_usdt: float = 1000.0, fee_rate: float = 0.0015) -> RevolutPaperVenue:
    return RevolutPaperVenue(starting_usdt=starting_usdt, fee_rate=fee_rate)
