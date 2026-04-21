"""
Spawns the four LIFO runners with their correct params / venues.

Called from api/main.py lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple

import config
from api.lifo_grid import LifoGridParams
from api.runners.lifo_runner import run_lifo_runner
from api.venues.binance import binance_live_venue, binance_testnet_venue
from api.venues.revolut import revolut_live_venue, revolut_paper_venue
from api.ws_manager import WSManager

log = logging.getLogger(__name__)


def _binance_params() -> LifoGridParams:
    return LifoGridParams(
        bullet_size_usdt=config.LIFO_BULLET_SIZE_USDT,
        max_bullets=config.LIFO_MAX_BULLETS,
        dip_pct=config.LIFO_DIP_PCT,
        tp_pct=config.LIFO_TP_PCT,
        trail_step_pct=config.LIFO_TRAIL_STEP_PCT,
        price_prec=config.LIFO_PRICE_PREC,
        qty_prec=config.LIFO_QTY_PREC,
        min_notional=config.LIFO_MIN_NOTIONAL,
    )


def _binance_paper_params() -> LifoGridParams:
    return LifoGridParams(
        bullet_size_usdt=config.LIFO_PAPER_BULLET_SIZE_USDT,
        max_bullets=config.LIFO_PAPER_MAX_BULLETS,
        dip_pct=config.LIFO_PAPER_DIP_PCT,
        tp_pct=config.LIFO_PAPER_TP_PCT,
        trail_step_pct=config.LIFO_PAPER_TRAIL_STEP_PCT,
        price_prec=config.LIFO_PRICE_PREC,
        qty_prec=config.LIFO_QTY_PREC,
        min_notional=config.LIFO_MIN_NOTIONAL,
    )


def _revolut_params() -> LifoGridParams:
    return LifoGridParams(
        bullet_size_usdt=config.LIFO_REVOLUT_BULLET_SIZE_USDT,
        max_bullets=config.LIFO_REVOLUT_MAX_BULLETS,
        dip_pct=config.LIFO_REVOLUT_DIP_PCT,
        tp_pct=config.LIFO_REVOLUT_TP_PCT,
        trail_step_pct=config.LIFO_REVOLUT_TRAIL_STEP_PCT,
        price_prec=config.LIFO_PRICE_PREC,
        qty_prec=config.LIFO_REVOLUT_QTY_PREC,
        min_notional=config.LIFO_REVOLUT_MIN_NOTIONAL,
    )


def spawn_all(ws_manager: WSManager) -> List[Tuple[str, asyncio.Task]]:
    """Create and return named asyncio tasks for each enabled runner."""
    tasks: List[Tuple[str, asyncio.Task]] = []

    if not config.LIFO_ENABLED:
        log.info("LIFO runners globally disabled (LIFO_ENABLED=false)")
        return tasks

    if config.LIFO_BINANCE_LIVE_ENABLED:
        log.info("Spawning LIFO runner: binance-live (mainnet)")
        t = asyncio.create_task(
            run_lifo_runner(
                venue=binance_live_venue(),
                params=_binance_params(),
                ws_manager=ws_manager,
                label="binance-live",
                poll_interval=config.LIFO_POLL_BINANCE_LIVE,
            ),
            name="lifo_binance_live",
        )
        tasks.append(("lifo_binance_live", t))

    if config.LIFO_BINANCE_PAPER_ENABLED:
        log.info("Spawning LIFO runner: binance-paper (testnet)")
        t = asyncio.create_task(
            run_lifo_runner(
                venue=binance_testnet_venue(),
                params=_binance_paper_params(),
                ws_manager=ws_manager,
                label="binance-paper",
                poll_interval=config.LIFO_POLL_BINANCE_PAPER,
            ),
            name="lifo_binance_paper",
        )
        tasks.append(("lifo_binance_paper", t))

    if config.LIFO_REVOLUT_LIVE_ENABLED:
        log.info("Spawning LIFO runner: revolut-live (mainnet)")
        t = asyncio.create_task(
            run_lifo_runner(
                venue=revolut_live_venue(fee_rate=config.LIFO_REVOLUT_FEE_RATE),
                params=_revolut_params(),
                ws_manager=ws_manager,
                label="revolut-live",
                poll_interval=config.LIFO_POLL_REVOLUT_LIVE,
            ),
            name="lifo_revolut_live",
        )
        tasks.append(("lifo_revolut_live", t))

    if config.LIFO_REVOLUT_PAPER_ENABLED:
        log.info("Spawning LIFO runner: revolut-paper (in-memory)")
        t = asyncio.create_task(
            run_lifo_runner(
                venue=revolut_paper_venue(
                    starting_usdt=config.LIFO_REVOLUT_PAPER_STARTING_USDT,
                    fee_rate=config.LIFO_REVOLUT_FEE_RATE,
                ),
                params=_revolut_params(),
                ws_manager=ws_manager,
                label="revolut-paper",
                poll_interval=config.LIFO_POLL_REVOLUT_PAPER,
                starting_capital_usdt=config.LIFO_REVOLUT_PAPER_STARTING_USDT,
            ),
            name="lifo_revolut_paper",
        )
        tasks.append(("lifo_revolut_paper", t))

    return tasks
