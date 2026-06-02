"""
Backtest runner for PullbackStrategyV1.

Fetches historical OHLCV data from Binance and replays the strategy
tick-by-tick over the requested period with zero lookahead bias.
"""

from __future__ import annotations

import bisect
import json
import logging
import time
from typing import List, Optional
from urllib.request import urlopen

from live_visualizer.strategies.pullback_v1 import (
    Candle, PullbackStrategyV1,
)

log = logging.getLogger(__name__)

_BINANCE_REST = "https://data-api.binance.vision/api/v3"


# ── data fetching with pagination ─────────────────────────────────────────────

def _fetch_page(symbol: str, interval: str, start_ms: int, limit: int = 1000) -> list:
    url = (f"{_BINANCE_REST}/klines?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&limit={limit}")
    with urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_all_klines(
    symbol: str,
    interval: str,
    from_ts: int,   # unix seconds
    to_ts: int,
) -> List[Candle]:
    """Fetch all klines between from_ts and to_ts, paginating as needed."""
    candles: List[Candle] = []
    start_ms = from_ts * 1000

    while True:
        rows = _fetch_page(symbol, interval, start_ms)
        if not rows:
            break
        for r in rows:
            t = int(r[0]) // 1000
            if t > to_ts:
                break
            candles.append(Candle(
                time=t,
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            ))
        else:
            if len(rows) < 1000:
                break
            start_ms = int(rows[-1][0]) + 1
            continue
        break

    return candles


# ── backtest ──────────────────────────────────────────────────────────────────

def run_backtest(
    symbol: str = "BTCUSDC",
    from_ts: int = 0,
    to_ts: int   = 0,
    capital: float = 5_000.0,
    tp_pct: float  = 0.001,
    atr_sl_mult: float = 0.5,
    rsi_threshold: float = 45.0,
    tranche_usdc: float  = 1_000.0,
) -> dict:
    """Run a full backtest. Returns result dict."""
    started = time.time()

    # ── fetch data ────────────────────────────────────────────────────────────
    # For indicators we need extra lookback before the period
    lookback_weekly = 60 * 7 * 24 * 3600   # 60 weeks
    lookback_daily  = 365 * 24 * 3600       # 1 year
    lookback_1h     = 30  * 24 * 3600       # 30 days
    lookback_5m     = 7   * 24 * 3600       # 1 week

    log.info("backtest: fetching data %s %s → %s",
             symbol, from_ts, to_ts)

    c_weekly = fetch_all_klines(symbol, "1w", from_ts - lookback_weekly, to_ts)
    c_daily  = fetch_all_klines(symbol, "1d", from_ts - lookback_daily,  to_ts)
    c_1h     = fetch_all_klines(symbol, "1h", from_ts - lookback_1h,     to_ts)
    c_5m     = fetch_all_klines(symbol, "5m", from_ts - lookback_5m,     to_ts)

    log.info("backtest: %d weekly, %d daily, %d 1h, %d 5m candles",
             len(c_weekly), len(c_daily), len(c_1h), len(c_5m))

    # Pre-build time index lists for binary search
    t_weekly = [c.time for c in c_weekly]
    t_daily  = [c.time for c in c_daily]
    t_1h     = [c.time for c in c_1h]

    # Filter 5M candles to actual backtest period
    bt_5m = [c for c in c_5m if c.time >= from_ts]

    if not bt_5m:
        return {"error": "No 5M candles in the requested period"}

    # ── strategy instance ─────────────────────────────────────────────────────
    import os
    os.environ["PULLBACK_TP_PCT"]        = str(tp_pct)
    os.environ["PULLBACK_ATR_SL_MULT"]   = str(atr_sl_mult)
    os.environ["PULLBACK_RSI_THRESHOLD"] = str(rsi_threshold)
    os.environ["PULLBACK_TRANCHE_USDC"]  = str(tranche_usdc)

    strat = PullbackStrategyV1(symbol=symbol, capital=capital)

    # ── replay loop ───────────────────────────────────────────────────────────
    equity_curve: List[dict] = []
    realized_pnl = 0.0

    for i, candle in enumerate(bt_5m):
        t = candle.time

        # Build "what was available at time t" slices (no lookahead)
        j_w = bisect.bisect_right(t_weekly, t) - 1
        j_d = bisect.bisect_right(t_daily,  t) - 1
        j_h = bisect.bisect_right(t_1h,     t) - 1

        w_weekly = c_weekly[max(0, j_w - 51): j_w + 1]   if j_w >= 0 else []
        w_daily  = c_daily [max(0, j_d - 199):j_d + 1]   if j_d >= 0 else []
        w_1h     = c_1h    [max(0, j_h - 199):j_h + 1]   if j_h >= 0 else []
        w_5m     = c_5m    [max(0, i - 199):  i + 1]      # includes current candle

        strat.tick(w_5m, w_1h, w_daily, w_weekly, candle.close)

        # Record equity every hour (every 12 5m candles)
        if i % 12 == 0:
            open_pnl = sum(
                (candle.close - t2.entry_price) * t2.qty
                for t2 in strat.tranches if t2.state == "OPEN"
            )
            realized_pnl = sum(t2.pnl for t2 in strat.tranches
                               if t2.state in ("CLOSED", "STOPPED"))
            equity_curve.append({
                "time":   t,
                "equity": round(capital + realized_pnl + open_pnl, 2),
                "price":  candle.close,
            })

    # ── build results ──────────────────────────────────────────────────────────
    closed = [(b, s) for b, s in _pair_ledger(strat.ledger)]

    total_trades  = len(closed)
    wins          = [s for _, s in closed if s.pnl > 0]
    losses        = [s for _, s in closed if s.pnl <= 0]
    total_pnl     = sum(s.pnl for _, s in closed)
    win_rate      = len(wins) / total_trades * 100 if total_trades else 0

    # Max drawdown from equity curve
    peak = capital
    max_dd = 0.0
    for pt in equity_curve:
        if pt["equity"] > peak:
            peak = pt["equity"]
        dd = (peak - pt["equity"]) / peak * 100
        if dd > max_dd:
            max_dd = dd

    trades_list = []
    for buy, sell in closed:
        trades_list.append({
            "id":          buy.tranche_id,
            "entry_time":  buy.timestamp,
            "exit_time":   sell.timestamp,
            "entry_price": buy.price,
            "exit_price":  sell.price,
            "qty":         round(buy.qty, 8),
            "size_usdc":   round(buy.usdc, 2),
            "pnl":         round(sell.pnl, 4),
            "result":      sell.reason,
        })

    # Also include still-open positions at end of period
    open_trades = [
        {
            "id":          t.id,
            "entry_time":  t.entry_time,
            "exit_time":   None,
            "entry_price": t.entry_price,
            "exit_price":  None,
            "qty":         round(t.qty, 8),
            "size_usdc":   round(t.entry_price * t.qty, 2),
            "pnl":         round((bt_5m[-1].close - t.entry_price) * t.qty, 4),
            "result":      "OPEN",
        }
        for t in strat.tranches if t.state == "OPEN"
    ]

    elapsed = round(time.time() - started, 1)

    return {
        "symbol":        symbol,
        "from_ts":       from_ts,
        "to_ts":         to_ts,
        "capital":       capital,
        "params": {
            "tp_pct":        tp_pct,
            "atr_sl_mult":   atr_sl_mult,
            "rsi_threshold": rsi_threshold,
            "tranche_usdc":  tranche_usdc,
        },
        # summary
        "total_trades":  total_trades,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 1),
        "total_pnl":     round(total_pnl, 4),
        "max_drawdown":  round(max_dd, 2),
        "best_trade":    round(max((s.pnl for _, s in closed), default=0), 4),
        "worst_trade":   round(min((s.pnl for _, s in closed), default=0), 4),
        "final_equity":  round(capital + total_pnl, 2),
        "candles_processed": len(bt_5m),
        "elapsed_s":     elapsed,
        # data
        "trades":        sorted(trades_list + open_trades,
                                key=lambda t: t["entry_time"], reverse=True),
        "equity_curve":  equity_curve,
    }


def _pair_ledger(ledger):
    """Yield (buy_entry, sell_entry) pairs from ledger."""
    buys  = {}
    sells = {}
    for e in ledger:
        if e.side == "BUY":
            buys[e.tranche_id] = e
        elif e.side == "SELL":
            sells[e.tranche_id] = e
    for tid, buy in buys.items():
        if tid in sells:
            yield buy, sells[tid]
