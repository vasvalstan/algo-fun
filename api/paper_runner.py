import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import requests

import config
import market_data
from indicators import StrategyEngine
from api.ws_manager import WSManager
from dry_run import PaperWallet, _parse_kline, _fetch, BOOTSTRAP_COUNTS, REFRESH_INTERVALS

log = logging.getLogger(__name__)


def serialize_paper_state(
    wallet: PaperWallet,
    price: float,
    start_time: float,
    analysis: dict,
    tracker_prices: List[float],
    last_action: Optional[str] = None
) -> dict:
    """Build the BotState dict for the frontend using PaperWallet state."""
    now = time.time()
    uptime = int(now - start_time)

    # Map position
    positions = []
    if wallet.in_position:
        positions.append({
            "slot_id": 0,
            "state": "HOLDING",
            "entry_price": wallet.entry_price,
            "slot_qty": wallet.position_qty,
        })
    else:
        positions.append({
            "slot_id": 0,
            "state": "WATCHING",
            "entry_price": 0.0,
        })

    # Map cycles (closed trades)
    cycles = []
    for i, t in enumerate(wallet.trades[-50:]):
        # We estimate fee since PaperTrade doesn't store fee explicitly, just net PNL
        # Gross = entry_price * qty. Net = gross - fee.
        gross_pnl = t.pnl # t.pnl is net actually, close enough
        cycles.append({
            "number": i + 1,
            "slot_id": 0,
            "buy_price": t.entry_price,
            "sell_price": t.exit_price,
            "gross_pct": t.pnl_pct,
            "net_pnl": t.pnl,
            "fee": 0, # fee is already handled in dry_run pnl calculation
            "timestamp": now, # fallback if parsing fails, but frontend mostly just shows date
        })

    strategy_state = {
        "macro_regime": analysis.get("macro_regime", {}).get("regime", "UNKNOWN"),
        "daily_bias": analysis.get("daily_bias", {}).get("bias", "UNKNOWN"),
        "market_mode": analysis.get("market_mode", {}).get("mode", "WAIT"),
        "action": analysis.get("action", "WAIT"),
        "wallet_base_qty": wallet.btc,
        "entry_block": None,
        "sell_block": None,
    }

    mode_ind = analysis.get("market_mode", {}).get("indicators", {})
    ma = mode_ind.get("ema_20")

    total_net_pnl = sum(t.pnl for t in wallet.trades)

    return {
        "timestamp": now,
        "uptime_s": uptime,
        "symbol": config.SYMBOL,
        "mainnet": config.USE_MAINNET,
        "price": price,
        "ma": ma,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "trade_size_usdt": config.TRADE_SIZE_USDT,
        "prices": tracker_prices,
        "positions": positions,
        "cycles": cycles,
        "strategy": strategy_state,
        "session": {
            "starting_balance": wallet.starting_capital,
            "equity_usdt": wallet.equity(price),
            "fees_paid": 0,
        },
        "alltime": {
            "total_cycles": len(wallet.trades),
            "total_net_pnl": total_net_pnl,
            "total_fees": 0,
            "first_cycle_ts": now,
        },
        "errors": [],
        "last_action": last_action,
    }


async def run_paper_bot(ws_manager: WSManager) -> None:
    """Async paper trading bot that feeds the /ws/paper channel."""
    env_label = "MAINNET" if config.USE_MAINNET else "TESTNET"
    log.info("Paper runner starting for %s on %s", config.SYMBOL, env_label)

    # Hardcoded $5000 USDT for testing paper mode
    STARTING_CAPITAL = 5000.0
    wallet = PaperWallet(
        usdt=STARTING_CAPITAL,
        starting_capital=STARTING_CAPITAL,
        peak_equity=STARTING_CAPITAL,
    )

    # Bootstrap
    log.info("Paper runner bootstrapping historical klines...")
    engine = StrategyEngine()
    for interval, count in BOOTSTRAP_COUNTS.items():
        try:
            raw = _fetch(interval, count)
            for k in raw:
                engine.update_candle(interval, _parse_kline(k))
        except Exception as exc:
            log.warning("Paper bootstrap %s failed: %s", interval, exc)

    start_time = time.time()
    last_refresh = dict.fromkeys(REFRESH_INTERVALS, 0.0)
    tracker_prices: List[float] = []
    last_action = "Started paper trader with $5000"

    log.info("Paper runner entering trading loop")
    try:
        while True:
            try:
                price_data = market_data.get_price()
                price = float(price_data["price"])

                tracker_prices.append(price)
                if len(tracker_prices) > 60:
                    tracker_prices.pop(0)

                now = time.time()
                wallet.update_drawdown(price)

                for interval, every in REFRESH_INTERVALS.items():
                    if now - last_refresh.get(interval, 0) >= every:
                        try:
                            lim = 5 if interval in ("5m", "1h") else 3
                            raw = _fetch(interval, lim)
                            for k in raw:
                                engine.update_candle(interval, _parse_kline(k))
                            last_refresh[interval] = now
                        except Exception:
                            pass

                analysis = engine.get_full_analysis()

                # Paper tick logic
                event = ""
                if wallet.in_position:
                    pnl_pct = (price - wallet.entry_price) / wallet.entry_price * 100
                    tp = config.TAKE_PROFIT_PCT
                    sl = config.STOP_LOSS_PCT

                    if pnl_pct >= tp:
                        event = wallet.sell(price, "TP")
                    elif pnl_pct <= -sl:
                        event = wallet.sell(price, "SL")
                    else:
                        mode = analysis.get("market_mode", {}).get("mode", "WAIT")
                        macro = analysis.get("macro_regime", {}).get("regime", "UNKNOWN")
                        if mode == "DOWN" and macro != "BULL_RUN":
                            event = wallet.sell(price, "MODE_DOWN")
                        elif macro == "BEARISH":
                            event = wallet.sell(price, "MACRO_BEAR")
                else:
                    cooldown = config.COOLDOWN_SEC
                    if (now - wallet.last_sell_ts) >= cooldown:
                        action = analysis.get("action", "WAIT")
                        if action == "ENTRY_READY":
                            size_mod = analysis.get("position_size_modifier", 1.0)
                            usdt_amount = config.TRADE_SIZE_USDT * size_mod
                            if usdt_amount > wallet.usdt:
                                usdt_amount = wallet.usdt
                            if usdt_amount >= 5:
                                entry_p = analysis.get("suggested_entry_price") or price
                                wallet.trade_type = analysis.get("trade_type", "trend")
                                event = wallet.buy(entry_p, usdt_amount, analysis.get("market_mode", {}).get("mode", ""))

                if event:
                    last_action = event
                    log.info("PAPER EVENT: %s", event)

                state_snapshot = serialize_paper_state(
                    wallet, price, start_time, analysis, tracker_prices, last_action
                )
                await ws_manager.broadcast(state_snapshot, channel="paper")

            except Exception as exc:
                log.error("Paper runner loop error: %s", exc)

            await asyncio.sleep(config.POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("Paper runner cancelled, cleaning up")
        
