from __future__ import annotations

from live_visualizer.data_feed.candle_buffer import Candle
from live_visualizer.overlays.builders import build_overlays
from live_visualizer.state.models import SharedBotState


def snapshot_payload(
    *,
    symbol: str,
    interval: str,
    candles: list[Candle],
    bot_state: SharedBotState,
    book: dict,
    feed_status: dict,
    state_status: dict,
) -> dict:
    latest = candles[-1] if candles else None
    last_price = bot_state.last_price or (latest.close if latest else 0.0)
    return {
        "symbol": symbol,
        "interval": interval,
        "last_price": last_price,
        "candles": [c.to_lightweight() for c in candles],
        "volume": [{"time": c.time, "value": c.volume, "color": "#1f9d55" if c.close >= c.open else "#b91c1c"} for c in candles],
        "book": book,
        "bot_state": bot_state.model_dump(exclude={"raw"}),
        "overlays": build_overlays(bot_state, candles),
        "feed": feed_status,
        "state_source": state_status,
    }

