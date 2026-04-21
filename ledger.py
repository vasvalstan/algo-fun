"""
Persistent all-time ledger — survives bot restarts.

Stores cumulative profit/loss, total cycles, and total fees in a small
JSON file (ledger.json).  The strategy calls `record_cycle()` after each
completed round-trip; the dashboard reads the totals.
"""

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "ledger.json")


@dataclass
class LedgerTotals:
    total_cycles: int = 0
    total_net_pnl: float = 0.0
    total_fees: float = 0.0
    first_cycle_ts: float = 0.0
    last_cycle_ts: float = 0.0


def load() -> LedgerTotals:
    if not os.path.exists(LEDGER_PATH):
        return LedgerTotals()
    try:
        with open(LEDGER_PATH, "r") as f:
            data = json.load(f)
        return LedgerTotals(**{k: data[k] for k in LedgerTotals.__dataclass_fields__ if k in data})
    except Exception:
        return LedgerTotals()


def _save(totals: LedgerTotals) -> None:
    with open(LEDGER_PATH, "w") as f:
        json.dump(asdict(totals), f, indent=2)


def record_cycle(net_pnl: float, fee: float, totals: Optional[LedgerTotals] = None) -> LedgerTotals:
    """Append one cycle's results to the persistent ledger."""
    if totals is None:
        totals = load()
    now = time.time()
    totals.total_cycles += 1
    totals.total_net_pnl += net_pnl
    totals.total_fees += fee
    if totals.first_cycle_ts == 0.0:
        totals.first_cycle_ts = now
    totals.last_cycle_ts = now
    _save(totals)
    return totals
