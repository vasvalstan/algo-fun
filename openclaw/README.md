# OpenClaw Trading Agent — ALGO-FUN

AI agent powered by **Gemma 4 31B** that controls the ALGO-FUN trading system via
natural language over Telegram. All trades require explicit approval.

## Architecture

```
You (Telegram) → OpenClaw (Gemma 4) → MCP Skill → FastAPI Backend → Binance
                                                  ↓
                                          Approve/Reject buttons → You (Telegram)
```

## What You Can Say

- "What's the BTC price?"
- "Execute adaptive strategy on BTCUSDT"
- "Show my open positions"
- "Close position abc123"
- "Disable the breakout strategy"
- "How's the bot performing today?"

## MCP Tools Available

| Tool | Description |
|------|-------------|
| `get_market_status` | Current price, signals, pending trade count |
| `request_trade` | Request a trade (requires Telegram approval) |
| `list_pending_trades` | Trades waiting for approval |
| `list_positions` | Open positions on exchange |
| `close_position` | Close an active position |
| `toggle_strategy` | Enable/disable a strategy |
| `get_performance` | P&L summary and trade history |
| `get_trade_history` | Recent trade request log |
| `get_strategies` | Strategy metadata and status |
| `bot_health` | Bot running status |

## Deploy on Railway (3rd Service)

### Prerequisites

You need:
1. Your backend already running on Railway (with the trade approval endpoints)
2. A **Google AI API key** — get one free at https://aistudio.google.com/apikey
3. A **second Telegram bot token** from @BotFather (separate from the notification bot)

> **Why a second bot?** The notification bot (`telegram_bot.py`) runs inside FastAPI
> and handles approval buttons. The OpenClaw bot is a separate agent that interprets
> natural language. They can share the same group chat, but need different bot tokens.

### Step 1: Add OpenClaw as a Railway Service

In your Railway project, add a new service:
- **Source**: This repo, workspace path `openclaw/`
- **Dockerfile**: `openclaw/Dockerfile`

### Step 2: Set Environment Variables

| Variable | Value | Required |
|----------|-------|----------|
| `GEMINI_API_KEY` | Your Google AI API key | Yes |
| `TELEGRAM_BOT_TOKEN` | Second bot token from @BotFather | Yes |
| `ALGOFUN_BACKEND_URL` | `https://backend-production-XXXX.up.railway.app` | Yes |
| `TRADE_API_SECRET` | Same value as backend's `TRADE_API_SECRET` | If set on backend |

### Step 3: Deploy

```bash
cd openclaw && railway up --service openclaw
```

### Step 4: Start Chatting

1. Find your OpenClaw bot on Telegram (the one from the second @BotFather token)
2. Send `/start`
3. Try: "What's the BTC price right now?"
4. Try: "Execute a buy on BTCUSDT with the adaptive strategy"
   → You'll get an Approve/Reject prompt from the notification bot

## Local Development

```bash
# Install OpenClaw
npm install -g openclaw@latest

# Install MCP skill dependencies
pip install fastmcp httpx

# Set environment
export GEMINI_API_KEY="your-key"
export TELEGRAM_BOT_TOKEN="your-openclaw-bot-token"
export ALGOFUN_BACKEND_URL="http://localhost:8000"

# Run the onboarding wizard
openclaw onboard --install-daemon

# Or run manually with our config
cp openclaw.json ~/.openclaw/openclaw.json
openclaw gateway start
```

Then DM your OpenClaw bot on Telegram.

## Configuration Reference

The `openclaw.json` configures:

- **Model**: Gemma 4 31B IT via Google AI API (free tier available)
- **Channel**: Telegram with DM allowlist policy
- **MCP Skill**: Our `mcp_server.py` registered as `algo-fun-trading`
- **System Prompt**: Trading assistant rules (always require approval, check market first)

### Switching to a Different Model

Edit `openclaw.json` → `models.default`:

```json
"default": "gemma-4-26b-it"
```

Or use Gemini models directly (same API key works):

```json
"default": "gemini-2.5-pro"
```

### DM Policy

- `"allowlist"` — Only approved users can chat (default, secure)
- `"pairing"` — New users must be approved via a pairing code
- `"open"` — Anyone can DM (not recommended for trading)
