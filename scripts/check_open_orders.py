"""
One-off diagnostic: dump all open Binance BTCUSDT orders and flag any
that look like the dashboard's $74,971.33 BUY.

Run from repo root:
    python -m scripts.check_open_orders
"""

from __future__ import annotations

import json
import sys
from typing import Any

import config
from trading import get_account, get_open_orders


TARGET_PRICE = 74971.33
PRICE_TOLERANCE = 5.0  # USDT


def _fmt(o: dict[str, Any]) -> str:
    side = o.get("side", "?")
    typ = o.get("type", "?")
    price = float(o.get("price", 0) or 0)
    qty = float(o.get("origQty", 0) or 0)
    executed = float(o.get("executedQty", 0) or 0)
    status = o.get("status", "?")
    notional = price * qty
    oid = o.get("orderId", "?")
    cli = o.get("clientOrderId", "")
    ts = o.get("time", 0)
    return (
        f"  [{oid}] {side:4s} {typ:12s} qty={qty:.8f} (filled {executed:.8f}) "
        f"@ {price:>12.2f} = ${notional:>8.2f}  status={status}  "
        f"cli={cli}  time_ms={ts}"
    )


def main() -> int:
    print(f"Endpoint:  {config.BASE_URL}")
    print(f"Symbol:    {config.SYMBOL}")
    print(f"Mainnet:   {config.USE_MAINNET}")
    print()

    try:
        acct = get_account()
    except Exception as exc:
        print(f"ERROR /api/v3/account: {exc}", file=sys.stderr)
        print(
            "\nIf this is -2015 Invalid API-key/IP/permissions, the API key "
            "is IP-whitelisted to the Railway pod (208.77.246.15) and refuses "
            "your local IP. Either temporarily allow your home IP on the key, "
            "or run this from the Railway shell.",
            file=sys.stderr,
        )
        return 2

    btc_bal = next((b for b in acct.get("balances", []) if b.get("asset") == "BTC"), {})
    usdt_bal = next((b for b in acct.get("balances", []) if b.get("asset") == "USDT"), {})
    print(
        f"Wallet:   BTC free={btc_bal.get('free', '0')} locked={btc_bal.get('locked', '0')} | "
        f"USDT free={usdt_bal.get('free', '0')} locked={usdt_bal.get('locked', '0')}"
    )
    print()

    try:
        orders = get_open_orders(config.SYMBOL)
    except Exception as exc:
        print(f"ERROR /api/v3/openOrders: {exc}", file=sys.stderr)
        return 3

    if not orders:
        print(f"NO open orders for {config.SYMBOL} on Binance.")
        return 0

    print(f"Open orders ({len(orders)}) for {config.SYMBOL}:")
    for o in orders:
        print(_fmt(o))

    print()
    matches = [
        o for o in orders
        if o.get("side") == "BUY"
        and abs(float(o.get("price", 0) or 0) - TARGET_PRICE) <= PRICE_TOLERANCE
    ]
    if matches:
        print(
            f"MATCH: {len(matches)} BUY order(s) within ±${PRICE_TOLERANCE:.2f} "
            f"of dashboard target ${TARGET_PRICE:,.2f}:"
        )
        for o in matches:
            print(_fmt(o))
            print(json.dumps(o, indent=2))
    else:
        print(
            f"NO BUY order found within ±${PRICE_TOLERANCE:.2f} "
            f"of dashboard target ${TARGET_PRICE:,.2f}. "
            "The dashboard may be showing stale state."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
