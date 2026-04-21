"""
Audit logger — append-only log of all trade-related actions.

Writes JSON lines to audit.log for post-mortem analysis.
"""

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "audit.log")


def _write(entry: dict) -> None:
    entry["timestamp"] = time.time()
    entry["iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        log.debug("Audit write failed: %s", exc)


def trade_requested(
    trade_id: str,
    strategy: str,
    pair: str,
    side: str,
    quantity: float,
    price: float,
    size_usdt: float,
    source: str,
) -> None:
    _write({
        "event": "trade_requested",
        "trade_id": trade_id,
        "strategy": strategy,
        "pair": pair,
        "side": side,
        "quantity": quantity,
        "price": price,
        "size_usdt": size_usdt,
        "source": source,
    })


def trade_approved(trade_id: str, order_id: Optional[str] = None) -> None:
    _write({
        "event": "trade_approved",
        "trade_id": trade_id,
        "order_id": order_id,
    })


def trade_rejected(trade_id: str, reason: str = "") -> None:
    _write({
        "event": "trade_rejected",
        "trade_id": trade_id,
        "reason": reason,
    })


def trade_expired(trade_id: str) -> None:
    _write({"event": "trade_expired", "trade_id": trade_id})


def trade_failed(trade_id: str, error: str) -> None:
    _write({"event": "trade_failed", "trade_id": trade_id, "error": error})


def mode_changed(new_mode: str, changed_by: str = "system") -> None:
    _write({"event": "mode_changed", "mode": new_mode, "changed_by": changed_by})


def strategy_toggled(strategy_id: str, enabled: bool) -> None:
    _write({"event": "strategy_toggled", "strategy_id": strategy_id, "enabled": enabled})


def strategy_config_updated(
    strategy_id: str,
    source: str,
    summary: str = "",
    patch_keys: Optional[list] = None,
) -> None:
    _write({
        "event": "strategy_config_updated",
        "strategy_id": strategy_id,
        "source": source,
        "summary": summary,
        "patch_keys": patch_keys or [],
    })
