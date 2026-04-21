"""
Shared helpers for the reset_<venue>_live.py scripts.

Each reset script does the same five steps in the same order:

  1. Snapshot venue state (open orders + balances + spot)
  2. Confirm with the user
  3. Cancel every open order
  4. Wait briefly so locked balance releases, then dump all base asset
     to quote at MARKET (taker fees acceptable for a one-shot reset)
  5. Flip LIFO_RESET_<VENUE> on Railway so the next boot purges the
     persisted bot state, then trigger a redeploy

Steps 3-5 each have an opt-out flag so the script can be used as a
diagnostic-only tool (`--dry-run`) or to skip the Railway leg when
running locally against a non-deployed instance (`--no-deploy`).

The script is intentionally venue-agnostic in this module; the per-venue
entry points just pass a `Venue` instance + label and handle their own
factory imports.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable, Optional


# Dust threshold below which we don't bother trying to market-sell the
# base asset — anything under $1 is almost always going to either:
#   a) hit the venue min-notional and reject, or
#   b) net less than the taker fee.
# Keep it small enough that we still catch real holdings (the LIFO bullet
# size is $6-$10) while skipping dust.
_DUST_USD = 1.0

# Seconds to wait between cancel_all and reading balances. Revolut takes
# ~1s to release locked funds; Binance is usually instant but the small
# pause gives both venues a clean read.
_RELEASE_DELAY_S = 2.0


def parse_args(venue_label: str) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=f"Cancel all {venue_label} orders, dump BTC at market, "
                    f"reset bot state, redeploy.",
    )
    p.add_argument("--yes", action="store_true", help="skip the interactive confirm prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="show plan only — no cancel, no sell, no Railway changes")
    p.add_argument("--no-sell", action="store_true",
                   help="cancel orders but do NOT market-sell base asset")
    p.add_argument("--no-deploy", action="store_true",
                   help="skip the Railway env-var flip + redeploy step")
    p.add_argument("--service", default="backend",
                   help="Railway service name (default: backend)")
    return p.parse_args()


def _snapshot(venue: Any) -> dict:
    """Pull a fresh read of orders + balances + spot from the venue."""
    spec = venue.spec
    orders = venue.get_open_orders_detail()
    balances = venue.get_detailed_balances()
    try:
        spot = float(venue.get_price() or 0.0)
    except Exception:
        spot = 0.0

    base_free = float(balances.get(spec.base_asset, {}).get("free", 0.0))
    base_locked = float(balances.get(spec.base_asset, {}).get("locked", 0.0))
    quote_free = float(balances.get(spec.quote_asset, {}).get("free", 0.0))
    quote_locked = float(balances.get(spec.quote_asset, {}).get("locked", 0.0))

    return {
        "spec": spec,
        "orders": orders,
        "spot": spot,
        "base_free": base_free,
        "base_locked": base_locked,
        "quote_free": quote_free,
        "quote_locked": quote_locked,
    }


def _print_snapshot(label: str, snap: dict) -> None:
    spec = snap["spec"]
    spot = snap["spot"]
    base_total = snap["base_free"] + snap["base_locked"]
    quote_total = snap["quote_free"] + snap["quote_locked"]
    base_value = base_total * spot

    print(f"\n=== {label} ===")
    print(f"  spot:           ${spot:,.2f}")
    print(f"  {spec.quote_asset:<6}:        free ${snap['quote_free']:,.2f}  locked ${snap['quote_locked']:,.2f}  total ${quote_total:,.2f}")
    print(f"  {spec.base_asset:<6}:        free {snap['base_free']:.8f}  locked {snap['base_locked']:.8f}  total {base_total:.8f}  (≈ ${base_value:,.2f})")

    if snap["orders"]:
        print(f"  open orders:    {len(snap['orders'])}")
        for o in snap["orders"]:
            side = str(o.get("side", "?")).ljust(5)
            price = float(o.get("price", 0))
            qty = float(o.get("qty", 0))
            oid = o.get("order_id", "?")
            notional = price * qty
            print(f"    - {side} {qty:.8f} @ ${price:,.2f}  (≈ ${notional:,.2f})  id={oid}")
    else:
        print("  open orders:    none")


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _set_railway_reset_var(service: str, env_var: str) -> bool:
    """Set LIFO_RESET_<VENUE>=1 on the given Railway service."""
    import subprocess
    print(f"\n[railway] setting {env_var}=1 on service '{service}' ...")
    proc = subprocess.run(
        ["railway", "variables", "--service", service, "--set", f"{env_var}=1"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[railway] FAILED: {proc.stderr.strip() or proc.stdout.strip()}")
        return False
    print("[railway] env var set.")
    return True


def _trigger_railway_redeploy(service: str) -> bool:
    import subprocess
    print(f"[railway] redeploying service '{service}' ...")
    proc = subprocess.run(
        ["railway", "redeploy", "--service", service, "--yes"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # Older railway CLIs use --skip-build instead of --yes; fall back.
        proc = subprocess.run(
            ["railway", "redeploy", "--service", service],
            capture_output=True, text=True, input="y\n",
        )
    if proc.returncode != 0:
        print(f"[railway] redeploy FAILED: {proc.stderr.strip() or proc.stdout.strip()}")
        return False
    print("[railway] redeploy triggered.")
    return True


def run_reset(
    *,
    label: str,
    venue_factory: Callable[[], Any],
    reset_env_var: str,
    args: argparse.Namespace,
) -> int:
    """
    Returns shell exit code: 0 on success, 1 on user-cancel, 2 on venue
    error, 3 on Railway error.
    """
    print(f"\n--- {label} reset ---")
    if args.dry_run:
        print("(dry-run mode: no destructive action will be taken)")

    try:
        venue = venue_factory()
    except Exception as exc:
        print(f"FATAL: failed to construct venue: {exc}")
        return 2

    try:
        before = _snapshot(venue)
    except Exception as exc:
        print(f"FATAL: failed to read venue state: {exc}")
        return 2

    _print_snapshot("BEFORE", before)
    spec = before["spec"]
    base_total = before["base_free"] + before["base_locked"]
    base_value = base_total * before["spot"]

    plan: list[str] = []
    if before["orders"]:
        plan.append(f"cancel {len(before['orders'])} open order(s)")
    will_sell = (
        not args.no_sell
        and base_total > 0
        and (base_value >= _DUST_USD or base_value == 0)  # always show if there's any
    )
    if will_sell and base_value >= _DUST_USD:
        plan.append(f"MARKET SELL {base_total:.8f} {spec.base_asset} (≈ ${base_value:,.2f}) for {spec.quote_asset}")
    elif base_total > 0 and base_value < _DUST_USD:
        plan.append(f"skip dust sell ({base_total:.8f} {spec.base_asset} ≈ ${base_value:,.2f} < ${_DUST_USD})")
    if not args.no_deploy:
        plan.append(f"set Railway env {reset_env_var}=1 on service '{args.service}'")
        plan.append(f"redeploy Railway service '{args.service}'")

    if not plan:
        print("\nNothing to do. Already flat.")
        return 0

    print("\nPLAN:")
    for step in plan:
        print(f"  - {step}")

    if args.dry_run:
        print("\n(dry-run) skipping execution.")
        return 0

    if not args.yes and not _confirm("\nProceed?"):
        print("Aborted.")
        return 1

    # ── 1. Cancel all open orders ────────────────────────────────────
    if before["orders"]:
        print("\n[venue] cancelling all open orders ...")
        try:
            venue.cancel_all()
        except Exception as exc:
            print(f"[venue] cancel_all FAILED: {exc}")
            return 2
        time.sleep(_RELEASE_DELAY_S)

    # ── 2. Sell remaining base asset at market ───────────────────────
    if not args.no_sell:
        try:
            mid = _snapshot(venue)
        except Exception as exc:
            print(f"[venue] post-cancel snapshot failed: {exc}")
            return 2
        base_total_now = mid["base_free"] + mid["base_locked"]
        spot_now = mid["spot"]
        value_now = base_total_now * spot_now

        if base_total_now <= 0:
            print("[venue] no base asset to sell.")
        elif value_now < _DUST_USD:
            print(f"[venue] skipping sell: only {base_total_now:.8f} {spec.base_asset} (≈ ${value_now:,.2f}) — dust.")
        else:
            sell_fn = getattr(venue, "place_market_sell", None)
            if not callable(sell_fn):
                print(f"[venue] FATAL: {spec.name} has no place_market_sell — cannot dump base asset.")
                return 2
            print(f"[venue] market-selling {base_total_now:.8f} {spec.base_asset} (≈ ${value_now:,.2f}) ...")
            # Pass FREE only — locked funds need cancel propagation that
            # may not have caught up yet on our 2s read; better to leave
            # any residual locked bit for a manual follow-up.
            sell_qty = mid["base_free"]
            if sell_qty <= 0:
                print(f"[venue] all {spec.base_asset} still shows locked after cancel; "
                      f"sleep 5s and retry one more time ...")
                time.sleep(5.0)
                mid = _snapshot(venue)
                sell_qty = mid["base_free"]
            if sell_qty <= 0:
                print(f"[venue] WARN: {spec.base_asset} still locked; cannot sell. "
                      f"Re-run after the venue releases the funds.")
            else:
                try:
                    placed = sell_fn(sell_qty)
                    print(f"[venue] market sell OK: order_id={placed.order_id} "
                          f"qty={placed.requested_qty:.8f} fill_price=${placed.price:,.2f}")
                except Exception as exc:
                    print(f"[venue] market sell FAILED: {exc}")
                    return 2
            time.sleep(_RELEASE_DELAY_S)

    # ── 3. Verify ────────────────────────────────────────────────────
    try:
        after = _snapshot(venue)
        _print_snapshot("AFTER", after)
    except Exception as exc:
        print(f"[venue] post-action snapshot failed: {exc}")
        # not fatal — the cancel + sell already ran

    # ── 4. Flip Railway env + redeploy ───────────────────────────────
    if not args.no_deploy:
        if not _set_railway_reset_var(args.service, reset_env_var):
            print("\nVenue side is clean but Railway env var was NOT set. "
                  "Set it manually then redeploy:")
            print(f"  railway variables --service {args.service} --set '{reset_env_var}=1'")
            print(f"  railway redeploy --service {args.service}")
            return 3
        if not _trigger_railway_redeploy(args.service):
            return 3

        print(
            f"\n--- DONE ---\n"
            f"After the new deploy completes and the bot is HUNTING (no bags, "
            f"resting BUY at -{0.75}% from current spot), unset the reset flag:\n"
            f"  railway variables --service {args.service} --remove {reset_env_var}\n"
            f"Otherwise the next redeploy will wipe state again."
        )
    else:
        print(
            f"\n--- DONE (venue side only) ---\n"
            f"To complete the reset, set the env var + redeploy manually:\n"
            f"  railway variables --service {args.service} --set '{reset_env_var}=1'\n"
            f"  railway redeploy --service {args.service}\n"
            f"Then unset {reset_env_var} after the new boot is HUNTING."
        )

    return 0


def main_for(label: str, venue_factory: Callable[[], Any], reset_env_var: str) -> int:
    args = parse_args(label)
    return run_reset(
        label=label,
        venue_factory=venue_factory,
        reset_env_var=reset_env_var,
        args=args,
    )
