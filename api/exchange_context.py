"""
Exchange context — credential + URL bundles for Binance mainnet / testnet.

Every function in auth.py, trading.py, and market_data.py accepts an optional
``ctx`` parameter.  When omitted the legacy config.py globals are used
(backward compatible).  Pass a context to target a specific network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class BinanceContext:
    api_key: str
    api_secret: str
    base_url: str
    symbol: str
    recv_window: int = 5000


def mainnet_context(symbol: str | None = None) -> BinanceContext:
    return BinanceContext(
        api_key=os.getenv("BINANCE_API_KEY", ""),
        api_secret=os.getenv("BINANCE_API_SECRET", ""),
        base_url="https://api.binance.com",
        symbol=(symbol or os.getenv("SYMBOL", "BTCUSDT")).strip().upper(),
    )


def testnet_context(symbol: str | None = None) -> BinanceContext:
    return BinanceContext(
        api_key=os.getenv("BINANCE_TESTNET_API_KEY", ""),
        api_secret=os.getenv("BINANCE_TESTNET_API_SECRET", ""),
        base_url="https://testnet.binance.vision",
        symbol=(symbol or os.getenv("SYMBOL", "BTCUSDT")).strip().upper(),
    )
