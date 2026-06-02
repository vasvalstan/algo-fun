"""
Shared, permanent trade history — append-only across ALL strategies.

Every closed trade from any strategy is recorded here so the History tab
always shows the full record, no matter which strategy is active or how
many times the service restarts.

Open positions are NOT stored here (they live in the active strategy's
state file); the API merges them in for display.
"""

from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

_DATA_DIR  = os.getenv("PULLBACK_DATA_DIR", "/data")
_HIST_PATH = os.path.join(_DATA_DIR, "trade_history.json")
_lock = threading.Lock()


def _load() -> list:
    try:
        with open(_HIST_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("trade_history load failed: %s", e)
        return []


def _save(records: list) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _HIST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(records, f)
        os.replace(tmp, _HIST_PATH)
    except Exception as e:
        log.warning("trade_history save failed: %s", e)


def _key(strategy: str, tranche_id: str, exit_time) -> str:
    return f"{strategy}:{tranche_id}:{exit_time}"


def sync(strategy: str, tranches: list) -> int:
    """Append any closed tranche not already recorded. Returns new count."""
    with _lock:
        records = _load()
        seen = {_key(r["strategy"], r["id"], r.get("exit_time")) for r in records}
        added = 0
        for t in tranches:
            if t.state not in ("CLOSED", "STOPPED"):
                continue
            k = _key(strategy, t.id, t.exit_time)
            if k in seen:
                continue
            records.append({
                "strategy":    strategy,
                "id":          t.id,
                "entry_time":  t.entry_time,
                "entry_price": round(t.entry_price, 2),
                "tp_price":    round(t.tp_price, 2),
                "sl_price":    round(t.sl_price, 2),
                "exit_time":   t.exit_time,
                "exit_price":  round(t.exit_price, 2) if t.exit_price else None,
                "qty":         round(t.qty, 8),
                "size_usdc":   round(t.entry_price * t.qty, 2),
                "pnl":         round(t.pnl, 4),
                "result":      t.reason or t.state,
                "duration_s":  int(t.exit_time - t.entry_time) if (t.exit_time and t.entry_time) else None,
            })
            seen.add(k)
            added += 1
        if added:
            _save(records)
        return added


def read(open_rows: list | None = None) -> dict:
    """All recorded closed trades + any current open positions, with summary."""
    with _lock:
        records = _load()

    rows = list(records)
    for r in (open_rows or []):
        rows.append(r)

    rows.sort(key=lambda r: r.get("entry_time", 0), reverse=True)

    closed = [r for r in rows if r.get("result") not in ("OPEN", "PENDING")
              and r.get("exit_time")]
    wins = [r for r in closed if r["pnl"] > 0]
    return {
        "rows":       rows,
        "total":      len(rows),
        "completed":  len(closed),
        "open":       sum(1 for r in rows if r.get("result") in ("OPEN", "PENDING")),
        "wins":       len(wins),
        "losses":     len(closed) - len(wins),
        "total_pnl":  round(sum(r["pnl"] for r in closed), 4),
        "win_rate":   round(len(wins) / len(closed) * 100, 1) if closed else 0,
    }
