from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

from pydantic import ValidationError

from live_visualizer.state.models import ActiveOrder, FillMarker, OpenBag, SharedBotState, now_iso

log = logging.getLogger(__name__)


class SharedStateReader:
    """Reads bot state from a file or HTTP endpoint and normalizes it."""

    def __init__(self, *, symbol: str, file_path: str = "", api_url: str = "") -> None:
        self.symbol = symbol.upper()
        self.file_path = Path(file_path).expanduser() if file_path else None
        self.api_url = api_url
        self.last_ok_at = 0.0
        self.last_error = ""
        self._fallback = SharedBotState(symbol=self.symbol)

    def read(self, last_price: float = 0.0) -> SharedBotState:
        raw: dict[str, Any] | None = None
        try:
            raw = self._read_raw()
            if raw is None:
                state = self._fallback.model_copy(update={"last_price": last_price, "updated_at": now_iso()})
            else:
                state = normalize_state(raw, default_symbol=self.symbol, last_price=last_price)
                self.last_ok_at = time.time()
                self.last_error = ""
                self._fallback = state
            return state
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Shared state read failed: %s", exc)
            return self._fallback.model_copy(update={"last_price": last_price, "updated_at": now_iso()})

    def _read_raw(self) -> Optional[dict[str, Any]]:
        if self.api_url:
            req = Request(self.api_url, headers={"User-Agent": "algo-fun-live-visualizer"})
            with urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        if self.file_path and self.file_path.exists():
            with self.file_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        return None

    def status(self) -> dict[str, Any]:
        return {
            "source": self.api_url or str(self.file_path or ""),
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
        }


def normalize_state(raw: dict[str, Any], *, default_symbol: str, last_price: float = 0.0) -> SharedBotState:
    if "last_price" in raw or "active_orders" in raw or "open_bags" in raw:
        data = dict(raw)
        data.setdefault("symbol", default_symbol)
        data.setdefault("last_price", last_price or float(data.get("price", 0.0) or 0.0))
        data.setdefault("raw", raw)
        try:
            return SharedBotState.model_validate(data)
        except ValidationError:
            log.debug("State did not match shared schema exactly; falling back to adapter", exc_info=True)

    price = float(raw.get("last_price", raw.get("price", last_price)) or 0.0)
    grid = raw.get("grid") or {}
    strategy = raw.get("strategy") or {}
    positions = raw.get("positions") or []
    cycles = raw.get("cycles") or []

    active_orders: list[ActiveOrder] = []
    resting = grid.get("resting_buy")
    if isinstance(resting, dict) and resting.get("price"):
        active_orders.append(ActiveOrder(
            id=str(resting.get("order_id", "")),
            side="BUY",
            price=float(resting["price"]),
            qty=float(resting.get("qty", 0.0) or 0.0),
            label=str(resting.get("kind", "resting buy")),
        ))

    open_bags: list[OpenBag] = []
    for pos in positions:
        if not isinstance(pos, dict) or not float(pos.get("entry_price", 0.0) or 0.0):
            continue
        bag = OpenBag(
            id=str(pos.get("slot_id", "")),
            entry_price=float(pos.get("entry_price", 0.0) or 0.0),
            qty=float(pos.get("slot_qty", 0.0) or 0.0),
            tp_price=_optional_float(pos.get("tp_price")),
            unrealized_pnl=float(pos.get("unrealized_usdt", 0.0) or 0.0),
            age_s=int(pos["age_s"]) if pos.get("age_s") is not None else None,
        )
        open_bags.append(bag)
        if bag.tp_price:
            active_orders.append(ActiveOrder(
                id=str(pos.get("sell_order_id", "")),
                side="SELL",
                price=bag.tp_price,
                qty=bag.qty,
                label=f"TP #{bag.id}",
            ))

    fills = [
        FillMarker(
            side="SELL",
            price=float(c.get("sell_price", 0.0) or 0.0),
            qty=0.0,
            ts=float(c.get("timestamp", 0.0) or 0.0),
            label=f"exit #{c.get('number', '')}",
        )
        for c in cycles[-30:]
        if isinstance(c, dict) and float(c.get("sell_price", 0.0) or 0.0)
    ]

    avg_entry = _weighted_avg(open_bags)
    support = float(raw.get("support_level", grid.get("resting_buy", {}).get("price", 0.0) if isinstance(grid.get("resting_buy"), dict) else 0.0) or 0.0)
    resistance_candidates = [b.tp_price for b in open_bags if b.tp_price]
    resistance = float(raw.get("resistance_level", min(resistance_candidates) if resistance_candidates else 0.0) or 0.0)

    return SharedBotState(
        symbol=str(raw.get("symbol", default_symbol)).replace("-", "").upper(),
        last_price=price,
        regime=str(raw.get("regime", strategy.get("macro_regime", grid.get("status", "UNKNOWN")))),
        support_level=support,
        resistance_level=resistance,
        active_orders=active_orders,
        open_bags=open_bags,
        fills=fills,
        cash=float(raw.get("cash", raw.get("session", {}).get("equity_usdt", 0.0) if isinstance(raw.get("session"), dict) else 0.0) or 0.0),
        position_qty=sum(b.qty for b in open_bags),
        avg_entry_price=avg_entry,
        pnl_realized=float(raw.get("pnl_realized", raw.get("alltime", {}).get("total_net_pnl", grid.get("total_pnl", 0.0)) if isinstance(raw.get("alltime"), dict) else grid.get("total_pnl", 0.0)) or 0.0),
        pnl_unrealized=sum(b.unrealized_pnl for b in open_bags),
        current_mode=str(strategy.get("market_mode", grid.get("status", ""))),
        gear=str(strategy.get("daily_bias", "")),
        pause_state=str(raw.get("pause_state", "")),
        risk_state=str(raw.get("risk_state", "")),
        grid_version=int(raw.get("grid_version", grid.get("closed_count", 0)) or 0),
        updated_at=now_iso(),
        raw=raw,
    )


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _weighted_avg(bags: list[OpenBag]) -> float:
    qty = sum(b.qty for b in bags)
    if qty <= 0:
        return 0.0
    return sum(b.entry_price * b.qty for b in bags) / qty
