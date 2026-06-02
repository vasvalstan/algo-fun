from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from live_visualizer.config import settings
from live_visualizer.runners.pullback_runner import run_pullback_loop, get_state as _pullback_state, get_strategy as _get_strategy, _save as _save_state
from live_visualizer.backtest.runner import run_backtest as _run_backtest

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parents[1] / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the pullback paper strategy runner
    task = asyncio.create_task(
        run_pullback_loop(symbol=settings.symbol, capital=5_000.0),
        name="pullback_v1",
    )
    log.info("pullback_v1 paper strategy started")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Algo Fun Live Visualizer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = UI_DIR / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    p = UI_DIR / "index.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return HTMLResponse("<h1>live-visualizer</h1><p>UI not found at " + str(UI_DIR) + "</p>")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict:
    return {
        "symbol": settings.symbol,
        "interval": settings.interval,
        "refresh_interval_ms": settings.refresh_interval_ms,
        "binance_ws_base": settings.binance_ws_base,
    }


@app.get("/api/snapshot")
async def snapshot() -> dict:
    """Full strategy state — polled by the browser every second."""
    return _pullback_state()


@app.post("/api/force-buy")
async def force_buy() -> dict:
    """Force open a tranche at current price, bypassing all strategy filters."""
    strat = _get_strategy()
    if strat is None:
        return {"status": "error", "message": "Strategy not started yet"}
    result = strat.force_buy()
    _save_state(strat)  # persist immediately
    return {"status": "ok" if result.startswith("ok") else "error", "message": result}


@app.post("/api/backtest")
async def backtest(body: dict) -> dict:
    """Run a backtest over a historical period."""
    import time as _time
    from_ts = int(body.get("from_ts", int(_time.time()) - 30 * 86400))
    to_ts   = int(body.get("to_ts",   int(_time.time())))
    symbol  = str(body.get("symbol",  settings.symbol))
    capital = float(body.get("capital", 5000.0))
    tp_pct  = float(body.get("tp_pct", 0.001))
    atr_sl  = float(body.get("atr_sl_mult", 0.5))
    rsi_thr = float(body.get("rsi_threshold", 45.0))
    tranche = float(body.get("tranche_usdc", 1000.0))

    result = await asyncio.to_thread(
        _run_backtest, symbol, from_ts, to_ts,
        capital, tp_pct, atr_sl, rsi_thr, tranche,
    )
    return result


@app.get("/api/ledger")
async def ledger() -> dict:
    """Trade ledger + recent log lines."""
    state = _pullback_state()
    return {
        "ledger": state.get("ledger", []),
        "log": state.get("log", []),
    }
