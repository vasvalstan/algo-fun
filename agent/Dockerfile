# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

# Bump CACHE_BUST when you need Railway to skip a stale Docker layer cache.
ARG CACHE_BUST=1
RUN true

WORKDIR /app

# System deps required by Hermes' install (git for pip-from-github,
# build-essential for PyJWT[crypto] / cryptography wheels on slim base).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install Hermes Agent.
#
# `hermes-agent` is not published on PyPI as of 2026-04-21 — install
# directly from the GitHub release tag. To upgrade, bump the tag in the
# git URL below.
#   - [messaging] pulls Telegram support and aiohttp (used by the
#     api_server platform adapter).
#   - [mcp] pulls the MCP client so the agent can talk to our trading
#     MCP server.
# Plus fastmcp + httpx for our own /app/mcp_server.py.
RUN pip install --no-cache-dir \
      "hermes-agent[messaging,mcp] @ git+https://github.com/NousResearch/hermes-agent@v2026.4.16" \
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
