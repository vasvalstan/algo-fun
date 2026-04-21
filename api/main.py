"""
FastAPI application — serves the WebSocket and REST endpoints for the
ALGO-FUN trading dashboard.

Start with:
    uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from typing import Optional

import requests as _requests_lib

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api import notifications
from api.ws_manager import WSManager, sender_loop, receiver_loop
from api.bot_runner import run_bot
from api.live_grid_runner import run_grid_bot
from api.paper_runner import run_paper_bot
from api.paper_runner_v2 import (
    SANDBOX_LOCK,
    SANDBOX_STATE_BY_ID,
    run_paper_bot_v2,
    STRATEGY_META,
)
from api.binance_demo_runner import run_binance_demo
from api.revolut_runner import run_revolut_live
from api.runners.lifo_launcher import spawn_all as spawn_lifo_runners
from api.runners.lifo_runner import mark_app_shutting_down as _mark_lifo_shutdown
from api import strategy_runtime
from api.strategy_params import STRATEGY_LAYER_OPTIONS, params_model_to_dict
from api.strategy_chat import propose_param_patch
from api.trade_manager import trade_manager, TradeStatus
from api.telegram_bot import run_telegram_bot
from api import audit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    force=True,
)
from api import log_buffer
log_buffer.install()
log = logging.getLogger(__name__)


def _task_done_callback(task: asyncio.Task) -> None:
    """Log unhandled exceptions from background tasks so they aren't swallowed."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("Background task %s crashed: %s", task.get_name(), exc, exc_info=exc)

# ── App setup ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_task, _paper_task, _paper_v2_task, _binance_demo_task, _revolut_live_task
    _lifo_tasks: list[tuple[str, asyncio.Task]] = []

    outbound_ip = _detect_outbound_ip()
    log.info("Server outbound IP: %s  — whitelist this in Binance & Revolut", outbound_ip)

    import config

    # ── LIFO unified runners (Binance Live/Paper + Revolut Live/Paper) ──
    # These replace the legacy grid, binance_demo, and revolut_live runners.
    # Opt out by setting LIFO_ENABLED=false; the legacy runners below will
    # then run instead (kept for backward-compat emergency fallback).
    if config.LIFO_ENABLED:
        _lifo_tasks = spawn_lifo_runners(ws_manager)
        for name, task in _lifo_tasks:
            task.add_done_callback(_task_done_callback)
    else:
        if config.LIVE_STRATEGY == "grid":
            log.info("Starting LEGACY GRID bot runner (live_grid_runner)")
            _bot_task = asyncio.create_task(run_grid_bot(ws_manager), name="grid_runner")
        else:
            log.info("Starting TREND-AWARE bot runner (bot_runner)")
            _bot_task = asyncio.create_task(run_bot(ws_manager), name="bot_runner")
        _bot_task.add_done_callback(_task_done_callback)

        log.info("Starting LEGACY Binance Demo runner (testnet)")
        _binance_demo_task = asyncio.create_task(run_binance_demo(ws_manager), name="binance_demo")
        _binance_demo_task.add_done_callback(_task_done_callback)

        log.info("Starting LEGACY Revolut Live runner")
        _revolut_live_task = asyncio.create_task(run_revolut_live(ws_manager), name="revolut_live")
        _revolut_live_task.add_done_callback(_task_done_callback)

    # Paper-v1 and Paper-v2 multi-strategy runners are independent of LIFO and
    # always run so the existing Paper dashboards keep working.
    log.info("Starting paper runner as background task")
    _paper_task = asyncio.create_task(run_paper_bot(ws_manager), name="paper_runner")
    _paper_task.add_done_callback(_task_done_callback)

    log.info("Starting V2 paper runner as background task")
    _paper_v2_task = asyncio.create_task(run_paper_bot_v2(ws_manager), name="paper_runner_v2")
    _paper_v2_task.add_done_callback(_task_done_callback)

    log.info("Starting trade manager expiry loop")
    trade_manager.start_expiry_loop()

    log.info("Starting Telegram bot")
    _telegram_task = asyncio.create_task(run_telegram_bot(), name="telegram_bot")
    _telegram_task.add_done_callback(_task_done_callback)

    yield

    trade_manager.stop()
    if _telegram_task and not _telegram_task.done():
        _telegram_task.cancel()
        try:
            await _telegram_task
        except asyncio.CancelledError:
            pass

    log.info("Cancelling automated runners")
    # Tell LIFO runners that the whole process is shutting down (Railway
    # SIGTERM, scale-to-zero, Ctrl+C). They use this to suppress the
    # 🛑 Telegram goodbye since a replacement container has already sent
    # 🧱 LIFO Grid started.
    _mark_lifo_shutdown()
    named_tasks: list[tuple[str, Optional[asyncio.Task]]] = [
        ("bot", _bot_task),
        ("paper", _paper_task),
        ("paper_v2", _paper_v2_task),
        ("binance_demo", _binance_demo_task),
        ("revolut_live", _revolut_live_task),
    ]
    named_tasks.extend(_lifo_tasks)
    for task_name, task in named_tasks:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    log.info("All runner tasks cancelled successfully")

app = FastAPI(
    title="ALGO-FUN API",
    description="Real-time trading bot dashboard API",
    version="2.0.0",
    lifespan=lifespan,
)


def _normalized_cors_origins() -> list[str]:
    """Strip whitespace and trailing slashes so env typos don't break CORS."""
    raw = [
        os.getenv("FRONTEND_URL", "http://localhost:5173"),
        "http://localhost:3000",
        *[o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()],
    ]
    seen: set[str] = set()
    out: list[str] = []
    for o in raw:
        o = o.strip().rstrip("/")
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


def _cors_origin_regex() -> Optional[str]:
    """
    Optional regex for extra origins (e.g. Railway public URLs).
    Set CORS_ORIGIN_REGEX explicitly, or rely on auto pattern when
    RAILWAY_ENVIRONMENT is set. Set CORS_NO_RAILWAY_REGEX=1 to disable auto.
    """
    explicit = os.getenv("CORS_ORIGIN_REGEX", "").strip()
    if explicit:
        return explicit
    if os.getenv("CORS_NO_RAILWAY_REGEX"):
        return None
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return r"^https://[a-zA-Z0-9][a-zA-Z0-9\-]*\.up\.railway\.app$"
    return None


_cors_kwargs: dict = {
    "allow_origins": _normalized_cors_origins(),
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}
_rx = _cors_origin_regex()
if _rx:
    _cors_kwargs["allow_origin_regex"] = _rx

app.add_middleware(CORSMiddleware, **_cors_kwargs)

# Shared WebSocket manager
ws_manager = WSManager()

# Background task handles
_bot_task: Optional[asyncio.Task] = None
_paper_task: Optional[asyncio.Task] = None
_paper_v2_task: Optional[asyncio.Task] = None
_binance_demo_task: Optional[asyncio.Task] = None
_revolut_live_task: Optional[asyncio.Task] = None


# ── WebSocket endpoints ──────────────────────────────────────────────


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Live trading dashboard WebSocket."""
    conn = await ws_manager.connect(websocket, channel="live")
    sender = asyncio.create_task(sender_loop(conn, "live", ws_manager))
    receiver = asyncio.create_task(receiver_loop(conn, "live", ws_manager))

    try:
        _, pending = await asyncio.wait(
            [sender, receiver], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except Exception:
        pass


@app.websocket("/ws/paper")
async def ws_paper(websocket: WebSocket) -> None:
    """Paper trading dashboard WebSocket (V1)."""
    conn = await ws_manager.connect(websocket, channel="paper")
    sender = asyncio.create_task(sender_loop(conn, "paper", ws_manager))
    receiver = asyncio.create_task(receiver_loop(conn, "paper", ws_manager))

    try:
        _, pending = await asyncio.wait(
            [sender, receiver], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except Exception:
        pass


@app.websocket("/ws/paper-v2")
async def ws_paper_v2(websocket: WebSocket) -> None:
    """V2 multi-strategy paper trading dashboard WebSocket."""
    conn = await ws_manager.connect(websocket, channel="paper_v2")
    sender = asyncio.create_task(sender_loop(conn, "paper_v2", ws_manager))
    receiver = asyncio.create_task(receiver_loop(conn, "paper_v2", ws_manager))

    try:
        _, pending = await asyncio.wait(
            [sender, receiver], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except Exception:
        pass


async def _ws_channel(websocket: WebSocket, channel: str) -> None:
    conn = await ws_manager.connect(websocket, channel=channel)
    sender = asyncio.create_task(sender_loop(conn, channel, ws_manager))
    receiver = asyncio.create_task(receiver_loop(conn, channel, ws_manager))
    try:
        _, pending = await asyncio.wait(
            [sender, receiver], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except Exception:
        pass


@app.websocket("/ws/binance-live")
async def ws_binance_live(websocket: WebSocket) -> None:
    """Alias for /ws/live — Binance mainnet feed."""
    await _ws_channel(websocket, "live")


@app.websocket("/ws/binance-demo")
async def ws_binance_demo(websocket: WebSocket) -> None:
    """Binance testnet sandbox grid feed."""
    await _ws_channel(websocket, "binance_demo")


@app.websocket("/ws/revolut-live")
async def ws_revolut_live(websocket: WebSocket) -> None:
    """Revolut X production LIFO grid feed."""
    await _ws_channel(websocket, "revolut_live")


@app.websocket("/ws/revolut-paper")
async def ws_revolut_paper(websocket: WebSocket) -> None:
    """Revolut X paper LIFO grid — in-memory simulation with Revolut fee/precision."""
    await _ws_channel(websocket, "revolut_paper")


# ── REST endpoints ───────────────────────────────────────────────────


class TestTelegramBody(BaseModel):
    """Optional secret when TELEGRAM_TEST_SECRET is set on the server."""

    secret: Optional[str] = Field(default=None)


@app.post("/api/test-telegram")
async def test_telegram(body: TestTelegramBody = TestTelegramBody()) -> dict:
    """Send a one-off Telegram message to verify token + chat_id.

    If env TELEGRAM_TEST_SECRET is set, JSON body must include the same value
    in ``secret`` (stops strangers from spamming your chat via the public API).
    """
    required = (os.getenv("TELEGRAM_TEST_SECRET") or "").strip()
    if required and (body.secret or "").strip() != required:
        raise HTTPException(status_code=403, detail="Invalid or missing test secret")

    if not notifications.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the backend)",
        )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        f"🧪 <b>ALGO-FUN Telegram test</b>\n\n"
        f"If you see this, the API can reach Telegram.\n"
        f"<code>{ts}</code>"
    )
    ok = notifications.send(text)
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Telegram send failed (check token, chat id, rate limits, and server logs)",
        )
    return {"ok": True, "detail": "Message sent"}


def _detect_outbound_ip() -> str:
    """Detect this server's outbound IP via a public echo service."""
    try:
        resp = _requests_lib.get("https://api.ipify.org?format=json", timeout=5)
        return resp.json().get("ip", "unknown")
    except Exception:
        return "unknown"


@app.get("/api/health")
async def health() -> dict:
    """Health check endpoint for Railway / monitoring."""
    return {
        "status": "ok",
        "outbound_ip": _detect_outbound_ip(),
        "bot_running": _bot_task is not None and not _bot_task.done(),
        "paper_running": _paper_task is not None and not _paper_task.done(),
        "paper_v2_running": _paper_v2_task is not None and not _paper_v2_task.done(),
        "binance_demo_running": _binance_demo_task is not None and not _binance_demo_task.done(),
        "revolut_live_running": _revolut_live_task is not None and not _revolut_live_task.done(),
        "ws_clients": {
            "live": ws_manager.client_count("live"),
            "paper": ws_manager.client_count("paper"),
            "paper_v2": ws_manager.client_count("paper_v2"),
            "binance_demo": ws_manager.client_count("binance_demo"),
            "revolut_live": ws_manager.client_count("revolut_live"),
        },
        "timestamp": time.time(),
    }


@app.get("/api/strategies")
async def list_strategies() -> dict:
    """List available V2 strategies with metadata."""
    import config
    strategies = []
    for sid, meta in STRATEGY_META.items():
        strategies.append({
            "id": sid,
            "name": meta["name"],
            "short": meta["short"],
            "description": meta["description"],
            "color": meta["color"],
            "icon": meta["icon"],
            "enabled": sid in config.V2_STRATEGIES,
            "layer_options": STRATEGY_LAYER_OPTIONS.get(sid, []),
        })
    return {
        "strategies": strategies,
        "pairs": config.V2_PAIRS,
        "capital": config.V2_PAPER_CAPITAL,
        "capital_per_strategy": round(config.V2_PAPER_CAPITAL / max(len(config.V2_STRATEGIES), 1), 2),
    }


def _check_strategy_config_secret(secret: Optional[str]) -> None:
    import config as app_config

    required = (app_config.STRATEGY_CHAT_SECRET or app_config.TRADE_API_SECRET or "").strip()
    if not required:
        return
    got = (secret or "").strip()
    if not got:
        raise HTTPException(
            status_code=403,
            detail=(
                "Missing API secret: the server has STRATEGY_CHAT_SECRET or TRADE_API_SECRET set. "
                'Paste that exact value into the "API secret" field on the strategy chat panel.'
            ),
        )
    if got != required:
        raise HTTPException(
            status_code=403,
            detail="Invalid API secret — does not match STRATEGY_CHAT_SECRET or TRADE_API_SECRET on the server.",
        )


@app.get("/api/strategy-config")
async def get_strategy_config() -> dict:
    """Effective V2 strategy parameters (paper bot)."""
    data = await strategy_runtime.get_all_effective_params()
    meta = data.pop("_meta", {})
    return {"strategies": data, "meta": meta}


class StrategyConfigPutBody(BaseModel):
    """Full or partial strategy params (merged with defaults)."""

    secret: Optional[str] = Field(default=None)
    params: dict = Field(default_factory=dict)


@app.put("/api/strategy-config/{strategy_id}")
async def put_strategy_config(strategy_id: str, body: StrategyConfigPutBody) -> dict:
    _check_strategy_config_secret(body.secret)
    if strategy_id not in STRATEGY_META:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    model = await strategy_runtime.merge_and_validate(strategy_id, body.params)
    audit.strategy_config_updated(
        strategy_id, source="put", summary="", patch_keys=list(body.params.keys())
    )
    return {
        "ok": True,
        "strategy_id": strategy_id,
        "effective": params_model_to_dict(model),
    }


class StrategyChatBody(BaseModel):
    strategy_id: str
    message: str = Field(default="")
    selected_layers: list[str] = Field(default_factory=list)
    secret: Optional[str] = Field(default=None)


@app.post("/api/strategy-config/chat")
async def strategy_config_chat(body: StrategyChatBody) -> dict:
    _check_strategy_config_secret(body.secret)
    if body.strategy_id not in STRATEGY_META:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {body.strategy_id}")
    if not (body.message or "").strip():
        raise HTTPException(status_code=400, detail="message is required")

    try:
        current = await strategy_runtime.get_effective_params_dict(body.strategy_id)
        summary, patch = propose_param_patch(
            body.strategy_id,
            body.message,
            body.selected_layers,
            current,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("strategy-config/chat LLM error (strategy_id=%s)", body.strategy_id)
        raise HTTPException(
            status_code=502,
            detail=(str(exc) or "LLM request failed")[:800],
        ) from exc

    if not patch:
        return {
            "ok": True,
            "summary": summary,
            "applied": False,
            "param_patch": {},
            "effective": current,
            "detail": "No parameter changes proposed.",
        }

    try:
        model = await strategy_runtime.merge_and_validate(body.strategy_id, patch)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid parameter patch: {exc}",
        ) from exc

    audit.strategy_config_updated(
        body.strategy_id,
        source="chat",
        summary=summary,
        patch_keys=list(patch.keys()),
    )
    return {
        "ok": True,
        "summary": summary,
        "applied": True,
        "param_patch": patch,
        "effective": params_model_to_dict(model),
    }


class StrategyResetBody(BaseModel):
    secret: Optional[str] = Field(default=None)


@app.post("/api/strategy-config/{strategy_id}/reset")
async def reset_strategy_config(strategy_id: str, body: StrategyResetBody = StrategyResetBody()) -> dict:
    _check_strategy_config_secret(body.secret)
    if strategy_id not in STRATEGY_META:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")
    await strategy_runtime.reset_strategy_params(strategy_id)
    model = await strategy_runtime.get_effective_params(strategy_id)
    audit.strategy_config_updated(strategy_id, source="reset", summary="defaults restored", patch_keys=[])
    return {"ok": True, "strategy_id": strategy_id, "effective": params_model_to_dict(model)}


def _check_trade_api_secret(secret: Optional[str]) -> None:
    """If either secret env is set, the caller must match one of them (same rule family as strategy chat)."""
    import config as app_config

    allowed = {
        s
        for s in (
            (app_config.TRADE_API_SECRET or "").strip(),
            (app_config.STRATEGY_CHAT_SECRET or "").strip(),
        )
        if s
    }
    if not allowed:
        return
    got = (secret or "").strip()
    if got not in allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "Invalid or missing API secret — use the exact TRADE_API_SECRET or STRATEGY_CHAT_SECRET "
                "from your Railway (or other) environment."
            ),
        )


class TradeRequestBody(BaseModel):
    strategy: str = Field(description="Strategy id, e.g. v2_adaptive")
    pair: str = Field(default="BTCUSDT")
    side: str = Field(default="BUY", description="BUY or SELL")
    size_usdt: Optional[float] = Field(default=None, description="Trade size in USDT (defaults to config)")
    secret: Optional[str] = Field(default=None)
    source: str = Field(default="manual")


@app.post("/api/trades/request")
async def request_trade(body: TradeRequestBody) -> dict:
    _check_trade_api_secret(body.secret)

    import market_data
    price = float(market_data.get_price(body.pair)["price"])
    size = body.size_usdt or config.TRADE_SIZE_USDT
    quantity = size / price

    try:
        trade = await trade_manager.create_trade(
            strategy=body.strategy,
            pair=body.pair,
            side=body.side,
            quantity=quantity,
            price=price,
            size_usdt=size,
            source=body.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True, "trade": trade.to_dict()}


class TradeActionBody(BaseModel):
    secret: Optional[str] = Field(default=None)
    reason: Optional[str] = Field(default=None)


@app.post("/api/trades/{trade_id}/approve")
async def approve_trade(trade_id: str, body: TradeActionBody = TradeActionBody()) -> dict:
    _check_trade_api_secret(body.secret)
    try:
        trade = await trade_manager.approve(trade_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "trade": trade.to_dict()}


@app.post("/api/trades/{trade_id}/reject")
async def reject_trade(trade_id: str, body: TradeActionBody = TradeActionBody()) -> dict:
    _check_trade_api_secret(body.secret)
    try:
        trade = await trade_manager.reject(trade_id, reason=body.reason or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "trade": trade.to_dict()}


@app.get("/api/trades/pending")
async def list_pending_trades() -> dict:
    pending = trade_manager.get_pending()
    return {"trades": [t.to_dict() for t in pending]}


@app.get("/api/trades/history")
async def trade_history() -> dict:
    history = trade_manager.get_history(limit=50)
    return {"trades": [t.to_dict() for t in history]}


class PaperSandboxManualBuyBody(BaseModel):
    """Trigger a simulated paper buy for the BTC sandbox (same tranche rules as the bot)."""

    secret: Optional[str] = Field(default=None)
    force: bool = Field(
        default=False,
        description="If true, allow buy while PAUSED or outside geofence (still respects reserve and max lots).",
    )


@app.post("/api/paper-v2/sandbox/manual-buy")
async def paper_sandbox_manual_buy(body: PaperSandboxManualBuyBody = PaperSandboxManualBuyBody()) -> dict:
    """Paper-only: fill one manual buy at the current spot (for UI testing)."""
    import config as app_config

    _check_trade_api_secret(body.secret)
    if "bitcoin_sandbox" not in app_config.V2_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail="bitcoin_sandbox is not listed in V2_STRATEGIES.",
        )
    import market_data

    pair = app_config.V2_PAIRS[0] if app_config.V2_PAIRS else "BTCUSDT"
    price = float(market_data.get_price(symbol=pair)["price"])
    async with SANDBOX_LOCK:
        st = SANDBOX_STATE_BY_ID.get("bitcoin_sandbox")
        if not st:
            raise HTTPException(
                status_code=503,
                detail="Sandbox not initialized yet — wait for the paper runner's first tick.",
            )
        ok, msg, ev = st.manual_buy_at_price(price, force=body.force)
    log.info("paper sandbox manual_buy ok=%s price=%.2f force=%s detail=%s", ok, price, body.force, msg)
    return {
        "ok": ok,
        "detail": msg,
        "events": ev,
        "price": price,
        "force": body.force,
    }


@app.get("/api/paper-v2/sandbox/orders")
async def paper_sandbox_orders() -> dict:
    """Pending / active / closed simulated orders for the paper BTC sandbox."""
    import config as app_config

    if "bitcoin_sandbox" not in app_config.V2_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail="bitcoin_sandbox is not listed in V2_STRATEGIES.",
        )
    import market_data

    pair = app_config.V2_PAIRS[0] if app_config.V2_PAIRS else "BTCUSDT"
    price = float(market_data.get_price(symbol=pair)["price"])
    async with SANDBOX_LOCK:
        st = SANDBOX_STATE_BY_ID.get("bitcoin_sandbox")
        if not st:
            return {
                "ok": False,
                "detail": "Sandbox not initialized yet.",
                "mark_price": round(price, 2),
                "pending_buys": [],
                "active_sells": [],
                "closed_trades": [],
            }
        snap = st.orders_snapshot(price)
    return {"ok": True, **snap}


ALLOWED_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}
INTERVAL_LIMITS = {
    "1m": 200, "3m": 200, "5m": 150, "15m": 120, "30m": 100,
    "1h": 100, "2h": 80, "4h": 60, "1d": 60,
}


@app.get("/api/paper-v2/candles")
async def paper_v2_candles(
    interval: str = Query("5m", description="Kline interval: 1m, 5m, 15m, 1h, 4h, etc."),
    limit: Optional[int] = Query(None, description="Max candles (defaults per interval)"),
) -> dict:
    """OHLCV candles for the trading chart, any supported Binance interval."""
    import config as app_config
    import market_data

    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'. Allowed: {sorted(ALLOWED_INTERVALS)}")

    max_limit = INTERVAL_LIMITS.get(interval, 120)
    actual_limit = min(limit or max_limit, max_limit)
    pair = app_config.V2_PAIRS[0] if app_config.V2_PAIRS else "BTCUSDT"

    try:
        raw = market_data.get_klines(symbol=pair, interval=interval, limit=actual_limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Binance kline fetch failed: {exc}")

    candles = []
    for k in raw:
        candles.append({
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })

    return {
        "ok": True,
        "interval": interval,
        "pair": pair,
        "count": len(candles),
        "candles": candles,
    }


@app.get("/api/candles")
async def live_candles(
    interval: str = Query("5m", description="Kline interval"),
    limit: Optional[int] = Query(None, description="Max candles"),
) -> dict:
    """Alias of paper-v2/candles — same Binance mainnet klines."""
    return await paper_v2_candles(interval=interval, limit=limit)


@app.post("/api/trades/{trade_id}/close")
async def close_trade(trade_id: str, body: TradeActionBody = TradeActionBody()) -> dict:
    _check_trade_api_secret(body.secret)
    trade = trade_manager.get_trade(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != TradeStatus.EXECUTED:
        raise HTTPException(status_code=400, detail=f"Trade is {trade.status.value}, cannot close")

    import trading as exchange
    try:
        open_orders = exchange.get_open_orders(trade.pair)
        for o in open_orders:
            exchange.cancel_order(o["orderId"], trade.pair)
    except Exception as exc:
        log.warning("Error cancelling orders for close: %s", exc)

    close_side = "SELL" if trade.side == "BUY" else "BUY"
    import market_data
    price = float(market_data.get_price(trade.pair)["price"])
    try:
        result = exchange.place_limit_order(
            side=close_side,
            quantity=f"{trade.quantity:.6f}",
            price=f"{price:.2f}",
            symbol=trade.pair,
        )
        return {"ok": True, "close_order": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Close failed: {exc}")


@app.get("/api/positions")
async def list_positions() -> dict:
    positions = []
    if _bot_task and not _bot_task.done():
        state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state.json")
        try:
            import json
            with open(state_path, "r") as f:
                state = json.load(f)
            for p in state.get("positions", []):
                if p.get("state") in ("HOLDING", "BUY_PLACED", "SELL_PLACED"):
                    positions.append(p)
        except Exception:
            pass

    pending = trade_manager.get_pending()
    executed = [t for t in trade_manager.get_history() if t.status == TradeStatus.EXECUTED]

    return {
        "bot_positions": positions,
        "pending_trades": [t.to_dict() for t in pending],
        "recent_executed": [t.to_dict() for t in executed[-10:]],
    }


@app.put("/api/strategies/{strategy_id}/toggle")
async def toggle_strategy(strategy_id: str) -> dict:
    if strategy_id not in STRATEGY_META:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {strategy_id}")

    if strategy_id in config.V2_STRATEGIES:
        config.V2_STRATEGIES.remove(strategy_id)
        enabled = False
    else:
        config.V2_STRATEGIES.append(strategy_id)
        enabled = True

    audit.strategy_toggled(strategy_id, enabled)
    return {"ok": True, "strategy_id": strategy_id, "enabled": enabled}


@app.get("/api/market/snapshot")
async def market_snapshot() -> dict:
    import market_data
    price = float(market_data.get_price()["price"])

    pending = trade_manager.get_pending()
    return {
        "symbol": config.SYMBOL,
        "price": price,
        "mainnet": config.USE_MAINNET,
        "approval_required": config.TRADE_APPROVAL_REQUIRED,
        "pending_trades": len(pending),
        "strategies_enabled": config.V2_STRATEGIES,
        "timestamp": time.time(),
    }


@app.get("/api/open-orders")
async def open_orders() -> dict:
    """Fetch open orders and balances from Binance (legacy endpoint)."""
    import trading
    import market_data
    import config

    try:
        orders = trading.get_open_orders()
    except Exception as exc:
        log.warning("/api/open-orders: get_open_orders failed: %s", exc)
        orders = []

    try:
        price = float(market_data.get_price()["price"])
    except Exception as exc:
        log.warning("/api/open-orders: market_data.get_price failed: %s", exc)
        price = 0.0

    base_asset = config.SYMBOL.replace("USDT", "")
    balances = {}
    try:
        acct = trading.get_account()
        for b in acct.get("balances", []):
            if b["asset"] in (base_asset, "USDT"):
                free = float(b["free"])
                locked = float(b["locked"])
                if free > 0 or locked > 0:
                    balances[b["asset"]] = {"free": free, "locked": locked}
    except Exception:
        pass

    return {
        "orders": [
            {
                "orderId": o["orderId"],
                "side": o["side"],
                "price": float(o["price"]),
                "origQty": float(o["origQty"]),
                "executedQty": float(o["executedQty"]),
                "status": o["status"],
                "time": o.get("time", 0),
                "notional": float(o["price"]) * float(o["origQty"]),
            }
            for o in orders
        ],
        "balances": balances,
        "price": price,
    }


# ── Generic per-venue exchange snapshot ─────────────────────────────────
#
# Used by the dashboard's "Exchange Account" card. Returns the same shape
# as /api/open-orders but parameterised by venue label so the frontend can
# show Binance USDT *or* Revolut USDC funds without hardcoded assumptions.

def _venue_factory(venue_label: str):
    """Lazy import to keep module load cheap & avoid Revolut SDK at startup."""
    from api.venues.binance import binance_live_venue, binance_testnet_venue
    from api.venues.revolut import revolut_live_venue

    factories = {
        "binance-live": binance_live_venue,
        "binance-paper": binance_testnet_venue,
        "revolut-live": revolut_live_venue,
    }
    return factories.get(venue_label)


@app.get("/api/exchange/{venue_label}")
async def exchange_snapshot(venue_label: str) -> dict:
    """
    Return open orders, balances, and last spot price for a given venue.

    Response shape (matches the legacy /api/open-orders for backward compat,
    plus a few extra fields):

        {
          "venue": "revolut-live",
          "symbol": "BTC-USDC",
          "base_asset": "BTC",
          "quote_asset": "USDC",
          "price": 75123.45,
          "balances": {"USDC": {"free": 100.0, "locked": 0.0}, ...},
          "orders": [{orderId, side, price, origQty, executedQty, status, time, notional}, ...]
        }
    """
    factory = _venue_factory(venue_label)
    if factory is None:
        raise HTTPException(status_code=404, detail=f"unknown venue '{venue_label}'")

    try:
        venue = factory()
    except Exception as exc:
        log.warning("/api/exchange/%s: factory failed: %s", venue_label, exc)
        raise HTTPException(status_code=503, detail=str(exc))

    spec = venue.spec

    try:
        balances = await asyncio.to_thread(venue.get_detailed_balances)
    except Exception as exc:
        log.warning("/api/exchange/%s: balances failed: %s", venue_label, exc)
        balances = {}

    try:
        orders_raw = await asyncio.to_thread(venue.get_open_orders_detail)
    except Exception as exc:
        log.warning("/api/exchange/%s: orders failed: %s", venue_label, exc)
        orders_raw = []

    try:
        price = float(await asyncio.to_thread(venue.get_price))
    except Exception as exc:
        log.warning("/api/exchange/%s: price failed: %s", venue_label, exc)
        price = 0.0

    # Normalise to the frontend's expected order shape (matches /api/open-orders).
    orders = [
        {
            "orderId": o.get("order_id", ""),
            "side": str(o.get("side", "")).upper(),
            "price": float(o.get("price", 0.0) or 0.0),
            "origQty": float(o.get("qty", 0.0) or 0.0),
            "executedQty": 0.0,
            "status": "OPEN",
            "time": int(o.get("time", 0) or 0),
            "notional": float(o.get("price", 0.0) or 0.0) * float(o.get("qty", 0.0) or 0.0),
        }
        for o in (orders_raw or [])
    ]

    return {
        "venue": spec.name,
        "platform": spec.platform,
        "symbol": spec.symbol,
        "base_asset": spec.base_asset,
        "quote_asset": spec.quote_asset,
        "price": price,
        "balances": balances,
        "orders": orders,
    }


# ── Manual market-buy ("Buy Now" button) ────────────────────────────
#
# Force-opens a fresh LIFO bag on demand. The runner places a MARKET
# BUY for ~`amount_usdt`, then threads the fill through the engine
# exactly like an organic limit-buy fill would: a new bag is created,
# its TP sell is placed, the next grid buy intent is emitted (if room).
# All the usual safeguards apply (max-ammo, min-notional, retry backoff).


class ForceBuyBody(BaseModel):
    amount_usdt: Optional[float] = Field(
        default=None,
        description="Quote amount to spend (USDT/USDC). Defaults to the venue's bullet size.",
    )
    secret: Optional[str] = Field(default=None)


@app.post("/api/exchange/{venue_label}/force-buy")
async def exchange_force_buy(venue_label: str, body: ForceBuyBody) -> dict:
    """
    Trigger a market buy on a live LIFO runner.

    Body:
        { "amount_usdt": 10.0, "secret": "<TRADE_API_SECRET>" }

    Auth: requires TRADE_API_SECRET (or STRATEGY_CHAT_SECRET) when those
    env vars are set on the server. If neither is set the endpoint is
    open — same convention as the rest of /api/trades/*.

    Returns the runner's status dict on success, e.g.:
        {
          "ok": true, "venue": "binance-live",
          "order_id": "12345", "bag_id": 7,
          "fill_price": 75123.45, "filled_qty": 0.00013,
          "notional_usdt": 9.97, "sell_target_price": 76250.30,
          "open_bags": 2, "max_bullets": 6
        }

    On failure returns HTTP 4xx with `{ ok: false, reason, message }`
    so the dashboard can render the cause inline.
    """
    _check_trade_api_secret(body.secret)

    from api.runners.lifo_runner import get_runner, list_runner_labels
    runner = get_runner(venue_label)
    if runner is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No live runner registered for '{venue_label}'. "
                f"Active labels: {sorted(list_runner_labels())}"
            ),
        )
    try:
        result = await runner.force_market_buy(body.amount_usdt)
    except Exception as exc:
        log.exception("/api/exchange/%s/force-buy crashed", venue_label)
        raise HTTPException(status_code=500, detail=str(exc))

    if not result.get("ok"):
        # 409 = "the request was understood but conflicts with current state"
        # (max-ammo, in backoff, paper venue, …). Lets the frontend tell
        # users *why* the click was refused without it looking like an
        # internal error.
        raise HTTPException(status_code=409, detail=result)
    return result


@app.get("/api/status")
async def status() -> dict:
    """One-shot JSON snapshot of bot state (for non-WebSocket clients)."""
    import json

    state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state.json")
    try:
        with open(state_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"status": "offline", "message": "Bot has not started yet"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
