"""
Trend-Filtered Pullback Strategy v1
====================================
Multi-timeframe analysis:
  Weekly       → big regime  (BULL / BEAR / SIDEWAYS / CRASH)
  Daily        → bias        (BULLISH / SIDEWAYS / BEARISH)
  1H           → support + resistance zones
  5M           → precise entry / exit

Capital  : 5 000 USDC, tranches of 1 000 USDC each
TP       : +0.15 % from entry
SL       : support_low − ATR(14)×0.5
Fee      : 0 % (paper)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────── data classes ────────────────────────────────

@dataclass
class Candle:
    time: int          # unix seconds (open time)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SupportZone:
    low: float
    high: float
    touches: int
    last_touch_time: int
    strength: str = "weak"   # weak / moderate / strong

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def to_dict(self) -> dict:
        return {
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "mid": round(self.mid, 2),
            "touches": self.touches,
            "strength": self.strength,
        }


@dataclass
class Tranche:
    id: str
    state: str          # PENDING | OPEN | CLOSED | STOPPED | CANCELLED
    order_price: float
    entry_price: float
    qty: float
    tp_price: float
    sl_price: float
    entry_time: int
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    reason: str = ""

    def unrealized_pnl(self, price: float) -> float:
        if self.state != "OPEN":
            return 0.0
        return (price - self.entry_price) * self.qty

    def to_dict(self, price: float) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "order_price": round(self.order_price, 2),
            "entry_price": round(self.entry_price, 2),
            "qty": round(self.qty, 8),
            "tp_price": round(self.tp_price, 2),
            "sl_price": round(self.sl_price, 2),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 2) if self.exit_price else None,
            "pnl": round(self.pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl(price), 4),
            "reason": self.reason,
        }


@dataclass
class LedgerEntry:
    id: int
    tranche_id: str
    side: str           # BUY | SELL
    price: float
    qty: float
    usdc: float
    timestamp: int
    pnl: float
    reason: str         # ENTRY | TP | SL | CANCELLED

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────── helpers ─────────────────────────────────────

def _sma(data: List[float], period: int) -> List[float]:
    out = []
    for i in range(len(data)):
        if i < period - 1:
            out.append(float("nan"))
        else:
            out.append(sum(data[i - period + 1 : i + 1]) / period)
    return out


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low  - candles[i - 1].close),
        )
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs))


# ─────────────────────────────── strategy ────────────────────────────────────

class PullbackStrategyV1:
    PIVOT_BARS   = 3
    ZONE_TOL_PCT = 0.003
    ATR_PERIOD   = 14

    def __init__(self, symbol: str = "BTCUSDC", capital: float = 5_000.0,
                 params: Optional[dict] = None):
        self.symbol  = symbol
        self.capital = capital
        # ── params: explicit override > env var > default ──
        p = params or {}
        self.TRANCHE_USDC  = float(p.get("tranche_usdc",  os.getenv("PULLBACK_TRANCHE_USDC",  "1000")))
        self.TP_PCT        = float(p.get("tp_pct",        os.getenv("PULLBACK_TP_PCT",        "0.001")))
        self.TP_DOLLARS    = float(p.get("tp_dollars",    os.getenv("PULLBACK_TP_DOLLARS",    "0")))
        self.ATR_SL_MULT   = float(p.get("atr_sl_mult",   os.getenv("PULLBACK_ATR_SL_MULT",   "0.5")))
        self.RSI_THRESHOLD = float(p.get("rsi_threshold", os.getenv("PULLBACK_RSI_THRESHOLD", "45")))

        self.tranches: List[Tranche]       = []
        self.ledger:   List[LedgerEntry]   = []
        self._counter  = 0

        self.regime      = "UNKNOWN"
        self.daily_bias  = "UNKNOWN"
        self.support_zones:    List[SupportZone] = []
        self.resistance_zones: List[SupportZone] = []

        self.current_price = 0.0
        self.atr_5m        = 0.0
        self.rsi_5m        = 50.0
        self._log_lines: List[str] = []

    # ── capital ──────────────────────────────────────────────────────────────

    def _deployed(self) -> float:
        return sum(
            t.order_price * t.qty
            for t in self.tranches
            if t.state in ("PENDING", "OPEN")
        )

    def _free(self) -> float:
        return max(0.0, self.capital - self._deployed())

    # ── regime ───────────────────────────────────────────────────────────────

    def _detect_regime(
        self,
        weekly: List[Candle],
        daily:  List[Candle],
    ) -> Tuple[str, str]:
        regime     = "SIDEWAYS"
        daily_bias = "SIDEWAYS"

        if len(weekly) >= 20:
            wc   = [c.close for c in weekly]
            ma20 = _sma(wc, 20)
            ma50 = _sma(wc, min(50, len(wc)))
            cur, m20, m50 = wc[-1], ma20[-1], ma50[-1]

            # crash: price down >8 % from 4-week high
            hi4 = max(c.high for c in weekly[-4:])
            if cur < hi4 * 0.92:
                regime = "CRASH"
            elif m50 == m50 and cur > m20 and m20 > m50:   # nan-safe
                regime = "BULL"
            elif m50 == m50 and cur < m20 and m20 < m50:
                regime = "BEAR"
            else:
                regime = "SIDEWAYS"

        if len(daily) >= 20:
            dc  = [c.close for c in daily]
            m20 = _sma(dc, 20)
            cur, m, pm = dc[-1], m20[-1], m20[-2] if len(m20) >= 2 else m20[-1]
            if cur > m and m > pm:
                daily_bias = "BULLISH"
            elif cur < m and m < pm:
                daily_bias = "BEARISH"
            else:
                daily_bias = "SIDEWAYS"

        return regime, daily_bias

    # ── zones ─────────────────────────────────────────────────────────────────

    def _find_zones(
        self, hourly: List[Candle]
    ) -> Tuple[List[SupportZone], List[SupportZone]]:
        n = self.PIVOT_BARS
        supports:    List[SupportZone] = []
        resistances: List[SupportZone] = []

        for i in range(n, len(hourly) - n):
            c = hourly[i]

            # pivot low → support
            if all(c.low <= hourly[i - j].low for j in range(1, n + 1)) and \
               all(c.low <= hourly[i + j].low for j in range(1, n + 1)):
                self._merge_zone(supports, c.low, c.low * 1.002, c.time, is_support=True)

            # pivot high → resistance
            if all(c.high >= hourly[i - j].high for j in range(1, n + 1)) and \
               all(c.high >= hourly[i + j].high for j in range(1, n + 1)):
                self._merge_zone(resistances, c.high * 0.998, c.high, c.time, is_support=False)

        for z in supports + resistances:
            z.strength = "strong" if z.touches >= 3 else "moderate" if z.touches >= 2 else "weak"

        cur = self.current_price or 1.0
        sup = sorted([z for z in supports    if z.low  < cur], key=lambda z: abs(z.low  - cur))
        res = sorted([z for z in resistances if z.high > cur], key=lambda z: abs(z.high - cur))
        return sup[:10], res[:5]

    @staticmethod
    def _merge_zone(
        zones: List[SupportZone],
        low: float, high: float, ts: int,
        is_support: bool,
    ) -> None:
        anchor = low if is_support else high
        for z in zones:
            ref = z.low if is_support else z.high
            if abs(anchor - ref) / ref < PullbackStrategyV1.ZONE_TOL_PCT:
                z.low  = min(z.low,  low)
                z.high = max(z.high, high)
                z.touches += 1
                z.last_touch_time = max(z.last_touch_time, ts)
                return
        zones.append(SupportZone(low=low, high=high, touches=1, last_touch_time=ts))

    # ── entry / exit ─────────────────────────────────────────────────────────

    def _check_entry(self, candles_5m: List[Candle]) -> Optional[dict]:
        """Return entry spec dict or None."""
        if self.regime in ("CRASH", "BEAR"):
            return None
        if self.regime == "SIDEWAYS" and self.daily_bias == "BEARISH":
            return None
        if self._free() < self.TRANCHE_USDC:
            return None
        if len(candles_5m) < 30:
            return None

        closes = [c.close for c in candles_5m]
        self.rsi_5m = _rsi(closes, 14)

        if self.rsi_5m >= self.RSI_THRESHOLD:
            return None

        # momentum flip: last close > prev close
        if candles_5m[-1].close <= candles_5m[-2].close:
            return None

        price = self.current_price
        for zone in self.support_zones:
            lower = zone.low  * 0.997
            upper = zone.high * 1.003
            if not (lower <= price <= upper):
                continue

            # don't double-enter same zone
            if any(
                t.state in ("PENDING", "OPEN")
                and abs(t.order_price - zone.low) / zone.low < 0.005
                for t in self.tranches
            ):
                continue

            buy   = zone.low
            tp    = self._calc_tp(buy)
            sl    = zone.low - self.atr_5m * self.ATR_SL_MULT
            return {"buy_price": buy, "tp_price": tp, "sl_price": sl, "zone": zone}

        return None

    def _calc_tp(self, entry_price: float) -> float:
        """Calculate TP price — fixed $$ distance or % based."""
        if self.TP_DOLLARS > 0:
            return entry_price + self.TP_DOLLARS
        return entry_price * (1.0 + self.TP_PCT)

    # ── forced buy (bypasses all regime/RSI gates) ───────────────────────────

    def force_buy(self) -> str:
        """Open a tranche immediately at current price, bypassing all filters."""
        price = self.current_price
        if price <= 0:
            return "error: price not available yet"
        if self._free() < self.TRANCHE_USDC:
            return f"error: not enough free capital (have ${self._free():.0f}, need $1,000)"

        # Use nearest support zone for SL/TP reference, fall back to ATR
        sl_ref = price - max(self.atr_5m * self.ATR_SL_MULT, price * 0.003)
        tp     = self._calc_tp(price)

        if self.support_zones:
            nearest = min(self.support_zones, key=lambda z: abs(z.low - price))
            sl_ref  = nearest.low - self.atr_5m * self.ATR_SL_MULT

        spec = {"buy_price": price, "tp_price": tp, "sl_price": sl_ref, "zone": None}
        t = self._open_tranche(spec)
        # Force-fill immediately at current price
        self._fill(t, price)
        return f"ok: bought {t.qty:.6f} BTC @ ${price:.2f}  TP=${tp:.2f}  SL=${sl_ref:.2f}"

    # ── tranche lifecycle ─────────────────────────────────────────────────────

    def _open_tranche(self, spec: dict) -> Tranche:
        self._counter += 1
        tid = f"T{self._counter:04d}"
        bp  = spec["buy_price"]
        qty = self.TRANCHE_USDC / bp
        t   = Tranche(
            id=tid, state="PENDING",
            order_price=bp, entry_price=bp, qty=qty,
            tp_price=spec["tp_price"], sl_price=spec["sl_price"],
            entry_time=int(time.time()),
        )
        self.tranches.append(t)
        self._emit(f"[{tid}] PENDING BUY limit @ {bp:.2f}  TP={t.tp_price:.2f}  SL={t.sl_price:.2f}")
        return t

    def _fill(self, t: Tranche, price: float) -> None:
        t.state       = "OPEN"
        t.entry_price = price
        t.entry_time  = int(time.time())
        self._counter += 1
        self.ledger.append(LedgerEntry(
            id=self._counter, tranche_id=t.id,
            side="BUY", price=price, qty=t.qty,
            usdc=price * t.qty, timestamp=t.entry_time,
            pnl=0.0, reason="ENTRY",
        ))
        self._emit(f"[{t.id}] FILLED BUY @ {price:.2f}  qty={t.qty:.6f} BTC")

    def _close(self, t: Tranche, price: float, reason: str) -> None:
        pnl          = (price - t.entry_price) * t.qty
        t.state      = "CLOSED" if reason == "TP" else "STOPPED"
        t.exit_price = price
        t.exit_time  = int(time.time())
        t.pnl        = pnl
        t.reason     = reason
        self._counter += 1
        self.ledger.append(LedgerEntry(
            id=self._counter, tranche_id=t.id,
            side="SELL", price=price, qty=t.qty,
            usdc=price * t.qty, timestamp=t.exit_time,
            pnl=round(pnl, 4), reason=reason,
        ))
        self._emit(f"[{t.id}] SELL {reason} @ {price:.2f}  PnL={pnl:+.4f} USDC")

    def _cancel(self, t: Tranche, reason: str) -> None:
        t.state  = "CANCELLED"
        t.reason = reason
        self._emit(f"[{t.id}] CANCELLED — {reason}")

    def _manage(self, price: float) -> None:
        for t in self.tranches:
            if t.state == "PENDING":
                if price <= t.order_price:
                    self._fill(t, t.order_price)
                elif price <= t.sl_price:
                    self._cancel(t, "SL_BREAK_BEFORE_FILL")
            elif t.state == "OPEN":
                if price >= t.tp_price:
                    self._close(t, t.tp_price, "TP")
                elif price <= t.sl_price:
                    self._close(t, t.sl_price, "SL")

    # ── main tick ─────────────────────────────────────────────────────────────

    def tick(
        self,
        candles_5m:  List[Candle],
        candles_1h:  List[Candle],
        candles_daily:  List[Candle],
        candles_weekly: List[Candle],
        price: float,
    ) -> List[str]:
        self.current_price = price
        self._log_lines.clear()

        if candles_5m:
            self.atr_5m = _atr(candles_5m, self.ATR_PERIOD)

        self.regime, self.daily_bias = self._detect_regime(candles_weekly, candles_daily)
        tp_label = f"${self.TP_DOLLARS:.0f}" if self.TP_DOLLARS > 0 else f"{self.TP_PCT*100:.2f}%"
        self._emit(f"📊 Regime: {self.regime} · Bias: {self.daily_bias} · RSI: {round(self.rsi_5m,1)} · TP: {tp_label}")

        if candles_1h:
            self.support_zones, self.resistance_zones = self._find_zones(candles_1h)
            if self.support_zones:
                z = self.support_zones[0]
                self._emit(f"🗺 Nearest support: ${z.low:,.2f}–${z.high:,.2f} ({z.strength}, {z.touches}x tested)")

        self._manage(price)

        spec = self._check_entry(candles_5m)
        if spec:
            self._open_tranche(spec)
        else:
            # Explain why not entering
            if self.regime in ("CRASH", "BEAR"):
                self._emit(f"⛔ No entry — regime is {self.regime}")
            elif self.regime == "SIDEWAYS" and self.daily_bias == "BEARISH":
                self._emit("⛔ No entry — sideways + bearish bias")
            elif self._free() < self.TRANCHE_USDC:
                self._emit(f"💰 No capital — ${self._free():.0f} free of $1,000 needed")
            elif self.rsi_5m >= self.RSI_THRESHOLD:
                self._emit(f"⏳ Waiting — RSI {self.rsi_5m:.1f} too high (need < {self.RSI_THRESHOLD})")
            elif self.support_zones:
                z = self.support_zones[0]
                dist = abs(price - z.low) / price * 100
                self._emit(f"📍 Price ${price:,.2f} — {dist:.2f}% from nearest support ${z.low:,.2f}")
            else:
                self._emit("🔍 Scanning for support zones...")

        return list(self._log_lines)

    # ── quick fill-check (called more frequently than full tick) ──────────────

    def fast_check(self, price: float) -> None:
        self.current_price = price
        self._manage(price)

    # ── state export ──────────────────────────────────────────────────────────

    def describe(self) -> dict:
        tp = (f"${self.TP_DOLLARS:.0f} above entry" if self.TP_DOLLARS > 0
              else f"{self.TP_PCT*100:.2f}% above entry")
        return {
            "name": "pullback_v1",
            "title": "Trend-Filtered Pullback",
            "summary": (
                "Only buys pullbacks (dips) inside a confirmed uptrend, at valid support "
                "zones. Uses multi-timeframe analysis to avoid catching falling knives — it "
                "stays out entirely during bear markets and crashes."
            ),
            "params": {
                "Take Profit":    tp,
                "Stop Loss":      f"support low − ATR×{self.ATR_SL_MULT}",
                "Trade Size":     f"${self.TRANCHE_USDC:.0f} per tranche",
                "Entry RSI gate": f"5-minute RSI below {self.RSI_THRESHOLD:.0f}",
            },
            "sections": [
                {"heading": "🌍 Regime Filter (decides IF it may trade)", "rules": [
                    "Weekly trend sets the big regime: BULL / BEAR / SIDEWAYS / CRASH",
                    "Daily trend sets the bias: BULLISH / SIDEWAYS / BEARISH",
                    "BULL → look for buys · SIDEWAYS → only near strong support",
                    "BEAR or CRASH → no new entries at all (stand aside)",
                ]},
                {"heading": "📥 Entry Rules (all must be true)", "rules": [
                    "Regime must allow trading (not CRASH/BEAR)",
                    "Price must be inside a valid 1H support zone (from pivot lows)",
                    f"5-minute RSI(14) below {self.RSI_THRESHOLD:.0f} (pullback is oversold)",
                    "Momentum flip: last 5m candle closes higher than the prior (dip ending)",
                    "Not already holding a position in that same zone",
                    f"At least ${self.TRANCHE_USDC:.0f} free capital",
                ]},
                {"heading": "🎯 Exit Rules", "rules": [
                    f"Take profit: {tp}",
                    f"Stop loss: just below support (support low − ATR×{self.ATR_SL_MULT})",
                    "If a candle closes below support, the idea is invalid — exit immediately",
                    "If support breaks, cancel any pending buy orders",
                ]},
                {"heading": "🛡️ Risk Rules", "rules": [
                    "Never average down forever · never add into a clear downtrend",
                    "Support break = trade idea invalid = exit",
                    "Maker-style limit entries at the support level",
                    "0% fees (paper trading)",
                    f"Total capital: ${self.capital:.0f}",
                ]},
            ],
        }

    def get_history(self) -> dict:
        """One consolidated record per tranche with every detail."""
        price = self.current_price
        rows = []
        for t in self.tranches:
            duration = None
            if t.exit_time and t.entry_time:
                duration = int(t.exit_time - t.entry_time)
            rows.append({
                "id":           t.id,
                "state":        t.state,
                "entry_time":   t.entry_time,
                "entry_price":  round(t.entry_price, 2),
                "tp_price":     round(t.tp_price, 2),
                "sl_price":     round(t.sl_price, 2),
                "exit_time":    t.exit_time,
                "exit_price":   round(t.exit_price, 2) if t.exit_price else None,
                "qty":          round(t.qty, 8),
                "size_usdc":    round(t.entry_price * t.qty, 2),
                "pnl":          round(t.pnl, 4) if t.state in ("CLOSED", "STOPPED") else
                                round(t.unrealized_pnl(price), 4),
                "result":       t.reason or t.state,
                "duration_s":   duration,
            })
        rows.sort(key=lambda r: r["entry_time"], reverse=True)

        closed = [r for r in rows if r["state"] in ("CLOSED", "STOPPED")]
        wins   = [r for r in closed if r["pnl"] > 0]
        return {
            "rows":          rows,
            "total":         len(rows),
            "completed":     len(closed),
            "open":          sum(1 for r in rows if r["state"] == "OPEN"),
            "wins":          len(wins),
            "losses":        len(closed) - len(wins),
            "total_pnl":     round(sum(r["pnl"] for r in closed), 4),
            "win_rate":      round(len(wins) / len(closed) * 100, 1) if closed else 0,
        }

    def get_state(self) -> dict:
        price = self.current_price
        open_t    = [t for t in self.tranches if t.state == "OPEN"]
        pending_t = [t for t in self.tranches if t.state == "PENDING"]

        pnl_real  = sum(t.pnl for t in self.tranches if t.state in ("CLOSED", "STOPPED"))
        pnl_unr   = sum(t.unrealized_pnl(price) for t in open_t)
        pos_qty   = sum(t.qty for t in open_t)

        # active_orders — pending buys + TP sells — for visualizer overlays
        active_orders = []
        for t in pending_t:
            active_orders.append({
                "id": t.id, "side": "BUY",
                "price": t.order_price, "qty": t.qty,
                "label": f"limit #{t.id}", "color": "#3b82f6",
            })
        for t in open_t:
            active_orders.append({
                "id": f"{t.id}_tp", "side": "SELL",
                "price": t.tp_price, "qty": t.qty,
                "label": f"TP #{t.id}", "color": "#0ecb81",
            })

        sl_lines = [
            {"price": t.sl_price, "label": f"SL #{t.id}", "color": "#f6465d"}
            for t in open_t
        ]

        open_bags = [
            {
                "id": t.id,
                "entry_price": t.entry_price,
                "qty": round(t.qty, 8),
                "tp_price": t.tp_price,
                "sl_price": t.sl_price,
                "unrealized_pnl": round(t.unrealized_pnl(price), 4),
                "age_s": int(time.time()) - t.entry_time,
            }
            for t in open_t
        ]

        return {
            # identity
            "strategy": "pullback_v1",
            "symbol": self.symbol,
            "params": {
                "tp_pct": self.TP_PCT,
                "atr_sl_mult": self.ATR_SL_MULT,
                "rsi_threshold": self.RSI_THRESHOLD,
                "tranche_usdc": self.TRANCHE_USDC,
            },
            # regime
            "regime": self.regime,
            "daily_bias": self.daily_bias,
            "rsi_5m": round(self.rsi_5m, 1),
            "atr_5m": round(self.atr_5m, 2),
            # capital
            "capital_total": self.capital,
            "capital_free": round(self._free(), 4),
            "capital_deployed": round(self._deployed(), 4),
            "cash": round(self._free(), 4),
            # zones (for visualizer shading)
            "support_zones":    [z.to_dict() for z in self.support_zones[:8]],
            "resistance_zones": [z.to_dict() for z in self.resistance_zones[:5]],
            "support_level":    self.support_zones[0].low    if self.support_zones    else 0,
            "resistance_level": self.resistance_zones[0].high if self.resistance_zones else 0,
            "sl_lines": sl_lines,
            # positions
            "tranches":      [t.to_dict(price) for t in self.tranches[-30:]],
            "open_bags":     open_bags,
            "active_orders": active_orders,
            "position_qty":  round(pos_qty, 8),
            "avg_entry_price": (
                sum(t.entry_price * t.qty for t in open_t) / pos_qty
                if pos_qty > 0 else 0
            ),
            # pnl
            "pnl_realized":   round(pnl_real, 4),
            "pnl_unrealized": round(pnl_unr,  4),
            # price
            "price":      price,
            "last_price": price,
            # ledger + log
            "ledger": [e.to_dict() for e in self.ledger[-100:]],
            "log":    self._log_lines[-50:],
            # meta
            "updated_at": time.time(),
        }

    def _emit(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        self._log_lines.append(line)
        if len(self._log_lines) > 200:
            self._log_lines = self._log_lines[-200:]
        log.info("pullback_v1 %s", msg)
