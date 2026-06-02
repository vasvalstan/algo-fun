"""
1-Minute Mean-Reversion Scalper v1
==================================
Buys oversold dips on the 1m chart, targets a small fixed-$ profit.

Signal (all on 1m):
  RSI(14) < oversold       (default 30)
  price <= lower Bollinger  (period 20, 2σ)
  not in a confirmed crash  (price > 24h-high × (1 − crash_pct))

Exits:
  TP  = entry + tp_dollars  (default $50)
  SL  = entry − sl_dollars  (default $100)   ← 1:2 risk

Stacking:
  Up to max_positions concurrent tranches, each its own TP/SL.
  New entry must be ≥ min_spacing below the nearest open entry.

Fee: 0 % (paper).  Reuses Tranche / LedgerEntry from pullback_v1.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import List, Optional

from live_visualizer.strategies.pullback_v1 import (
    Candle, LedgerEntry, Tranche, _rsi, _sma,
)

log = logging.getLogger(__name__)


def _stddev(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


class ScalpStrategyV1:
    NAME = "scalp_v1"

    def __init__(self, symbol: str = "BTCUSDC", capital: float = 5_000.0,
                 params: Optional[dict] = None):
        self.symbol  = symbol
        self.capital = capital
        p = params or {}

        # ── tunables (param > env > default) ──
        self.TRANCHE_USDC  = float(p.get("tranche_usdc",  os.getenv("SCALP_TRANCHE_USDC",  "1000")))
        self.TP_DOLLARS    = float(p.get("tp_dollars",    os.getenv("SCALP_TP_DOLLARS",    "50")))
        self.SL_DOLLARS    = float(p.get("sl_dollars",    os.getenv("SCALP_SL_DOLLARS",    "100")))
        self.RSI_OVERSOLD  = float(p.get("rsi_oversold",  os.getenv("SCALP_RSI_OVERSOLD",  "30")))
        self.BB_PERIOD     = int  (float(p.get("bb_period", os.getenv("SCALP_BB_PERIOD",   "20"))))
        self.BB_MULT       = float(p.get("bb_mult",       os.getenv("SCALP_BB_MULT",       "2.0")))
        self.MAX_POSITIONS = int  (float(p.get("max_positions", os.getenv("SCALP_MAX_POSITIONS", "5"))))
        self.MIN_SPACING   = float(p.get("min_spacing",   os.getenv("SCALP_MIN_SPACING",   "40")))
        self.CRASH_PCT     = float(p.get("crash_pct",     os.getenv("SCALP_CRASH_PCT",     "0.06")))

        # ── state ──
        self.tranches: List[Tranche]     = []
        self.ledger:   List[LedgerEntry] = []
        self._counter = 0

        self.current_price = 0.0
        self.rsi_1m   = 50.0
        self.bb_lower = 0.0
        self.bb_mid   = 0.0
        self.bb_upper = 0.0
        self.high_24h = 0.0
        self.is_crash = False
        self._log_lines: List[str] = []

    # ── capital ──────────────────────────────────────────────────────────────
    def _open_positions(self) -> List[Tranche]:
        return [t for t in self.tranches if t.state == "OPEN"]

    def _deployed(self) -> float:
        return sum(t.entry_price * t.qty for t in self._open_positions())

    def _free(self) -> float:
        return max(0.0, self.capital - self._deployed())

    # ── indicators ───────────────────────────────────────────────────────────
    def _update_indicators(self, candles_1m: List[Candle]) -> None:
        closes = [c.close for c in candles_1m]
        if len(closes) >= 15:
            self.rsi_1m = _rsi(closes, 14)
        if len(closes) >= self.BB_PERIOD:
            window = closes[-self.BB_PERIOD:]
            self.bb_mid   = sum(window) / self.BB_PERIOD
            sd            = _stddev(window)
            self.bb_lower = self.bb_mid - self.BB_MULT * sd
            self.bb_upper = self.bb_mid + self.BB_MULT * sd
        # 24h high from up to 1440 1m candles
        if candles_1m:
            self.high_24h = max(c.high for c in candles_1m[-1440:])
            self.is_crash = self.current_price < self.high_24h * (1 - self.CRASH_PCT)

    # ── lifecycle (mirrors pullback) ─────────────────────────────────────────
    def _open(self, price: float) -> Tranche:
        self._counter += 1
        tid = f"S{self._counter:04d}"
        qty = self.TRANCHE_USDC / price
        t = Tranche(
            id=tid, state="OPEN", order_price=price, entry_price=price, qty=qty,
            tp_price=price + self.TP_DOLLARS, sl_price=price - self.SL_DOLLARS,
            entry_time=int(time.time()),
        )
        self.tranches.append(t)
        self._counter += 1
        self.ledger.append(LedgerEntry(
            id=self._counter, tranche_id=tid, side="BUY", price=price,
            qty=qty, usdc=price * qty, timestamp=t.entry_time, pnl=0.0, reason="ENTRY",
        ))
        self._emit(f"[{tid}] BUY scalp @ {price:.2f}  TP={t.tp_price:.2f}  SL={t.sl_price:.2f}")
        return t

    def _close(self, t: Tranche, price: float, reason: str) -> None:
        pnl = (price - t.entry_price) * t.qty
        t.state = "CLOSED" if reason == "TP" else "STOPPED"
        t.exit_price = price
        t.exit_time  = int(time.time())
        t.pnl = pnl
        t.reason = reason
        self._counter += 1
        self.ledger.append(LedgerEntry(
            id=self._counter, tranche_id=t.id, side="SELL", price=price,
            qty=t.qty, usdc=price * t.qty, timestamp=t.exit_time,
            pnl=round(pnl, 4), reason=reason,
        ))
        self._emit(f"[{t.id}] SELL {reason} @ {price:.2f}  PnL={pnl:+.4f}")

    def _manage(self, price: float) -> None:
        for t in self._open_positions():
            if price >= t.tp_price:
                self._close(t, t.tp_price, "TP")
            elif price <= t.sl_price:
                self._close(t, t.sl_price, "SL")

    def _check_entry(self) -> bool:
        if self.is_crash:
            return False
        opens = self._open_positions()
        if len(opens) >= self.MAX_POSITIONS:
            return False
        if self._free() < self.TRANCHE_USDC:
            return False
        if self.rsi_1m >= self.RSI_OVERSOLD:
            return False
        if not self.bb_lower or self.current_price > self.bb_lower:
            return False
        # spacing: don't cluster near an existing open entry
        for t in opens:
            if abs(self.current_price - t.entry_price) < self.MIN_SPACING:
                return False
        return True

    # ── main tick ─────────────────────────────────────────────────────────────
    def tick(self, candles_1m: List[Candle], price: float) -> List[str]:
        self.current_price = price
        self._log_lines.clear()
        self._update_indicators(candles_1m)
        self._manage(price)

        if self._check_entry():
            self._open(price)
        else:
            # narrate why not
            if self.is_crash:
                self._emit(f"🚨 Crash guard — price {price:.0f} below 24h high {self.high_24h:.0f} − {self.CRASH_PCT*100:.0f}%")
            elif len(self._open_positions()) >= self.MAX_POSITIONS:
                self._emit(f"📦 Max {self.MAX_POSITIONS} positions open — waiting for exits")
            elif self.rsi_1m >= self.RSI_OVERSOLD:
                self._emit(f"⏳ RSI {self.rsi_1m:.1f} — waiting for < {self.RSI_OVERSOLD:.0f} (oversold dip)")
            elif self.bb_lower and price > self.bb_lower:
                d = (price - self.bb_lower) / price * 100
                self._emit(f"📉 Price {d:.2f}% above lower band ${self.bb_lower:.0f} — waiting for dip")
            else:
                self._emit("🔍 Scanning 1m for oversold dip...")
        return list(self._log_lines)

    def fast_check(self, price: float) -> None:
        self.current_price = price
        self._manage(price)

    def force_buy(self) -> str:
        price = self.current_price
        if price <= 0:
            return "error: price not available yet"
        if self._free() < self.TRANCHE_USDC:
            return f"error: not enough free capital (have ${self._free():.0f})"
        t = self._open(price)
        return f"ok: bought {t.qty:.6f} BTC @ ${price:.2f}  TP=${t.tp_price:.2f}  SL=${t.sl_price:.2f}"

    # ── history (same shape as pullback) ──────────────────────────────────────
    def get_history(self) -> dict:
        price = self.current_price
        rows = []
        for t in self.tranches:
            dur = int(t.exit_time - t.entry_time) if (t.exit_time and t.entry_time) else None
            rows.append({
                "id": t.id, "state": t.state,
                "entry_time": t.entry_time, "entry_price": round(t.entry_price, 2),
                "tp_price": round(t.tp_price, 2), "sl_price": round(t.sl_price, 2),
                "exit_time": t.exit_time,
                "exit_price": round(t.exit_price, 2) if t.exit_price else None,
                "qty": round(t.qty, 8), "size_usdc": round(t.entry_price * t.qty, 2),
                "pnl": round(t.pnl, 4) if t.state in ("CLOSED", "STOPPED")
                       else round(t.unrealized_pnl(price), 4),
                "result": t.reason or t.state, "duration_s": dur,
            })
        rows.sort(key=lambda r: r["entry_time"], reverse=True)
        closed = [r for r in rows if r["state"] in ("CLOSED", "STOPPED")]
        wins = [r for r in closed if r["pnl"] > 0]
        return {
            "rows": rows, "total": len(rows), "completed": len(closed),
            "open": sum(1 for r in rows if r["state"] == "OPEN"),
            "wins": len(wins), "losses": len(closed) - len(wins),
            "total_pnl": round(sum(r["pnl"] for r in closed), 4),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        }

    # ── state (same shape as pullback so UI/chart/persistence reuse) ─────────
    def get_state(self) -> dict:
        price = self.current_price
        opens = self._open_positions()
        pnl_real = sum(t.pnl for t in self.tranches if t.state in ("CLOSED", "STOPPED"))
        pnl_unr  = sum(t.unrealized_pnl(price) for t in opens)
        pos_qty  = sum(t.qty for t in opens)

        active_orders, sl_lines, open_bags = [], [], []
        for t in opens:
            active_orders.append({"id": f"{t.id}_tp", "side": "SELL",
                                  "price": t.tp_price, "qty": t.qty,
                                  "label": f"TP #{t.id}", "color": "#0ecb81"})
            sl_lines.append({"price": t.sl_price, "label": f"SL #{t.id}", "color": "#f6465d"})
            open_bags.append({
                "id": t.id, "entry_price": t.entry_price, "qty": round(t.qty, 8),
                "tp_price": t.tp_price, "sl_price": t.sl_price,
                "unrealized_pnl": round(t.unrealized_pnl(price), 4),
                "age_s": int(time.time()) - t.entry_time,
            })

        regime = "CRASH" if self.is_crash else "SCALP"
        return {
            "strategy": "scalp_v1", "symbol": self.symbol,
            "regime": regime, "daily_bias": "MEAN-REVERSION",
            "rsi_5m": round(self.rsi_1m, 1),   # reuse field name UI reads
            "atr_5m": 0.0,
            "capital_total": self.capital,
            "capital_free": round(self._free(), 4),
            "capital_deployed": round(self._deployed(), 4),
            "cash": round(self._free(), 4),
            # show BB bands as support/resistance lines
            "support_level": round(self.bb_lower, 2),
            "resistance_level": round(self.bb_upper, 2),
            "support_zones": ([{"low": round(self.bb_lower, 2),
                                "high": round(self.bb_mid, 2),
                                "mid": round((self.bb_lower+self.bb_mid)/2, 2),
                                "touches": 1, "strength": "moderate"}]
                              if self.bb_lower else []),
            "resistance_zones": [],
            "sl_lines": sl_lines,
            "active_orders": active_orders,
            "open_bags": open_bags,
            "position_qty": round(pos_qty, 8),
            "avg_entry_price": (sum(t.entry_price*t.qty for t in opens)/pos_qty
                                if pos_qty > 0 else 0),
            "pnl_realized": round(pnl_real, 4),
            "pnl_unrealized": round(pnl_unr, 4),
            "price": price, "last_price": price,
            "params": {
                "tp_dollars": self.TP_DOLLARS, "sl_dollars": self.SL_DOLLARS,
                "rsi_oversold": self.RSI_OVERSOLD, "max_positions": self.MAX_POSITIONS,
                "rsi_threshold": self.RSI_OVERSOLD, "atr_sl_mult": 0,
            },
            "bb_lower": round(self.bb_lower, 2),
            "bb_upper": round(self.bb_upper, 2),
            "high_24h": round(self.high_24h, 2),
            "ledger": [e.to_dict() for e in self.ledger[-100:]],
            "log": self._log_lines[-50:],
            "updated_at": time.time(),
        }

    def _emit(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log_lines.append(f"{ts}  {msg}")
        if len(self._log_lines) > 200:
            self._log_lines = self._log_lines[-200:]
        log.info("scalp_v1 %s", msg)
