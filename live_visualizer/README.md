# Algo Fun Live Visualizer

Separate real-time paper-trading visualization service for Binance `BTCUSDC`.

It subscribes to Binance public market data, renders a live candlestick chart, and overlays paper-bot state such as support, resistance, regime, active orders, open bags, average entry, and PnL. It does not place exchange orders.

## Run Locally

```bash
cd "/Users/foitik/Desktop/ALGO FUN"
.venv/bin/python -m uvicorn live_visualizer.api.app:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`.

## Environment

- `VIS_SYMBOL`: default `BTCUSDC`
- `VIS_INTERVAL`: default `1m`
- `VIS_CANDLE_LIMIT`: default `500`
- `VIS_REFRESH_INTERVAL_MS`: default `1000`
- `VIS_STATE_FILE`: optional JSON state file from a paper bot
- `VIS_STATE_API_URL`: optional HTTP endpoint returning shared bot state
- `VIS_BINANCE_WS_BASE`: default `wss://stream.binance.com:9443`
- `PORT`: Railway port, default `8080`

## Shared State

The preferred state shape is:

```json
{
  "symbol": "BTCUSDC",
  "last_price": 0.0,
  "regime": "RANGE",
  "support_level": 0.0,
  "resistance_level": 0.0,
  "active_orders": [],
  "open_bags": [],
  "cash": 0.0,
  "position_qty": 0.0,
  "pnl_realized": 0.0,
  "pnl_unrealized": 0.0,
  "grid_version": 0,
  "updated_at": "2026-05-30T00:00:00Z"
}
```

The reader also adapts existing Algo Fun LIFO dashboard snapshots.

## Railway

Create and deploy as a separate Railway service from the repository root:

```bash
railway add --service live-visualizer
railway up ./live_visualizer --path-as-root --service live-visualizer --detach
railway domain --service live-visualizer --port 8080
```

If the Railway service is configured from the repository root instead of
`./live_visualizer`, set its Dockerfile path to:

```text
Dockerfile.live_visualizer
```

Then redeploy `live-visualizer`. Do not let this service use the repo-root
`Dockerfile`, because that file starts the existing Algo Fun backend.
