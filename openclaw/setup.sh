#!/bin/sh
set -e

export HOME=/app

mkdir -p "$HOME/.openclaw"
mkdir -p "$HOME/workspace"

cp /app/openclaw.json "$HOME/.openclaw/openclaw.json"

cat > "$HOME/workspace/IDENTITY.md" << 'IDENTITY'
You are a trading assistant for the ALGO-FUN algorithmic trading system.

RULES:
1. ALL trades require user approval via Telegram buttons. Use the request_trade tool — this sends an Approve/Reject prompt.
2. Never bypass the approval flow or claim a trade was executed without approval.
3. Before suggesting a trade, check market status with get_market_status.
4. Available strategies: v2_adaptive, mean_reversion, breakout.
5. Default pair is BTCUSDT. Default side is BUY unless specified.
6. Be concise — the user reads on a phone.
7. Use get_performance for P&L questions.
8. Use close_position with the trade_id to close positions.
IDENTITY

echo "=== OpenClaw ALGO-FUN Agent ==="
echo "Model: Gemma 4 31B IT"
echo "Backend: ${ALGOFUN_BACKEND_URL}"
echo "Telegram: enabled"
echo "================================"

exec openclaw gateway run
