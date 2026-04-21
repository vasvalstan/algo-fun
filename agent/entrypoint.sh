#!/usr/bin/env bash
# Hermes agent entrypoint.
#
# - Seeds /data/.hermes/config.yaml from /app/config.yaml on first boot.
# - Always rewrites /data/.hermes/.env from current Railway env vars so
#   secret rotation works without touching the volume manually.
# - Then exec's `hermes gateway` so signals (SIGTERM from Railway) reach
#   Hermes directly.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/data/.hermes}"
mkdir -p "$HERMES_HOME"

# Seed config.yaml only on first boot. After that the operator can edit
# it via `hermes config edit` or `hermes config set` and changes survive
# redeploys.
if [ ! -f "$HERMES_HOME/config.yaml" ]; then
  echo "[entrypoint] Seeding $HERMES_HOME/config.yaml from /app/config.yaml"
  cp /app/config.yaml "$HERMES_HOME/config.yaml"
fi

# Always overwrite .env from env vars (idempotent secret rotation).
#
# Hermes reads platform config from these env vars, not from config.yaml:
#   - OPENROUTER_API_KEY               → enables OpenRouter provider
#   - TELEGRAM_BOT_TOKEN/ALLOWED_USERS → enables Telegram gateway
#   - API_SERVER_ENABLED/HOST/PORT     → enables OpenAI-compatible HTTP API
# (See `hermes config check` for the full list.)
if [ -z "${AGENT_CHAT_TOKEN:-}" ]; then
  echo "[entrypoint] FATAL: AGENT_CHAT_TOKEN must be set." >&2
  echo "[entrypoint] Hermes refuses to bind api_server to 0.0.0.0 without API_SERVER_KEY." >&2
  exit 1
fi

cat > "$HERMES_HOME/.env" <<EOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}

# Telegram gateway (Hermes auto-enables when bot token is present).
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-}

# api_server gateway: OpenAI-compatible HTTP server for the web chat UI.
# Reachable only on Railway's private network; backend is the sole client.
# Hermes refuses to bind to 0.0.0.0 without API_SERVER_KEY (sensible — it
# guards against accidental public exposure). We reuse AGENT_CHAT_TOKEN
# as the upstream key; the backend proxy injects it as a Bearer token.
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642
API_SERVER_KEY=${AGENT_CHAT_TOKEN}

# Used by /app/mcp_server.py (passed through via mcp_servers.*.env in
# config.yaml).
ALGOFUN_BACKEND_URL=${ALGOFUN_BACKEND_URL:-}
TRADE_API_SECRET=${TRADE_API_SECRET:-}
EOF
chmod 600 "$HERMES_HOME/.env"

echo "[entrypoint] HERMES_HOME=$HERMES_HOME"
echo "[entrypoint] config.yaml first lines:"
head -n 5 "$HERMES_HOME/config.yaml" || true
echo "[entrypoint] .env keys: $(grep -oE '^[A-Z_]+' "$HERMES_HOME/.env" | tr '\n' ' ')"

# Hand off to Hermes — `gateway run` runs in the foreground (recommended
# for Docker per `hermes gateway --help`) so SIGTERM from Railway reaches
# Hermes directly. Runs Telegram and api_server platforms concurrently.
exec hermes gateway run
