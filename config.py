"""
Central configuration.

Reads API credentials from a .env file and selects testnet or mainnet URLs.
Everything else in the project imports from here instead of reading env vars
directly, so there is one place to change if anything moves.
"""

import os
from dotenv import load_dotenv

load_dotenv()

API_KEY: str = os.getenv("BINANCE_API_KEY", "")
API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

USE_MAINNET: bool = os.getenv("USE_MAINNET", "false").lower() == "true"

# Binance spot REST base URLs
BASE_URL: str = (
    "https://api.binance.com"
    if USE_MAINNET
    else "https://testnet.binance.vision"
)

# Spot pair, no slash: BTCUSDC, BTCUSDT, BNBUSDT, ETHUSDT, …
# Default is BTCUSDC because Binance VIP-0 charges 0% maker fee on USDC pairs
# (vs 0.075% on USDT with BNB on). Override via env if you need USDT.
SYMBOL: str = os.getenv("SYMBOL", "BTCUSDC").strip().upper()

# recv_window: Binance rejects a request if it arrives more than this many
# milliseconds after the timestamp you sent.  5 000 ms is conservative.
RECV_WINDOW: int = 5000

# ── Bot / strategy settings ─────────────────────────────────────────

TRADE_QUANTITY: str = os.getenv("TRADE_QUANTITY", "0.001")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "3"))
MA_WINDOW: int = int(os.getenv("MA_WINDOW", "20"))
BUY_DIP_PCT: float = float(os.getenv("BUY_DIP_PCT", "0.15"))
SELL_TARGET_PCT: float = float(os.getenv("SELL_TARGET_PCT", "0.30"))
STALE_ORDER_SEC: int = int(os.getenv("STALE_ORDER_SEC", "120"))
MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "1"))
MAX_BUYS_PER_TICK: int = int(os.getenv("MAX_BUYS_PER_TICK", "20"))

# ── Trend-aware maker strategy settings ─────────────────────────────

TRADE_SIZE_USDT: float = float(os.getenv("TRADE_SIZE_USDT", "8"))
TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "0.5"))
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.3"))
COOLDOWN_SEC: int = int(os.getenv("COOLDOWN_SEC", "120"))
AGGRESSIVE_ENTRY: bool = os.getenv("AGGRESSIVE_ENTRY", "true").lower() in ("true", "1", "yes")

# BTC already in the wallet when the bot starts:
#   analyze_first — bootstrap klines, run full analysis, do not auto-place a sell
#                   on that BTC; still place new maker buys when ENTRY_READY + USDT.
#   manage_immediately — adopt free BTC as the managed position (TP/SL) right away.
WALLET_BTC_POLICY: str = os.getenv("WALLET_BTC_POLICY", "analyze_first").strip().lower()

# ── Live grid strategy (Sandbox Grid on Binance) ─────────────────────

LIVE_STRATEGY: str = os.getenv("LIVE_STRATEGY", "grid")  # "grid" or "trend_aware"
GRID_GEOFENCE_LOW: float = float(os.getenv("GRID_GEOFENCE_LOW", "65000"))
GRID_GEOFENCE_HIGH: float = float(os.getenv("GRID_GEOFENCE_HIGH", "85000"))
GRID_TP_PCT: float = float(os.getenv("GRID_TP_PCT", "0.71"))
GRID_DIP_PCT: float = float(os.getenv("GRID_DIP_PCT", "0.75"))
GRID_NUM_BULLETS: int = int(os.getenv("GRID_NUM_BULLETS", "8"))
GRID_RESERVE_USDT: float = float(os.getenv("GRID_RESERVE_USDT", "0"))
GRID_REPRICE_THRESHOLD: float = float(os.getenv("GRID_REPRICE_THRESHOLD", "0.1"))

# ── Telegram notifications ──────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── FastAPI / dashboard settings ────────────────────────────────────

API_PORT: int = int(os.getenv("API_PORT", "8000"))
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")

# ── V2 multi-strategy settings ──────────────────────────────────────

V2_PAIRS: list = [p.strip().upper() for p in os.getenv("V2_PAIRS", "BTCUSDT,BTCFDUSD").split(",") if p.strip()]
V2_STRATEGIES: list = [s.strip() for s in os.getenv("V2_STRATEGIES", "bitcoin_sandbox").split(",") if s.strip()]
V2_PAPER_CAPITAL: float = float(os.getenv("V2_PAPER_CAPITAL", "5000"))
V2_RISK_PCT: float = float(os.getenv("V2_RISK_PCT", "0.1"))  # Risk 0.1% of equity per trade
V2_ATR_TP_MULT: float = float(os.getenv("V2_ATR_TP_MULT", "2.5"))
V2_ATR_SL_MULT: float = float(os.getenv("V2_ATR_SL_MULT", "1.5"))
V2_TRAIL_TRIGGER_ATR: float = float(os.getenv("V2_TRAIL_TRIGGER_ATR", "1.5"))
V2_TRAIL_DISTANCE_ATR: float = float(os.getenv("V2_TRAIL_DISTANCE_ATR", "1.0"))
V2_MAX_HOLD_MINUTES: int = int(os.getenv("V2_MAX_HOLD_MINUTES", "45"))

# ── Trade approval / agent control ───────────────────────────────────

TRADE_APPROVAL_REQUIRED: bool = os.getenv("TRADE_APPROVAL_REQUIRED", "false").lower() == "true"
TRADE_APPROVAL_TIMEOUT: int = int(os.getenv("TRADE_APPROVAL_TIMEOUT", "300"))
TRADE_API_SECRET: str = os.getenv("TRADE_API_SECRET", "")
# Optional: protect /api/strategy-config/* and chat; falls back to TRADE_API_SECRET in main if unset.
STRATEGY_CHAT_SECRET: str = os.getenv("STRATEGY_CHAT_SECRET", "")
# Strategy chat LLM: set GEMINI_API_KEY or GOOGLE_API_KEY (preferred if set), else OPENAI_API_KEY.
MAX_PENDING_TRADES: int = int(os.getenv("MAX_PENDING_TRADES", "5"))
MAX_TRADE_SIZE_USDT: float = float(os.getenv("MAX_TRADE_SIZE_USDT", "500"))
MAX_DAILY_LOSS_USDT: float = float(os.getenv("MAX_DAILY_LOSS_USDT", "200"))

# ── Binance testnet credentials ──────────────────────────────────────

BINANCE_TESTNET_API_KEY: str = os.getenv("BINANCE_TESTNET_API_KEY", "")
BINANCE_TESTNET_API_SECRET: str = os.getenv("BINANCE_TESTNET_API_SECRET", "")

# ── Revolut Live runner safety gate ──────────────────────────────────

REVOLUT_LIVE_ENABLED: bool = os.getenv("REVOLUT_LIVE_ENABLED", "false").lower() == "true"
REVOLUT_POLL_INTERVAL: int = int(os.getenv("REVOLUT_POLL_INTERVAL", "10"))


# ── LIFO Tranche Grid (new unified strategy) ─────────────────────────
#
# A single grid engine drives four deployments:
#   * binance-live     — real money, mainnet LIMIT_MAKER
#   * binance-paper    — Binance testnet LIMIT_MAKER (same behavior as live)
#   * revolut-live     — real money, Revolut X post-only limit
#   * revolut-paper    — in-memory fill sim with Revolut precision/fees
#
# Binance params come from LIFO_*.
# Revolut overrides come from LIFO_REVOLUT_* (wider to clear fees,
# sparser tick-writes to stay under the 1000 orders/day cap).
#
# Progressive scaling protocol (see README):
#   Phase 1 — widen the net: bump LIFO_MAX_BULLETS by +10 every +$60.
#   Phase 2 — at MAX_BULLETS=40, lock count and grow LIFO_BULLET_SIZE_USDT.

LIFO_ENABLED: bool = os.getenv("LIFO_ENABLED", "true").lower() == "true"

# ── Binance LIFO params ──
LIFO_BULLET_SIZE_USDT: float = float(os.getenv("LIFO_BULLET_SIZE_USDT", "10.0"))
LIFO_MAX_BULLETS: int = int(os.getenv("LIFO_MAX_BULLETS", "6"))
LIFO_DIP_PCT: float = float(os.getenv("LIFO_DIP_PCT", "0.75"))
LIFO_TP_PCT: float = float(os.getenv("LIFO_TP_PCT", "0.71"))
LIFO_TRAIL_STEP_PCT: float = float(os.getenv("LIFO_TRAIL_STEP_PCT", "0.15"))
LIFO_PRICE_PREC: int = int(os.getenv("LIFO_PRICE_PREC", "2"))
LIFO_QTY_PREC: int = int(os.getenv("LIFO_QTY_PREC", "5"))
LIFO_MIN_NOTIONAL: float = float(os.getenv("LIFO_MIN_NOTIONAL", "5.0"))

# ── Paper tweaks (optional; same-shape but allows A/B) ──
# If any LIFO_PAPER_* is unset, the corresponding LIFO_* is used.
LIFO_PAPER_BULLET_SIZE_USDT: float = float(os.getenv("LIFO_PAPER_BULLET_SIZE_USDT", str(LIFO_BULLET_SIZE_USDT)))
LIFO_PAPER_MAX_BULLETS: int = int(os.getenv("LIFO_PAPER_MAX_BULLETS", str(LIFO_MAX_BULLETS)))
LIFO_PAPER_DIP_PCT: float = float(os.getenv("LIFO_PAPER_DIP_PCT", str(LIFO_DIP_PCT)))
LIFO_PAPER_TP_PCT: float = float(os.getenv("LIFO_PAPER_TP_PCT", str(LIFO_TP_PCT)))
LIFO_PAPER_TRAIL_STEP_PCT: float = float(os.getenv("LIFO_PAPER_TRAIL_STEP_PCT", str(LIFO_TRAIL_STEP_PCT)))

# ── Revolut overrides ──
# Revolut X: 0% maker / 0.09% taker (official + empirically verified via
# scripts/probe_revolut_fees.py — see EXECUTION_COSTS.md). The LIFO bot
# is post_only-only, so realised fees are 0% per leg. We keep this knob
# in case Revolut introduces a fee tier change.
LIFO_REVOLUT_FEE_RATE: float = float(os.getenv("LIFO_REVOLUT_FEE_RATE", "0.0"))
LIFO_REVOLUT_BULLET_SIZE_USDT: float = float(os.getenv("LIFO_REVOLUT_BULLET_SIZE_USDT", "10.0"))
LIFO_REVOLUT_MAX_BULLETS: int = int(os.getenv("LIFO_REVOLUT_MAX_BULLETS", "10"))
LIFO_REVOLUT_DIP_PCT: float = float(os.getenv("LIFO_REVOLUT_DIP_PCT", "1.0"))
LIFO_REVOLUT_TP_PCT: float = float(os.getenv("LIFO_REVOLUT_TP_PCT", "1.2"))
LIFO_REVOLUT_TRAIL_STEP_PCT: float = float(os.getenv("LIFO_REVOLUT_TRAIL_STEP_PCT", "0.30"))
LIFO_REVOLUT_QTY_PREC: int = int(os.getenv("LIFO_REVOLUT_QTY_PREC", "8"))
LIFO_REVOLUT_MIN_NOTIONAL: float = float(os.getenv("LIFO_REVOLUT_MIN_NOTIONAL", "1.0"))
LIFO_REVOLUT_PAPER_STARTING_USDT: float = float(os.getenv("LIFO_REVOLUT_PAPER_STARTING_USDT", "1000.0"))

# ── Poll intervals (seconds) ──
LIFO_POLL_BINANCE_LIVE: float = float(os.getenv("LIFO_POLL_BINANCE_LIVE", "3"))
LIFO_POLL_BINANCE_PAPER: float = float(os.getenv("LIFO_POLL_BINANCE_PAPER", "5"))
LIFO_POLL_REVOLUT_LIVE: float = float(os.getenv("LIFO_POLL_REVOLUT_LIVE", "10"))
LIFO_POLL_REVOLUT_PAPER: float = float(os.getenv("LIFO_POLL_REVOLUT_PAPER", "5"))

# ── Persistence ──
LIFO_STATE_DIR: str = os.getenv("LIFO_STATE_DIR", ".")

# ── Individual runner gates (so you can disable one without touching code) ──
LIFO_BINANCE_LIVE_ENABLED: bool = os.getenv("LIFO_BINANCE_LIVE_ENABLED", "true").lower() == "true"
LIFO_BINANCE_PAPER_ENABLED: bool = os.getenv("LIFO_BINANCE_PAPER_ENABLED", "true").lower() == "true"
LIFO_REVOLUT_LIVE_ENABLED: bool = os.getenv("LIFO_REVOLUT_LIVE_ENABLED", "false").lower() == "true"
LIFO_REVOLUT_PAPER_ENABLED: bool = os.getenv("LIFO_REVOLUT_PAPER_ENABLED", "false").lower() == "true"

