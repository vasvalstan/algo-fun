"""
Telegram notification sender.

Sends formatted trade alerts via the Telegram Bot API.  No LLM — purely
deterministic message formatting.  The bot_runner calls these functions
when specific events occur (order filled, stop loss, etc.).

Setup:
  1. Talk to @BotFather on Telegram → /newbot → copy the token.
  2. Start a chat with your new bot and send any message.
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
  4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.
"""

import logging
import time
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

# Rate limiting: minimum seconds between messages.
# Telegram allows ~1 message/sec to a chat; we throttle to 2s to be safe
# AND we DON'T silently drop anymore — instead `send()` sleeps the
# remaining gap and then sends. Silent drops were causing the second
# of two near-simultaneous startup notifications (e.g. binance-live +
# revolut-live boot together) to vanish from the user's chat.
_MIN_INTERVAL = 2.0
_last_sent: float = 0.0


def _token() -> str:
    return getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""


def _chat_id() -> str:
    return getattr(config, "TELEGRAM_CHAT_ID", "") or ""


def is_configured() -> bool:
    """True if both token and chat_id are set."""
    return bool(_token()) and bool(_chat_id())


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat.

    Returns True on success, False on failure (silently — never raises).
    """
    global _last_sent

    if not is_configured():
        return False

    # Rate limiting: SLEEP the remaining gap rather than drop the message.
    # Two runners booting back-to-back used to lose the second notification
    # entirely (the rate-limited one returned False and we moved on). Now
    # we honor the throttle but still deliver every message.
    gap = time.time() - _last_sent
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_token()}/sendMessage",
            json={
                "chat_id": _chat_id(),
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        _last_sent = time.time()
        if resp.status_code != 200:
            log.warning("Telegram API error %d: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        log.debug("Telegram send failed: %s", exc)
        return False


# ── Pre-formatted message templates ────────────────────────────────────


def notify_bot_started(symbol: str, mainnet: bool, balance: float) -> bool:
    env = "🔴 MAINNET" if mainnet else "🟢 TESTNET"
    return send(
        f"🤖 <b>Bot started</b>\n"
        f"Pair: <code>{symbol}</code>\n"
        f"Mode: {env}\n"
        f"Balance: <code>{balance:.2f} USDT</code>"
    )


def notify_bot_stopped(uptime_s: int, cycles: int) -> bool:
    h = uptime_s // 3600
    m = (uptime_s % 3600) // 60
    return send(
        f"⏹ <b>Bot stopped</b>\n"
        f"Uptime: {h}h {m}m\n"
        f"Cycles completed: {cycles}"
    )


def notify_buy_placed(price: float, quantity: float, usdt_amount: float) -> bool:
    return send(
        f"📥 <b>BUY order placed</b>\n"
        f"Price: <code>${price:,.2f}</code>\n"
        f"Qty: <code>{quantity:.6f}</code>\n"
        f"Size: <code>{usdt_amount:.0f} USDT</code>"
    )


def notify_buy_filled(price: float, quantity: float) -> bool:
    return send(
        f"✅ <b>BUY filled</b>\n"
        f"Price: <code>${price:,.2f}</code>\n"
        f"Qty: <code>{quantity:.6f}</code>"
    )


def notify_sell_placed(price: float, quantity: float, reason: str = "TP") -> bool:
    return send(
        f"📤 <b>SELL order placed</b> ({reason})\n"
        f"Price: <code>${price:,.2f}</code>\n"
        f"Qty: <code>{quantity:.6f}</code>"
    )


def notify_cycle_complete(
    cycle_num: int,
    buy_price: float,
    sell_price: float,
    net_pnl: float,
    gross_pct: float,
) -> bool:
    emoji = "💰" if net_pnl > 0 else "📉"
    sign = "+" if net_pnl >= 0 else ""
    return send(
        f"{emoji} <b>Cycle #{cycle_num} complete</b>\n"
        f"Buy: <code>${buy_price:,.2f}</code>\n"
        f"Sell: <code>${sell_price:,.2f}</code>\n"
        f"Gross: <code>{gross_pct:+.2f}%</code>\n"
        f"Net P&L: <code>{sign}{net_pnl:.4f} USDT</code>"
    )


def notify_stop_loss(price: float, entry_price: float, loss_pct: float) -> bool:
    return send(
        f"🛑 <b>STOP LOSS triggered</b>\n"
        f"Entry: <code>${entry_price:,.2f}</code>\n"
        f"Exit: <code>${price:,.2f}</code>\n"
        f"Loss: <code>{loss_pct:.2f}%</code>"
    )


def notify_emergency_sell(reason: str, price: float, net_pnl: float) -> bool:
    sign = "+" if net_pnl >= 0 else ""
    return send(
        f"⚠️ <b>Emergency sell: {reason}</b>\n"
        f"Price: <code>${price:,.2f}</code>\n"
        f"Net P&L: <code>{sign}{net_pnl:.4f} USDT</code>"
    )


def notify_error(error_msg: str) -> bool:
    return send(f"❌ <b>Error</b>\n<code>{error_msg[:500]}</code>")


def notify_status_update(
    price: float,
    signal: str,
    reasons: list,
    usdt_free: float,
    trade_size: float,
    auto_trade: bool,
    macro: str = "?",
    mode_1h: str = "?",
    pos_state: str = "WATCHING",
) -> bool:
    auto_label = "ON" if auto_trade else "OFF"
    signal_emoji = {
        "ENTRY_READY": "🟢", "WAIT_FOR_DIP": "🟡",
        "WAIT": "🟠", "NO_TRADE": "🔴",
    }.get(signal, "⚪")
    balance_ok = "✅" if usdt_free >= trade_size else f"⚠️ need ${trade_size:.0f}"
    reason_lines = "\n".join(f"  • {r}" for r in (reasons or [])[:5])
    return send(
        f"📡 <b>Status Update</b>\n\n"
        f"Price: <code>${price:,.2f}</code>\n"
        f"State: <code>{pos_state}</code>\n"
        f"Signal: {signal_emoji} <code>{signal}</code>\n"
        f"Macro: <code>{macro}</code> | 1H: <code>{mode_1h}</code>\n"
        f"Balance: <code>${usdt_free:.2f} USDT</code> {balance_ok}\n"
        f"Auto-trade: <code>{auto_label}</code>\n\n"
        f"<b>Reasons:</b>\n{reason_lines}"
    )


def notify_daily_summary(
    total_cycles: int,
    session_pnl: float,
    alltime_pnl: float,
    current_price: float,
) -> bool:
    s_sign = "+" if session_pnl >= 0 else ""
    a_sign = "+" if alltime_pnl >= 0 else ""
    return send(
        f"📊 <b>Daily Summary</b>\n"
        f"BTC Price: <code>${current_price:,.2f}</code>\n"
        f"Session cycles: {total_cycles}\n"
        f"Session P&L: <code>{s_sign}{session_pnl:.4f} USDT</code>\n"
        f"All-time P&L: <code>{a_sign}{alltime_pnl:.4f} USDT</code>"
    )
