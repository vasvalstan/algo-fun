"""
Read-only probe: Binance ground truth vs. bot state.

Run from repo root (uses .env credentials):
    python -m scripts.probe_binance_state

What it does
------------
1. Queries Binance /api/v3/account → BTC / USDT / USDC free + locked balance.
2. Queries Binance /api/v3/openOrders for BOTH BTCUSDT and BTCUSDC so we
   surface stale orders left over from the migration.
3. Loads ./data/state_lifo_binance_live.json (or LIFO_STATE_DIR) and
   prints bag count, bag_seq, total tracked BTC, and per-bag detail.
4. Cross-checks:
     * sum(bag.btc_amount) vs wallet free+locked BTC
     * sum(open SELL qty) vs wallet locked BTC
     * each bag.sell_order_id vs Binance open orders

Nothing is placed, modified, cancelled, or moved. Fully read-only.

Run this AFTER seeing -2010 / "would immediately match" or
"insufficient balance" errors in the dashboard — it tells you which bag
is mis-aligned with reality.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import config
from auth import signed_request


SYMBOLS_TO_CHECK = ("BTCUSDC", "BTCUSDT")


def _account() -> dict:
    return signed_request("GET", "/api/v3/account")


def _open_orders(symbol: str) -> list[dict]:
    return signed_request("GET", "/api/v3/openOrders", {"symbol": symbol})


def _ticker(symbol: str) -> dict:
    return signed_request("GET", "/api/v3/ticker/price", {"symbol": symbol})


def _state_file_path() -> Path:
    base = Path(os.getenv("LIFO_STATE_DIR", "./data"))
    return base / "state_lifo_binance_live.json"


def _load_state() -> dict[str, Any] | None:
    p = _state_file_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"[!] failed to parse {p}: {exc}")
        return None


def _balance(account: dict, asset: str) -> tuple[float, float]:
    for b in account.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0.0)), float(b.get("locked", 0.0))
    return 0.0, 0.0


def main() -> int:
    print("═" * 72)
    print(" BINANCE GROUND TRUTH vs. BOT STATE")
    print("═" * 72)

    # ── 1. Wallet balances ────────────────────────────────────────────
    print("\n[1] Wallet balances (live):")
    try:
        acct = _account()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ /account failed: {exc}")
        return 1

    for asset in ("BTC", "USDC", "USDT", "BNB"):
        free, locked = _balance(acct, asset)
        if free or locked:
            print(f"  {asset:>5}  free={free:.8f}  locked={locked:.8f}  total={free+locked:.8f}")

    # ── 2. Open orders on both symbols ────────────────────────────────
    print("\n[2] Open orders on Binance:")
    open_orders_by_symbol: dict[str, list[dict]] = {}
    for sym in SYMBOLS_TO_CHECK:
        try:
            oo = _open_orders(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: ✗ {exc}")
            continue
        open_orders_by_symbol[sym] = oo
        if not oo:
            print(f"  {sym}: (none)")
            continue
        print(f"  {sym}: {len(oo)} open")
        for o in oo:
            print(
                f"    • {o.get('side'):4s}  qty={o.get('origQty')}  "
                f"price={o.get('price')}  type={o.get('type')}  "
                f"orderId={o.get('orderId')}  status={o.get('status')}"
            )

    # ── 3. Spot prices for context ────────────────────────────────────
    print("\n[3] Live spot prices:")
    for sym in SYMBOLS_TO_CHECK:
        try:
            tk = _ticker(sym)
            print(f"  {sym}: {float(tk['price']):.2f}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: ✗ {exc}")

    # ── 4. Bot state file ─────────────────────────────────────────────
    print(f"\n[4] Bot state file: {_state_file_path()}")
    state = _load_state()
    if state is None:
        print("  (no state file found — bot is on a fresh boot or running on a different host)")
        print("\n[5] Cross-check skipped (no local state to compare).")
        return 0

    bags = state.get("bags", []) or []
    bag_seq = state.get("_bag_seq", 0)
    resting_buy = state.get("resting_buy")
    print(f"  bag_seq            = {bag_seq}")
    print(f"  open bags          = {len(bags)}")
    print(f"  resting_buy        = {resting_buy}")
    print(f"  closed_trades      = {len(state.get('closed_trades', []))}")

    if bags:
        print("\n  Per-bag detail:")
        print("    bag_id  buy_fill_price   btc_amount    sell_target_price   sell_order_id")
        for b in bags:
            print(
                f"    #{int(b.get('bag_id', 0)):<5d}"
                f"  {float(b.get('buy_fill_price', 0)):>12.2f}"
                f"   {float(b.get('btc_amount', 0)):>11.8f}"
                f"     {float(b.get('sell_target_price', 0)):>12.2f}"
                f"      {b.get('sell_order_id') or '(none)'}"
            )

    # ── 5. Cross-check ────────────────────────────────────────────────
    print("\n[5] Cross-check (bot state vs Binance reality):")

    btc_free, btc_locked = _balance(acct, "BTC")
    btc_total = btc_free + btc_locked
    bag_btc_sum = sum(float(b.get("btc_amount", 0)) for b in bags)
    drift = btc_total - bag_btc_sum
    print(f"  Σ bag.btc_amount   = {bag_btc_sum:.8f}")
    print(f"  wallet BTC total   = {btc_total:.8f}")
    print(f"  drift (wallet-bag) = {drift:+.8f}  "
          f"({'wallet has MORE than bot tracks' if drift > 1e-8 else 'wallet has LESS than bot tracks' if drift < -1e-8 else 'aligned'})")

    cur_sym = config.SYMBOL  # what the bot would trade right now
    cur_open = open_orders_by_symbol.get(cur_sym, [])
    cur_sells = [o for o in cur_open if o.get("side") == "SELL"]
    bag_sell_ids = {b.get("sell_order_id") for b in bags if b.get("sell_order_id")}
    venue_sell_ids = {str(o.get("orderId")) for o in cur_sells}

    print(f"\n  Active SYMBOL      = {cur_sym}")
    print(f"  bot tracks SELL ids: {bag_sell_ids or '(none)'}")
    print(f"  Binance open SELLs : {venue_sell_ids or '(none)'}")
    missing = bag_sell_ids - venue_sell_ids
    extra = venue_sell_ids - bag_sell_ids
    if missing:
        print(f"  ⚠ bot tracks but Binance does NOT have: {missing}")
        print(f"    (these bags will spam place_sell every tick — engine will retry)")
    if extra:
        print(f"  ⚠ Binance has but bot does NOT track: {extra}")
        print(f"    (orphan SELLs — startup reconciliation usually adopts these)")
    if not missing and not extra:
        print(f"  ✓ SELL order tracking is consistent.")

    # Stale orders on the OTHER symbol (migration leftovers)
    other = "BTCUSDT" if cur_sym == "BTCUSDC" else "BTCUSDC"
    other_open = open_orders_by_symbol.get(other, [])
    if other_open:
        print(
            f"\n  ⚠ {len(other_open)} STALE order(s) still open on {other} — "
            f"these lock balance the {cur_sym} bot can't see/use."
        )

    print("\n" + "═" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
