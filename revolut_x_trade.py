#!/usr/bin/env python3
"""
Revolut X CLI: verify auth + scopes, then optionally place a tiny market order.

Usage:
  python revolut_x_trade.py balances       # verify signing + whitelisted IP
  python revolut_x_trade.py check-scopes   # classify the key (view / trade / both)
  python revolut_x_trade.py market-buy
  python revolut_x_trade.py market-sell

Requires in .env:
  REVOLUT_X_API_KEY
  REVOLUT_X_PRIVATE_KEY_PATH (default: private.pem next to this file)
  REVOLUT_X_BASE_URL (optional; default production)
  REVOLUT_X_SYMBOL (e.g. BTC-USD)
  REVOLUT_X_MARKET_BASE_SIZE (string decimal, must meet pair minimums)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

from revolut_x import revx_request


def _symbol() -> str:
    return os.getenv("REVOLUT_X_SYMBOL", "BTC-USD").strip()


def _base_size() -> str:
    return os.getenv("REVOLUT_X_MARKET_BASE_SIZE", "0.00001").strip()


def cmd_balances() -> None:
    data = revx_request("GET", "/balances")
    print(json.dumps(data, indent=2))


def cmd_check_scopes() -> None:
    """
    Classify the API key by probing one read (Spot view) and one
    deliberately-unfillable write (Spot trade). We never send a fillable
    order — the price is 50% below market so it would rest harmlessly,
    and we cancel immediately if Revolut accepts it.
    """
    import uuid as _uuid
    sym = _symbol()

    view_ok: bool = False
    trade_ok: bool = False
    errors: list[str] = []

    try:
        revx_request("GET", "/balances")
        view_ok = True
    except Exception as e:
        errors.append(f"Spot view  : {e}")

    try:
        tick = revx_request("GET", "/tickers", params={"symbols": sym})
        rows = tick.get("data", tick) if isinstance(tick, dict) else tick
        last = None
        if isinstance(rows, list) and rows:
            last = float(rows[0].get("last_price", 0) or 0)
        if not last:
            raise RuntimeError("could not read last_price for probe")
        # 50% below market → guaranteed non-crossing; post_only so it won't
        # become a trade anyway.
        probe_price = f"{last * 0.5:.2f}"
        body = {
            "client_order_id": str(_uuid.uuid4()),
            "symbol": sym,
            "side": "buy",
            "order_configuration": {
                "limit": {
                    "base_size": "0.00001",
                    "price": probe_price,
                    "execution_instructions": ["post_only"],
                },
            },
        }
        resp = revx_request("POST", "/orders", json_body=body)
        order = resp.get("data", resp) if isinstance(resp, dict) else resp
        oid = order.get("id") or order.get("venue_order_id") or order.get("order_id")
        trade_ok = True
        if oid:
            try:
                revx_request("DELETE", f"/orders/{oid}")
                print(f"(probe order {oid} placed + cancelled cleanly)")
            except Exception as e:
                print(f"(probe order placed but cancel failed: {e})")
    except Exception as e:
        errors.append(f"Spot trade : {e}")

    print()
    print(f"Spot view  : {'YES' if view_ok else 'NO'}")
    print(f"Spot trade : {'YES' if trade_ok else 'NO'}")
    for err in errors:
        print(f"  ! {err}")
    if view_ok and not trade_ok:
        print()
        print("→ Enable 'Spot trade' on this key at exchange.revolut.com (Profile → API keys).")
    if not view_ok:
        print()
        print("→ 401? Check the IP whitelist on this key, or your signing key pair.")


def cmd_market(side: str) -> None:
    sym = _symbol()
    size = _base_size()
    body = {
        "client_order_id": str(uuid.uuid4()),
        "symbol": sym,
        "side": side,
        "order_configuration": {"market": {"base_size": size}},
    }
    print(f"Placing MARKET {side.upper()} {size} {sym} …")
    data = revx_request("POST", "/orders", json_body=body)
    print(json.dumps(data, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="Revolut X API helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("balances", help="GET /balances (verify signing + key)")
    sub.add_parser("check-scopes", help="classify key as Spot view / Spot trade / both")

    sub.add_parser("market-buy", help="POST market buy (REVOLUT_X_MARKET_BASE_SIZE)")
    sub.add_parser("market-sell", help="POST market sell (base asset)")

    args = p.parse_args()
    if args.cmd == "balances":
        cmd_balances()
    elif args.cmd == "check-scopes":
        cmd_check_scopes()
    elif args.cmd == "market-buy":
        cmd_market("buy")
    elif args.cmd == "market-sell":
        cmd_market("sell")
    else:
        p.error("unknown command")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)
