"""
Bidirectional Telegram bot with inline keyboard trade approvals and slash commands.

Runs inside the FastAPI process as an async polling loop (no webhook needed).
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import config
from api.trade_manager import trade_manager, PendingTrade, TradeStatus

log = logging.getLogger(__name__)

try:
    from telegram import (
        Bot,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
    )
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
    )

    HAS_PTB = True
except ImportError:
    HAS_PTB = False
    log.warning("python-telegram-bot not installed — Telegram bot disabled")

    class Bot:  # type: ignore[misc, empty-body]
        """Stub when python-telegram-bot is missing."""

    class Update:  # type: ignore[misc, empty-body]
        """Stub when python-telegram-bot is missing."""

    class ContextTypes:  # type: ignore[misc, empty-body]
        DEFAULT_TYPE = Any

    Application = Any  # type: ignore[misc, assignment]


def _chat_id() -> str:
    return config.TELEGRAM_CHAT_ID


def _token() -> str:
    return config.TELEGRAM_BOT_TOKEN


# ── Approval message builder ─────────────────────────────────────────


def build_approval_message(trade: PendingTrade) -> tuple:
    """Returns (text, InlineKeyboardMarkup) for a trade approval request."""
    text = (
        f"🔔 <b>Trade Approval Required</b>\n\n"
        f"<b>{trade.side}</b> {trade.quantity:.6f} {trade.pair}\n"
        f"Price: <code>${trade.price_at_request:,.2f}</code>\n"
        f"Size: <code>{trade.size_usdt:.0f} USDT</code>\n"
        f"Strategy: <code>{trade.strategy}</code>\n"
        f"Source: {trade.source}\n"
        f"Expires in: {trade.ttl_s}s\n\n"
        f"<code>ID: {trade.trade_id}</code>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{trade.trade_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{trade.trade_id}"),
        ]
    ])
    return text, keyboard


def build_resolution_message(trade: PendingTrade) -> str:
    """Build the edited message text after approval/rejection."""
    if trade.status == TradeStatus.EXECUTED:
        order_id = (trade.execution_result or {}).get("orderId", "?")
        return (
            f"✅ <b>APPROVED & EXECUTED</b>\n\n"
            f"<b>{trade.side}</b> {trade.quantity:.6f} {trade.pair}\n"
            f"Price: <code>${trade.price_at_request:,.2f}</code>\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"<code>ID: {trade.trade_id}</code>"
        )
    elif trade.status == TradeStatus.FAILED:
        err = (trade.execution_result or {}).get("error", "unknown")
        return (
            f"⚠️ <b>APPROVED but FAILED</b>\n\n"
            f"<b>{trade.side}</b> {trade.quantity:.6f} {trade.pair}\n"
            f"Error: <code>{err[:200]}</code>\n"
            f"<code>ID: {trade.trade_id}</code>"
        )
    elif trade.status == TradeStatus.REJECTED:
        return (
            f"❌ <b>REJECTED</b>\n\n"
            f"<b>{trade.side}</b> {trade.quantity:.6f} {trade.pair}\n"
            f"Reason: {trade.reject_reason or 'User rejected'}\n"
            f"<code>ID: {trade.trade_id}</code>"
        )
    elif trade.status == TradeStatus.EXPIRED:
        return (
            f"⏰ <b>EXPIRED</b>\n\n"
            f"<b>{trade.side}</b> {trade.quantity:.6f} {trade.pair}\n"
            f"No response within timeout.\n"
            f"<code>ID: {trade.trade_id}</code>"
        )
    return f"Trade {trade.trade_id}: {trade.status.value}"


# ── Callback handler (button presses) ────────────────────────────────


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        return

    action, trade_id = parts

    try:
        if action == "approve":
            trade = await trade_manager.approve(trade_id)
        elif action == "reject":
            trade = await trade_manager.reject(trade_id, reason="Rejected via Telegram")
        else:
            return
    except ValueError as exc:
        await query.edit_message_text(f"⚠️ {exc}", parse_mode="HTML")
        return

    await query.edit_message_text(
        build_resolution_message(trade),
        parse_mode="HTML",
    )


# ── Slash command handlers ────────────────────────────────────────────


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current bot status, positions, and pending trades."""
    import market_data

    try:
        price = float(market_data.get_price()["price"])
    except Exception:
        price = 0.0

    pending = trade_manager.get_pending()
    recent = trade_manager.get_history(limit=5)

    mode_label = "🔴 MAINNET" if config.USE_MAINNET else "🟢 TESTNET"
    approval_label = "ON" if config.TRADE_APPROVAL_REQUIRED else "OFF"
    lines = [
        "📊 <b>ALGO-FUN Status</b>\n",
        f"Price: <code>${price:,.2f}</code>",
        f"Symbol: <code>{config.SYMBOL}</code>",
        f"Mode: {mode_label}",
        f"Approval: {approval_label}",
        "",
        f"<b>Pending trades:</b> {len(pending)}",
    ]
    for t in pending:
        lines.append(f"  • {t.side} {t.quantity:.6f} {t.pair} (TTL {t.ttl_s}s)")

    if recent:
        lines.append("\n<b>Recent trades:</b>")
        for t in recent[-5:]:
            lines.append(f"  • {t.status.value}: {t.side} {t.pair} ({t.strategy})")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List pending trades with approval buttons."""
    pending = trade_manager.get_pending()
    if not pending:
        await update.message.reply_text("No pending trades.")
        return

    for trade in pending:
        text, keyboard = build_approval_message(trade)
        msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        trade.telegram_message_id = msg.message_id


async def _cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show open positions."""
    state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state.json")
    positions = []
    try:
        with open(state_path, "r") as f:
            state = json.load(f)
        positions = [p for p in state.get("positions", []) if p.get("state") != "WATCHING"]
    except Exception:
        pass

    if not positions:
        await update.message.reply_text("No open positions.")
        return

    lines = ["📈 <b>Open Positions</b>\n"]
    for p in positions:
        lines.append(
            f"  • Slot {p.get('slot_id', '?')}: {p.get('state', '?')} "
            f"entry=${p.get('entry_price', 0):,.2f}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _cmd_strategies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List strategies and their status."""
    from api.paper_runner_v2 import STRATEGY_META

    lines = ["🧠 <b>Strategies</b>\n"]
    for sid, meta in STRATEGY_META.items():
        enabled = "✅" if sid in config.V2_STRATEGIES else "❌"
        lines.append(f"  {enabled} <code>{sid}</code> — {meta['name']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle between autonomous and approval mode."""
    args = context.args
    if not args or args[0] not in ("auto", "approval"):
        current = "auto" if trade_manager.auto_approve else "approval"
        await update.message.reply_text(
            f"Current mode: <b>{current}</b>\n"
            f"Usage: /mode auto | /mode approval",
            parse_mode="HTML",
        )
        return

    if args[0] == "approval":
        trade_manager.auto_approve = False
        await update.message.reply_text(
            "🔒 Approval mode <b>ON</b> — trades need your manual approval.",
            parse_mode="HTML",
        )
    else:
        trade_manager.auto_approve = True
        await update.message.reply_text(
            "🔓 Auto mode <b>ON</b> — trades execute automatically. Sleep well!",
            parse_mode="HTML",
        )


async def _cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel all open orders on Binance."""
    import trading

    try:
        orders = trading.get_open_orders()
        if not orders:
            await update.message.reply_text("No open orders to cancel.")
            return

        cancelled = 0
        for o in orders:
            try:
                trading.cancel_order(o["orderId"])
                cancelled += 1
            except Exception:
                pass

        await update.message.reply_text(
            f"🗑 Cancelled <b>{cancelled}/{len(orders)}</b> open orders.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Failed: <code>{str(exc)[:300]}</code>",
            parse_mode="HTML",
        )


async def _cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current open orders on Binance."""
    import trading

    try:
        orders = trading.get_open_orders()
        if not orders:
            await update.message.reply_text("No open orders.")
            return

        lines = [f"📋 <b>Open Orders ({len(orders)})</b>\n"]
        for o in orders:
            price = float(o["price"])
            qty = float(o["origQty"])
            notional = price * qty
            lines.append(
                f"  • {o['side']} {qty:.6f} @ ${price:,.2f} "
                f"(${notional:.2f}) — <code>{o['orderId']}</code>"
            )
        lines.append(f"\n/cancel to cancel all")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Failed: <code>{str(exc)[:300]}</code>",
            parse_mode="HTML",
        )


async def _cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force an immediate market buy trade."""
    import market_data
    import trading

    args = context.args
    usdt_size = float(args[0]) if args else config.TRADE_SIZE_USDT

    try:
        price = float(market_data.get_price()["price"])
        info = market_data.get_exchange_info()
        qty_prec = 5
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = f["stepSize"]
                qty_prec = max(0, len(step.rstrip("0").split(".")[-1]))

        quantity = usdt_size / price
        step = 10.0 ** (-qty_prec)
        quantity = int(quantity / step + 1e-12) * step
        notional = quantity * price

        if notional < 5.0:
            await update.message.reply_text(
                f"⚠️ Order too small: ${notional:.2f} < $5 minimum.\n"
                f"Usage: /force [usdt_amount]  (default: ${config.TRADE_SIZE_USDT:.0f})",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            f"⏳ Forcing BUY: {quantity:.{qty_prec}f} {config.SYMBOL} @ ~${price:,.2f} "
            f"(${usdt_size:.0f} USDT)...",
            parse_mode="HTML",
        )

        resp = trading.place_maker_order(
            side="BUY",
            quantity=f"{quantity:.{qty_prec}f}",
            price=f"{price * 0.9999:.2f}",
        )
        order_id = resp.get("orderId", "?")
        await update.message.reply_text(
            f"✅ <b>BUY order placed!</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Qty: <code>{quantity:.{qty_prec}f}</code>\n"
            f"Price: <code>${price:,.2f}</code>\n"
            f"Size: <code>${usdt_size:.0f} USDT</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Force trade failed:\n<code>{str(exc)[:500]}</code>",
            parse_mode="HTML",
        )


async def _cmd_autotrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle auto-trade on/off."""
    trade_manager.auto_approve = not trade_manager.auto_approve
    if trade_manager.auto_approve:
        await update.message.reply_text(
            "🔓 Auto-trade <b>ON</b> — trades execute automatically without approval.\n"
            "Use /autotrade again to switch back to manual.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "🔒 Auto-trade <b>OFF</b> — trades require your approval.\n"
            "Use /autotrade again to switch back.",
            parse_mode="HTML",
        )


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>ALGO-FUN Bot Commands</b>\n\n"
        "/status — Price, positions, pending trades\n"
        "/autotrade — Toggle auto-trade on/off\n"
        "/force [usdt] — Force a BUY now (default $8)\n"
        "/orders — Show open orders on Binance\n"
        "/cancel — Cancel all open orders\n"
        "/pending — Pending trades with approve/reject\n"
        "/positions — Bot-tracked positions\n"
        "/mode auto|approval — Execution mode\n"
        "/help — This message",
        parse_mode="HTML",
    )


# ── Application lifecycle ─────────────────────────────────────────────


_app: Optional["Application"] = None
_bot: Optional[Bot] = None


async def send_approval_request(trade: PendingTrade) -> None:
    """Send a trade approval request to Telegram with inline buttons."""
    if not _bot or not _chat_id():
        log.warning("Telegram bot not initialized, cannot send approval")
        return

    text, keyboard = build_approval_message(trade)
    try:
        msg = await _bot.send_message(
            chat_id=_chat_id(),
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        trade.telegram_message_id = msg.message_id
    except Exception as exc:
        log.error("Failed to send approval request: %s", exc)


async def send_resolution_update(trade: PendingTrade) -> None:
    """Edit the original approval message to show the resolution."""
    if not _bot or not _chat_id() or not trade.telegram_message_id:
        return

    try:
        await _bot.edit_message_text(
            chat_id=_chat_id(),
            message_id=trade.telegram_message_id,
            text=build_resolution_message(trade),
            parse_mode="HTML",
        )
    except Exception as exc:
        log.debug("Failed to edit approval message: %s", exc)


async def run_telegram_bot() -> None:
    """Start the Telegram bot polling loop. Runs as an asyncio task."""
    global _app, _bot

    if not HAS_PTB:
        log.warning("python-telegram-bot not installed, skipping Telegram bot")
        return

    token = _token()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set, skipping Telegram bot")
        return

    log.info("Starting Telegram bot (polling mode)")

    _app = Application.builder().token(token).build()
    _bot = _app.bot

    _app.add_handler(CommandHandler("start", _cmd_help))
    _app.add_handler(CommandHandler("help", _cmd_help))
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(CommandHandler("pending", _cmd_pending))
    _app.add_handler(CommandHandler("positions", _cmd_positions))
    _app.add_handler(CommandHandler("strategies", _cmd_strategies))
    _app.add_handler(CommandHandler("mode", _cmd_mode))
    _app.add_handler(CommandHandler("force", _cmd_force))
    _app.add_handler(CommandHandler("cancel", _cmd_cancel))
    _app.add_handler(CommandHandler("orders", _cmd_orders))
    _app.add_handler(CommandHandler("autotrade", _cmd_autotrade))
    _app.add_handler(CallbackQueryHandler(_handle_callback))

    trade_manager.set_callbacks(
        on_created=send_approval_request,
    )

    try:
        await _app.initialize()
        await _app.start()
        await _app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot polling started")

        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        log.info("Telegram bot shutting down")
    finally:
        try:
            if _app.updater.running:
                await _app.updater.stop()
            if _app.running:
                await _app.stop()
            await _app.shutdown()
        except Exception as exc:
            log.debug("Telegram bot cleanup: %s", exc)
