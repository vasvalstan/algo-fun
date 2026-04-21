FROM node:22-slim

# Bump when you need Railway to skip a stale Docker layer cache.
ARG CACHE_BUST=2
RUN true

WORKDIR /app

# Telegram polling loads from openclaw/dist/*.js — Node ESM resolves @scoped packages from
# dist/node_modules first, NOT openclaw/node_modules. Install at package root AND mirror into dist/.
RUN set -eux \
    && npm install -g openclaw@latest \
    && OC="$(npm root -g)/openclaw" \
    && cd "$OC" \
    && npm install grammy @grammyjs/runner \
    && test -d node_modules/@grammyjs/runner \
    && mkdir -p dist/node_modules/@grammyjs \
    && rm -rf dist/node_modules/@grammyjs/runner \
    && cp -a node_modules/@grammyjs/runner dist/node_modules/@grammyjs/runner \
    && cd dist \
    && node --input-type=module -e "import('@grammyjs/runner').then(() => console.log('esm:@grammyjs/runner ok'))"

ENV NODE_PATH=/usr/local/lib/node_modules/openclaw/node_modules:/usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install --no-cache-dir fastmcp httpx

COPY mcp_server.py /app/mcp_server.py
COPY openclaw.json /app/openclaw.json
COPY setup.sh /app/setup.sh
RUN chmod +x /app/setup.sh

CMD ["/app/setup.sh"]
