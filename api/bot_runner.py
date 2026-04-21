"""
Bot runner — wraps the existing bot.py trading loop as an async background task.

Instead of writing state.json to disk, it serializes the strategy state
in-process and pushes it to the WebSocket manager for live broadcast.
Also fires Telegram notifications on key events.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional, Tuple

import requests

import config
import dashboard
import market_data
import state_writer
from strategy import TrendAwareMakerStrategy, State

from api import notifications, log_buffer
from api.ws_manager import WSManager
from api.trade_manager import trade_manager

log = logging.getLogger(__name__)

# ── Helpers (copied from bot.py to avoid circular imports) ────────────


def _get_precision() -> Tuple[int, int, float]:
    """Fetch price/qty precision and min notional from exchange info."""
    info = market_data.get_exchange_info()
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


def _floor_qty(amount: float, qty_prec: int) -> float:
    if qty_prec <= 0:
        return float(int(amount))
    step = 10.0 ** (-qty_prec)
    return int(amount / step + 1e-12) * step


def _get_balances() -> dict:
    from trading import get_account

    base_asset = config.SYMBOL.replace("USDT", "")
    want = {base_asset, "USDT"}
    result = {}
    acct = get_account()
    for b in acct.get("balances", []):
        if b["asset"] in want:
            result[b["asset"]] = {
                "free": float(b["free"]),
                "locked": float(b["locked"]),
            }
    return result


def _portfolio_equity_usdt(spot_price: float) -> float:
    balances = _get_balances()
    base = config.SYMBOL.replace("USDT", "")
    u = balances.get("USDT", {"free": 0.0, "locked": 0.0})
    b = balances.get(base, {"free": 0.0, "locked": 0.0})
    return (u["free"] + u["locked"]) + (b["free"] + b["locked"]) * spot_price


# ── State serialization (replaces state_writer for WebSocket push) ────


def serialize_state(
    strat: TrendAwareMakerStrategy,
    start_time: float,
    last_action: Optional[str] = None,
) -> dict:
    """Build the state dict that gets sent to the frontend via WebSocket.

    Same schema as the old state_writer.write_state, so the frontend
    can use the same TypeScript interfaces.
    """
    now = time.time()
    uptime = int(now - start_time)

    positions = []
    for p in strat.positions:
        pos_data = {
            "slot_id": p.slot_id,
            "state": p.state.name,
            "entry_price": p.entry_price,
        }
        if p.slot_qty is not None:
            pos_data["slot_qty"] = p.slot_qty
        if p.open_order is not None:
            o = p.open_order
            pos_data["order"] = {
                "side": o.side,
                "price": o.price,
                "quantity": o.quantity,
                "age_s": int(now - o.placed_at),
            }
        positions.append(pos_data)

    cycles = []
    for c in strat.cycles[-50:]:
        cycles.append(
            {
                "number": c.number,
                "slot_id": c.slot_id,
                "buy_price": c.buy_price,
                "sell_price": c.sell_price,
                "gross_pct": round(c.gross_pct, 4),
                "net_pnl": round(c.net_pnl, 6),
                "fee": round(c.fee_estimate, 6),
                "timestamp": c.timestamp,
            }
        )

    prices_list = list(strat.prices)

    strategy_state = {}
    if hasattr(strat, "macro_regime"):
        analysis = getattr(strat, "last_analysis", None) or {}
        reasons = analysis.get("reasons", [])

        trend_4h = analysis.get("trend_4h", {})
        pullback_5m = analysis.get("pullback_5m", {})
        mode_data = analysis.get("market_mode", {})
        macro_data = analysis.get("macro_regime", {})
        daily_data = analysis.get("daily_bias", {})

        cooldown_remaining = 0
        last_sell_t = getattr(strat, "_last_sell_time", 0)
        if last_sell_t > 0:
            elapsed = now - last_sell_t
            if elapsed < config.COOLDOWN_SEC:
                cooldown_remaining = int(config.COOLDOWN_SEC - elapsed)

        strategy_state = {
            "macro_regime": strat.macro_regime,
            "daily_bias": strat.daily_bias,
            "market_mode": strat.market_mode,
            "action": strat.action,
            "wallet_base_qty": round(
                getattr(strat, "wallet_base_qty", 0.0) or 0.0, 8
            ),
            "entry_block": getattr(strat, "last_entry_block_reason", None),
            "sell_block": getattr(strat, "last_sell_block_reason", None),
            "reasons": reasons,
            "cooldown_s": cooldown_remaining,
            "trend_4h": trend_4h.get("trend", "unknown"),
            "trend_4h_strength": trend_4h.get("strength", 0),
            "pullback_valid": pullback_5m.get("pullback_valid", False),
            "pullback_pct": pullback_5m.get("pullback_pct", 0),
            "suggested_entry": analysis.get("suggested_entry_price"),
            "position_size_mod": analysis.get("position_size_modifier", 1.0),
            "trade_type": analysis.get("trade_type", "trend"),
            "mode_indicators": mode_data.get("indicators", {}),
            "macro_detail": macro_data.get("detail", ""),
            "daily_detail": daily_data.get("detail", ""),
        }

    return {
        "timestamp": now,
        "uptime_s": uptime,
        "symbol": config.SYMBOL,
        "mainnet": config.USE_MAINNET,
        "price": strat.current_price,
        "ma": strat.ma,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "trade_size_usdt": config.TRADE_SIZE_USDT,
        "prices": prices_list,
        "positions": positions,
        "cycles": cycles,
        "strategy": strategy_state,
        "session": {
            "starting_balance": round(strat.starting_balance, 4),
            "equity_usdt": round(strat.session_equity_usdt, 4),
            "fees_paid": round(strat.total_fees, 6),
        },
        "alltime": {
            "total_cycles": strat.ledger.total_cycles,
            "total_net_pnl": round(strat.ledger.total_net_pnl, 6),
            "total_fees": round(strat.ledger.total_fees, 6),
            "first_cycle_ts": strat.ledger.first_cycle_ts,
        },
        "errors": strat.errors[-5:],
        "last_action": last_action,
        "logs": log_buffer.recent(50, modules=log_buffer.LIVE_MODULES),
    }


# ── Event detection (for Telegram notifications) ─────────────────────


class EventTracker:
    """Tracks state transitions to fire one-shot notifications."""

    def __init__(self) -> None:
        self.prev_state: Optional[str] = None
        self.prev_cycle_count: int = 0
        self.prev_error_count: int = 0

    def detect_events(
        self, strat: TrendAwareMakerStrategy, action: Optional[str]
    ) -> None:
        """Compare current state against previous tick and fire notifications."""
        pos = strat.positions[0]
        curr_state = pos.state.name

        # Buy filled: BUY_PLACED → HOLDING
        if self.prev_state == "BUY_PLACED" and curr_state == "HOLDING":
            notifications.notify_buy_filled(pos.entry_price, pos.slot_qty or 0)

        # Buy placed: WATCHING → BUY_PLACED
        if self.prev_state == "WATCHING" and curr_state == "BUY_PLACED":
            if pos.open_order:
                usdt = pos.open_order.price * pos.open_order.quantity
                notifications.notify_buy_placed(
                    pos.open_order.price, pos.open_order.quantity, usdt
                )

        # Sell placed: HOLDING → SELL_PLACED
        if self.prev_state == "HOLDING" and curr_state == "SELL_PLACED":
            if pos.open_order:
                notifications.notify_sell_placed(
                    pos.open_order.price, pos.open_order.quantity, "TP"
                )

        # Cycle complete
        if len(strat.cycles) > self.prev_cycle_count:
            for c in strat.cycles[self.prev_cycle_count :]:
                # Check if this was a stop loss or emergency sell
                if action and ("STOP_LOSS" in (action or "")):
                    loss_pct = (c.sell_price - c.buy_price) / c.buy_price * 100
                    notifications.notify_stop_loss(c.sell_price, c.buy_price, loss_pct)
                elif action and any(
                    tag in (action or "")
                    for tag in ("MODE_DOWN", "MACRO_BEARISH", "EMERGENCY")
                ):
                    notifications.notify_emergency_sell(
                        action or "unknown", c.sell_price, c.net_pnl
                    )
                else:
                    notifications.notify_cycle_complete(
                        c.number, c.buy_price, c.sell_price, c.net_pnl, c.gross_pct
                    )

        # New errors
        if len(strat.errors) > self.prev_error_count:
            for err in strat.errors[self.prev_error_count :]:
                notifications.notify_error(err)

        # Update tracking
        self.prev_state = curr_state
        self.prev_cycle_count = len(strat.cycles)
        self.prev_error_count = len(strat.errors)


# ── Main async bot loop ──────────────────────────────────────────────


async def run_bot(ws_manager: WSManager) -> None:
    """The main trading bot loop, running as an asyncio background task.

    Mirrors the logic from bot.py's main() function but:
    - Runs async (uses asyncio.sleep instead of time.sleep)
    - Pushes state via WebSocket instead of writing state.json
    - Fires Telegram notifications on key events
    - Still writes state.json for backward compatibility
    """
    env_label = "MAINNET" if config.USE_MAINNET else "TESTNET"
    log.info("Bot runner starting on %s  symbol=%s", env_label, config.SYMBOL)

    # ── Startup checks ───────────────────────────────────────────────
    price_prec, qty_prec, min_notional = _get_precision()
    log.info(
        "Precision: price=%d  qty=%d  min_notional=%.2f USDT",
        price_prec,
        qty_prec,
        min_notional,
    )

    price_now = float(market_data.get_price()["price"])
    balances = _get_balances()

    base_asset = config.SYMBOL.replace("USDT", "")
    usdt_free = balances.get("USDT", {}).get("free", 0.0)
    usdt_locked = balances.get("USDT", {}).get("locked", 0.0)
    base_free = balances.get(base_asset, {}).get("free", 0.0)
    base_locked = balances.get(base_asset, {}).get("locked", 0.0)

    starting_value = (usdt_free + usdt_locked) + (base_free + base_locked) * price_now
    log.info("Starting value: %.4f USDT", starting_value)

    strat = TrendAwareMakerStrategy(
        price_precision=price_prec,
        qty_precision=qty_prec,
        starting_balance=starting_value,
    )

    # ── Recover existing open orders ─────────────────────────────────
    from trading import get_open_orders, cancel_order

    try:
        existing_orders = get_open_orders()
    except Exception as exc:
        existing_orders = []
        log.warning("Could not fetch open orders: %s", exc)

    if existing_orders:
        log.info("Found %d open orders on Binance", len(existing_orders))
        sorted_orders = sorted(existing_orders, key=lambda o: o.get("time", 0), reverse=True)
        newest = sorted_orders[0]
        dupes = sorted_orders[1:]

        for dup in dupes:
            try:
                cancel_order(dup["orderId"])
                log.info("Cancelled duplicate order #%s (%s %.6f @ %s)",
                         dup["orderId"], dup["side"], float(dup["origQty"]), dup["price"])
            except Exception as exc:
                log.warning("Failed to cancel order #%s: %s", dup["orderId"], exc)

        action = strat.recover_open_order(
            order_id=newest["orderId"],
            side=newest["side"],
            price=float(newest["price"]),
            quantity=float(newest["origQty"]),
        )
        if action:
            log.info("RECOVERY: %s", action)
            notifications.send(
                f"🔄 <b>Order Recovered</b>\n\n"
                f"{newest['side']} {float(newest['origQty']):.6f} {config.SYMBOL}\n"
                f"Price: <code>${float(newest['price']):,.2f}</code>\n"
                f"Order ID: <code>{newest['orderId']}</code>"
                + (f"\n\nCancelled {len(dupes)} duplicate(s)" if dupes else "")
            )

    # ── Bootstrap klines ─────────────────────────────────────────────
    log.info("Bootstrapping historical klines...")
    boot_msg = strat.bootstrap()
    log.info(boot_msg)

    # Free BTC reconciliation
    if strat.positions[0].state == State.WATCHING and base_free > 0:
        floored = _floor_qty(base_free, qty_prec)
        notion = floored * price_now
        if floored > 0 and notion >= min_notional:
            wmsg = strat.reconcile_wallet_btc(floored, price_now, min_notional)
            if wmsg:
                log.info("Wallet BTC: %s", wmsg)

    # ── Trade interceptor (always wired — auto_approve flag handles the rest)
    auto_label = "AUTO-APPROVE" if trade_manager.auto_approve else "MANUAL APPROVAL"
    log.info("Trade gate: %s (toggle with /autotrade on Telegram)", auto_label)

    def _intercept_trade(strategy_name, side, price, quantity, size_usdt):
        asyncio.ensure_future(trade_manager.create_trade(
            strategy=strategy_name,
            pair=config.SYMBOL,
            side=side,
            quantity=quantity,
            price=price,
            size_usdt=size_usdt,
            source="bot_signal",
        ))

    strat.on_trade_intercepted = _intercept_trade

    async def _on_trade_resolved(trade):
        from api.trade_manager import TradeStatus
        from api.telegram_bot import send_resolution_update
        try:
            await send_resolution_update(trade)
        except Exception:
            pass
        if trade.status == TradeStatus.EXECUTED and trade.side == "BUY":
            order_id = (trade.execution_result or {}).get("orderId", 0)
            strat.apply_external_fill(order_id, trade.price_at_request, trade.quantity)
        elif trade.status == TradeStatus.FAILED:
            strat.cancel_pending_approval(f"execution failed: {trade.execution_result}")
        elif trade.status == TradeStatus.REJECTED:
            strat.cancel_pending_approval(f"rejected: {trade.reject_reason}")

    trade_manager._on_approval = _on_trade_resolved
    trade_manager._on_rejection = _on_trade_resolved

    if not hasattr(trade_manager, '_expiry_patched'):
        _orig_expire = trade_manager._expire_stale
        def _patched_expire():
            _orig_expire()
            pos = strat.positions[0]
            if (pos.state.name == "BUY_PLACED"
                    and pos.open_order
                    and pos.open_order.order_id == 0):
                pending = [t for t in trade_manager._trades.values()
                           if t.status.value == "pending" and t.side == "BUY"]
                if not pending:
                    strat.cancel_pending_approval("no pending trades remain")
        trade_manager._expire_stale = _patched_expire
        trade_manager._expiry_patched = True

    # ── Notify startup ───────────────────────────────────────────────
    notifications.notify_bot_started(config.SYMBOL, config.USE_MAINNET, starting_value)

    start_time = time.time()
    last_action: Optional[str] = "starting up…"
    event_tracker = EventTracker()

    log.info("Entering trading loop  TRADE_SIZE=%s USDT  TP=%.1f%%  SL=%.1f%%",
             config.TRADE_SIZE_USDT, config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT)

    _status_tick = 0
    _STATUS_INTERVAL = 10  # log status every N ticks (~30s at 3s poll)
    _tg_status_tick = 0
    _TG_STATUS_INTERVAL = 300  # Telegram status every N ticks (~15 min at 3s poll)

    # ── Main loop ────────────────────────────────────────────────────
    try:
        while True:
            try:
                price_data = market_data.get_price()
                price = float(price_data["price"])

                action = strat.tick(price)
                if action:
                    last_action = action
                    log.info(action)

                try:
                    strat.session_equity_usdt = _portfolio_equity_usdt(price)
                except Exception as exc:
                    log.debug("equity refresh failed: %s", exc)

                _status_tick += 1
                if _status_tick >= _STATUS_INTERVAL:
                    _status_tick = 0
                    pos = strat.positions[0]
                    state_name = pos.state.name
                    usdt_free = 0.0
                    try:
                        bals = _get_balances()
                        usdt_free = bals.get("USDT", {}).get("free", 0.0)
                    except Exception:
                        pass

                    if state_name == "WATCHING":
                        parts = [
                            f"${price:,.2f}",
                            f"macro={strat.macro_regime}",
                            f"daily={strat.daily_bias}",
                            f"4h={getattr(strat, '_last_4h_trend', '?')}",
                            f"1h={strat.market_mode}",
                            f"signal={strat.action}",
                            f"free=${usdt_free:.2f}",
                            f"need=${config.TRADE_SIZE_USDT:.0f}",
                        ]
                        block = strat.last_entry_block_reason
                        if block:
                            parts.append(f"block={block}")
                        cooldown = 0
                        if hasattr(strat, '_last_sell_time') and strat._last_sell_time > 0:
                            elapsed = time.time() - strat._last_sell_time
                            if elapsed < config.COOLDOWN_SEC:
                                cooldown = int(config.COOLDOWN_SEC - elapsed)
                        if cooldown > 0:
                            parts.append(f"cooldown={cooldown}s")
                        log.info("SCANNING | %s", " | ".join(parts))
                    elif state_name == "BUY_PLACED":
                        oid = pos.open_order.order_id if pos.open_order else 0
                        age = int(time.time() - pos.open_order.placed_at) if pos.open_order else 0
                        if oid == 0:
                            log.info("PENDING APPROVAL | waiting for Telegram | age=%ds", age)
                        else:
                            log.info("BUY OPEN | order=%s | price=$%s | age=%ds", oid, f"{pos.open_order.price:,.2f}", age)
                    elif state_name in ("HOLDING", "SELL_PLACED"):
                        pnl_pct = ((price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price > 0 else 0
                        tp = pos.entry_price * (1 + config.TAKE_PROFIT_PCT / 100) if pos.entry_price > 0 else 0
                        sl = pos.entry_price * (1 - config.STOP_LOSS_PCT / 100) if pos.entry_price > 0 else 0
                        dist_tp = ((tp - price) / price * 100) if tp > 0 else 0
                        log.info(
                            "IN TRADE | entry=$%s | now=$%s | pnl=%+.2f%% | TP=$%s (%.2f%% away) | SL=$%s",
                            f"{pos.entry_price:,.2f}", f"{price:,.2f}", pnl_pct,
                            f"{tp:,.2f}", dist_tp, f"{sl:,.2f}",
                        )

                _tg_status_tick += 1
                if _tg_status_tick >= _TG_STATUS_INTERVAL:
                    _tg_status_tick = 0
                    try:
                        _tg_bals = _get_balances()
                        _tg_usdt = _tg_bals.get("USDT", {}).get("free", 0.0)
                    except Exception:
                        _tg_usdt = 0.0
                    _analysis = getattr(strat, "last_analysis", None) or {}
                    notifications.notify_status_update(
                        price=price,
                        signal=strat.action,
                        reasons=_analysis.get("reasons", []),
                        usdt_free=_tg_usdt,
                        trade_size=config.TRADE_SIZE_USDT,
                        auto_trade=trade_manager.auto_approve,
                        macro=strat.macro_regime,
                        mode_1h=strat.market_mode,
                        pos_state=strat.positions[0].state.name,
                    )

                # Fire Telegram notifications for state transitions
                event_tracker.detect_events(strat, action)

                # Serialize state and broadcast via WebSocket
                state_snapshot = serialize_state(strat, start_time, last_action)
                await ws_manager.broadcast(state_snapshot, channel="live")

                # Also write state.json for backward compat / local debugging
                try:
                    state_writer.write_state(strat, start_time, last_action)
                except Exception:
                    pass  # Non-critical

            except requests.HTTPError as exc:
                resp = exc.response
                body = resp.text if resp is not None else str(exc)
                msg = f"HTTP {resp.status_code if resp else '?'}: {body}"
                log.warning(msg)
                last_action = msg
                strat.errors.append(msg)
            except requests.ConnectionError:
                msg = "connection error, retrying…"
                log.warning(msg)
                last_action = msg
            except Exception as exc:
                msg = f"unexpected: {exc}"
                log.error(msg, exc_info=True)
                last_action = msg
                strat.errors.append(msg)

            await asyncio.sleep(config.POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Bot runner cancelled, cleaning up")
        uptime = int(time.time() - start_time)
        notifications.notify_bot_stopped(uptime, len(strat.cycles))

        if strat.positions[0].open_order is not None:
            log.info("Cancelling open order")
            strat.cancel_all_open_orders()

        log.info(
            "Ran for %ds, %d cycles completed",
            uptime,
            len(strat.cycles),
        )
