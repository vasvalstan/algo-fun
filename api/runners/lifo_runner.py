"""
LIFO grid runner — binds engine + venue + state.json + WebSocket.

One function serves all four deployments:
  * Binance Live (mainnet)
  * Binance Paper (testnet)
  * Revolut Live (mainnet)
  * Revolut Paper (in-memory)

Only the Venue differs between them. The engine bytes are identical.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

import config
from api import notifications, log_buffer
from api.lifo_grid import (
    CancelIntent,
    LifoGridParams,
    LifoGridState,
    PlaceBuyIntent,
    PlaceSellIntent,
    RestingBuy,
    floor_qty,
    round_price,
)
from api.lifo_state_store import LifoStateStore
from api.venues import Venue, VenueSpec
from api.ws_manager import WSManager

log = logging.getLogger(__name__)


# ── Persistence throttle ─────────────────────────────────────────────

_PERSIST_INTERVAL_S = 5.0

# ── Live-log narration heartbeat ─────────────────────────────────────
# How often each runner emits a "what am I thinking" line into the live
# log. Independent of poll_interval — purely wall-clock throttled so the
# dashboard always has something fresh to render every few seconds.
_THOUGHTS_INTERVAL_S = 3.0

# ── App-shutdown gate ────────────────────────────────────────────────
# Set by main.lifespan right before it cancels the runner tasks. When
# True, runners suppress their 🛑 Telegram message because we know the
# whole process is going away (Railway redeploy / scale-to-zero / Ctrl+C),
# which means a new container has already sent 🧱 LIFO Grid started.
# A 5-second arming window in run() also blocks late 🧱→🛑 spam if the
# runner is killed before it really got going.
_APP_SHUTTING_DOWN: bool = False
_STOP_NOTIFY_ARM_S = 5.0


def mark_app_shutting_down() -> None:
    """Called from FastAPI lifespan teardown before cancelling tasks."""
    global _APP_SHUTTING_DOWN
    _APP_SHUTTING_DOWN = True


def is_app_shutting_down() -> bool:
    return _APP_SHUTTING_DOWN


# ── Runner registry ────────────────────────────────────────────────
# Populated by run_lifo_runner() before the loop starts. Lets the API
# layer look up a live runner by its label (e.g. "binance-live") to
# trigger out-of-band actions like the dashboard "Buy at market" button
# without smuggling references through the WS manager.
_RUNNERS: Dict[str, "LifoRunner"] = {}


def get_runner(label: str) -> Optional["LifoRunner"]:
    return _RUNNERS.get(label)


def list_runner_labels() -> list[str]:
    return list(_RUNNERS.keys())


# ── place_buy failure backoff ────────────────────────────────────────
# When the venue keeps rejecting buy attempts (Binance -2010 because
# orphan orders have eaten all free USDT, Revolut 403 missing scope, etc.)
# the engine emits the same PlaceBuyIntent every tick. Without a
# cooldown the runner hammers the API and floods the live log. These
# constants tune how long to wait before the next attempt.
_BUY_BACKOFF_DEFAULT_S = 30.0
_BUY_BACKOFF_INSUFFICIENT_S = 120.0  # -2010 / similar — needs human action
_BUY_BACKOFF_PERMISSION_S = 300.0    # 403 — needs API-key fix
# Binance returns code -2010 for *both* "insufficient balance" AND the
# post-only LIMIT_MAKER reject ("Order would immediately match and take").
# The latter is a transient pricing-stale: spot moved between the tick
# read and the POST so our maker price crossed the book. The next polled
# tick already knows the new price, so we just need a brief breather
# before the engine's normal trail-down re-anchor or trail-up reprice
# emits a fresh intent at a non-crossing price. Anything longer just
# wedges the bot in a backoff loop while the market keeps moving.
_BUY_BACKOFF_POST_ONLY_CROSS_S = 5.0

# ── place_sell failure backoff ───────────────────────────────────────
# Per-bag cooldown applied when a TP sell can't be placed (typically
# because an orphan SELL from a previous container is still locking the
# BTC after a state-loss redeploy). The runner first tries to ADOPT
# that orphan in-band; if no match is found the bag enters this cooldown
# so the periodic re-arm pass below doesn't hammer the venue every tick.
_SELL_BACKOFF_INSUFFICIENT_S = 120.0
_SELL_BACKOFF_DEFAULT_S = 30.0


def _is_post_only_cross(exc: BaseException) -> bool:
    """True when a post-only (LIMIT_MAKER) order was rejected for crossing the spread.

    Binance: `-2010` "Order would immediately match and take."
    Revolut X: similar wording in 422 body when post_only is set.

    This is a transient pricing-stale: the orderbook moved between our
    tick read and the POST. The engine recovers by re-anchoring to spot
    on the next tick (see the runner's anchor re-seat in `_place_buy`).
    """
    text = str(exc).lower()
    return (
        "would immediately match" in text
        or "post only" in text
        or "post-only" in text
        or "limit_maker" in text
    )


def _is_insufficient_balance(exc: BaseException) -> bool:
    """True for venue errors that signal locked / missing balance.

    Covers both Binance (`-2010`, `-2019`) and Revolut X (HTTP 422 with
    "insufficient balance") wordings. Used to decide whether the
    `_place_sell` failure is the kind that an orphan-SELL adoption can
    actually fix.

    Important: `-2010` alone is NOT enough to classify as insufficient
    balance because Binance overloads code -2010 for the post-only
    "would immediately match" rejection too. We delegate to
    `_is_post_only_cross` first to disambiguate.
    """
    if _is_post_only_cross(exc):
        return False
    text = str(exc).lower()
    return (
        "-2019" in text
        or "insufficient" in text
        or "balance" in text
        or "422" in text
    )


# ── Orphan-BTC sweep ────────────────────────────────────────────────
# Strategy invariant: "after any cycle the working capital is USDT,
# never idle BTC". The bot achieves this naturally because every BUY
# fill is bracketed by a TP SELL. But the wallet can still accumulate
# untracked base asset from:
#   1. State loss across redeploys when no Railway volume is mounted
#      (the bag's BTC stays in the wallet but the engine forgets it).
#   2. Manual deposits / out-of-band trading on the same account.
#   3. BNB balance running out → fees taken in BTC → bag.btc_amount
#      slightly overestimates real BTC (dust residue per cycle).
# At startup we compute (wallet_btc - sum(bag.btc_amount)). If the
# excess is above SWEEP_DUST_BTC and the env-var gate is on, we MARKET
# sell the excess so the next HUNT_INITIAL has USDT to spend. Defaults
# to "true" because the strategy explicitly hunts in USDT — anyone
# using this account for non-bot BTC custody should set it to "false".
_AUTO_SWEEP_ENV = "LIFO_AUTO_SWEEP_ORPHAN_BTC"
_SWEEP_DUST_BTC = 1e-5  # ~$0.76 at $76k — below Binance LOT_SIZE noise


def _auto_sweep_enabled() -> bool:
    return os.getenv(_AUTO_SWEEP_ENV, "true").strip().lower() in ("1", "true", "yes", "on")


def _classify_buy_failure_cooldown(exc: BaseException) -> float:
    """Pick a backoff duration based on the venue error string.

    Order matters: the post-only "would immediately match" case must be
    detected BEFORE the generic -2010 branch, because Binance reuses
    code -2010 for both that transient cross AND for real insufficient-
    balance rejections that need a much longer cooldown.
    """
    if _is_post_only_cross(exc):
        return _BUY_BACKOFF_POST_ONLY_CROSS_S
    text = str(exc).lower()
    if "-2010" in text or "insufficient" in text or "balance" in text:
        return _BUY_BACKOFF_INSUFFICIENT_S
    if "403" in text or "forbidden" in text or "permission" in text:
        return _BUY_BACKOFF_PERMISSION_S
    return _BUY_BACKOFF_DEFAULT_S


# ── Runner ───────────────────────────────────────────────────────────


class LifoRunner:
    """One runner per venue. Long-lived; driven by `run()` coroutine."""

    def __init__(
        self,
        venue: Venue,
        params: LifoGridParams,
        ws_manager: WSManager,
        *,
        label: str,
        poll_interval: float,
        starting_capital_usdt: Optional[float] = None,
    ) -> None:
        self.venue = venue
        self.params = params
        self.ws_manager = ws_manager
        self.label = label
        self.poll_interval = max(0.5, float(poll_interval))
        self._forced_capital = starting_capital_usdt

        self.state = LifoGridState(params=params)
        self.store = LifoStateStore(venue.spec.name)
        self.errors: list[str] = []
        self._start_ts: float = 0.0
        self._last_persist: float = 0.0
        self._tick_count: int = 0
        self._last_thoughts: float = 0.0
        self._last_thoughts_msg: str = ""
        # Wall-clock timestamp before which place_buy attempts are skipped
        # because the venue keeps rejecting (insufficient balance, missing
        # permissions, etc.). Reset on the next successful placement.
        self._buy_retry_after: float = 0.0
        # Price of the last placement that triggered a backoff. The
        # cooldown only applies to retries at the SAME price — when the
        # engine emits a different price (e.g. trail-down re-anchor, or
        # a brand new NEXT_GRID after a fill), we let it through so we
        # don't sit out a real opportunity for a stale 120s.
        self._buy_retry_after_price: float = 0.0
        # Serialises tick(s) vs out-of-band actions like force_market_buy
        # so a manual "Buy at market" can't race the polling loop while
        # bags / resting_buy are being mutated.
        self._lock = asyncio.Lock()

    # ── State IO ────────────────────────────────────────────────────

    def _reset_env_var(self) -> str:
        """Per-venue env var that, when set to a truthy value, makes the
        next boot purge the persisted state file and start fresh.

        Naming: venue.spec.name="binance-live" → LIFO_RESET_BINANCE_LIVE.

        Use case: the bot's state diverged from the exchange (e.g. user
        manually sold the BTC the bot tracks, or migrated symbols). The
        in-process `_maybe_persist` always wins races against external
        `rm`, so the only race-free way to wipe state is at boot before
        load. Set the env var, redeploy once, then unset.
        """
        return "LIFO_RESET_" + self.venue.spec.name.upper().replace("-", "_")

    def _reset_requested(self) -> bool:
        return os.getenv(self._reset_env_var(), "").strip().lower() in ("1", "true", "yes", "on")

    async def _purge_state_file(self) -> bool:
        """Delete the persisted state file. Returns True if a file was actually removed."""
        try:
            existed = await asyncio.to_thread(self.store.path.exists)
            if existed:
                await asyncio.to_thread(self.store.path.unlink)
            return existed
        except FileNotFoundError:
            return False
        except Exception as exc:
            log.warning("[%s] purge failed: %s", self.label, exc)
            return False

    async def _load_state(self) -> bool:
        if self._reset_requested():
            removed = await self._purge_state_file()
            log.warning(
                "[%s] %s=1 detected — %s. Unset the env var after this boot to "
                "avoid wiping legitimate state on every restart.",
                self.label,
                self._reset_env_var(),
                f"purged {self.store.path}" if removed else "no state file to purge",
            )
            return False  # always boot fresh when reset is requested
        data = await self.store.load()
        if not data:
            return False
        try:
            self.state.load_state_dict(data)
            return True
        except Exception as exc:
            log.warning("[%s] failed to parse state file: %s — starting fresh", self.label, exc)
            return False

    async def _maybe_persist(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_persist) < _PERSIST_INTERVAL_S:
            return
        snapshot = self.state.to_state_dict()
        snapshot["venue_name"] = self.venue.spec.name
        snapshot["label"] = self.label
        await self.store.save(snapshot)
        self._last_persist = now

    # ── Exchange-side reconciliation on startup ─────────────────────

    async def _reconcile_with_exchange(self) -> None:
        """
        Bring persisted engine state into agreement with the live exchange.

        Strategy: for each tracked order id, ask the venue for its real
        status (Venue.get_order_status). This handles every case correctly:

          * resting_buy → if FILLED/PARTIALLY_FILLED on the exchange while
            we were offline, treat it as a buy fill (a new bag is created
            and the TP sell is placed). If CANCELED, just clear and re-arm.
            If still OPEN, leave it.
          * bag.sell_order_id → if FILLED, close the bag at the TP price.
            If CANCELED, re-place the TP (we still hold the BTC).
            If still OPEN, leave it.
          * bag.sell_order_id is None → re-place the TP.

        For UNKNOWN status (e.g. paper venues across restart), we fall back
        to the safe-forward heuristic: missing buy → cancel, missing sell →
        assume filled. That matches the previous behaviour for paper.
        """
        try:
            open_ids = await asyncio.to_thread(self.venue.get_open_order_ids)
        except Exception as exc:
            log.warning("[%s] could not fetch open orders on boot: %s", self.label, exc)
            return

        # Fetch detailed open SELLs ONCE so the bag loop can ADOPT any
        # existing TP that we lost the link to (state-loss across deploys
        # is the typical trigger). Without adoption we'd blindly POST a
        # fresh SELL → exchange returns 422 "insufficient balance"
        # because the previous SELL already locked the BTC, and we'd
        # spin on that error forever. List is mutated as bags claim
        # orders so the same SELL can never be adopted twice.
        adoptable_sells: list[dict] = []
        try:
            details = await asyncio.to_thread(self.venue.get_open_orders_detail)
            for o in details:
                if str(o.get("side", "")).upper() != "SELL":
                    continue
                oid = str(o.get("order_id") or o.get("orderId") or "")
                if not oid:
                    continue
                adoptable_sells.append({
                    "order_id": oid,
                    "qty": float(o.get("qty", 0.0) or o.get("origQty", 0.0) or 0.0),
                    "price": float(o.get("price", 0.0) or 0.0),
                })
        except Exception as exc:
            log.debug("[%s] adoption: get_open_orders_detail failed: %s", self.label, exc)

        # ── Resting buy reconciliation ─────────────────────────────
        rb = self.state.resting_buy
        if rb and rb.order_id not in open_ids:
            status, executed_qty = await self._lookup_status(rb.order_id)
            if status in ("FILLED", "PARTIALLY_FILLED"):
                fill_qty = executed_qty if executed_qty > 0 else self.venue.filled_qty_after_fees(rb.requested_qty)
                log.info(
                    "[%s] recovery: buy %s was %s offline (qty=%.8f) → bracketing",
                    self.label, rb.order_id, status, fill_qty,
                )
                intents = self.state.on_buy_filled(rb.order_id, rb.price, fill_qty)
                await self._apply_intents(intents)
            elif status == "CANCELED":
                log.info("[%s] recovery: buy %s was CANCELED → re-arm", self.label, rb.order_id)
                self.state.on_buy_cancelled(rb.order_id)
            elif status == "OPEN":
                log.info("[%s] buy %s still OPEN per get_order — keeping", self.label, rb.order_id)
            else:  # UNKNOWN
                log.warning(
                    "[%s] recovery: buy %s status UNKNOWN — clearing (will re-arm next tick)",
                    self.label, rb.order_id,
                )
                self.state.on_buy_cancelled(rb.order_id)

        # ── Sell reconciliation ────────────────────────────────────
        for bag in list(self.state.bags):
            if bag.sell_order_id is None:
                # Try to ADOPT an existing open SELL that matches this bag
                # before placing a new one. Avoids the 422 "insufficient
                # balance" loop when the previous run placed the SELL but
                # crashed/redeployed before persisting the order_id.
                adopted = self._claim_matching_sell(bag, adoptable_sells)
                if adopted:
                    bag.sell_order_id = adopted["order_id"]
                    log.warning(
                        "[%s] recovery: ADOPTED existing sell %s for bag #%d "
                        "(qty=%.8f vs bag=%.8f, price=$%.2f vs target $%.2f) — "
                        "TP placed in a previous run, re-linking instead of "
                        "placing a duplicate.",
                        self.label, adopted["order_id"], bag.bag_id,
                        adopted["qty"], bag.btc_amount,
                        adopted["price"], bag.sell_target_price,
                    )
                    continue
                log.info("[%s] recovery: bag #%d missing sell_order_id — re-placing TP", self.label, bag.bag_id)
                await self._apply_intents([PlaceSellIntent(bag.bag_id, bag.sell_target_price, bag.btc_amount)])
                continue

            if bag.sell_order_id in open_ids:
                continue  # still resting — nothing to do

            status, _exec = await self._lookup_status(bag.sell_order_id)
            if status in ("FILLED", "PARTIALLY_FILLED"):
                log.info(
                    "[%s] recovery: sell %s was %s offline → closing bag #%d at %.2f",
                    self.label, bag.sell_order_id, status, bag.bag_id, bag.sell_target_price,
                )
                intents = self.state.on_sell_filled(bag.sell_order_id, bag.sell_target_price)
                await self._apply_intents(intents)
            elif status == "CANCELED":
                # Same adoption guard: if there's a different open SELL on
                # the book that matches this bag, link to it rather than
                # placing a duplicate that the venue will reject.
                bag.sell_order_id = None
                adopted = self._claim_matching_sell(bag, adoptable_sells)
                if adopted:
                    bag.sell_order_id = adopted["order_id"]
                    log.warning(
                        "[%s] recovery: tracked sell was CANCELED but ADOPTED "
                        "live SELL %s for bag #%d (qty=%.8f, price=$%.2f).",
                        self.label, adopted["order_id"], bag.bag_id,
                        adopted["qty"], adopted["price"],
                    )
                    continue
                log.warning(
                    "[%s] recovery: sell was CANCELED → re-placing TP for bag #%d",
                    self.label, bag.bag_id,
                )
                await self._apply_intents([PlaceSellIntent(bag.bag_id, bag.sell_target_price, bag.btc_amount)])
            else:  # UNKNOWN — fall back to "assume filled" (legacy heuristic)
                log.warning(
                    "[%s] recovery: sell %s status UNKNOWN → assuming FILLED at %.2f (bag #%d)",
                    self.label, bag.sell_order_id, bag.sell_target_price, bag.bag_id,
                )
                intents = self.state.on_sell_filled(bag.sell_order_id, bag.sell_target_price)
                await self._apply_intents(intents)

    async def _fetch_price_with_retry(self, attempts: int = 10, delay: float = 1.5) -> float:
        """
        Some venues (e.g. Revolut paper) need a warm-up call before /tickers
        returns a fresh row. Loop until we get a non-zero price or run out.
        """
        for i in range(attempts):
            advance = getattr(self.venue, "advance_tick", None)
            if callable(advance):
                try:
                    await asyncio.to_thread(advance)
                except Exception:
                    pass
            try:
                price = float(await asyncio.to_thread(self.venue.get_price))
            except Exception as exc:
                log.warning("[%s] price fetch attempt %d failed: %s", self.label, i + 1, exc)
                price = 0.0
            if price > 0:
                return price
            await asyncio.sleep(delay)
        return 0.0

    async def _lookup_status(self, order_id: str) -> tuple[str, float]:
        """Wrap venue.get_order_status with thread-offload + safe defaults."""
        lookup = getattr(self.venue, "get_order_status", None)
        if not callable(lookup):
            return ("UNKNOWN", 0.0)
        try:
            status, qty = await asyncio.to_thread(lookup, order_id)
            return (str(status).upper(), float(qty or 0.0))
        except Exception as exc:
            log.warning("[%s] get_order_status(%s) failed: %s", self.label, order_id, exc)
            return ("UNKNOWN", 0.0)

    # ── Orphan order + BTC sweep (startup self-heal) ────────────────

    async def _cancel_orphan_orders(self) -> int:
        """
        Cancel every open order on the venue that isn't tracked in our
        state. Without this, base/quote balances stay locked in stale
        orders (typical aftermath of a state-loss redeploy: old TP SELLs
        keep BTC locked, old grid BUYs keep USDT locked) and the
        subsequent BTC sweep silently fails with -2010 even though the
        wallet's free+locked total looks healthy.

        Returns number of orders cancelled.
        """
        try:
            open_orders = await asyncio.to_thread(self.venue.get_open_orders_detail)
        except Exception as exc:
            log.warning("[%s] sweep: get_open_orders_detail failed: %s", self.label, exc)
            return 0

        tracked = self._known_order_ids()
        cancelled = 0
        for o in open_orders:
            oid = str(o.get("order_id", ""))
            if not oid or oid in tracked:
                continue
            side = o.get("side", "?")
            price = float(o.get("price", 0.0))
            qty = float(o.get("qty", 0.0))
            log.warning(
                "[%s] sweep: cancelling orphan %s @ $%.2f × %.8f (id=%s)",
                self.label, side, price, qty, oid,
            )
            try:
                await asyncio.to_thread(self.venue.cancel, oid)
                cancelled += 1
            except Exception as exc:
                self._record_error(f"sweep cancel {oid} failed: {exc}")

        if cancelled:
            # Brief pause so the venue settles balances before we read them.
            await asyncio.sleep(1.5)
        return cancelled

    async def _sweep_orphan_btc_if_any(self) -> None:
        """
        Cancel orphan orders, then sell any base asset that isn't
        accounted for by a tracked bag.

        Run ONCE at startup, AFTER state load + exchange reconciliation,
        so that bags whose state we still know about are correctly
        accounted for. Anything left over is "orphan": probably from a
        previous container that lost its state.json (no Railway volume),
        a manual deposit, or fee dust. Convert it to USDT so the next
        HUNT_INITIAL has working capital.
        """
        if not _auto_sweep_enabled():
            return
        sweep_fn = getattr(self.venue, "place_market_sell", None)
        if not callable(sweep_fn):
            return  # Venue doesn't support market sells (e.g. Revolut today).

        cancelled = await self._cancel_orphan_orders()
        if cancelled:
            log.warning("[%s] sweep: cancelled %d orphan order(s)", self.label, cancelled)

        try:
            balances = await asyncio.to_thread(self.venue.get_balances)
        except Exception as exc:
            log.warning("[%s] sweep: get_balances failed: %s", self.label, exc)
            return

        spec = self.venue.spec
        wallet_btc = float(balances.get(spec.base_asset, 0.0))
        tracked_btc = sum(b.btc_amount for b in self.state.bags)
        orphan_btc = wallet_btc - tracked_btc

        # Symmetric check: a meaningfully NEGATIVE orphan means the bot
        # tracks more BTC than the wallet actually holds — i.e. phantom
        # bags (BTC was sold/converted off-bot, state was loaded from a
        # stale snapshot, or a previous symbol migration left dangling
        # bags). Without this branch the sweep used to log and return,
        # then `_place_sell` would spam `-2010 insufficient balance` for
        # every phantom bag forever. The bot would also reach MAX_AMMO
        # and stop hunting BUYs, so it'd be silently frozen.
        if orphan_btc < -_SWEEP_DUST_BTC:
            shortfall = -orphan_btc
            try:
                spot_for_estimate = float(await asyncio.to_thread(self.venue.get_price))
            except Exception:
                spot_for_estimate = 0.0
            usd_short = shortfall * spot_for_estimate if spot_for_estimate > 0 else 0.0
            self._record_error(
                f"PHANTOM BAGS DETECTED: bot tracks {tracked_btc:.8f} {spec.base_asset} "
                f"across {len(self.state.bags)} bag(s) but wallet only has "
                f"{wallet_btc:.8f} (short by {shortfall:.8f} ≈ ${usd_short:.2f}). "
                f"BTC was likely sold or converted off-bot. Set "
                f"{self._reset_env_var()}=1 and redeploy to wipe state and start fresh."
            )
            if spec.account_mode == "live":
                try:
                    notifications.send(
                        f"⚠️ <b>Phantom bags detected</b> ({self.label})\n"
                        f"Engine tracks <code>{tracked_btc:.8f}</code> {spec.base_asset} "
                        f"but wallet has only <code>{wallet_btc:.8f}</code>.\n"
                        f"Set <code>{self._reset_env_var()}=1</code> and redeploy "
                        f"to wipe state. New buys will be skipped until then."
                    )
                except Exception:
                    pass
            return

        if orphan_btc <= _SWEEP_DUST_BTC:
            log.info(
                "[%s] sweep: wallet %s=%.8f, tracked=%.8f, orphan=%.8f ≤ dust %.8f → no action",
                self.label, spec.base_asset, wallet_btc, tracked_btc, orphan_btc, _SWEEP_DUST_BTC,
            )
            return

        sell_qty = floor_qty(orphan_btc, self.params.qty_prec)
        if sell_qty <= 0:
            return

        # Notional sanity: refuse to sweep below min_notional (Binance
        # would reject anyway, but logging is clearer this way).
        try:
            spot = float(await asyncio.to_thread(self.venue.get_price))
        except Exception:
            spot = 0.0
        notional = sell_qty * spot
        if spot > 0 and notional < self.params.min_notional:
            log.info(
                "[%s] sweep: orphan %.8f %s ≈ $%.2f below min_notional $%.2f — leaving",
                self.label, sell_qty, spec.base_asset, notional, self.params.min_notional,
            )
            return

        log.warning(
            "[%s] sweep: %.8f %s untracked (wallet %.8f − bags %.8f) ≈ $%.2f → MARKET SELL",
            self.label, sell_qty, spec.base_asset, wallet_btc, tracked_btc, notional,
        )
        try:
            placed = await asyncio.to_thread(sweep_fn, sell_qty)
        except Exception as exc:
            self._record_error(f"sweep MARKET sell {sell_qty} {spec.base_asset} failed: {exc}")
            return

        proceeds_usdt = (placed.price or spot) * (placed.requested_qty or sell_qty)
        log.warning(
            "[%s] sweep: SOLD %.8f %s @ ~$%.2f → +$%.2f USDT freed",
            self.label, placed.requested_qty or sell_qty, spec.base_asset,
            placed.price or spot, proceeds_usdt,
        )
        if spec.account_mode == "live":
            try:
                notifications.send(
                    f"♻️ <b>Orphan {spec.base_asset} swept</b> ({self.label})\n"
                    f"Sold <code>{placed.requested_qty or sell_qty:.8f}</code> {spec.base_asset} "
                    f"@ ~<code>${placed.price or spot:,.2f}</code>\n"
                    f"Freed ≈ <code>${proceeds_usdt:,.2f}</code> {spec.quote_asset} for hunting.\n"
                    f"<i>Untracked base asset detected at startup (wallet "
                    f"{wallet_btc:.8f} vs tracked bags {tracked_btc:.8f}).</i>"
                )
            except Exception:
                pass

    # ── Intent dispatcher ───────────────────────────────────────────

    async def _apply_intents(self, intents: list[Any]) -> None:
        """Send intents to the exchange. Cancels first, then placements."""
        cancels = [i for i in intents if isinstance(i, CancelIntent)]
        buys = [i for i in intents if isinstance(i, PlaceBuyIntent)]
        sells = [i for i in intents if isinstance(i, PlaceSellIntent)]

        for c in cancels:
            try:
                await asyncio.to_thread(self.venue.cancel, c.order_id)
                self.state.on_buy_cancelled(c.order_id)
            except Exception as exc:
                self._record_error(f"cancel {c.order_id} ({c.reason}): {exc}")

        for b in buys:
            await self._place_buy(b)

        for s in sells:
            await self._place_sell(s)

    async def _place_buy(self, intent: PlaceBuyIntent) -> None:
        buy_price = round_price(intent.price, self.params.price_prec)
        qty = floor_qty(intent.bullet_size_usdt / buy_price, self.params.qty_prec)
        notional = qty * buy_price
        if qty <= 0 or notional < self.params.min_notional:
            self._record_error(
                f"skip buy [{intent.tag}]: notional {notional:.2f} < min {self.params.min_notional:.2f}"
            )
            return

        # Failure backoff: if the venue keeps rejecting (e.g. -2010
        # insufficient balance because orphan orders lock the USDT), don't
        # hammer the API every poll. Skip until the cooldown expires —
        # but only for retries at the SAME price. A re-priced intent is
        # a fresh attempt and may well succeed where the stale one failed.
        now = time.time()
        if now < self._buy_retry_after and abs(buy_price - self._buy_retry_after_price) < 10.0 ** -self.params.price_prec:
            return

        try:
            placed = await asyncio.to_thread(self.venue.place_limit_buy, buy_price, qty)
        except Exception as exc:
            cooldown = _classify_buy_failure_cooldown(exc)
            self._buy_retry_after = now + cooldown
            self._buy_retry_after_price = buy_price

            # Post-only cross self-heal: when the venue rejects because
            # our maker price would have crossed the spread, force the
            # engine to re-anchor at the most recent traded price. The
            # next tick then computes a fresh dip target safely below
            # the ask, and (because the price is different) the cooldown
            # above won't block the retry. Without this, `last_price`
            # hovering a hair above the stale target while best ask sits
            # a hair below it would ping-pong on the same intent every
            # cooldown interval — annoying for the user, wasteful for
            # the rate limit, and exactly what the dashboard explanation
            # promises does NOT happen.
            #
            # Only meaningful for buys that re-derive from `anchor_price`
            # on the next tick: HUNT_INITIAL, NEXT_GRID, LIFO_REPLACE,
            # TRAIL_REPRICE. (All current PlaceBuyIntent tags qualify;
            # the if-guard is defensive against future tags.)
            if _is_post_only_cross(exc) and intent.tag in (
                "HUNT_INITIAL", "NEXT_GRID", "LIFO_REPLACE", "TRAIL_REPRICE"
            ):
                spot = self.state.last_price or buy_price
                old_anchor = self.state.anchor_price
                if spot > 0 and abs(spot - old_anchor) > 10.0 ** -self.params.price_prec:
                    self.state.anchor_price = spot
                    self.state.internal_high_record = spot
                    log.info(
                        "[%s] post-only cross @ %.2f → re-anchor %.2f → %.2f "
                        "(next tick will compute a fresh, non-crossing target)",
                        self.label, buy_price, old_anchor, spot,
                    )

            self._record_error(
                f"place_buy [{intent.tag}] failed @ {buy_price:.2f}: {exc} → backoff {int(cooldown)}s"
            )
            return
        self._buy_retry_after = 0.0
        self._buy_retry_after_price = 0.0
        self.state.on_buy_placed(placed.order_id, placed.price, placed.requested_qty, intent.tag)

    async def _place_sell(self, intent: PlaceSellIntent) -> None:
        sell_price = round_price(intent.price, self.params.price_prec)
        qty = floor_qty(intent.qty, self.params.qty_prec)
        notional = qty * sell_price
        if qty <= 0 or notional < self.params.min_notional:
            self._record_error(
                f"skip sell bag#{intent.bag_id}: notional {notional:.2f} < min {self.params.min_notional:.2f}"
            )
            return
        try:
            placed = await asyncio.to_thread(self.venue.place_limit_sell, sell_price, qty)
        except Exception as exc:
            # In-band recovery for the classic post-redeploy failure mode:
            # an orphan SELL placed by a previous container is still on
            # the book and has the BTC locked, so a fresh POST gets 422
            # / -2010 / -2019. Try to ADOPT that orphan right now instead
            # of waiting for the next process restart. If adoption finds
            # nothing, fall back to a per-bag cooldown so the periodic
            # re-arm pass doesn't hammer the venue.
            bag = self.state._bag(intent.bag_id) if hasattr(self.state, "_bag") else None
            if bag is None:
                self._record_error(f"place_sell bag#{intent.bag_id} failed: {exc}")
                return

            if _is_insufficient_balance(exc):
                adopted_id = await self._try_adopt_sell_for_bag(bag)
                if adopted_id:
                    self.state.on_sell_placed(bag.bag_id, adopted_id)
                    bag.sell_retry_after = 0.0
                    log.warning(
                        "[%s] place_sell bag#%d 422 → ADOPTED existing SELL %s "
                        "(qty=%.8f @ $%.2f) instead of placing a duplicate",
                        self.label, bag.bag_id, adopted_id,
                        bag.btc_amount, bag.sell_target_price,
                    )
                    return
                cooldown = _SELL_BACKOFF_INSUFFICIENT_S
            else:
                cooldown = _SELL_BACKOFF_DEFAULT_S

            bag.sell_retry_after = time.time() + cooldown
            self._record_error(
                f"place_sell bag#{intent.bag_id} failed: {exc} → backoff {int(cooldown)}s"
            )
            return
        # Success: clear any lingering cooldown from a previous failure.
        bag = self.state._bag(intent.bag_id) if hasattr(self.state, "_bag") else None
        if bag is not None:
            bag.sell_retry_after = 0.0
        self.state.on_sell_placed(intent.bag_id, placed.order_id)

    async def _try_adopt_sell_for_bag(self, bag: Any) -> Optional[str]:
        """
        Look up live open SELLs and try to claim one that matches `bag`.

        Returns the adopted order_id on success, None otherwise. Used
        both at startup (`_reconcile_with_exchange`) and in-band when
        `_place_sell` hits an "insufficient balance" error — which is
        almost always caused by an orphan SELL holding the BTC.
        """
        try:
            details = await asyncio.to_thread(self.venue.get_open_orders_detail)
        except Exception as exc:
            log.debug("[%s] adoption: get_open_orders_detail failed: %s", self.label, exc)
            return None

        tracked = self._known_order_ids()
        candidates: list[dict] = []
        for o in details:
            if str(o.get("side", "")).upper() != "SELL":
                continue
            oid = str(o.get("order_id") or o.get("orderId") or "")
            if not oid or oid in tracked:
                continue  # don't steal a SELL that another bag already owns
            candidates.append({
                "order_id": oid,
                "qty": float(o.get("qty", 0.0) or o.get("origQty", 0.0) or 0.0),
                "price": float(o.get("price", 0.0) or 0.0),
            })

        match = self._claim_matching_sell(bag, candidates)
        return match["order_id"] if match else None

    async def _rearm_orphan_sells(self) -> None:
        """
        Periodic re-arm: any bag without a tracked SELL gets another
        adoption / placement attempt once its per-bag cooldown elapses.

        This makes the `place_sell` recovery story work without a
        process restart. Without this pass, a bag that fails its initial
        TP placement (typically right after a buy fill on a freshly
        redeployed container) stays unprotected by a tracked TP forever
        — the orphan SELL on the exchange still acts as the real TP, but
        when it eventually fills the engine never sees it and we end up
        with a phantom bag.
        """
        if not self.state.bags:
            return
        now = time.time()
        for bag in list(self.state.bags):
            if bag.sell_order_id is not None:
                continue
            if now < getattr(bag, "sell_retry_after", 0.0):
                continue
            adopted_id = await self._try_adopt_sell_for_bag(bag)
            if adopted_id:
                self.state.on_sell_placed(bag.bag_id, adopted_id)
                bag.sell_retry_after = 0.0
                log.warning(
                    "[%s] re-arm: ADOPTED existing SELL %s for bag #%d "
                    "(qty=%.8f @ $%.2f)",
                    self.label, adopted_id, bag.bag_id,
                    bag.btc_amount, bag.sell_target_price,
                )
                continue
            # No matching orphan — try a fresh placement. _place_sell
            # will set the next cooldown if this also fails.
            await self._place_sell(PlaceSellIntent(
                bag.bag_id, bag.sell_target_price, bag.btc_amount,
            ))

    def _claim_matching_sell(
        self,
        bag: Any,
        candidates: list[dict],
    ) -> Optional[dict]:
        """
        Find — and consume — an open SELL on the exchange that matches
        this bag, so we can adopt it instead of placing a duplicate.

        Match rules (qty is the strong key, price is a sanity check):
          * |order.qty − bag.btc_amount| / bag.btc_amount ≤ 1%
          * |order.price − bag.sell_target_price| / bag.sell_target_price ≤ 5%

        The 5% price tolerance covers small TP-recompute drift across
        runs (e.g. tp_pct config edits). The 1% qty tolerance covers
        venue precision rounding and fee adjustments. Anything outside
        these bands is left alone — we'd rather place a fresh order
        than mistakenly adopt an unrelated SELL.

        On match the candidate is REMOVED from the list so two bags
        with similar sizes can't claim the same order.
        """
        if not candidates or bag.btc_amount <= 0 or bag.sell_target_price <= 0:
            return None

        qty_band = max(bag.btc_amount * 0.01, 1e-8)
        px_band = bag.sell_target_price * 0.05

        best: Optional[dict] = None
        best_qty_diff = float("inf")
        for c in candidates:
            if abs(c["qty"] - bag.btc_amount) > qty_band:
                continue
            if abs(c["price"] - bag.sell_target_price) > px_band:
                continue
            d = abs(c["qty"] - bag.btc_amount)
            if d < best_qty_diff:
                best = c
                best_qty_diff = d

        if best is not None:
            candidates.remove(best)
        return best

    def _record_error(self, msg: str) -> None:
        log.warning("[%s] %s", self.label, msg)
        # Prefix every recorded error with a wall-clock timestamp so the
        # frontend dashboard can show *when* something went wrong, not just
        # *what* — without that context every error looks "current".
        stamp = time.strftime("%H:%M:%S")
        self.errors.append(f"[{stamp}] {msg}")
        if len(self.errors) > 50:
            self.errors = self.errors[-25:]

    # ── Telegram wallet/P&L block ───────────────────────────────────

    async def _build_wallet_block(self, price: float, *, resumed: bool) -> str:
        """Format a balance + P&L + open-orders summary appended to startup/stop notifications."""
        spec = self.venue.spec
        try:
            balances = await asyncio.to_thread(self.venue.get_balances)
        except Exception as exc:
            log.warning("[%s] _build_wallet_block: get_balances failed: %s", self.label, exc)
            balances = {}
        try:
            open_orders = await asyncio.to_thread(self.venue.get_open_orders_detail)
        except Exception as exc:
            log.warning("[%s] _build_wallet_block: get_open_orders_detail failed: %s", self.label, exc)
            open_orders = []

        base = float(balances.get(spec.base_asset, 0.0))
        quote = float(balances.get(spec.quote_asset, 0.0))
        equity = quote + base * price

        capital = float(self.state.starting_capital_usdt or 0.0)
        realized = float(self.state.realized_pnl_usdt or 0.0)
        bags_n = len(self.state.bags)
        exposure = sum(b.btc_amount * b.buy_fill_price for b in self.state.bags)
        unrealized = sum((price - b.buy_fill_price) * b.btc_amount for b in self.state.bags)

        delta_str = ""
        if capital > 0:
            delta = equity - capital
            delta_pct = (delta / capital) * 100 if capital else 0.0
            sign = "+" if delta >= 0 else ""
            delta_str = f" ({sign}${delta:,.2f} / {sign}{delta_pct:.2f}%)"

        lines = ["", "─────────"]
        lines.append("💰 <b>Wallet</b>")
        lines.append(f"  {spec.base_asset}: <code>{base:.8f}</code> (≈ <code>${base * price:,.2f}</code>)")
        lines.append(f"  {spec.quote_asset}: <code>${quote:,.2f}</code>")
        lines.append(f"  Equity: <code>${equity:,.2f}</code>{delta_str}")
        lines.append("")
        lines.append("📊 <b>P&amp;L</b>")
        if capital > 0:
            lines.append(f"  Initial capital: <code>${capital:,.2f}</code>" + (" (resumed)" if resumed else " (new)"))
        else:
            lines.append("  Initial capital: <code>—</code>")
        lines.append(f"  Realized: <code>${realized:+,.4f}</code> across <code>{len(self.state.closed_trades)}</code> cycles")
        if bags_n:
            sign = "+" if unrealized >= 0 else ""
            lines.append(
                f"  Open: <code>{bags_n}</code> bag{'s' if bags_n != 1 else ''} · "
                f"exposure <code>${exposure:,.2f}</code> · "
                f"unrealized <code>{sign}${unrealized:,.4f}</code>"
            )
        else:
            lines.append("  Open: <code>0</code> bags · waiting for first entry")

        # Open orders block — distinguish bot-tracked vs orphan.
        if open_orders:
            tracked_ids: set[str] = set()
            if self.state.resting_buy:
                tracked_ids.add(self.state.resting_buy.order_id)
            for b in self.state.bags:
                if b.sell_order_id:
                    tracked_ids.add(b.sell_order_id)

            buys = sorted(
                (o for o in open_orders if o["side"] == "BUY"),
                key=lambda o: o["price"],
                reverse=True,
            )
            sells = sorted(
                (o for o in open_orders if o["side"] == "SELL"),
                key=lambda o: o["price"],
            )
            tracked_n = sum(1 for o in open_orders if str(o["order_id"]) in tracked_ids)
            orphan_n = len(open_orders) - tracked_n

            lines.append("")
            header = f"📋 <b>Open orders</b> ({len(open_orders)})"
            if orphan_n:
                header += f" · <code>{tracked_n}</code> bot · <code>{orphan_n}</code> orphan"
            else:
                header += " · all bot-tracked"
            lines.append(header)

            def _format_row(o: dict) -> str:
                tag = "🟢" if str(o["order_id"]) in tracked_ids else "⚠️"
                notional = o["price"] * o["qty"]
                # Truncate qty to reasonable precision for readability.
                qty_str = f"{o['qty']:.8f}".rstrip("0").rstrip(".") or "0"
                return (
                    f"  {tag} {o['side']:<4} <code>${o['price']:,.2f}</code> · "
                    f"<code>{qty_str}</code> ≈ <code>${notional:,.2f}</code>"
                )

            shown = 0
            cap = 12  # avoid blowing the Telegram 4096-char limit
            for o in buys + sells:
                if shown >= cap:
                    lines.append(f"  … (+{len(open_orders) - shown} more)")
                    break
                lines.append(_format_row(o))
                shown += 1
        else:
            lines.append("")
            lines.append("📋 <b>Open orders</b> · none")

        return "\n".join(lines)

    # ── Live-log narration (heartbeat) ──────────────────────────────
    # Rationale: the engine logs only on state transitions (HUNT seed,
    # TRAIL reprice, fill, bracket). Between transitions the live log
    # would otherwise be silent for minutes. This emits a compact one
    # liner every ~3s so the operator can *see* the bot's current
    # reasoning — anchor, distance to next buy/TP, unrealized P&L,
    # what it's waiting for.

    def _maybe_log_thoughts(self, price: float) -> None:
        now = time.time()
        if now - self._last_thoughts < _THOUGHTS_INTERVAL_S:
            return
        self._last_thoughts = now
        msg = self._compose_thoughts(price)
        # Suppress back-to-back identical lines (e.g. paper venue with
        # frozen price) — only emit when wording changes OR after 30s.
        if msg == self._last_thoughts_msg and (now - getattr(self, "_last_thoughts_emit", 0.0)) < 30.0:
            return
        self._last_thoughts_msg = msg
        self._last_thoughts_emit = now
        log.info("[%s] %s", self.label, msg)

    def _compose_pending_buy(self, price: float) -> Optional[Dict[str, Any]]:
        """
        Build a "what we're waiting on" block for the dashboard whenever
        there is NO resting buy on the book.

        Three reasons we'd be waiting:

          * `max_ammo`  — every bullet slot is filled; new buys are
                          disabled until a TP fills.
          * `backoff`   — the venue rejected the last buy attempt and
                          we're in cooldown (e.g. -2010 / 422).
          * `trail`     — normal climb-and-dip: the buy will be placed
                          when the local high crosses `trigger_high_price`
                          and the price subsequently falls to
                          `target_price`.

        Each entry includes a human-readable `label` plus the structured
        fields the frontend uses for live tile updates.
        """
        s = self.state
        p = self.params
        bags_n = len(s.bags)
        cap = p.max_bullets

        anchor = s.anchor_price
        target_price = round(anchor * p.dip_mult, 2) if anchor > 0 else 0.0
        trigger_high = round(anchor * p.trail_mult, 2) if anchor > 0 else 0.0
        # Once the buy fills, the auto-bracketed sell target is the
        # buy price scaled by the TP multiplier — show it now so the
        # user can see the round-trip plan, not just the entry leg.
        projected_sell = round(target_price * (1.0 + p.tp_pct / 100.0), 2) if target_price > 0 else 0.0

        # MAX_AMMO: no buy by design.
        if bags_n >= cap:
            return {
                "reason": "max_ammo",
                "target_price": target_price,
                "projected_sell_target": projected_sell,
                "label": (
                    f"All {cap} bullets deployed — waiting for any TP to "
                    f"fill before the next buy is armed."
                ),
            }

        # Backoff after a recent venue rejection (insufficient balance,
        # post-only crossed the spread, etc.).
        backoff_left = self._buy_retry_after - time.time() if self._buy_retry_after else 0.0
        if backoff_left > 0:
            return {
                "reason": "backoff",
                "target_price": target_price,
                "projected_sell_target": projected_sell,
                "backoff_remaining_s": int(backoff_left),
                "label": (
                    f"Last buy attempt was rejected by the venue — "
                    f"cooling down for {int(backoff_left)}s before retrying."
                ),
            }

        # Trail re-arm: how far does the local high still need to climb
        # before the engine cancels-and-replaces the buy at a new dip target?
        high_now = s.internal_high_record
        high_to_trigger_pct = (
            (trigger_high - high_now) / high_now * 100.0
            if high_now > 0 and trigger_high > 0 else 0.0
        )
        spot_to_target_pct = (
            (target_price - price) / price * 100.0
            if price > 0 and target_price > 0 else 0.0
        )

        if anchor <= 0:
            label = "Booting — waiting for the first valid market price."
        elif high_to_trigger_pct > 0:
            label = (
                f"Trail re-arms after the local high climbs to "
                f"${trigger_high:,.2f} (+{high_to_trigger_pct:.2f}% from "
                f"current high ${high_now:,.2f}). The buy will then be "
                f"placed at ${target_price:,.2f}."
            )
        else:
            # Trail already triggered: the next tick's reconcile loop
            # will place the buy. This window is normally sub-second.
            label = (
                f"Trail armed — placing buy at ${target_price:,.2f} on "
                f"the next reconcile tick."
            )

        return {
            "reason": "trail",
            "target_price": target_price,
            "projected_sell_target": projected_sell,
            "trigger_high_price": trigger_high,
            "current_high": round(high_now, 2),
            "high_to_trigger_pct": round(high_to_trigger_pct, 3),
            "spot_to_target_pct": round(spot_to_target_pct, 3),
            "label": label,
        }

    def _compose_thoughts(self, price: float) -> str:
        s = self.state
        p = self.params
        bags_n = len(s.bags)
        cap = p.max_bullets

        if s.anchor_price > 0:
            gap_anchor = (price - s.anchor_price) / s.anchor_price * 100
            anchor_part = f"anchor ${s.anchor_price:,.2f} ({gap_anchor:+.2f}%)"
        else:
            anchor_part = "anchor —"

        # ── 0 bags held: either ARMED (limit buy on the book waiting to
        #    fill) or HUNTING (no order placed yet, watching the trail).
        #    The distinction is purely cosmetic in the heartbeat — the
        #    engine's internal `state.hunting` boolean doesn't change.
        if bags_n == 0:
            target_buy = (
                round_price(s.anchor_price * p.dip_mult, p.price_prec)
                if s.anchor_price > 0 else 0.0
            )
            trail_trigger = s.anchor_price * p.trail_mult if s.anchor_price > 0 else 0.0
            high_to_trigger = (
                (trail_trigger - s.internal_high_record) / s.anchor_price * 100
                if s.anchor_price > 0 else 0.0
            )

            if s.resting_buy:
                dist_to_fill = (
                    (s.resting_buy.price - price) / price * 100
                    if price > 0 else 0.0
                )
                trail_part = (
                    f"trail re-arms when high crosses ${trail_trigger:,.2f} "
                    f"(need {high_to_trigger:+.2f}% from current high ${s.internal_high_record:,.2f})"
                ) if trail_trigger > 0 else "trail idle"
                return (
                    f"ARMED 0/{cap} · spot ${price:,.2f} · {anchor_part} · "
                    f"resting BUY ${s.resting_buy.price:,.2f} ({dist_to_fill:+.2f}% to fill) · "
                    f"{trail_part}"
                )
            backoff_left = self._buy_retry_after - time.time()
            backoff_part = (
                f" · ⏸ backoff {int(backoff_left)}s (last buy rejected)"
                if backoff_left > 0 else ""
            )
            return (
                f"HUNTING 0/{cap} · spot ${price:,.2f} · {anchor_part} · "
                f"no resting buy · target ${target_buy:,.2f} "
                f"(dip {p.dip_pct:.2f}% from anchor) · waiting for venue capacity"
                f"{backoff_part}"
            )

        # ── At least one bag held: aggregate position math ───────────
        qty_sum = sum(b.btc_amount for b in s.bags)
        avg_entry = (
            sum(b.btc_amount * b.buy_fill_price for b in s.bags) / qty_sum
            if qty_sum > 0 else 0.0
        )
        ur_pct = (price - avg_entry) / avg_entry * 100 if avg_entry > 0 else 0.0
        ur_usdt = qty_sum * (price - avg_entry)

        # Newest bag = top of LIFO stack = next TP we're waiting on
        newest = s.bags[-1]
        next_tp_dist = (
            (newest.sell_target_price - price) / price * 100
            if price > 0 else 0.0
        )
        oldest = s.bags[0]
        held_min = max(0.0, (time.time() - oldest.entry_ts) / 60.0)

        next_tp_part = (
            f"next TP (LIFO #{newest.bag_id}) ${newest.sell_target_price:,.2f} "
            f"({next_tp_dist:+.2f}% away)"
        )
        position_part = (
            f"avg entry ${avg_entry:,.2f} · "
            f"UR {ur_pct:+.2f}% (${ur_usdt:+.4f}) · "
            f"oldest bag held {held_min:.1f}m"
        )
        realized_part = f"realized ${s.realized_pnl_usdt:+.4f}"

        # ── MAX_AMMO: every slot used, only TPs are armed ────────────
        if bags_n >= cap:
            return (
                f"MAX_AMMO {bags_n}/{cap} · spot ${price:,.2f} · "
                f"{position_part} · {next_tp_part} · "
                f"{realized_part} · new buys disabled until a TP fills"
            )

        # ── ACTIVE: holding bags but room to add more ───────────────
        rb_part = (
            f"resting BUY ${s.resting_buy.price:,.2f}"
            if s.resting_buy
            else "no resting buy (will rearm on next bracket)"
        )
        return (
            f"ACTIVE {bags_n}/{cap} · spot ${price:,.2f} · "
            f"{position_part} · {next_tp_part} · {rb_part} · {realized_part}"
        )

    # ── Fill detection (each tick) ──────────────────────────────────

    async def _detect_fills(self, open_ids: set[str]) -> None:
        spec = self.venue.spec

        # Buy fill?
        rb = self.state.resting_buy
        buy_filled_this_tick = False
        if rb and rb.order_id not in open_ids:
            filled_qty = self.venue.filled_qty_after_fees(rb.requested_qty)
            bags_before = len(self.state.bags)
            intents = self.state.on_buy_filled(rb.order_id, rb.price, filled_qty)
            await self._apply_intents(intents)
            buy_filled_this_tick = True
            # If a new bag was opened, fire a per-lot Telegram on live venues.
            if spec.account_mode == "live" and len(self.state.bags) > bags_before:
                self._notify_buy_filled(self.state.bags[-1], rb.price)

        # Sell fills?
        #
        # CRITICAL: refresh `open_ids` if a buy filled this tick. The snapshot
        # passed in was taken BEFORE `_apply_intents` ran, so any TP SELL
        # placed for the just-filled bag would NOT be in it — and the loop
        # below would see `bag.sell_order_id not in open_ids` and falsely
        # conclude the fresh SELL has filled, closing the bag instantly at
        # its target price (phantom profit + an orphan SELL stranded on the
        # book locking the BTC). This was the cause of the 5 orphan SELLs
        # on Binance Live and the bag-3-with-no-BTC mess on Revolut Live.
        # One extra REST call per organic fill is essentially free; we
        # protect against the (rare) failure by skipping the SELL-fill check
        # entirely if the refresh fails — at worst we delay detecting one
        # real fill by one tick, never close a bag that didn't actually
        # close.
        if buy_filled_this_tick:
            try:
                open_ids = await asyncio.to_thread(self.venue.get_open_order_ids)
            except Exception as exc:
                log.debug(
                    "[%s] _detect_fills: refresh open_ids after buy-fill failed (%s) — "
                    "skipping SELL-fill check this tick",
                    self.label, exc,
                )
                return

        for bag in list(self.state.bags):
            if bag.sell_order_id and bag.sell_order_id not in open_ids:
                closed_before = len(self.state.closed_trades)
                snapshot = (bag.bag_id, bag.buy_fill_price, bag.sell_target_price, bag.btc_amount)
                intents = self.state.on_sell_filled(bag.sell_order_id, bag.sell_target_price)
                await self._apply_intents(intents)
                if spec.account_mode == "live" and len(self.state.closed_trades) > closed_before:
                    self._notify_sell_filled(self.state.closed_trades[-1], snapshot)

    # ── Manual force-buy (dashboard button) ─────────────────────────

    async def force_market_buy(self, amount_usdt: Optional[float] = None) -> Dict[str, Any]:
        """
        Place a MARKET BUY for ~`amount_usdt` of the quote asset, then
        thread the fill through the engine exactly like an organic buy:
        a new bag is opened, its TP sell is placed, and (if room) the
        next grid buy intent is emitted. Persistence + Telegram are
        triggered identically.

        Why this is safe:
          * Held under self._lock so the polling loop can't tick mid-mutation.
          * Refuses if the account_mode is not 'live' (paper venues
            simulate fills via their own tick driver and don't need this).
          * Honours MAX_AMMO so a user can't open more bags than the
            engine is allowed to manage.
          * Honours the buy-failure cooldown — the same one regular
            grid buys obey — to avoid retry storms when the venue
            keeps rejecting (insufficient balance, missing scopes, …).

        Returns a small status dict the API layer relays to the dashboard.
        """
        spec = self.venue.spec
        if spec.account_mode != "live":
            return {
                "ok": False,
                "reason": "paper_venue",
                "message": f"{self.label} is a paper venue — manual market buys are disabled.",
            }
        if not self.venue.is_ready():
            return {
                "ok": False,
                "reason": "not_ready",
                "message": f"{self.label} venue is not ready (missing credentials or whitelist).",
            }

        size = float(amount_usdt) if amount_usdt is not None else float(self.params.bullet_size_usdt)
        if size <= 0:
            return {"ok": False, "reason": "bad_amount", "message": "amount_usdt must be > 0"}
        if size < self.params.min_notional:
            return {
                "ok": False,
                "reason": "below_min_notional",
                "message": (
                    f"${size:.2f} is below the venue minimum notional "
                    f"of ${self.params.min_notional:.2f}."
                ),
            }

        async with self._lock:
            if len(self.state.bags) >= self.params.max_bullets:
                return {
                    "ok": False,
                    "reason": "max_ammo",
                    "message": (
                        f"Already at MAX_AMMO ({self.params.max_bullets} bags). "
                        "Wait for a TP fill before opening another."
                    ),
                }
            now = time.time()
            if now < self._buy_retry_after:
                wait = int(self._buy_retry_after - now)
                return {
                    "ok": False,
                    "reason": "in_backoff",
                    "message": (
                        f"Venue rejected recent buys; on cooldown for ~{wait}s. "
                        "Try again then."
                    ),
                }

            # Place the market order.
            try:
                placed = await asyncio.to_thread(self.venue.place_market_buy, size)
            except Exception as exc:
                cooldown = _classify_buy_failure_cooldown(exc)
                self._buy_retry_after = now + cooldown
                self._record_error(f"force_market_buy ${size:.2f} failed: {exc} → backoff {int(cooldown)}s")
                return {"ok": False, "reason": "venue_error", "message": str(exc)}

            self._buy_retry_after = 0.0
            self._buy_retry_after_price = 0.0

            fill_price = float(placed.price or 0.0)
            requested_qty = float(placed.requested_qty or 0.0)
            if fill_price <= 0 or requested_qty <= 0:
                # Defensive — venue accepted but didn't echo a fill.
                # We still install a bag so the user can manage it; use
                # current spot as the entry estimate.
                spot = await asyncio.to_thread(self.venue.get_price)
                fill_price = fill_price or float(spot)
                if requested_qty <= 0:
                    requested_qty = (size / fill_price) if fill_price > 0 else 0.0
                if fill_price <= 0 or requested_qty <= 0:
                    self._record_error(
                        f"force_market_buy: venue accepted order {placed.order_id} but no fill data"
                    )
                    return {
                        "ok": False,
                        "reason": "no_fill_data",
                        "message": "Order accepted but no fill data; check venue manually.",
                    }

            filled_qty = self.venue.filled_qty_after_fees(requested_qty)

            # Splice the fill into the engine. on_buy_filled gates on
            # self.resting_buy.order_id matching, so we install a tagged
            # placeholder first; the call clears it back to None.
            self.state.resting_buy = RestingBuy(
                order_id=placed.order_id,
                price=fill_price,
                requested_qty=requested_qty,
                tag="MANUAL_MARKET",
            )
            bags_before = len(self.state.bags)
            intents = self.state.on_buy_filled(placed.order_id, fill_price, filled_qty)
            await self._apply_intents(intents)
            await self._maybe_persist(force=True)

            new_bag = self.state.bags[-1] if len(self.state.bags) > bags_before else None
            if new_bag is not None:
                # Reuse the same Telegram template as organic fills, but
                # prefix with the manual marker so users can spot it.
                try:
                    notional = (new_bag.btc_amount or 0.0) * (new_bag.buy_fill_price or fill_price)
                    notifications.send(
                        f"⚡ <b>LIFO MANUAL BUY</b> ({self.label}) · lot #{new_bag.bag_id}\n"
                        f"Pair: <code>{spec.symbol}</code>\n"
                        f"Entry: <code>${new_bag.buy_fill_price:,.2f}</code> · "
                        f"Qty: <code>{new_bag.btc_amount:.8f}</code> {spec.base_asset} "
                        f"(≈ <code>${notional:,.2f}</code> {spec.quote_asset})\n"
                        f"TP target: <code>${new_bag.sell_target_price:,.2f}</code> "
                        f"(+{self.params.tp_pct:.2f}%)\n"
                        f"Open bags: <code>{len(self.state.bags)}/{self.params.max_bullets}</code>"
                    )
                except Exception as exc:
                    log.debug("[%s] manual-buy telegram failed: %s", self.label, exc)

                return {
                    "ok": True,
                    "venue": self.label,
                    "order_id": placed.order_id,
                    "bag_id": new_bag.bag_id,
                    "fill_price": new_bag.buy_fill_price,
                    "filled_qty": new_bag.btc_amount,
                    "notional_usdt": (new_bag.btc_amount or 0.0) * (new_bag.buy_fill_price or fill_price),
                    "sell_target_price": new_bag.sell_target_price,
                    "open_bags": len(self.state.bags),
                    "max_bullets": self.params.max_bullets,
                }

            return {
                "ok": False,
                "reason": "no_bag_opened",
                "message": "Engine accepted the fill but didn't open a bag (zero qty?).",
            }

    # ── Telegram fill notifications (live venues only) ──────────────

    def _notify_buy_filled(self, bag: Any, fill_price: float) -> None:
        """Telegram ping when a LIFO buy lot opens. Mirrors the legacy
        Binance grid bot's 📥 alert so the user sees fills on Revolut
        Live and Binance Live with identical formatting."""
        spec = self.venue.spec
        try:
            notional = (bag.btc_amount or 0.0) * (bag.buy_fill_price or fill_price)
            notifications.send(
                f"📥 <b>LIFO BUY filled</b> ({self.label}) · lot #{bag.bag_id}\n"
                f"Pair: <code>{spec.symbol}</code>\n"
                f"Entry: <code>${bag.buy_fill_price:,.2f}</code> · "
                f"Qty: <code>{bag.btc_amount:.8f}</code> {spec.base_asset} "
                f"(≈ <code>${notional:,.2f}</code> {spec.quote_asset})\n"
                f"TP target: <code>${bag.sell_target_price:,.2f}</code> "
                f"(+{self.params.tp_pct:.2f}%)\n"
                f"Open bags: <code>{len(self.state.bags)}/{self.params.max_bullets}</code>"
            )
        except Exception as exc:
            log.debug("[%s] buy-fill telegram failed: %s", self.label, exc)

    def _notify_sell_filled(self, closed: Any, snapshot: tuple) -> None:
        """Telegram ping when a LIFO sell (TP) lot closes — includes the
        realised P&L for the cycle and the running total."""
        spec = self.venue.spec
        bag_id, buy_px, sell_px, qty = snapshot
        # Prefer the closed-trade record if present (carries fee-aware PnL),
        # fall back to the snapshot we captured before the state mutation.
        entry = getattr(closed, "buy_fill_price", buy_px) or buy_px
        exit_ = getattr(closed, "sell_fill_price", sell_px) or sell_px
        amt = getattr(closed, "qty", qty) or qty
        pnl = getattr(closed, "gross_pnl_usdt", None)
        if pnl is None:
            pnl = (exit_ - entry) * amt
        pnl_pct = ((exit_ - entry) / entry * 100) if entry else 0.0
        running = sum(t.gross_pnl_usdt for t in self.state.closed_trades)
        try:
            notifications.send(
                f"💰 <b>LIFO SELL filled</b> ({self.label}) · lot #{bag_id}\n"
                f"Pair: <code>{spec.symbol}</code>\n"
                f"Exit: <code>${exit_:,.2f}</code> ← Entry: <code>${entry:,.2f}</code>\n"
                f"P&L: <code>{'+' if pnl >= 0 else ''}{pnl:.4f}</code> {spec.quote_asset} "
                f"(<code>{pnl_pct:+.2f}%</code>)\n"
                f"Closed cycles: <code>{len(self.state.closed_trades)}</code> · "
                f"Realised total: <code>{'+' if running >= 0 else ''}{running:.4f}</code> {spec.quote_asset}\n"
                f"Remaining bags: <code>{len(self.state.bags)}/{self.params.max_bullets}</code>"
            )
        except Exception as exc:
            log.debug("[%s] sell-fill telegram failed: %s", self.label, exc)

    # ── Main loop ──────────────────────────────────────────────────

    async def run(self) -> None:
        # Tag every log record emitted inside this asyncio task with the
        # runner's channel label. Set ONCE at task entry; the ContextVar
        # then propagates through every awaited call (including engine-
        # internal logs from api.lifo_grid._log()), so the dashboard can
        # filter the live log per channel without backend cooperation.
        log_buffer.set_channel(self.label)

        spec = self.venue.spec
        log.info(
            "[%s] starting venue=%s symbol=%s bullet=$%.2f max=%d tp=%.2f%% dip=%.2f%% step=%.2f%%",
            self.label, spec.name, spec.symbol,
            self.params.bullet_size_usdt, self.params.max_bullets,
            self.params.tp_pct, self.params.dip_pct, self.params.trail_step_pct,
        )

        if not self.venue.is_ready():
            log.warning("[%s] venue not ready (missing credentials?) — runner idle", self.label)
            return

        had_persisted = await self._load_state()
        price_now = await self._fetch_price_with_retry()
        if price_now <= 0:
            log.error("[%s] giving up: could not obtain a non-zero price on boot", self.label)
            return

        if had_persisted:
            log.info(
                "[%s] resumed from state.json bags=%d anchor=%.2f high=%.2f",
                self.label, len(self.state.bags), self.state.anchor_price, self.state.internal_high_record,
            )
            await self._reconcile_with_exchange()
        else:
            try:
                capital = float(self._forced_capital) if self._forced_capital else float(
                    await asyncio.to_thread(self.venue.starting_equity_usdt)
                )
            except Exception:
                capital = float(self._forced_capital or 0.0)
            self.state.starting_capital_usdt = capital
            self.state.on_startup(price_now)

        # Self-heal: convert any untracked BTC sitting in the wallet
        # (state-loss orphans, manual deposits, fee dust) into USDT so
        # the next HUNT_INITIAL has working capital. Runs once per boot,
        # AFTER reconciliation so live bags are correctly subtracted.
        await self._sweep_orphan_btc_if_any()

        self._start_ts = time.time()
        await self._maybe_persist(force=True)

        if spec.account_mode == "live":
            wallet_block = await self._build_wallet_block(price_now, resumed=had_persisted)
            ok = notifications.send(
                f"🧱 <b>LIFO Grid started</b> ({self.label})\n"
                f"Pair: <code>{spec.symbol}</code> @ <code>${price_now:,.2f}</code>\n"
                f"Bullets: <code>${self.params.bullet_size_usdt:.2f}</code> × "
                f"<code>{self.params.max_bullets}</code> · "
                f"TP <code>{self.params.tp_pct}%</code> · Dip <code>{self.params.dip_pct}%</code> · "
                f"Step <code>{self.params.trail_step_pct}%</code>\n"
                f"Anchor: <code>${self.state.anchor_price:,.2f}</code> · "
                f"Bags: <code>{len(self.state.bags)}</code>"
                f"{wallet_block}"
            )
            log.info("[%s] startup notification result: %s", self.label, ok)

        try:
            while True:
                try:
                    # Advance paper-venue tick (no-op on live).
                    advance = getattr(self.venue, "advance_tick", None)
                    if callable(advance):
                        await asyncio.to_thread(advance)

                    price = await asyncio.to_thread(self.venue.get_price)
                    try:
                        open_ids = await asyncio.to_thread(self.venue.get_open_order_ids)
                    except Exception:
                        open_ids = self._known_order_ids()

                    await self._detect_fills(open_ids)
                    # Self-heal any bag whose TP placement failed on a
                    # previous tick (typically post-redeploy state-loss
                    # where an orphan SELL locks the BTC). Cheap when
                    # everything is healthy: short-circuits unless at
                    # least one bag has sell_order_id is None.
                    await self._rearm_orphan_sells()
                    intents = self.state.tick_trailing(price)
                    if intents:
                        await self._apply_intents(intents)

                    self._tick_count += 1
                    self._maybe_log_thoughts(price)

                    snapshot = self._build_ws_snapshot(price)
                    await self.ws_manager.broadcast(snapshot, channel=spec.ws_channel)
                    await self._maybe_persist()

                except requests.HTTPError as exc:
                    resp = getattr(exc, "response", None)
                    body = resp.text if resp is not None else str(exc)
                    self._record_error(f"HTTP {getattr(resp, 'status_code', '?')}: {body[:200]}")
                except requests.ConnectionError:
                    self._record_error("network error (will retry)")
                except Exception as exc:
                    self._record_error(f"unexpected: {exc}")
                    log.error("[%s] tick crashed", self.label, exc_info=True)

                await asyncio.sleep(self.poll_interval)

        except asyncio.CancelledError:
            log.info("[%s] runner cancelled", self.label)
            await self._maybe_persist(force=True)
            uptime = time.time() - (self._start_ts or time.time())
            # Three reasons to skip the 🛑 Telegram message:
            #   1. Paper account → never notify.
            #   2. App-shutdown flag set → Railway is redeploying or the
            #      process is going away, so a new container has already
            #      (or will imminently) send 🧱 — no need to spam.
            #   3. Runner died within the 5s arm window → it never really
            #      started, suppress the false-positive goodbye.
            should_notify = (
                spec.account_mode == "live"
                and not _APP_SHUTTING_DOWN
                and uptime >= _STOP_NOTIFY_ARM_S
            )
            if should_notify:
                try:
                    last_price = await asyncio.to_thread(self.venue.get_price)
                except Exception:
                    last_price = price_now
                wallet_block = await self._build_wallet_block(last_price, resumed=True)
                notifications.send(
                    f"🛑 <b>LIFO Grid stopped</b> ({self.label})\n"
                    f"Bags: <code>{len(self.state.bags)}</code> · "
                    f"Closed cycles: <code>{len(self.state.closed_trades)}</code>"
                    f"{wallet_block}"
                )
            else:
                log.info(
                    "[%s] suppressing stop notification (uptime=%.1fs, app_shutdown=%s)",
                    self.label, uptime, _APP_SHUTTING_DOWN,
                )
            raise

    def _known_order_ids(self) -> set[str]:
        ids: set[str] = set()
        if self.state.resting_buy:
            ids.add(self.state.resting_buy.order_id)
        for b in self.state.bags:
            if b.sell_order_id:
                ids.add(b.sell_order_id)
        return ids

    # ── WebSocket snapshot builders ─────────────────────────────────

    def _build_ws_snapshot(self, price: float) -> dict:
        """
        All LIFO runners share the same dashboard shape (BotState),
        regardless of venue. The /ws/{channel} endpoints are venue-specific
        but the rendered UI is the same `LiveDashboardContent`, so the
        snapshot must always include positions, grid, cycles, session,
        alltime, errors, last_action, and logs.

        The `_snapshot_v2` shape is kept around as a reference for the
        old "force-fit into PaperV2DashboardContent" path, but is no
        longer wired up — the multi-strategy V2 dashboard is reserved
        for the actual `paper_runner_v2` sandbox runner.
        """
        return self._snapshot_live(price)

    # live (BotState) shape — consumed by LiveDashboardContent
    def _snapshot_live(self, price: float) -> dict:
        now = time.time()
        uptime = int(now - self._start_ts) if self._start_ts else 0
        spec = self.venue.spec

        positions = []
        for b in self.state.bags:
            ur_pct = (price - b.buy_fill_price) / b.buy_fill_price * 100 if b.buy_fill_price else 0
            ur_usdt = (price - b.buy_fill_price) * b.btc_amount
            positions.append({
                "slot_id": b.bag_id,
                "state": "HOLDING",
                "entry_price": b.buy_fill_price,
                "slot_qty": b.btc_amount,
                "tp_price": b.sell_target_price,
                "sell_order_id": b.sell_order_id,
                "unrealized_pct": round(ur_pct, 3),
                "unrealized_usdt": round(ur_usdt, 6),
                "age_s": int(now - b.entry_ts) if b.entry_ts else 0,
            })
        if not positions:
            positions.append({"slot_id": 0, "state": "WATCHING", "entry_price": 0})

        cycles = []
        for t in self.state.closed_trades[-50:]:
            gross_pct = (t.sell_fill_price - t.buy_fill_price) / t.buy_fill_price * 100 if t.buy_fill_price else 0
            cycles.append({
                "number": t.bag_id,
                "slot_id": t.bag_id,
                "buy_price": t.buy_fill_price,
                "sell_price": t.sell_fill_price,
                "gross_pct": round(gross_pct, 4),
                "net_pnl": round(t.gross_pnl_usdt, 6),
                "fee": 0,
                "timestamp": t.exit_ts,
            })

        equity = self._estimate_equity(price)
        resting = None
        if self.state.resting_buy:
            dist_pct = (price - self.state.resting_buy.price) / self.state.resting_buy.price * 100 if self.state.resting_buy.price else 0
            resting = {
                "order_id": self.state.resting_buy.order_id,
                "price": self.state.resting_buy.price,
                "kind": self.state.resting_buy.tag,
                "distance_pct": round(dist_pct, 3),
            }

        # Pending-buy block: when no order is on the book, surface what
        # the bot is *waiting* on so the dashboard isn't a black hole.
        # The frontend swaps this in where the resting-buy card would be.
        pending = self._compose_pending_buy(price) if resting is None else None

        return {
            "timestamp": now,
            "uptime_s": uptime,
            "symbol": spec.symbol,
            # Any LIVE venue (real money on the exchange) → green LIVE badge.
            # Paper venues (binance-testnet, revolut-paper) → amber TESTNET badge.
            "mainnet": spec.account_mode == "live",
            "venue": spec.name,
            "platform": spec.platform,
            "price": price,
            "ma": 0,
            "take_profit_pct": self.params.tp_pct,
            "stop_loss_pct": 0,
            "trade_size_usdt": self.params.bullet_size_usdt,
            "prices": [],
            "positions": positions,
            "cycles": cycles,
            "grid_mode": True,
            "grid": {
                "status": self.state.system_state(),
                "local_high": round(self.state.internal_high_record, 2),
                "anchor_price": round(self.state.anchor_price, 2),
                "resting_buy": resting,
                "pending_buy": pending,
                "open_lots": len(self.state.bags),
                "max_lots": self.params.max_bullets,
                "tranche_usdt": self.params.bullet_size_usdt,
                "tp_pct": self.params.tp_pct,
                "dip_pct": self.params.dip_pct,
                "trail_step_pct": self.params.trail_step_pct,
                "closed_count": len(self.state.closed_trades),
                "total_pnl": round(self.state.realized_pnl_usdt, 6),
            },
            "strategy": {
                "macro_regime": f"LIFO {self.state.system_state()}",
                "daily_bias": f"bullet ${self.params.bullet_size_usdt:.0f} × {self.params.max_bullets}",
                "market_mode": f"{len(self.state.bags)}/{self.params.max_bullets} bags",
                "action": self.state.system_state(),
                "reasons": self.state.event_log[-5:],
            },
            "session": {
                "starting_balance": round(self.state.starting_capital_usdt, 4),
                "equity_usdt": round(equity, 4),
                "fees_paid": 0,
            },
            "alltime": {
                "total_cycles": len(self.state.closed_trades),
                "total_net_pnl": round(self.state.realized_pnl_usdt, 6),
                "total_fees": 0,
                "first_cycle_ts": self.state.closed_trades[0].exit_ts if self.state.closed_trades else 0,
            },
            "errors": self.errors[-5:],
            "last_action": self.state.event_log[-1] if self.state.event_log else "starting...",
            # Send up to 100 raw entries; the frontend will channel-filter
            # and slice down to a small visible window. Each entry carries
            # a `channel` field (runner label or null = global).
            "logs": log_buffer.recent(100, modules=log_buffer.LIVE_MODULES),
        }

    # v2 (V2BotState) shape — consumed by PaperV2DashboardContent
    def _snapshot_v2(self, price: float) -> dict:
        now = time.time()
        spec = self.venue.spec
        equity = self._estimate_equity(price)
        starting = self.state.starting_capital_usdt or 1.0
        pnl = equity - starting
        pnl_pct = pnl / starting * 100 if starting > 0 else 0

        position: Optional[Dict[str, Any]] = None
        status = "HUNTING" if self.state.hunting else ("MAX_AMMO" if self.state.at_max_ammo else "HOLDING")
        if self.state.bags:
            qty_sum = sum(b.btc_amount for b in self.state.bags)
            avg_entry = sum(b.btc_amount * b.buy_fill_price for b in self.state.bags) / qty_sum if qty_sum else 0
            ur_pct = (price - avg_entry) / avg_entry * 100 if avg_entry else 0
            ur_usdt = qty_sum * (price - avg_entry)
            position = {
                "entry_price": round(avg_entry, 2),
                "entry_time": "",
                "qty": round(qty_sum, 8),
                "usdt": round(qty_sum * avg_entry, 4),
                "unrealized_pct": round(ur_pct, 4),
                "unrealized_usdt": round(ur_usdt, 6),
                "hold_minutes": 0,
                "open_lots": len(self.state.bags),
            }

        closed = self.state.closed_trades
        pnls = [t.gross_pnl_usdt for t in closed]
        winners = [p for p in pnls if p > 0]
        performance = {
            "total_trades": len(closed),
            "win_rate": round(len(winners) / len(pnls) * 100, 1) if pnls else 0,
            "total_pnl": round(sum(pnls), 4) if pnls else 0,
            "best_trade": round(max(pnls), 4) if pnls else 0,
            "worst_trade": round(min(pnls), 4) if pnls else 0,
            "avg_hold_time_min": round(sum(t.hold_seconds for t in closed) / 60 / len(closed), 2) if closed else 0,
        }

        trade_markers: List[Dict[str, Any]] = []
        for b in self.state.bags:
            trade_markers.append({
                "time": int(b.entry_ts),
                "position": "belowBar",
                "color": "#22c55e",
                "shape": "arrowUp",
                "text": f"Buy bag#{b.bag_id}",
                "price": round(b.buy_fill_price, 2),
                "tp_price": round(b.sell_target_price, 2),
                "side": "buy",
                "active": True,
            })
        for t in closed[-40:]:
            trade_markers.append({
                "time": int(t.entry_ts),
                "position": "belowBar",
                "color": "#22c55e",
                "shape": "arrowUp",
                "text": f"Buy bag#{t.bag_id}",
                "price": round(t.buy_fill_price, 2),
                "side": "buy",
                "active": False,
            })
            trade_markers.append({
                "time": int(t.exit_ts),
                "position": "aboveBar",
                "color": "#ef4444",
                "shape": "arrowDown",
                "text": f"Sell bag#{t.bag_id} {'+' if t.gross_pnl_usdt >= 0 else ''}{t.gross_pnl_usdt:.2f}",
                "price": round(t.sell_fill_price, 2),
                "side": "sell",
                "active": False,
            })

        bals = {}
        try:
            bals = self.venue.get_balances()
        except Exception:
            pass
        base_qty = bals.get(spec.base_asset, 0.0)
        quote_qty = bals.get(spec.quote_asset, 0.0)

        strat_state = {
            "id": "lifo_grid",
            "name": f"LIFO Grid ({spec.name})",
            "short": "LIFO",
            "pair": spec.symbol,
            "color": "#f59e0b" if spec.platform == "binance" else "#0666eb",
            "icon": "🧱",
            "status": status,
            "wallet": {
                "starting": round(starting, 2),
                "equity": round(equity, 4),
                "usdt": round(quote_qty, 4),
                "btc": round(base_qty, 8),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 4),
            },
            "position": position,
            "last_signal": {
                "action": self.state.system_state(),
                "reasons": self.state.event_log[-5:],
            },
            "indicators": {
                "lifo": {
                    "anchor_price": round(self.state.anchor_price, 2),
                    "internal_high_record": round(self.state.internal_high_record, 2),
                    "resting_buy": (
                        {
                            "price": round(self.state.resting_buy.price, 2),
                            "order_id": self.state.resting_buy.order_id,
                            "tag": self.state.resting_buy.tag,
                        }
                        if self.state.resting_buy else None
                    ),
                    "bags": [
                        {
                            "bag_id": b.bag_id,
                            "buy_fill_price": b.buy_fill_price,
                            "sell_target_price": b.sell_target_price,
                            "qty": b.btc_amount,
                        }
                        for b in self.state.bags
                    ],
                    "open_bags": len(self.state.bags),
                    "max_bullets": self.params.max_bullets,
                    "closed_trades": len(self.state.closed_trades),
                    "realized_pnl_usdt": round(self.state.realized_pnl_usdt, 4),
                }
            },
            "tp_price": None,
            "sl_price": None,
            "tp_type": "lifo_grid",
            "trade_history": [
                {
                    "entry_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t.entry_ts)) if t.entry_ts else "",
                    "exit_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t.exit_ts)) if t.exit_ts else "",
                    "entry_price": round(t.buy_fill_price, 2),
                    "exit_price": round(t.sell_fill_price, 2),
                    "qty": round(t.qty, 8),
                    "pnl": round(t.gross_pnl_usdt, 6),
                    "pnl_pct": round(
                        (t.sell_fill_price - t.buy_fill_price) / t.buy_fill_price * 100
                        if t.buy_fill_price else 0, 4,
                    ),
                    "net_profit_usdt": round(t.gross_pnl_usdt, 6),
                    "exit_reason": t.exit_reason,
                    "lot_id": t.bag_id,
                    "hold_seconds": round(t.hold_seconds, 1),
                }
                for t in closed[-30:]
            ],
            "performance": performance,
            "explanation": {
                "strategy_summary": (
                    f"LIFO tranche grid on {spec.name}. "
                    f"Trails live price with a single resting limit buy at "
                    f"−{self.params.dip_pct}% below the anchor; only re-prices after a "
                    f"+{self.params.trail_step_pct}% advance. Each fill brackets with a "
                    f"+{self.params.tp_pct}% TP and places the next grid buy."
                ),
                "current_state": (
                    f"{self.state.system_state()}: {len(self.state.bags)}/"
                    f"{self.params.max_bullets} bags, anchor ${self.state.anchor_price:,.2f}, "
                    f"equity ${equity:,.2f} ({pnl_pct:+.2f}% vs start)."
                ),
                "layer_summary": f"{len(self.state.bags)} bags, {len(self.state.closed_trades)} closed",
                "layers": [],
            },
        }

        return {
            "timestamp": now,
            "uptime_s": int(now - self._start_ts) if self._start_ts else 0,
            "symbol": spec.symbol,
            "price": round(price, 2),
            "prices": [],
            "trade_markers": trade_markers,
            "strategies": [strat_state],
            "global_summary": {
                "total_strategies": 1,
                "active_positions": 1 if self.state.bags else 0,
                "combined_equity": round(equity, 4),
                "combined_pnl": round(pnl, 4),
                "combined_pnl_pct": round(pnl_pct, 4),
                "starting_capital": round(starting, 2),
            },
            "glossary": {},
            "strategy_params": {},
            "strategy_params_meta": {},
        }

    def _estimate_equity(self, price: float) -> float:
        try:
            bals = self.venue.get_balances()
            spec = self.venue.spec
            return bals.get(spec.quote_asset, 0.0) + bals.get(spec.base_asset, 0.0) * price
        except Exception:
            fallback_btc = sum(b.btc_amount for b in self.state.bags)
            return self.state.starting_capital_usdt + self.state.realized_pnl_usdt + fallback_btc * price


# ── Convenience entry points ────────────────────────────────────────


async def run_lifo_runner(
    venue: Venue,
    params: LifoGridParams,
    ws_manager: WSManager,
    *,
    label: str,
    poll_interval: float,
    starting_capital_usdt: Optional[float] = None,
) -> None:
    runner = LifoRunner(
        venue=venue,
        params=params,
        ws_manager=ws_manager,
        label=label,
        poll_interval=poll_interval,
        starting_capital_usdt=starting_capital_usdt,
    )
    _RUNNERS[label] = runner
    try:
        await runner.run()
    finally:
        # Drop the registration on exit so a relaunched runner replaces
        # any stale reference. Idempotent — pop is safe if already gone.
        if _RUNNERS.get(label) is runner:
            _RUNNERS.pop(label, None)
