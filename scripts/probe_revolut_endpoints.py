"""Read-only: enumerate which Revolut X endpoints respond to our key."""

from __future__ import annotations

import sys
from revolut_x import revx_request


CANDIDATES = [
    # Known-good
    "/orders/active", "/balances", "/tickers",
    # Configuration / docs hints
    "/configuration", "/configuration/pairs", "/configuration/symbols",
    "/configuration/fees", "/fees", "/fee-tiers",
    # Account / fees
    "/account", "/account/fees", "/account/balance-history", "/account/info",
    # History flavours
    "/orders/historical",
    "/balance-history", "/balances/history", "/wallet/history",
    # Market data
    "/orderbook", "/depth", "/quotes", "/instruments", "/symbols",
]


def main() -> int:
    print("Revolut X endpoint sweep (read-only):\n")
    for ep in CANDIDATES:
        try:
            data = revx_request("GET", ep)
            shape: str
            if isinstance(data, list):
                shape = f"list[{len(data)}]"
            elif isinstance(data, dict):
                shape = f"dict keys={sorted(data.keys())}"
            else:
                shape = type(data).__name__
            print(f"  ✓ GET {ep:40s} → {shape}")
        except Exception as exc:
            print(f"  ✗ GET {ep:40s} → {str(exc)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
