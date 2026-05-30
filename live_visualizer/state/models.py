from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ActiveOrder(BaseModel):
    id: str = ""
    side: Literal["BUY", "SELL"] = "BUY"
    price: float
    qty: float = 0.0
    status: str = "OPEN"
    label: str = ""


class OpenBag(BaseModel):
    id: str = ""
    entry_price: float
    qty: float = 0.0
    tp_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    age_s: Optional[int] = None


class FillMarker(BaseModel):
    side: Literal["BUY", "SELL"] = "BUY"
    price: float
    qty: float = 0.0
    ts: Optional[float] = None
    label: str = ""


class SharedBotState(BaseModel):
    symbol: str = "BTCUSDC"
    last_price: float = 0.0
    regime: str = "UNKNOWN"
    support_level: float = 0.0
    resistance_level: float = 0.0
    active_orders: list[ActiveOrder] = Field(default_factory=list)
    open_bags: list[OpenBag] = Field(default_factory=list)
    fills: list[FillMarker] = Field(default_factory=list)
    cash: float = 0.0
    position_qty: float = 0.0
    avg_entry_price: float = 0.0
    pnl_realized: float = 0.0
    pnl_unrealized: float = 0.0
    current_mode: str = ""
    gear: str = ""
    pause_state: str = ""
    risk_state: str = ""
    grid_version: int = 0
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw: dict[str, Any] = Field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
