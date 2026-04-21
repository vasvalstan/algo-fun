"""Tests for POST /api/agent/chat/completions reverse proxy."""

import importlib
from typing import Any, Dict

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_agent_env(monkeypatch):
    """Force a known token + upstream URL for every test in this module."""
    monkeypatch.setenv("AGENT_CHAT_TOKEN", "test-token-123")
    monkeypatch.setenv("AGENT_INTERNAL_URL", "http://upstream.test:8642")
    # Reload modules so the new env vars are picked up by `config` and
    # any FastAPI routers that capture the values at import time.
    import config  # type: ignore[import-untyped]

    importlib.reload(config)
    yield


@pytest.fixture
def client():
    """A TestClient that exercises only the agent router (avoids importing
    api.main, which spins up runners, telegram bots, market data feeds,
    etc. — none of which are relevant to the proxy unit tests)."""
    from fastapi import FastAPI

    from api.agent_proxy import router as agent_proxy_router

    # api/agent_proxy.py captures `config.AGENT_CHAT_TOKEN` /
    # AGENT_INTERNAL_URL at request time (not import time), so reloading
    # `config` in the fixture above is enough.
    app = FastAPI()
    app.include_router(agent_proxy_router)
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
def test_valid_token_proxies_with_upstream_auth_and_session_id(client):
    """The proxy must:
    - Strip our `session_id` extension from the JSON body
    - Send it upstream as `X-Hermes-Session-Id`
    - Inject `Authorization: Bearer <AGENT_CHAT_TOKEN>` upstream
      (Hermes' api_server requires API_SERVER_KEY when bound to 0.0.0.0)
    """
    captured: Dict[str, Any] = {}

    def _record(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.read()
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
    headers = captured["headers"]
    assert headers.get("x-hermes-session-id") == "browser-tab-abc"
    assert headers.get("authorization") == "Bearer test-token-123"

    import json as _json

    forwarded_body = _json.loads(captured["body"])
    assert "session_id" not in forwarded_body, (
        "session_id is a proxy-only extension and must be stripped before "
        "forwarding upstream"
    )


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
