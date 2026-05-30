from __future__ import annotations

from typing import Any

from live_visualizer.data_feed.candle_buffer import Candle
from live_visualizer.state.models import SharedBotState


def build_overlays(state: SharedBotState, candles: list[Candle]) -> dict[str, Any]:
    start = candles[0].time if candles else None
    end = candles[-1].time if candles else None
    lines = []
    if start and end:
        if state.support_level > 0:
            lines.append(_price_line("support", "Support", state.support_level, "#22c55e", start, end))
        if state.resistance_level > 0:
            lines.append(_price_line("resistance", "Resistance", state.resistance_level, "#ef4444", start, end))
        if state.avg_entry_price > 0:
            lines.append(_price_line("avg-entry", "Avg Entry", state.avg_entry_price, "#f59e0b", start, end))

    return {
        "price_lines": lines,
        "orders": [
            {
                "id": order.id,
                "side": order.side,
                "price": order.price,
                "qty": order.qty,
                "label": order.label or order.side,
                "color": "#22c55e" if order.side == "BUY" else "#f97316",
            }
            for order in state.active_orders
        ],
        "bags": [
            {
                "id": bag.id,
                "entry_price": bag.entry_price,
                "qty": bag.qty,
                "tp_price": bag.tp_price,
                "unrealized_pnl": bag.unrealized_pnl,
            }
            for bag in state.open_bags
        ],
        "fills": [
            {
                "side": fill.side,
                "price": fill.price,
                "time": fill.ts,
                "label": fill.label,
                "color": "#22c55e" if fill.side == "BUY" else "#ef4444",
            }
            for fill in state.fills
        ],
        "regime": regime_payload(state.regime),
    }


def regime_payload(regime: str) -> dict[str, str]:
    key = (regime or "UNKNOWN").upper()
    color = "#64748b"
    if any(word in key for word in ("BULL", "UP", "HUNT", "RANGE")):
        color = "#22c55e"
    if any(word in key for word in ("BEAR", "DOWN", "RISK", "PAUSE")):
        color = "#ef4444"
    if any(word in key for word in ("ACTIVE", "HOLD", "LIFO")):
        color = "#f59e0b"
    return {"label": regime or "UNKNOWN", "color": color}


def _price_line(
    id_: str,
    label: str,
    price: float,
    color: str,
    start: int,
    end: int,
) -> dict[str, Any]:
    return {
        "id": id_,
        "label": label,
        "price": price,
        "color": color,
        "series": [
            {"time": start, "value": price},
            {"time": end, "value": price},
        ],
    }

