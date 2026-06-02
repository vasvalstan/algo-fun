"""
Backtest + optimization engine for PullbackStrategyV1.

Two entry points:
  run_backtest()      — single param set, full trade detail + equity curve
  run_optimization()  — grid search over many param sets, ranked summary

Data is fetched ONCE and reused across all simulations during optimization.
Replays tick-by-tick with zero lookahead bias.
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.request import urlopen

from live_visualizer.strategies.pullback_v1 import Candle, PullbackStrategyV1

log = logging.getLogger(__name__)

_BINANCE_REST = "https://data-api.binance.vision/api/v3"


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch_page(symbol: str, interval: str, start_ms: int, limit: int = 1000) -> list:
    url = (f"{_BINANCE_REST}/klines?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&limit={limit}")
    with urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_all_klines(symbol: str, interval: str, from_ts: int, to_ts: int) -> List[Candle]:
    """Fetch all klines between from_ts and to_ts (unix seconds), paginating."""
    candles: List[Candle] = []
    start_ms = from_ts * 1000
    while True:
        rows = _fetch_page(symbol, interval, start_ms)
        if not rows:
            break
        stop = False
        for r in rows:
            t = int(r[0]) // 1000
            if t > to_ts:
                stop = True
                break
            candles.append(Candle(
                time=t, open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]), volume=float(r[5]),
            ))
        if stop or len(rows) < 1000:
            break
        start_ms = int(rows[-1][0]) + 1
    return candles


@dataclass
class BacktestData:
    symbol:   str
    from_ts:  int
    to_ts:    int
    c_weekly: List[Candle]
    c_daily:  List[Candle]
    c_1h:     List[Candle]
    c_5m:     List[Candle]
    bt_5m:    List[Candle] = field(default_factory=list)
    t_weekly: List[int]    = field(default_factory=list)
    t_daily:  List[int]    = field(default_factory=list)
    t_1h:     List[int]    = field(default_factory=list)

    def __post_init__(self):
        self.t_weekly = [c.time for c in self.c_weekly]
        self.t_daily  = [c.time for c in self.c_daily]
        self.t_1h     = [c.time for c in self.c_1h]
        self.bt_5m    = [c for c in self.c_5m if c.time >= self.from_ts]


def fetch_backtest_data(symbol: str, from_ts: int, to_ts: int) -> BacktestData:
    """Fetch all timeframes with indicator lookback. Slow — do this ONCE."""
    lb_weekly = 60 * 7 * 24 * 3600
    lb_daily  = 365 * 24 * 3600
    lb_1h     = 30  * 24 * 3600
    lb_5m     = 7   * 24 * 3600

    log.info("backtest: fetching %s data %s → %s", symbol, from_ts, to_ts)
    data = BacktestData(
        symbol=symbol, from_ts=from_ts, to_ts=to_ts,
        c_weekly=fetch_all_klines(symbol, "1w", from_ts - lb_weekly, to_ts),
        c_daily =fetch_all_klines(symbol, "1d", from_ts - lb_daily,  to_ts),
        c_1h    =fetch_all_klines(symbol, "1h", from_ts - lb_1h,     to_ts),
        c_5m    =fetch_all_klines(symbol, "5m", from_ts - lb_5m,     to_ts),
    )
    log.info("backtest: %d weekly, %d daily, %d 1h, %d 5m candles",
             len(data.c_weekly), len(data.c_daily), len(data.c_1h), len(data.c_5m))
    return data


# ── simulation ────────────────────────────────────────────────────────────────

def simulate(data: BacktestData, params: dict, capital: float,
             with_details: bool = False) -> dict:
    """Run the strategy over pre-fetched data with one param set."""
    strat = PullbackStrategyV1(symbol=data.symbol, capital=capital, params=params)

    equity_curve: List[dict] = []

    for i, candle in enumerate(data.bt_5m):
        t = candle.time
        j_w = bisect.bisect_right(data.t_weekly, t) - 1
        j_d = bisect.bisect_right(data.t_daily,  t) - 1
        j_h = bisect.bisect_right(data.t_1h,     t) - 1

        w_weekly = data.c_weekly[max(0, j_w - 51): j_w + 1]  if j_w >= 0 else []
        w_daily  = data.c_daily [max(0, j_d - 199):j_d + 1]  if j_d >= 0 else []
        w_1h     = data.c_1h    [max(0, j_h - 199):j_h + 1]  if j_h >= 0 else []
        w_5m     = data.c_5m    [max(0, len(data.c_5m) - len(data.bt_5m) + i - 199):
                                 len(data.c_5m) - len(data.bt_5m) + i + 1]

        strat.tick(w_5m, w_1h, w_daily, w_weekly, candle.close)

        if with_details and i % 12 == 0:
            open_pnl = sum((candle.close - tr.entry_price) * tr.qty
                           for tr in strat.tranches if tr.state == "OPEN")
            real = sum(tr.pnl for tr in strat.tranches
                       if tr.state in ("CLOSED", "STOPPED"))
            equity_curve.append({
                "time": t, "equity": round(capital + real + open_pnl, 2),
                "price": candle.close,
            })

    # ── metrics ──
    closed = list(_pair_ledger(strat.ledger))
    total_trades = len(closed)
    wins   = [s for _, s in closed if s.pnl > 0]
    losses = [s for _, s in closed if s.pnl <= 0]
    total_pnl = sum(s.pnl for _, s in closed)
    win_rate  = len(wins) / total_trades * 100 if total_trades else 0
    gross_win  = sum(s.pnl for _, s in closed if s.pnl > 0)
    gross_loss = abs(sum(s.pnl for _, s in closed if s.pnl <= 0))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (gross_win if gross_win else 0)

    # Max drawdown
    peak, max_dd, running = capital, 0.0, capital
    for _, s in sorted(closed, key=lambda x: x[1].timestamp):
        running += s.pnl
        peak = max(peak, running)
        max_dd = max(max_dd, (peak - running) / peak * 100 if peak else 0)

    open_count = sum(1 for tr in strat.tranches if tr.state == "OPEN")

    result = {
        "params": {
            "tp_pct":        strat.TP_PCT,
            "tp_dollars":    strat.TP_DOLLARS,
            "atr_sl_mult":   strat.ATR_SL_MULT,
            "rsi_threshold": strat.RSI_THRESHOLD,
            "tranche_usdc":  strat.TRANCHE_USDC,
        },
        "total_trades":  total_trades,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 1),
        "total_pnl":     round(total_pnl, 4),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown":  round(max_dd, 2),
        "best_trade":    round(max((s.pnl for _, s in closed), default=0), 4),
        "worst_trade":   round(min((s.pnl for _, s in closed), default=0), 4),
        "final_equity":  round(capital + total_pnl, 2),
        "return_pct":    round(total_pnl / capital * 100, 2),
        "open_at_end":   open_count,
    }

    if with_details:
        trades = [{
            "id": b.tranche_id, "entry_time": b.timestamp, "exit_time": s.timestamp,
            "entry_price": b.price, "exit_price": s.price, "qty": round(b.qty, 8),
            "size_usdc": round(b.usdc, 2), "pnl": round(s.pnl, 4), "result": s.reason,
        } for b, s in closed]
        open_trades = [{
            "id": tr.id, "entry_time": tr.entry_time, "exit_time": None,
            "entry_price": tr.entry_price, "exit_price": None, "qty": round(tr.qty, 8),
            "size_usdc": round(tr.entry_price * tr.qty, 2),
            "pnl": round((data.bt_5m[-1].close - tr.entry_price) * tr.qty, 4),
            "result": "OPEN",
        } for tr in strat.tranches if tr.state == "OPEN"]
        result["trades"] = sorted(trades + open_trades,
                                  key=lambda t: t["entry_time"], reverse=True)
        result["equity_curve"] = equity_curve
        result["candles_processed"] = len(data.bt_5m)

    return result


# ── public entry points ───────────────────────────────────────────────────────

def run_backtest(symbol="BTCUSDC", from_ts=0, to_ts=0, capital=5_000.0,
                 tp_pct=0.001, atr_sl_mult=0.5, rsi_threshold=45.0,
                 tranche_usdc=1_000.0, tp_dollars=0.0) -> dict:
    started = time.time()
    data = fetch_backtest_data(symbol, from_ts, to_ts)
    if not data.bt_5m:
        return {"error": "No 5M candles in the requested period"}

    params = {
        "tp_pct": tp_pct, "tp_dollars": tp_dollars, "atr_sl_mult": atr_sl_mult,
        "rsi_threshold": rsi_threshold, "tranche_usdc": tranche_usdc,
    }
    result = simulate(data, params, capital, with_details=True)
    result.update({
        "symbol": symbol, "from_ts": from_ts, "to_ts": to_ts,
        "capital": capital, "elapsed_s": round(time.time() - started, 1),
    })
    return result


def run_optimization(symbol="BTCUSDC", from_ts=0, to_ts=0, capital=5_000.0,
                     grid: Optional[dict] = None, sort_by="total_pnl",
                     max_combos: int = 200) -> dict:
    """Grid search. `grid` maps param name → list of values to try."""
    started = time.time()

    grid = grid or {
        "tp_dollars":    [25, 50, 75, 100, 150, 200],
        "atr_sl_mult":   [0.5, 1.0, 1.5, 2.0],
        "rsi_threshold": [35, 45, 55],
    }

    # Build all combos
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    if len(combos) > max_combos:
        return {"error": f"Too many combos ({len(combos)}). Max {max_combos}. Narrow the grid."}

    log.info("optimize: fetching data once, then %d combos", len(combos))
    data = fetch_backtest_data(symbol, from_ts, to_ts)
    if not data.bt_5m:
        return {"error": "No 5M candles in the requested period"}

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        # tranche default if not in grid
        params.setdefault("tranche_usdc", 1000.0)
        try:
            r = simulate(data, params, capital, with_details=False)
            results.append(r)
        except Exception as e:
            log.warning("optimize: combo %s failed: %s", params, e)

    # Sort: higher is better for most metrics, lower for drawdown
    reverse = sort_by != "max_drawdown"
    results.sort(key=lambda r: r.get(sort_by, 0), reverse=reverse)

    return {
        "symbol": symbol, "from_ts": from_ts, "to_ts": to_ts, "capital": capital,
        "grid": grid, "sort_by": sort_by,
        "combos_tested": len(results),
        "candles_processed": len(data.bt_5m),
        "elapsed_s": round(time.time() - started, 1),
        "results": results,
    }


def _pair_ledger(ledger):
    buys, sells = {}, {}
    for e in ledger:
        if e.side == "BUY":
            buys[e.tranche_id] = e
        elif e.side == "SELL":
            sells[e.tranche_id] = e
    for tid, buy in buys.items():
        if tid in sells:
            yield buy, sells[tid]
