from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    symbol: str = os.getenv("VIS_SYMBOL", os.getenv("BINANCE_SYMBOL", "BTCUSDC")).upper()
    interval: str = os.getenv("VIS_INTERVAL", "1m")
    candle_limit: int = int(os.getenv("VIS_CANDLE_LIMIT", "500"))
    refresh_interval_ms: int = int(os.getenv("VIS_REFRESH_INTERVAL_MS", "1000"))
    state_file_path: str = os.getenv("VIS_STATE_FILE", "")
    state_api_url: str = os.getenv("VIS_STATE_API_URL", "")
    binance_ws_base: str = os.getenv("VIS_BINANCE_WS_BASE", "wss://stream.binance.com:443")
    host: str = os.getenv("VIS_HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", os.getenv("VIS_PORT", "8080")))


settings = Settings()

