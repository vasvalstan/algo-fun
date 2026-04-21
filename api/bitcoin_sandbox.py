"""
BTC $60k–$85k sandbox grid (paper / backtest).

State machine from new-bitcoin-test.md:
  • Geofence: cancel limit buys outside band; keep sells; PAUSED + alert.
  • All-cash: trail local high, one limit buy at high * (1 - dip_pct).
  • On buy fill: bracket with sell at entry * (1 + tp_pct) and next buy at entry * (1 - dip_pct).
  • On sell fill (LIFO lot): cancel deepest grid buy, place new buy at sell * (1 - dip_pct).
  • Max tranches; reserve USDT not spent on new buys.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

MAKER_FEE = 0.001


@dataclass
class SandboxHolding:
    lot_id: int
    qty: float
    entry_price: float
    sell_limit: float
    cost_usdt: float
    buy_fee_usdt: float
    entry_ts: float


@dataclass
class SandboxClosedTrade:
    lot_id: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_usdt: float
    pnl_pct: float
    exit_time: str
    entry_time_iso: str
    exit_time_iso: str
    buy_fee_usdt: float
    sell_fee_usdt: float
    total_fees_usdt: float
    fee_pct_of_turnover: float
    maker_fee_leg_pct: float
    notional_entry_usdt: float
    gross_exit_usdt: float
    net_profit_usdt: float
    hold_seconds: float


@dataclass
class BitcoinSandboxParams:
    """Runtime tuning (defaults match blueprint)."""

    geofence_low: float = 60_000.0
    geofence_high: float = 85_000.0
    reserve_usdt: float = 1_000.0
    num_bullets: int = 26
    tp_pct: float = 0.71
    dip_pct: float = 0.75


class BitcoinSandboxState:
    """
    Paper simulation: limit fills when bar/tick range crosses resting prices.
    """

    def __init__(
        self,
        starting_usdt: float,
        params: BitcoinSandboxParams,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.p = params
        self.usdt = float(starting_usdt)
        self.btc = 0.0
        self.starting_capital = float(starting_usdt)
        self._notify = notify

        self.status: str = "ACTIVE"  # ACTIVE | PAUSED
        self.local_high: float = 0.0
        self.trailing_buy_price: Optional[float] = None
        self.grid_buy_price: Optional[float] = None

        self.holdings: list[SandboxHolding] = []
        self.closed_trades: list[SandboxClosedTrade] = []
        self.event_log: list[str] = []

        self._lot_seq = 0
        self._breach_notified = False
        self.peak_equity: float = float(starting_usdt)
        self.max_drawdown: float = 0.0

    def equity(self, price: float) -> float:
        return self.usdt + self.btc * price

    def update_drawdown(self, price: float) -> None:
        eq = self.equity(price)
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.peak_equity - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    @property
    def tranche_usdt(self) -> float:
        tradable = max(0.0, self.starting_capital - self.p.reserve_usdt)
        return max(5.0, tradable / max(self.p.num_bullets, 1))

    def _tradable_usdt(self) -> float:
        return max(0.0, self.usdt - self.p.reserve_usdt)

    def _log(self, msg: str, events: list[str]) -> None:
        events.append(msg)
        log.info("BITCOIN_SANDBOX %s", msg)

    def _tp_mult(self) -> float:
        return 1.0 + self.p.tp_pct / 100.0

    def _dip_mult(self) -> float:
        return 1.0 - self.p.dip_pct / 100.0

    def _in_geofence(self, price: float) -> bool:
        return self.p.geofence_low <= price <= self.p.geofence_high

    def _cancel_all_buys(self, events: list[str]) -> None:
        if self.trailing_buy_price is not None or self.grid_buy_price is not None:
            self._log(
                "CANCEL resting buy(s): trailing=%s grid=%s"
                % (self.trailing_buy_price, self.grid_buy_price),
                events,
            )
        self.trailing_buy_price = None
        self.grid_buy_price = None

    def _geofence_step(self, price: float, events: list[str]) -> None:
        if self._in_geofence(price):
            if self.status == "PAUSED":
                self.status = "ACTIVE"
                self._breach_notified = False
                self._log(
                    "RESUME price back inside geofence (%.2f). Re-arming logic."
                    % price,
                    events,
                )
                if not self.holdings:
                    self.local_high = price
                    self.trailing_buy_price = self.local_high * self._dip_mult()
                    self._log(
                        "TRAILING BUY @ %.2f (high %.2f - dip)"
                        % (self.trailing_buy_price, self.local_high),
                        events,
                    )
            return

        self.status = "PAUSED"
        self._cancel_all_buys(events)
        if not self._breach_notified:
            self._breach_notified = True
            msg = (
                "🚨 GEOFENCE BREACH price=%.2f (allowed %.0f–%.0f). "
                "Buys halted; sells left working."
                % (price, self.p.geofence_low, self.p.geofence_high)
            )
            self._log(msg, events)
            if self._notify:
                try:
                    self._notify(msg)
                except Exception:
                    pass

    def _max_holdings(self) -> int:
        return max(1, self.p.num_bullets)

    def _execute_buy(
        self,
        fill_price: float,
        events: list[str],
        tag: str,
        *,
        skip_status_check: bool = False,
    ) -> bool:
        if not skip_status_check and self.status != "ACTIVE":
            return False
        size = min(self.tranche_usdt, self._tradable_usdt())
        if size < 5.0:
            self._log("SKIP BUY %s: tradable USDT %.2f below min" % (tag, self._tradable_usdt()), events)
            return False
        if len(self.holdings) >= self._max_holdings():
            self._log("SKIP BUY %s: max %d lots" % (tag, self._max_holdings()), events)
            return False

        fee = size * MAKER_FEE
        net = size - fee
        qty = net / fill_price
        self.usdt -= size
        self.btc += qty
        self._lot_seq += 1
        sell_limit = round(fill_price * self._tp_mult(), 2)
        h = SandboxHolding(
            lot_id=self._lot_seq,
            qty=qty,
            entry_price=fill_price,
            sell_limit=sell_limit,
            cost_usdt=size,
            buy_fee_usdt=fee,
            entry_ts=time.time(),
        )
        self.holdings.append(h)
        self._log(
            "BUY [%s] lot #%d %.8f BTC @ %.2f  (%.2f USDT, fee %.4f) → TP limit %.2f"
            % (tag, h.lot_id, qty, fill_price, size, fee, sell_limit),
            events,
        )

        self.trailing_buy_price = None
        self.grid_buy_price = round(fill_price * self._dip_mult(), 2)
        self._log("GRID BUY placed @ %.2f (below fill %.2f)" % (self.grid_buy_price, fill_price), events)
        return True

    def manual_buy_at_price(
        self,
        fill_price: float,
        *,
        force: bool = False,
    ) -> tuple[bool, str, list[str]]:
        """
        UI / API: simulate an immediate buy at ``fill_price`` (paper), same tranche sizing as grid.

        * ``force=False`` (default): only when sandbox is ACTIVE and price is inside the geofence.
        * ``force=True``: bypass ACTIVE + geofence (still enforces reserve / max lots / min size).
        """
        events: list[str] = []
        if not force:
            if self.status != "ACTIVE":
                return False, "Sandbox is not ACTIVE (e.g. geofence pause). Use force=true to override.", events
            if not self._in_geofence(fill_price):
                return (
                    False,
                    "Price outside geofence. Use force=true to test anyway.",
                    events,
                )
        ok = self._execute_buy(
            fill_price,
            events,
            "MANUAL_UI",
            skip_status_check=bool(force),
        )
        if not ok:
            detail = events[-1] if events else "Buy did not execute (see logs)."
            return False, detail, events
        return True, "Manual paper buy filled.", events

    def orders_snapshot(self, mark_price: float) -> dict[str, Any]:
        """Rich snapshot: pending buys, active lots (with unrealized), and closed trades."""
        now_ts = time.time()

        pending_buys: list[dict[str, Any]] = []
        if self.trailing_buy_price is not None:
            dist = mark_price - self.trailing_buy_price
            dist_pct = dist / mark_price * 100 if mark_price else 0
            pending_buys.append(
                {
                    "side": "BUY",
                    "kind": "trailing",
                    "price": round(self.trailing_buy_price, 2),
                    "distance_usdt": round(dist, 2),
                    "distance_pct": round(dist_pct, 3),
                    "tranche_usdt": round(self.tranche_usdt, 2),
                    "status": "PENDING",
                }
            )
        if self.grid_buy_price is not None:
            dist = mark_price - self.grid_buy_price
            dist_pct = dist / mark_price * 100 if mark_price else 0
            pending_buys.append(
                {
                    "side": "BUY",
                    "kind": "grid",
                    "price": round(self.grid_buy_price, 2),
                    "distance_usdt": round(dist, 2),
                    "distance_pct": round(dist_pct, 3),
                    "tranche_usdt": round(self.tranche_usdt, 2),
                    "status": "PENDING",
                }
            )

        active_sells: list[dict[str, Any]] = []
        for h in self.holdings:
            ur_pct = (mark_price - h.entry_price) / h.entry_price * 100 if h.entry_price else 0
            ur_usdt = h.qty * (mark_price - h.entry_price)
            dist_to_tp = h.sell_limit - mark_price
            dist_to_tp_pct = dist_to_tp / mark_price * 100 if mark_price else 0
            hold_s = max(0.0, now_ts - h.entry_ts)
            entry_iso = datetime.fromtimestamp(h.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            active_sells.append(
                {
                    "side": "SELL",
                    "kind": "take_profit",
                    "lot_id": h.lot_id,
                    "tp_price": round(h.sell_limit, 2),
                    "entry_price": round(h.entry_price, 2),
                    "entry_time_iso": entry_iso,
                    "qty": round(h.qty, 8),
                    "cost_usdt": round(h.cost_usdt, 4),
                    "buy_fee_usdt": round(h.buy_fee_usdt, 6),
                    "unrealized_pct": round(ur_pct, 4),
                    "unrealized_usdt": round(ur_usdt, 6),
                    "distance_to_tp_usdt": round(dist_to_tp, 2),
                    "distance_to_tp_pct": round(dist_to_tp_pct, 3),
                    "hold_seconds": round(hold_s, 1),
                    "status": "ACTIVE",
                }
            )

        closed = [asdict(t) for t in self.closed_trades[-30:]]

        total_closed_pnl = sum(t.net_profit_usdt for t in self.closed_trades)
        total_closed_fees = sum(t.total_fees_usdt for t in self.closed_trades)

        return {
            "strategy_id": "bitcoin_sandbox",
            "sandbox_status": self.status,
            "mark_price": round(mark_price, 2),
            "geofence_low": self.p.geofence_low,
            "geofence_high": self.p.geofence_high,
            "usdt": round(self.usdt, 4),
            "btc": round(self.btc, 8),
            "equity_usdt": round(self.equity(mark_price), 4),
            "starting_capital": round(self.starting_capital, 2),
            "reserve_usdt": self.p.reserve_usdt,
            "tradable_usdt": round(self._tradable_usdt(), 4),
            "tranche_usdt": round(self.tranche_usdt, 2),
            "num_bullets": self.p.num_bullets,
            "tp_pct": self.p.tp_pct,
            "dip_pct": self.p.dip_pct,
            "pending_buys": pending_buys,
            "active_sells": active_sells,
            "open_lots": len(self.holdings),
            "total_closed": len(self.closed_trades),
            "total_closed_pnl": round(total_closed_pnl, 4),
            "total_closed_fees": round(total_closed_fees, 4),
            "peak_equity": round(self.peak_equity, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "closed_trades": closed,
        }

    def _execute_sell(self, h: SandboxHolding, fill_price: float, events: list[str]) -> None:
        gross = h.qty * fill_price
        sell_fee = gross * MAKER_FEE
        net = gross - sell_fee
        pnl = net - h.cost_usdt
        pnl_pct = (fill_price - h.entry_price) / h.entry_price * 100.0 if h.entry_price else 0.0
        buy_fee = h.buy_fee_usdt
        total_fees = buy_fee + sell_fee
        turnover = h.cost_usdt + gross
        fee_pct_turn = (100.0 * total_fees / turnover) if turnover > 0 else 0.0
        now_ts = time.time()
        hold_s = max(0.0, now_ts - h.entry_ts)
        entry_iso = datetime.fromtimestamp(h.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        exit_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.usdt += net
        self.btc -= h.qty
        ct = SandboxClosedTrade(
            lot_id=h.lot_id,
            entry_price=h.entry_price,
            exit_price=fill_price,
            qty=h.qty,
            pnl_usdt=pnl,
            pnl_pct=pnl_pct,
            exit_time=time.strftime("%H:%M:%S"),
            entry_time_iso=entry_iso,
            exit_time_iso=exit_iso,
            buy_fee_usdt=buy_fee,
            sell_fee_usdt=sell_fee,
            total_fees_usdt=total_fees,
            fee_pct_of_turnover=fee_pct_turn,
            maker_fee_leg_pct=MAKER_FEE * 100.0,
            notional_entry_usdt=h.cost_usdt,
            gross_exit_usdt=gross,
            net_profit_usdt=pnl,
            hold_seconds=hold_s,
        )
        self.closed_trades.append(ct)
        self.holdings.remove(h)
        sig = "+" if pnl >= 0 else ""
        self._log(
            "SELL lot #%d %.8f BTC @ %.2f  %s%.4f USDT (%.2f%% vs cost)"
            % (h.lot_id, h.qty, fill_price, sig, pnl, pnl_pct),
            events,
        )

        self.grid_buy_price = round(fill_price * self._dip_mult(), 2)
        self._log("GRID BUY (post-sell) @ %.2f from exit %.2f" % (self.grid_buy_price, fill_price), events)

        if not self.holdings:
            self._cancel_all_buys(events)
            self.local_high = fill_price
            self.trailing_buy_price = round(self.local_high * self._dip_mult(), 2)
            self._log(
                "ALL FLAT — reset Step 1: high %.2f → TRAILING BUY @ %.2f"
                % (self.local_high, self.trailing_buy_price),
                events,
            )

    def _fill_sells(self, hi: float, events: list[str]) -> None:
        for h in list(self.holdings):
            if hi >= h.sell_limit:
                self._execute_sell(h, h.sell_limit, events)

    def _fill_buys(self, lo: float, events: list[str]) -> None:
        if self.status != "ACTIVE":
            return
        # Trailing buy (flat)
        if not self.holdings and self.trailing_buy_price is not None and lo <= self.trailing_buy_price:
            self._execute_buy(self.trailing_buy_price, events, "TRAIL")
            return
        # Grid buy
        if self.holdings and self.grid_buy_price is not None and lo <= self.grid_buy_price:
            self._execute_buy(self.grid_buy_price, events, "GRID")

    def _update_trailing_high(self, cur: float, events: list[str]) -> None:
        if self.status != "ACTIVE":
            return
        if self.holdings:
            return
        if self.local_high <= 0:
            self.local_high = cur
            self.trailing_buy_price = round(self.local_high * self._dip_mult(), 2)
            self._log(
                "INIT high=%.2f TRAILING BUY @ %.2f" % (self.local_high, self.trailing_buy_price),
                events,
            )
            return
        if cur > self.local_high:
            self.local_high = cur
            old = self.trailing_buy_price
            self.trailing_buy_price = round(self.local_high * self._dip_mult(), 2)
            self._log(
                "TRAIL REPRICE high %.2f → buy %.2f (was %s)"
                % (self.local_high, self.trailing_buy_price, old),
                events,
            )

    def tick_bar(
        self,
        low: float,
        high: float,
        close: float,
        events: Optional[list[str]] = None,
    ) -> list[str]:
        """One OHLC bar: geofence on close; fills use low/high."""
        ev = events if events is not None else []
        self._geofence_step(close, ev)
        if self.status == "ACTIVE":
            self._update_trailing_high(close, ev)
        self._fill_sells(high, ev)
        self._fill_buys(low, ev)
        return ev

    def tick_live(self, prev_price: float, cur_price: float, events: Optional[list[str]] = None) -> list[str]:
        """Live tick: synthetic bar from prev→cur."""
        lo, hi = min(prev_price, cur_price), max(prev_price, cur_price)
        ev = events if events is not None else []
        self._geofence_step(cur_price, ev)
        if self.status == "ACTIVE":
            self._update_trailing_high(cur_price, ev)
        self._fill_sells(hi, ev)
        self._fill_buys(lo, ev)
        return ev

    def to_analysis_dict(self, price: float) -> dict[str, Any]:
        layers_display: list[dict[str, Any]] = []
        for h in self.holdings:
            layers_display.append(
                {
                    "lot_id": h.lot_id,
                    "status": "HOLDING_BTC",
                    "buy_filled": round(h.entry_price, 2),
                    "sell_resting": round(h.sell_limit, 2),
                    "qty": round(h.qty, 8),
                }
            )
        if self.grid_buy_price is not None:
            layers_display.append(
                {
                    "lot_id": "grid",
                    "status": "WAITING_TO_BUY",
                    "buy_resting": self.grid_buy_price,
                    "sell_resting": None,
                }
            )
        elif self.trailing_buy_price is not None and not self.holdings:
            layers_display.append(
                {
                    "lot_id": "trail",
                    "status": "WAITING_TO_BUY",
                    "buy_resting": self.trailing_buy_price,
                    "sell_resting": None,
                }
            )

        reasons = [
            "geofence %.0f–%.0f" % (self.p.geofence_low, self.p.geofence_high),
            "status=%s" % self.status,
            "reserve=%.0f USDT" % self.p.reserve_usdt,
            "tranche≈%.2f USDT" % self.tranche_usdt,
        ]

        return {
            "action": "ACTIVE" if self.status == "ACTIVE" else "PAUSED",
            "reasons": reasons,
            "layers": [],
            "indicators": {
                "sandbox": {
                    "local_high": round(self.local_high, 2),
                    "trailing_buy": self.trailing_buy_price,
                    "grid_buy": self.grid_buy_price,
                    "open_lots": len(self.holdings),
                    "closed_trades": len(self.closed_trades),
                    "layers_preview": layers_display,
                }
            },
        }


def sandbox_params_from_pydantic(m: Any) -> BitcoinSandboxParams:
    """Build runtime params from strategy_params.BitcoinSandboxParamsModel."""
    return BitcoinSandboxParams(
        geofence_low=float(m.geofence_low),
        geofence_high=float(m.geofence_high),
        reserve_usdt=float(m.reserve_usdt),
        num_bullets=int(m.num_bullets),
        tp_pct=float(m.tp_pct),
        dip_pct=float(m.dip_pct),
    )
