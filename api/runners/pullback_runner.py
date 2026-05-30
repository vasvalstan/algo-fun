"""
Async runner for PullbackStrategyV1.

Refresh cadence
  5M candles + fills   every 10 s   (quick fills, current candle update)
  1H zones             every  5 min
  Daily + Weekly       every 30 min
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import List, Optional
from urllib.request import urlopen

from api.strategies.pullback_v1 import Candle, PullbackStrategyV1

log = logging.getLogger(__name__)

# ── paths ─────────────────────────────────────────────────────────────────────

_STATE_DIR  = os.getenv("LIFO_STATE_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data"))
_STATE_FILE = os.path.join(_STATE_DIR, "pullback_v1_state.json")

_BINANCE_REST = "https://data-api.binance.vision/api/v3"

# Singleton strategy instance (set in run_pullback_loop)
_strategy: Optional[PullbackStrategyV1] = None


def get_strategy() -> Optional[PullbackStrategyV1]:
    return _strategy


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
        data = json.loads(resp.read())
    return float(data["price"])


# ── state persistence ─────────────────────────────────────────────────────────

def _write_state(state: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _STATE_FILE)


def read_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"status": "not_started"}
    except Exception as e:
        log.warning("pullback state read error: %s", e)
        return {"status": "error"}


# ── runner ────────────────────────────────────────────────────────────────────

async def run_pullback_loop(symbol: str = "BTCUSDC", capital: float = 5_000.0) -> None:
    global _strategy
    log.info("pullback_v1 runner starting  symbol=%s  capital=%.0f", symbol, capital)

    _strategy = PullbackStrategyV1(symbol=symbol, capital=capital)

    # cache
    candles_5m:     List[Candle] = []
    candles_1h:     List[Candle] = []
    candles_daily:  List[Candle] = []
    candles_weekly: List[Candle] = []

    last_full_tick   = 0.0   # last time we ran strategy.tick()
    last_zone_fetch  = 0.0   # last 1H fetch
    last_regime_fetch = 0.0  # last daily+weekly fetch

    FILL_INTERVAL   = 10    # s
    TICK_INTERVAL   = 60    # s  (5M entry analysis)
    ZONE_INTERVAL   = 300   # s  (1H zone refresh)
    REGIME_INTERVAL = 1800  # s  (daily+weekly regime refresh)

    while True:
        try:
            now = time.time()

            # ── fetch prices + 5M candles every fill interval ──
            price = await asyncio.to_thread(_fetch_price, symbol)
            _strategy.current_price = price

            candles_5m = await asyncio.to_thread(_fetch_klines, symbol, "5m", 200)

            # ── refresh regime data (daily + weekly) ──
            if now - last_regime_fetch > REGIME_INTERVAL or not candles_daily:
                candles_daily  = await asyncio.to_thread(_fetch_klines, symbol, "1d", 200)
                candles_weekly = await asyncio.to_thread(_fetch_klines, symbol, "1w", 52)
                last_regime_fetch = now
                log.debug("pullback_v1: refreshed regime data")

            # ── refresh zone data (1H) ──
            if now - last_zone_fetch > ZONE_INTERVAL or not candles_1h:
                candles_1h    = await asyncio.to_thread(_fetch_klines, symbol, "1h", 200)
                last_zone_fetch = now
                log.debug("pullback_v1: refreshed 1H zone data")

            # ── full strategy tick ──
            if now - last_full_tick > TICK_INTERVAL:
                msgs = _strategy.tick(
                    candles_5m, candles_1h, candles_daily, candles_weekly, price
                )
                for m in msgs:
                    log.info(m)
                last_full_tick = now
            else:
                # quick fill check only
                _strategy.fast_check(price)

            # ── persist state ──
            _write_state(_strategy.get_state())

        except asyncio.CancelledError:
            log.info("pullback_v1 runner cancelled")
            raise
        except Exception as e:
            log.warning("pullback_v1 runner error: %s", e, exc_info=True)

        await asyncio.sleep(FILL_INTERVAL)
