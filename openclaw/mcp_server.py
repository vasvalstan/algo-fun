"""
OpenClaw MCP skill — exposes ALGO-FUN trading system as tools.

Run standalone:
    uvx fastmcp run openclaw/mcp_server.py

Or install into OpenClaw:
    clawhub install ./openclaw
"""

import json
import os
from typing import Optional

import httpx
from fastmcp import FastMCP

BACKEND_URL = os.getenv("ALGOFUN_BACKEND_URL", "http://localhost:8000")
API_SECRET = os.getenv("TRADE_API_SECRET", "")

mcp = FastMCP(
    "algo-fun-trading",
    instructions=(
        "ALGO-FUN trading agent. Use these tools to monitor markets, request trades "
        "(all trades require user approval via Telegram), manage positions, and control strategies. "
        "Always confirm the user's intent before requesting a trade."
    ),
)


def _headers() -> dict:
    return {"Content-Type": "application/json"}


def _body(**kwargs) -> dict:
    payload = {k: v for k, v in kwargs.items() if v is not None}
    if API_SECRET:
        payload["secret"] = API_SECRET
    return payload


async def _get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BACKEND_URL}{path}")
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{BACKEND_URL}{path}", json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _put(path: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(f"{BACKEND_URL}{path}")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_market_status() -> str:
    """Get current BTC price, strategy signals, active strategies, and number of pending trades.

    Use this to understand the current market conditions before making any decisions.
    """
    data = await _get("/api/market/snapshot")
    return json.dumps(data, indent=2)


@mcp.tool()
async def request_trade(
    strategy: str,
    pair: str = "BTCUSDT",
    side: str = "BUY",
    size_usdt: Optional[float] = None,
) -> str:
    """Request a trade that requires user approval via Telegram.

    The trade will NOT execute immediately. Instead, the user receives a Telegram
    message with Approve/Reject buttons. Only after explicit approval does the
    trade go to the exchange.

    Args:
        strategy: Strategy ID (e.g. 'v2_adaptive', 'mean_reversion', 'breakout')
        pair: Trading pair (default BTCUSDT)
        side: BUY or SELL
        size_usdt: Trade size in USDT (defaults to system config)
    """
    body = _body(
        strategy=strategy,
        pair=pair,
        side=side.upper(),
        size_usdt=size_usdt,
        source="openclaw",
    )
    data = await _post("/api/trades/request", body)
    trade = data.get("trade", {})
    return (
        f"Trade requested (ID: {trade.get('trade_id', '?')}). "
        f"Waiting for user approval via Telegram. "
        f"Side: {trade.get('side')}, Size: {trade.get('size_usdt', 0):.0f} USDT, "
        f"Price: ${trade.get('price_at_request', 0):,.2f}"
    )


@mcp.tool()
async def list_pending_trades() -> str:
    """List all trades waiting for user approval.

    Shows trade details including ID, side, pair, price, and time remaining.
    """
    data = await _get("/api/trades/pending")
    trades = data.get("trades", [])
    if not trades:
        return "No pending trades."

    lines = []
    for t in trades:
        lines.append(
            f"- {t['trade_id']}: {t['side']} {t['pair']} "
            f"${t['price_at_request']:,.2f} ({t['size_usdt']:.0f} USDT) "
            f"TTL: {t['ttl_s']}s [{t['strategy']}]"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_positions() -> str:
    """List all open positions (bot-managed and manually created).

    Shows current positions on the exchange plus any pending/executed trades.
    """
    data = await _get("/api/positions")

    lines = []
    bot_pos = data.get("bot_positions", [])
    if bot_pos:
        lines.append("Bot positions:")
        for p in bot_pos:
            lines.append(
                f"  - Slot {p.get('slot_id')}: {p.get('state')} "
                f"entry=${p.get('entry_price', 0):,.2f}"
            )

    pending = data.get("pending_trades", [])
    if pending:
        lines.append(f"Pending trades: {len(pending)}")

    executed = data.get("recent_executed", [])
    if executed:
        lines.append("Recently executed:")
        for t in executed[-5:]:
            lines.append(f"  - {t['side']} {t['pair']} @ ${t['price_at_request']:,.2f}")

    return "\n".join(lines) if lines else "No open positions."


@mcp.tool()
async def close_position(trade_id: str, reason: str = "") -> str:
    """Close an active position by placing a counter-order.

    Args:
        trade_id: The trade ID to close
        reason: Optional reason for closing
    """
    body = _body(reason=reason)
    data = await _post(f"/api/trades/{trade_id}/close", body)
    close_order = data.get("close_order", {})
    return f"Close order placed. Order ID: {close_order.get('orderId', '?')}"


@mcp.tool()
async def toggle_strategy(strategy_id: str) -> str:
    """Enable or disable a trading strategy.

    Args:
        strategy_id: Strategy to toggle (v2_adaptive, mean_reversion, breakout)
    """
    data = await _put(f"/api/strategies/{strategy_id}/toggle")
    status = "enabled" if data.get("enabled") else "disabled"
    return f"Strategy '{strategy_id}' is now {status}."


@mcp.tool()
async def get_performance() -> str:
    """Get P&L summary, trade history, and overall bot performance.

    Reads the latest bot state including cycle history and session stats.
    """
    data = await _get("/api/status")
    if data.get("status") == "offline":
        return "Bot is offline — no performance data available."

    session = data.get("session", {})
    alltime = data.get("alltime", {})
    cycles = data.get("cycles", [])

    lines = [
        "Performance Summary:",
        f"  Session equity: ${session.get('equity_usdt', 0):,.2f}",
        f"  Session fees: ${session.get('fees_paid', 0):.4f}",
        f"  All-time cycles: {alltime.get('total_cycles', 0)}",
        f"  All-time net P&L: ${alltime.get('total_net_pnl', 0):.4f}",
    ]

    if cycles:
        lines.append(f"\nLast {min(5, len(cycles))} cycles:")
        for c in cycles[-5:]:
            lines.append(
                f"  #{c['number']}: buy=${c['buy_price']:,.2f} "
                f"sell=${c['sell_price']:,.2f} "
                f"P&L={c['net_pnl']:+.4f} ({c['gross_pct']:+.2f}%)"
            )

    return "\n".join(lines)


@mcp.tool()
async def get_trade_history(limit: int = 20) -> str:
    """Get recent trade request history (approved, rejected, expired, etc).

    Args:
        limit: Number of recent trades to return (default 20)
    """
    data = await _get("/api/trades/history")
    trades = data.get("trades", [])
    if not trades:
        return "No trade history."

    lines = []
    for t in trades[-limit:]:
        lines.append(
            f"- [{t['status']}] {t['side']} {t['pair']} "
            f"${t['price_at_request']:,.2f} ({t['size_usdt']:.0f} USDT) "
            f"strategy={t['strategy']} source={t['source']}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_strategies() -> str:
    """List all available strategies with metadata and enabled status."""
    data = await _get("/api/strategies")
    strategies = data.get("strategies", [])

    lines = ["Available strategies:"]
    for s in strategies:
        status = "✅ enabled" if s["enabled"] else "❌ disabled"
        lines.append(f"  - {s['id']}: {s['name']} ({status})")
        lines.append(f"    {s['description']}")

    lines.append(f"\nCapital: ${data.get('capital', 0):,.0f}")
    lines.append(f"Per strategy: ${data.get('capital_per_strategy', 0):,.0f}")
    return "\n".join(lines)


@mcp.tool()
async def bot_health() -> str:
    """Check if the trading bot is running and healthy."""
    data = await _get("/api/health")
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mcp.run()
