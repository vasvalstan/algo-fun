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
import time
from typing import List, Optional
from urllib.request import urlopen

from live_visualizer.strategies.pullback_v1 import Candle, PullbackStrategyV1

log = logging.getLogger(__name__)

_BINANCE_REST = "https://data-api.binance.vision/api/v3"

# Module-level singleton
_strategy: Optional[PullbackStrategyV1] = None


def get_strategy() -> Optional[PullbackStrategyV1]:
    return _strategy


def get_state() -> dict:
    if _strategy is None:
        return {"status": "starting", "strategy": "pullback_v1"}
    return _strategy.get_state()


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
            else:
                _strategy.fast_check(price)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("pullback_v1 error: %s", e)

        await asyncio.sleep(FILL_INTERVAL)
