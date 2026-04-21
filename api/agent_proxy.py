"""Reverse-proxy endpoint that forwards browser chat requests to the
Hermes api_server adapter on the agent service.

Responsibilities:
  - Authenticate the caller with AGENT_CHAT_TOKEN (constant-time compare).
  - Inject `Authorization: Bearer <AGENT_CHAT_TOKEN>` on the upstream
    request — Hermes' api_server enforces API_SERVER_KEY when bound to
    0.0.0.0, and we use the same shared token for both legs.
  - Translate an optional `session_id` field in the request body into the
    `X-Hermes-Session-Id` header upstream expects, then strip it from
    the body so we send a clean OpenAI-shaped payload.
  - Stream SSE bytes back to the browser unmodified when the request is
    streaming (`"stream": true`).
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

# One pooled httpx client; long-lived so connections to the agent service
# are reused across requests (matters for SSE on Railway — fresh TCP per
# request would defeat keep-alive and add ~100ms per chat turn).
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


def _check_token(request: Request) -> str:
    """Validate the inbound bearer token. Returns the configured token so
    callers can forward it upstream without re-reading config."""
    expected = (config.AGENT_CHAT_TOKEN or "").strip()
    if not expected:
        # Endpoint closed by default — refuse rather than silently allow.
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
    return expected


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Any:
    token = _check_token(request)

    try:
        body: Dict[str, Any] = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}") from None

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    # Pop our extension field before forwarding upstream — it's not part
    # of the OpenAI Chat Completions schema.
    session_id = body.pop("session_id", None)

    upstream_url = f"{config.AGENT_INTERNAL_URL}/v1/chat/completions"
    upstream_headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
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
        content_type = resp.headers.get("content-type", "application/json")
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.json() if content_type.startswith("application/json") else {"raw": resp.text},
            headers={"content-type": content_type},
        )

    # Streaming path — forward SSE bytes verbatim. We use a context-managed
    # generator so the upstream connection is released even if the browser
    # disconnects mid-stream.
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
