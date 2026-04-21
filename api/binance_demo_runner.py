"""
Binance Demo Runner — sandbox grid with real LIMIT_MAKER orders on testnet.

Uses the same BitcoinSandboxState for grid decisions but reconciles
resting levels against actual testnet orders each tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import market_data
import trading
from api.exchange_context import testnet_context, BinanceContext
from api.bitcoin_sandbox import (
    BitcoinSandboxState,
    BitcoinSandboxParams,
    SandboxHolding,
    sandbox_params_from_pydantic,
)
from api.strategy_params import parse_strategy_params
from api import strategy_runtime, notifications
from api.ws_manager import WSManager

log = logging.getLogger(__name__)

STRATEGY_ID = "bitcoin_sandbox"

STRATEGY_META = {
    "bitcoin_sandbox": {
        "name": "BTC Sandbox Grid (Testnet)",
        "short": "Testnet Grid",
        "description": (
            "Geofenced grid running on the Binance testnet with real LIMIT_MAKER orders. "
            "Same logic as the paper sandbox but orders hit the testnet order book."
        ),
        "color": "#f59e0b",
        "icon": "🧪",
    },
}


class _OrderTracker:
    """Tracks which exchange order IDs map to which sandbox intent."""

    def __init__(self) -> None:
        self.buy_orders: Dict[int, float] = {}   # orderId → price
        self.sell_orders: Dict[int, int] = {}     # orderId → lot_id

    def clear(self) -> None:
        self.buy_orders.clear()
        self.sell_orders.clear()


def _get_precision(ctx: BinanceContext) -> tuple[int, int, float]:
    info = market_data.get_exchange_info(symbol=ctx.symbol, ctx=ctx)
    price_prec = 2
    qty_prec = 5
    min_notional = 5.0
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


def _get_testnet_usdt(ctx: BinanceContext) -> float:
    acct = trading.get_account(ctx=ctx)
    for b in acct.get("balances", []):
        if b["asset"] == "USDT":
            return float(b["free"]) + float(b["locked"])
    return 0.0


def _cancel_all_orders(ctx: BinanceContext) -> None:
    try:
        orders = trading.get_open_orders(symbol=ctx.symbol, ctx=ctx)
        for o in orders:
            trading.cancel_order(o["orderId"], symbol=ctx.symbol, ctx=ctx)
    except Exception as exc:
        log.warning("cancel_all_orders: %s", exc)


def _reconcile_orders(
    sandbox: BitcoinSandboxState,
    tracker: _OrderTracker,
    ctx: BinanceContext,
    price_prec: int,
    qty_prec: int,
    events: list[str],
) -> None:
    """Sync resting orders on the exchange with sandbox intent."""
    try:
        open_orders = trading.get_open_orders(symbol=ctx.symbol, ctx=ctx)
    except Exception as exc:
        log.warning("reconcile: cannot fetch open orders: %s", exc)
        return

    open_ids = {int(o["orderId"]) for o in open_orders}
    open_by_id = {int(o["orderId"]): o for o in open_orders}

    # --- Detect fills (tracked order disappeared from exchange) ---------
    for oid in list(tracker.buy_orders):
        if oid not in open_ids:
            fill_price = tracker.buy_orders.pop(oid)
            o_data = open_by_id.get(oid)
            if o_data and o_data.get("status") == "CANCELED":
                continue
            sandbox._execute_buy(fill_price, events, "TESTNET_FILL")
            log.info("TESTNET BUY FILL detected oid=%d price=%.2f", oid, fill_price)

    for oid in list(tracker.sell_orders):
        if oid not in open_ids:
            lot_id = tracker.sell_orders.pop(oid)
            holding = next((h for h in sandbox.holdings if h.lot_id == lot_id), None)
            if holding:
                sandbox._execute_sell(holding, holding.sell_limit, events)
                log.info("TESTNET SELL FILL detected oid=%d lot=%d", oid, lot_id)

    # --- Desired resting levels from sandbox ----------------------------
    desired_buys: dict[float, str] = {}
    if sandbox.trailing_buy_price is not None and sandbox.status == "ACTIVE":
        desired_buys[sandbox.trailing_buy_price] = "trail"
    if sandbox.grid_buy_price is not None and sandbox.status == "ACTIVE":
        desired_buys[sandbox.grid_buy_price] = "grid"

    desired_sells: dict[int, float] = {}
    for h in sandbox.holdings:
        desired_sells[h.lot_id] = h.sell_limit

    # --- Cancel stale exchange orders that no longer match ---------------
    tracked_buy_prices = set(tracker.buy_orders.values())
    for oid in list(tracker.buy_orders):
        price = tracker.buy_orders[oid]
        if price not in desired_buys:
            try:
                trading.cancel_order(oid, symbol=ctx.symbol, ctx=ctx)
                log.info("CANCEL stale buy oid=%d price=%.2f", oid, price)
            except Exception:
                pass
            tracker.buy_orders.pop(oid, None)

    tracked_sell_lots = set(tracker.sell_orders.values())
    for oid in list(tracker.sell_orders):
        lot_id = tracker.sell_orders[oid]
        if lot_id not in desired_sells:
            try:
                trading.cancel_order(oid, symbol=ctx.symbol, ctx=ctx)
                log.info("CANCEL stale sell oid=%d lot=%d", oid, lot_id)
            except Exception:
                pass
            tracker.sell_orders.pop(oid, None)

    # --- Place missing buy orders ---------------------------------------
    for bp, tag in desired_buys.items():
        if bp in tracker.buy_orders.values():
            continue
        tranche = sandbox.tranche_usdt
        qty = tranche / bp
        qty_str = f"{qty:.{qty_prec}f}"
        price_str = f"{bp:.{price_prec}f}"
        try:
            resp = trading.place_maker_order(
                side="BUY", quantity=qty_str, price=price_str,
                symbol=ctx.symbol, ctx=ctx,
            )
            oid = resp["orderId"]
            tracker.buy_orders[oid] = bp
            log.info("PLACE testnet BUY [%s] oid=%d qty=%s @ %s", tag, oid, qty_str, price_str)
        except Exception as exc:
            log.warning("place buy [%s] failed: %s", tag, exc)

    # --- Place missing sell orders --------------------------------------
    for lot_id, sell_price in desired_sells.items():
        if lot_id in tracker.sell_orders.values():
            continue
        holding = next((h for h in sandbox.holdings if h.lot_id == lot_id), None)
        if not holding:
            continue
        qty_str = f"{holding.qty:.{qty_prec}f}"
        price_str = f"{sell_price:.{price_prec}f}"
        try:
            resp = trading.place_maker_order(
                side="SELL", quantity=qty_str, price=price_str,
                symbol=ctx.symbol, ctx=ctx,
            )
            oid = resp["orderId"]
            tracker.sell_orders[oid] = lot_id
            log.info("PLACE testnet SELL lot=%d oid=%d qty=%s @ %s", lot_id, oid, qty_str, price_str)
        except Exception as exc:
            log.warning("place sell lot=%d failed: %s", lot_id, exc)


def _serialize(
    sandbox: BitcoinSandboxState,
    pair: str,
    price: float,
) -> dict:
    """Build V2BotState-compatible snapshot for the frontend."""
    analysis = sandbox.to_analysis_dict(price)
    meta = STRATEGY_META[STRATEGY_ID]
    eq = sandbox.equity(price)
    pnl = eq - sandbox.starting_capital
    pnl_pct = (pnl / sandbox.starting_capital * 100) if sandbox.starting_capital > 0 else 0

    position = None
    status = "PAUSED" if sandbox.status == "PAUSED" else "WATCHING"
    if sandbox.holdings:
        status = "HOLDING"
        qty_sum = sum(h.qty for h in sandbox.holdings)
        w_entry = sum(h.qty * h.entry_price for h in sandbox.holdings) / qty_sum if qty_sum else 0
        ur_pct = (price - w_entry) / w_entry * 100 if w_entry else 0
        ur_usdt = qty_sum * (price - w_entry)
        position = {
            "entry_price": round(w_entry, 2),
            "entry_time": "",
            "qty": round(qty_sum, 8),
            "usdt": round(qty_sum * w_entry, 4),
            "unrealized_pct": round(ur_pct, 4),
            "unrealized_usdt": round(ur_usdt, 6),
            "hold_minutes": 0,
            "open_lots": len(sandbox.holdings),
        }

    closed = sandbox.closed_trades
    pnls_list = [t.pnl_usdt for t in closed]
    winners = [p for p in pnls_list if p > 0]
    performance = {
        "total_trades": len(closed),
        "win_rate": round(len(winners) / len(pnls_list) * 100, 1) if pnls_list else 0,
        "total_pnl": round(sum(pnls_list), 4) if pnls_list else 0,
        "best_trade": round(max(pnls_list), 4) if pnls_list else 0,
        "worst_trade": round(min(pnls_list), 4) if pnls_list else 0,
        "avg_hold_time_min": 0,
    }

    trade_history: list[dict] = []
    for t in closed[-30:]:
        trade_history.append({
            "entry_time": t.entry_time_iso,
            "exit_time": t.exit_time_iso,
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "qty": round(t.qty, 8),
            "pnl": round(t.pnl_usdt, 6),
            "pnl_pct": round(t.pnl_pct, 4),
            "net_profit_usdt": round(t.net_profit_usdt, 6),
            "exit_reason": "TESTNET_TP",
            "lot_id": t.lot_id,
            "buy_fee_usdt": round(t.buy_fee_usdt, 6),
            "sell_fee_usdt": round(t.sell_fee_usdt, 6),
            "total_fees_usdt": round(t.total_fees_usdt, 6),
            "fee_pct_of_turnover": round(t.fee_pct_of_turnover, 4),
            "maker_fee_leg_pct": round(t.maker_fee_leg_pct, 4),
            "notional_entry_usdt": round(t.notional_entry_usdt, 4),
            "gross_exit_usdt": round(t.gross_exit_usdt, 4),
            "hold_seconds": round(t.hold_seconds, 1),
        })

    ind = analysis.get("indicators", {})
    sb = ind.get("sandbox", {})
    strat_state = {
        "id": STRATEGY_ID,
        "name": meta["name"],
        "short": meta["short"],
        "pair": pair,
        "color": meta["color"],
        "icon": meta["icon"],
        "status": status,
        "wallet": {
            "starting": round(sandbox.starting_capital, 2),
            "equity": round(eq, 4),
            "usdt": round(sandbox.usdt, 4),
            "btc": round(sandbox.btc, 8),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
        },
        "position": position,
        "last_signal": {
            "action": analysis.get("action", "WAIT"),
            "reasons": analysis.get("reasons", []),
        },
        "indicators": ind,
        "tp_price": None,
        "sl_price": None,
        "tp_type": "sandbox_grid",
        "trade_history": trade_history,
        "performance": performance,
        "explanation": {
            "strategy_summary": meta["description"],
            "current_state": (
                f"Testnet {sandbox.status}: {len(sandbox.holdings)} lot(s), "
                f"equity ${eq:,.2f} ({pnl_pct:+.2f}% vs start)."
            ),
            "layer_summary": (
                f"{len(sb.get('layers_preview', []))} resting / holding rows"
                if sb.get("layers_preview") else "No open orders"
            ),
            "layers": [],
        },
    }

    return strat_state


# ── Main loop ───────────────────────────────────────────────────────


async def run_binance_demo(ws_manager: WSManager) -> None:
    """Async loop: sandbox grid with real testnet orders."""
    ctx = testnet_context()
    if not ctx.api_key or not ctx.api_secret:
        log.warning(
            "Binance Demo runner DISABLED — "
            "set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET in .env"
        )
        return

    log.info("Binance Demo runner starting (testnet) symbol=%s", ctx.symbol)

    try:
        price_prec, qty_prec, min_notional = _get_precision(ctx)
    except Exception as exc:
        log.error("Demo runner cannot fetch exchange info: %s", exc)
        return

    starting_usdt = _get_testnet_usdt(ctx)
    if starting_usdt < 10:
        log.warning("Testnet USDT balance too low (%.2f) — demo runner idle", starting_usdt)
        starting_usdt = max(starting_usdt, 5000.0)

    raw_params = await strategy_runtime.get_effective_params_dict(STRATEGY_ID)
    pr_model = parse_strategy_params(STRATEGY_ID, raw_params)
    bp = sandbox_params_from_pydantic(pr_model)

    sandbox = BitcoinSandboxState(starting_usdt=starting_usdt, params=bp, notify=None)
    tracker = _OrderTracker()

    _cancel_all_orders(ctx)

    pair = ctx.symbol
    start_time = time.time()
    prev_price = 0.0
    tracker_prices: List[float] = []

    log.info(
        "Demo runner entering loop — capital=%.2f geofence=%.0f–%.0f",
        starting_usdt, bp.geofence_low, bp.geofence_high,
    )

    try:
        while True:
            try:
                price_data = market_data.get_price(symbol=pair, ctx=ctx)
                price = float(price_data["price"])
                tracker_prices.append(price)
                if len(tracker_prices) > 80:
                    tracker_prices.pop(0)

                events: list[str] = []
                pe = prev_price if prev_price > 0 else price
                sandbox.update_drawdown(price)
                sandbox.tick_live(pe, price, events)
                prev_price = price

                _reconcile_orders(sandbox, tracker, ctx, price_prec, qty_prec, events)

                for line in events:
                    log.info("DEMO [%s] %s", STRATEGY_ID, line)

                strat_state = _serialize(sandbox, pair, price)

                trade_markers: List[Dict[str, Any]] = []
                for h in sandbox.holdings:
                    trade_markers.append({
                        "time": int(h.entry_ts),
                        "position": "belowBar",
                        "color": "#22c55e",
                        "shape": "arrowUp",
                        "text": f"Buy #{h.lot_id}",
                        "price": round(h.entry_price, 2),
                        "tp_price": round(h.sell_limit, 2),
                        "side": "buy",
                        "active": True,
                    })
                for t in sandbox.closed_trades[-40:]:
                    trade_markers.append({
                        "time": int(datetime.strptime(t.entry_time_iso, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()),
                        "position": "belowBar",
                        "color": "#22c55e",
                        "shape": "arrowUp",
                        "text": f"Buy #{t.lot_id}",
                        "price": round(t.entry_price, 2),
                        "side": "buy",
                        "active": False,
                    })
                    trade_markers.append({
                        "time": int(datetime.strptime(t.exit_time_iso, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()),
                        "position": "aboveBar",
                        "color": "#ef4444",
                        "shape": "arrowDown",
                        "text": f"Sell #{t.lot_id} {'+' if t.net_profit_usdt >= 0 else ''}{t.net_profit_usdt:.2f}",
                        "price": round(t.exit_price, 2),
                        "side": "sell",
                        "active": False,
                    })

                snapshot = {
                    "timestamp": time.time(),
                    "uptime_s": int(time.time() - start_time),
                    "symbol": pair,
                    "price": round(price, 2),
                    "prices": [round(p, 2) for p in tracker_prices],
                    "trade_markers": trade_markers,
                    "strategies": [strat_state],
                    "global_summary": {
                        "total_strategies": 1,
                        "active_positions": 1 if sandbox.holdings else 0,
                        "combined_equity": round(sandbox.equity(price), 4),
                        "combined_pnl": round(sandbox.equity(price) - sandbox.starting_capital, 4),
                        "combined_pnl_pct": round(
                            (sandbox.equity(price) - sandbox.starting_capital) /
                            sandbox.starting_capital * 100, 4
                        ) if sandbox.starting_capital > 0 else 0,
                        "starting_capital": round(sandbox.starting_capital, 2),
                    },
                    "glossary": {},
                    "strategy_params": {},
                    "strategy_params_meta": {},
                }

                await ws_manager.broadcast(snapshot, channel="binance_demo")

            except Exception as exc:
                log.error("Demo runner tick error: %s", exc, exc_info=True)

            await asyncio.sleep(config.POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Binance Demo runner cancelled — cleaning up testnet orders")
        _cancel_all_orders(ctx)
