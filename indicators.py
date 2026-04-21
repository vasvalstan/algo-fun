"""
Top-down multi-timeframe strategy engine for BTCUSDT Spot.

Hierarchy (highest to lowest):
  Monthly — macro market regime classification
  Weekly  — macro trend confirmation via EMA 50/200 + market structure (HH/HL)
  Daily   — trading bias via EMA 20/50/200, RSI 14, ATR 14
  4H      — intermediate trend confirmation
  1H      — regime timing, UP / DOWN / WATCH mode
  5M      — execution timing, pullback detection for maker entries

The engine classifies the macro regime first, then cascades downward.
Lower timeframes are only used for precise entry/exit when the higher
timeframes support the trade direction.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from api.strategy_params import (
    BreakoutParams,
    MeanReversionParams,
    V2AdaptiveParams,
    default_breakout,
    default_mean_reversion,
    default_v2_adaptive,
)


# ── Candle representation ────────────────────────────────────────────


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    is_closed: bool


def candle_from_dict(d: dict) -> Candle:
    return Candle(
        open_time=d["open_time"],
        open=float(d["open"]),
        high=float(d["high"]),
        low=float(d["low"]),
        close=float(d["close"]),
        volume=float(d["volume"]),
        quote_volume=float(d["quote_volume"]),
        trades=d["trades"],
        is_closed=d["is_closed"],
    )


# ── Strategy engine ─────────────────────────────────────────────────


class StrategyEngine:
    """Top-down multi-timeframe indicator engine."""

    def __init__(self) -> None:
        self.candles: dict[str, deque[Candle]] = {
            "5m": deque(maxlen=200),   # ~16 hours
            "15m": deque(maxlen=200),  # ~2 days (Bollinger Bands for mean reversion)
            "1h": deque(maxlen=300),   # ~12 days  (EMA 200)
            "4h": deque(maxlen=100),   # ~16 days
            "1d": deque(maxlen=300),   # ~10 months (EMA 200)
            "1w": deque(maxlen=300),   # ~5.7 years (EMA 200)
            "1M": deque(maxlen=120),   # ~10 years  (EMA 50; 200 not possible)
        }
        self._recent_trades: deque[dict] = deque(maxlen=3000)

    # ── Ingest ───────────────────────────────────────────────────────

    def update_candle(self, interval: str, kline: dict) -> None:
        if interval not in self.candles:
            return
        bar = candle_from_dict(kline)
        buf = self.candles[interval]
        if buf and buf[-1].open_time == bar.open_time:
            buf[-1] = bar
        else:
            buf.append(bar)

    def update_trade(self, trade: dict) -> None:
        self._recent_trades.append({
            "timestamp": trade["timestamp"],
            "price": float(trade["price"]),
            "qty": float(trade["quantity"]),
            "quote_qty": float(trade["price"]) * float(trade["quantity"]),
            "side": trade["side"],
        })

    # ── Core indicators ──────────────────────────────────────────────

    def _closed(self, interval: str) -> list[Candle]:
        return [c for c in self.candles.get(interval, []) if c.is_closed]

    def ema(self, interval: str, period: int) -> float | None:
        closes = [c.close for c in self._closed(interval)]
        return _ema(closes, period) if len(closes) >= period else None

    def sma(self, interval: str, period: int) -> float | None:
        closes = [c.close for c in self._closed(interval)]
        return sum(closes[-period:]) / period if len(closes) >= period else None

    def rsi(self, interval: str = "1h", period: int = 14) -> float | None:
        closes = [c.close for c in self._closed(interval)]
        return _rsi(closes, period) if len(closes) >= period + 1 else None

    def atr(self, interval: str = "1h", period: int = 14) -> float | None:
        candles = self._closed(interval)
        return _atr(candles, period) if len(candles) >= period + 1 else None

    def last_price(self, interval: str = "1h") -> float | None:
        closed = self._closed(interval)
        return closed[-1].close if closed else None

    # ── V2 composite indicators ──────────────────────────────────────

    def get_keltner_channels(
        self, interval: str = "5m", ema_period: int = 20, atr_mult: float = 1.5
    ) -> dict[str, Any] | None:
        """Keltner Channels = EMA ± (ATR × multiplier)."""
        mid = self.ema(interval, ema_period)
        a = self.atr(interval, 14)
        if mid is None or a is None:
            return None
        price = self.last_price(interval)
        return {
            "upper": round(mid + a * atr_mult, 2),
            "middle": round(mid, 2),
            "lower": round(mid - a * atr_mult, 2),
            "atr": round(a, 2),
            "price": round(price, 2) if price else None,
            "below_lower": price is not None and price <= mid - a * atr_mult,
            "above_upper": price is not None and price >= mid + a * atr_mult,
        }

    def get_macd(
        self, interval: str = "1h", fast: int = 12, slow: int = 26, signal: int = 9
    ) -> dict[str, Any] | None:
        """MACD line, signal line, and histogram."""
        closes = [c.close for c in self._closed(interval)]
        if len(closes) < slow + signal:
            return None
        fast_ema = _ema(closes, fast)
        slow_ema = _ema(closes, slow)
        if fast_ema is None or slow_ema is None:
            return None
        macd_line = fast_ema - slow_ema
        # Build MACD series for signal EMA
        macd_series = []
        for i in range(slow, len(closes) + 1):
            fe = _ema(closes[:i], fast)
            se = _ema(closes[:i], slow)
            if fe is not None and se is not None:
                macd_series.append(fe - se)
        sig = _ema(macd_series, signal) if len(macd_series) >= signal else None
        hist = macd_line - sig if sig is not None else None
        # Determine slope (rising/falling) from last 2 histogram values
        prev_hist = None
        if len(macd_series) >= signal + 1 and sig is not None:
            prev_macd = macd_series[-2] if len(macd_series) >= 2 else macd_line
            prev_sig = _ema(macd_series[:-1], signal) if len(macd_series[:-1]) >= signal else sig
            if prev_sig is not None:
                prev_hist = prev_macd - prev_sig
        histogram_rising = hist is not None and prev_hist is not None and hist > prev_hist
        return {
            "macd_line": round(macd_line, 4),
            "signal_line": round(sig, 4) if sig is not None else None,
            "histogram": round(hist, 4) if hist is not None else None,
            "histogram_rising": histogram_rising,
        }

    def get_vwap(self, interval: str = "1h", lookback: int = 24) -> float | None:
        """Volume-weighted average price over recent candles."""
        closed = self._closed(interval)
        if len(closed) < lookback:
            return None
        recent = closed[-lookback:]
        total_vq = sum(c.volume * (c.high + c.low + c.close) / 3 for c in recent)
        total_vol = sum(c.volume for c in recent)
        if total_vol == 0:
            return None
        return round(total_vq / total_vol, 2)

    def get_bollinger_bands(
        self,
        interval: str = "15m",
        period: int = 20,
        std_dev: float = 2.5,
        rsi_oversold_reversal: float = 25.0,
    ) -> dict[str, Any] | None:
        """Bollinger Bands = SMA ± (StdDev × multiplier)."""
        closes = [c.close for c in self._closed(interval)]
        if len(closes) < period:
            return None
        window = closes[-period:]
        sma_val = sum(window) / period
        variance = sum((p - sma_val) ** 2 for p in window) / period
        std = variance ** 0.5
        price = closes[-1]
        lower = sma_val - std_dev * std
        upper = sma_val + std_dev * std
        rsi_val = self.rsi(interval, 14)
        # Check if prev candle pierced lower band and current closed back inside
        prev_pierced_lower = len(closes) >= 2 and closes[-2] < lower
        closed_back_inside = price >= lower
        rev_rsi = rsi_val is not None and rsi_val < rsi_oversold_reversal
        return {
            "upper": round(upper, 2),
            "middle": round(sma_val, 2),
            "lower": round(lower, 2),
            "std": round(std, 2),
            "price": round(price, 2),
            "rsi": round(rsi_val, 1) if rsi_val is not None else None,
            "below_lower": price < lower,
            "above_upper": price > upper,
            "prev_pierced_lower": prev_pierced_lower,
            "closed_back_inside": closed_back_inside,
            "reversal_signal": prev_pierced_lower and closed_back_inside and rev_rsi,
        }

    def get_bollinger_bandwidth(
        self, interval: str = "1h", period: int = 20, std_dev: float = 2.0
    ) -> dict[str, Any] | None:
        """Bollinger Bandwidth = (upper - lower) / middle. Measures compression."""
        bb = self.get_bollinger_bands(interval, period, std_dev, rsi_oversold_reversal=25.0)
        if bb is None:
            return None
        mid = bb["middle"]
        if mid == 0:
            return None
        bw = (bb["upper"] - bb["lower"]) / mid
        # Check if bandwidth is at 3-day low (72 candles at 1h)
        closes = [c.close for c in self._closed(interval)]
        is_compressed = False
        if len(closes) >= 72:
            bw_history = []
            for i in range(72, len(closes) + 1):
                w = closes[max(0, i - period):i]
                if len(w) < period:
                    continue
                s = sum(w) / period
                v = sum((p - s) ** 2 for p in w) / period
                sd = v ** 0.5
                if s > 0:
                    bw_history.append(((s + 2 * sd) - (s - 2 * sd)) / s)
            if bw_history and bw <= min(bw_history):
                is_compressed = True
        return {
            "bandwidth": round(bw, 6),
            "is_3day_low": is_compressed,
        }

    def get_volume_sma(
        self, interval: str = "5m", period: int = 20, surge_ratio: float = 3.0
    ) -> dict[str, Any] | None:
        """Volume SMA and current candle volume for breakout confirmation."""
        closed = self._closed(interval)
        if len(closed) < period + 1:
            return None
        vols = [c.volume for c in closed[-(period + 1):-1]]
        vol_sma = sum(vols) / period
        current_vol = closed[-1].volume
        ratio = current_vol / vol_sma if vol_sma > 0 else 0
        return {
            "vol_sma": round(vol_sma, 2),
            "current_vol": round(current_vol, 2),
            "ratio": round(ratio, 2),
            "is_breakout_volume": ratio >= surge_ratio,
        }

    def get_cvd(self, window_seconds: int = 60) -> dict[str, Any]:
        """Cumulative Volume Delta from recent trades — toxic flow detection."""
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - window_seconds * 1000
        window = [t for t in self._recent_trades if t["timestamp"] >= cutoff]
        if not window:
            return {"status": "no_data", "sell_ratio": 0, "toxic": False}
        buy_vol = sum(t["quote_qty"] for t in window if t["side"] == "BUY")
        sell_vol = sum(t["quote_qty"] for t in window if t["side"] == "SELL")
        total = buy_vol + sell_vol
        sell_ratio = sell_vol / total if total > 0 else 0
        # Toxic if sell volume is > 3x the average
        avg_sell = sell_vol / len(window) if window else 0
        recent_avg = total / len(window) / 2 if window else 1
        toxic = sell_vol > 0 and (avg_sell > recent_avg * 3)
        decaying = False
        if len(window) >= 4:
            half = len(window) // 2
            first_sells = sum(t["quote_qty"] for t in window[:half] if t["side"] == "SELL")
            second_sells = sum(t["quote_qty"] for t in window[half:] if t["side"] == "SELL")
            decaying = second_sells < first_sells * 0.7
        return {
            "status": "active",
            "buy_vol": round(buy_vol, 2),
            "sell_vol": round(sell_vol, 2),
            "sell_ratio": round(sell_ratio, 3),
            "toxic": toxic,
            "seller_exhaustion": decaying,
        }

    # ══════════════════════════════════════════════════════════════════
    # V2 STRATEGY ANALYSES
    # ══════════════════════════════════════════════════════════════════

    def get_v2_full_analysis(self, params: V2AdaptiveParams | None = None) -> dict[str, Any]:
        """V2 Adaptive Volatility-Scaled Maker Scalper — 4-layer cascade.

        Layer 1: Daily macro bias (Daily EMA 20/50 + VWAP)
        Layer 2: Intraday momentum (1H VWAP + MACD histogram)
        Layer 3: Dynamic pullback (5M Keltner Channels)
        Layer 4: Toxic flow gate (1M CVD)
        """
        p = params if params is not None else default_v2_adaptive()
        le = p.layers_enabled
        reasons: list[str] = []
        layers: list[dict] = []

        atr_5m = self.atr("5m", p.atr_period_5m)

        # ── Layer 1: Macro Bias (Daily) ──
        daily_vwap = self.get_vwap("1d", p.daily_vwap_lookback)
        e20_d = self.ema("1d", p.daily_ema_fast)
        e50_d = self.ema("1d", p.daily_ema_slow)
        price_d = self.last_price("1d")

        l1_pass = False
        l1_detail = "Insufficient daily data"
        if price_d and daily_vwap and e20_d and e50_d:
            above_vwap = price_d > daily_vwap
            ema_aligned = e20_d > e50_d
            l1_pass = above_vwap and ema_aligned
            if l1_pass:
                l1_detail = f"Price ${price_d:,.0f} > VWAP ${daily_vwap:,.0f}, EMA20 > EMA50 — bullish bias"
            elif not above_vwap:
                l1_detail = f"Price ${price_d:,.0f} BELOW VWAP ${daily_vwap:,.0f} — no bullish bias"
            else:
                l1_detail = f"EMA20 ${e20_d:,.0f} < EMA50 ${e50_d:,.0f} — trend weakening"
        l1_on = le.get("l1_macro", True)
        if l1_on:
            reasons.append(f"L1 Macro: {'PASS' if l1_pass else 'FAIL'} — {l1_detail}")
        else:
            l1_pass = True
            l1_detail = f"Skipped — {l1_detail}"
            reasons.append("L1 Macro: skipped (layer disabled)")

        layers.append({
            "name": "Macro Bias (Daily)",
            "status": "PASS" if l1_pass else "FAIL",
            "detail": l1_detail,
            "icon": "✅" if l1_pass else "❌",
            "indicators": {"vwap": daily_vwap, "ema20": e20_d, "ema50": e50_d},
        })

        # ── Layer 2: Intraday Momentum (1H) ──
        vwap_1h = self.get_vwap("1h", p.vwap_1h_lookback)
        macd_1h = self.get_macd("1h")
        price_1h = self.last_price("1h")

        l2_pass = False
        l2_detail = "Insufficient 1H data"
        if price_1h and vwap_1h and macd_1h:
            above_vwap = price_1h > vwap_1h
            hist_rising = macd_1h.get("histogram_rising", False)
            l2_pass = above_vwap and hist_rising
            if l2_pass:
                l2_detail = "Price > 1H VWAP, MACD histogram rising — momentum accelerating"
            elif not above_vwap:
                l2_detail = f"Price ${price_1h:,.0f} below 1H VWAP ${vwap_1h:,.0f}"
            else:
                l2_detail = "MACD histogram not rising — momentum decaying"
        l2_on = le.get("l2_momentum", True)
        if l2_on:
            reasons.append(f"L2 Momentum: {'PASS' if l2_pass else 'FAIL'} — {l2_detail}")
        else:
            l2_pass = True
            l2_detail = f"Skipped — {l2_detail}"
            reasons.append("L2 Momentum: skipped (layer disabled)")

        layers.append({
            "name": "Intraday Momentum (1H)",
            "status": "PASS" if l2_pass else ("FAIL" if price_1h else "WAITING"),
            "detail": l2_detail,
            "icon": "✅" if l2_pass else ("❌" if price_1h else "⏳"),
            "indicators": {"vwap_1h": vwap_1h, "macd": macd_1h},
        })

        # ── Layer 3: Dynamic Pullback (5M Keltner) ──
        kc = self.get_keltner_channels("5m", p.keltner_ema_period, p.keltner_atr_mult)

        l3_pass = False
        l3_detail = "Insufficient 5M data"
        entry_price = None
        near_eps = p.keltner_near_lower_pct
        if kc and kc["price"]:
            at_lower = kc["below_lower"]
            near_lower = kc["price"] is not None and kc["lower"] is not None and (
                (kc["price"] - kc["lower"]) / kc["lower"] * 100 < near_eps if kc["lower"] > 0 else False
            )
            l3_pass = at_lower or near_lower
            if l3_pass:
                entry_price = round(kc["price"] * p.entry_price_multiplier, 2)
                l3_detail = f"Price ${kc['price']:,.0f} at/near lower Keltner ${kc['lower']:,.0f} — pullback zone"
            else:
                gap_pct = ((kc["price"] - kc["lower"]) / kc["lower"] * 100) if kc["lower"] > 0 else 0
                l3_detail = f"Price ${kc['price']:,.0f} is {gap_pct:.2f}% above Keltner lower ${kc['lower']:,.0f}"
        l3_on = le.get("l3_keltner", True)
        if l3_on:
            reasons.append(f"L3 Pullback: {'PASS' if l3_pass else 'WAIT'} — {l3_detail}")
        else:
            l3_pass = True
            l3_detail = f"Skipped — {l3_detail}"
            reasons.append("L3 Keltner: skipped (layer disabled)")

        layers.append({
            "name": "Dynamic Pullback (5M Keltner)",
            "status": "PASS" if l3_pass else ("WAITING" if kc else "NOT_READY"),
            "detail": l3_detail,
            "icon": "✅" if l3_pass else "⏳",
            "indicators": {"keltner": kc},
        })

        # ── Layer 4: Toxic Flow Gate (CVD) ──
        l4_status = "NOT_CHECKED"
        l4_detail = "Checked only when pullback triggers"
        l4_pass = True
        cvd = None

        l4_on = le.get("l4_cvd", True)
        prior_for_cvd = l1_pass and l2_pass and l3_pass
        if not l4_on:
            l4_status = "PASS"
            l4_detail = "Skipped (layer disabled)"
            reasons.append("L4 CVD: skipped (layer disabled)")
        elif prior_for_cvd:
            cvd = self.get_cvd(p.cvd_window_seconds)
            toxic = cvd.get("toxic", False)
            exhaustion = cvd.get("seller_exhaustion", False)
            l4_pass = not toxic or exhaustion
            l4_status = "PASS" if l4_pass else "FAIL"
            if l4_pass:
                l4_detail = "No toxic sell flow detected — safe to enter"
                if exhaustion:
                    l4_detail = "Seller exhaustion detected — good entry timing"
            else:
                l4_detail = f"TOXIC sell flow! Sell ratio {cvd.get('sell_ratio', 0):.0%} — blocking entry"
            reasons.append(f"L4 Toxic Flow: {'PASS' if l4_pass else 'BLOCK'} — {l4_detail}")

        layers.append({
            "name": "Toxic Flow Gate (1M CVD)",
            "status": l4_status,
            "detail": l4_detail,
            "icon": "✅" if l4_status == "PASS" else ("❌" if l4_status == "FAIL" else "⬜"),
            "indicators": {"cvd": cvd},
        })

        all_pass = l1_pass and l2_pass and l3_pass and l4_pass
        action = "ENTRY_READY" if all_pass else "WAIT"
        if not l1_pass:
            action = "NO_TRADE"

        tp_price = None
        sl_price = None
        if entry_price and atr_5m:
            sl_price = round(entry_price - p.atr_sl_mult * atr_5m, 2)
            tp_price = round(entry_price + p.atr_tp_mult * atr_5m, 2)

        return {
            "strategy": "v2_adaptive",
            "timestamp": int(time.time()),
            "action": action,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "atr_5m": round(atr_5m, 2) if atr_5m else None,
            "tp_type": "dynamic_atr",
            "layers": layers,
            "reasons": reasons,
            "indicators": {
                "daily_vwap": daily_vwap,
                "ema20_d": round(e20_d, 2) if e20_d else None,
                "ema50_d": round(e50_d, 2) if e50_d else None,
                "vwap_1h": vwap_1h,
                "macd_1h": macd_1h,
                "keltner_5m": kc,
                "cvd": cvd,
                "atr_5m": round(atr_5m, 2) if atr_5m else None,
            },
        }

    def get_mean_reversion_analysis(self, params: MeanReversionParams | None = None) -> dict[str, Any]:
        """Alt 1: Bollinger Mean Reversion — fades extremes in sideways markets.

        Entry: Price pierces lower BB AND RSI < threshold, then closes back inside.
        Exit:  TP at middle BB (SMA). SL at ATR× mult below pierce candle low.
        """
        p = params if params is not None else default_mean_reversion()
        le = p.layers_enabled
        reasons: list[str] = []
        bb = self.get_bollinger_bands(
            "15m", p.bb_period, p.bb_std_dev, rsi_oversold_reversal=p.rsi_oversold
        )
        atr_15m = self.atr("15m", p.atr_period_15m)

        if bb is None:
            return {
                "strategy": "mean_reversion",
                "action": "WAIT",
                "reasons": ["Need ≥20 15M candles for Bollinger Bands"],
                "indicators": {},
                "layers": [{"name": "Bollinger Setup", "status": "NOT_READY", "detail": "Warming up", "icon": "⏳"}],
            }

        entry_price = None
        tp_price = None
        sl_price = None

        reversal = bb.get("reversal_signal", False)
        rsi_val = bb.get("rsi")
        price = bb["price"]

        layers = []

        macro = self.get_macro_regime()
        regime = macro.get("regime", "UNKNOWN")
        regime_ok = regime in set(p.regime_allow)
        regime_pass = regime_ok if le.get("l1_regime", True) else True
        if not le.get("l1_regime", True):
            reasons.append("L1 Regime: skipped (layer disabled)")
        layers.append({
            "name": "Market Regime",
            "status": "PASS" if regime_pass else "FAIL",
            "detail": (
                f"Skipped — regime {regime}" if not le.get("l1_regime", True)
                else f"Regime: {regime} — {'Good for mean reversion' if regime_ok else 'Trending market, risky for mean reversion'}"
            ),
            "icon": "✅" if regime_pass else "⚠️",
        })

        at_extreme = bb["below_lower"] or bb.get("prev_pierced_lower", False)
        extreme_pass = at_extreme if le.get("l2_bb_extreme", True) else True
        if not le.get("l2_bb_extreme", True):
            reasons.append("L2 Bollinger extreme: skipped (layer disabled)")
        layers.append({
            "name": "Bollinger Extreme",
            "status": "PASS" if extreme_pass else "WAITING",
            "detail": (
                "Skipped — " if not le.get("l2_bb_extreme", True) else ""
            ) + f"Price ${price:,.0f} vs Lower BB ${bb['lower']:,.0f}" + (f", RSI {rsi_val:.0f}" if rsi_val else ""),
            "icon": "✅" if extreme_pass else "⏳",
        })

        rev_pass = reversal if le.get("l3_reversal", True) else True
        if not le.get("l3_reversal", True):
            reasons.append("L3 Reversal: skipped (layer disabled)")
        layers.append({
            "name": "Reversal Confirmation",
            "status": "PASS" if rev_pass else "WAITING",
            "detail": (
                "Skipped — " if not le.get("l3_reversal", True) else ""
            ) + ("Price pierced lower BB and closed back inside" if reversal else "Waiting for candle to close back inside band"),
            "icon": "✅" if rev_pass else "⏳",
        })

        g1 = regime_ok if le.get("l1_regime", True) else True
        g2 = at_extreme if le.get("l2_bb_extreme", True) else True
        g3 = reversal if le.get("l3_reversal", True) else True

        action = "WAIT"
        if not g1:
            action = "NO_TRADE"
            reasons.append(f"Regime {regime} — mean reversion not suitable in trending markets")
        elif g1 and g2 and g3:
            action = "ENTRY_READY"
            entry_price = round(price * 0.9998, 2)
            tp_price = bb["middle"]
            if atr_15m:
                closed_15m = self._closed("15m")
                if closed_15m:
                    lb = min(len(closed_15m), max(1, p.swing_low_lookback))
                    recent_low = min(c.low for c in closed_15m[-lb:])
                    sl_price = round(recent_low - p.atr_sl_mult * atr_15m, 2)
            reasons.append(f"Bollinger reversal confirmed — entry at ${entry_price:,.0f}, TP at SMA ${tp_price:,.0f}")
        elif le.get("l3_reversal", True) and at_extreme and not reversal:
            action = "WAIT_FOR_CLOSE"
            reasons.append("Price at lower BB extreme — waiting for candle to close back inside")
        else:
            gap_pct = ((price - bb["lower"]) / bb["lower"] * 100) if bb["lower"] > 0 else 0
            reasons.append(f"Price {gap_pct:.1f}% above lower BB — no extreme yet")

        return {
            "strategy": "mean_reversion",
            "timestamp": int(time.time()),
            "action": action,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_type": "bollinger_mid",
            "layers": layers,
            "reasons": reasons,
            "indicators": {
                "bollinger": bb,
                "atr_15m": round(atr_15m, 2) if atr_15m else None,
                "regime": regime,
            },
        }

    def get_breakout_analysis(self, params: BreakoutParams | None = None) -> dict[str, Any]:
        """Alt 2: Volatility Breakout Expansion — catches explosive moves.

        Entry: BB bandwidth at 3-day low + price breaks N×1H high + volume surge vs SMA.
        Exit:  No fixed TP. Trailing stop below previous 5M candle low.
        """
        p = params if params is not None else default_breakout()
        le = p.layers_enabled
        reasons: list[str] = []
        layers: list[dict] = []

        bw = self.get_bollinger_bandwidth(
            p.bandwidth_interval, p.bandwidth_bb_period, p.bandwidth_bb_std
        )
        vol = self.get_volume_sma(
            p.volume_interval, p.volume_sma_period, p.volume_surge_ratio
        )

        compressed = bool(bw and bw.get("is_3day_low", False))
        closed_1h = self._closed("1h")
        price = self.last_price("5m")
        broke_high = False
        recent_high = None
        lb = max(2, min(len(closed_1h), p.high_lookback_bars))
        if len(closed_1h) >= 2 and price:
            recent_high = max(c.high for c in closed_1h[-lb:])
            broke_high = price > recent_high

        vol_surge = bool(vol and vol.get("is_breakout_volume", False))

        c_pass = compressed if le.get("l1_compression", True) else True
        h_pass = broke_high if le.get("l2_high_break", True) else True
        v_pass = vol_surge if le.get("l3_volume", True) else True

        if bw:
            layers.append({
                "name": "Volatility Compression",
                "status": "PASS" if c_pass else "WAITING",
                "detail": (
                    ("Skipped — " if not le.get("l1_compression", True) else "")
                    + f"BB Bandwidth {bw['bandwidth']:.4f}"
                    + (" — 3-day LOW, compression detected!" if compressed else " — not at extreme low")
                ),
                "icon": "✅" if c_pass else "⏳",
            })
        else:
            layers.append({
                "name": "Volatility Compression",
                "status": "NOT_READY",
                "detail": "Warming up bandwidth data",
                "icon": "⏳",
            })

        if len(closed_1h) >= 2 and price and recent_high is not None:
            layers.append({
                "name": "1H High Breakout",
                "status": "PASS" if h_pass else "WAITING",
                "detail": (
                    ("Skipped — " if not le.get("l2_high_break", True) else "")
                    + f"Price ${price:,.0f} {'>' if broke_high else '<='} {lb}×1H high ${recent_high:,.0f}"
                ),
                "icon": "✅" if h_pass else "⏳",
            })
        else:
            layers.append({
                "name": "1H High Breakout",
                "status": "NOT_READY",
                "detail": "Insufficient 1H data",
                "icon": "⏳",
            })

        if vol:
            layers.append({
                "name": "Volume Surge",
                "status": "PASS" if v_pass else "WAITING",
                "detail": (
                    ("Skipped — " if not le.get("l3_volume", True) else "")
                    + f"Current vol {vol['ratio']:.0f}× average (need ≥{p.volume_surge_ratio:.1f}×)"
                    + (" — BREAKOUT volume!" if vol_surge else "")
                ),
                "icon": "✅" if v_pass else "⏳",
            })
        else:
            layers.append({
                "name": "Volume Surge",
                "status": "NOT_READY",
                "detail": "Warming up volume data",
                "icon": "⏳",
            })

        g1 = compressed if le.get("l1_compression", True) else True
        g2 = broke_high if le.get("l2_high_break", True) else True
        g3 = vol_surge if le.get("l3_volume", True) else True

        action = "WAIT"
        entry_price = None
        sl_price = None
        if g1 and g2 and g3 and price:
            action = "ENTRY_READY"
            entry_price = round(price, 2)
            closed_5m = self._closed("5m")
            if closed_5m and len(closed_5m) >= 2:
                sl_price = round(closed_5m[-2].low, 2)
            reasons.append(f"BREAKOUT! Compression + high break + volume surge — entry at ${entry_price:,.0f}")
        elif compressed or (not le.get("l1_compression", True) and bw):
            reasons.append("Compression detected — watching for breakout above 1H high with volume")
        else:
            reasons.append("No volatility compression yet")

        return {
            "strategy": "breakout",
            "timestamp": int(time.time()),
            "action": action,
            "entry_price": entry_price,
            "tp_price": None,
            "sl_price": sl_price,
            "tp_type": "trailing",
            "layers": layers,
            "reasons": reasons,
            "indicators": {
                "bandwidth": bw,
                "volume": vol,
                "recent_1h_high": recent_high,
            },
        }

    # ══════════════════════════════════════════════════════════════════
    # LAYER 1 — Macro regime  (Monthly + Weekly)
    # ══════════════════════════════════════════════════════════════════

    def get_macro_regime(self) -> dict[str, Any]:
        """
        Classify the macro market regime from monthly and weekly data.

        Returns one of:
          BULL_RUN          — clear uptrend on weekly/monthly
          HEALTHY_PULLBACK  — weekly bullish structure intact but price pulling back
          SIDEWAYS          — no clear direction
          BEARISH           — downtrend on weekly/monthly
          UNKNOWN           — insufficient data
        """
        weekly = self._analyse_timeframe("1w", ema_periods=(50, 200), swing_lookback=3)
        monthly = self._analyse_timeframe("1M", ema_periods=(20, 50), swing_lookback=2)

        w_ok = weekly.get("status") == "ready"
        m_ok = monthly.get("status") == "ready"

        if not w_ok:
            return {
                "regime": "UNKNOWN",
                "reason": weekly.get("reason", "Weekly data not ready"),
                "weekly": weekly,
                "monthly": monthly,
            }

        w_struct = weekly.get("structure", "unknown")
        w_above_slow = weekly.get("price_above_slow_ema", False)
        w_fast_above_slow = weekly.get("fast_above_slow", False)

        # ── BULL_RUN ─────────────────────────────────────────────────
        if (w_struct in ("bullish", "leaning_bullish")
                and w_above_slow
                and w_fast_above_slow):
            return {
                "regime": "BULL_RUN",
                "reason": "Weekly structure bullish, price above EMA, EMAs aligned",
                "weekly": weekly,
                "monthly": monthly,
            }

        # ── HEALTHY_PULLBACK ─────────────────────────────────────────
        if (w_struct in ("bullish", "leaning_bullish")
                and w_fast_above_slow
                and not w_above_slow):
            return {
                "regime": "HEALTHY_PULLBACK",
                "reason": "Weekly structure bullish but price pulled below slow EMA — normal correction",
                "weekly": weekly,
                "monthly": monthly,
            }

        # ── BEARISH ──────────────────────────────────────────────────
        if (w_struct in ("bearish", "leaning_bearish")
                and not w_above_slow
                and not w_fast_above_slow):
            return {
                "regime": "BEARISH",
                "reason": "Weekly structure bearish, price and EMAs aligned down",
                "weekly": weekly,
                "monthly": monthly,
            }

        # ── SIDEWAYS ─────────────────────────────────────────────────
        return {
            "regime": "SIDEWAYS",
            "reason": "No clear weekly trend — mixed structure and EMAs",
            "weekly": weekly,
            "monthly": monthly,
        }

    # ══════════════════════════════════════════════════════════════════
    # LAYER 2 — Daily bias
    # ══════════════════════════════════════════════════════════════════

    def get_daily_bias(self) -> dict[str, Any]:
        """
        Determine the daily chart trading bias.

        Returns one of:
          BULLISH_ALIGNED       — daily EMAs stacked bullish, RSI > 50
          WEAK_BUT_MACRO_BULL   — daily pulling back but macro still bullish
          BEARISH               — daily EMAs bearish
          NEUTRAL               — mixed signals
          UNKNOWN               — insufficient data
        """
        closed = self._closed("1d")
        if len(closed) < 200:
            return {
                "bias": "UNKNOWN",
                "reason": f"Need 200 daily candles, have {len(closed)}",
            }

        price = closed[-1].close
        e20 = self.ema("1d", 20)
        e50 = self.ema("1d", 50)
        e200 = self.ema("1d", 200)
        r = self.rsi("1d", 14)
        a = self.atr("1d", 14)

        if None in (e20, e50, e200, r, a):
            return {"bias": "UNKNOWN", "reason": "Daily indicators not ready"}

        indicators = {
            "price": round(price, 2),
            "ema_20": round(e20, 2),
            "ema_50": round(e50, 2),
            "ema_200": round(e200, 2),
            "rsi_14": round(r, 1),
            "atr_14": round(a, 2),
        }

        bullish = price > e200 and e20 > e50 and e50 > e200 and r > 50
        bearish = price < e200 and e20 < e50 and e50 < e200 and r < 50

        macro = self.get_macro_regime()
        macro_bullish = macro["regime"] in ("BULL_RUN", "HEALTHY_PULLBACK")

        if bullish:
            bias = "BULLISH_ALIGNED"
        elif bearish and not macro_bullish:
            bias = "BEARISH"
        elif not bullish and macro_bullish:
            bias = "WEAK_BUT_MACRO_BULL"
        else:
            bias = "NEUTRAL"

        return {"bias": bias, "indicators": indicators, "macro_regime": macro["regime"]}

    # ══════════════════════════════════════════════════════════════════
    # LAYER 3 — 4H trend
    # ══════════════════════════════════════════════════════════════════

    def get_4h_trend(self) -> dict[str, Any]:
        closed = self._closed("4h")
        if len(closed) < 50:
            return {"trend": "unknown", "reason": f"Need 50 4H candles, have {len(closed)}"}
        price = closed[-1].close
        e20 = self.ema("4h", 20)
        e50 = self.ema("4h", 50)
        if None in (e20, e50):
            return {"trend": "unknown", "reason": "4H EMAs not ready"}
        if price > e20 and price > e50 and e20 > e50:
            trend = "bullish"
        elif price < e20 and price < e50 and e20 < e50:
            trend = "bearish"
        else:
            trend = "neutral"
        return {
            "trend": trend,
            "price": round(price, 2),
            "ema_20": round(e20, 2),
            "ema_50": round(e50, 2),
        }

    # ══════════════════════════════════════════════════════════════════
    # LAYER 4 — 1H market mode  (UP / DOWN / WATCH)
    # ══════════════════════════════════════════════════════════════════

    def get_market_mode(self) -> dict[str, Any]:
        closed_1h = self._closed("1h")
        if len(closed_1h) < 200:
            return {
                "mode": "WAIT",
                "reason": f"Need 200 1H candles for EMA200, have {len(closed_1h)}",
                "indicators": {},
            }

        price = closed_1h[-1].close
        e20 = self.ema("1h", 20)
        e50 = self.ema("1h", 50)
        e200 = self.ema("1h", 200)
        r = self.rsi("1h", 14)
        a = self.atr("1h", 14)

        if None in (e20, e50, e200, r, a):
            return {"mode": "WAIT", "reason": "Indicators not ready", "indicators": {}}

        atr_excessive = self._is_atr_excessive(closed_1h, a)

        indicators = {
            "price": round(price, 2),
            "ema_20": round(e20, 2),
            "ema_50": round(e50, 2),
            "ema_200": round(e200, 2),
            "rsi_14": round(r, 1),
            "atr_14": round(a, 2),
            "atr_excessive": atr_excessive,
        }

        up_conditions = [price > e200, e20 > e50, e50 > e200, r > 50, not atr_excessive]
        if all(up_conditions):
            return {"mode": "UP", "reason": "Price>EMA200, EMA20>EMA50>EMA200, RSI>50, ATR normal", "indicators": indicators}

        down_conditions = [price < e200, e20 < e50, e50 < e200, r < 50]
        if all(down_conditions):
            return {"mode": "DOWN", "reason": "Price<EMA200, EMA20<EMA50<EMA200, RSI<50", "indicators": indicators}

        watch_conds = {
            "price_above_ema20": price > e20,
            "ema20_crossing_ema50": e20 > e50 or (e20 / e50 > 0.998),
            "rsi_recovering": r > 40,
            "lows_stabilising": self._lows_stabilising(closed_1h),
        }
        if sum(watch_conds.values()) >= 3:
            return {"mode": "WATCH", "reason": f"Reversal forming — {sum(watch_conds.values())}/4 conditions met", "watch_conditions": watch_conds, "indicators": indicators}

        return {"mode": "DOWN", "reason": "Conditions unclear, defaulting to safe mode", "indicators": indicators}

    # ══════════════════════════════════════════════════════════════════
    # LAYER 5 — 5M pullback detection
    # ══════════════════════════════════════════════════════════════════

    def get_5m_pullback(self) -> dict[str, Any]:
        closed = self._closed("5m")
        if len(closed) < 12:
            return {"pullback_valid": False, "reason": "Need ≥12 5M candles"}

        recent = closed[-12:]
        recent_high = max(c.high for c in recent)
        current_price = closed[-1].close
        pullback_pct = (recent_high - current_price) / recent_high * 100

        import config as _cfg
        if getattr(_cfg, "AGGRESSIVE_ENTRY", False):
            valid = pullback_pct >= 0.08
        else:
            valid = 0.3 <= pullback_pct <= 0.6
        suggested_entry = round(current_price * 0.9998, 2) if valid else None

        return {
            "pullback_valid": valid,
            "pullback_pct": round(pullback_pct, 3),
            "recent_high": round(recent_high, 2),
            "current_price": round(current_price, 2),
            "suggested_entry": suggested_entry,
            "candles_checked": len(recent),
        }

    # ══════════════════════════════════════════════════════════════════
    # LAYER 5b — Sideways range detection (1H)
    # ══════════════════════════════════════════════════════════════════

    def get_range_levels(self) -> dict[str, Any]:
        """
        Detect a trading range from recent 1H candles.

        Uses the last 48 1H candles (~2 days) to find support/resistance.
        Returns range_high, range_low, range_pct, and where price sits
        within the range (0.0 = bottom, 1.0 = top).
        """
        closed = self._closed("1h")
        if len(closed) < 48:
            return {"valid": False, "reason": "Need 48 1H candles"}

        recent = closed[-48:]
        range_high = max(c.high for c in recent)
        range_low = min(c.low for c in recent)
        span = range_high - range_low

        if span <= 0 or range_low <= 0:
            return {"valid": False, "reason": "Invalid range"}

        range_pct = span / range_low * 100
        price = closed[-1].close
        position_in_range = (price - range_low) / span

        rsi = self.rsi("1h", 14)
        ema20 = self.ema("1h", 20)

        import config as _cfg
        aggressive = getattr(_cfg, "AGGRESSIVE_ENTRY", False)
        support_threshold = 0.60 if aggressive else 0.40
        rsi_threshold = 60 if aggressive else 50

        near_support = position_in_range <= support_threshold
        near_resistance = position_in_range >= 0.70
        rsi_oversold = rsi is not None and rsi < rsi_threshold
        rsi_overbought = rsi is not None and rsi > 65
        below_ema20 = ema20 is not None and price < ema20

        if aggressive:
            buy_zone = near_support
        else:
            buy_zone = near_support and (rsi_oversold or below_ema20)
        sell_zone = near_resistance and rsi_overbought

        suggested_entry = round(price * 0.9998, 2) if buy_zone else None
        tp_at_mid = round(range_low + span * 0.55, 2)

        return {
            "valid": True,
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "range_pct": round(range_pct, 3),
            "price": round(price, 2),
            "position_in_range": round(position_in_range, 3),
            "buy_zone": buy_zone,
            "sell_zone": sell_zone,
            "near_support": near_support,
            "near_resistance": near_resistance,
            "rsi_1h": round(rsi, 1) if rsi else None,
            "suggested_entry": suggested_entry,
            "tp_target": tp_at_mid,
        }

    # ── Trade flow ───────────────────────────────────────────────────

    def get_trade_flow(self, window_seconds: int = 60) -> dict:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - window_seconds * 1000
        window = [t for t in self._recent_trades if t["timestamp"] >= cutoff]
        if not window:
            return {"status": "no_trades", "window_seconds": window_seconds}

        buys = [t for t in window if t["side"] == "BUY"]
        sells = [t for t in window if t["side"] == "SELL"]
        buy_vol = sum(t["quote_qty"] for t in buys)
        sell_vol = sum(t["quote_qty"] for t in sells)
        total_vol = buy_vol + sell_vol

        return {
            "window_seconds": window_seconds,
            "total_trades": len(window),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_pct": round(len(buys) / len(window) * 100, 1) if window else 0,
            "buy_volume_usdt": round(buy_vol, 2),
            "sell_volume_usdt": round(sell_vol, 2),
            "net_flow_usdt": round(buy_vol - sell_vol, 2),
            "buy_vol_pct": round(buy_vol / total_vol * 100, 1) if total_vol else 0,
        }

    # ══════════════════════════════════════════════════════════════════
    # FULL ANALYSIS — top-down cascade
    # ══════════════════════════════════════════════════════════════════

    def get_full_analysis(self) -> dict[str, Any]:
        """
        Complete top-down snapshot:
        Monthly/Weekly macro → Daily bias → 4H trend → 1H mode → 5M entry.

        Returns a single action recommendation that respects every layer.
        """
        macro = self.get_macro_regime()
        daily = self.get_daily_bias()
        trend_4h = self.get_4h_trend()
        mode = self.get_market_mode()
        pullback = self.get_5m_pullback()
        range_levels = self.get_range_levels()
        flow = {
            "1min": self.get_trade_flow(60),
            "5min": self.get_trade_flow(300),
        }

        decision = self._top_down_decision(macro, daily, trend_4h, mode, pullback, range_levels)

        return {
            "timestamp": int(time.time()),
            "action": decision["action"],
            "suggested_entry_price": decision.get("entry_price"),
            "position_size_modifier": decision.get("size_mod", 1.0),
            "trade_type": decision.get("trade_type", "trend"),
            "reasons": decision["reasons"],
            "macro_regime": macro,
            "daily_bias": daily,
            "trend_4h": trend_4h,
            "market_mode": mode,
            "pullback_5m": pullback,
            "range_levels": range_levels,
            "trade_flow": flow,
        }

    # ── Top-down decision cascade ────────────────────────────────────

    def _top_down_decision(
        self,
        macro: dict,
        daily: dict,
        trend_4h: dict,
        mode: dict,
        pullback: dict,
        range_lvl: dict | None = None,
    ) -> dict[str, Any]:
        regime = macro.get("regime", "UNKNOWN")
        bias = daily.get("bias", "UNKNOWN")
        t4h = trend_4h.get("trend", "unknown")
        m = mode.get("mode", "DOWN")
        pb_valid = pullback.get("pullback_valid", False)
        entry_price = pullback.get("suggested_entry")
        reasons: list[str] = []

        # ── Gate 1: Macro regime ─────────────────────────────────────
        if regime == "BEARISH":
            reasons.append("Macro regime BEARISH — weekly/monthly bearish structure")
            return {"action": "NO_TRADE", "reasons": reasons}

        if regime == "UNKNOWN":
            reasons.append("Macro data not ready (need more weekly/monthly candles)")

        # ── SIDEWAYS range-trading path ──────────────────────────────
        if regime == "SIDEWAYS" and range_lvl and range_lvl.get("valid"):
            return self._sideways_range_decision(range_lvl, mode, pullback, reasons)

        # ── Gate 2: Daily bias ───────────────────────────────────────
        if bias == "BEARISH" and regime not in ("BULL_RUN", "HEALTHY_PULLBACK", "UNKNOWN"):
            reasons.append("Daily bias BEARISH and macro not bullish — no dip buys")
            return {"action": "NO_TRADE", "reasons": reasons}

        # ── Gate 3: 1H mode must permit ──────────────────────────────
        import config as _cfg
        if m == "WAIT" and not getattr(_cfg, "AGGRESSIVE_ENTRY", False):
            reasons.append(mode.get("reason", "1H indicators warming up"))
            return {"action": "WAIT", "reasons": reasons}

        if m == "DOWN":
            if regime in ("BULL_RUN", "HEALTHY_PULLBACK"):
                reasons.append(
                    f"1H mode DOWN but macro {regime} — sit tight, wait for 1H recovery"
                )
                return {"action": "WAIT", "reasons": reasons}
            reasons.append("1H mode DOWN — do not buy")
            return {"action": "NO_TRADE", "reasons": reasons}

        # ── From here: 1H is UP or WATCH ─────────────────────────────
        reasons.append(f"1H mode: {m}")

        # ── Gate 4: 4H confirmation ──────────────────────────────────
        if t4h == "bearish" and m == "UP":
            reasons.append("1H UP but 4H bearish — skip for safety")
            return {"action": "WAIT", "reasons": reasons}

        # ── Gate 5: Daily + Macro interaction ────────────────────────
        size_mod = 1.0
        if bias == "WEAK_BUT_MACRO_BULL":
            reasons.append("Daily weak but macro bullish — selective dips only (half size)")
            size_mod = 0.5
        elif bias == "BULLISH_ALIGNED":
            reasons.append("Daily aligned bullish — full size")
        elif bias == "NEUTRAL":
            reasons.append("Daily neutral — conservative size")
            size_mod = 0.75

        # ── Gate 6: 5M pullback ──────────────────────────────────────
        if pb_valid:
            reasons.append(
                f"5M pullback {pullback.get('pullback_pct', 0):.2f}% — entry zone"
            )
            return {
                "action": "ENTRY_READY",
                "entry_price": entry_price,
                "size_mod": size_mod,
                "reasons": reasons,
            }

        pct = pullback.get("pullback_pct", 0)
        if getattr(_cfg, "AGGRESSIVE_ENTRY", False) and pct >= 0.02:
            reasons.append(f"5M pullback {pct:.2f}% — aggressive entry")
            return {
                "action": "ENTRY_READY",
                "entry_price": entry_price or pullback.get("current_price"),
                "size_mod": max(size_mod, 0.75),
                "reasons": reasons,
            }
        if pct < 0.3:
            reasons.append(f"5M pullback only {pct:.2f}% — wait for 0.3–0.6%")
        else:
            reasons.append(f"5M pullback {pct:.2f}% — too deep, wait for stabilisation")
        return {"action": "WAIT_FOR_DIP", "size_mod": size_mod, "reasons": reasons}

    def _sideways_range_decision(
        self,
        rng: dict,
        mode: dict,
        pullback: dict,
        reasons: list[str],
    ) -> dict[str, Any]:
        """
        Range-trading logic for SIDEWAYS macro regimes.

        Buy in the lower 40% of the range, sell near resistance.
        Uses tighter sizing since range trades are lower-conviction.
        """
        import config as _cfg
        aggressive = getattr(_cfg, "AGGRESSIVE_ENTRY", False)
        pos = rng.get("position_in_range", 0.5)
        buy_zone = rng.get("buy_zone", False)
        rsi = rng.get("rsi_1h")
        m = mode.get("mode", "DOWN")
        rng_pct = rng.get("range_pct", 0)

        reasons.append(
            f"SIDEWAYS range-trade mode — range {rng['range_low']:,.0f}–{rng['range_high']:,.0f} "
            f"({rng_pct:.1f}%)"
        )
        reasons.append(f"Price at {pos:.0%} of range (0%=support, 100%=resistance)")

        if rng_pct < 0.2:
            reasons.append(f"Range too tight ({rng_pct:.2f}%) — not worth the fees")
            return {"action": "WAIT", "trade_type": "range", "reasons": reasons}

        size_mod = 0.5
        if pos <= 0.20:
            size_mod = 0.75
        elif pos <= 0.30:
            size_mod = 0.6

        if buy_zone:
            rsi_str = f", RSI {rsi:.0f}" if rsi else ""
            reasons.append(f"Price in buy zone (bottom 40%{rsi_str})")

            pb_pct = pullback.get("pullback_pct", 0) or 0
            entry = rng.get("suggested_entry") or pullback.get("suggested_entry") or rng.get("price")

            aggressive = getattr(_cfg, "AGGRESSIVE_ENTRY", False)
            dip_threshold = 0.01 if aggressive else 0.05
            if pb_pct >= dip_threshold:
                reasons.append(f"5M dip ({pb_pct:.2f}%) — range entry ready")
                return {
                    "action": "ENTRY_READY",
                    "entry_price": entry,
                    "size_mod": size_mod,
                    "trade_type": "range",
                    "reasons": reasons,
                }

            reasons.append("In buy zone — micro-dip pending")
            return {"action": "WAIT_FOR_DIP", "size_mod": size_mod, "trade_type": "range", "reasons": reasons}

        if pos <= 0.50:
            reasons.append("Lower half of range — approaching buy zone")
            if aggressive:
                reasons.append("Aggressive mode — entering at mid-range")
                entry = rng.get("suggested_entry") or pullback.get("suggested_entry") or rng.get("price")
                return {
                    "action": "ENTRY_READY",
                    "entry_price": entry,
                    "size_mod": max(size_mod, 0.75),
                    "trade_type": "range",
                    "reasons": reasons,
                }
            return {"action": "WAIT_FOR_DIP", "size_mod": size_mod, "trade_type": "range", "reasons": reasons}

        if m == "DOWN" and pos > 0.6:
            reasons.append("1H DOWN + upper range — avoid")
            return {"action": "NO_TRADE", "trade_type": "range", "reasons": reasons}

        reasons.append("Upper half of range — not a buy zone")
        return {"action": "WAIT", "trade_type": "range", "reasons": reasons}

    # ══════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════

    def _analyse_timeframe(
        self,
        interval: str,
        ema_periods: tuple[int, int] = (50, 200),
        swing_lookback: int = 3,
    ) -> dict[str, Any]:
        """Compute EMAs + market structure for a given interval."""
        closed = self._closed(interval)
        fast_p, slow_p = ema_periods
        needed = max(slow_p, fast_p) + 1
        if len(closed) < needed:
            return {
                "status": "not_ready",
                "reason": f"Need {needed} {interval} candles, have {len(closed)}",
            }

        price = closed[-1].close
        fast = self.ema(interval, fast_p)
        slow = self.ema(interval, slow_p)
        if None in (fast, slow):
            return {"status": "not_ready", "reason": f"{interval} EMAs not ready"}

        structure = self._detect_structure(closed, swing_lookback)

        return {
            "status": "ready",
            "price": round(price, 2),
            f"ema_{fast_p}": round(fast, 2),
            f"ema_{slow_p}": round(slow, 2),
            "fast_above_slow": fast > slow,
            "price_above_fast_ema": price > fast,
            "price_above_slow_ema": price > slow,
            "structure": structure,
        }

    @staticmethod
    def _detect_structure(candles: list[Candle], lookback: int = 3) -> str:
        """
        Classify market structure from swing highs / swing lows.

        Returns: bullish, leaning_bullish, bearish, leaning_bearish, sideways, unknown.
        """
        if len(candles) < lookback * 2 + 3:
            return "unknown"

        swing_highs: list[Candle] = []
        swing_lows: list[Candle] = []

        for i in range(lookback, len(candles) - lookback):
            is_high = all(
                candles[i].high >= candles[i + j].high
                and candles[i].high >= candles[i - j].high
                for j in range(1, lookback + 1)
            )
            is_low = all(
                candles[i].low <= candles[i + j].low
                and candles[i].low <= candles[i - j].low
                for j in range(1, lookback + 1)
            )
            if is_high:
                swing_highs.append(candles[i])
            if is_low:
                swing_lows.append(candles[i])

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "unknown"

        n_h = min(3, len(swing_highs))
        rh = swing_highs[-n_h:]
        hh = all(rh[i].high > rh[i - 1].high for i in range(1, len(rh)))
        lh = all(rh[i].high < rh[i - 1].high for i in range(1, len(rh)))

        n_l = min(3, len(swing_lows))
        rl = swing_lows[-n_l:]
        hl = all(rl[i].low > rl[i - 1].low for i in range(1, len(rl)))
        ll = all(rl[i].low < rl[i - 1].low for i in range(1, len(rl)))

        if hh and hl:
            return "bullish"
        if lh and ll:
            return "bearish"
        if hh or hl:
            return "leaning_bullish"
        if lh or ll:
            return "leaning_bearish"
        return "sideways"

    @staticmethod
    def _is_atr_excessive(candles: list[Candle], current_atr: float) -> bool:
        if len(candles) < 50:
            return False
        avg_range = sum(c.high - c.low for c in candles[-50:]) / 50
        return current_atr > avg_range * 2

    @staticmethod
    def _lows_stabilising(candles_1h: list[Candle]) -> bool:
        if len(candles_1h) < 10:
            return False
        recent = candles_1h[-10:]
        half = len(recent) // 2
        first_low = min(c.low for c in recent[:half])
        second_low = min(c.low for c in recent[half:])
        return second_low >= first_low * 0.998


# ── Pure math ────────────────────────────────────────────────────────


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(d, 0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val
