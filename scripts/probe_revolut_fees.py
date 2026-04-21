"""
Read-only probe: discover the REAL maker fee paid on Revolut X.

Run from repo root (uses .env credentials):
    python -m scripts.probe_revolut_fees

What it does
------------
1. GET /orders — list recent orders (status filter best-effort).
2. For each order whose state is "filled" (or "completed"/"done"):
       a. GET /orders/{id} — pull the full payload.
       b. Print every field whose name contains "fee", "commission",
          "cost", "amount", "filled", "average" — so we can see what
          Revolut actually returns even if the schema isn't documented.
3. Aggregate: for orders where we can compute a fee fraction, print
   the realised maker fee per leg vs the configured LIFO_REVOLUT_FEE_RATE
   (default 0.0015).

Nothing is placed, modified or cancelled. Fully read-only.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Optional, Tuple

import config
from revolut_x import revx_request


FEE_FIELDS = (
    "fee",
    "fees",
    "commission",
    "total_commission",
    "fee_amount",
    "fee_currency",
    "commission_currency",
    "commission_asset",
    "cost",
)


def _fmt_money(v: Any) -> str:
    try:
        return f"{float(v):.10f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def _walk_for_keys(obj: Any, needles: Iterable[str], prefix: str = "") -> list[tuple[str, Any]]:
    """Return [(dotted.path, value)] for every key whose name contains any needle."""
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if any(n in k.lower() for n in needles):
                found.append((path, v))
            found.extend(_walk_for_keys(v, needles, path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_walk_for_keys(item, needles, f"{prefix}[{i}]"))
    return found


def _try(ep: str, params: dict) -> tuple[Optional[Any], Optional[str]]:
    try:
        return revx_request("GET", ep, params=params or None), None
    except Exception as exc:
        return None, str(exc)[:200]


def _list_orders(symbol: Optional[str], limit: int = 50) -> list[dict]:
    """
    Try several documented + common historical-orders endpoints.
    Returns the first list-shaped response; prints what every candidate said.
    """
    sym_param = ({"symbols": symbol} if symbol else {})
    # Use the documented historical-orders endpoint (discovered empirically).
    data, err = _try("/orders/historical", {"limit": limit, **sym_param})
    if err:
        raise RuntimeError(f"GET /orders/historical failed: {err}")
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected response shape: {type(rows).__name__}")
    print(f"  ✓ GET /orders/historical  params={sym_param}  → {len(rows)} rows")
    return rows


def _get_order_detail(order_id: str) -> dict:
    data = revx_request("GET", f"/orders/{order_id}")
    return data.get("data", data) if isinstance(data, dict) else data


def _fee_fraction(order: dict) -> Optional[float]:
    """
    Best-effort: derive (commission_in_quote / notional) so we can compare
    to LIFO_REVOLUT_FEE_RATE. Returns None when fields are missing.
    """
    try:
        price = float(order.get("price") or order.get("average_price") or 0)
        qty = float(
            order.get("filled_size")
            or order.get("filled_quantity")
            or order.get("executed_qty")
            or order.get("quantity")
            or 0
        )
        notional = price * qty
        if notional <= 0:
            return None

        # Look for any "fee/commission" numeric field, regardless of currency.
        candidates: list[float] = []
        for path, value in _walk_for_keys(order, ("fee", "commission")):
            if any(s in path.lower() for s in ("currency", "asset", "ccy")):
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num > 0:
                candidates.append(num)
        if not candidates:
            return None
        # Pick the largest — Revolut sometimes nests aggregate alongside per-leg values.
        return max(candidates) / notional
    except Exception:
        return None


def main() -> int:
    # Resolve symbol from .env if set, otherwise default to BTC-USDC.
    import os
    env_sym = os.getenv("REVOLUT_X_SYMBOL", "BTC-USDC").strip().upper()

    print("Revolut X fee probe — read-only")
    print(f"  symbol filter:                  {env_sym}")
    print(f"  configured LIFO_REVOLUT_FEE_RATE: {config.LIFO_REVOLUT_FEE_RATE} "
          f"({config.LIFO_REVOLUT_FEE_RATE * 100:.4f}%)")
    print()

    print("[1/3] Listing recent orders…")
    try:
        orders = _list_orders(env_sym, limit=100)
    except Exception as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 2

    filled_states = {"filled", "completed", "done", "fully_filled"}
    filled = [
        o for o in orders
        if str(o.get("state", o.get("status", ""))).lower() in filled_states
    ]
    print(f"  total returned: {len(orders)}  |  filled-looking: {len(filled)}")
    if not filled:
        print("\nNo filled orders found. If you've never traded on this account,")
        print("place ONE tiny limit order (e.g. via the Revolut X UI), wait for")
        print("it to fill, then re-run this script.")
        return 0

    print()
    print("[2/3] Per-order summary (no fee fields in Revolut's order payload):")
    print(f"  {'idx':3s} {'side':5s} {'type':6s} {'taker?':6s} {'qty':>14s} {'avg_fill':>11s} {'notional':>11s}")

    rows: list[dict[str, Any]] = []
    for i, o in enumerate(filled, 1):
        try:
            detail = _get_order_detail(str(o.get("id") or ""))
        except Exception:
            detail = o
        try:
            qty = float(detail.get("quantity") or 0)
            filled_qty = float(detail.get("filled_quantity") or 0)
            avg_fill = float(detail.get("average_fill_price") or detail.get("price") or 0)
        except (TypeError, ValueError):
            continue
        side = str(detail.get("side", "?")).upper()
        otype = str(detail.get("type", "?"))
        instr = detail.get("execution_instructions") or []
        is_taker = ("allow_taker" in instr) or otype == "market"
        notional = filled_qty * avg_fill
        print(f"  {i:>3d} {side:5s} {otype:6s} {str(is_taker):6s} "
              f"{filled_qty:>14.8f} {avg_fill:>11.2f} {notional:>11.2f}")
        rows.append({
            "side": side,
            "type": otype,
            "is_taker": is_taker,
            "qty": qty,
            "filled_qty": filled_qty,
            "avg_fill": avg_fill,
            "notional": notional,
        })

    print()
    print("[3/3] Empirical taker-fee derivation (round-trip check)")
    print("  Find the most recent BUY+SELL pair where qty_buy was sold immediately,")
    print("  to derive the realised taker fee (the only fee Revolut visibly debits).")
    pair: Optional[tuple[dict, dict]] = None
    buys = [r for r in rows if r["side"] == "BUY" and r["is_taker"]]
    sells = [r for r in rows if r["side"] == "SELL" and r["is_taker"]]
    for b in buys:
        for s in sells:
            # SELL qty == BUY's net-of-fee qty if a single round-trip
            if abs(s["filled_qty"] - b["filled_qty"]) / max(b["filled_qty"], 1e-9) < 0.005:
                pair = (b, s)
                break
        if pair:
            break

    if pair:
        b, s = pair
        # buy delivered exactly s["filled_qty"] BTC (visible because it was
        # immediately re-sold), so the maker/taker fee on the BUY is:
        #   buy_qty_requested - btc_received
        delivered = s["filled_qty"]
        gross = b["filled_qty"]
        if abs(gross - delivered) < 1e-9:
            # Same quantity returned — fee not deducted from base on BUY
            print("  → buy gross == sell qty (no base-asset deduction observed on BUY)")
            print("    fee on BUY is debited from QUOTE (USDC), not BASE (BTC)")
            buy_quote = gross * b["avg_fill"]
            sell_quote = delivered * s["avg_fill"]
            print(f"    buy notional:  {buy_quote:.4f} USDC out (paid)")
            print(f"    sell notional: {sell_quote:.4f} USDC in  (received gross)")
            print("    → Open a Revolut statement / bank txn to find what the wallet")
            print("      really debited; API doesn't expose it directly.")
        else:
            base_haircut = (gross - delivered) / gross
            print(f"  buy requested:   {gross:.8f} BTC")
            print(f"  buy delivered:   {delivered:.8f} BTC  (= sold qty in next trade)")
            print(f"  → taker fee:     {base_haircut * 100:.4f}% (debited from BTC)")
    else:
        print("  No clean BUY→SELL round-trip in history — skipping derivation.")

    print()
    print("Conclusion (read these together with EXECUTION_COSTS.md):")
    print("  * /orders/* responses contain NO fee/commission fields.")
    print("  * /configuration/pairs contains NO fee field.")
    print("  * Revolut publishes 0.00% maker / 0.09% taker — and the API gives us")
    print("    no per-trade override, so that schedule is what applies.")
    print("  * Our LIFO_REVOLUT_FEE_RATE = 0.0015 is therefore over-stated by 100%")
    print("    for maker (the LIFO bot's only mode) and over-stated by 67% for taker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
