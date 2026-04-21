"""
Top-down trend-aware maker strategy for BTCUSDT Spot.

Analyses 6 timeframes (Monthly → Weekly → Daily → 4H → 1H → 5M) using
the StrategyEngine from indicators.py.  Only enters when the higher-
timeframe trend supports the direction.  All entries and exits use
Post Only (LIMIT_MAKER) orders.

Replaces the old MeanReversionStrategy but keeps the same public interface
for compatibility with bot.py, dashboard.py, and state_writer.py.

Balance vs signal:
  • Buys: multi-timeframe signal must be ENTRY_READY, then we require enough
    free USDT for TRADE_SIZE_USDT (via get_account, cached briefly per tick).
  • Sells: only for the bot-managed position size (slot_qty). We require
    enough free base on the exchange before TP or emergency sells. Whether
    to exit early still follows price vs SL and macro/mode rules — not a
    second “wallet BTC” opinion for coins left in analyze_first mode.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Deque, List, Optional

import config
import ledger
import market_data
import trading
from indicators import StrategyEngine

log = logging.getLogger(__name__)


# ── How often to refresh each timeframe (seconds) ───────────────────

KLINE_REFRESH = {
    "5m": 30,
    "1h": 300,
    "4h": 900,
    "1d": 1800,
    "1w": 3600,
    "1M": 3600,
}

BOOTSTRAP_COUNTS = {
    "5m": 200,
    "1h": 250,
    "4h": 60,
    "1d": 250,
    "1w": 250,
    "1M": 100,
}


# ── Types (kept compatible with dashboard / state_writer) ────────────


class State(Enum):
    WATCHING = auto()
    BUY_PLACED = auto()
    HOLDING = auto()
    SELL_PLACED = auto()


@dataclass
class Cycle:
    number: int
    slot_id: int
    buy_price: float
    sell_price: float
    quantity: float
    gross_pct: float
    fee_estimate: float
    net_pnl: float
    timestamp: float


@dataclass
class OpenOrder:
    order_id: int
    side: str
    price: float
    quantity: float
    placed_at: float


@dataclass
class Position:
    slot_id: int
    state: State = State.WATCHING
    open_order: Optional[OpenOrder] = None
    entry_price: float = 0.0
    slot_qty: Optional[float] = None


def _parse_rest_kline(raw: list) -> dict:
    return {
        "open_time": raw[0],
        "close_time": raw[6],
        "open": raw[1],
        "high": raw[2],
        "low": raw[3],
        "close": raw[4],
        "volume": raw[5],
        "quote_volume": raw[7],
        "trades": raw[8],
        "is_closed": True,
    }


# ── Strategy ─────────────────────────────────────────────────────────


class TrendAwareMakerStrategy:
    """
    Top-down trend-aware maker strategy.

    Public attrs (dashboard-compatible):
        positions, prices, ma, current_price,
        cycles, total_fees, starting_balance, current_balance, errors,
        market_mode, macro_regime, daily_bias, action, last_analysis
    """

    def __init__(
        self,
        price_precision: int = 2,
        qty_precision: int = 5,
        starting_balance: float = 0.0,
        num_slots: int = 1,
    ) -> None:
        self.engine = StrategyEngine()
        self.positions: List[Position] = [Position(slot_id=0)]

        self.prices: Deque[float] = deque(maxlen=config.MA_WINDOW)
        self.ma: Optional[float] = None
        self.current_price: float = 0.0

        self.cycles: List[Cycle] = []
        self.total_fees: float = 0.0
        self.starting_balance: float = starting_balance
        self.current_balance: float = starting_balance
        self.session_equity_usdt: float = starting_balance

        self.price_precision: int = price_precision
        self.qty_precision: int = qty_precision

        self.errors: List[str] = []
        self.ledger: ledger.LedgerTotals = ledger.load()

        self.market_mode: str = "WAIT"
        self.macro_regime: str = "UNKNOWN"
        self.daily_bias: str = "UNKNOWN"
        self.action: str = "WAIT"
        self.last_analysis: Optional[dict] = None

        self.on_trade_intercepted = None  # async callback(strategy, side, price, qty, size_usdt)

        self._last_sell_time: float = 0
        self._last_kline_refresh: dict = dict.fromkeys(KLINE_REFRESH, 0.0)
        self._bootstrapped: bool = False

        # Free base on the exchange not adopted into the slot (analyze_first policy)
        self.wallet_base_qty: float = 0.0
        self.last_entry_block_reason: Optional[str] = None
        self.last_sell_block_reason: Optional[str] = None

        # Short TTL cache so one tick can check USDT + base without duplicate /account calls
        self._bal_snap: Optional[dict[str, tuple[float, float]]] = None
        self._bal_snap_ts: float = 0.0

    # ── Bootstrap ────────────────────────────────────────────────────

    def bootstrap(self) -> str:
        """Fetch historical klines for all timeframes. Call once on startup."""
        log.info("Bootstrapping historical klines...")
        results = []
        for interval, count in BOOTSTRAP_COUNTS.items():
            try:
                klines = market_data.get_klines(interval=interval, limit=count)
                loaded = 0
                for k in klines:
                    self.engine.update_candle(interval, _parse_rest_kline(k))
                    loaded += 1
                results.append(f"{interval}:{loaded}")
                self._last_kline_refresh[interval] = time.time()
                log.info("Bootstrapped %d %s candles", loaded, interval)
            except Exception as exc:
                log.error("Failed to bootstrap %s: %s", interval, exc)
                self.errors.append(f"bootstrap {interval}: {exc}")
        self._bootstrapped = True
        try:
            self.last_analysis = self.engine.get_full_analysis()
            self._sync_state()
        except Exception:
            pass
        return f"Bootstrapped {', '.join(results)}"

    # ── Kline refresh ────────────────────────────────────────────────

    def refresh_klines_if_due(self) -> Optional[str]:
        if not self._bootstrapped:
            return None
        now = time.time()
        refreshed = []
        for interval, refresh_sec in KLINE_REFRESH.items():
            if now - self._last_kline_refresh.get(interval, 0) >= refresh_sec:
                try:
                    limit = 5 if interval in ("5m", "1h") else 3
                    klines = market_data.get_klines(interval=interval, limit=limit)
                    for k in klines:
                        self.engine.update_candle(interval, _parse_rest_kline(k))
                    self._last_kline_refresh[interval] = now
                    refreshed.append(interval)
                except Exception as exc:
                    log.debug("Kline refresh %s failed: %s", interval, exc)
        if refreshed:
            try:
                self.last_analysis = self.engine.get_full_analysis()
                self._sync_state()
            except Exception as exc:
                log.debug("Analysis update failed: %s", exc)
        return None

    def _sync_state(self) -> None:
        if not self.last_analysis:
            return
        self.action = self.last_analysis.get("action", "WAIT")
        mode_data = self.last_analysis.get("market_mode", {})
        self.market_mode = mode_data.get("mode", "WAIT")
        macro_data = self.last_analysis.get("macro_regime", {})
        self.macro_regime = macro_data.get("regime", "UNKNOWN")
        daily_data = self.last_analysis.get("daily_bias", {})
        self.daily_bias = daily_data.get("bias", "UNKNOWN")
        ema20 = mode_data.get("indicators", {}).get("ema_20")
        if ema20:
            self.ma = ema20

    # ── Startup recovery ─────────────────────────────────────────────

    def format_analysis_summary(self) -> str:
        """Human-readable snapshot from the last full analysis (after bootstrap)."""
        if not self.last_analysis:
            return "Analysis not available yet."
        a = self.last_analysis
        t4 = a.get("trend_4h") or {}
        pb = a.get("pullback_5m") or {}
        lines = [
            f"Macro regime: {self.macro_regime}",
            f"Daily bias:   {self.daily_bias}",
            f"4H trend:     {t4.get('trend', 'unknown')}",
            f"1H mode:      {self.market_mode}",
            f"5M pullback:  valid={pb.get('pullback_valid', False)}  "
            f"({pb.get('pullback_pct', 0) or 0:.2f}%)",
            f"Signal:       {self.action}",
        ]
        reasons = a.get("reasons") or []
        if reasons:
            lines.append("Context:")
            for r in reasons[:8]:
                lines.append(f"  • {r}")
        return "\n".join(lines)

    def reconcile_wallet_btc(
        self,
        base_qty: float,
        spot_price: float,
        min_notional: float,
    ) -> str:
        """
        After bootstrap: decide how to treat BTC already in the wallet.

        Default (analyze_first): track as wallet_base_qty, do not force a sell.
        manage_immediately: same as legacy recover_position.
        """
        pos = self.positions[0]
        notion = base_qty * spot_price
        if base_qty <= 0 or notion < min_notional:
            self.wallet_base_qty = 0.0
            return ""

        policy = config.WALLET_BTC_POLICY
        if policy not in ("analyze_first", "manage_immediately"):
            policy = "analyze_first"

        if pos.state != State.WATCHING:
            self.wallet_base_qty = base_qty
            return (
                f"Wallet ~{base_qty} BTC noted; slot busy ({pos.state.name}) — "
                "not adopting into managed position"
            )

        if policy == "manage_immediately":
            msg = self.recover_position(base_qty, spot_price)
            self.wallet_base_qty = 0.0
            return msg or ""

        self.wallet_base_qty = base_qty
        return (
            f"Wallet ~{base_qty} BTC — policy=analyze_first: "
            "no auto-sell; bot will buy more on ENTRY_READY if you have USDT"
        )

    def recover_position(self, qty: float, estimated_price: float) -> Optional[str]:
        pos = self.positions[0]
        if pos.state != State.WATCHING:
            return None
        pos.state = State.HOLDING
        pos.entry_price = estimated_price
        pos.slot_qty = qty
        return (
            f"recovered {qty} @ ~{estimated_price:.{self.price_precision}f} "
            f"(will sell at +{config.TAKE_PROFIT_PCT}%)"
        )

    def recover_open_order(
        self, order_id: int, side: str, price: float, quantity: float
    ) -> Optional[str]:
        pos = self.positions[0]
        if pos.state != State.WATCHING:
            return None
        if side == "SELL":
            pos.state = State.SELL_PLACED
            pos.entry_price = price / (1 + config.TAKE_PROFIT_PCT / 100)
            pos.slot_qty = quantity
        else:
            pos.state = State.BUY_PLACED
        pos.open_order = OpenOrder(
            order_id=order_id, side=side, price=price,
            quantity=quantity, placed_at=time.time(),
        )
        return f"adopted {side} order #{order_id} @ {price:.{self.price_precision}f}"

    # ── Dashboard-compat properties ──────────────────────────────────

    @property
    def state(self) -> State:
        return self.positions[0].state

    @property
    def entry_price(self) -> float:
        return self.positions[0].entry_price

    @property
    def open_order(self) -> Optional[OpenOrder]:
        return self.positions[0].open_order

    # ── Main tick ────────────────────────────────────────────────────

    def tick(self, current_price: float) -> Optional[str]:
        self.current_price = current_price
        self.prices.append(current_price)

        self.refresh_klines_if_due()

        pos = self.positions[0]
        open_ids = self._fetch_open_ids()

        if pos.state == State.BUY_PLACED:
            return self._handle_buy_placed(pos, open_ids)
        if pos.state == State.HOLDING:
            return self._handle_holding(pos)
        if pos.state == State.SELL_PLACED:
            return self._handle_sell_placed(pos, open_ids)
        return self._handle_watching(pos)

    # ── WATCHING: should we enter? ───────────────────────────────────

    @staticmethod
    def _base_asset_symbol() -> str:
        return config.SYMBOL.replace("USDT", "").replace("BUSD", "").strip() or "BTC"

    def _balances(self) -> dict[str, tuple[float, float]]:
        """free, locked per asset. Cached ~2s to pair USDT + base checks in one tick."""
        now = time.time()
        if self._bal_snap is not None and now - self._bal_snap_ts < 2.0:
            return self._bal_snap
        try:
            acct = trading.get_account()
            snap: dict[str, tuple[float, float]] = {}
            for b in acct.get("balances", []):
                snap[b["asset"]] = (float(b["free"]), float(b["locked"]))
            self._bal_snap = snap
            self._bal_snap_ts = now
            return snap
        except Exception as exc:
            log.debug("get_account failed: %s", exc)
            return {}

    def _usdt_free(self) -> float:
        return self._balances().get("USDT", (0.0, 0.0))[0]

    def _base_free(self) -> float:
        return self._balances().get(self._base_asset_symbol(), (0.0, 0.0))[0]

    def _base_locked(self) -> float:
        return self._balances().get(self._base_asset_symbol(), (0.0, 0.0))[1]

    def _can_sell_base_qty(self, qty: float) -> bool:
        """True if free balance can cover a new sell order of this size."""
        if qty <= 0:
            return False
        return self._base_free() + 1e-12 >= qty

    def _handle_watching(self, pos: Position) -> Optional[str]:
        self.last_entry_block_reason = None
        self.last_sell_block_reason = None

        if not self.last_analysis:
            return f"mode:{self.market_mode} — warming up"

        if time.time() - self._last_sell_time < config.COOLDOWN_SEC:
            remaining = int(config.COOLDOWN_SEC - (time.time() - self._last_sell_time))
            return f"mode:{self.market_mode} — cooldown {remaining}s"

        if self.action != "ENTRY_READY":
            return None

        entry_price = self.last_analysis.get("suggested_entry_price")
        if not entry_price:
            entry_price = self.current_price * 0.9998

        size_mod = self.last_analysis.get("position_size_modifier", 1.0)
        usdt_amount = max(config.TRADE_SIZE_USDT * size_mod, 6.0)
        need_usdt = usdt_amount * 1.005
        free_usdt = self._usdt_free()
        if free_usdt < need_usdt:
            self.last_entry_block_reason = (
                f"ENTRY_READY but need ~{need_usdt:.0f} USDT free "
                f"(have {free_usdt:.2f})"
            )
            return None

        quantity = usdt_amount / entry_price

        price_str = f"{entry_price:.{self.price_precision}f}"
        qty_str = f"{quantity:.{self.qty_precision}f}"

        if self.on_trade_intercepted is not None:
            self.on_trade_intercepted(
                "TrendAwareMaker", "BUY", float(price_str), float(qty_str), usdt_amount
            )
            pos.open_order = OpenOrder(
                order_id=0, side="BUY",
                price=float(price_str), quantity=float(qty_str),
                placed_at=time.time(),
            )
            pos.state = State.BUY_PLACED
            return f"APPROVAL REQUESTED: BUY @ {price_str} ({usdt_amount:.0f} USDT)"

        try:
            resp = trading.place_maker_order(
                side="BUY", quantity=qty_str, price=price_str,
            )
            pos.open_order = OpenOrder(
                order_id=resp["orderId"], side="BUY",
                price=float(price_str), quantity=float(qty_str),
                placed_at=time.time(),
            )
            pos.state = State.BUY_PLACED
            return f"MAKER BUY @ {price_str} ({usdt_amount:.0f} USDT, {size_mod:.0%})"
        except Exception as exc:
            err = str(exc)
            if "would immediately" in err.lower() or "-2010" in err:
                return None
            self.errors.append(f"buy failed: {exc}")
            return f"BUY failed: {exc}"

    # ── BUY_PLACED: waiting for fill ─────────────────────────────────

    def _handle_buy_placed(self, pos: Position, open_ids: Optional[set]) -> Optional[str]:
        if pos.open_order is None:
            pos.state = State.WATCHING
            return "buy order lost"

        if pos.open_order.order_id == 0:
            age = time.time() - pos.open_order.placed_at
            if age > 300:
                pos.open_order = None
                pos.state = State.WATCHING
                return "approval timeout (5 min), back to watching"
            return None

        if self._is_stale(pos.open_order, config.STALE_ORDER_SEC):
            self._cancel(pos)
            pos.state = State.WATCHING
            return "buy stale, cancelled"

        if self._filled(pos.open_order, open_ids):
            pos.entry_price = pos.open_order.price
            pos.slot_qty = pos.open_order.quantity
            pos.open_order = None
            pos.state = State.HOLDING
            return self._place_take_profit(pos)
        return None

    # ── HOLDING: place TP sell ───────────────────────────────────────

    def _handle_holding(self, pos: Position) -> Optional[str]:
        return self._place_take_profit(pos)

    # ── SELL_PLACED: waiting for TP, checking SL / mode flip ─────────

    def _handle_sell_placed(self, pos: Position, open_ids: Optional[set]) -> Optional[str]:
        if pos.open_order is None:
            pos.state = State.HOLDING
            return "sell lost, re-placing"

        if self._filled(pos.open_order, open_ids):
            return self._complete_cycle(pos)

        if pos.entry_price > 0:
            sl = pos.entry_price * (1 - config.STOP_LOSS_PCT / 100)
            if self.current_price <= sl:
                self._cancel(pos)
                return self._emergency_sell(pos, "STOP_LOSS")

        if self.market_mode == "DOWN" and self.macro_regime != "BULL_RUN":
            self._cancel(pos)
            return self._emergency_sell(pos, "MODE_DOWN")

        if self.macro_regime == "BEARISH":
            self._cancel(pos)
            return self._emergency_sell(pos, "MACRO_BEARISH")

        if pos.open_order and self._is_stale(pos.open_order, 18000):
            self._cancel(pos)
            pos.state = State.HOLDING
            return "TP stale (5h), reassessing"

        return None

    # ── Order placement helpers ──────────────────────────────────────

    def _place_take_profit(self, pos: Position) -> Optional[str]:
        sell_price = pos.entry_price * (1 + config.TAKE_PROFIT_PCT / 100)
        price_str = f"{sell_price:.{self.price_precision}f}"
        qty = pos.slot_qty or float(config.TRADE_QUANTITY)
        qty_str = f"{qty:.{self.qty_precision}f}"

        if not self._can_sell_base_qty(qty):
            sym = self._base_asset_symbol()
            self.last_sell_block_reason = (
                f"TP sell needs {qty:.{self.qty_precision}f} {sym} free "
                f"(have {self._base_free():.{self.qty_precision}f} free, "
                f"{self._base_locked():.{self.qty_precision}f} locked)"
            )
            return None

        self.last_sell_block_reason = None

        try:
            resp = trading.place_maker_order(
                side="SELL", quantity=qty_str, price=price_str,
            )
        except Exception:
            try:
                resp = trading.place_limit_order(
                    side="SELL", quantity=qty_str, price=price_str,
                )
            except Exception as exc2:
                self.errors.append(f"sell failed: {exc2}")
                return f"SELL failed: {exc2}"

        pos.open_order = OpenOrder(
            order_id=resp["orderId"], side="SELL",
            price=float(price_str), quantity=float(qty_str),
            placed_at=time.time(),
        )
        pos.state = State.SELL_PLACED
        return f"SELL (TP) @ {price_str}"

    def _emergency_sell(self, pos: Position, reason: str) -> str:
        qty = pos.slot_qty or float(config.TRADE_QUANTITY)
        qty_str = f"{qty:.{self.qty_precision}f}"
        price_str = f"{self.current_price:.{self.price_precision}f}"

        if not self._can_sell_base_qty(qty):
            sym = self._base_asset_symbol()
            msg = (
                f"{reason} exit skipped: need {qty_str} {sym} free "
                f"(have {self._base_free():.{self.qty_precision}f})"
            )
            self.errors.append(msg)
            self.last_sell_block_reason = msg
            return msg

        self.last_sell_block_reason = None

        try:
            trading.place_limit_order(
                side="SELL", quantity=qty_str, price=price_str,
            )
        except Exception as exc:
            self.errors.append(f"emergency sell failed: {exc}")
            return f"EMERGENCY SELL FAILED ({reason}): {exc}"

        sell_price = self.current_price
        gross_pct = (sell_price - pos.entry_price) / pos.entry_price * 100
        fee = sell_price * qty * 0.001 * 2
        net_pnl = (sell_price - pos.entry_price) * qty - fee

        cycle = Cycle(
            number=len(self.cycles) + 1, slot_id=pos.slot_id,
            buy_price=pos.entry_price, sell_price=sell_price,
            quantity=qty, gross_pct=gross_pct,
            fee_estimate=fee, net_pnl=net_pnl, timestamp=time.time(),
        )
        self.cycles.append(cycle)
        self.total_fees += fee
        self.current_balance += net_pnl
        self.ledger = ledger.record_cycle(net_pnl, fee, self.ledger)

        pos.open_order = None
        pos.entry_price = 0.0
        pos.slot_qty = None
        pos.state = State.WATCHING
        self._last_sell_time = time.time()
        self.last_sell_block_reason = None

        return f"{reason} SELL @ {price_str} net {net_pnl:+.4f} USDT"

    def _complete_cycle(self, pos: Position) -> str:
        sell_price = pos.open_order.price
        quantity = pos.open_order.quantity
        gross_pct = (sell_price - pos.entry_price) / pos.entry_price * 100
        fee = sell_price * quantity * 0.001 * 2
        net_pnl = (sell_price - pos.entry_price) * quantity - fee

        cycle = Cycle(
            number=len(self.cycles) + 1, slot_id=pos.slot_id,
            buy_price=pos.entry_price, sell_price=sell_price,
            quantity=quantity, gross_pct=gross_pct,
            fee_estimate=fee, net_pnl=net_pnl, timestamp=time.time(),
        )
        self.cycles.append(cycle)
        self.total_fees += fee
        self.current_balance += net_pnl
        self.ledger = ledger.record_cycle(net_pnl, fee, self.ledger)

        pos.open_order = None
        pos.entry_price = 0.0
        pos.slot_qty = None
        pos.state = State.WATCHING
        self._last_sell_time = time.time()
        self.last_sell_block_reason = None

        return (
            f"CYCLE #{cycle.number}: "
            f"BUY {cycle.buy_price:.{self.price_precision}f} → "
            f"SELL {cycle.sell_price:.{self.price_precision}f}  "
            f"net {cycle.net_pnl:+.4f} USDT"
        )

    # ── External trade callbacks (approval flow) ───────────────────

    def apply_external_fill(self, order_id: int, price: float, quantity: float) -> None:
        """Called by trade_manager when an approved buy has been executed."""
        pos = self.positions[0]
        if pos.state != State.BUY_PLACED:
            log.warning("apply_external_fill: state is %s, not BUY_PLACED — ignoring", pos.state.name)
            return
        pos.open_order = OpenOrder(
            order_id=order_id, side="BUY",
            price=price, quantity=quantity,
            placed_at=time.time(),
        )
        log.info("External fill applied: orderId=%s price=%.2f qty=%.6f", order_id, price, quantity)

    def cancel_pending_approval(self, reason: str = "trade failed") -> None:
        """Called when an approval was rejected or execution failed."""
        pos = self.positions[0]
        if pos.state == State.BUY_PLACED and pos.open_order and pos.open_order.order_id == 0:
            pos.open_order = None
            pos.state = State.WATCHING
            log.info("Pending approval cancelled: %s", reason)

    # ── Low-level helpers ────────────────────────────────────────────

    def _fetch_open_ids(self) -> Optional[set]:
        if self.positions[0].open_order is None:
            return None
        try:
            return {o["orderId"] for o in trading.get_open_orders()}
        except Exception as exc:
            self.errors.append(f"fill check: {exc}")
            return None

    def _filled(self, order: OpenOrder, open_ids: Optional[set]) -> bool:
        return open_ids is not None and order.order_id not in open_ids

    def _cancel(self, pos: Position) -> None:
        if pos.open_order is not None:
            try:
                trading.cancel_order(pos.open_order.order_id)
            except Exception as exc:
                self.errors.append(f"cancel: {exc}")
            pos.open_order = None

    @staticmethod
    def _is_stale(order: OpenOrder, max_age: int) -> bool:
        return (time.time() - order.placed_at) > max_age

    def cancel_all_open_orders(self) -> None:
        for pos in self.positions:
            self._cancel(pos)
            pos.state = State.WATCHING
            pos.slot_qty = None

    def cancel_open_order(self) -> None:
        self.cancel_all_open_orders()


# Backward compatibility — dashboard.py and state_writer.py import this name
MeanReversionStrategy = TrendAwareMakerStrategy
