"""
Revolut Live Runner — sandbox grid with real limit orders on Revolut X.

Uses BitcoinSandboxState for grid decisions and reconciles resting
levels against Revolut X open orders.  Rate-limit aware (1,000 order
requests / day on Revolut X).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from revolut_x import revx_request
from api.bitcoin_sandbox import (
    BitcoinSandboxState,
    BitcoinSandboxParams,
    sandbox_params_from_pydantic,
)
from api.strategy_params import BitcoinSandboxParamsModel, parse_strategy_params
from api import strategy_runtime, notifications
from api.ws_manager import WSManager

log = logging.getLogger(__name__)

STRATEGY_ID = "bitcoin_sandbox"

STRATEGY_META = {
    "bitcoin_sandbox": {
        "name": "BTC Grid (Revolut X)",
        "short": "RevX Grid",
        "description": (
            "Geofenced grid running on Revolut X production with post-only limit orders. "
            "Same logic as the paper sandbox, real money on Revolut."
        ),
        "color": "#0666eb",
        "icon": "R",
    },
}


# ── Revolut X exchange helpers ──────────────────────────────────────


def _revx_symbol() -> str:
    return os.getenv("REVOLUT_X_SYMBOL", "BTC-USD").strip()


def _revx_get_price(symbol: str) -> float:
    data = revx_request("GET", "/tickers", params={"symbols": symbol})
    tickers = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(tickers, list):
        for t in tickers:
            if t.get("symbol") == symbol:
                return float(t["last_price"])
        return float(tickers[0]["last_price"]) if tickers else 0.0
    return float(tickers.get("last_price", 0)) if isinstance(tickers, dict) else 0.0


def _revx_get_balances() -> Dict[str, float]:
    data = revx_request("GET", "/balances")
    items = data.get("data", data) if isinstance(data, dict) else data
    out: Dict[str, float] = {}
    if isinstance(items, list):
        for b in items:
            asset = b.get("currency", b.get("asset", ""))
            total = b.get("total")
            if total is not None:
                out[asset] = float(total)
            else:
                avail = float(b.get("available", b.get("free", 0)))
                reserved = float(b.get("reserved", b.get("locked", 0)))
                out[asset] = avail + reserved
    return out


def _revx_place_limit(
    side: str,
    price: float,
    base_size: float,
    symbol: str,
) -> str:
    """Place a post-only limit order, return the order ID string."""
    body = {
        "client_order_id": str(uuid.uuid4()),
        "symbol": symbol,
        "side": side.lower(),
        "order_configuration": {
            "limit": {
                "base_size": f"{base_size:.8f}",
                "price": f"{price:.2f}",
                "execution_instructions": ["post_only"],
            },
        },
    }
    resp = revx_request("POST", "/orders", json_body=body)
    order = resp.get("data", resp) if isinstance(resp, dict) else resp
    return str(order.get("venue_order_id", order.get("id", order.get("order_id", ""))))


def _revx_get_open_orders(symbol: str) -> List[dict]:
    try:
        data = revx_request("GET", "/orders/active", params={"symbols": symbol})
        items = data.get("data", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except Exception as exc:
        log.warning("revx open orders: %s", exc)
        return []


def _revx_cancel(order_id: str) -> None:
    try:
        revx_request("DELETE", f"/orders/{order_id}")
    except Exception as exc:
        log.warning("revx cancel %s: %s", order_id, exc)


def _revx_cancel_all(symbol: str) -> None:
    for o in _revx_get_open_orders(symbol):
        oid = o.get("id", o.get("order_id", ""))
        if oid:
            _revx_cancel(oid)


# ── Order tracker (same pattern as binance_demo_runner) ──────────────


TRADING_PAUSE_403_SEC = 60 * 60  # pause POST/DELETE /orders for 1h on 403/permission


class _OrderTracker:
    def __init__(self) -> None:
        self.buy_orders: Dict[str, float] = {}   # orderId → price
        self.sell_orders: Dict[str, int] = {}     # orderId → lot_id
        self.daily_count = 0
        self.day_start = time.time()
        self.write_paused_until: float = 0.0
        self.write_pause_reason: str = ""
        self._alerted_403: bool = False

    def _check_day(self) -> None:
        if time.time() - self.day_start > 86_400:
            self.daily_count = 0
            self.day_start = time.time()

    def can_place(self) -> bool:
        self._check_day()
        if time.time() < self.write_paused_until:
            return False
        return self.daily_count < 950  # keep 50 buffer under 1000 limit

    def placed(self) -> None:
        self._check_day()
        self.daily_count += 1

    def pause_writes(self, reason: str, seconds: float = TRADING_PAUSE_403_SEC) -> None:
        self.write_paused_until = max(self.write_paused_until, time.time() + seconds)
        self.write_pause_reason = reason

    def note_attempt(self) -> None:
        """Count every attempted exchange write toward the daily limit."""
        self._check_day()
        self.daily_count += 1


def _is_permission_error(exc: Exception) -> bool:
    msg = str(exc)
    return "403" in msg and "forbidden" in msg.lower()


def _reconcile_orders(
    sandbox: BitcoinSandboxState,
    tracker: _OrderTracker,
    symbol: str,
    events: list[str],
) -> None:
    open_orders = _revx_get_open_orders(symbol)
    open_ids = {o.get("id", o.get("order_id", "")) for o in open_orders}

    # Detect fills
    for oid in list(tracker.buy_orders):
        if oid not in open_ids:
            fill_price = tracker.buy_orders.pop(oid)
            sandbox._execute_buy(fill_price, events, "REVX_FILL")
            log.info("REVX BUY FILL detected oid=%s price=%.2f", oid, fill_price)

    for oid in list(tracker.sell_orders):
        if oid not in open_ids:
            lot_id = tracker.sell_orders.pop(oid)
            holding = next((h for h in sandbox.holdings if h.lot_id == lot_id), None)
            if holding:
                sandbox._execute_sell(holding, holding.sell_limit, events)
                log.info("REVX SELL FILL detected oid=%s lot=%d", oid, lot_id)

    # Desired resting levels
    desired_buys: dict[float, str] = {}
    if sandbox.trailing_buy_price is not None and sandbox.status == "ACTIVE":
        desired_buys[sandbox.trailing_buy_price] = "trail"
    if sandbox.grid_buy_price is not None and sandbox.status == "ACTIVE":
        desired_buys[sandbox.grid_buy_price] = "grid"

    desired_sells: dict[int, float] = {}
    for h in sandbox.holdings:
        desired_sells[h.lot_id] = h.sell_limit

    # Cancel stale
    for oid in list(tracker.buy_orders):
        price = tracker.buy_orders[oid]
        if price not in desired_buys:
            _revx_cancel(oid)
            tracker.buy_orders.pop(oid, None)
            tracker.note_attempt()

    for oid in list(tracker.sell_orders):
        lot_id = tracker.sell_orders[oid]
        if lot_id not in desired_sells:
            _revx_cancel(oid)
            tracker.sell_orders.pop(oid, None)
            tracker.note_attempt()

    if not tracker.can_place():
        if tracker.write_paused_until > time.time():
            remaining = int(tracker.write_paused_until - time.time())
            log.warning(
                "REVX writes paused (%s) — %ds remaining; skipping new placements",
                tracker.write_pause_reason, remaining,
            )
        else:
            log.warning("REVX daily order limit approaching — skipping new placements")
        return

    def _handle_place_failure(exc: Exception, tag: str) -> bool:
        """Log + count; return True when caller should abort this reconcile pass."""
        tracker.note_attempt()
        if _is_permission_error(exc):
            tracker.pause_writes("403 forbidden (API key lacks Spot trade scope)")
            if not tracker._alerted_403:
                tracker._alerted_403 = True
                log.error(
                    "Revolut X POST /orders returned 403 — API key is missing the "
                    "'Spot trade' permission. Pausing order placement for %d minutes. "
                    "Fix: Revolut X app → API keys → edit key → enable 'Spot trade' → save.",
                    TRADING_PAUSE_403_SEC // 60,
                )
                try:
                    notifications.send(
                        "⚠️ Revolut X: API key missing 'Spot trade' permission — "
                        "order placement paused. Enable it in the Revolut X app."
                    )
                except Exception:
                    pass
            return True
        log.warning("revx place %s failed: %s", tag, exc)
        return False

    # Place missing buys
    for bp, tag in desired_buys.items():
        if bp in tracker.buy_orders.values():
            continue
        tranche = sandbox.tranche_usdt
        qty = tranche / bp
        try:
            oid = _revx_place_limit("buy", bp, qty, symbol)
            tracker.buy_orders[oid] = bp
            tracker.placed()
            log.info("PLACE revx BUY [%s] oid=%s qty=%.8f @ %.2f", tag, oid, qty, bp)
        except Exception as exc:
            if _handle_place_failure(exc, f"buy [{tag}]"):
                return

    # Place missing sells
    for lot_id, sell_price in desired_sells.items():
        if lot_id in tracker.sell_orders.values():
            continue
        holding = next((h for h in sandbox.holdings if h.lot_id == lot_id), None)
        if not holding:
            continue
        try:
            oid = _revx_place_limit("sell", sell_price, holding.qty, symbol)
            tracker.sell_orders[oid] = lot_id
            tracker.placed()
            log.info("PLACE revx SELL lot=%d oid=%s qty=%.8f @ %.2f", lot_id, oid, holding.qty, sell_price)
        except Exception as exc:
            if _handle_place_failure(exc, f"sell lot={lot_id}"):
                return


def _serialize(
    sandbox: BitcoinSandboxState,
    pair: str,
    price: float,
) -> dict:
    """Build V2BotState-compatible snapshot."""
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
            "exit_reason": "REVX_TP",
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

    return {
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
                f"Revolut X {sandbox.status}: {len(sandbox.holdings)} lot(s), "
                f"equity ${eq:,.2f} ({pnl_pct:+.2f}% vs start)."
            ),
            "layer_summary": (
                f"{len(sb.get('layers_preview', []))} resting / holding rows"
                if sb.get("layers_preview") else "No open orders"
            ),
            "layers": [],
        },
    }


# ── Main loop ───────────────────────────────────────────────────────


async def run_revolut_live(ws_manager: WSManager) -> None:
    """Async loop: sandbox grid with real Revolut X limit orders."""
    if not config.REVOLUT_LIVE_ENABLED:
        log.info("Revolut Live runner DISABLED — set REVOLUT_LIVE_ENABLED=true in .env")
        return

    api_key = os.getenv("REVOLUT_X_API_KEY", "").strip()
    if not api_key:
        log.warning("Revolut Live runner DISABLED — REVOLUT_X_API_KEY is empty")
        return

    symbol = _revx_symbol()
    log.info("Revolut Live runner starting symbol=%s", symbol)

    try:
        balances = _revx_get_balances()
    except Exception as exc:
        log.error("Revolut runner cannot fetch balances: %s", exc)
        return

    starting_usdt = balances.get("USD", 0.0)
    if starting_usdt < 10:
        log.warning("Revolut USD balance too low (%.2f)", starting_usdt)
        starting_usdt = max(starting_usdt, 100.0)

    # Mirror Binance live grid: same algorithm (BitcoinSandboxState), same knobs
    # (GRID_* env vars consumed by LiveGridState in api/live_grid_runner.py).
    # Runtime overrides set via the dashboard/chat (strategy_runtime) still win
    # if the user has tuned them.
    base_bp = BitcoinSandboxParams(
        geofence_low=config.GRID_GEOFENCE_LOW,
        geofence_high=config.GRID_GEOFENCE_HIGH,
        reserve_usdt=config.GRID_RESERVE_USDT,
        num_bullets=config.GRID_NUM_BULLETS,
        tp_pct=config.GRID_TP_PCT,
        dip_pct=config.GRID_DIP_PCT,
    )
    override_dict = await strategy_runtime.get_effective_params_dict(STRATEGY_ID)
    default_dict = BitcoinSandboxParamsModel().model_dump()
    changed = {k: v for k, v in override_dict.items() if default_dict.get(k) != v}
    if changed:
        log.info("Revolut: applying strategy_runtime overrides %s on top of GRID_* config", changed)
        merged = {**base_bp.__dict__, **changed}
        pr_model = parse_strategy_params(STRATEGY_ID, merged)
        bp = sandbox_params_from_pydantic(pr_model)
    else:
        bp = base_bp

    sandbox = BitcoinSandboxState(starting_usdt=starting_usdt, params=bp, notify=None)
    tracker = _OrderTracker()

    _revx_cancel_all(symbol)

    start_time = time.time()
    prev_price = 0.0
    tracker_prices: List[float] = []

    log.info(
        "Revolut runner entering loop — capital=%.2f geofence=%.0f–%.0f poll=%ds",
        starting_usdt, bp.geofence_low, bp.geofence_high, config.REVOLUT_POLL_INTERVAL,
    )

    try:
        while True:
            try:
                price = _revx_get_price(symbol)
                if price <= 0:
                    await asyncio.sleep(config.REVOLUT_POLL_INTERVAL)
                    continue

                tracker_prices.append(price)
                if len(tracker_prices) > 80:
                    tracker_prices.pop(0)

                events: list[str] = []
                pe = prev_price if prev_price > 0 else price
                sandbox.update_drawdown(price)
                sandbox.tick_live(pe, price, events)
                prev_price = price

                _reconcile_orders(sandbox, tracker, symbol, events)

                for line in events:
                    log.info("REVX [%s] %s", STRATEGY_ID, line)

                strat_state = _serialize(sandbox, symbol, price)

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
                    "symbol": symbol,
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

                await ws_manager.broadcast(snapshot, channel="revolut_live")

            except Exception as exc:
                log.error("Revolut runner tick error: %s", exc, exc_info=True)

            await asyncio.sleep(config.REVOLUT_POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Revolut Live runner cancelled — cleaning up orders")
        _revx_cancel_all(symbol)
