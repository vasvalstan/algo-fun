# Hermes Agent Integration — Design

**Date:** 2026-04-21
**Status:** Approved (ready for implementation plan) — revised 2026-04-21 after Hermes docs research surfaced the built-in OpenAI-compatible API server adapter (replaces planned ACP spike + custom web gateway adapter).
**Scope:** Sub-project #1 of 3 — replace OpenClaw with Hermes (NousResearch) as the trading-control agent, add a web chat UI, keep existing Telegram approval flow untouched.

---

## Background

Today's Railway deployment has three services:

- `frontend` — Vite/React SPA served by Nginx, talks directly to backend's public URL.
- `backend` — FastAPI/Uvicorn. Owns the trading loop, exposes the REST API, and runs the **trade-approval Telegram bot** (separate from the agent).
- `openclaw` — OpenClaw + Gemma 4 31B + an MCP server (`openclaw/mcp_server.py`) that wraps the backend's REST endpoints. Chats via Telegram only.

The user wants to replace OpenClaw with [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the Nous Research successor to OpenClaw, MIT, Python), add a web chat UI in the React frontend, and eventually grant the agent code-edit + self-redeploy capabilities.

That last capability is large and security-sensitive enough to be its own design. This document covers **only** the OpenClaw → Hermes swap and the web chat UI. The code-editing and self-redeploy capabilities are explicitly deferred (see "Out of scope" below).

## Goals

1. Replace the `openclaw` Railway service with an `agent` service running Hermes.
2. Reach feature parity with today's OpenClaw bot for trading commands (price checks, position listing, trade requests, strategy toggles, etc.).
3. Add a web chat UI in the React frontend that talks to Hermes through the backend.
4. Keep the existing trade-approval Telegram bot inside the backend untouched — every trade still requires a Telegram tap.
5. Persist Hermes' memory, skills, and session history across Railway redeploys.

## Non-goals (deferred)

- **Code editing, git push, Railway redeploy from the agent.** This is sub-project #3 and has its own threat model.
- **Multi-user accounts.** Single-operator system; shared-token auth is sufficient.
- **Migrating OpenClaw memory/skills/personality.** Fresh Hermes install with no inherited state.
- **Multi-provider model routing.** OpenRouter only for v1; revisit if cost/quality demands it.
- **Polished chat UI** (history sidebar, model picker, token usage dashboard, etc.). Minimum viable surface in v1; iterate later.

## Architecture

```
Browser ──https──▶ frontend (Vite/React/Nginx, static)
                       │
                       │ HTTPS + SSE streaming (token-gated)
                       ▼
                   backend (FastAPI)
                       │
                       │ thin reverse proxy:
                       │   - check Authorization: Bearer <AGENT_CHAT_TOKEN>
                       │   - inject X-Hermes-Session-Id (stable per browser session)
                       │   - forward to agent service over Railway private network
                       │   - stream SSE bytes back unmodified
                       ▼
                   agent service (Hermes, Python)
                       │
                       ├── OpenRouter (model provider, OPENROUTER_API_KEY)
                       ├── MCP server: algo-fun-trading (moved from openclaw/)
                       │       └── HTTP calls to backend REST endpoints
                       ├── Telegram gateway (Hermes built-in, second bot token)
                       └── api_server platform adapter (Hermes built-in)
                              - listens on 127.0.0.1:8642
                              - OpenAI-compatible: POST /v1/chat/completions
                                (SSE streaming, X-Hermes-Session-Id for continuity)
                              - GET /health for Railway healthcheck
                       │
                       └── Persistent state: Railway Volume mounted at /data
                           (HERMES_HOME=/data/.hermes)
                              - config.yaml + .env + sessions/ + skills/ +
                                memories/ + logs/
```

Three Railway services after migration: `frontend`, `backend`, `agent` (replacing `openclaw`).

## Components

### `agent` service (new, replaces `openclaw`)

- Python base image (`python:3.11-slim` or similar), Hermes installed via the upstream installer or `pip install hermes-agent` from a pinned commit.
- **Curated install — not `[all]`.** Excludes voice deps, RL/Atropos, Modal/Daytona/Singularity backends. Goal: smallest image that still has the gateway runtime, MCP client, OpenAI-wire provider client (for OpenRouter), Telegram adapter, and `api_server` adapter dependencies (`aiohttp`).
- One Railway Volume mounted at `/data`. `HERMES_HOME=/data/.hermes`.
- Hermes config split (per upstream convention):
  - **Non-secrets** in `/data/.hermes/config.yaml` (model, terminal backend, gateway platform enablement, MCP server registration, tool/toolset config).
  - **Secrets** in `/data/.hermes/.env` (`OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`).
  - Required Railway env vars (read at container start, written into `.env` by the entrypoint script the first time the volume is empty): `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` (operator's Telegram numeric ID), `ALGOFUN_BACKEND_URL`, `TRADE_API_SECRET`, `AGENT_CHAT_TOKEN`.
- **Terminal backend:** `local` (inside the container). The agent has no need for sandboxed terminal access in sub-project #1 — it only calls MCP tools that hit the backend's REST API. We further restrict capabilities via `hermes tools` (disable `terminal_tool`, `file_tools`, `code_execution_tool`, `browser_tool`) so a model misstep can't run shell commands or touch the filesystem. Code-edit capabilities arrive in sub-project #3 with their own threat model.
- Process: a single long-lived `hermes gateway` invocation that runs both the Telegram adapter and the `api_server` adapter concurrently in one event loop.
- Container `EXPOSE 8642` (api_server) for backend → agent traffic over Railway private networking.

### MCP server (moved, not rewritten)

- `openclaw/mcp_server.py` → `agent/mcp_server.py`. Same FastMCP module, same tools, same backend endpoints. No logic changes in v1.
- Registered in Hermes config under `mcp.servers.algo-fun-trading`.

### Web chat surface — Hermes' built-in `api_server` adapter

Hermes ships an OpenAI-compatible HTTP server as a first-class platform adapter. We use it as-is — no spike, no fork, no custom adapter.

Endpoints we use:

- `POST /v1/chat/completions` — OpenAI Chat Completions format with SSE streaming (`stream: true`). Session continuity opt-in via `X-Hermes-Session-Id` header (a stable opaque ID we generate per browser tab on first load and persist in `localStorage`).
- `GET /health` — used as the Railway healthcheck for the agent service.

Tool-call rendering: Chat Completions streams tool-call deltas in the standard OpenAI shape. The frontend renders them as collapsible blocks (tool name, arguments, result) using the streamed `delta.tool_calls` events.

Adapter config (`config.yaml`):

```yaml
platforms:
  api_server:
    enabled: true
    extra:
      host: "0.0.0.0"   # bind to all interfaces inside the container
      port: 8642
      # No public auth here — the backend is the only client and gates with
      # AGENT_CHAT_TOKEN. The api_server itself is reachable only over
      # Railway's private network, never from the public internet.
```

Subprocess + stdio bridging (parsing TUI output) is **rejected** — too brittle for production. ACP is **rejected** — it's IDE-only (VS Code/Zed/JetBrains via stdio/JSON-RPC), not a browser chat surface.

### `backend` service (modified)

- One new endpoint:
  - `POST /api/agent/chat/completions` — accepts an OpenAI Chat Completions request body, requires `Authorization: Bearer <AGENT_CHAT_TOKEN>`, forwards to the agent service's `POST http://agent.railway.internal:8642/v1/chat/completions`. SSE bytes from upstream are streamed back to the browser unmodified (`StreamingResponse` with `media_type="text/event-stream"`).
  - Backend injects `X-Hermes-Session-Id` if the client included a `session_id` field in the request body (which the frontend generates per browser tab and stores in `localStorage`).
- Backend does not run any model or agent logic — pure reverse proxy + auth check + session-id injection.
- No other backend changes. The trading REST API, the trade-approval Telegram bot, and existing endpoints are untouched.

### `frontend` service (modified)

- New route `/chat` with a chat panel. Components:
  - Token entry on first load (saves to `localStorage` under `agentChatToken`); cleared on 401.
  - Stable session id generated on first load (`crypto.randomUUID()`), persisted in `localStorage` under `agentSessionId`. Sent on every request as the `session_id` field; backend translates to `X-Hermes-Session-Id` header for upstream.
  - Message list with streaming assistant tokens (read SSE via `fetch` + `ReadableStream` — no extra dependencies).
  - Tool-call rendering (collapsible blocks: tool name, arguments, result), driven by `delta.tool_calls` chunks in the SSE stream.
  - Input box; submit on Enter, Shift+Enter for newline.
- POSTs to `${VITE_BACKEND_URL}/api/agent/chat/completions` with `Authorization: Bearer <token>` and request body `{ messages, model: "hermes", stream: true, session_id }`.
- No other frontend changes; existing dashboard pages stay as they are.

### Hermes config (baked into the image at `agent/config.yaml`, copied to `/data/.hermes/config.yaml` on first boot)

- **Model:** OpenRouter as the default provider. Default model: **Hermes 4 70B** (`openrouter/nousresearch/hermes-4-70b`) — same vendor as the agent framework (Nous Research), so it's the most likely model the framework's tool-calling pipeline is verified against. 131K context, hybrid reasoning (toggle via `reasoning: enabled`), $0.13 / $0.40 per M tokens on OpenRouter — roughly 8× cheaper than the 405B variant and a fit for single-operator burst usage. The operator can switch any time via `hermes config set model <slug>` or `/model` in chat. Documented alternatives: `openrouter/nousresearch/hermes-4-405b` (smarter, $1/$3) or fall back to OpenClaw's familiar Gemma family if Hermes-family tool-use disappoints.
- **Provider env:** `OPENROUTER_API_KEY` in `.env`; provider-block in `config.yaml` references it via `${OPENROUTER_API_KEY}`.
- **Platforms enabled:** `telegram` and `api_server`. Both share the gateway process.
- **Telegram:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` (operator's numeric ID only). Hermes' default DM-deny policy keeps everyone else out.
- **MCP:** `algo-fun-trading` server pointing at `/app/mcp_server.py` (moved from `openclaw/mcp_server.py` with no logic change). Receives `ALGOFUN_BACKEND_URL` and `TRADE_API_SECRET` via env passthrough.
- **Tools:** Restrict to MCP-only via `hermes tools` (disable `terminal_tool`, `file_tools`, `code_execution_tool`, `browser_tool`, `web_tools`). The agent's only capabilities in v1 are the trading MCP tools.
- **Skills/memory:** defaults, stored under `HERMES_HOME` on the Railway Volume.

## Auth model

- **Web chat:** shared `AGENT_CHAT_TOKEN` (single string, env var). Backend rejects any chat request without the matching `Authorization: Bearer ...` header.
- **Telegram:** Hermes' built-in DM allowlist policy — only the operator's Telegram user ID can DM the Hermes bot.
- **Trade execution:** unchanged. Every trade still produces a Telegram approval prompt from the backend's notification bot, with Approve/Reject buttons. A leaked `AGENT_CHAT_TOKEN` lets an attacker chat with the agent and propose trades, but cannot move money without a tap on the operator's phone.

## Persistence

- One Railway Volume attached to the `agent` service, mounted at `/data`.
- `HERMES_HOME=/data/.hermes`. Hermes writes its SQLite databases (sessions, FTS5 search, Honcho), skills directory, memory files, and any future learning-loop artifacts under that path.
- Survives container restarts and redeploys. Backed up by Railway.

## Deletion plan (`openclaw`)

- Stop and delete the `openclaw` Railway service.
- Delete the `openclaw/` directory from the repo (the MCP server file is being moved to `agent/mcp_server.py` first; everything else goes).
- Remove OpenClaw-specific env vars from project settings.
- Remove OpenClaw mentions from `RAILWAY_DEPLOYMENT.md` and `README.md`; replace with the `agent` service section.

## Acceptance criteria

1. Browser chat at `/chat` works end-to-end: token entry → first message → streamed reply, including tool-call rendering for at least `get_market_status` and `list_positions`.
2. Hermes can perform every action OpenClaw could today: price checks, listing positions, requesting trades (which still require Telegram approval), toggling strategies, fetching performance.
3. Telegram chat with the Hermes bot works (parity with old OpenClaw bot's Telegram surface).
4. Memory or a created skill survives a Railway redeploy of the `agent` service (verifiable via `/sessions` or `/skills` listing after redeploy).
5. The `openclaw` Railway service is deleted; the `openclaw/` directory is removed; documentation no longer references OpenClaw.

## Open questions / risks

1. **Hermes resource footprint on Railway.** Need to measure RAM/CPU on the Railway Hobby/Starter plan. Mitigation: curated install (no voice/RL/Daytona/Modal/Singularity), restricted toolset.
2. **Default model tool-use quality.** Hermes 4 70B is chosen as default because it's same-vendor as the agent framework — most likely to be regression-tested by them — but we have not independently benchmarked its MCP tool-call accuracy on the algo-fun trading tools. Phase 5 AC2 is the verification gate: if tool-call accuracy is poor, switch to `openrouter/nousresearch/hermes-4-405b` or `openrouter/google/gemma-4-31b-it` via `hermes config set model <slug>` — no code change.
3. **SSE through Railway's proxy.** Long-lived SSE connections occasionally get cut by intermediaries. The OpenAI client and Hermes' adapter both handle reconnection/keepalive; verify keepalive comments arrive within Railway's idle-timeout window during implementation. Hermes' `api_server` already sends SSE keepalives every 30s (`CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS`).
4. **Railway private networking name.** Spec assumes the agent service is reachable at `agent.railway.internal:8642`; confirm exact hostname during deploy and update env var `AGENT_INTERNAL_URL` accordingly.

## Implementation phases (high-level — full plan written next)

1. Build `agent` service: Dockerfile, `config.yaml`, entrypoint that seeds `.env` from Railway env vars, MCP server moved from `openclaw/`.
2. Provision Railway Volume; deploy `agent` service; verify `GET /health` and a manual `curl` to `/v1/chat/completions`.
3. Add backend `POST /api/agent/chat/completions` reverse-proxy endpoint with `AGENT_CHAT_TOKEN` check and SSE pass-through.
4. Add frontend `/chat` route and chat panel (token entry, session-id, SSE streaming, tool-call rendering).
5. End-to-end verification against acceptance criteria, including a Railway redeploy of the `agent` service to confirm volume persistence.
6. Cutover: delete `openclaw` Railway service, remove `openclaw/` directory, update `RAILWAY_DEPLOYMENT.md` and `README.md`.
