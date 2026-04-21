# Railway Deployment Guide

Lessons learned and patterns for deploying the ALGO-FUN monorepo (FastAPI backend + Vite/React frontend) on Railway with WebSocket streaming.

---

## Architecture

```
Browser ──wss──▶ backend-production-XXXX.up.railway.app (Uvicorn)
Browser ──https──▶ frontend-production-XXXX.up.railway.app (Nginx → static files only)
```

The frontend connects **directly** to the backend's public URL for WebSocket and API calls. Nginx serves static assets only — no reverse proxying.

---

## Why Not Nginx Reverse Proxy?

We tried proxying `/ws/` and `/api/` through Nginx to the backend's private hostname (`backend.railway.internal`). This failed repeatedly because:

1. **DNS caching** — Nginx resolves upstream hostnames once at startup and caches the IP forever. When Railway redeploys the backend (new container = new IP), Nginx keeps connecting to the dead old IP → `upstream timed out`.

2. **No usable resolver** — Railway's private DNS (`*.railway.internal`) is not reachable from the system resolver in `/etc/resolv.conf`. Using `resolver` + variable `proxy_pass` (the standard nginx fix for dynamic DNS) fails with `could not be resolved (110: Operation timed out)`.

3. **Every backend redeploy breaks the frontend** — Even env-var-only changes trigger a backend redeploy, which changes the IP and breaks the cached Nginx connection until the frontend is also redeployed.

**Solution**: Give the backend its own public domain and have the browser connect directly. This completely sidesteps DNS caching, and backend redeploys don't affect the frontend at all.

---

## Step-by-Step Setup

### 1. Create the Railway Project

Two services in one project: `backend` and `frontend`.

### 2. Backend Service

**Source**: Root directory (`/`)  
**Dockerfile**: `./Dockerfile`

The Dockerfile must bind Uvicorn to `$PORT` (Railway injects this):

```dockerfile
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
```

**Generate a public domain** for the backend:

```
railway domain --service backend
# → https://backend-production-XXXX.up.railway.app
```

**Required env vars on the backend service:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `FRONTEND_URL` | `https://frontend-production-XXXX.up.railway.app` | CORS origin |
| `CORS_ORIGINS` | `https://frontend-production-XXXX.up.railway.app` | Extra CORS origins |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | Notifications |
| `TELEGRAM_CHAT_ID` | `-XXXXXXXXXX` (group) or `XXXXXXXXX` (DM) | Notifications |
| `TELEGRAM_TEST_SECRET` | Any passphrase | Protects the test endpoint |

### 3. Frontend Service

**Source**: `frontend/` subdirectory  
**Dockerfile**: `frontend/Dockerfile`

> **Critical**: When deploying via Railway CLI or MCP, set `workspacePath` to the `frontend/` directory, not the repo root. Otherwise Railway picks up the root `Dockerfile` (backend) and deploys the wrong service.

The Dockerfile passes `VITE_BACKEND_URL` as a build arg so Vite can inline it:

```dockerfile
FROM node:22-alpine AS build
WORKDIR /app
ARG VITE_BACKEND_URL
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.d/10-backend-upstream.sh /docker-entrypoint.d/10-backend-upstream.sh
RUN chmod +x /docker-entrypoint.d/10-backend-upstream.sh
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
```

**Required env vars on the frontend service:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `VITE_BACKEND_URL` | `https://backend-production-XXXX.up.railway.app` | Baked into JS at build time; browser connects directly |

### 4. Nginx Config (Static Only)

Nginx only serves the SPA and static assets. No proxy blocks:

```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location = /index.html {
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }

    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
```

The entrypoint script (`10-backend-upstream.sh`) only handles Railway's `PORT` injection:

```bash
#!/bin/sh
set -e
LISTEN_PORT="${PORT:-80}"
sed -i "s|listen 80;|listen ${LISTEN_PORT};|" /etc/nginx/conf.d/default.conf
```

### 5. Frontend Code Pattern

WebSocket and API calls read the backend URL from `import.meta.env.VITE_BACKEND_URL`:

```typescript
// WebSocket connection
const backendUrl = import.meta.env.VITE_BACKEND_URL || '';
let url: string;
if (backendUrl) {
  const wsBase = backendUrl.replace(/^http/, 'ws');
  url = `${wsBase}/ws/${channel}`;
} else {
  // Local dev fallback — same origin
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  url = `${protocol}//${window.location.host}/ws/${channel}`;
}

// API calls
const base = import.meta.env.VITE_BACKEND_URL || '';
fetch(`${base}/api/test-telegram`, { ... });
```

This means local dev (`npm run dev` with Vite proxy or same origin) works with no env var, and production uses the injected URL.

### 6. CORS Configuration

The backend's FastAPI CORS middleware must include the frontend's public domain:

```python
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
EXTRA_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", *EXTRA_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

## Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| WebSocket 504 / timeout after backend redeploy | Nginx cached the old backend IP | Don't proxy through Nginx; connect directly to backend public URL |
| `*.railway.internal could not be resolved` | System DNS can't resolve Railway private hostnames from Nginx resolver directive | Use public domain instead of private networking |
| Frontend deploys backend code | `workspacePath` pointed to repo root instead of `frontend/` | Always use `frontend/` as the workspace path for frontend deploys |
| Old UI shows after deploy | Browser cached `index.html` | Set `Cache-Control: no-cache` on `index.html`; hard refresh with `Cmd+Shift+R` |
| Healthcheck fails on frontend | Nginx not listening on Railway's `PORT` | Entrypoint script replaces `listen 80` with `listen $PORT` |
| Healthcheck fails on backend | Uvicorn not binding to Railway's `PORT` | Use `--port ${PORT:-8000}` in CMD |
| Telegram 502 error | Wrong `TELEGRAM_CHAT_ID` | Use the real chat/group ID from `getUpdates`; group IDs are negative numbers |
| `VITE_BACKEND_URL` empty at runtime | Env var not declared as `ARG` in Dockerfile | Add `ARG VITE_BACKEND_URL` + `ENV VITE_BACKEND_URL=$VITE_BACKEND_URL` before `npm run build` |

---

## Deployment Commands

```bash
# Deploy backend (from repo root)
railway up --service backend

# Deploy frontend (from frontend/ dir)
cd frontend && railway up --service frontend

# Or via MCP:
# deploy(workspacePath="/path/to/algo-fun", service="backend")
# deploy(workspacePath="/path/to/algo-fun/frontend", service="frontend")
```

---

## OpenClaw Service (AI Agent — Optional)

A third Railway service that runs OpenClaw with Gemma 4, connected to the backend via MCP.

**Source**: `openclaw/` subdirectory  
**Dockerfile**: `openclaw/Dockerfile`

**Required env vars:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `GEMINI_API_KEY` | Google AI API key | Powers Gemma 4 model |
| `TELEGRAM_BOT_TOKEN` | Second bot token | OpenClaw's Telegram bot (separate from notification bot) |
| `ALGOFUN_BACKEND_URL` | `https://backend-production-XXXX.up.railway.app` | Backend API for MCP skill |
| `TRADE_API_SECRET` | Same as backend | Authenticates trade requests |

**Deploy:**

```bash
cd openclaw && railway up --service openclaw
```

> You need **two Telegram bots**: one for trade notifications/approvals (runs in FastAPI backend), and one for OpenClaw's natural language agent. Both can be in the same group chat.

---

## Redeployment Behavior

- **Backend env var change** → Railway auto-redeploys backend. Frontend is unaffected (connects via public URL).
- **Frontend env var change** → If it's a `VITE_*` var, you must redeploy (it's baked at build time). Non-VITE vars take effect on restart.
- **OpenClaw env var change** → Railway auto-redeploys. No impact on backend or frontend.
- **Code change** → Push or manually trigger deploy. No auto-deploy unless connected to a Git repo.
