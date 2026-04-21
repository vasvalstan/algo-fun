"""
V2 Multi-Strategy Paper Trading Runner.

Runs multiple strategies (V2 Adaptive, Mean Reversion, Breakout) on multiple
pairs (BTCUSDT, BTCFDUSD) in parallel, each with its own wallet (~$1,666).

Features over V1 paper_runner:
  • ATR-based dynamic TP/SL
  • Trailing take profit (sell 50% at +1.5×ATR, trail rest at 1.0×ATR)
  • Time-in-trade exit (45 min max hold)
  • Per-strategy educational explanations for the dashboard
  • Independent wallets with combined equity tracking
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
import market_data
from indicators import StrategyEngine
from api.strategy_params import parse_strategy_params
from api import strategy_runtime
from api.ws_manager import WSManager
from api.bitcoin_sandbox import BitcoinSandboxState, sandbox_params_from_pydantic
from api import notifications
from dry_run import PaperWallet, _parse_kline, BOOTSTRAP_COUNTS, REFRESH_INTERVALS

log = logging.getLogger(__name__)

LEGACY_STRATEGY_IDS = frozenset({"v2_adaptive", "mean_reversion", "breakout"})

# Shared paper sandbox state (HTTP + runner loop). Mutations must hold SANDBOX_LOCK.
SANDBOX_LOCK = asyncio.Lock()
SANDBOX_STATE_BY_ID: dict[str, BitcoinSandboxState] = {}
SANDBOX_PREV_PX: dict[str, float] = {}

# ── Strategy metadata (paper V2 UI) ───────────────────────────────────

STRATEGY_META = {
    "bitcoin_sandbox": {
        "name": "BTC $60k–$85k Sandbox Grid",
        "short": "Sandbox",
        "description": (
            "Geofenced grid ($60k–$85k): trails spot high with a limit buy −0.75%, brackets each fill "
            "with +0.71% take-profit and the next buy −0.75%. Pauses new buys outside the band; "
            "resting sells stay working. Sized for ~26 tranches with a USDT reserve."
        ),
        "color": "#f7931a",  # bitcoin orange
        "icon": "₿",
    },
}

# ── Educational explanations ─────────────────────────────────────────

GLOSSARY = {
    "ATR": "Average True Range — measures how much an asset typically moves in a given period. Higher ATR = more volatile.",
    "Keltner Channel": "A volatility envelope around an EMA. When price touches the lower band, it suggests a pullback in an uptrend.",
    "VWAP": "Volume Weighted Average Price — the average price weighted by volume. Institutional benchmark for fair value.",
    "MACD": "Moving Average Convergence Divergence — shows momentum direction. Rising histogram = strengthening momentum.",
    "Bollinger Band": "A volatility band around a moving average. Prices touching outer bands often snap back to the middle.",
    "CVD": "Cumulative Volume Delta — net buying vs selling pressure. High sell ratio = toxic flow, unsafe for entries.",
    "RSI": "Relative Strength Index (0-100). Below 30 = oversold (possible bounce), above 70 = overbought (possible drop).",
    "EMA": "Exponential Moving Average — recent prices weighted more heavily. EMA20 > EMA50 = short-term uptrend.",
    "Trailing Stop": "A stop-loss that moves up as price rises, locking in profits while letting winners run.",
    "Bandwidth": "Bollinger Band width relative to the middle. Low bandwidth = tight compression, often precedes a big move.",
}


def _explain_state(strategy_id: str, analysis: dict, wallet: PaperWallet, price: float) -> dict:
    """Generate beginner-friendly explanation of current strategy state."""
    meta = STRATEGY_META.get(strategy_id, {})
    action = analysis.get("action", "WAIT")
    reasons = analysis.get("reasons", [])
    layers = analysis.get("layers", [])

    if wallet.in_position:
        pnl_pct = (price - wallet.entry_price) / wallet.entry_price * 100
        if pnl_pct > 0:
            current_state = (
                f"We're in a trade and it's going well! The position is up {pnl_pct:+.2f}%. "
                f"Bought at ${wallet.entry_price:,.2f}, current price ${price:,.2f}."
            )
        else:
            current_state = (
                f"We're in a trade but it's slightly underwater ({pnl_pct:+.2f}%). "
                f"Bought at ${wallet.entry_price:,.2f}, current price ${price:,.2f}. "
                f"The stop-loss will protect us if it drops further."
            )
    elif action == "ENTRY_READY":
        current_state = "All conditions are met! The strategy is ready to enter a trade."
    elif action == "NO_TRADE":
        current_state = "Market conditions don't favor this strategy right now. Staying safely on the sidelines."
    elif action == "WAIT_FOR_DIP":
        current_state = "The trend is right but we need price to pull back a bit before entering. Patience pays."
    elif action == "WAIT_FOR_CLOSE":
        current_state = "A setup is forming! We're watching for the candle to close to confirm the signal."
    else:
        current_state = "Scanning the market for opportunities. The bot continuously checks all conditions."

    # Layer summary
    pass_count = sum(1 for l in layers if l.get("status") == "PASS")
    total = len(layers)
    if total > 0:
        layer_summary = f"{pass_count} of {total} conditions are met."
    else:
        layer_summary = "Warming up indicator data..."

    return {
        "strategy_summary": meta.get("description", ""),
        "current_state": current_state,
        "layer_summary": layer_summary,
        "layers": layers,
    }


def _serialize_strategy_state(
    strategy_id: str,
    pair: str,
    wallet: PaperWallet,
    price: float,
    analysis: dict,
    holding_since: Optional[float],
) -> dict:
    """Build per-strategy state for the dashboard."""
    meta = STRATEGY_META.get(strategy_id, {})
    eq = wallet.equity(price)
    pnl = eq - wallet.starting_capital
    pnl_pct = (pnl / wallet.starting_capital * 100) if wallet.starting_capital > 0 else 0

    # Position info
    position = None
    status = "WATCHING"
    if wallet.in_position:
        ur_pct = (price - wallet.entry_price) / wallet.entry_price * 100
        ur_usdt = wallet.position_qty * (price - wallet.entry_price)
        hold_min = int((time.time() - holding_since) / 60) if holding_since else 0
        status = "HOLDING"
        position = {
            "entry_price": round(wallet.entry_price, 2),
            "entry_time": wallet.entry_time,
            "qty": round(wallet.position_qty, 8),
            "usdt": round(wallet.position_usdt, 4),
            "unrealized_pct": round(ur_pct, 4),
            "unrealized_usdt": round(ur_usdt, 6),
            "hold_minutes": hold_min,
        }

    # Trade performance
    trades = wallet.trades
    pnls = [t.pnl for t in trades]
    winners = [p for p in pnls if p > 0]
    performance = {
        "total_trades": len(trades),
        "win_rate": round(len(winners) / len(pnls) * 100, 1) if pnls else 0,
        "total_pnl": round(sum(pnls), 4) if pnls else 0,
        "best_trade": round(max(pnls), 4) if pnls else 0,
        "worst_trade": round(min(pnls), 4) if pnls else 0,
        "avg_hold_time_min": 0,
    }

    # Trade history
    trade_history = []
    for t in trades[-20:]:
        trade_history.append({
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "pnl": round(t.pnl, 6),
            "pnl_pct": round(t.pnl_pct, 4),
            "exit_reason": t.exit_reason,
        })

    explanation = _explain_state(strategy_id, analysis, wallet, price)

    return {
        "id": strategy_id,
        "name": meta.get("name", strategy_id),
        "short": meta.get("short", strategy_id),
        "pair": pair,
        "color": meta.get("color", "#888"),
        "icon": meta.get("icon", "📊"),
        "status": status,
        "wallet": {
            "starting": round(wallet.starting_capital, 2),
            "equity": round(eq, 4),
            "usdt": round(wallet.usdt, 4),
            "btc": round(wallet.btc, 8),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
        },
        "position": position,
        "last_signal": {
            "action": analysis.get("action", "WAIT"),
            "reasons": analysis.get("reasons", []),
        },
        "indicators": analysis.get("indicators", {}),
        "tp_price": analysis.get("tp_price"),
        "sl_price": analysis.get("sl_price"),
        "tp_type": analysis.get("tp_type", "fixed"),
        "trade_history": trade_history,
        "performance": performance,
        "explanation": explanation,
    }


def _serialize_bitcoin_sandbox(
    strategy_id: str,
    pair: str,
    state: BitcoinSandboxState,
    price: float,
    analysis: dict,
) -> dict:
    """Dashboard JSON for the BTC sandbox grid (multi-lot paper sim)."""
    meta = STRATEGY_META.get(strategy_id, {})
    eq = state.equity(price)
    pnl = eq - state.starting_capital
    pnl_pct = (pnl / state.starting_capital * 100) if state.starting_capital > 0 else 0

    position = None
    status = "PAUSED" if state.status == "PAUSED" else "WATCHING"
    if state.holdings:
        status = "HOLDING"
        qty_sum = sum(h.qty for h in state.holdings)
        w_entry = sum(h.qty * h.entry_price for h in state.holdings) / qty_sum if qty_sum else 0
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
            "open_lots": len(state.holdings),
        }

    closed = state.closed_trades
    pnls = [t.pnl_usdt for t in closed]
    winners = [p for p in pnls if p > 0]
    performance = {
        "total_trades": len(closed),
        "win_rate": round(len(winners) / len(pnls) * 100, 1) if pnls else 0,
        "total_pnl": round(sum(pnls), 4) if pnls else 0,
        "best_trade": round(max(pnls), 4) if pnls else 0,
        "worst_trade": round(min(pnls), 4) if pnls else 0,
        "avg_hold_time_min": 0,
    }

    trade_history = []
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
            "exit_reason": "SANDBOX_TP",
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
    current_state = (
        f"Sandbox {state.status}: {len(state.holdings)} lot(s), "
        f"equity ${eq:,.2f} ({pnl_pct:+.2f}% vs start). "
        f"Trailing buy {state.trailing_buy_price}, grid buy {state.grid_buy_price}."
    )
    explanation = {
        "strategy_summary": meta.get("description", ""),
        "current_state": current_state,
        "layer_summary": sb.get("layers_preview") and f"{len(sb['layers_preview'])} resting / holding rows" or "No open orders",
        "layers": [],
    }

    return {
        "id": strategy_id,
        "name": meta.get("name", strategy_id),
        "short": meta.get("short", strategy_id),
        "pair": pair,
        "color": meta.get("color", "#888"),
        "icon": meta.get("icon", "₿"),
        "status": status,
        "wallet": {
            "starting": round(state.starting_capital, 2),
            "equity": round(eq, 4),
            "usdt": round(state.usdt, 4),
            "btc": round(state.btc, 8),
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
        "explanation": explanation,
    }


# ── Main V2 Paper Bot ───────────────────────────────────────────────


async def run_paper_bot_v2(ws_manager: WSManager) -> None:
    """Async multi-strategy paper trading bot feeding /ws/paper-v2."""
    log.info("V2 Paper runner starting — strategies: %s, pairs: %s",
             config.V2_STRATEGIES, config.V2_PAIRS)

    num_strategies = max(len(config.V2_STRATEGIES), 1)
    capital_per = config.V2_PAPER_CAPITAL / num_strategies
    log.info("Allocating $%.2f per strategy ($%.2f total / %d strategies)",
             capital_per, config.V2_PAPER_CAPITAL, num_strategies)

    pair = config.V2_PAIRS[0] if config.V2_PAIRS else "BTCUSDT"
    log.info("Using pair: %s", pair)

    use_legacy = bool(set(config.V2_STRATEGIES) & LEGACY_STRATEGY_IDS)
    engine: Optional[StrategyEngine] = StrategyEngine() if use_legacy else None


    if use_legacy and engine is not None:
        log.info("V2 bootstrapping historical klines for %s...", pair)
        for interval, count in BOOTSTRAP_COUNTS.items():
            try:
                raw = market_data.get_klines(symbol=pair, interval=interval, limit=count)
                for k in raw:
                    engine.update_candle(interval, _parse_kline(k))
                log.info("V2 bootstrapped %d %s candles", len(raw), interval)
            except Exception as exc:
                log.warning("V2 bootstrap %s failed: %s", interval, exc)
        try:
            raw_15m = market_data.get_klines(symbol=pair, interval="15m", limit=200)
            for k in raw_15m:
                engine.update_candle("15m", _parse_kline(k))
            log.info("V2 bootstrapped %d 15m candles", len(raw_15m))
        except Exception as exc:
            log.warning("V2 bootstrap 15m failed: %s", exc)

    wallets: dict[str, PaperWallet] = {}

    def _ensure_wallet(sid: str) -> PaperWallet:
        if sid not in wallets:
            n = max(len(config.V2_STRATEGIES), 1)
            capital = config.V2_PAPER_CAPITAL / n
            wallets[sid] = PaperWallet(
                usdt=capital,
                starting_capital=capital,
                peak_equity=capital,
            )
        return wallets[sid]

    def _ensure_sandbox(sid: str, raw_params: dict) -> BitcoinSandboxState:
        if sid not in SANDBOX_STATE_BY_ID:
            pr_model = parse_strategy_params(sid, raw_params)
            bp = sandbox_params_from_pydantic(pr_model)
            cap = config.V2_PAPER_CAPITAL / max(len(config.V2_STRATEGIES), 1)

            def _notify(msg: str) -> None:
                notifications.send(msg.replace("🚨", "<b>ALERT</b>"))

            SANDBOX_STATE_BY_ID[sid] = BitcoinSandboxState(
                starting_usdt=cap,
                params=bp,
                notify=_notify if notifications.is_configured() else None,
            )
            SANDBOX_PREV_PX[sid] = 0.0
            log.info(
                "Bitcoin sandbox init capital=%.2f geofence=%.0f–%.0f reserve=%.0f bullets=%d",
                cap,
                bp.geofence_low,
                bp.geofence_high,
                bp.reserve_usdt,
                bp.num_bullets,
            )
        return SANDBOX_STATE_BY_ID[sid]

    for sid in list(config.V2_STRATEGIES):
        if sid in LEGACY_STRATEGY_IDS:
            _ensure_wallet(sid)

    start_time = time.time()
    last_refresh = dict.fromkeys(REFRESH_INTERVALS, 0.0)
    last_refresh["15m"] = 0.0
    holding_since: dict[str, Optional[float]] = {}
    tracker_prices: List[float] = []

    log.info("V2 Paper runner entering trading loop")
    try:
        while True:
            try:
                price_data = market_data.get_price(symbol=pair)
                price = float(price_data["price"])

                tracker_prices.append(price)
                if len(tracker_prices) > 80:
                    tracker_prices.pop(0)

                now = time.time()

                if use_legacy and engine is not None:
                    refresh_map = {**REFRESH_INTERVALS, "15m": 60}
                    for interval, every in refresh_map.items():
                        if now - last_refresh.get(interval, 0) >= every:
                            try:
                                lim = 5 if interval in ("5m", "15m", "1h") else 3
                                raw = market_data.get_klines(symbol=pair, interval=interval, limit=lim)
                                for k in raw:
                                    engine.update_candle(interval, _parse_kline(k))
                                last_refresh[interval] = now
                            except Exception:
                                pass

                strategies_state: List[dict] = []
                strategy_params_snapshot = await strategy_runtime.get_all_effective_params()

                for sid in config.V2_STRATEGIES:
                    if sid == "bitcoin_sandbox":
                        async with SANDBOX_LOCK:
                            st = _ensure_sandbox(sid, strategy_params_snapshot[sid])
                            st.update_drawdown(price)
                            prev = SANDBOX_PREV_PX.get(sid, 0.0)
                            tick_ev: list[str] = []
                            pe = prev if prev > 0 else price
                            st.tick_live(pe, price, tick_ev)
                            SANDBOX_PREV_PX[sid] = price
                        for line in tick_ev:
                            log.info("V2 PAPER [%s] %s", sid, line)
                        analysis = st.to_analysis_dict(price)
                        strategies_state.append(
                            _serialize_bitcoin_sandbox(strategy_id=sid, pair=pair, state=st, price=price, analysis=analysis)
                        )
                        continue

                    if sid not in LEGACY_STRATEGY_IDS or engine is None:
                        log.warning("Unknown or unsupported strategy id %r — skipping", sid)
                        continue

                    wallet = _ensure_wallet(sid)
                    if sid not in holding_since:
                        holding_since[sid] = None
                    wallet.update_drawdown(price)

                    pr = parse_strategy_params(sid, strategy_params_snapshot[sid])
                    if sid == "v2_adaptive":
                        analysis = engine.get_v2_full_analysis(pr)
                    elif sid == "mean_reversion":
                        analysis = engine.get_mean_reversion_analysis(pr)
                    elif sid == "breakout":
                        analysis = engine.get_breakout_analysis(pr)
                    else:
                        continue

                    if wallet.in_position:
                        pnl_pct = (price - wallet.entry_price) / wallet.entry_price * 100
                        tp_p = analysis.get("tp_price")
                        sl_p = analysis.get("sl_price")
                        h_since = holding_since.get(sid)
                        hold_min = (now - h_since) / 60 if h_since else 0
                        if hold_min >= config.V2_MAX_HOLD_MINUTES:
                            event = wallet.sell(price, "TIME_EXIT")
                            if event:
                                log.info("V2 PAPER [%s] %s", sid, event)
                                holding_since[sid] = None
                        elif tp_p and price >= tp_p:
                            event = wallet.sell(price, "TP")
                            if event:
                                log.info("V2 PAPER [%s] %s", sid, event)
                                holding_since[sid] = None
                        elif sl_p and price <= sl_p:
                            event = wallet.sell(price, "SL")
                            if event:
                                log.info("V2 PAPER [%s] %s", sid, event)
                                holding_since[sid] = None
                        elif pnl_pct <= -config.STOP_LOSS_PCT:
                            event = wallet.sell(price, "SL_FIXED")
                            if event:
                                log.info("V2 PAPER [%s] %s", sid, event)
                                holding_since[sid] = None
                    else:
                        cooldown = 300 if sid == "mean_reversion" else config.COOLDOWN_SEC
                        if (now - wallet.last_sell_ts) >= cooldown:
                            action = analysis.get("action", "WAIT")
                            if action == "ENTRY_READY":
                                eq = wallet.equity(price)
                                risk_amount = eq * (config.V2_RISK_PCT / 100)
                                sl_p = analysis.get("sl_price")
                                entry_p = analysis.get("entry_price") or price
                                if sl_p and entry_p > sl_p:
                                    sl_dist_pct = (entry_p - sl_p) / entry_p
                                    usdt_amount = risk_amount / sl_dist_pct if sl_dist_pct > 0 else config.TRADE_SIZE_USDT
                                else:
                                    usdt_amount = config.TRADE_SIZE_USDT
                                usdt_amount = min(usdt_amount, wallet.usdt * 0.95)
                                if usdt_amount >= 5:
                                    event = wallet.buy(entry_p, usdt_amount)
                                    if event:
                                        holding_since[sid] = now
                                        log.info("V2 PAPER [%s] %s", sid, event)

                    strategies_state.append(
                        _serialize_strategy_state(
                            sid, pair, wallet, price, analysis, holding_since.get(sid)
                        )
                    )

                combined_eq = 0.0
                combined_pnl = 0.0
                active_positions = 0
                for s in strategies_state:
                    w = s.get("wallet") or {}
                    combined_eq += float(w.get("equity", 0))
                    combined_pnl += float(w.get("pnl", 0))
                    if s.get("status") == "HOLDING":
                        active_positions += 1

                # Build trade markers for the chart overlay
                trade_markers: List[Dict[str, Any]] = []
                for sid in config.V2_STRATEGIES:
                    if sid == "bitcoin_sandbox":
                        sb = SANDBOX_STATE_BY_ID.get(sid)
                        if sb:
                            for h in sb.holdings:
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
                            for t in sb.closed_trades[-40:]:
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

                state_snapshot = {
                    "timestamp": time.time(),
                    "uptime_s": int(now - start_time),
                    "symbol": pair,
                    "price": round(price, 2),
                    "prices": [round(p, 2) for p in tracker_prices],
                    "trade_markers": trade_markers,
                    "strategies": strategies_state,
                    "global_summary": {
                        "total_strategies": len(config.V2_STRATEGIES),
                        "active_positions": active_positions,
                        "combined_equity": round(combined_eq, 4),
                        "combined_pnl": round(combined_pnl, 4),
                        "combined_pnl_pct": round(
                            combined_pnl / config.V2_PAPER_CAPITAL * 100, 4
                        ) if config.V2_PAPER_CAPITAL > 0 else 0,
                        "starting_capital": round(config.V2_PAPER_CAPITAL, 2),
                    },
                    "glossary": GLOSSARY,
                    "strategy_params": {
                        k: v for k, v in strategy_params_snapshot.items() if k != "_meta"
                    },
                    "strategy_params_meta": strategy_params_snapshot.get("_meta", {}),
                }

                await ws_manager.broadcast(state_snapshot, channel="paper_v2")

            except Exception as exc:
                log.error("V2 paper runner loop error: %s", exc, exc_info=True)

            await asyncio.sleep(config.POLL_INTERVAL)

    except asyncio.CancelledError:
        log.info("V2 Paper runner cancelled, cleaning up")
