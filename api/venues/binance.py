"""
Binance venue — handles BOTH mainnet (live) and testnet (paper) via BinanceContext.

One class, constructed with a context; no per-venue duplication.
"""

from __future__ import annotations

import logging
from typing import Optional

import config
import market_data
import trading
from api.exchange_context import BinanceContext, mainnet_context, testnet_context
from api.venues import PlacedOrder, VenueSpec, apply_fee_model

log = logging.getLogger(__name__)


def _fetch_precision(ctx: BinanceContext) -> tuple[int, int, float]:
    price_prec, qty_prec, min_notional = 2, 5, 5.0
    try:
        info = market_data.get_exchange_info(symbol=ctx.symbol, ctx=ctx)
    except AttributeError:
        info = {}
    except Exception as exc:
        log.warning("Binance exchange info fetch failed: %s", exc)
        info = {}
    for f in info.get("filters", []) or []:
        if f["filterType"] == "PRICE_FILTER":
            tick = f["tickSize"]
            price_prec = max(0, len(tick.rstrip("0").split(".")[-1]))
        elif f["filterType"] == "LOT_SIZE":
            step = f["stepSize"]
            qty_prec = max(0, len(step.rstrip("0").split(".")[-1]))
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            raw = f.get("minNotional") or f.get("notional")
            if raw is not None:
                min_notional = float(raw)
    return price_prec, qty_prec, min_notional


class BinanceVenue:
    """Venue wrapper for Binance spot (mainnet OR testnet)."""

    def __init__(
        self,
        ctx: BinanceContext,
        *,
        account_mode: str,
        ws_channel: str,
        fee_model: str,
        fee_rate: float,
        forced_price_prec: Optional[int] = None,
        forced_qty_prec: Optional[int] = None,
        forced_min_notional: Optional[float] = None,
    ) -> None:
        self._ctx = ctx
        symbol = ctx.symbol.upper()
        # Split BTCUSDT → base=BTC, quote=USDT by well-known quote suffixes.
        # 4-char suffixes (USDT, USDC, FDUSD, BUSD, TUSD) MUST be checked
        # before falling back to the 3-char heuristic, otherwise BTCUSDC
        # parses as base="BTCU" + quote="SDC".
        quote = next(
            (q for q in ("FDUSD", "BUSD", "TUSD", "USDT", "USDC") if symbol.endswith(q)),
            symbol[-3:],
        )
        base = symbol[: -len(quote)]

        pp, qp, mn = _fetch_precision(ctx) if ctx.api_key else (2, 5, 5.0)
        self._spec = VenueSpec(
            name=f"binance-{account_mode}",
            platform="binance",
            account_mode=account_mode,  # "live" | "paper"
            symbol=symbol,
            base_asset=base,
            quote_asset=quote,
            price_prec=forced_price_prec if forced_price_prec is not None else pp,
            qty_prec=forced_qty_prec if forced_qty_prec is not None else qp,
            min_notional=forced_min_notional if forced_min_notional is not None else mn,
            fee_model=fee_model,  # "bnb_subsidized" for mainnet, "paper_free" or "bnb_subsidized" for testnet
            fee_rate=fee_rate,
            ws_channel=ws_channel,
        )

    # ── Protocol ──────────────────────────────────────────────────

    @property
    def spec(self) -> VenueSpec:
        return self._spec

    def is_ready(self) -> bool:
        return bool(self._ctx.api_key and self._ctx.api_secret)

    def get_price(self) -> float:
        data = market_data.get_price(symbol=self._spec.symbol, ctx=self._ctx)
        return float(data["price"])

    def get_balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for asset, bal in self.get_detailed_balances().items():
            out[asset] = bal["free"] + bal["locked"]
        return out

    def get_detailed_balances(self) -> dict[str, dict[str, float]]:
        """Return {asset: {free, locked}} for the symbol's base, quote, and BNB."""
        try:
            acct = trading.get_account(ctx=self._ctx)
        except Exception as exc:
            log.warning("Binance detailed balances fetch failed: %s", exc)
            return {}
        out: dict[str, dict[str, float]] = {}
        wanted = {self._spec.base_asset, self._spec.quote_asset, "BNB"}
        for b in acct.get("balances", []) or []:
            if b.get("asset") not in wanted:
                continue
            free = float(b.get("free", 0.0) or 0.0)
            locked = float(b.get("locked", 0.0) or 0.0)
            if free > 0 or locked > 0:
                out[b["asset"]] = {"free": free, "locked": locked}
        return out

    def starting_equity_usdt(self) -> float:
        bals = self.get_balances()
        price = self.get_price() if bals.get(self._spec.base_asset, 0) > 0 else 0.0
        return bals.get(self._spec.quote_asset, 0.0) + bals.get(self._spec.base_asset, 0.0) * price

    def get_open_order_ids(self) -> set[str]:
        orders = trading.get_open_orders(symbol=self._spec.symbol, ctx=self._ctx)
        return {str(o["orderId"]) for o in orders}

    def get_open_orders_detail(self) -> list[dict]:
        try:
            orders = trading.get_open_orders(symbol=self._spec.symbol, ctx=self._ctx)
        except Exception as exc:
            log.warning("Binance open-orders detail fetch failed: %s", exc)
            return []
        out: list[dict] = []
        for o in orders or []:
            try:
                out.append({
                    "order_id": str(o["orderId"]),
                    "side": str(o.get("side", "")).upper(),
                    "price": float(o.get("price", 0.0)),
                    "qty": float(o.get("origQty", 0.0)),
                    "time": int(o.get("time", 0) or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _format(self, price: float, qty: float) -> tuple[str, str]:
        p = f"{price:.{self._spec.price_prec}f}"
        q = f"{qty:.{self._spec.qty_prec}f}"
        return p, q

    def place_limit_buy(self, price: float, qty: float) -> PlacedOrder:
        p, q = self._format(price, qty)
        resp = trading.place_maker_order(
            side="BUY", quantity=q, price=p, symbol=self._spec.symbol, ctx=self._ctx,
        )
        return PlacedOrder(order_id=str(resp["orderId"]), price=price, requested_qty=qty)

    def place_limit_sell(self, price: float, qty: float) -> PlacedOrder:
        p, q = self._format(price, qty)
        resp = trading.place_maker_order(
            side="SELL", quantity=q, price=p, symbol=self._spec.symbol, ctx=self._ctx,
        )
        return PlacedOrder(order_id=str(resp["orderId"]), price=price, requested_qty=qty)

    def place_market_buy(self, quote_amount: float) -> PlacedOrder:
        """
        Spend `quote_amount` USDT on a MARKET BUY using Binance's
        `quoteOrderQty` parameter. The venue handles base-qty sizing
        and lot-size rounding for us, so even tiny ($5–$10) orders work.

        Pays taker fees. Returns the volume-weighted average fill price
        and the executed base qty (gross — fee deduction is handled by
        `filled_qty_after_fees`, which is a no-op for BNB-subsidised
        Binance accounts).
        """
        # Format with 2-dp quote precision; USDT is a 2dp asset and
        # Binance accepts up to 8dp here, so this is always safe.
        quote_str = f"{float(quote_amount):.2f}"
        resp = trading.place_market_quote_order(
            side="BUY", quote_order_qty=quote_str, symbol=self._spec.symbol, ctx=self._ctx,
        )
        executed = float(resp.get("executedQty", 0.0))
        cum_quote = float(resp.get("cummulativeQuoteQty", 0.0))
        fill_price = (cum_quote / executed) if executed > 0 else 0.0
        return PlacedOrder(
            order_id=str(resp["orderId"]),
            price=fill_price,
            requested_qty=executed,
        )

    def place_market_sell(self, qty: float) -> PlacedOrder:
        """
        Convert `qty` base asset → quote at market. Used ONLY by the LIFO
        runner's startup orphan-BTC sweep, never in normal flow. Pays
        taker fees; price comes back from the fill report.
        """
        _, q = self._format(0.0, qty)
        resp = trading.place_market_order(
            side="SELL", quantity=q, symbol=self._spec.symbol, ctx=self._ctx,
        )
        # Best-effort fill price; cummulativeQuoteQty / executedQty.
        executed = float(resp.get("executedQty", 0.0))
        cum_quote = float(resp.get("cummulativeQuoteQty", 0.0))
        fill_price = (cum_quote / executed) if executed > 0 else 0.0
        return PlacedOrder(order_id=str(resp["orderId"]), price=fill_price, requested_qty=executed or qty)

    def cancel(self, order_id: str) -> None:
        try:
            trading.cancel_order(int(order_id), symbol=self._spec.symbol, ctx=self._ctx)
        except Exception as exc:
            log.warning("Binance cancel %s failed: %s", order_id, exc)

    def cancel_all(self) -> None:
        for oid in self.get_open_order_ids():
            self.cancel(oid)

    def get_order_status(self, order_id: str) -> tuple[str, float]:
        """
        Map Binance order status → canonical OrderStatus.

        Binance enum: NEW, PARTIALLY_FILLED, FILLED, CANCELED, PENDING_CANCEL,
                      REJECTED, EXPIRED, EXPIRED_IN_MATCH.
        """
        try:
            o = trading.get_order(int(order_id), symbol=self._spec.symbol, ctx=self._ctx)
        except Exception as exc:
            log.warning("Binance get_order %s failed: %s", order_id, exc)
            return ("UNKNOWN", 0.0)
        st = str(o.get("status", "")).upper()
        executed = float(o.get("executedQty", 0.0))
        if st == "FILLED":
            return ("FILLED", executed)
        if st == "PARTIALLY_FILLED":
            return ("PARTIALLY_FILLED", executed)
        if st == "NEW":
            return ("OPEN", executed)
        if st in ("CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "PENDING_CANCEL"):
            return ("CANCELED", executed)
        return ("UNKNOWN", executed)

    def filled_qty_after_fees(self, requested_qty: float) -> float:
        return apply_fee_model(requested_qty, self._spec)


# ── Factories ───────────────────────────────────────────────────────


def binance_live_venue() -> BinanceVenue:
    """
    Mainnet Binance with BNB fees enabled (per spec §2 'BNB Fee Mandate').

    fee_rate is informational only — `bnb_subsidized` does NOT deduct from
    the base asset (the fee is paid in BNB on a separate ledger). The number
    is the official Binance VIP-0 maker fee for the configured symbol class:

      * BTCUSDT (and other USDT pairs)  → 0.0750 % with BNB on
      * BTCUSDC (and other USDC pairs)  → 0.0000 % (zero-fee maker promo)

    Source: https://www.binance.info/en/fee/schedule (snapshot 2026-04).
    """
    is_usdc = config.SYMBOL.endswith("USDC")
    return BinanceVenue(
        ctx=mainnet_context(symbol=config.SYMBOL),
        account_mode="live",
        ws_channel="live",
        fee_model="bnb_subsidized",
        fee_rate=0.0 if is_usdc else 0.00075,
    )


def binance_testnet_venue() -> BinanceVenue:
    """Testnet acts as Binance Paper per user's 'paper_mode=testnet' choice."""
    return BinanceVenue(
        ctx=testnet_context(symbol=config.SYMBOL),
        account_mode="paper",
        ws_channel="binance_demo",
        fee_model="bnb_subsidized",  # testnet behaves like BNB-on for accounting simplicity
        fee_rate=0.0,
    )
