"""
Read-only probe: discover the REAL maker fee paid on Binance Spot.

Run from repo root (uses .env credentials):
    python -m scripts.probe_binance_fees

What it does
------------
Hits Binance's `/api/v3/myTrades` for the configured SYMBOL, pulls the
last N fills, and reports:

    side   isMaker   commission       commissionAsset   fee%-of-notional

This answers two questions empirically:
  1. Are we *actually* getting maker fills? (`isMaker == True` for every
     LIMIT_MAKER fill)
  2. Is the BNB-discounted fee what we think it is? (commissionAsset
     should be "BNB" if `Use BNB to pay for fees` is on; numeric value
     should equal 0.075% of the trade notional in BNB-equivalent terms.)

Nothing is placed, modified or cancelled. Fully read-only.
"""

from __future__ import annotations

import sys
from typing import Optional

import config
from auth import signed_request


def _my_trades(symbol: str, limit: int = 50) -> list[dict]:
    return signed_request("GET", "/api/v3/myTrades", {"symbol": symbol, "limit": limit})


def _bnb_to_quote(bnb_price_in_quote: float, qty: float) -> float:
    return bnb_price_in_quote * qty


def _bnb_price_in(quote: str) -> Optional[float]:
    """Fetch BNB price denominated in `quote` (e.g. USDT or USDC)."""
    from market_data import get_price
    try:
        sym = f"BNB{quote.upper()}"
        return float(get_price(sym).get("price", 0))
    except Exception:
        return None


def main() -> int:
    symbol = config.SYMBOL
    quote = "USDT" if symbol.endswith("USDT") else symbol[-3:] if not symbol.endswith("USDC") else "USDC"

    print("Binance fee probe — read-only")
    print(f"  endpoint: {config.BASE_URL}")
    print(f"  symbol:   {symbol}  (quote asset: {quote})")
    print(f"  configured Binance fee_rate (informational): 0.00075 (0.0750%)")
    print()

    print("[1/2] Fetching last 50 fills via /api/v3/myTrades…")
    try:
        trades = _my_trades(symbol, limit=50)
    except Exception as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        print(
            "\nIf this is -2015 Invalid API-key, the live key is IP-whitelisted to\n"
            "the Railway pod. Either temporarily allow your home IP on the key,\n"
            "or run this from the Railway shell.",
            file=sys.stderr,
        )
        return 2

    if not trades:
        print(f"  No fills on {symbol} for this account yet.")
        return 0

    print(f"  got {len(trades)} fills.\n")

    bnb_quote_price = _bnb_price_in(quote) or 0.0
    if bnb_quote_price:
        print(f"  BNB/{quote} (for converting BNB-paid fees back to {quote}): {bnb_quote_price:.4f}")
    else:
        print(f"  WARN: could not fetch BNB/{quote} — BNB-denominated fees won't be %-converted.")
    print()

    print("[2/2] Per-fill breakdown:")
    header = f"  {'side':5s} {'isMaker':8s} {'price':>12s} {'qty':>12s} {'commission':>14s} {'asset':6s} {'fee%':>9s}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    fee_fractions: list[float] = []
    maker_count = 0
    for t in trades:
        side = "BUY" if t.get("isBuyer") else "SELL"
        is_maker = bool(t.get("isMaker"))
        if is_maker:
            maker_count += 1
        price = float(t.get("price", 0) or 0)
        qty = float(t.get("qty", 0) or 0)
        notional_quote = price * qty
        commission = float(t.get("commission", 0) or 0)
        comm_asset = str(t.get("commissionAsset", ""))

        # Convert commission → quote currency for a fair % calc.
        if comm_asset == quote:
            comm_quote = commission
        elif comm_asset == "BNB" and bnb_quote_price:
            comm_quote = commission * bnb_quote_price
        elif comm_asset == "BTC" or (symbol.startswith(comm_asset) and price):
            comm_quote = commission * price  # base-asset deduction
        else:
            comm_quote = float("nan")

        if notional_quote > 0 and not (comm_quote != comm_quote):  # not NaN
            frac = comm_quote / notional_quote
            fee_fractions.append(frac)
            frac_str = f"{frac * 100:.5f}%"
        else:
            frac_str = "?"

        print(
            f"  {side:5s} {str(is_maker):8s} {price:>12.2f} {qty:>12.8f} "
            f"{commission:>14.8f} {comm_asset:6s} {frac_str:>9s}"
        )

    print()
    print(f"  Maker fills: {maker_count}/{len(trades)}  "
          f"({maker_count / len(trades) * 100:.1f}%)")
    if fee_fractions:
        avg = sum(fee_fractions) / len(fee_fractions)
        print(f"  Mean fee per leg (quote-denominated): {avg * 100:.5f}%")
        if avg > 0.0008:
            print("  → HIGHER than 0.075% — check whether 'Use BNB for fees' is actually ON.")
        elif avg < 0.0001:
            print("  → Effectively zero (likely BTCUSDC zero-maker promo).")
        else:
            print("  → Matches BNB-discounted maker fee within rounding.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
