#!/usr/bin/env python3
"""
Backtest the BTC $60k–$75k sandbox grid on historical Binance candles.

  cd repo && .venv/bin/python scripts/backtest_bitcoin_sandbox.py --days 14

Uses each bar's low/high for limit fills (same rules as paper runner).
"""

from __future__ import annotations

import argparse
import os
import sys

# Repo root on path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.chdir(_ROOT)

import market_data  # noqa: E402

from api.bitcoin_sandbox import BitcoinSandboxState, sandbox_params_from_pydantic  # noqa: E402
from api.strategy_params import default_bitcoin_sandbox  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest bitcoin_sandbox grid")
    p.add_argument("--symbol", default="BTCUSDT", help="Binance spot symbol")
    p.add_argument("--interval", default="5m", help="Kline interval")
    p.add_argument("--days", type=float, default=14.0, help="Approximate lookback days")
    p.add_argument("--capital", type=float, default=5000.0, help="Starting USDT")
    args = p.parse_args()

    bars_per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6}.get(args.interval, 288)
    max_k = min(int(args.days * bars_per_day), 1000)
    print(f"Fetching up to {max_k} {args.interval} candles for {args.symbol}...")
    raw = market_data.get_klines(symbol=args.symbol, interval=args.interval, limit=max_k)
    if not raw:
        print("No klines returned (check API / symbol).")
        sys.exit(1)

    model = default_bitcoin_sandbox()
    bp = sandbox_params_from_pydantic(model)
    st = BitcoinSandboxState(starting_usdt=args.capital, params=bp, notify=None)

    all_ev: list[str] = []
    for k in raw:
        lo, hi, cl = float(k[3]), float(k[2]), float(k[4])
        st.tick_bar(lo, hi, cl, all_ev)

    eq = st.equity(cl)
    pnl = eq - st.starting_capital
    print("\n=== Sandbox backtest ===")
    print(f"Bars: {len(raw)}  Final close: {cl:,.2f}")
    print(f"Starting USDT: {st.starting_capital:,.2f}  Final equity: {eq:,.2f}  PnL: {pnl:+,.2f}")
    print(f"Closed round-trips: {len(st.closed_trades)}  Open lots: {len(st.holdings)}")
    print(f"Max drawdown (USDT): {st.max_drawdown:,.2f}")
    if st.closed_trades:
        wins = sum(1 for t in st.closed_trades if t.pnl_usdt > 0)
        print(f"Win rate: {100.0 * wins / len(st.closed_trades):.1f}%")
    print("\n--- Last 30 log lines ---")
    for line in all_ev[-30:]:
        print(line)


if __name__ == "__main__":
    main()
