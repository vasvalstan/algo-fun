# Hermes Agent Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the OpenClaw Railway service with a Hermes Agent service, add a token-gated web chat UI in the React frontend that streams from Hermes' built-in OpenAI-compatible API server through a FastAPI reverse proxy, and decommission OpenClaw.

**Architecture:** Three Railway services (`frontend`, `backend`, `agent`). Hermes runs in the new `agent` container with both the Telegram gateway and the `api_server` platform adapter (port 8642) enabled. Backend proxies `POST /api/agent/chat/completions` over Railway private network with `AGENT_CHAT_TOKEN` auth and SSE pass-through. Frontend `/chat` route uses `fetch + ReadableStream` to consume the SSE stream — no extra dependencies. Hermes state (sessions, skills, memory) lives on a Railway Volume mounted at `/data` (`HERMES_HOME=/data/.hermes`).

**Tech Stack:** Python 3.12, FastAPI 0.115, httpx 0.27, Hermes Agent (latest), aiohttp, React 19, Vite 8, TanStack Router, zustand, OpenRouter, Railway Volumes, Docker.

**Spec:** [`docs/superpowers/specs/2026-04-21-hermes-agent-integration-design.md`](../specs/2026-04-21-hermes-agent-integration-design.md)

---

## Conventions used by this plan

- **Test command for backend:** `python -m pytest tests/ -v` from repo root.
- **Test command for new agent code:** `python -m pytest agent/tests/ -v` from repo root.
- **Run dev backend:** `uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload` from repo root.
- **Run dev frontend:** `cd frontend && npm run dev` (Vite serves on `http://localhost:5173`).
- **Run agent locally (after build):** `docker run --rm -it --env-file ./agent/.env.local -p 8642:8642 -v $(pwd)/agent/.local-data:/data algo-fun-agent`
- **Lint frontend:** `cd frontend && npm run lint`.
- **Railway CLI deploy commands:** `railway up --service <name>` from the appropriate workspace path. The Railway MCP `deploy` tool with `workspacePath=...` works the same way and is preferred when available.
- **Where commits should be made:** at the end of each task. The repo is initialized in Phase 0 Task 0.1; if you skip that task, drop the `git commit` lines but keep the rest of the steps.

---

## Phase 0 — Foundations

### Task 0.1: Initialize git, add `.gitignore`, baseline commit

**Files:**
- Create: `.gitignore`
- Create: `.git/` (via `git init`)

- [ ] **Step 1: Initialize the repository**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
git init -b main
git config user.email "$(git config --get user.email || echo 'operator@algo-fun.local')"
git config user.name  "$(git config --get user.name  || echo 'Operator')"
```

Expected output: `Initialized empty Git repository in .../algo-fun/.git/`.

- [ ] **Step 2: Write `.gitignore`** (covers Python, Node, Vite, secrets, runtime artifacts already in the repo)

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
.venv/
venv/
.pytest_cache/

# Node / Vite
node_modules/
dist/

# Secrets and local env
.env
.env.local
.env.*.local
*.pem.bak.*
private.pem
public.pem

# Runtime state and logs
state.json
paper_state.json
paper_dashboard.json
ledger.json
bot.log
dry_run.log
*.log

# Local agent data
agent/.local-data/
agent/.env.local

# Editor / OS
.DS_Store
.idea/
.vscode/
*.swp
```

- [ ] **Step 3: Initial commit (baseline)**

```bash
git add .gitignore
git commit -m "chore: git init with .gitignore"

git add -A
git commit -m "chore: import existing codebase as baseline"
```

Expected: two commits in `git log --oneline`; runtime files (`*.pem`, `state.json`, `bot.log`, `node_modules/`, `dist/`) excluded by `.gitignore`. Run `git status` and confirm it's clean.

---

### Task 0.2: Install pytest and create the `tests/` skeleton

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Add pytest to `requirements.txt`**

Append to `/Users/vasvalstan/Downloads/algo-fun/requirements.txt`:

```text
pytest>=8.3.0
pytest-asyncio>=0.24.0
respx>=0.21.0
```

(`respx` mocks `httpx` — needed in Phase 3 for the reverse-proxy tests.)

- [ ] **Step 2: Install the new deps**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
python -m pip install -r requirements.txt
```

- [ ] **Step 3: Create `tests/__init__.py`** (empty file)

```python
```

- [ ] **Step 4: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def anyio_backend():
    """Force pytest-asyncio to use asyncio (not trio)."""
    return "asyncio"
```

- [ ] **Step 5: Create `tests/test_smoke.py`** to prove pytest works

```python
def test_pytest_runs():
    assert 1 + 1 == 2
```

- [ ] **Step 6: Run the smoke test**

```bash
python -m pytest tests/test_smoke.py -v
```

Expected: `1 passed` in green.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt tests/
git commit -m "chore: add pytest, respx, and tests/ skeleton"
```

---

## Phase 1 — Build the agent service

### Task 1.1: Create `agent/` directory and move the MCP server (no logic change)

**Files:**
- Create: `agent/`
- Create: `agent/mcp_server.py` (copied from `openclaw/mcp_server.py`)
- Modify (to be deleted later in Phase 6): `openclaw/mcp_server.py` stays in place for now

- [ ] **Step 1: Copy the MCP server file unchanged**

```bash
mkdir -p /Users/vasvalstan/Downloads/algo-fun/agent
cp /Users/vasvalstan/Downloads/algo-fun/openclaw/mcp_server.py /Users/vasvalstan/Downloads/algo-fun/agent/mcp_server.py
```

- [ ] **Step 2: Sanity-check it imports**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
python -c "import importlib.util; spec=importlib.util.spec_from_file_location('m','agent/mcp_server.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print('tools:', [t for t in dir(m) if not t.startswith('_')][:5])"
```

Expected: prints a list of attribute names including `mcp` and the tool functions (`get_market_status`, `request_trade`, `list_positions`, ...). No ImportError.

- [ ] **Step 3: Commit**

```bash
git add agent/mcp_server.py
git commit -m "feat(agent): copy MCP server from openclaw/ as starting point"
```

---

### Task 1.2: Write the Hermes config baked into the image

**Files:**
- Create: `agent/config.yaml`

- [ ] **Step 1: Write `agent/config.yaml`** with model, platforms, MCP, tool restrictions

```yaml
# Hermes Agent config — non-secrets only.
# Secrets (OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, etc.) live in /data/.hermes/.env,
# seeded from Railway env vars by /app/entrypoint.sh on first boot.

model: openrouter/nousresearch/hermes-4-70b

terminal:
  backend: local
  cwd: "/app"
  timeout: 30

# Restrict the agent to MCP-only tooling for sub-project #1.
# Code-edit / shell access is intentionally disabled here — that's sub-project #3
# territory and has its own threat model.
tools:
  disabled:
    - terminal_tool
    - file_tools
    - code_execution_tool
    - browser_tool
    - web_tools
    - delegate_tool

mcp_servers:
  algo-fun-trading:
    command: python
    args: ["-m", "fastmcp", "run", "/app/mcp_server.py"]
    env:
      ALGOFUN_BACKEND_URL: ${ALGOFUN_BACKEND_URL}
      TRADE_API_SECRET: ${TRADE_API_SECRET}

platforms:
  telegram:
    enabled: true
    extra:
      # TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS are read from .env.
      pass

  api_server:
    enabled: true
    extra:
      host: "0.0.0.0"
      port: 8642
      # Public auth happens at the backend (AGENT_CHAT_TOKEN). The api_server
      # is only reachable on Railway's private network.

display:
  tool_progress: all
```

- [ ] **Step 2: YAML lint check**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
python -c "import yaml; yaml.safe_load(open('agent/config.yaml'))" && echo OK
```

Expected: prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add agent/config.yaml
git commit -m "feat(agent): add baked Hermes config (OpenRouter + Telegram + api_server + MCP)"
```

---

### Task 1.3: Write the entrypoint that seeds `.env` from Railway env vars

The Railway Volume mounted at `/data` is empty on first boot of a new service. The entrypoint copies the baked `config.yaml` into `/data/.hermes/config.yaml` if not present, and writes `/data/.hermes/.env` from the env vars Railway injects.

**Files:**
- Create: `agent/entrypoint.sh`

- [ ] **Step 1: Write `agent/entrypoint.sh`**

```bash
#!/usr/bin/env bash
# Hermes agent entrypoint.
#
# - Seeds /data/.hermes/config.yaml from /app/config.yaml on first boot.
# - Always rewrites /data/.hermes/.env from current Railway env vars so secret
#   rotation works without touching the volume manually.
# - Then exec's `hermes gateway` so signals (SIGTERM from Railway) reach Hermes.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/data/.hermes}"
mkdir -p "$HERMES_HOME"

# Seed config.yaml only on first boot. After that the operator can edit it via
# `hermes config edit` or `hermes config set` and changes survive redeploys.
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

# Show what's enabled (without leaking secrets).
echo "[entrypoint] HERMES_HOME=$HERMES_HOME"
echo "[entrypoint] config.yaml first lines:"
head -n 5 "$HERMES_HOME/config.yaml" || true
echo "[entrypoint] .env keys: $(grep -oE '^[A-Z_]+' "$HERMES_HOME/.env" | tr '\n' ' ')"

# Hand off to Hermes — gateway runs both Telegram and api_server platforms.
exec hermes gateway
```

- [ ] **Step 2: Mark it executable in git**

```bash
chmod +x /Users/vasvalstan/Downloads/algo-fun/agent/entrypoint.sh
```

- [ ] **Step 3: Smoke-check the script syntax**

```bash
bash -n /Users/vasvalstan/Downloads/algo-fun/agent/entrypoint.sh && echo OK
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add agent/entrypoint.sh
git update-index --chmod=+x agent/entrypoint.sh
git commit -m "feat(agent): add entrypoint that seeds Hermes config + .env from Railway env"
```

---

### Task 1.4: Write the agent Dockerfile

**Files:**
- Create: `agent/Dockerfile`
- Create: `agent/.dockerignore`

- [ ] **Step 1: Write `agent/.dockerignore`**

```text
.local-data/
.env.local
__pycache__/
*.pyc
```

- [ ] **Step 2: Write `agent/Dockerfile`**

The Hermes installer requires git, curl, and a few common build tools. We then install a curated set of Python packages — Hermes itself, plus `aiohttp` (api_server adapter), `python-telegram-bot` (Telegram adapter via Hermes), `fastmcp` and `httpx` (our MCP server).

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# Bump CACHE_BUST when you need Railway to skip a stale Docker layer cache.
ARG CACHE_BUST=1
RUN true

WORKDIR /app

# System deps required by Hermes' installer + a few of its optional features.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install Hermes Agent.
# We pin to the latest released version on PyPI. To upgrade, bump the version
# and rebuild. (`hermes-agent[telegram]` pulls Telegram-only extras.)
RUN pip install --no-cache-dir \
      "hermes-agent>=0.10.0" \
      "aiohttp>=3.9" \
      "fastmcp>=0.4.0" \
      "httpx>=0.27"

# Copy our MCP server, baked Hermes config, and entrypoint.
COPY mcp_server.py /app/mcp_server.py
COPY config.yaml   /app/config.yaml
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# HERMES_HOME points at the Railway Volume mount.
ENV HERMES_HOME=/data/.hermes \
    PYTHONUNBUFFERED=1

# api_server adapter listens here; Railway exposes via private networking.
EXPOSE 8642

# Healthcheck against the api_server adapter's /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8642/health || exit 1

CMD ["/app/entrypoint.sh"]
```

- [ ] **Step 3: Build the image locally to confirm it builds**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/agent
docker build -t algo-fun-agent:dev .
```

Expected: image builds without errors. If `pip install hermes-agent` fails with "no matching distribution", check PyPI for the actual current version: `pip index versions hermes-agent` (you may need to use `pip install hermes` or install from GitHub via `pip install git+https://github.com/NousResearch/hermes-agent@v0.10.0` — pin to the exact tag in that case and update the `RUN pip install` line accordingly).

- [ ] **Step 4: Commit**

```bash
git add agent/Dockerfile agent/.dockerignore
git commit -m "feat(agent): add Dockerfile and .dockerignore for the Hermes service"
```

---

### Task 1.5: Local smoke run of the agent container

This verifies the container boots, the entrypoint seeds the volume, the api_server adapter binds, and `/health` responds — all without needing Railway.

**Files:**
- Create: `agent/.env.local` (gitignored — local secrets)
- Create: `agent/.local-data/` (gitignored — emulates Railway Volume locally)

- [ ] **Step 1: Create local-only env file**

```bash
mkdir -p /Users/vasvalstan/Downloads/algo-fun/agent/.local-data
cat > /Users/vasvalstan/Downloads/algo-fun/agent/.env.local <<'EOF'
OPENROUTER_API_KEY=sk-or-REPLACE_ME
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
ALGOFUN_BACKEND_URL=http://host.docker.internal:8000
TRADE_API_SECRET=local-test-secret
EOF
```

> Replace `sk-or-REPLACE_ME` with your real OpenRouter key (any key that can hit at least one model on the free tier — used only for the boot smoke test, not real chats yet). Leave Telegram empty for the local run; we test it end-to-end in Phase 6.

- [ ] **Step 2: Run the container**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
docker run --rm --name algo-fun-agent-smoke \
  --env-file ./agent/.env.local \
  -p 8642:8642 \
  -v "$(pwd)/agent/.local-data:/data" \
  algo-fun-agent:dev
```

Watch the logs. Expected (within ~30s):
- `[entrypoint] Seeding /data/.hermes/config.yaml from /app/config.yaml`
- `[entrypoint] HERMES_HOME=/data/.hermes`
- Hermes startup banner
- A line indicating `api_server` is listening on `0.0.0.0:8642`

If you see "TELEGRAM_BOT_TOKEN missing" warnings, that's fine — Telegram is empty on purpose for now.

- [ ] **Step 3: Hit the health endpoint** from another terminal

```bash
curl -i http://127.0.0.1:8642/health
```

Expected: `HTTP/1.1 200 OK` and a JSON body like `{"status":"ok",...}`.

- [ ] **Step 4: Hit `/v1/chat/completions` non-streaming with a trivial prompt**

```bash
curl -sS -X POST http://127.0.0.1:8642/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "hermes",
        "stream": false,
        "messages": [{"role":"user","content":"Say hello in one word."}]
      }' | head -c 500
```

Expected: an OpenAI-shaped response with a `choices[0].message.content` field. (If you get an OpenRouter auth error, fix the key in `.env.local` and restart the container — the entrypoint rewrites `.env` on every boot.)

- [ ] **Step 5: Stop the container** (`Ctrl-C` in the terminal running it).

- [ ] **Step 6: Verify volume persistence**

```bash
ls /Users/vasvalstan/Downloads/algo-fun/agent/.local-data/.hermes/
```

Expected: at least `config.yaml` and `.env`. May also include `sessions.db` and other Hermes-created files.

- [ ] **Step 7: Commit** (only `.dockerignore` covers `.local-data/` and `.env.local`; nothing new should be staged)

```bash
git status   # expect: nothing to commit
```

If `git status` shows something unexpected, either add it to `.gitignore` or commit it deliberately.

---

## Phase 2 — Backend reverse-proxy endpoint

### Task 2.1: Add config and the new env var to the backend

**Files:**
- Modify: `config.py` (add the two new env vars)

- [ ] **Step 1: Find the existing config block**

```bash
grep -n "TRADE_API_SECRET" /Users/vasvalstan/Downloads/algo-fun/config.py | head -5
```

Note the line where existing secret env vars are read.

- [ ] **Step 2: Add `AGENT_CHAT_TOKEN` and `AGENT_INTERNAL_URL` to `config.py`**

Append (after the line where `TRADE_API_SECRET` is defined — adapt to the file's actual style; the snippet below assumes the file uses `os.getenv(...)` directly):

```python
# ─── Hermes Agent (web chat reverse proxy) ─────────────────────────────
# AGENT_CHAT_TOKEN: shared secret between frontend → backend → agent.
# Backend rejects /api/agent/* requests without a matching Bearer token.
AGENT_CHAT_TOKEN = (os.getenv("AGENT_CHAT_TOKEN") or "").strip()

# AGENT_INTERNAL_URL: where the agent service listens on Railway's private
# network. Locally for development point at the docker container, e.g.
# AGENT_INTERNAL_URL=http://127.0.0.1:8642
AGENT_INTERNAL_URL = (
    os.getenv("AGENT_INTERNAL_URL")
    or "http://agent.railway.internal:8642"
).rstrip("/")
```

- [ ] **Step 3: Verify import still works**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
python -c "import config; print('AGENT_CHAT_TOKEN present:', bool(config.AGENT_CHAT_TOKEN)); print('AGENT_INTERNAL_URL:', config.AGENT_INTERNAL_URL)"
```

Expected: prints `AGENT_CHAT_TOKEN present: False` (no env var set yet) and the default Railway URL.

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat(backend): add AGENT_CHAT_TOKEN and AGENT_INTERNAL_URL config"
```

---

### Task 2.2: Write the failing tests for the reverse-proxy endpoint

**Files:**
- Create: `tests/test_agent_proxy.py`

We test three things: (a) missing/wrong token → 401; (b) valid token → upstream is called with `X-Hermes-Session-Id` injected; (c) SSE bytes from upstream are streamed back unmodified.

- [ ] **Step 1: Create `tests/test_agent_proxy.py`**

```python
"""Tests for POST /api/agent/chat/completions reverse proxy."""

import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_agent_env(monkeypatch):
    """Force a known token + upstream URL for every test in this module."""
    monkeypatch.setenv("AGENT_CHAT_TOKEN", "test-token-123")
    monkeypatch.setenv("AGENT_INTERNAL_URL", "http://upstream.test:8642")
    # Reload config so the new env vars are picked up.
    import importlib
    import config
    importlib.reload(config)
    yield


@pytest.fixture
def client():
    # Import after env is set so config picks up our test token.
    from api.main import app
    return TestClient(app)


def test_missing_token_returns_401(client):
    resp = client.post(
        "/api/agent/chat/completions",
        json={"model": "hermes", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_wrong_token_returns_401(client):
    resp = client.post(
        "/api/agent/chat/completions",
        json={"model": "hermes", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


@respx.mock
def test_valid_token_proxies_to_upstream_with_session_id(client):
    captured_headers = {}

    def _record(request):
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    respx.post("http://upstream.test:8642/v1/chat/completions").mock(side_effect=_record)

    resp = client.post(
        "/api/agent/chat/completions",
        json={
            "model": "hermes",
            "messages": [{"role": "user", "content": "hi"}],
            "session_id": "browser-tab-abc",
        },
        headers={"Authorization": "Bearer test-token-123"},
    )
    assert resp.status_code == 200
    assert captured_headers.get("x-hermes-session-id") == "browser-tab-abc"


@respx.mock
def test_sse_stream_is_passed_through(client):
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    respx.post("http://upstream.test:8642/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )
    )

    resp = client.post(
        "/api/agent/chat/completions",
        json={
            "model": "hermes",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": "Bearer test-token-123"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == sse_body
```

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
python -m pytest tests/test_agent_proxy.py -v
```

Expected: all four tests fail with 404 (the route doesn't exist yet) — that's our target.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_agent_proxy.py
git commit -m "test(backend): add failing tests for /api/agent/chat/completions proxy"
```

---

### Task 2.3: Implement the reverse-proxy endpoint

**Files:**
- Create: `api/agent_proxy.py`
- Modify: `api/main.py` (mount the new router)

- [ ] **Step 1: Create `api/agent_proxy.py`**

```python
"""Reverse-proxy endpoint that forwards browser chat requests to the Hermes
api_server adapter on the agent service.

- Authenticates the caller with AGENT_CHAT_TOKEN.
- Translates an optional `session_id` field in the request body into the
  X-Hermes-Session-Id header upstream expects (so we don't have to teach the
  frontend to set custom headers across the proxy boundary).
- Streams SSE bytes back to the browser unmodified when the request is
  streaming (`"stream": true` in the body).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# One pooled httpx client; long-lived so connections to the agent service are
# reused across requests (important for SSE on Railway).
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


def _check_token(request: Request) -> None:
    """Raises 401 if Authorization header doesn't carry the configured token."""
    expected = (config.AGENT_CHAT_TOKEN or "").strip()
    if not expected:
        # If no token is configured the endpoint is closed by default — refuse
        # rather than silently allowing all traffic.
        raise HTTPException(
            status_code=503,
            detail="Agent chat is disabled: AGENT_CHAT_TOKEN is not set on the backend.",
        )
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token.")
    presented = auth[len("Bearer "):].strip()
    if presented != expected:
        raise HTTPException(status_code=401, detail="Invalid agent chat token.")


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Any:
    _check_token(request)

    try:
        body: Dict[str, Any] = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}") from None

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    # Pop our extension field before forwarding upstream — it's not part of
    # the OpenAI Chat Completions schema.
    session_id = body.pop("session_id", None)

    upstream_url = f"{config.AGENT_INTERNAL_URL}/v1/chat/completions"
    upstream_headers: Dict[str, str] = {"Content-Type": "application/json"}
    if isinstance(session_id, str) and session_id.strip():
        upstream_headers["X-Hermes-Session-Id"] = session_id.strip()

    is_streaming = bool(body.get("stream"))
    client = _get_client()

    if not is_streaming:
        try:
            resp = await client.post(upstream_url, json=body, headers=upstream_headers)
        except httpx.RequestError as e:
            log.warning("Agent upstream unreachable: %s", e)
            raise HTTPException(status_code=502, detail=f"Agent upstream error: {e}") from None
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text},
            headers={"content-type": resp.headers.get("content-type", "application/json")},
        )

    # Streaming path — forward SSE bytes verbatim.
    async def _iter():
        try:
            async with client.stream("POST", upstream_url, json=body, headers=upstream_headers) as upstream:
                if upstream.status_code != 200:
                    text = (await upstream.aread()).decode("utf-8", errors="replace")
                    yield (
                        f'data: {{"error": {json.dumps({"status": upstream.status_code, "body": text})}}}\n\n'
                    ).encode()
                    return
                async for chunk in upstream.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.RequestError as e:
            log.warning("Agent SSE upstream error: %s", e)
            yield f'data: {{"error": {json.dumps({"message": str(e)})}}}\n\n'.encode()

    return StreamingResponse(
        _iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable any proxy buffering
            "Connection": "keep-alive",
        },
    )
```

- [ ] **Step 2: Mount the router in `api/main.py`**

Find a good place near the top of the route-definition section (after `app = FastAPI(...)` and the CORS middleware setup) and insert:

```python
from api.agent_proxy import router as agent_proxy_router

app.include_router(agent_proxy_router)
```

If `api/main.py` already has an `include_router` block, add this line alongside the others; otherwise add the import near the other `from api ...` imports at the top of the file and add the `app.include_router` call after the FastAPI app is created. The bottom of the imports block is around line 44 (`from api import audit`); add the new import there, and add `app.include_router(agent_proxy_router)` immediately after the `app = FastAPI(...)` declaration on/around line 154–156.

- [ ] **Step 3: Run the tests and confirm they pass**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
python -m pytest tests/test_agent_proxy.py -v
```

Expected: all four tests pass. If `test_missing_token_returns_401` fails with 503 instead of 401, the test config didn't set `AGENT_CHAT_TOKEN` correctly — re-check Task 2.2 fixture.

- [ ] **Step 4: Run the existing test suite to make sure we didn't break anything**

```bash
python -m pytest tests/ -v
```

Expected: previous tests still pass, plus the four new ones.

- [ ] **Step 5: Manual sanity test against the local agent container**

In one terminal:

```bash
cd /Users/vasvalstan/Downloads/algo-fun
docker run --rm --name algo-fun-agent-smoke \
  --env-file ./agent/.env.local \
  -p 8642:8642 \
  -v "$(pwd)/agent/.local-data:/data" \
  algo-fun-agent:dev
```

In another terminal:

```bash
cd /Users/vasvalstan/Downloads/algo-fun
AGENT_CHAT_TOKEN=local-dev-token \
AGENT_INTERNAL_URL=http://127.0.0.1:8642 \
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

In a third terminal:

```bash
curl -sS -N -X POST http://127.0.0.1:8000/api/agent/chat/completions \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "hermes",
        "stream": true,
        "messages": [{"role":"user","content":"What MCP tools do you have?"}],
        "session_id": "local-tab-1"
      }'
```

Expected: SSE chunks stream in (`data: ...` lines) ending in `data: [DONE]`. If Hermes uses its MCP server you should see tool-call deltas. Cancel with `Ctrl-C` if the stream is taking too long.

- [ ] **Step 6: Commit**

```bash
git add api/agent_proxy.py api/main.py
git commit -m "feat(backend): reverse-proxy /api/agent/chat/completions to Hermes api_server"
```

---

## Phase 3 — Frontend chat UI

### Task 3.1: Add the chat token + session id helpers

**Files:**
- Create: `frontend/src/lib/agentChat.ts`

- [ ] **Step 1: Create `frontend/src/lib/agentChat.ts`**

```typescript
/**
 * Token + session id helpers for the agent chat UI.
 *
 * Token: shared secret entered once on the chat page, persisted in localStorage.
 * Session id: stable opaque ID per browser tab so Hermes can keep conversation
 *             continuity across messages (X-Hermes-Session-Id upstream).
 */

const TOKEN_KEY = 'agentChatToken';
const SESSION_KEY = 'agentSessionId';

export function getAgentToken(): string {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setAgentToken(token: string): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

export function getOrCreateSessionId(): string {
  let sid = localStorage.getItem(SESSION_KEY);
  if (!sid) {
    sid = (crypto.randomUUID?.() ?? Math.random().toString(36).slice(2));
    localStorage.setItem(SESSION_KEY, sid);
  }
  return sid;
}

export function resetSession(): void {
  localStorage.removeItem(SESSION_KEY);
}
```

- [ ] **Step 2: Type-check the new file**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/frontend
npx tsc --noEmit
```

Expected: no errors. If TanStack's generated `routeTree.gen.ts` has unrelated noise, ignore — we only care about no errors in `src/lib/agentChat.ts`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/agentChat.ts
git commit -m "feat(frontend): add agent chat token + session id helpers"
```

---

### Task 3.2: Add the SSE streaming client

**Files:**
- Create: `frontend/src/lib/agentStream.ts`

The browser's `EventSource` doesn't support POST or custom headers, so we use `fetch` + `ReadableStream` and parse SSE frames ourselves.

- [ ] **Step 1: Create `frontend/src/lib/agentStream.ts`**

```typescript
import { apiUrl } from './apiBase';
import { getAgentToken, getOrCreateSessionId } from './agentChat';

export type ChatMessage = {
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  tool_calls?: Array<{
    id?: string;
    function?: { name?: string; arguments?: string };
  }>;
};

export type StreamEvent =
  | { type: 'delta'; content: string }
  | { type: 'tool_call_delta'; index: number; name?: string; argumentsDelta?: string }
  | { type: 'done' }
  | { type: 'error'; message: string };

type StreamHandlers = {
  onEvent: (e: StreamEvent) => void;
  signal?: AbortSignal;
};

/**
 * Parses an OpenAI-style SSE delta into our normalized StreamEvent.
 * Returns undefined if the chunk is not interesting (e.g. role-only delta).
 */
function parseChunk(json: any): StreamEvent[] {
  const out: StreamEvent[] = [];
  const choice = json?.choices?.[0];
  if (!choice) return out;
  const delta = choice.delta || {};
  if (typeof delta.content === 'string' && delta.content.length > 0) {
    out.push({ type: 'delta', content: delta.content });
  }
  if (Array.isArray(delta.tool_calls)) {
    for (const tc of delta.tool_calls) {
      out.push({
        type: 'tool_call_delta',
        index: typeof tc.index === 'number' ? tc.index : 0,
        name: tc.function?.name,
        argumentsDelta: tc.function?.arguments,
      });
    }
  }
  if (choice.finish_reason) {
    out.push({ type: 'done' });
  }
  return out;
}

/**
 * POST a chat completion request and stream events back via onEvent.
 * Throws on auth failure (401) so callers can clear the saved token.
 */
export async function streamChat(
  messages: ChatMessage[],
  handlers: StreamHandlers,
): Promise<void> {
  const token = getAgentToken();
  if (!token) throw new Error('No agent token set.');

  const body = {
    model: 'hermes',
    stream: true,
    messages,
    session_id: getOrCreateSessionId(),
  };

  const resp = await fetch(apiUrl('/api/agent/chat/completions'), {
    method: 'POST',
    signal: handlers.signal,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
  });

  if (resp.status === 401) {
    throw new Error('UNAUTHORIZED');
  }
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => '');
    throw new Error(`Upstream error ${resp.status}: ${text || 'no body'}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  // SSE frames are separated by a blank line (\n\n). Each frame is one or
  // more "field: value" lines. We only consume `data:` lines (OpenAI doesn't
  // use event types here).
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      const data = dataLines.join('\n').trim();
      if (!data) continue;
      if (data === '[DONE]') {
        handlers.onEvent({ type: 'done' });
        return;
      }
      try {
        const parsed = JSON.parse(data);
        if (parsed?.error) {
          handlers.onEvent({ type: 'error', message: JSON.stringify(parsed.error) });
          continue;
        }
        for (const ev of parseChunk(parsed)) handlers.onEvent(ev);
      } catch {
        // Tolerate occasional partial frames — they'll be retried by the
        // next read tick.
      }
    }
  }
  handlers.onEvent({ type: 'done' });
}
```

- [ ] **Step 2: Type-check**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/frontend
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/agentStream.ts
git commit -m "feat(frontend): SSE streaming client for /api/agent/chat/completions"
```

---

### Task 3.3: Build the chat panel component

**Files:**
- Create: `frontend/src/components/AgentChatPanel.tsx`

- [ ] **Step 1: Create `frontend/src/components/AgentChatPanel.tsx`**

```tsx
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  getAgentToken,
  setAgentToken,
} from '../lib/agentChat';
import { streamChat, type ChatMessage } from '../lib/agentStream';

type ToolCallView = {
  index: number;
  name: string;
  argumentsText: string;
};

type Turn = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  toolCalls: ToolCallView[];
  isStreaming: boolean;
};

const PANEL: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: 'calc(100vh - 80px)',
  maxWidth: 900,
  margin: '0 auto',
  padding: '16px',
  gap: 12,
};

const MESSAGES: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  border: '1px solid var(--border-subtle)',
  borderRadius: 10,
  background: 'var(--bg-secondary)',
  padding: 12,
};

const BUBBLE_USER: React.CSSProperties = {
  alignSelf: 'flex-end',
  maxWidth: '80%',
  background: 'rgba(167, 139, 250, 0.18)',
  border: '1px solid rgba(167, 139, 250, 0.4)',
  padding: '8px 12px',
  borderRadius: 12,
  margin: '6px 0',
  whiteSpace: 'pre-wrap',
};

const BUBBLE_ASSISTANT: React.CSSProperties = {
  alignSelf: 'flex-start',
  maxWidth: '80%',
  background: 'var(--bg-tertiary, #1a1a1a)',
  border: '1px solid var(--border-subtle)',
  padding: '8px 12px',
  borderRadius: 12,
  margin: '6px 0',
  whiteSpace: 'pre-wrap',
};

const TOOLCALL_BOX: React.CSSProperties = {
  marginTop: 6,
  padding: '6px 10px',
  background: 'rgba(52, 211, 153, 0.08)',
  border: '1px solid rgba(52, 211, 153, 0.3)',
  borderRadius: 8,
  fontFamily: 'ui-monospace, SFMono-Regular, monospace',
  fontSize: '0.78rem',
};

const INPUT_ROW: React.CSSProperties = {
  display: 'flex',
  gap: 8,
};

const INPUT: React.CSSProperties = {
  flex: 1,
  resize: 'vertical',
  minHeight: 56,
  padding: 10,
  borderRadius: 8,
  border: '1px solid var(--border-subtle)',
  background: 'var(--bg-primary, #0d0d0d)',
  color: 'var(--text-primary)',
  fontFamily: 'inherit',
};

const BUTTON: React.CSSProperties = {
  alignSelf: 'flex-end',
  padding: '10px 16px',
  borderRadius: 8,
  border: 'none',
  background: 'var(--accent, #a78bfa)',
  color: '#fff',
  cursor: 'pointer',
  fontWeight: 600,
};

export function AgentChatPanel() {
  const [tokenInput, setTokenInput] = useState('');
  const [hasToken, setHasToken] = useState(() => Boolean(getAgentToken()));
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Convert UI turns back into the OpenAI message format on send.
  const sendableHistory = useMemo<ChatMessage[]>(
    () =>
      turns
        .filter((t) => t.text || t.toolCalls.length > 0)
        .map((t) => ({ role: t.role, content: t.text })),
    [turns],
  );

  useEffect(() => {
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight });
  }, [turns]);

  function saveToken() {
    const t = tokenInput.trim();
    if (!t) return;
    setAgentToken(t);
    setHasToken(true);
    setTokenInput('');
  }

  function clearToken() {
    setAgentToken('');
    setHasToken(false);
  }

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    setError(null);
    setDraft('');

    const userTurn: Turn = {
      id: `u-${Date.now()}`,
      role: 'user',
      text,
      toolCalls: [],
      isStreaming: false,
    };
    const asstTurn: Turn = {
      id: `a-${Date.now()}`,
      role: 'assistant',
      text: '',
      toolCalls: [],
      isStreaming: true,
    };
    setTurns((prev) => [...prev, userTurn, asstTurn]);
    setBusy(true);

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      const messagesForApi: ChatMessage[] = [
        ...sendableHistory,
        { role: 'user', content: text },
      ];
      await streamChat(messagesForApi, {
        signal: ac.signal,
        onEvent: (ev) => {
          setTurns((prev) => {
            const next = prev.slice();
            const last = next[next.length - 1];
            if (!last || last.role !== 'assistant') return prev;
            if (ev.type === 'delta') {
              last.text += ev.content;
            } else if (ev.type === 'tool_call_delta') {
              const existing = last.toolCalls[ev.index];
              if (existing) {
                if (ev.name && !existing.name) existing.name = ev.name;
                if (ev.argumentsDelta)
                  existing.argumentsText += ev.argumentsDelta;
              } else {
                last.toolCalls[ev.index] = {
                  index: ev.index,
                  name: ev.name ?? '',
                  argumentsText: ev.argumentsDelta ?? '',
                };
              }
            } else if (ev.type === 'done') {
              last.isStreaming = false;
            } else if (ev.type === 'error') {
              setError(ev.message);
              last.isStreaming = false;
            }
            return next;
          });
        },
      });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg === 'UNAUTHORIZED') {
        setError('Token rejected. Please re-enter it.');
        clearToken();
      } else {
        setError(msg);
      }
      setTurns((prev) => {
        const next = prev.slice();
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') last.isStreaming = false;
        return next;
      });
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  if (!hasToken) {
    return (
      <div style={{ ...PANEL, justifyContent: 'center' }}>
        <h2 style={{ margin: 0 }}>Agent chat</h2>
        <p style={{ color: 'var(--text-dim)' }}>
          Enter the agent chat token (the value of <code>AGENT_CHAT_TOKEN</code> on
          the backend). It's saved in this browser only.
        </p>
        <input
          type="password"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') saveToken();
          }}
          placeholder="paste token"
          style={{ ...INPUT, minHeight: 'unset' }}
        />
        <button type="button" onClick={saveToken} style={BUTTON}>
          Save and continue
        </button>
      </div>
    );
  }

  return (
    <div style={PANEL}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: 0 }}>Agent chat</h2>
        <button type="button" onClick={clearToken} style={{ ...BUTTON, background: 'transparent', color: 'var(--text-dim)' }}>
          Sign out
        </button>
      </div>

      <div ref={messagesRef} style={MESSAGES}>
        {turns.length === 0 && (
          <p style={{ color: 'var(--text-dim)' }}>
            Try: "What's the BTC price right now?" or "List my open positions."
          </p>
        )}
        {turns.map((t) => (
          <div
            key={t.id}
            style={{ display: 'flex', flexDirection: 'column', alignItems: t.role === 'user' ? 'flex-end' : 'flex-start' }}
          >
            <div style={t.role === 'user' ? BUBBLE_USER : BUBBLE_ASSISTANT}>
              {t.text || (t.isStreaming ? <em style={{ color: 'var(--text-dim)' }}>thinking…</em> : null)}
              {t.toolCalls.map((tc) => (
                <details key={tc.index} style={TOOLCALL_BOX}>
                  <summary>tool: {tc.name || '(streaming…)'}</summary>
                  <pre style={{ margin: '6px 0 0', whiteSpace: 'pre-wrap' }}>
                    {tc.argumentsText || '(streaming…)'}
                  </pre>
                </details>
              ))}
            </div>
          </div>
        ))}
      </div>

      {error && (
        <div style={{ color: 'var(--danger, #f87171)' }}>{error}</div>
      )}

      <div style={INPUT_ROW}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Ask Hermes…  (Enter to send, Shift+Enter for newline)"
          style={INPUT}
        />
        {busy ? (
          <button type="button" onClick={stop} style={{ ...BUTTON, background: 'var(--danger, #f87171)' }}>
            Stop
          </button>
        ) : (
          <button type="button" onClick={() => void send()} style={BUTTON} disabled={!draft.trim()}>
            Send
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/frontend
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AgentChatPanel.tsx
git commit -m "feat(frontend): AgentChatPanel component (token gate, SSE streaming, tool-call rendering)"
```

---

### Task 3.4: Wire the `/chat` route

**Files:**
- Create: `frontend/src/routes/chat.tsx`
- Modify: `frontend/src/routes/__root.tsx` (add nav link)
- Auto-modified by Vite plugin: `frontend/src/routeTree.gen.ts`

- [ ] **Step 1: Create `frontend/src/routes/chat.tsx`**

```tsx
import { createFileRoute } from '@tanstack/react-router';
import { AgentChatPanel } from '../components/AgentChatPanel';

export const Route = createFileRoute('/chat')({
  component: ChatRoute,
});

function ChatRoute() {
  return <AgentChatPanel />;
}
```

- [ ] **Step 2: Add the nav link in `__root.tsx`**

In `/Users/vasvalstan/Downloads/algo-fun/frontend/src/routes/__root.tsx`, find the existing nav block (around line 109–114):

```tsx
<Link to="/" className="nav-link" activeProps={{ style: { opacity: 1 } }}>
  Trading
</Link>
<Link to="/history" className="nav-link" activeProps={{ style: { opacity: 1 } }}>
  History
</Link>
```

Append a third link:

```tsx
<Link to="/chat" className="nav-link" activeProps={{ style: { opacity: 1 } }}>
  Chat
</Link>
```

- [ ] **Step 3: Run dev server, regenerate route tree, type-check**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/frontend
npm run dev
```

Vite/TanStack Router file-route plugin auto-writes `routeTree.gen.ts` with the new `/chat` entry. Wait for it to print "ready", then in another terminal:

```bash
cd /Users/vasvalstan/Downloads/algo-fun/frontend
npx tsc --noEmit
```

Expected: no errors. If `routeTree.gen.ts` doesn't include `/chat` yet, save `chat.tsx` again to trigger regeneration.

- [ ] **Step 4: Manual smoke test in the browser**

With the agent container, the backend, and `npm run dev` all running locally (see Phase 2 Task 2.3 Step 5):

1. Open `http://localhost:5173/chat`.
2. Enter `local-dev-token` (the token you exported in Step 5) and click Save.
3. Send "What MCP tools do you have?".

Expected: streaming text appears, optionally followed by collapsible tool-call blocks. If you see "Token rejected", confirm `AGENT_CHAT_TOKEN=local-dev-token` is set in the backend's environment.

- [ ] **Step 5: Stop dev server (Ctrl+C). Commit.**

```bash
git add frontend/src/routes/chat.tsx frontend/src/routes/__root.tsx frontend/src/routeTree.gen.ts
git commit -m "feat(frontend): add /chat route and nav link"
```

---

## Phase 4 — Railway deployment

### Task 4.1: Document environment variables required by Railway

**Files:**
- Modify: `RAILWAY_DEPLOYMENT.md`

- [ ] **Step 1: In `RAILWAY_DEPLOYMENT.md`, replace the entire "OpenClaw Service (AI Agent — Optional)" section** (currently lines 213–245 in the existing file) with this new section:

```markdown
## Agent Service (Hermes)

A third Railway service that runs Hermes Agent (Nous Research). Replaces the
old OpenClaw service with the same trading-control surface plus a web chat UI.

**Source**: `agent/` subdirectory  
**Dockerfile**: `agent/Dockerfile`

**Required env vars on the backend service (one new var):**

| Variable             | Value                                  | Purpose                                          |
|----------------------|----------------------------------------|--------------------------------------------------|
| `AGENT_CHAT_TOKEN`   | A long random string                   | Shared secret for /api/agent/* requests          |
| `AGENT_INTERNAL_URL` | `http://agent.railway.internal:8642`   | Where to reach the agent service privately       |

**Required env vars on the new `agent` service:**

| Variable                | Value                                                      | Purpose                                          |
|-------------------------|------------------------------------------------------------|--------------------------------------------------|
| `OPENROUTER_API_KEY`    | `sk-or-...`                                                | Model provider                                   |
| `TELEGRAM_BOT_TOKEN`    | Second bot token from @BotFather                           | Hermes' Telegram chat                            |
| `TELEGRAM_ALLOWED_USERS`| Your Telegram numeric user ID                              | DM allowlist (single-operator)                   |
| `ALGOFUN_BACKEND_URL`   | `https://backend-production-XXXX.up.railway.app`           | Backend REST for the MCP server                  |
| `TRADE_API_SECRET`      | Same value as backend's `TRADE_API_SECRET`                 | Authenticates trade requests                     |
| `AGENT_CHAT_TOKEN`      | Same value as backend's `AGENT_CHAT_TOKEN`                 | Currently unused inside the agent (reserved)     |

**Required Railway Volume on the `agent` service:**

- Mount path: `/data` (Hermes uses `HERMES_HOME=/data/.hermes`).
- Stores SQLite session DB, skills, memories, logs, and the operator's
  `config.yaml` / `.env` (seeded by the entrypoint on first boot).
- Survives redeploys; backed up by Railway.

**Required healthcheck:** `GET /health` on port `8642`.

**Deploy:**

```bash
cd agent && railway up --service agent
```

**Why two Telegram bots?** The notification bot in the backend handles
trade-approval buttons. The Hermes bot is a separate conversational agent.
They can share the same group chat but need different bot tokens.
```

- [ ] **Step 2: Sanity-check the markdown renders**

```bash
head -n 30 /Users/vasvalstan/Downloads/algo-fun/RAILWAY_DEPLOYMENT.md
```

(Just confirm the file is intact; no broken sections.)

- [ ] **Step 3: Commit**

```bash
git add RAILWAY_DEPLOYMENT.md
git commit -m "docs(railway): replace openclaw section with agent (Hermes) service docs"
```

---

### Task 4.2: Provision Railway env vars and Volume on the new `agent` service

This task is operator-driven (no code changes). Use the Railway dashboard or the Railway MCP tools.

- [ ] **Step 1: Create the `agent` service**

In the Railway project that already contains `frontend` and `backend`:

```
Project → Add a new service → Empty service → name it "agent"
```

Or via Railway MCP:

```
deploy(workspacePath="/Users/vasvalstan/Downloads/algo-fun/agent", service="agent")
```

Note: this first deploy will fail because env vars and the volume aren't set yet — that's expected. We just want the service to exist.

- [ ] **Step 2: Attach a Volume**

Dashboard → `agent` service → Volumes → New Volume → mount path `/data`. (Default size — 1 GB is plenty for a single-operator agent.)

- [ ] **Step 3: Set env vars on the `agent` service**

Set the seven variables listed in Task 4.1 (`OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `ALGOFUN_BACKEND_URL`, `TRADE_API_SECRET`, `AGENT_CHAT_TOKEN`).

For `AGENT_CHAT_TOKEN`, generate a random one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save this value — you'll need to set the same value on the backend service in the next step.

- [ ] **Step 4: Set the two new env vars on the `backend` service**

| Variable             | Value                                  |
|----------------------|----------------------------------------|
| `AGENT_CHAT_TOKEN`   | The token from Step 3                  |
| `AGENT_INTERNAL_URL` | `http://agent.railway.internal:8642`   |

- [ ] **Step 5: Deploy the `agent` service**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/agent
railway up --service agent
```

Or via Railway MCP `deploy(workspacePath="/Users/vasvalstan/Downloads/algo-fun/agent", service="agent")`.

- [ ] **Step 6: Verify the service is healthy**

Watch the deployment logs:

```bash
railway logs --service agent
```

Expected: entrypoint output, Hermes startup, `api_server` listening on `0.0.0.0:8642`, no `OPENROUTER_API_KEY missing` errors. The Railway healthcheck on `:8642/health` should pass within ~60 seconds.

- [ ] **Step 7: Smoke-test from the backend container**

The `agent` service is on the private network, so we can't curl it from a laptop. Test via the backend instead — once the backend redeploys with `AGENT_CHAT_TOKEN` set, hit it from your laptop:

```bash
TOKEN=<the AGENT_CHAT_TOKEN you generated>
BACKEND=https://backend-production-XXXX.up.railway.app

curl -sS -N -X POST "$BACKEND/api/agent/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "hermes",
        "stream": true,
        "messages": [{"role":"user","content":"Reply with just OK."}],
        "session_id": "smoke-test"
      }' | head -c 1000
```

Expected: SSE chunks. If you get `502`, check the backend logs (`AGENT_INTERNAL_URL` typo or agent service not running).

- [ ] **Step 8: Verify volume persistence**

Trigger a redeploy of the `agent` service:

```bash
railway redeploy --service agent
```

Wait for it to come back up, then send another smoke test message with the same `session_id`. Hermes should remember the previous turn (you can ask "what did I just ask you to reply with?"). If it doesn't, check that the volume is actually mounted at `/data` and `HERMES_HOME=/data/.hermes` in the running container.

- [ ] **Step 9: No git commit needed for this task** (operator config only).

---

### Task 4.3: Frontend deploy

Frontend code already changed in Phase 3, but Railway hasn't picked it up yet. Re-deploy.

- [ ] **Step 1: Push and redeploy the frontend**

```bash
cd /Users/vasvalstan/Downloads/algo-fun/frontend
railway up --service frontend
```

Note: per `RAILWAY_DEPLOYMENT.md`, you must run from the `frontend/` workspace path so Railway picks up the right Dockerfile. The MCP equivalent: `deploy(workspacePath="/Users/vasvalstan/Downloads/algo-fun/frontend", service="frontend")`.

- [ ] **Step 2: Verify in the browser**

Visit `https://frontend-production-XXXX.up.railway.app/chat`. Enter the `AGENT_CHAT_TOKEN`. Send a message. Verify streaming + tool calls work end-to-end.

- [ ] **Step 3: No git commit needed** (no new code in this task).

---

## Phase 5 — End-to-end verification

### Task 5.1: Acceptance criteria walkthrough

- [ ] **AC1: Browser chat at `/chat` works end-to-end with token gate**

  - Visit `/chat` in incognito → token entry screen appears.
  - Enter the token → chat panel appears.
  - Send "what's the BTC price right now?" → streaming reply appears with a collapsible `get_market_status` tool-call block (or whichever tool Hermes picks).

- [ ] **AC2: Hermes can perform every action OpenClaw could**

  Send each of these in the chat panel and verify a sensible tool call + reply:

  - "What's the BTC price right now?" → `get_market_status`
  - "List my open positions" → `list_positions`
  - "Show me the strategies and which are enabled" → `get_strategies`
  - "Request a small BUY on BTCUSDT using v2_adaptive, 10 USDT" → `request_trade` (and you receive a Telegram approval prompt from the existing notification bot — **do not approve**, this is a verification step only; reject the prompt)
  - "Toggle the breakout strategy" → `toggle_strategy`
  - "How am I performing today?" → `get_performance`

- [ ] **AC3: Telegram chat with the Hermes bot works**

  - DM the second Telegram bot (the one whose token is set on the agent service).
  - Send `/start`, then "what's the BTC price?".
  - Expected: the Hermes bot replies. (If allowlist is wrong it silently ignores you — check `TELEGRAM_ALLOWED_USERS`.)

- [ ] **AC4: Memory survives a Railway redeploy**

  - In the chat panel, ask: "Remember that my preferred trade size is 25 USDT."
  - In Telegram, optionally do the same.
  - Run `railway redeploy --service agent` (or via Railway MCP).
  - Wait ~90 seconds for the service to come back up.
  - In the same browser tab (same `session_id`), ask: "What's my preferred trade size?"
  - Expected: Hermes recalls "25 USDT" from prior context.

- [ ] **AC5: Cutover is verified separately in Phase 6.**

If any of AC1–AC4 fail, do not proceed to Phase 6 — fix and re-verify.

---

## Phase 6 — Cutover (delete OpenClaw)

Only proceed once Phase 5 passes.

### Task 6.1: Stop and delete the `openclaw` Railway service

- [ ] **Step 1: Stop the OpenClaw service**

Dashboard: `openclaw` service → Settings → Pause service.

Watch its logs for ~5 minutes to confirm nothing is depending on it (e.g. no scheduled jobs, no background loops). If anything looks suspicious, leave it paused and investigate before deleting.

- [ ] **Step 2: Delete the service**

Dashboard: `openclaw` service → Settings → Danger zone → Delete service.

- [ ] **Step 3: Remove OpenClaw env vars from the project**

Dashboard: project-level shared variables → remove any OpenClaw-specific values (e.g. an old `GEMINI_API_KEY` if it was only for OpenClaw and not used elsewhere).

- [ ] **Step 4: No git commit needed** (operator action only).

---

### Task 6.2: Delete the `openclaw/` directory and references from the repo

**Files:**
- Delete: `openclaw/` directory
- Modify: `README.md` (remove any OpenClaw mentions; add a brief Hermes mention)

- [ ] **Step 1: Remove the directory**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
git rm -r openclaw/
```

- [ ] **Step 2: Find and update README references**

```bash
grep -n "openclaw\|OpenClaw" README.md
```

For each match, replace the OpenClaw mention with the equivalent Hermes mention, or delete the line if it was a deploy/configuration instruction (those now live in `RAILWAY_DEPLOYMENT.md`). If the README has a top-level "Services" section, replace any OpenClaw bullet with:

```markdown
- **agent** — Hermes Agent (Nous Research). Trading-control conversation surface
  reachable via the in-app `/chat` page or via Telegram. Wraps the backend's
  REST API as MCP tools. All trades still require a Telegram approval tap
  via the backend's notification bot.
```

- [ ] **Step 3: Confirm no remaining references**

```bash
grep -rn "openclaw\|OpenClaw" \
  --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=dist . | grep -v "docs/superpowers"
```

Expected: no matches outside the design/plan docs (those describe the migration and are allowed to mention OpenClaw historically).

- [ ] **Step 4: Commit the cutover**

```bash
git add -A
git commit -m "chore(cutover): remove openclaw/ directory and README references after Hermes migration"
```

---

### Task 6.3: Final tag + done

- [ ] **Step 1: Tag the cutover commit**

```bash
cd /Users/vasvalstan/Downloads/algo-fun
git tag -a hermes-v1 -m "Sub-project #1 complete: OpenClaw → Hermes swap, web chat UI, OpenClaw deleted."
```

- [ ] **Step 2: Verify final state**

```bash
git log --oneline -10
ls openclaw 2>&1   # expect: "ls: openclaw: No such file or directory"
ls agent/          # expect: Dockerfile, config.yaml, entrypoint.sh, mcp_server.py, .dockerignore
```

- [ ] **Step 3: Mark plan complete in the spec**

Edit the spec's status line:

```markdown
**Status:** ✅ Implemented (hermes-v1, 2026-04-21)
```

```bash
git add docs/superpowers/specs/2026-04-21-hermes-agent-integration-design.md
git commit -m "docs(spec): mark Hermes integration as implemented"
```

---

## Self-Review Notes

This plan was self-reviewed against the spec on 2026-04-21:

- **Spec coverage:** Each acceptance criterion (AC1–AC5) has a verifying step in Phase 5 or Phase 6. Each "Component" section in the spec maps to at least one task: `agent` service → Tasks 1.1–1.5 + 4.2; MCP server → Task 1.1; web chat surface (api_server) → Tasks 1.2 + 1.5 + 4.2; backend modifications → Tasks 2.1–2.3; frontend modifications → Tasks 3.1–3.4; Hermes config → Task 1.2; auth model → Task 2.1 (`AGENT_CHAT_TOKEN`) + Task 1.2 (Telegram allowlist) + unchanged backend approval bot; persistence → Task 4.2 Step 2 + AC4; deletion plan → Tasks 6.1–6.2.
- **Placeholder scan:** No "TBD/TODO" content in steps. Two intentional operator-judgment choices remain (the exact OpenRouter model in `config.yaml` defaults to `openrouter/anthropic/claude-3.5-sonnet`; the operator can `hermes config set model <slug>` after deploy without code changes — this matches the spec's deferred decision).
- **Type/path consistency:** `AGENT_CHAT_TOKEN` and `AGENT_INTERNAL_URL` names are used identically across `config.py`, the proxy module, the test, the docs, and Railway env-var instructions. The session-id field is named `session_id` in the JSON body and translated to header `X-Hermes-Session-Id` in exactly one place (`api/agent_proxy.py`).
- **Known gap:** The plan assumes `pip install hermes-agent` resolves on PyPI. If the upstream package name or version differs at implementation time, Task 1.4 Step 3 includes a fallback note (install from a pinned GitHub tag) — operator follows that note and the rest of the plan is unaffected.
