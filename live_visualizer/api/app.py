from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from live_visualizer.config import settings
from live_visualizer.state.reader import SharedStateReader

log = logging.getLogger(__name__)

state_reader = SharedStateReader(
    symbol=settings.symbol,
    file_path=settings.state_file_path,
    api_url=settings.state_api_url,
)

app = FastAPI(title="Algo Fun Live BTC/USDC Visualizer")

UI_DIR = Path(__file__).resolve().parents[1] / "ui"
app.mount("/static", StaticFiles(directory=str(UI_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (UI_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
async def config() -> dict:
    return {
        "symbol": settings.symbol,
        "interval": settings.interval,
        "refresh_interval_ms": settings.refresh_interval_ms,
        "binance_ws_base": settings.binance_ws_base,
        "state_source": settings.state_api_url or settings.state_file_path,
    }


@app.get("/api/snapshot")
async def snapshot() -> dict:
    return _state_payload()


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_text(json.dumps(_state_payload()))
            await asyncio.sleep(settings.refresh_interval_ms / 1000)
    except WebSocketDisconnect:
        return


def _state_payload() -> dict:
    bot_state = state_reader.read()
    status = state_reader.status()
    return {
        "bot_state": bot_state.model_dump(),
        "state_source": status,
    }
