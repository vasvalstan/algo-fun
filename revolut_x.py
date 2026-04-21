"""
Revolut X Crypto Exchange REST client (Ed25519 request signing).

Sign payload: timestamp (ms string) + METHOD + path + query + body
(no separators). Path is the URL path starting with /api (e.g. /api/1.0/orders).
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE = "https://revx.revolut.com/api/1.0"


def _config() -> tuple[str, str, Path]:
    api_key = os.getenv("REVOLUT_X_API_KEY", "").strip()
    base = os.getenv("REVOLUT_X_BASE_URL", DEFAULT_BASE).rstrip("/")
    key_path = Path(os.getenv("REVOLUT_X_PRIVATE_KEY_PATH", "private.pem")).expanduser()
    return api_key, base, key_path


def _load_signing_key(pem_path: Path) -> Ed25519PrivateKey:
    b64 = os.getenv("REVOLUT_X_PRIVATE_KEY_BASE64", "").strip()
    if b64:
        data = base64.b64decode(b64)
    elif pem_path.is_file():
        data = pem_path.read_bytes()
    else:
        raise FileNotFoundError(
            f"Private key not found: {pem_path.resolve()}. "
            "Set REVOLUT_X_PRIVATE_KEY_BASE64 or place the PEM at REVOLUT_X_PRIVATE_KEY_PATH."
        )
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("REVOLUT_X private key must be Ed25519 (openssl genpkey -algorithm ed25519)")
    return key


def _sign(key: Ed25519PrivateKey, message: bytes) -> str:
    return base64.b64encode(key.sign(message)).decode("ascii")


def revx_request(
    method: str,
    endpoint: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Any:
    """
    Signed HTTP request to Revolut X.

    `endpoint` is appended to REVOLUT_X_BASE_URL, e.g. "/orders" or "orders".
    """
    api_key, base, pem_path = _config()
    if not api_key:
        raise RuntimeError("Set REVOLUT_X_API_KEY in .env")

    ep = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    url = f"{base}{ep}"

    data: Optional[str] = None
    headers: Dict[str, str] = {}
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":"))
        headers["Content-Type"] = "application/json"

    m = method.upper()
    req = requests.Request(m, url, params=params or None, data=data, headers=headers)
    session = requests.Session()
    prep = session.prepare_request(req)

    parsed = urlparse(prep.url)
    sign_path = parsed.path or ""
    sign_query = parsed.query if parsed.query else ""
    body = ""
    if prep.body:
        body = prep.body.decode("utf-8") if isinstance(prep.body, bytes) else str(prep.body)

    ts = str(int(time.time() * 1000))
    msg = f"{ts}{m}{sign_path}{sign_query}{body}".encode("utf-8")

    sk = _load_signing_key(pem_path)
    sig = _sign(sk, msg)

    prep.headers["X-Revx-API-Key"] = api_key
    prep.headers["X-Revx-Timestamp"] = ts
    prep.headers["X-Revx-Signature"] = sig

    resp = session.send(prep, timeout=timeout)
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Revolut X {resp.status_code}: {detail}")

    if not resp.content:
        return None
    return resp.json()
