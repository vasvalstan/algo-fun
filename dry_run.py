"""
Dry-run / paper trading — real Binance data, fake wallet.

  python dry_run.py                        # observe only (old mode)
  python dry_run.py --paper                # paper trade with $1000
  python dry_run.py --paper --capital 500  # paper trade with $500
  python dry_run.py --days 30              # 30-day backtest first

Paper mode:
  • Starts with a local wallet (default 1000 USDT, 0 BTC).
  • Uses real live Binance prices and klines.
  • When strategy says ENTRY_READY → simulates a maker buy.
  • Applies TP / SL / mode-exit rules on live price ticks.
  • Tracks every trade, running P&L, equity curve.
  • Never touches your real Binance account.

Ctrl+C to stop.

Web dashboard (same machine): cd web && npm run dev → http://localhost:3000/paper
(requires --paper; reads paper_dashboard.json next to this file)
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import config
import market_data
from indicators import StrategyEngine

logging.basicConfig(
    filename="dry_run.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RESET = "\033[0m"
CLEAR = "\033[H\033[J"

MAKER_FEE = 0.001
UP_ARROW = "▲"
DN_ARROW = "▼"
SPARKLINE_CHARS = "▁▂▃▄▅▆▇█"
BG_GREEN = "\033[42m\033[30m"
BG_RED = "\033[41m\033[37m"
BG_YELLOW = "\033[43m\033[30m"
BG_RESET = "\033[0m"


class PriceTracker:
    """Track live price movements for sparkline and session stats."""

    def __init__(self, maxlen: int = 60) -> None:
        self.prices: deque[float] = deque(maxlen=maxlen)
        self.timestamps: deque[float] = deque(maxlen=maxlen)
        self.session_high = 0.0
        self.session_low = float("inf")
        self.prev_price = 0.0
        self.tick_count = 0

    def update(self, price: float) -> None:
        self.prev_price = self.prices[-1] if self.prices else price
        self.prices.append(price)
        self.timestamps.append(time.time())
        if price > self.session_high:
            self.session_high = price
        if price < self.session_low:
            self.session_low = price
        self.tick_count += 1

    @property
    def delta(self) -> float:
        return self.prices[-1] - self.prev_price if len(self.prices) >= 2 else 0.0

    @property
    def delta_pct(self) -> float:
        if self.prev_price and len(self.prices) >= 2:
            return self.delta / self.prev_price * 100
        return 0.0

    def sparkline(self, width: int = 40) -> str:
        if len(self.prices) < 2:
            return f"{DIM}waiting for data…{RESET}"
        pts = list(self.prices)[-width:]
        lo, hi = min(pts), max(pts)
        span = hi - lo if hi > lo else 1
        result = []
        for i, p in enumerate(pts):
            idx = int((p - lo) / span * (len(SPARKLINE_CHARS) - 1))
            idx = max(0, min(len(SPARKLINE_CHARS) - 1, idx))
            if i > 0:
                c = GREEN if p >= pts[i - 1] else RED
            else:
                c = DIM
            result.append(f"{c}{SPARKLINE_CHARS[idx]}{RESET}")
        return "".join(result)

    def session_range_bar(self, price: float, width: int = 30) -> str:
        if self.session_high <= self.session_low:
            return ""
        span = self.session_high - self.session_low
        pos = (price - self.session_low) / span
        marker = max(0, min(width - 1, int(pos * width)))
        bar = list("─" * width)
        bar[0] = "╠"
        bar[-1] = "╣"
        bar[marker] = "●"
        return (
            f"{RED}{self.session_low:,.0f}{RESET} "
            f"{CYAN}{''.join(bar)}{RESET} "
            f"{GREEN}{self.session_high:,.0f}{RESET}"
        )


class EventFeed:
    """Scrolling event log for the dashboard."""

    def __init__(self, maxlen: int = 15) -> None:
        # (time_str, level, plain_text) — level drives ANSI in render + JSON feed
        self.events: deque[tuple[str, str, str]] = deque(maxlen=maxlen)

    def add(self, text: str, level: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        self.events.append((ts, level, text))

    def render(self, max_lines: int = 8) -> list[str]:
        cmap = {
            "buy": GREEN, "sell": RED, "tp": GREEN,
            "sl": RED, "info": DIM, "warn": YELLOW, "signal": CYAN,
        }
        lines = []
        for ts, level, text in list(self.events)[-max_lines:]:
            color = cmap.get(level, DIM)
            lines.append(f"  {DIM}{ts}{RESET}  {color}{text}{RESET}")
        return lines

BOOTSTRAP_COUNTS = {
    "5m": 200, "1h": 250, "4h": 60, "1d": 250, "1w": 250, "1M": 100,
}
REFRESH_INTERVALS = {
    "5m": 30, "1h": 300, "4h": 900, "1d": 1800, "1w": 3600, "1M": 3600,
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")
PAPER_DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "paper_dashboard.json")

PAPER_DASHBOARD_NOTE = (
    "Paper mode fills immediately when ENTRY_READY — there is no resting Binance limit order. "
    "The terminal 'open order' block is an open spot position (BTC held), not an order book entry."
)


def _parse_kline(raw: list) -> dict:
    return {
        "open_time": raw[0], "close_time": raw[6],
        "open": raw[1], "high": raw[2], "low": raw[3], "close": raw[4],
        "volume": raw[5], "quote_volume": raw[7], "trades": raw[8],
        "is_closed": True,
    }


def _fetch(interval: str, limit: int) -> list:
    return market_data.get_klines(interval=interval, limit=limit)


# ── Paper wallet ─────────────────────────────────────────────────────

@dataclass
class PaperTrade:
    entry_time: str
    entry_price: float
    qty: float
    size_usdt: float
    exit_time: str = ""
    exit_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    mode: str = ""


@dataclass
class PaperWallet:
    usdt: float = 1000.0
    btc: float = 0.0
    starting_capital: float = 1000.0

    in_position: bool = False
    entry_price: float = 0.0
    entry_time: str = ""
    position_qty: float = 0.0
    position_usdt: float = 0.0
    entry_mode: str = ""
    trade_type: str = "trend"
    range_tp_target: float = 0.0

    trades: list = field(default_factory=list)
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    last_sell_ts: float = 0.0

    def equity(self, price: float) -> float:
        return self.usdt + self.btc * price

    def buy(self, price: float, usdt_amount: float, mode: str = "") -> str:
        if usdt_amount > self.usdt:
            usdt_amount = self.usdt
        if usdt_amount < 5:
            return ""

        fee = usdt_amount * MAKER_FEE
        net_usdt = usdt_amount - fee
        qty = net_usdt / price

        self.usdt -= usdt_amount
        self.btc += qty
        self.in_position = True
        self.entry_price = price
        self.entry_time = time.strftime("%H:%M:%S")
        self.position_qty = qty
        self.position_usdt = usdt_amount
        self.entry_mode = mode
        # trade_type and range_tp_target are set by paper_tick before calling buy

        return (
            f"BUY {qty:.6f} BTC @ {price:,.2f}  "
            f"({usdt_amount:.2f} USDT, fee {fee:.4f})"
        )

    def sell(self, price: float, reason: str = "TP") -> str:
        if not self.in_position or self.position_qty <= 0:
            return ""

        gross = self.position_qty * price
        fee = gross * MAKER_FEE
        net = gross - fee

        pnl_pct = (price - self.entry_price) / self.entry_price * 100
        pnl_usdt = net - self.position_usdt

        self.usdt += net
        self.btc -= self.position_qty

        trade = PaperTrade(
            entry_time=self.entry_time,
            entry_price=self.entry_price,
            qty=self.position_qty,
            size_usdt=self.position_usdt,
            exit_time=time.strftime("%H:%M:%S"),
            exit_price=price,
            pnl=pnl_usdt,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            mode=self.entry_mode,
        )
        self.trades.append(trade)
        self.last_sell_ts = time.time()

        self.in_position = False
        self.entry_price = 0.0
        self.position_qty = 0.0
        self.position_usdt = 0.0

        sign = "+" if pnl_usdt >= 0 else ""
        return (
            f"SELL @ {price:,.2f}  {sign}{pnl_usdt:.4f} USDT  "
            f"({pnl_pct:+.2f}%)  [{reason}]"
        )

    def update_drawdown(self, price: float) -> None:
        eq = self.equity(price)
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.peak_equity - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    def stats(self) -> dict:
        pnls = [t.pnl for t in self.trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]
        total = sum(pnls) if pnls else 0
        wr = len(winners) / len(pnls) * 100 if pnls else 0
        avg = total / len(pnls) if pnls else 0
        return {
            "trades": len(pnls),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": wr,
            "total_pnl": total,
            "avg_pnl": avg,
            "best": max(pnls) if pnls else 0,
            "worst": min(pnls) if pnls else 0,
        }

    def save(self) -> None:
        data = {
            "usdt": self.usdt,
            "btc": self.btc,
            "starting_capital": self.starting_capital,
            "in_position": self.in_position,
            "entry_price": self.entry_price,
            "position_qty": self.position_qty,
            "position_usdt": self.position_usdt,
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "trades": [asdict(t) for t in self.trades[-50:]],
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)


# ── Backtester ───────────────────────────────────────────────────────

def run_backtest(days: int = 14, trade_size: float = 25.0,
                 tp_pct: float = 0.5, sl_pct: float = 0.3) -> dict:
    limit_5m = min(days * 24 * 12, 1000)
    print(f"  Fetching {limit_5m} 5m candles for {days}-day backtest...")
    raw_5m = _fetch("5m", limit_5m)
    klines_5m = [_parse_kline(k) for k in raw_5m]

    bt_engine = StrategyEngine()
    for interval in ("1M", "1w", "1d", "4h", "1h"):
        raw = _fetch(interval, BOOTSTRAP_COUNTS.get(interval, 100))
        for k in raw:
            bt_engine.update_candle(interval, _parse_kline(k))

    trades, in_pos = [], False
    entry_price = entry_idx = 0
    entry_time, last_mode = "", ""
    last_sell_idx = -999
    eff_size = trade_size

    for i, k5 in enumerate(klines_5m):
        bt_engine.update_candle("5m", k5)
        price = float(k5["close"])
        ts = datetime.fromtimestamp(
            k5["open_time"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        if i < 12:
            continue
        if in_pos:
            pnl_pct = (price - entry_price) / entry_price * 100
            hold = i - entry_idx
            reason = ""
            if pnl_pct >= tp_pct:
                reason = "TP"
            elif pnl_pct <= -sl_pct:
                reason = "SL"
            elif hold >= 60:
                reason = "max_hold"
            else:
                mode = bt_engine.get_market_mode()
                macro = bt_engine.get_macro_regime()
                if mode["mode"] == "DOWN" and macro.get("regime") != "BULL_RUN":
                    reason = "mode_DOWN"
                elif macro.get("regime") == "BEARISH":
                    reason = "macro_BEAR"
            if reason:
                fee = eff_size * MAKER_FEE * 2
                pnl = eff_size * (pnl_pct / 100) - fee
                trades.append({"entry": entry_time, "exit": ts,
                               "entry_p": entry_price, "exit_p": price,
                               "pnl": pnl, "pnl_pct": pnl_pct,
                               "reason": reason, "mode": last_mode})
                in_pos = False
                last_sell_idx = i
        else:
            if i - last_sell_idx < 6:
                continue
            analysis = bt_engine.get_full_analysis()
            action = analysis.get("action", "WAIT")
            last_mode = analysis.get("market_mode", {}).get("mode", "")
            if action == "ENTRY_READY":
                in_pos = True
                entry_price = price
                entry_time = ts
                entry_idx = i
                eff_size = trade_size * analysis.get("position_size_modifier", 1.0)

    pnls = [t["pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    total = sum(pnls) if pnls else 0
    wr = len(winners) / len(pnls) * 100 if pnls else 0
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    avg = total / len(pnls) if pnls else 0
    if len(pnls) > 1:
        std = math.sqrt(sum((p - avg) ** 2 for p in pnls) / (len(pnls) - 1))
        sharpe = (avg / std) * math.sqrt(252) if std > 0 else 0
    else:
        sharpe = 0
    return {
        "days": days, "candles_5m": len(klines_5m), "trades": len(trades),
        "winners": len(winners), "losers": len(pnls) - len(winners),
        "win_rate": wr, "total_pnl": total, "max_drawdown": dd,
        "sharpe": sharpe, "best": max(pnls) if pnls else 0,
        "worst": min(pnls) if pnls else 0, "sample": trades[-10:],
    }


# ── Confidence ───────────────────────────────────────────────────────

def compute_confidence(
    analysis: dict,
    bt: dict,
    tracker: "PriceTracker | None" = None,
) -> dict:
    scores = {"buy": 0.0, "sell": 0.0}
    details = []

    # ── 1. Macro regime (slow, updates daily/weekly) ─────────────────
    regime = analysis.get("macro_regime", {}).get("regime", "UNKNOWN")
    bmap = {"BULL_RUN": (20, 0), "HEALTHY_PULLBACK": (15, 3),
            "SIDEWAYS": (8, 8), "BEARISH": (0, 20), "UNKNOWN": (8, 8)}
    b, s = bmap.get(regime, (8, 8))
    scores["buy"] += b; scores["sell"] += s
    details.append(f"Macro {regime}: buy +{b}  sell +{s}")

    # ── 2. Daily bias (slow, updates every 30min) ────────────────────
    bias = analysis.get("daily_bias", {}).get("bias", "UNKNOWN")
    dmap = {"BULLISH_ALIGNED": (15, 0), "WEAK_BUT_MACRO_BULL": (10, 3),
            "NEUTRAL": (8, 5), "BEARISH": (0, 15), "UNKNOWN": (5, 5)}
    b, s = dmap.get(bias, (5, 5))
    scores["buy"] += b; scores["sell"] += s
    details.append(f"Daily {bias}: buy +{b}  sell +{s}")

    # ── 3. 4H trend (medium, updates every 15min) ────────────────────
    t4h = analysis.get("trend_4h", {}).get("trend", "unknown")
    t4map = {"bullish": (8, 0), "neutral": (4, 3), "bearish": (0, 8), "unknown": (3, 3)}
    b, s = t4map.get(t4h, (3, 3))
    scores["buy"] += b; scores["sell"] += s
    details.append(f"4H {t4h}: buy +{b}  sell +{s}")

    # ── 4. 1H mode (medium, updates every 5min) ─────────────────────
    mode = analysis.get("market_mode", {}).get("mode", "WAIT")
    mmap = {"UP": (12, 0), "WATCH": (8, 3), "DOWN": (0, 12), "WAIT": (4, 4)}
    b, s = mmap.get(mode, (4, 4))
    scores["buy"] += b; scores["sell"] += s
    details.append(f"1H mode {mode}: buy +{b}  sell +{s}")

    # ── 5. 1H RSI (real-time, updates every refresh) ─────────────────
    rsi = analysis.get("market_mode", {}).get("indicators", {}).get("rsi_14")
    if rsi is not None:
        if rsi < 30:
            b, s = 12, 0
        elif rsi < 40:
            b, s = 8, 2
        elif rsi < 50:
            b, s = 5, 4
        elif rsi < 60:
            b, s = 3, 5
        elif rsi < 70:
            b, s = 1, 8
        else:
            b, s = 0, 12
        scores["buy"] += b; scores["sell"] += s
        details.append(f"RSI {rsi:.1f}: buy +{b}  sell +{s}")

    # ── 6. Range position (real-time when SIDEWAYS) ──────────────────
    rng = analysis.get("range_levels", {})
    if rng.get("valid"):
        pos_r = rng.get("position_in_range", 0.5)
        b = max(0, int((1.0 - pos_r) * 15))
        s = max(0, int(pos_r * 15))
        scores["buy"] += b; scores["sell"] += s
        zone = "support" if pos_r < 0.3 else "resistance" if pos_r > 0.7 else "mid"
        details.append(f"Range {pos_r:.0%} ({zone}): buy +{b}  sell +{s}")

    # ── 7. 5M pullback (real-time) ───────────────────────────────────
    pb = analysis.get("pullback_5m", {})
    if pb.get("pullback_valid", False):
        scores["buy"] += 10
        details.append("5M pullback valid: buy +10")
    else:
        pct = pb.get("pullback_pct", 0) or 0
        if pct >= 0.2:
            scores["buy"] += 6
            details.append(f"5M dip {pct:.2f}%: buy +6")
        elif pct >= 0.05:
            scores["buy"] += 3
            details.append(f"5M dip {pct:.2f}%: buy +3")
        else:
            scores["sell"] += 2
            details.append(f"5M flat {pct:.2f}%: sell +2")

    # ── 8. Price momentum (REAL-TIME, updates every tick) ────────────
    if tracker and len(tracker.prices) >= 5:
        prices = list(tracker.prices)
        recent = prices[-10:] if len(prices) >= 10 else prices
        oldest, newest = recent[0], recent[-1]
        if oldest > 0:
            momentum_pct = (newest - oldest) / oldest * 100
            if momentum_pct < -0.05:
                b = min(10, int(abs(momentum_pct) * 50))
                scores["buy"] += b
                details.append(f"Momentum {momentum_pct:+.3f}% (falling): buy +{b}")
            elif momentum_pct > 0.05:
                s_val = min(10, int(momentum_pct * 50))
                scores["sell"] += s_val
                details.append(f"Momentum {momentum_pct:+.3f}% (rising): sell +{s_val}")
            else:
                details.append(f"Momentum {momentum_pct:+.3f}% (flat)")

    # ── 9. Price volatility (REAL-TIME) ──────────────────────────────
    if tracker and len(tracker.prices) >= 10:
        prices = list(tracker.prices)[-20:]
        hi, lo = max(prices), min(prices)
        if lo > 0:
            vol_pct = (hi - lo) / lo * 100
            if vol_pct > 0.1:
                scores["buy"] += 3
                scores["sell"] += 3
                details.append(f"Tick volatility {vol_pct:.3f}%: +3/+3")

    # ── 10. Backtest (fixed at startup) ──────────────────────────────
    wr = bt.get("win_rate", 50)
    bt_b = min(5, int(wr / 20))
    scores["buy"] += bt_b; scores["sell"] += (5 - bt_b)
    details.append(f"Backtest WR {wr:.0f}%: buy +{bt_b}  sell +{5 - bt_b}")

    total = scores["buy"] + scores["sell"]
    if total > 0:
        scores["buy"] = scores["buy"] / total * 100
        scores["sell"] = scores["sell"] / total * 100

    return {
        "buy_pct": scores["buy"], "sell_pct": scores["sell"],
        "details": details, "action": analysis.get("action", "WAIT"),
        "reasons": analysis.get("reasons", []),
    }


# ── Rendering ────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 30) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    bar = "█" * filled + "░" * (width - filled)
    c = GREEN if pct >= 70 else YELLOW if pct >= 40 else RED
    return f"{c}{bar}{RESET} {BOLD}{pct:.0f}%{RESET}"


def _pnl_c(v: float) -> str:
    c = GREEN if v > 0 else RED if v < 0 else ""
    return f"{c}{v:+.4f}{RESET}"


def _order_level_bar(
    price: float, entry: float, tp: float, sl: float, width: int = 40
) -> str:
    """Visual bar showing SL ← entry ← price → TP positions."""
    lo = min(sl, price) - (tp - sl) * 0.1
    hi = max(tp, price) + (tp - sl) * 0.1
    span = hi - lo if hi > lo else 1

    def pos(v: float) -> int:
        return max(0, min(width - 1, int((v - lo) / span * width)))

    bar = list("·" * width)
    sl_i, entry_i, price_i, tp_i = pos(sl), pos(entry), pos(price), pos(tp)

    fill_lo = min(sl_i, entry_i)
    fill_hi = max(tp_i, entry_i)
    for i in range(fill_lo, min(fill_hi + 1, width)):
        bar[i] = "─"

    bar[sl_i] = "╳"
    bar[entry_i] = "◆"
    bar[tp_i] = "◎"
    bar[price_i] = "●"

    colored = []
    for i, ch in enumerate(bar):
        if ch == "╳":
            colored.append(f"{RED}{ch}{RESET}")
        elif ch == "●":
            c = GREEN if price >= entry else RED
            colored.append(f"{c}{BOLD}{ch}{RESET}")
        elif ch == "◎":
            colored.append(f"{GREEN}{ch}{RESET}")
        elif ch == "◆":
            colored.append(f"{CYAN}{ch}{RESET}")
        elif ch == "─":
            colored.append(f"{DIM}{ch}{RESET}")
        else:
            colored.append(f"{DIM}{ch}{RESET}")

    return "".join(colored)


def write_paper_dashboard_json(
    price: float,
    analysis: dict,
    conf: dict,
    bt: dict,
    wallet: PaperWallet,
    start_time: float,
    tracker: "PriceTracker | None",
    feed: "EventFeed | None",
) -> None:
    """Emit JSON for the local Next.js paper dashboard (web/app/paper)."""
    eq = wallet.equity(price)
    rng = analysis.get("range_levels") or {}
    pb = analysis.get("pullback_5m") or {}

    cooldown_left = 0
    cd = 300 if analysis.get("trade_type") == "range" else config.COOLDOWN_SEC
    if wallet.last_sell_ts > 0:
        elapsed = time.time() - wallet.last_sell_ts
        if elapsed < cd:
            cooldown_left = int(cd - elapsed)

    position = None
    if wallet.in_position:
        ur_pct = (price - wallet.entry_price) / wallet.entry_price * 100
        ur_usdt = wallet.position_qty * (price - wallet.entry_price)
        is_rng = wallet.trade_type == "range"
        tp_pct = 0.3 if is_rng else config.TAKE_PROFIT_PCT
        sl_pct = 0.2 if is_rng else config.STOP_LOSS_PCT
        tp_p = wallet.entry_price * (1 + tp_pct / 100)
        sl_p = wallet.entry_price * (1 - sl_pct / 100)
        rt = wallet.range_tp_target if is_rng and wallet.range_tp_target > 0 else None
        position = {
            "active": True,
            "trade_type": wallet.trade_type,
            "entry_price": round(wallet.entry_price, 2),
            "entry_time": wallet.entry_time,
            "qty": round(wallet.position_qty, 8),
            "usdt": round(wallet.position_usdt, 4),
            "unrealized_pct": round(ur_pct, 4),
            "unrealized_usdt": round(ur_usdt, 6),
            "tp_price": round(tp_p, 2),
            "sl_price": round(sl_p, 2),
            "range_mid_target": round(rt, 2) if rt else None,
        }

    feed_out = []
    if feed:
        for ts, level, text in feed.events:
            feed_out.append({"ts": ts, "level": level, "text": text})

    closed = []
    for t in wallet.trades[-40:]:
        closed.append({
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "qty": round(t.qty, 8),
            "pnl": round(t.pnl, 6),
            "pnl_pct": round(t.pnl_pct, 4),
            "exit_reason": t.exit_reason,
            "mode": t.mode,
        })

    payload = {
        "source": "dry_run",
        "timestamp": time.time(),
        "symbol": config.SYMBOL,
        "mainnet": config.USE_MAINNET,
        "paper": True,
        "price": round(price, 2),
        "uptime_s": int(time.time() - start_time),
        "prices": [round(p, 2) for p in list(tracker.prices)[-80:]] if tracker else [],
        "session": {
            "starting_capital": round(wallet.starting_capital, 2),
            "usdt": round(wallet.usdt, 8),
            "btc": round(wallet.btc, 8),
            "equity": round(eq, 4),
            "max_drawdown": round(wallet.max_drawdown, 4),
            "pnl_usdt": round(eq - wallet.starting_capital, 4),
            "pnl_pct": round(
                (eq - wallet.starting_capital) / wallet.starting_capital * 100, 4
            )
            if wallet.starting_capital
            else 0.0,
        },
        "analysis": {
            "action": analysis.get("action", "WAIT"),
            "trade_type": analysis.get("trade_type", "trend"),
            "macro": analysis.get("macro_regime", {}).get("regime", "?"),
            "daily": analysis.get("daily_bias", {}).get("bias", "?"),
            "trend_4h": analysis.get("trend_4h", {}).get("trend", "?"),
            "mode_1h": analysis.get("market_mode", {}).get("mode", "?"),
            "pullback_pct": pb.get("pullback_pct"),
            "pullback_valid": bool(pb.get("pullback_valid")),
            "range_low": rng.get("range_low") if rng.get("valid") else None,
            "range_high": rng.get("range_high") if rng.get("valid") else None,
            "position_in_range": rng.get("position_in_range") if rng.get("valid") else None,
            "buy_zone": bool(rng.get("buy_zone")),
            "reasons": analysis.get("reasons", []),
        },
        "confidence": {
            "buy_pct": round(conf.get("buy_pct", 0), 2),
            "sell_pct": round(conf.get("sell_pct", 0), 2),
            "details": conf.get("details", []),
        },
        "backtest": {
            "days": bt.get("days", 0),
            "trades": bt.get("trades", 0),
            "win_rate": round(bt.get("win_rate", 0), 2),
            "total_pnl": round(bt.get("total_pnl", 0), 4),
            "sharpe": round(bt.get("sharpe", 0), 2),
        },
        "strategy_config": {
            "trade_size_usdt": config.TRADE_SIZE_USDT,
            "take_profit_pct": config.TAKE_PROFIT_PCT,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "cooldown_sec": config.COOLDOWN_SEC,
            "poll_interval": config.POLL_INTERVAL,
        },
        "position": position,
        "cooldown_s_remaining": cooldown_left,
        "closed_trades": closed,
        "feed": feed_out,
        "note": PAPER_DASHBOARD_NOTE,
    }

    tmp = PAPER_DASHBOARD_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, PAPER_DASHBOARD_FILE)
    except OSError:
        pass


def render(
    price: float, analysis: dict, conf: dict, bt: dict,
    wallet: PaperWallet, start_time: float,
    paper_mode: bool,
    tracker: "PriceTracker | None" = None,
    feed: "EventFeed | None" = None,
) -> None:
    w = 76
    sep = "─" * w
    lines = []

    mode_label = f"{MAGENTA}PAPER TRADING{RESET}" if paper_mode else "DRY RUN"
    now_s = time.strftime("%H:%M:%S")
    uptime = int(time.time() - start_time)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    lines.append(
        f" {BOLD}{mode_label}{RESET}  {config.SYMBOL}  "
        f"{DIM}{now_s}  up {h}h{m:02d}m{s:02d}s{RESET}"
    )
    lines.append(sep)

    # ── Live price with delta ─────────────────────────────────────────
    if tracker and tracker.tick_count >= 2:
        delta = tracker.delta
        delta_pct = tracker.delta_pct
        arrow = UP_ARROW if delta >= 0 else DN_ARROW
        dc = GREEN if delta >= 0 else RED
        lines.append(
            f" {BOLD}PRICE{RESET}  {BOLD}{price:>12,.2f}{RESET}  "
            f"{dc}{arrow} {delta:+.2f} ({delta_pct:+.3f}%){RESET}"
        )
    else:
        lines.append(f" {BOLD}PRICE{RESET}  {BOLD}{price:>12,.2f}{RESET}")

    # ── Sparkline chart ───────────────────────────────────────────────
    if tracker and len(tracker.prices) >= 3:
        lines.append(f"  {tracker.sparkline(50)}")
        lines.append(f"  {tracker.session_range_bar(price, 34)}")
    lines.append("")

    # ── Paper wallet + P&L ──────────────────────────────────────────
    if paper_mode:
        eq = wallet.equity(price)
        pnl_total = eq - wallet.starting_capital
        pnl_pct = pnl_total / wallet.starting_capital * 100
        pnl_color = GREEN if pnl_total >= 0 else RED

        base = config.SYMBOL.replace("USDT", "")
        btc_val = wallet.btc * price

        pnl_badge = (
            f"{BG_GREEN} P&L {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%) {BG_RESET}"
            if pnl_total >= 0 else
            f"{BG_RED} P&L {pnl_total:+.2f} USDT ({pnl_pct:+.2f}%) {BG_RESET}"
        )

        lines.append(f" {BOLD}PORTFOLIO{RESET}  {pnl_badge}")
        lines.append(
            f"  USDT   {BOLD}{wallet.usdt:>10,.2f}{RESET}    "
            f"{base}  {wallet.btc:.8f}  (~{btc_val:,.2f} USDT)"
        )
        lines.append(
            f"  Equity {BOLD}{eq:>10,.2f}{RESET}    "
            f"Start {DIM}{wallet.starting_capital:,.0f}{RESET}    "
            f"MaxDD {RED}{wallet.max_drawdown:.2f}{RESET}"
        )

        st = wallet.stats()
        if st["trades"] > 0:
            wr_c = GREEN if st["win_rate"] >= 50 else RED
            lines.append(
                f"  Trades {BOLD}{st['trades']}{RESET}  "
                f"W {GREEN}{st['winners']}{RESET}  L {RED}{st['losers']}{RESET}  "
                f"WR {wr_c}{BOLD}{st['win_rate']:.0f}%{RESET}  "
                f"avg {_pnl_c(st['avg_pnl'])}  "
                f"best {GREEN}{st['best']:+.4f}{RESET}  "
                f"worst {RED}{st['worst']:+.4f}{RESET}"
            )
        else:
            lines.append(f"  {DIM}No trades yet — waiting for entry signal{RESET}")

        lines.append("")

        # ── OPEN ORDER / POSITION ─────────────────────────────────────
        if wallet.in_position:
            unrealized = (price - wallet.entry_price) / wallet.entry_price * 100
            unrealized_usdt = wallet.position_qty * (price - wallet.entry_price)
            ur_c = GREEN if unrealized >= 0 else RED
            is_rng = wallet.trade_type == "range"
            tp_pct = 0.3 if is_rng else config.TAKE_PROFIT_PCT
            sl_pct = 0.2 if is_rng else config.STOP_LOSS_PCT
            tp_price = wallet.entry_price * (1 + tp_pct / 100)
            sl_price = wallet.entry_price * (1 - sl_pct / 100)
            rng_label = f" {MAGENTA}RANGE{RESET}" if is_rng else f" {CYAN}TREND{RESET}"

            order_badge = (
                f"{BG_GREEN} OPEN POSITION {BG_RESET}"
                if unrealized >= 0 else
                f"{BG_RED} OPEN POSITION {BG_RESET}"
            )
            lines.append(f" {order_badge}{rng_label}")
            lines.append(
                f"  {BOLD}BUY{RESET}  {wallet.position_qty:.6f} {base} "
                f"@ {BOLD}{wallet.entry_price:,.2f}{RESET}  "
                f"({wallet.position_usdt:.2f} USDT)  "
                f"at {DIM}{wallet.entry_time}{RESET}"
            )
            lines.append(
                f"  Now   {BOLD}{price:,.2f}{RESET}  "
                f"P&L {ur_c}{BOLD}{unrealized:+.2f}%{RESET}  "
                f"({ur_c}{unrealized_usdt:+.2f} USDT{RESET})"
            )
            lvl_bar = _order_level_bar(price, wallet.entry_price, tp_price, sl_price)
            lines.append(
                f"  {RED}SL {sl_price:,.0f}{RESET}  {lvl_bar}  "
                f"{GREEN}TP {tp_price:,.0f}{RESET}"
            )
            if is_rng and wallet.range_tp_target > 0:
                lines.append(
                    f"  {DIM}Range mid-target:{RESET} "
                    f"{MAGENTA}{wallet.range_tp_target:,.0f}{RESET}"
                )
            lines.append("")
        else:
            cooldown_left = 0
            cd = 300 if analysis.get("trade_type") == "range" else config.COOLDOWN_SEC
            if wallet.last_sell_ts > 0:
                elapsed = time.time() - wallet.last_sell_ts
                if elapsed < cd:
                    cooldown_left = int(cd - elapsed)

            lines.append(f" {DIM}▸ NO OPEN POSITION{RESET}")
            if cooldown_left > 0:
                lines.append(
                    f"  {YELLOW}Cooldown: {cooldown_left}s remaining{RESET}"
                )
            lines.append("")

    # ── Backtest ─────────────────────────────────────────────────────
    wr_c = GREEN if bt["win_rate"] >= 50 else RED
    pnl_c2 = GREEN if bt["total_pnl"] >= 0 else RED
    lines.append(
        f" {BOLD}BACKTEST{RESET} ({bt['days']}d)  "
        f"{bt['trades']} trades  WR {wr_c}{bt['win_rate']:.0f}%{RESET}  "
        f"P&L {pnl_c2}{bt['total_pnl']:+.2f}{RESET}  "
        f"Sharpe {bt['sharpe']:.1f}"
    )
    lines.append("")

    # ── Analysis ─────────────────────────────────────────────────────
    regime = analysis.get("macro_regime", {}).get("regime", "?")
    bias = analysis.get("daily_bias", {}).get("bias", "?")
    t4h = analysis.get("trend_4h", {}).get("trend", "?")
    mode = analysis.get("market_mode", {}).get("mode", "?")
    pb = analysis.get("pullback_5m", {})
    action = analysis.get("action", "WAIT")

    rc = {"BULL_RUN": GREEN, "HEALTHY_PULLBACK": CYAN, "BEARISH": RED, "SIDEWAYS": YELLOW}.get(regime, DIM)
    mc = {"UP": GREEN, "DOWN": RED, "WATCH": YELLOW}.get(mode, DIM)
    ac = {"ENTRY_READY": GREEN, "WAIT_FOR_DIP": YELLOW, "NO_TRADE": RED}.get(action, DIM)

    lines.append(f" {BOLD}ANALYSIS{RESET}")
    lines.append(f"  Macro {rc}{regime}{RESET}  Daily {CYAN}{bias}{RESET}  4H {t4h}  1H {mc}{mode}{RESET}")

    pb_pct = pb.get("pullback_pct", 0) or 0
    pb_v = pb.get("pullback_valid", False)
    pb_s = f"{GREEN}VALID {pb_pct:.2f}%{RESET}" if pb_v else f"{pb_pct:.2f}%"
    trade_type = analysis.get("trade_type", "trend")
    tt_label = f"  {MAGENTA}[RANGE]{RESET}" if trade_type == "range" else ""

    action_badge = f"{ac}{BOLD}{action}{RESET}"
    if action == "ENTRY_READY":
        action_badge = f"{BG_GREEN} {action} {BG_RESET}"
    elif action == "NO_TRADE":
        action_badge = f"{BG_RED} {action} {BG_RESET}"
    lines.append(f"  5M pullback {pb_s}    Signal {action_badge}{tt_label}")

    rng = analysis.get("range_levels", {})
    if rng.get("valid") and regime == "SIDEWAYS":
        pos_r = rng.get("position_in_range", 0)
        bz = f"{BG_GREEN} BUY ZONE {BG_RESET}" if rng.get("buy_zone") else ""
        sz = f"{BG_RED} SELL ZONE {BG_RESET}" if rng.get("sell_zone") else ""
        zone = bz or sz or f"{DIM}mid-range{RESET}"
        bar_w = 20
        marker = max(0, min(bar_w - 1, int(pos_r * bar_w)))
        range_bar = "░" * marker + "█" + "░" * (bar_w - marker - 1)
        lines.append(
            f"  {DIM}Range{RESET} {rng['range_low']:,.0f}–{rng['range_high']:,.0f}  "
            f"{CYAN}{range_bar}{RESET}  {pos_r:.0%}  {zone}"
        )

    mode_ind = analysis.get("market_mode", {}).get("indicators", {})
    parts = []
    for k in ("ema_20", "ema_50", "ema_200"):
        v = mode_ind.get(k)
        if v is not None:
            parts.append(f"{k.upper()} {v:,.0f}")
    rsi = mode_ind.get("rsi_14")
    if rsi is not None:
        rc2 = GREEN if rsi > 50 else RED if rsi < 40 else YELLOW
        parts.append(f"RSI {rc2}{rsi:.1f}{RESET}")
    atr = mode_ind.get("atr_14")
    if atr is not None:
        parts.append(f"ATR {atr:.0f}")
    if parts:
        lines.append(f"  {DIM}1H:{RESET} {'  '.join(parts)}")
    lines.append("")

    # ── Confidence ───────────────────────────────────────────────────
    lines.append(f" {BOLD}CONFIDENCE{RESET}")
    lines.append(f"  BUY  {_bar(conf['buy_pct'])}")
    lines.append(f"  SELL {_bar(conf['sell_pct'])}")
    for d in conf["details"]:
        lines.append(f"    {DIM}{d}{RESET}")
    lines.append("")

    # ── Strategy reasons ─────────────────────────────────────────────
    reasons = conf.get("reasons", [])
    if reasons:
        lines.append(f" {BOLD}STRATEGY{RESET}")
        for r in reasons[:5]:
            lines.append(f"  • {r}")
        lines.append("")

    # ── Live event feed ──────────────────────────────────────────────
    if feed and feed.events:
        lines.append(f" {BOLD}LIVE FEED{RESET}")
        lines.extend(feed.render(10))
        lines.append("")

    # ── Closed orders / trade log ───────────────────────────────────
    if paper_mode and wallet.trades:
        cum_pnl = sum(t.pnl for t in wallet.trades)
        cum_c = GREEN if cum_pnl >= 0 else RED
        lines.append(
            f" {BOLD}CLOSED TRADES{RESET}  "
            f"({len(wallet.trades)} total, net {cum_c}{BOLD}{cum_pnl:+.4f} USDT{RESET})"
        )
        lines.append(
            f"  {'TIME':^17}  {'ENTRY':>8}  {'EXIT':>8}  "
            f"{'P&L':>10}  {'%':>7}  {'TYPE':^10}"
        )
        for t in wallet.trades[-8:]:
            tc = GREEN if t.pnl > 0 else RED
            badge = f"{BG_GREEN}WIN {BG_RESET}" if t.pnl > 0 else f"{BG_RED}LOSS{BG_RESET}"
            lines.append(
                f"  {t.entry_time}→{t.exit_time}  "
                f"{t.entry_price:>8,.0f}  {t.exit_price:>8,.0f}  "
                f"{tc}{t.pnl:>+10.4f}{RESET}  {tc}{t.pnl_pct:>+6.2f}%{RESET}  "
                f"{badge} {t.exit_reason}"
            )
        lines.append("")

    # ── Footer ───────────────────────────────────────────────────────
    lines.append(sep)
    label = "PAPER — NO REAL ORDERS" if paper_mode else "NO ORDERS PLACED"
    tick_info = f"  tick #{tracker.tick_count}" if tracker else ""
    lines.append(f" {DIM}Ctrl+C to stop  •  {label}{tick_info}{RESET}")

    sys.stdout.write(CLEAR + "\n".join(lines) + "\n")
    sys.stdout.flush()

    if paper_mode:
        write_paper_dashboard_json(
            price, analysis, conf, bt, wallet, start_time, tracker, feed
        )


# ── Paper trade logic ────────────────────────────────────────────────

def paper_tick(
    wallet: PaperWallet,
    price: float,
    analysis: dict,
) -> str:
    """Simulate one tick. Returns event string or empty."""
    wallet.update_drawdown(price)

    if wallet.in_position:
        pnl_pct = (price - wallet.entry_price) / wallet.entry_price * 100
        is_range = wallet.trade_type == "range"

        tp = 0.3 if is_range else config.TAKE_PROFIT_PCT
        sl = 0.2 if is_range else config.STOP_LOSS_PCT

        if is_range and wallet.range_tp_target > 0 and price >= wallet.range_tp_target:
            return wallet.sell(price, "RANGE_TP")

        if pnl_pct >= tp:
            return wallet.sell(price, "TP")

        if pnl_pct <= -sl:
            return wallet.sell(price, "SL")

        mode = analysis.get("market_mode", {}).get("mode", "WAIT")
        macro = analysis.get("macro_regime", {}).get("regime", "UNKNOWN")

        if is_range:
            rng = analysis.get("range_levels", {})
            if rng.get("sell_zone", False):
                return wallet.sell(price, "RANGE_RESIST")
        else:
            if mode == "DOWN" and macro != "BULL_RUN":
                return wallet.sell(price, "MODE_DOWN")

        if macro == "BEARISH":
            return wallet.sell(price, "MACRO_BEAR")

        return ""

    cooldown = 300 if analysis.get("trade_type") == "range" else config.COOLDOWN_SEC
    if time.time() - wallet.last_sell_ts < cooldown:
        return ""

    action = analysis.get("action", "WAIT")
    if action != "ENTRY_READY":
        return ""

    trade_type = analysis.get("trade_type", "trend")
    size_mod = analysis.get("position_size_modifier", 1.0)
    usdt_amount = config.TRADE_SIZE_USDT * size_mod
    if usdt_amount > wallet.usdt:
        usdt_amount = wallet.usdt
    if usdt_amount < 5:
        return ""

    entry_p = analysis.get("suggested_entry_price") or (price * 0.9998)
    mode = analysis.get("market_mode", {}).get("mode", "")

    wallet.trade_type = trade_type
    rng = analysis.get("range_levels", {})
    wallet.range_tp_target = rng.get("tp_target", 0) if trade_type == "range" else 0

    return wallet.buy(entry_p, usdt_amount, mode)


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run / paper trading")
    parser.add_argument("--days", type=int, default=14, help="Backtest period")
    parser.add_argument("--paper", action="store_true", help="Enable paper trading")
    parser.add_argument("--capital", type=float, default=1000.0,
                        help="Starting capital in USDT (paper mode)")
    args = parser.parse_args()

    paper_mode = args.paper
    mode_str = "PAPER TRADING" if paper_mode else "OBSERVATION ONLY"
    env_label = "MAINNET" if config.USE_MAINNET else "TESTNET"
    print(f"{BOLD}{mode_str}{RESET} — {config.SYMBOL} on {env_label}")
    if paper_mode:
        print(f"Starting with {args.capital:,.0f} USDT (simulated). No real orders.\n")
    else:
        print("No orders will be placed.\n")

    wallet = PaperWallet(
        usdt=args.capital, starting_capital=args.capital, peak_equity=args.capital,
    )

    # ── Backtest ─────────────────────────────────────────────────────
    print(f"Running {args.days}-day backtest...")
    bt = run_backtest(days=args.days, trade_size=config.TRADE_SIZE_USDT,
                      tp_pct=config.TAKE_PROFIT_PCT, sl_pct=config.STOP_LOSS_PCT)
    wr_c = GREEN if bt["win_rate"] >= 50 else RED
    print(
        f"  {bt['trades']} trades  WR {wr_c}{bt['win_rate']:.0f}%{RESET}  "
        f"P&L {bt['total_pnl']:+.2f}  Sharpe {bt['sharpe']:.1f}"
    )
    if bt["sample"]:
        for t in bt["sample"][-3:]:
            pc = GREEN if t["pnl"] > 0 else RED
            print(f"    {t['entry']}  {t['entry_p']:,.0f}→{t['exit_p']:,.0f}  "
                  f"{pc}{t['pnl']:+.3f}{RESET} ({t['reason']})")
    print()

    # ── Bootstrap engine ─────────────────────────────────────────────
    print("Bootstrapping live analysis...")
    engine = StrategyEngine()
    for interval, count in BOOTSTRAP_COUNTS.items():
        try:
            raw = _fetch(interval, count)
            for k in raw:
                engine.update_candle(interval, _parse_kline(k))
            print(f"  {interval}: {len(raw)} candles")
        except Exception as exc:
            print(f"  {interval}: {RED}failed — {exc}{RESET}")
    print()

    last_refresh = dict.fromkeys(REFRESH_INTERVALS, 0.0)
    start_time = time.time()
    last_event = "starting up…"
    tracker = PriceTracker(maxlen=60)
    feed = EventFeed(maxlen=15)
    feed.add("Engine bootstrapped — watching live market", "info")
    prev_action = ""
    prev_mode = ""
    tick_interval = 1

    # ── Live loop ────────────────────────────────────────────────────
    try:
        while True:
            try:
                price = float(market_data.get_price()["price"])
                tracker.update(price)

                now = time.time()
                for interval, every in REFRESH_INTERVALS.items():
                    if now - last_refresh.get(interval, 0) >= every:
                        try:
                            lim = 5 if interval in ("5m", "1h") else 3
                            raw = _fetch(interval, lim)
                            for k in raw:
                                engine.update_candle(interval, _parse_kline(k))
                            last_refresh[interval] = now
                        except Exception:
                            pass

                analysis = engine.get_full_analysis()
                conf = compute_confidence(analysis, bt, tracker)

                cur_action = analysis.get("action", "WAIT")
                cur_mode = analysis.get("market_mode", {}).get("mode", "")
                if cur_action != prev_action and prev_action:
                    lvl = "signal" if cur_action == "ENTRY_READY" else "info"
                    feed.add(f"Signal → {cur_action}", lvl)
                if cur_mode != prev_mode and prev_mode:
                    feed.add(f"1H mode → {cur_mode}", "warn")
                prev_action = cur_action
                prev_mode = cur_mode

                rng = analysis.get("range_levels", {})
                if rng.get("valid"):
                    pos_r = rng.get("position_in_range", 0)
                    if rng.get("buy_zone"):
                        feed.add(
                            f"Price in BUY ZONE ({pos_r:.0%} of range)", "buy"
                        )

                if paper_mode:
                    event = paper_tick(wallet, price, analysis)
                    if event:
                        last_event = event
                        log.info("PAPER: %s", event)
                        wallet.save()
                        if "BUY" in event:
                            feed.add(event, "buy")
                        elif "SELL" in event:
                            pnl_in_event = wallet.trades[-1].pnl if wallet.trades else 0
                            lvl = "tp" if pnl_in_event > 0 else "sl"
                            feed.add(event, lvl)

                render(
                    price, analysis, conf, bt, wallet, start_time,
                    paper_mode, tracker, feed,
                )

            except Exception as exc:
                sys.stdout.write(f"\n  {RED}Error: {exc}{RESET}\n")
                log.error("Loop: %s", exc, exc_info=True)
                feed.add(f"Error: {exc}", "warn")

            time.sleep(tick_interval)

    except KeyboardInterrupt:
        print(f"\n{sep}")
        if paper_mode:
            eq = wallet.equity(price) if 'price' in dir() else wallet.usdt
            wallet.save()
            print(f" {BOLD}Paper session ended.{RESET}")
            st = wallet.stats()
            pnl = eq - wallet.starting_capital
            pnl_c = GREEN if pnl >= 0 else RED
            print(f"  Capital: {wallet.starting_capital:,.0f} → {eq:,.2f} USDT  "
                  f"({pnl_c}{pnl:+.2f}{RESET})")
            print(f"  Trades: {st['trades']}  WR: {st['win_rate']:.0f}%  "
                  f"MaxDD: {wallet.max_drawdown:.2f}")
            if wallet.trades:
                print(f"\n {BOLD}All trades:{RESET}")
                for t in wallet.trades:
                    tc = GREEN if t.pnl > 0 else RED
                    print(f"  {t.entry_time}→{t.exit_time}  "
                          f"{t.entry_price:,.0f}→{t.exit_price:,.0f}  "
                          f"{tc}{t.pnl:+.4f}{RESET} ({t.pnl_pct:+.2f}%)  "
                          f"[{t.exit_reason}]")
        else:
            print(f" {BOLD}Stopped.{RESET}")
        print(f"\n {DIM}No real orders were placed.{RESET}")
        print(f" {DIM}State saved to {STATE_FILE}{RESET}")


if __name__ == "__main__":
    main()
