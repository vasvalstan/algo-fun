from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = False

    def to_lightweight(self) -> dict[str, float | int]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }


class CandleBuffer:
    def __init__(self, limit: int = 500) -> None:
        self.limit = limit
        self._candles: OrderedDict[int, Candle] = OrderedDict()
        self._lock = RLock()

    def update_from_kline(self, payload: dict[str, Any]) -> Candle:
        k = payload.get("k", payload)
        candle = Candle(
            time=int(k["t"]) // 1000,
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k.get("v", 0.0)),
            closed=bool(k.get("x", False)),
        )
        with self._lock:
            self._candles[candle.time] = candle
            self._candles.move_to_end(candle.time)
            while len(self._candles) > self.limit:
                self._candles.popitem(last=False)
        return candle

    def seed(self, candles: list[Candle]) -> None:
        with self._lock:
            self._candles.clear()
            for candle in candles[-self.limit:]:
                self._candles[candle.time] = candle

    def latest(self) -> Candle | None:
        with self._lock:
            if not self._candles:
                return None
            return next(reversed(self._candles.values()))

    def as_list(self) -> list[Candle]:
        with self._lock:
            return list(self._candles.values())

