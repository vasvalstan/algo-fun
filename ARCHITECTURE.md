# Technical Architecture (V2)

## System Overview

ALGO-FUN is a cloud-native, real-time algorithmic trading system for BTCUSDT Spot on Binance. It consists of an asynchronous Python FastAPI backend trading engine and a high-performance React SPA frontend. They communicate via low-latency WebSockets. The system supports live trading on Binance and simulated paper trading simultaneously. The entire application is containerized and configured for deployment on Railway.

## Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Trading Engine | Python 3.9+ | — |
| Backend API | FastAPI + Uvicorn | ≥ 0.100 |
| Real-time Comm. | WebSockets | — |
| Web Framework | Vite + React | 19.x |
| Routing | TanStack Router | — |
| State Management| Zustand + TanStack Query | — |
| Styling | Tailwind CSS v4 / Vanilla CSS | ^4 |
| Deployment | Docker + Railway | — |
| Notifications | Telegram Bot API | — |
| Exchange | Binance Spot REST API | v3 |

## Python Backend (`api/`)

| File | Role |
|------|------|
| `api/main.py` | FastAPI entry point, manages background bot runners and WebSocket routing. |
| `api/ws_manager.py` | WebSocket connection manager handling backpressure and state broadcasting per-channel. |
| `api/bot_runner.py` | Async background task wrapping the live core strategy, pushes state to `/ws/live`. |
| `api/paper_runner.py` | Async background task simulating a $5,000 USDT paper wallet, pushes state to `/ws/paper` (no longer exposed in the SPA; still runs in-process). |
| `api/paper_runner_v2.py` | Multi-strategy paper runner; pushes unified state to `/ws/paper-v2`. |
| `api/notifications.py` | Handles asynchronous notifications to Telegram for buys, sells, stop losses, and errors. |
| `config.py` | Central config — reads `.env`, exposes all settings (API keys, symbol, parameters, Telegram config). |
| `auth.py` | Binance REST authentication — HMAC-SHA256 signing. |
| `trading.py` | Private API wrapping real Binance interactions (`place_order()`). |
| `indicators.py` | Strategy Engine — 6-timeframe analysis cascade (EMA, RSI, Swing structure, pullback detection). |
| `strategy.py` | Position Manager state machine (WATCHING → BUY_PLACED → HOLDING → SELL_PLACED). |
| `ledger.py` | Persistent all-time P&L ledger (`ledger.json`). |
| `dry_run.py` | Core offline paper trading + backtester engine (re-used by `api/paper_runner.py`). |

## Web Frontend (`frontend/src/`)

The frontend was massively upgraded from an SSR Next.js app to a CSR Vite SPA for drastically lower latency during high-speed market ticks.

| File/Dir | Role |
|------|------|
| `routes/` | TanStack file-based routing (`__root.tsx`, `index.tsx` for live, `paper-v2.tsx` for paper dashboard). |
| `components/` | Reusable React components rendering positions, strategy states, errors, etc. |
| `hooks/useWebSocket.ts` | Robust WebSocket hook with exponential backoff algorithm and strict memory leak protection. |
| `hooks/useBotState.ts` | Global Zustand store holding real-time snapshots of the currently active trading bot. |
| `lib/types.ts` | Strict TypeScript interfaces mapping 1-to-1 with the backend's Python dictionary payloads. |

## Data Flow

### Live Bot (`_bot_task`)
Runs endlessly as a background `asyncio` task within FastAPI. It polls the Binance REST API, passes price checks into the `TrendAwareMakerStrategy`, handles complex state transitions, updates the JSON ledger, dispatches Telegram message alerts, and finally broadcasts its massive `BotState` JSON snapshot dictionary to all clients connected via `/ws/live`. 

### Paper Trading (`_paper_task`)
Runs concurrently and autonomously inside FastAPI. It fetches real Binance klines and parses the exact same `StrategyEngine` logic, but instead operates a pseudo-wallet starting with a virtual `5,000 USDT`. It pushes state over `/ws/paper` (legacy channel; the dashboard uses V2 below).

### Paper Trading V2 (`_paper_v2_task`)
Runs as a background task (`api/paper_runner_v2.py`), drives multiple simulated strategies with split capital, and broadcasts rich state on `/ws/paper-v2` for the SPA.

### Web Dashboard
Connects via WebSocket through the shared hooks. Navigating to `/` hooks into `/ws/live` (live bot). Navigating to `/paper-v2` tears down the prior socket, clears stale client state, and connects to `/ws/paper-v2` for the multi-strategy paper view. Other routes (e.g. `/history`) follow the same pattern per page. 

## File Persistence

There are no external databases.
| File | Purpose |
|------|---------|
| `ledger.json` | All-time cumulative P&L and cycle tracker. Maintained across restarts. |
| `bot-data` (Volume) | Docker named volume ensuring `/app/ledger.json` outlives ephemeral container deployments. |
| `state.json` | Backward-compatibility legacy file fallback for the original terminal-based architecture. |
| `.env` | Environment file containing `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and Binance API Keys. |

## Containerization & Deployment (Railway)

This application leverages a monorepo architecture engineered for cloud deployment on platforms like Railway. It consists of two primary Docker containers run natively without needing `docker-compose`.

### 1. Backend Service (FastAPI)
The root `Dockerfile` configures the trading engine and WebSocket server.
- **Environment:** Lightweight Python `3.12-slim` image running `uvicorn api.main:app`.
- **State Persistence:** Requires a persistent volume mounted at `/app/data` to ensure `ledger.json` survives container restarts and updates.

### 2. Frontend Service (Vite + Nginx)
The `frontend/Dockerfile` uses a multi-stage process to serve the Dashboard.
- **Build Stage:** Compiles the Vite project into static CSR components using Node.
- **Production Stage:** An Nginx container that acts as a web server and a reverse proxy. By default, it aggressively proxies `/ws/` and `/api/` traffic to the backend, avoiding complex frontend CORS issues.

### Deploying to Railway (Step-by-Step Guide)

To host this repository on Railway, you must create **two separate services** from the same GitHub repository to handle the monorepo cleanly:

**Step 1: Deploy the Backend**
1. In your Railway project, click **New > GitHub Repo** and select this repository.
2. Railway will automatically detect the root `Dockerfile` and build it.
3. Once created, go to **Variables** and inject your environment keys (e.g., `BINANCE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `USE_MAINNET`).
4. Go to **Settings > Volumes**, click `Add Volume`, and set the Mount Path to `/app/data`.
5. Go to **Settings > Networking** and generate a **Private Network Domain**. Set the Custom Domain to `backend.railway.internal`. (This is critical for the frontend proxy to function).

**Step 2: Deploy the Frontend**
1. In the same Railway project, click **New > GitHub Repo** and select this repository again.
2. Before it builds, immediately go to the new service's **Settings > Build**.
3. Change the **Root Directory** from `/` to `/frontend`.
4. Railway will now detect `frontend/Dockerfile` and compile the web interface.
5. Go to **Settings > Networking** and generate a **Public Domain** (e.g., `algo-dashboard.up.railway.app`).

Because the frontend's Nginx configuration (`nginx.conf`) is designed to point proxy pass targets directly to `http://backend:8000`, mapping your backend to a private network domain named `backend` (or natively mapping the internal container name depending on Railway's routing) allows instant zero-config cross-talk while keeping your FastAPI instance completely private from the public web. In Railway, if you define the Private Domain as `backend.railway.internal`, you would simply update the `nginx.conf` proxy pass to point to `http://backend.railway.internal:8000` prior to deploying.

## Notifications & Security

**Telegram**: Key lifecycle events (Started, Stopped, Buy Filled, Sell Placed, Emergency Exit, Error) are asynchronously streamed directly to an admin's phone.
**Binance**: HMAC-SHA256 signed requests. Timestamp + `recvWindow` restrict replay attacks.

## Legacy Subsystems (Pending Cleanup/Deprecation)

- `web/`: The old V1 Next.js web application.
- `bot.py` / `dashboard.py`: The original V1 standalone CLI loop + ANSI console frontend.
