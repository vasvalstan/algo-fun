"""
Reset Revolut LIVE: cancel everything, dump BTC at market, wipe bot state.

Usage (from repo root with .venv activated):

    python -m scripts.reset_revolut_live              # interactive
    python -m scripts.reset_revolut_live --yes        # no prompt
    python -m scripts.reset_revolut_live --dry-run    # show plan only
    python -m scripts.reset_revolut_live --no-deploy  # local cleanup, skip Railway
    python -m scripts.reset_revolut_live --no-sell    # cancel only, keep BTC

What it does (in order):
  1. Snapshots open orders + balances + spot price
  2. Cancels every open order on BTC-USDC
  3. Market-sells all free BTC into USDC (uses the Revolut market
     order endpoint added alongside this script — without it the
     runner's startup orphan-BTC sweep also short-circuited on Revolut)
  4. Sets LIFO_RESET_REVOLUT_LIVE=1 on Railway and redeploys, so the
     next process boot purges the persisted bot state and starts fresh
     in HUNTING mode anchored at the new spot.

After the new deploy is HUNTING, unset the reset flag:
  railway variables --service backend --remove LIFO_RESET_REVOLUT_LIVE
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.venues.revolut import revolut_live_venue  # noqa: E402
from scripts._reset_common import main_for  # noqa: E402


if __name__ == "__main__":
    sys.exit(main_for(
        label="revolut-live",
        venue_factory=revolut_live_venue,
        reset_env_var="LIFO_RESET_REVOLUT_LIVE",
    ))
