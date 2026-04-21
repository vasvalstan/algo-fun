"""In-memory ring buffer that captures Python log records for the live dashboard."""

import contextvars
import logging
from collections import deque
from typing import List, Optional, Set

_BUFFER: deque[dict] = deque(maxlen=400)

# Per-asyncio-task channel tag. Each LifoRunner.run() coroutine sets this
# at startup; because asyncio.Task copies the parent context on creation,
# every log emitted inside that runner — including engine-level logs from
# api.lifo_grid._log() — automatically inherits the tag. Code outside any
# runner context (api.main, telegram_bot, paper_runner, etc.) leaves the
# value as None, which the frontend treats as "global" and shows on every
# channel.
_channel_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "lifo_log_channel", default=None
)


def set_channel(label: Optional[str]) -> None:
    """Tag all subsequent log records emitted in this task with `label`."""
    _channel_var.set(label)


def current_channel() -> Optional[str]:
    return _channel_var.get()

LIVE_MODULES: Set[str] = {
    # Legacy runners (kept so older deployments still surface)
    "api.bot_runner",
    "api.trade_manager",
    "api.main",
    "strategy",
    "indicators",
    "trading",
    "market_data",
    "ledger",
    # LIFO grid (current canonical strategy)
    "api.lifo_grid",
    "api.lifo_state_store",
    "api.runners.lifo_runner",
    "api.runners.lifo_launcher",
    "api.venues.binance",
    "api.venues.revolut",
    # Notification / approval surface — useful context in the live log
    "api.notifications",
    "api.telegram_bot",
}

NOISE_SUBSTRINGS = frozenset({
    "HTTP Request: POST https://api.telegram.org",
    "HTTP Request: GET",
})


class DashboardLogHandler(logging.Handler):
    """Appends formatted log entries to a shared ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if any(n in msg for n in NOISE_SUBSTRINGS):
                return
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "name": record.name.replace("api.", ""),
                "msg": msg,
                "module": record.name,
                "channel": _channel_var.get(),
            }
            _BUFFER.append(entry)
        except Exception:
            pass


def install(level: int = logging.INFO) -> None:
    handler = DashboardLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)


def recent(limit: int = 50, modules: Optional[Set[str]] = None) -> List[dict]:
    """Return recent log entries, optionally filtered to specific modules.

    Channel filtering is left to the frontend: every entry carries a
    `channel` field (the runner label that produced it, or `None` for
    global / non-runner code) and the dashboard slices by current view.

    IMPORTANT: returns sanitized COPIES of buffer entries (without the
    internal `module` key). Earlier versions stripped `module` from the
    original dicts in `_BUFFER`, which broke the module filter on every
    subsequent call — only freshly-added entries still had `module` set,
    so each snapshot ended up with just the 1–2 logs emitted since the
    previous tick.
    """
    items = list(_BUFFER)
    if modules:
        items = [e for e in items if e.get("module", "") in modules]
    sliced = items[-limit:]
    return [
        {k: v for k, v in e.items() if k != "module"}
        for e in sliced
    ]
