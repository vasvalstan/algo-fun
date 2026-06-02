"""
Async runner for PullbackStrategyV1 — runs inside live_visualizer.

Refresh cadence:
  Fills + 5M candle   every 10 s
  1H zones            every  5 min
  Daily + Weekly      every 30 min
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import List, Optional
from urllib.request import urlopen

from live_visualizer.strategies.pullback_v1 import Candle, LedgerEntry, PullbackStrategyV1, Tranche

log = logging.getLogger(__name__)

_BINANCE_REST = "https://data-api.binance.vision/api/v3"
_DATA_DIR     = os.getenv("PULLBACK_DATA_DIR", "/data")
# Separate state file per strategy so histories don't mix when switching.
_STATE_PATH   = os.path.join(_DATA_DIR, f"{os.getenv('VIS_STRATEGY', 'pullback').lower()}_v1_state.json")

# Module-level singleton
_strategy: Optional[PullbackStrategyV1] = None


def get_strategy() -> Optional[PullbackStrategyV1]:
    return _strategy


def get_state() -> dict:
    if _strategy is None:
        return {"status": "starting", "strategy": "pullback_v1"}
    return _strategy.get_state()


def get_history() -> dict:
    if _strategy is None:
        return {"rows": [], "total": 0}
    return _strategy.get_history()


def describe() -> dict:
    if _strategy is None:
        return {"title": "Starting...", "summary": "Strategy not yet initialized.",
                "params": {}, "sections": []}
    return _strategy.describe()


# ── persistence ───────────────────────────────────────────────────────────────

def _save(strat: PullbackStrategyV1) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        data = {
            "capital": strat.capital,
            "counter": strat._counter,
            "tranches": [t.to_dict(strat.current_price) for t in strat.tranches],
            "ledger":   [e.to_dict() for e in strat.ledger],
        }
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _STATE_PATH)
    except Exception as e:
        log.warning("pullback_v1: save failed: %s", e)


def _restore(strat: PullbackStrategyV1) -> int:
    """Load saved tranches + ledger. Returns number of tranches restored."""
    try:
        with open(_STATE_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        log.info("pullback_v1: no saved state found — starting fresh")
        return 0
    except Exception as e:
        log.warning("pullback_v1: restore failed: %s", e)
        return 0

    strat._counter = data.get("counter", 0)

    for td in data.get("tranches", []):
        t = Tranche(
            id=td["id"],
            state=td["state"],
            order_price=td["order_price"],
            entry_price=td["entry_price"],
            qty=td["qty"],
            tp_price=td["tp_price"],
            sl_price=td["sl_price"],
            entry_time=td["entry_time"],
            exit_time=td.get("exit_time"),
            exit_price=td.get("exit_price"),
            pnl=td.get("pnl", 0.0),
            reason=td.get("reason", ""),
        )
        strat.tranches.append(t)

    for ld in data.get("ledger", []):
        strat.ledger.append(LedgerEntry(
            id=ld["id"],
            tranche_id=ld["tranche_id"],
            side=ld["side"],
            price=ld["price"],
            qty=ld["qty"],
            usdc=ld["usdc"],
            timestamp=ld["timestamp"],
            pnl=ld["pnl"],
            reason=ld["reason"],
        ))

    active = sum(1 for t in strat.tranches if t.state in ("PENDING", "OPEN"))
    log.info("pullback_v1: restored %d tranches (%d active), %d ledger entries",
             len(strat.tranches), active, len(strat.ledger))
    return active


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch_klines(symbol: str, interval: str, limit: int) -> List[Candle]:
    url = f"{_BINANCE_REST}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    with urlopen(url, timeout=10) as resp:
        rows = json.loads(resp.read())
    return [
        Candle(
            time=int(row[0]) // 1000,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
    ]


def _fetch_price(symbol: str) -> float:
    url = f"{_BINANCE_REST}/ticker/price?symbol={symbol}"
    with urlopen(url, timeout=5) as resp:
        return float(json.loads(resp.read())["price"])


# ── main loop ─────────────────────────────────────────────────────────────────

async def run_pullback_loop(symbol: str = "BTCUSDC", capital: float = 5_000.0) -> None:
    global _strategy
    log.info("pullback_v1 runner starting  symbol=%s  capital=%.0f", symbol, capital)

    _strategy = PullbackStrategyV1(symbol=symbol, capital=capital)
    restored = _restore(_strategy)
    if restored:
        log.info("pullback_v1: resumed with %d active position(s)", restored)

    candles_5m:     List[Candle] = []
    candles_1h:     List[Candle] = []
    candles_daily:  List[Candle] = []
    candles_weekly: List[Candle] = []

    last_tick    = 0.0
    last_zones   = 0.0
    last_regime  = 0.0

    FILL_INTERVAL   = 10
    TICK_INTERVAL   = 60
    ZONE_INTERVAL   = 300
    REGIME_INTERVAL = 1800

    while True:
        try:
            now = time.time()

            price      = await asyncio.to_thread(_fetch_price, symbol)
            candles_5m = await asyncio.to_thread(_fetch_klines, symbol, "5m", 200)

            if now - last_regime > REGIME_INTERVAL or not candles_daily:
                candles_daily  = await asyncio.to_thread(_fetch_klines, symbol, "1d", 200)
                candles_weekly = await asyncio.to_thread(_fetch_klines, symbol, "1w", 52)
                last_regime = now

            if now - last_zones > ZONE_INTERVAL or not candles_1h:
                candles_1h  = await asyncio.to_thread(_fetch_klines, symbol, "1h", 200)
                last_zones  = now

            if now - last_tick > TICK_INTERVAL:
                msgs = _strategy.tick(candles_5m, candles_1h, candles_daily, candles_weekly, price)
                for m in msgs:
                    log.info(m)
                last_tick = now
                _save(_strategy)   # persist after every full tick
            else:
                _strategy.fast_check(price)
                _save(_strategy)   # persist after fill checks too

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("pullback_v1 error: %s", e)

        await asyncio.sleep(FILL_INTERVAL)


async def run_scalp_loop(symbol: str = "BTCUSDC", capital: float = 5_000.0) -> None:
    """1-minute mean-reversion scalper loop. Fast: ticks every 5s on 1m data."""
    from live_visualizer.strategies.scalp_v1 import ScalpStrategyV1
    global _strategy
    log.info("scalp_v1 runner starting  symbol=%s  capital=%.0f", symbol, capital)

    _strategy = ScalpStrategyV1(symbol=symbol, capital=capital)
    restored = _restore(_strategy)
    if restored:
        log.info("scalp_v1: resumed with %d active position(s)", restored)

    candles_1m: List[Candle] = []
    last_tick = 0.0
    TICK_INTERVAL = 15   # full signal eval every 15s
    POLL_INTERVAL = 5    # price + fill check every 5s

    while True:
        try:
            now = time.time()
            price = await asyncio.to_thread(_fetch_price, symbol)

            if now - last_tick > TICK_INTERVAL or not candles_1m:
                candles_1m = await asyncio.to_thread(_fetch_klines, symbol, "1m", 200)
                msgs = _strategy.tick(candles_1m, price)
                for m in msgs:
                    log.info(m)
                last_tick = now
            else:
                _strategy.fast_check(price)

            _save(_strategy)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("scalp_v1 error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)


def run_strategy_loop(symbol: str = "BTCUSDC", capital: float = 5_000.0):
    """Pick the active strategy loop from VIS_STRATEGY env var."""
    which = os.getenv("VIS_STRATEGY", "pullback").lower()
    if which == "scalp":
        log.info("Active strategy: SCALP (1m mean-reversion)")
        return run_scalp_loop(symbol, capital)
    log.info("Active strategy: PULLBACK (multi-TF trend)")
    return run_pullback_loop(symbol, capital)
