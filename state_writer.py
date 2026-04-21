"""
Write the bot's live state to state.json every tick,
and optionally push it to a remote Vercel dashboard.

The Next.js dashboard reads this file (local) or the POST endpoint (Vercel).
"""

import json
import logging
import os
import time
from typing import Optional

import requests as _requests

import config
from strategy import MeanReversionStrategy, State

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

def _remote_url():
    return os.getenv("DASHBOARD_URL", "").rstrip("/")

def _push_secret():
    return os.getenv("DASHBOARD_SECRET", "")

log = logging.getLogger(__name__)


def write_state(
    strat: MeanReversionStrategy,
    start_time: float,
    last_action: Optional[str] = None,
) -> None:
    now = time.time()
    uptime = int(now - start_time)

    positions = []
    for p in strat.positions:
        pos_data = {
            "slot_id": p.slot_id,
            "state": p.state.name,
            "entry_price": p.entry_price,
        }
        if p.slot_qty is not None:
            pos_data["slot_qty"] = p.slot_qty
        if p.open_order is not None:
            o = p.open_order
            pos_data["order"] = {
                "side": o.side,
                "price": o.price,
                "quantity": o.quantity,
                "age_s": int(now - o.placed_at),
            }
        positions.append(pos_data)

    cycles = []
    for c in strat.cycles[-20:]:
        cycles.append({
            "number": c.number,
            "slot_id": c.slot_id,
            "buy_price": c.buy_price,
            "sell_price": c.sell_price,
            "gross_pct": round(c.gross_pct, 4),
            "net_pnl": round(c.net_pnl, 6),
            "fee": round(c.fee_estimate, 6),
            "timestamp": c.timestamp,
        })

    prices_list = list(strat.prices)

    strategy_state = {}
    if hasattr(strat, "macro_regime"):
        strategy_state = {
            "macro_regime": strat.macro_regime,
            "daily_bias": strat.daily_bias,
            "market_mode": strat.market_mode,
            "action": strat.action,
            "wallet_base_qty": round(getattr(strat, "wallet_base_qty", 0.0) or 0.0, 8),
            "entry_block": getattr(strat, "last_entry_block_reason", None),
            "sell_block": getattr(strat, "last_sell_block_reason", None),
        }

    state = {
        "timestamp": now,
        "uptime_s": uptime,
        "symbol": config.SYMBOL,
        "mainnet": config.USE_MAINNET,
        "price": strat.current_price,
        "ma": strat.ma,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "trade_size_usdt": config.TRADE_SIZE_USDT,
        "prices": prices_list,
        "positions": positions,
        "cycles": cycles,
        "strategy": strategy_state,
        "session": {
            "starting_balance": round(strat.starting_balance, 4),
            "equity_usdt": round(strat.session_equity_usdt, 4),
            "fees_paid": round(strat.total_fees, 6),
        },
        "alltime": {
            "total_cycles": strat.ledger.total_cycles,
            "total_net_pnl": round(strat.ledger.total_net_pnl, 6),
            "total_fees": round(strat.ledger.total_fees, 6),
            "first_cycle_ts": strat.ledger.first_cycle_ts,
        },
        "errors": strat.errors[-5:],
        "last_action": last_action,
    }

    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)

    url = _remote_url()
    secret = _push_secret()
    if url and secret:
        try:
            _requests.post(
                f"{url}/api/status",
                json=state,
                headers={"Authorization": f"Bearer {secret}"},
                timeout=5,
            )
        except Exception as exc:
            log.debug("remote push failed: %s", exc)
