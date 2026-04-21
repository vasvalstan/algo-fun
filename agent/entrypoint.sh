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
cat > "$HERMES_HOME/.env" <<EOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-}
ALGOFUN_BACKEND_URL=${ALGOFUN_BACKEND_URL:-}
TRADE_API_SECRET=${TRADE_API_SECRET:-}
EOF
chmod 600 "$HERMES_HOME/.env"

echo "[entrypoint] HERMES_HOME=$HERMES_HOME"
echo "[entrypoint] config.yaml first lines:"
head -n 5 "$HERMES_HOME/config.yaml" || true
echo "[entrypoint] .env keys: $(grep -oE '^[A-Z_]+' "$HERMES_HOME/.env" | tr '\n' ' ')"

# Hand off to Hermes — gateway runs both Telegram and api_server platforms.
exec hermes gateway
