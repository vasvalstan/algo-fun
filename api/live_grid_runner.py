"""
Live Grid Runner — Sandbox Grid strategy on real Binance.

Trails the local high with a limit buy at -dip_pct%, brackets each fill
with a TP sell at +tp_pct%, and places the next grid buy below the entry.
Geofenced to a configurable price band. On restart, recovers state from
open Binance orders and balances.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

import config
import market_data
from api import notifications, log_buffer
from api.ws_manager import WSManager

log = logging.getLogger(__name__)

MAKER_FEE = 0.001


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class GridHolding:
    lot_id: int
    qty: float
    entry_price: float
    tp_price: float
    sell_order_id: Optional[int] = None
    cost_usdt: float = 0.0
    buy_fee: float = 0.0
    entry_ts: float = 0.0


@dataclass
class GridClosedTrade:
    lot_id: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_usdt: float
    pnl_pct: float
    buy_fee: float
    sell_fee: float
    entry_ts: float
    exit_ts: float


# ── Precision helpers ────────────────────────────────────────────────

def _get_precision():
    info = market_data.get_exchange_info()
    price_prec, qty_prec, min_notional = 2, 5, 5.0
    for f in info.get("filters", []):
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


def _floor_qty(amount: float, qty_prec: int) -> float:
    if qty_prec <= 0:
        return float(int(amount))
    step = 10.0 ** (-qty_prec)
    return int(amount / step + 1e-12) * step


def _get_balances():
    from trading import get_account
    base_asset = config.SYMBOL.replace("USDT", "")
    result = {}
    acct = get_account()
    for b in acct.get("balances", []):
        if b["asset"] in (base_asset, "USDT"):
            result[b["asset"]] = {
                "free": float(b["free"]),
                "locked": float(b["locked"]),
            }
    return result


# ── Live Grid State ──────────────────────────────────────────────────

class LiveGridState:
    """Manages the grid strategy state and Binance order lifecycle."""

    def __init__(self, price_prec: int, qty_prec: int, min_notional: float,
                 starting_usdt: float):
        self.price_prec = price_prec
        self.qty_prec = qty_prec
        self.min_notional = min_notional
        self.starting_usdt = starting_usdt

        self.status: str = "ACTIVE"
        self.local_high: float = 0.0
        self.current_price: float = 0.0

        self.resting_buy: Optional[dict] = None
        self.holdings: list[GridHolding] = []
        self.closed_trades: list[GridClosedTrade] = []
        self.event_log: list[str] = []
        self.errors: list[str] = []

        self._lot_seq = 0
        self._last_reprice_high: float = 0.0

    # ── Config accessors ─────────────────────────────────────────────

    @property
    def geofence_low(self) -> float:
        return config.GRID_GEOFENCE_LOW

    @property
    def geofence_high(self) -> float:
        return config.GRID_GEOFENCE_HIGH

    @property
    def tp_mult(self) -> float:
        return 1.0 + config.GRID_TP_PCT / 100.0

    @property
    def dip_mult(self) -> float:
        return 1.0 - config.GRID_DIP_PCT / 100.0

    @property
    def max_holdings(self) -> int:
        return max(1, config.GRID_NUM_BULLETS)

    @property
    def tranche_usdt(self) -> float:
        tradable = max(0.0, self.starting_usdt - config.GRID_RESERVE_USDT)
        return max(6.0, tradable / max(config.GRID_NUM_BULLETS, 1))

    def _in_geofence(self, price: float) -> bool:
        return self.geofence_low <= price <= self.geofence_high

    def _log_event(self, msg: str):
        log.info("GRID: %s", msg)
        self.event_log.append(msg)
        if len(self.event_log) > 200:
            self.event_log = self.event_log[-100:]

    # ── Order placement ──────────────────────────────────────────────

    def _place_buy(self, price: float, kind: str) -> Optional[dict]:
        """Place a limit buy on Binance. Returns {order_id, price, kind} or None."""
        from trading import place_limit_order

        buy_price = round(price, self.price_prec)
        size_usdt = self.tranche_usdt
        qty = _floor_qty(size_usdt / buy_price, self.qty_prec)
        notional = qty * buy_price

        if notional < self.min_notional:
            self._log_event(f"Skip buy: notional ${notional:.2f} < ${self.min_notional:.2f}")
            return None

        price_str = f"{buy_price:.{self.price_prec}f}"
        qty_str = f"{qty:.{self.qty_prec}f}"

        try:
            resp = place_limit_order(side="BUY", quantity=qty_str, price=price_str)
            order_id = resp["orderId"]
            self._log_event(
                f"BUY [{kind}] placed: #{order_id} {qty_str} @ ${price_str} "
                f"(${size_usdt:.2f})"
            )
            return {"order_id": order_id, "price": buy_price, "qty": qty, "kind": kind}
        except Exception as exc:
            err = f"buy placement failed: {exc}"
            self._log_event(err)
            self.errors.append(err)
            return None

    def _place_sell(self, holding: GridHolding) -> Optional[int]:
        """Place a TP sell for a holding. Returns order_id or None."""
        from trading import place_limit_order

        sell_price = round(holding.tp_price, self.price_prec)
        qty = _floor_qty(holding.qty, self.qty_prec)
        notional = qty * sell_price

        if notional < self.min_notional or qty <= 0:
            self._log_event(f"Skip sell lot #{holding.lot_id}: notional ${notional:.2f}")
            return None

        price_str = f"{sell_price:.{self.price_prec}f}"
        qty_str = f"{qty:.{self.qty_prec}f}"

        try:
            resp = place_limit_order(side="SELL", quantity=qty_str, price=price_str)
            order_id = resp["orderId"]
            self._log_event(
                f"SELL TP placed: #{order_id} lot #{holding.lot_id} "
                f"{qty_str} @ ${price_str}"
            )
            return order_id
        except Exception as exc:
            err = f"sell placement lot #{holding.lot_id} failed: {exc}"
            self._log_event(err)
            self.errors.append(err)
            return None

    def _cancel_order(self, order_id: int) -> bool:
        from trading import cancel_order
        try:
            cancel_order(order_id)
            return True
        except Exception as exc:
            log.debug("cancel #%s failed: %s", order_id, exc)
            return False

    # ── Core tick logic ──────────────────────────────────────────────

    def tick(self, price: float, open_order_ids: set) -> list[str]:
        """Run one grid tick. Returns list of event strings."""
        self.current_price = price
        events: list[str] = []

        # Geofence
        if not self._in_geofence(price):
            if self.status == "ACTIVE":
                self.status = "PAUSED"
                events.append(f"GEOFENCE BREACH ${price:,.2f} — pausing new buys")
                if self.resting_buy:
                    self._cancel_order(self.resting_buy["order_id"])
                    events.append(f"Cancelled resting buy #{self.resting_buy['order_id']}")
                    self.resting_buy = None
            return events

        if self.status == "PAUSED":
            self.status = "ACTIVE"
            self.local_high = price
            self._last_reprice_high = price
            events.append(f"GEOFENCE OK — resuming at ${price:,.2f}")

        # Detect buy fill
        if self.resting_buy and self.resting_buy["order_id"] not in open_order_ids:
            fill = self.resting_buy
            self._lot_seq += 1
            fee = fill["qty"] * fill["price"] * MAKER_FEE
            h = GridHolding(
                lot_id=self._lot_seq,
                qty=fill["qty"],
                entry_price=fill["price"],
                tp_price=round(fill["price"] * self.tp_mult, self.price_prec),
                cost_usdt=fill["qty"] * fill["price"],
                buy_fee=fee,
                entry_ts=time.time(),
            )
            self.holdings.append(h)
            self.resting_buy = None
            events.append(
                f"BUY FILLED lot #{h.lot_id}: {h.qty:.6f} @ ${h.entry_price:,.2f} "
                f"→ TP ${h.tp_price:,.2f}"
            )
            self._log_event(events[-1])

            sell_oid = self._place_sell(h)
            if sell_oid:
                h.sell_order_id = sell_oid

            grid_price = round(fill["price"] * self.dip_mult, self.price_prec)
            if len(self.holdings) < self.max_holdings:
                result = self._place_buy(grid_price, "GRID")
                if result:
                    self.resting_buy = result

            notifications.send(
                f"📥 <b>Grid BUY filled</b> (lot #{h.lot_id})\n"
                f"Entry: <code>${h.entry_price:,.2f}</code>\n"
                f"TP target: <code>${h.tp_price:,.2f}</code>\n"
                f"Qty: <code>{h.qty:.6f}</code>\n"
                f"Open lots: {len(self.holdings)}"
            )

        # Detect sell fills
        for h in self.holdings[:]:
            if h.sell_order_id and h.sell_order_id not in open_order_ids:
                gross = h.qty * h.tp_price
                sell_fee = gross * MAKER_FEE
                net = gross - sell_fee
                pnl = net - h.cost_usdt - h.buy_fee
                pnl_pct = (h.tp_price - h.entry_price) / h.entry_price * 100

                ct = GridClosedTrade(
                    lot_id=h.lot_id,
                    entry_price=h.entry_price,
                    exit_price=h.tp_price,
                    qty=h.qty,
                    pnl_usdt=pnl,
                    pnl_pct=pnl_pct,
                    buy_fee=h.buy_fee,
                    sell_fee=sell_fee,
                    entry_ts=h.entry_ts,
                    exit_ts=time.time(),
                )
                self.closed_trades.append(ct)
                self.holdings.remove(h)

                events.append(
                    f"SELL FILLED lot #{h.lot_id}: ${h.tp_price:,.2f} "
                    f"PnL ${pnl:.4f} ({pnl_pct:+.2f}%)"
                )
                self._log_event(events[-1])

                notifications.send(
                    f"💰 <b>Grid SELL filled</b> (lot #{h.lot_id})\n"
                    f"Exit: <code>${h.tp_price:,.2f}</code>\n"
                    f"Entry: <code>${h.entry_price:,.2f}</code>\n"
                    f"P&L: <code>{'+' if pnl >= 0 else ''}{pnl:.4f} USDT ({pnl_pct:+.2f}%)</code>\n"
                    f"Remaining lots: {len(self.holdings)}"
                )

                if not self.holdings and not self.resting_buy:
                    self.local_high = h.tp_price
                    self._last_reprice_high = self.local_high
                    result = self._place_buy(
                        round(self.local_high * self.dip_mult, self.price_prec),
                        "TRAIL"
                    )
                    if result:
                        self.resting_buy = result
                        events.append("ALL FLAT — trailing from exit price")
                elif not self.resting_buy and len(self.holdings) < self.max_holdings:
                    grid_price = round(h.tp_price * self.dip_mult, self.price_prec)
                    result = self._place_buy(grid_price, "GRID")
                    if result:
                        self.resting_buy = result

        # Ensure sell limits for holdings missing them or cancelled externally
        for h in self.holdings:
            needs_sell = (
                h.sell_order_id is None
                or h.sell_order_id not in open_order_ids
            )
            if needs_sell:
                sell_oid = self._place_sell(h)
                if sell_oid:
                    h.sell_order_id = sell_oid

        # Trailing buy management (when flat)
        if not self.holdings and self.status == "ACTIVE":
            if self.local_high <= 0:
                self.local_high = price
                self._last_reprice_high = price

            if price > self.local_high:
                self.local_high = price

            move_pct = abs(self.local_high - self._last_reprice_high) / self._last_reprice_high * 100 if self._last_reprice_high > 0 else 999
            target_buy = round(self.local_high * self.dip_mult, self.price_prec)

            if self.resting_buy is None:
                result = self._place_buy(target_buy, "TRAIL")
                if result:
                    self.resting_buy = result
                    self._last_reprice_high = self.local_high
            elif move_pct >= config.GRID_REPRICE_THRESHOLD:
                self._cancel_order(self.resting_buy["order_id"])
                result = self._place_buy(target_buy, "TRAIL")
                if result:
                    old_price = self.resting_buy["price"]
                    self.resting_buy = result
                    self._last_reprice_high = self.local_high
                    events.append(
                        f"TRAIL REPRICE high ${self.local_high:,.2f} → "
                        f"buy ${target_buy:,.2f} (was ${old_price:,.2f})"
                    )

        return events

    # ── Recovery ─────────────────────────────────────────────────────

    def recover_from_exchange(self, open_orders: list, balances: dict, price: float):
        """Rebuild state from Binance open orders and balances on restart."""
        buy_orders = [o for o in open_orders if o["side"] == "BUY"]
        sell_orders = [o for o in open_orders if o["side"] == "SELL"]

        # Cancel duplicate buys, keep newest
        if len(buy_orders) > 1:
            buy_orders.sort(key=lambda o: o.get("time", 0), reverse=True)
            for dup in buy_orders[1:]:
                self._cancel_order(dup["orderId"])
                self._log_event(f"Recovery: cancelled duplicate buy #{dup['orderId']}")

        if buy_orders:
            b = buy_orders[0]
            self.resting_buy = {
                "order_id": b["orderId"],
                "price": float(b["price"]),
                "qty": float(b["origQty"]),
                "kind": "recovered",
            }
            self._log_event(
                f"Recovery: adopted buy #{b['orderId']} @ ${float(b['price']):,.2f}"
            )

        # Reconstruct holdings from sell orders
        for s in sell_orders:
            sell_price = float(s["price"])
            qty = float(s["origQty"])
            inferred_entry = round(sell_price / self.tp_mult, self.price_prec)

            self._lot_seq += 1
            h = GridHolding(
                lot_id=self._lot_seq,
                qty=qty,
                entry_price=inferred_entry,
                tp_price=sell_price,
                sell_order_id=s["orderId"],
                cost_usdt=qty * inferred_entry,
                entry_ts=s.get("time", time.time() * 1000) / 1000,
            )
            self.holdings.append(h)
            self._log_event(
                f"Recovery: lot #{h.lot_id} from sell #{s['orderId']} "
                f"entry≈${inferred_entry:,.2f} TP=${sell_price:,.2f}"
            )

        # Check for unmatched BTC (holdings exist on exchange but no sell order)
        base_asset = config.SYMBOL.replace("USDT", "")
        base_bal = balances.get(base_asset, {})
        free_btc = base_bal.get("free", 0.0)
        accounted_btc = sum(h.qty for h in self.holdings)
        unmatched = free_btc - accounted_btc

        if unmatched > 0:
            notional = unmatched * price
            if notional >= self.min_notional:
                self._lot_seq += 1
                tp_price = round(price * self.tp_mult, self.price_prec)
                floored_qty = _floor_qty(unmatched, self.qty_prec)
                h = GridHolding(
                    lot_id=self._lot_seq,
                    qty=floored_qty,
                    entry_price=price,
                    tp_price=tp_price,
                    cost_usdt=floored_qty * price,
                    entry_ts=time.time(),
                )
                self.holdings.append(h)
                sell_oid = self._place_sell(h)
                if sell_oid:
                    h.sell_order_id = sell_oid
                self._log_event(
                    f"Recovery: unmatched {floored_qty:.6f} BTC → lot #{h.lot_id} "
                    f"TP=${tp_price:,.2f}"
                )

        self.local_high = price
        self._last_reprice_high = price

        total = len(self.holdings)
        buys = 1 if self.resting_buy else 0
        self._log_event(
            f"Recovery complete: {total} lots, {buys} resting buy, "
            f"{len(self.closed_trades)} closed"
        )


# ── State serialization ─────────────────────────────────────────────

def serialize_grid_state(grid: LiveGridState, start_time: float) -> dict:
    now = time.time()
    uptime = int(now - start_time)

    positions = []
    for h in grid.holdings:
        unrealized_pct = (grid.current_price - h.entry_price) / h.entry_price * 100 if h.entry_price > 0 else 0
        unrealized_usdt = (grid.current_price - h.entry_price) * h.qty
        positions.append({
            "slot_id": h.lot_id,
            "state": "HOLDING",
            "entry_price": h.entry_price,
            "slot_qty": h.qty,
            "tp_price": h.tp_price,
            "sell_order_id": h.sell_order_id,
            "unrealized_pct": round(unrealized_pct, 3),
            "unrealized_usdt": round(unrealized_usdt, 6),
            "age_s": int(now - h.entry_ts) if h.entry_ts > 0 else 0,
        })

    if not positions:
        positions.append({
            "slot_id": 0,
            "state": "WATCHING",
            "entry_price": 0,
        })

    cycles = []
    for ct in grid.closed_trades[-50:]:
        gross_pct = (ct.exit_price - ct.entry_price) / ct.entry_price * 100 if ct.entry_price > 0 else 0
        cycles.append({
            "number": ct.lot_id,
            "slot_id": ct.lot_id,
            "buy_price": ct.entry_price,
            "sell_price": ct.exit_price,
            "gross_pct": round(gross_pct, 4),
            "net_pnl": round(ct.pnl_usdt, 6),
            "fee": round(ct.buy_fee + ct.sell_fee, 6),
            "timestamp": ct.exit_ts,
        })

    try:
        balances = _get_balances()
        base = config.SYMBOL.replace("USDT", "")
        u = balances.get("USDT", {"free": 0.0, "locked": 0.0})
        b = balances.get(base, {"free": 0.0, "locked": 0.0})
        equity = (u["free"] + u["locked"]) + (b["free"] + b["locked"]) * grid.current_price
    except Exception:
        equity = grid.starting_usdt

    total_pnl = sum(ct.pnl_usdt for ct in grid.closed_trades)
    total_fees = sum(ct.buy_fee + ct.sell_fee for ct in grid.closed_trades)

    resting_info = None
    if grid.resting_buy:
        dist_pct = (grid.current_price - grid.resting_buy["price"]) / grid.resting_buy["price"] * 100 if grid.resting_buy["price"] > 0 else 0
        resting_info = {
            "order_id": grid.resting_buy["order_id"],
            "price": grid.resting_buy["price"],
            "kind": grid.resting_buy["kind"],
            "distance_pct": round(dist_pct, 3),
        }

    return {
        "timestamp": now,
        "uptime_s": uptime,
        "symbol": config.SYMBOL,
        "mainnet": config.USE_MAINNET,
        "price": grid.current_price,
        "ma": 0,
        "take_profit_pct": config.GRID_TP_PCT,
        "stop_loss_pct": 0,
        "trade_size_usdt": grid.tranche_usdt,
        "prices": [],
        "positions": positions,
        "cycles": cycles,
        "grid_mode": True,
        "grid": {
            "status": grid.status,
            "local_high": round(grid.local_high, 2),
            "resting_buy": resting_info,
            "open_lots": len(grid.holdings),
            "max_lots": grid.max_holdings,
            "tranche_usdt": round(grid.tranche_usdt, 2),
            "geofence_low": grid.geofence_low,
            "geofence_high": grid.geofence_high,
            "tp_pct": config.GRID_TP_PCT,
            "dip_pct": config.GRID_DIP_PCT,
            "closed_count": len(grid.closed_trades),
            "total_pnl": round(total_pnl, 6),
        },
        "strategy": {
            "macro_regime": f"GRID {grid.status}",
            "daily_bias": f"${grid.geofence_low/1000:.0f}k–${grid.geofence_high/1000:.0f}k",
            "market_mode": f"{len(grid.holdings)}/{grid.max_holdings} lots",
            "action": grid.status,
            "reasons": grid.event_log[-5:],
        },
        "session": {
            "starting_balance": round(grid.starting_usdt, 4),
            "equity_usdt": round(equity, 4),
            "fees_paid": round(total_fees, 6),
        },
        "alltime": {
            "total_cycles": len(grid.closed_trades),
            "total_net_pnl": round(total_pnl, 6),
            "total_fees": round(total_fees, 6),
            "first_cycle_ts": grid.closed_trades[0].exit_ts if grid.closed_trades else 0,
        },
        "errors": grid.errors[-5:],
        "last_action": grid.event_log[-1] if grid.event_log else "starting...",
        "logs": log_buffer.recent(50, modules=log_buffer.LIVE_MODULES),
    }


# ── Main async runner ────────────────────────────────────────────────

async def run_grid_bot(ws_manager: WSManager) -> None:
    """Live grid bot loop — runs as an asyncio background task."""
    log.info(
        "Grid runner starting: %s geofence=$%s–$%s tp=%.2f%% dip=%.2f%% bullets=%d",
        config.SYMBOL, f"{config.GRID_GEOFENCE_LOW:,.0f}",
        f"{config.GRID_GEOFENCE_HIGH:,.0f}",
        config.GRID_TP_PCT, config.GRID_DIP_PCT, config.GRID_NUM_BULLETS,
    )

    price_prec, qty_prec, min_notional = _get_precision()
    log.info("Precision: price=%d qty=%d min_notional=%.2f", price_prec, qty_prec, min_notional)

    price_now = float(market_data.get_price()["price"])
    balances = _get_balances()
    base_asset = config.SYMBOL.replace("USDT", "")
    usdt_total = (
        balances.get("USDT", {}).get("free", 0.0)
        + balances.get("USDT", {}).get("locked", 0.0)
        + (balances.get(base_asset, {}).get("free", 0.0)
           + balances.get(base_asset, {}).get("locked", 0.0)) * price_now
    )
    log.info("Starting equity: $%.2f USDT", usdt_total)

    grid = LiveGridState(price_prec, qty_prec, min_notional, usdt_total)

    # Recovery
    from trading import get_open_orders
    try:
        existing = get_open_orders()
    except Exception as exc:
        existing = []
        log.warning("Could not fetch open orders: %s", exc)

    grid.recover_from_exchange(existing, balances, price_now)

    notifications.send(
        f"🤖 <b>Grid Bot started</b>\n"
        f"Pair: <code>{config.SYMBOL}</code>\n"
        f"Geofence: <code>${config.GRID_GEOFENCE_LOW:,.0f}–${config.GRID_GEOFENCE_HIGH:,.0f}</code>\n"
        f"TP: <code>{config.GRID_TP_PCT}%</code> | Dip: <code>{config.GRID_DIP_PCT}%</code>\n"
        f"Tranches: <code>{config.GRID_NUM_BULLETS}</code> × <code>${grid.tranche_usdt:.2f}</code>\n"
        f"Equity: <code>${usdt_total:.2f}</code>\n"
        f"Recovered: {len(grid.holdings)} lots, {'1 buy' if grid.resting_buy else '0 buys'}"
    )

    start_time = time.time()
    _status_tick = 0
    _STATUS_INTERVAL = 10
    _tg_status_tick = 0
    _TG_STATUS_INTERVAL = 300

    try:
        while True:
            try:
                price_data = market_data.get_price()
                price = float(price_data["price"])

                try:
                    open_orders = get_open_orders()
                    open_ids = {o["orderId"] for o in open_orders}
                except Exception:
                    open_ids = set()
                    if grid.resting_buy:
                        open_ids.add(grid.resting_buy["order_id"])
                    for h in grid.holdings:
                        if h.sell_order_id:
                            open_ids.add(h.sell_order_id)

                events = grid.tick(price, open_ids)
                for ev in events:
                    log.info(ev)

                _status_tick += 1
                if _status_tick >= _STATUS_INTERVAL:
                    _status_tick = 0
                    lots_str = ", ".join(
                        f"#{h.lot_id}@${h.entry_price:,.0f}" for h in grid.holdings
                    ) or "flat"
                    buy_str = (
                        f"buy@${grid.resting_buy['price']:,.2f}({grid.resting_buy['kind']})"
                        if grid.resting_buy else "none"
                    )
                    log.info(
                        "GRID | $%s | %s | lots=[%s] | resting=%s | closed=%d | pnl=$%.4f",
                        f"{price:,.2f}", grid.status, lots_str, buy_str,
                        len(grid.closed_trades),
                        sum(ct.pnl_usdt for ct in grid.closed_trades),
                    )

                _tg_status_tick += 1
                if _tg_status_tick >= _TG_STATUS_INTERVAL:
                    _tg_status_tick = 0
                    total_pnl = sum(ct.pnl_usdt for ct in grid.closed_trades)
                    notifications.send(
                        f"📡 <b>Grid Status</b>\n\n"
                        f"Price: <code>${price:,.2f}</code>\n"
                        f"Status: <code>{grid.status}</code>\n"
                        f"Open lots: <code>{len(grid.holdings)}/{grid.max_holdings}</code>\n"
                        f"Resting buy: <code>{buy_str}</code>\n"
                        f"Closed: <code>{len(grid.closed_trades)}</code>\n"
                        f"Total P&L: <code>${total_pnl:.4f}</code>"
                    )

                state = serialize_grid_state(grid, start_time)
                await ws_manager.broadcast(state, channel="live")

            except requests.HTTPError as exc:
                resp = exc.response
                body = resp.text if resp is not None else str(exc)
                msg = f"HTTP {resp.status_code if resp else '?'}: {body}"
                log.warning(msg)
                grid.errors.append(msg)
            except requests.ConnectionError:
                log.warning("connection error, retrying…")
            except Exception as exc:
                msg = f"unexpected: {exc}"
                log.error(msg, exc_info=True)
                grid.errors.append(msg)

            await asyncio.sleep(config.POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Grid runner cancelled")
        uptime = int(time.time() - start_time)
        notifications.notify_bot_stopped(uptime, len(grid.closed_trades))
        raise
