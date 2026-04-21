#!/usr/bin/env bash
# Deploy algo-fun backend (repo root Dockerfile) and frontend (frontend/Dockerfile) to Railway,
# and apply environment variables from scripts/railway.env.
#
# Prerequisites:
#   1. railway CLI installed (https://docs.railway.com/guides/cli)
#   2. railway login   (required — Cursor Railway MCP also needs a valid token / login)
#   3. One Railway project with two services (e.g. "api" + "frontend") attached to this repo.
#
# Linking (once per machine):
#   cd /path/to/algo-fun && railway link -p <project> -s <backend-service-name>
#   cd /path/to/algo-fun/frontend && railway link -p <project> -s <frontend-service-name>
#
# Usage:
#   cp scripts/railway.env.example scripts/railway.env
#   # edit scripts/railway.env
#   ./scripts/railway-deploy-all.sh              # set env + deploy both
#   ./scripts/railway-deploy-all.sh --env-only   # only railway variable set
#   ./scripts/railway-deploy-all.sh --skip-deploy
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${RAILWAY_ENV_FILE:-$REPO_ROOT/scripts/railway.env}"

ENV_ONLY=false
SKIP_DEPLOY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-only) ENV_ONLY=true ;;
    --skip-deploy) SKIP_DEPLOY=true ;;
    -h|--help)
      sed -n '1,35p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

if ! command -v railway >/dev/null 2>&1; then
  echo "Install Railway CLI: https://docs.railway.com/guides/cli"
  exit 1
fi

if ! railway whoami >/dev/null 2>&1; then
  echo "Not logged in. Run:  railway login"
  echo "Then retry this script. Cursor Railway MCP also requires a valid CLI session/token."
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy scripts/railway.env.example and set BACKEND_PUBLIC_URL / FRONTEND_PUBLIC_URL."
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

BACKEND_SERVICE="${BACKEND_SERVICE:-api}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-frontend}"

apply_backend_env() {
  cd "$REPO_ROOT"
  if [[ -n "${FRONTEND_PUBLIC_URL:-}" ]]; then
    railway variable set "FRONTEND_URL=${FRONTEND_PUBLIC_URL}" -s "$BACKEND_SERVICE" --skip-deploys
  fi
  if [[ -n "${CORS_ORIGINS:-}" ]]; then
    railway variable set "CORS_ORIGINS=${CORS_ORIGINS}" -s "$BACKEND_SERVICE" --skip-deploys
  fi
  for key in BINANCE_API_KEY BINANCE_API_SECRET USE_MAINNET SYMBOL OPENAI_API_KEY OPENAI_STRATEGY_MODEL \
             STRATEGY_CHAT_SECRET TRADE_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID \
             V2_STRATEGIES V2_PAPER_CAPITAL POLL_INTERVAL \
             LIVE_STRATEGY GRID_GEOFENCE_LOW GRID_GEOFENCE_HIGH GRID_TP_PCT GRID_DIP_PCT \
             GRID_NUM_BULLETS GRID_RESERVE_USDT GRID_REPRICE_THRESHOLD \
             REVOLUT_LIVE_ENABLED REVOLUT_X_API_KEY REVOLUT_X_PRIVATE_KEY_BASE64 \
             REVOLUT_X_BASE_URL REVOLUT_X_SYMBOL REVOLUT_X_MARKET_BASE_SIZE REVOLUT_POLL_INTERVAL; do
    val="${!key:-}"
    if [[ -n "$val" ]]; then
      railway variable set "${key}=${val}" -s "$BACKEND_SERVICE" --skip-deploys
    fi
  done
}

apply_frontend_env() {
  cd "$REPO_ROOT"
  if [[ -n "${BACKEND_PUBLIC_URL:-}" ]]; then
    railway variable set "VITE_BACKEND_URL=${BACKEND_PUBLIC_URL}" -s "$FRONTEND_SERVICE" --skip-deploys
  fi
}

echo "==> Applying Railway variables (backend: $BACKEND_SERVICE, frontend: $FRONTEND_SERVICE)"
apply_backend_env
apply_frontend_env

if $SKIP_DEPLOY || $ENV_ONLY; then
  echo "==> Skipping deploy (--skip-deploy or --env-only)."
  exit 0
fi

if [[ ! -d "$REPO_ROOT/.railway" ]]; then
  echo "Repo root is not linked. Run from $REPO_ROOT:"
  echo "  railway link -p <project> -s $BACKEND_SERVICE"
  exit 1
fi

if [[ ! -d "$REPO_ROOT/frontend/.railway" ]]; then
  echo "Frontend dir is not linked. Run from $REPO_ROOT/frontend:"
  echo "  railway link -p <project> -s $FRONTEND_SERVICE"
  exit 1
fi

echo "==> Deploying backend ($BACKEND_SERVICE)…"
(cd "$REPO_ROOT" && railway up -s "$BACKEND_SERVICE" -d --ci)

echo "==> Deploying frontend ($FRONTEND_SERVICE)…"
(cd "$REPO_ROOT/frontend" && railway up -s "$FRONTEND_SERVICE" -d --ci)

echo "Done."
