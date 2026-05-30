from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

import websockets

from live_visualizer.data_feed.candle_buffer import Candle, CandleBuffer

log = logging.getLogger(__name__)


@dataclass
class BookTicker:
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    updated_at: float = 0.0


class BinanceMarketFeed:
    """Public Binance stream consumer for live klines and best bid/ask."""

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        candle_buffer: CandleBuffer,
        ws_base: str = "wss://stream.binance.com:9443",
        reconnect_base_s: float = 1.0,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self.symbol = symbol.upper()
        self.interval = interval
        self.candle_buffer = candle_buffer
        self.ws_base = ws_base.rstrip("/")
        self.book = BookTicker()
        self.connected = False
        self.last_message_at = 0.0
        self.reconnect_attempts = 0
        self._stop = asyncio.Event()
        self._reconnect_base_s = reconnect_base_s
        self._reconnect_max_s = reconnect_max_s

    def seed_from_rest(self, limit: int = 500) -> int:
        params = urlencode({"symbol": self.symbol, "interval": self.interval, "limit": limit})
        url = f"https://data-api.binance.vision/api/v3/klines?{params}"
        with urlopen(url, timeout=10) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        candles = [
            Candle(
                time=int(row[0]) // 1000,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                closed=True,
            )
            for row in rows
        ]
        self.candle_buffer.seed(candles)
        return len(candles)

    def stream_url(self) -> str:
        lower = self.symbol.lower()
        streams = f"{lower}@kline_{self.interval}/{lower}@bookTicker"
        return f"{self.ws_base}/stream?{urlencode({'streams': streams})}"

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.reconnect_attempts += 1
                delay = min(self._reconnect_max_s, self._reconnect_base_s * (2 ** min(self.reconnect_attempts, 5)))
                log.warning("Binance feed disconnected: %s; reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stop.set()

    async def _run_once(self) -> None:
        async with websockets.connect(self.stream_url(), ping_interval=20, ping_timeout=20) as ws:
            self.connected = True
            self.reconnect_attempts = 0
            log.info("Connected Binance stream %s", self.stream_url())
            async for raw in ws:
                self.handle_message(json.loads(raw))
                if self._stop.is_set():
                    break

    def handle_message(self, message: dict[str, Any]) -> None:
        data = message.get("data", message)
        event = data.get("e")
        self.last_message_at = time.time()
        if event == "kline" or "k" in data:
            self.candle_buffer.update_from_kline(data)
            return
        if event == "bookTicker" or {"b", "a"}.issubset(data.keys()):
            self.book = BookTicker(
                bid=float(data.get("b", 0.0)),
                ask=float(data.get("a", 0.0)),
                bid_qty=float(data.get("B", 0.0)),
                ask_qty=float(data.get("A", 0.0)),
                updated_at=self.last_message_at,
            )

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_message_at": self.last_message_at,
            "reconnect_attempts": self.reconnect_attempts,
            "stream_url": self.stream_url(),
        }
