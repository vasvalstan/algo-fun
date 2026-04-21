"""
LIFO Tranche Grid — pure state machine.

Venue-agnostic by design: the engine emits Intents (place buy / place sell /
cancel) and the runner binds them to Binance mainnet, Binance testnet, Revolut
live, or Revolut paper. Same state-machine bytes everywhere.

State machine (spec in new-bitcoin-test / user prompt):

  STATE 0 (HUNTING, 0 bags):
    Maintain one resting BUY at anchor * (1 - dip_pct).
    On new high, advance anchor by at least trail_step_pct, cancel, re-place.

  STATE 1 (ACTIVE, on BUY fill):
    Stop trailing. Push a Bag with the exact buy_fill_price + qty.
    Bracket with a LIMIT SELL at buy_fill_price * (1 + tp_pct).
    If bags < max_bullets: place next grid BUY at buy_fill_price * (1 - dip_pct).

  STATE 2 (on SELL fill):
    Remove that specific Bag (LIFO, by bag_id).
    If no bags remain: return to HUNTING anchored at the sell fill price.
    If bags remain: cancel current resting buy; place a new one at exactly the
                    sold bag's buy_fill_price (LIFO-exact replacement).

Invariants:
  * 0 or 1 resting_buy at any time.
  * Every bag has a sell_order_id once placement succeeds.
  * anchor_price is climb-only; it only resets on HUNTING re-entry.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional, Union

log = logging.getLogger(__name__)


# ── Parameters ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class LifoGridParams:
    """Runtime tuning. Venue translates these into exchange-specific units."""

    bullet_size_usdt: float = 6.0
    max_bullets: int = 10
    dip_pct: float = 0.75
    tp_pct: float = 0.71
    trail_step_pct: float = 0.15
    price_prec: int = 2
    qty_prec: int = 5
    min_notional: float = 5.0

    @property
    def dip_mult(self) -> float:
        return 1.0 - self.dip_pct / 100.0

    @property
    def tp_mult(self) -> float:
        return 1.0 + self.tp_pct / 100.0

    @property
    def trail_mult(self) -> float:
        return 1.0 + self.trail_step_pct / 100.0


# ── Data records ────────────────────────────────────────────────────

@dataclass
class Bag:
    """A filled buy tranche with its matching live TP sell."""

    bag_id: int
    buy_fill_price: float
    btc_amount: float                 # qty that actually landed in the wallet
    sell_target_price: float
    sell_order_id: Optional[str] = None  # None until runner places the sell
    entry_ts: float = 0.0
    # Wall-clock timestamp before which the runner should NOT retry placing
    # this bag's TP sell. Set when a placement attempt fails with a venue
    # error that won't self-clear in seconds (typically "insufficient
    # balance" because an orphan SELL from a previous run still locks the
    # BTC). Defaulted to 0.0 so persisted snapshots from before this field
    # existed deserialize cleanly.
    sell_retry_after: float = 0.0


@dataclass
class RestingBuy:
    """The single live resting buy order. Tag conveys intent for logs."""

    order_id: str
    price: float
    requested_qty: float
    tag: str                          # HUNT_INITIAL | TRAIL_REPRICE | NEXT_GRID | LIFO_REPLACE | LIFO_REARM


@dataclass
class ClosedTrade:
    """Book entry for dashboard/analytics."""

    bag_id: int
    buy_fill_price: float
    sell_fill_price: float
    qty: float
    gross_pnl_usdt: float
    hold_seconds: float
    exit_reason: str = "TP"
    entry_ts: float = 0.0
    exit_ts: float = 0.0


# ── Intents (returned to the runner for IO) ─────────────────────────

@dataclass
class PlaceBuyIntent:
    price: float
    bullet_size_usdt: float
    tag: str                          # HUNT_INITIAL | TRAIL_REPRICE | NEXT_GRID | LIFO_REPLACE | LIFO_REARM


@dataclass
class PlaceSellIntent:
    bag_id: int
    price: float
    qty: float


@dataclass
class CancelIntent:
    order_id: str
    reason: str                       # TRAIL_REPRICE | LIFO_REPLACE | HUNT_RESET | GEOFENCE …


Intent = Union[PlaceBuyIntent, PlaceSellIntent, CancelIntent]


# ── Helpers (exchange-precision math) ───────────────────────────────

def floor_qty(raw: float, qty_prec: int) -> float:
    """
    Strict rounding-DOWN to qty_prec decimals (spec §2 uses math.floor).

    The tiny epsilon nudge (`+ 1e-9` on the scaled value) absorbs
    IEEE-754 representation drift so values that are *mathematically*
    a clean step — e.g. 0.00007 stored as 6.999999999999999e-05 —
    don't get truncated down a whole step. Without it,
    `floor_qty(0.00007, 5)` returns 0.00006, which silently shaves
    ~17% off the lot and can drop a sell order under min_notional.
    The nudge is 1e-9 of a step (i.e. one part in a billion of the
    smallest tradable unit), so it never "rounds up" a real partial step.
    """
    if qty_prec <= 0:
        return float(math.floor(raw + 1e-12))
    step = 10.0 ** qty_prec
    return math.floor(raw * step + 1e-9) / step


def round_price(raw: float, price_prec: int) -> float:
    """Round-HALF-UP to price_prec decimals; Binance tick is 0.01."""
    if price_prec <= 0:
        return float(round(raw))
    step = 10.0 ** price_prec
    return round(raw * step) / step


# ── Engine ──────────────────────────────────────────────────────────

class LifoGridState:
    """Venue-free LIFO grid state machine."""

    def __init__(self, params: LifoGridParams, *, starting_capital_usdt: float = 0.0) -> None:
        self.p = params
        self.starting_capital_usdt = float(starting_capital_usdt)

        self.anchor_price: float = 0.0
        self.internal_high_record: float = 0.0

        self.resting_buy: Optional[RestingBuy] = None
        self.bags: List[Bag] = []
        self.closed_trades: List[ClosedTrade] = []

        self.realized_pnl_usdt: float = 0.0
        self._bag_seq: int = 0
        self.last_price: float = 0.0
        self.event_log: List[str] = []

    # ── Accessors ───────────────────────────────────────────────────

    @property
    def hunting(self) -> bool:
        return len(self.bags) == 0

    @property
    def at_max_ammo(self) -> bool:
        return len(self.bags) >= self.p.max_bullets

    def system_state(self) -> str:
        if self.hunting:
            return "HUNTING"
        if self.at_max_ammo:
            return "MAX_AMMO"
        return "ACTIVE"

    # ── Internal helpers ────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        # Dedupe: HUNT seed / TRAIL reprice can fire every tick when the
        # venue rejects placement (e.g. -2010 insufficient balance). Suppress
        # consecutive duplicates so the dashboard activity log and Python log
        # don't drown in identical lines.
        if self.event_log:
            last = self.event_log[-1]
            # event_log entries are formatted as "HH:MM:SS msg" — compare msg only.
            if last.endswith(msg):
                return
        log.info("LIFO %s", msg)
        self.event_log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        if len(self.event_log) > 200:
            self.event_log = self.event_log[-150:]

    def _buy_qty_gross(self, buy_price: float) -> float:
        """Requested qty for a $bullet_size buy — floored to venue precision."""
        if buy_price <= 0:
            return 0.0
        return floor_qty(self.p.bullet_size_usdt / buy_price, self.p.qty_prec)

    # ── Tick API (runner calls these) ───────────────────────────────

    def on_startup(self, price: float) -> None:
        """Seed anchor on process boot when state.json is empty."""
        self.last_price = price
        if self.anchor_price <= 0:
            self.anchor_price = price
        if self.internal_high_record <= 0:
            self.internal_high_record = price
        self._log(f"boot anchor={self.anchor_price:.2f} high={self.internal_high_record:.2f} bags={len(self.bags)}")

    def tick_trailing(self, price: float) -> List[Intent]:
        """Called every tick. Trails in HUNTING; rearms a lost buy in HOLDING."""
        self.last_price = price

        # Lazy seed if runner forgot on_startup.
        if self.anchor_price <= 0:
            self.anchor_price = price
            self.internal_high_record = price

        # While any bag is open: do not trail, but DO re-emit a buy intent
        # if one was lost (silently dropped after a venue rejection on the
        # original on_buy_filled / on_sell_filled emission). The runner's
        # _buy_retry_after cooldown gates how often we actually hit the
        # venue, so this method can run every tick without spamming.
        if not self.hunting:
            return self.rearm_next_buy()

        intents: List[Intent] = []

        # Track highs in local RAM (no API touch).
        if price > self.internal_high_record:
            self.internal_high_record = price

        target_buy_price = round_price(self.anchor_price * self.p.dip_mult, self.p.price_prec)

        # ── Trail-DOWN re-anchor (HUNT only, no resting buy) ──────────
        # The trail-up logic below is climb-only by design — anchor never
        # drifts down on its own. That works as long as the resting BUY
        # at `target_buy_price` stays alive: when price falls through it,
        # it just fills. But if we wake up HUNTING with no resting buy
        # AND spot has already collapsed at-or-below the target (e.g.
        # post-redeploy reconciliation cleared the order, or the previous
        # buy was cancelled out-of-band, or boot anchor was stale), the
        # engine would emit a HUNT_INITIAL ABOVE market every tick — and
        # Binance's LIMIT_MAKER (post-only) refuses any order that would
        # cross the spread (-2010 "Order would immediately match and
        # take"), so the bot gets wedged in a backoff loop and never
        # trades. Re-seat the anchor at current spot so the new dip
        # target lands safely below the bid; the climb-only trail then
        # resumes from here as price recovers.
        if self.resting_buy is None and price > 0 and price <= target_buy_price:
            old_anchor = self.anchor_price
            self.anchor_price = price
            self.internal_high_record = price
            target_buy_price = round_price(self.anchor_price * self.p.dip_mult, self.p.price_prec)
            self._log(
                f"HUNT re-anchor DOWN (spot {price:.2f} ≤ stale target {round_price(old_anchor * self.p.dip_mult, self.p.price_prec):.2f}) "
                f"anchor {old_anchor:.2f} → {price:.2f} buy → {target_buy_price:.2f}"
            )

        qty = self._buy_qty_gross(target_buy_price)
        notional_ok = qty * target_buy_price >= self.p.min_notional and qty > 0

        # Seed initial hunt buy when we have none resting.
        if self.resting_buy is None:
            if notional_ok:
                intents.append(PlaceBuyIntent(target_buy_price, self.p.bullet_size_usdt, "HUNT_INITIAL"))
                self._log(f"HUNT seed buy target {target_buy_price:.2f} (anchor {self.anchor_price:.2f})")
            else:
                self._log(f"HUNT seed skipped: notional {qty*target_buy_price:.2f} < min {self.p.min_notional}")
            return intents

        # Reprice only once the anchor has advanced by ≥ trail_step_pct.
        trigger = self.anchor_price * self.p.trail_mult
        if self.internal_high_record >= trigger:
            new_anchor = self.internal_high_record
            new_buy_price = round_price(new_anchor * self.p.dip_mult, self.p.price_prec)
            if abs(new_buy_price - self.resting_buy.price) >= 10.0 ** -self.p.price_prec:
                intents.append(CancelIntent(self.resting_buy.order_id, "TRAIL_REPRICE"))
                intents.append(PlaceBuyIntent(new_buy_price, self.p.bullet_size_usdt, "TRAIL_REPRICE"))
                self._log(
                    f"TRAIL reprice anchor {self.anchor_price:.2f} → {new_anchor:.2f} "
                    f"buy {self.resting_buy.price:.2f} → {new_buy_price:.2f}"
                )
                self.anchor_price = new_anchor
                # Runner clears resting_buy upon apply(cancel); engine does NOT mutate here.

        return intents

    def rearm_next_buy(self) -> List[Intent]:
        """
        Re-emit a buy intent when we are HOLDING (bags > 0) but have no
        resting buy. Cures the parked-state failure mode where one of the
        engine's one-shot buy intents (NEXT_GRID from on_buy_filled,
        LIFO_REPLACE from on_sell_filled) was rejected by the venue
        (insufficient balance, post-only cross, network blip) and silently
        dropped because nothing in the engine resurrects it.

        No-op when:
          * we are HUNTING (tick_trailing already owns the buy lifecycle)
          * a resting buy already exists
          * we have no bags to anchor off
          * we are at MAX AMMO (engine is flat-and-monitoring; emitting a
            buy now would over-stuff the grid the next time a SELL fires)

        Target = `latest_bag.buy_fill_price * dip_mult` — the same price
        on_buy_filled would have produced as NEXT_GRID. We deliberately do
        NOT compute it from current spot: the dip target should anchor off
        the last fill, not wherever price has wandered to since.

        The runner separately enforces a wall-clock cooldown via
        `_buy_retry_after`, so this method can be called every tick
        without spamming the venue.
        """
        if self.hunting or self.resting_buy is not None or not self.bags:
            return []
        if self.at_max_ammo:
            return []
        anchor_bag = self.bags[-1]
        if anchor_bag.buy_fill_price <= 0:
            return []
        target = round_price(anchor_bag.buy_fill_price * self.p.dip_mult, self.p.price_prec)
        qty = self._buy_qty_gross(target)
        if qty <= 0 or qty * target < self.p.min_notional:
            return []
        self._log(
            f"REARM lost buy target {target:.2f} "
            f"(anchor bag #{anchor_bag.bag_id} @ {anchor_bag.buy_fill_price:.2f})"
        )
        return [PlaceBuyIntent(target, self.p.bullet_size_usdt, "LIFO_REARM")]

    # ── Event hooks (runner calls when exchange state changes) ─────

    def on_buy_placed(self, order_id: str, price: float, requested_qty: float, tag: str) -> None:
        """Runner confirms a buy intent was accepted by the venue."""
        self.resting_buy = RestingBuy(order_id=order_id, price=price, requested_qty=requested_qty, tag=tag)

    def on_buy_cancelled(self, order_id: str) -> None:
        """Runner confirms the resting buy was cancelled (by us, not filled)."""
        if self.resting_buy and self.resting_buy.order_id == order_id:
            self.resting_buy = None

    def on_sell_placed(self, bag_id: int, order_id: str) -> None:
        bag = self._bag(bag_id)
        if bag:
            bag.sell_order_id = order_id

    def on_buy_filled(self, order_id: str, fill_price: float, filled_qty: float) -> List[Intent]:
        """
        STATE 1: a resting BUY filled.

        `filled_qty` is the amount that actually landed in the wallet
        (gross on Binance+BNB, net-of-fees on Revolut).
        """
        if self.resting_buy is None or self.resting_buy.order_id != order_id:
            self._log(f"on_buy_filled: untracked order {order_id} — ignoring")
            return []

        self.resting_buy = None
        if filled_qty <= 0:
            self._log(f"on_buy_filled oid={order_id} zero qty — skipping")
            return []

        self._bag_seq += 1
        sell_target = round_price(fill_price * self.p.tp_mult, self.p.price_prec)
        bag = Bag(
            bag_id=self._bag_seq,
            buy_fill_price=fill_price,
            btc_amount=filled_qty,
            sell_target_price=sell_target,
            entry_ts=time.time(),
        )
        self.bags.append(bag)
        self._log(
            f"BUY FILLED bag#{bag.bag_id} qty={bag.btc_amount:.8f} @ {bag.buy_fill_price:.2f} "
            f"→ TP {bag.sell_target_price:.2f}  (bags={len(self.bags)}/{self.p.max_bullets})"
        )

        intents: List[Intent] = [PlaceSellIntent(bag.bag_id, bag.sell_target_price, bag.btc_amount)]

        # Place next grid buy unless at max ammo.
        if len(self.bags) < self.p.max_bullets:
            next_buy_price = round_price(fill_price * self.p.dip_mult, self.p.price_prec)
            intents.append(PlaceBuyIntent(next_buy_price, self.p.bullet_size_usdt, "NEXT_GRID"))
            self._log(f"NEXT grid buy target {next_buy_price:.2f}")
        else:
            self._log(f"MAX AMMO ({self.p.max_bullets}) — no new buy; monitoring sells only")

        return intents

    def on_sell_filled(self, order_id: str, fill_price: float) -> List[Intent]:
        """
        STATE 2: a tracked SELL filled.

        Always closes out the bag with that order_id (LIFO by identity).
        """
        bag = next((b for b in self.bags if b.sell_order_id == order_id), None)
        if bag is None:
            self._log(f"on_sell_filled: untracked sell {order_id} — ignoring")
            return []

        gross_pnl = (fill_price - bag.buy_fill_price) * bag.btc_amount
        self.realized_pnl_usdt += gross_pnl
        self.closed_trades.append(ClosedTrade(
            bag_id=bag.bag_id,
            buy_fill_price=bag.buy_fill_price,
            sell_fill_price=fill_price,
            qty=bag.btc_amount,
            gross_pnl_usdt=gross_pnl,
            hold_seconds=max(0.0, time.time() - bag.entry_ts),
            entry_ts=bag.entry_ts,
            exit_ts=time.time(),
        ))
        self.bags.remove(bag)
        sign = "+" if gross_pnl >= 0 else ""
        self._log(
            f"SELL FILLED bag#{bag.bag_id} {bag.btc_amount:.8f} @ {fill_price:.2f}  "
            f"P&L {sign}{gross_pnl:.4f} USDT  (remaining {len(self.bags)})"
        )

        intents: List[Intent] = []

        if not self.bags:
            # Return to HUNTING anchored at the sell fill price.
            if self.resting_buy is not None:
                intents.append(CancelIntent(self.resting_buy.order_id, "HUNT_RESET"))
            self.anchor_price = fill_price
            self.internal_high_record = fill_price
            new_buy = round_price(fill_price * self.p.dip_mult, self.p.price_prec)
            intents.append(PlaceBuyIntent(new_buy, self.p.bullet_size_usdt, "HUNT_INITIAL"))
            self._log(f"ALL FLAT → HUNT anchor {fill_price:.2f} buy {new_buy:.2f}")
            return intents

        # LIFO-exact replacement: next buy sits at the sold bag's buy_fill_price.
        if self.resting_buy is not None:
            intents.append(CancelIntent(self.resting_buy.order_id, "LIFO_REPLACE"))
        target = round_price(bag.buy_fill_price, self.p.price_prec)
        intents.append(PlaceBuyIntent(target, self.p.bullet_size_usdt, "LIFO_REPLACE"))
        self._log(f"LIFO replace → buy @ {target:.2f} (exact sold-bag entry)")
        return intents

    def _bag(self, bag_id: int) -> Optional[Bag]:
        return next((b for b in self.bags if b.bag_id == bag_id), None)

    # ── Snapshot / serialization ────────────────────────────────────

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "system_state": self.system_state(),
            "active_bullets_count": len(self.bags),
            "anchor_price": self.anchor_price,
            "internal_high_record": self.internal_high_record,
            "last_price": self.last_price,
            "starting_capital_usdt": self.starting_capital_usdt,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "resting_buy_order": asdict(self.resting_buy) if self.resting_buy else None,
            "active_bags": [asdict(b) for b in self.bags],
            "closed_trades": [asdict(t) for t in self.closed_trades[-200:]],
            "bag_seq": self._bag_seq,
            "saved_at": time.time(),
            "params": {
                "bullet_size_usdt": self.p.bullet_size_usdt,
                "max_bullets": self.p.max_bullets,
                "dip_pct": self.p.dip_pct,
                "tp_pct": self.p.tp_pct,
                "trail_step_pct": self.p.trail_step_pct,
                "price_prec": self.p.price_prec,
                "qty_prec": self.p.qty_prec,
                "min_notional": self.p.min_notional,
            },
        }

    def load_state_dict(self, data: dict[str, Any]) -> None:
        self.anchor_price = float(data.get("anchor_price") or 0.0)
        self.internal_high_record = float(data.get("internal_high_record") or 0.0)
        self.last_price = float(data.get("last_price") or 0.0)
        self.realized_pnl_usdt = float(data.get("realized_pnl_usdt") or 0.0)
        self._bag_seq = int(data.get("bag_seq") or 0)

        rb = data.get("resting_buy_order")
        if rb:
            self.resting_buy = RestingBuy(
                order_id=str(rb["order_id"]),
                price=float(rb["price"]),
                requested_qty=float(rb.get("requested_qty", 0.0)),
                tag=str(rb.get("tag", "RECOVERED")),
            )
        else:
            self.resting_buy = None

        self.bags = []
        for b in data.get("active_bags", []) or []:
            self.bags.append(Bag(
                bag_id=int(b["bag_id"]),
                buy_fill_price=float(b["buy_fill_price"]),
                btc_amount=float(b["btc_amount"]),
                sell_target_price=float(b["sell_target_price"]),
                sell_order_id=(str(b["sell_order_id"]) if b.get("sell_order_id") else None),
                entry_ts=float(b.get("entry_ts") or 0.0),
                sell_retry_after=float(b.get("sell_retry_after") or 0.0),
            ))
        if self.bags:
            self._bag_seq = max(self._bag_seq, max(b.bag_id for b in self.bags))

        self.closed_trades = []
        for t in data.get("closed_trades", []) or []:
            self.closed_trades.append(ClosedTrade(
                bag_id=int(t.get("bag_id", 0)),
                buy_fill_price=float(t.get("buy_fill_price", 0.0)),
                sell_fill_price=float(t.get("sell_fill_price", 0.0)),
                qty=float(t.get("qty", 0.0)),
                gross_pnl_usdt=float(t.get("gross_pnl_usdt", 0.0)),
                hold_seconds=float(t.get("hold_seconds", 0.0)),
                exit_reason=str(t.get("exit_reason", "TP")),
                entry_ts=float(t.get("entry_ts", 0.0)),
                exit_ts=float(t.get("exit_ts", 0.0)),
            ))
