# algo-fun — Binance Spot Trading Bot (Starter)

A minimal Python trading bot that talks to the Binance REST API.
Built for learning: every file is small, does one thing, and is meant to be
read top-to-bottom.

**Default mode is testnet** — no real money is at risk until you explicitly
switch to mainnet.

### Companion docs

- [`LIFO_GRID_STRATEGY.md`](./LIFO_GRID_STRATEGY.md) — what the strategy does (plain-English + technical).
- [`EXECUTION_COSTS.md`](./EXECUTION_COSTS.md) — verified fee/spread numbers per venue + the BTCUSDT→BTCUSDC migration guide.
- [`RAILWAY_DEPLOYMENT.md`](./RAILWAY_DEPLOYMENT.md) — initial deploy of backend + frontend on Railway.
- [`OPERATIONS.md`](./OPERATIONS.md) — production runbook: state reset, phantom-bag recovery, common errors, probe scripts.

---

## What's in the box

| File | What it does |
|------|-------------|
| `config.py` | Loads API keys + strategy settings from `.env` |
| `auth.py` | HMAC-SHA256 request signing — the core of Binance authentication |
| `market_data.py` | Public endpoints: current price, order book, exchange info |
| `trading.py` | Private endpoints: place / list / cancel orders, account info |
| `main.py` | One-shot demo: fetch price, place order, cancel it |
| `strategy.py` | Mean-reversion state machine (buy dips, sell bounces) |
| `dashboard.py` | Live-updating terminal display (price, P&L, trade log) |
| `bot.py` | Continuous trading bot — runs the strategy with the dashboard |

---

## Prerequisites

- **Python 3.9+** (check with `python3 --version`)
- A **Binance Testnet** account (free, no KYC, fake money)

---

## Setup (step by step)

### 1. Get testnet API keys

Go to **https://testnet.binance.vision/** and log in with GitHub.
Once logged in, click **Generate HMAC_SHA256 Key**.
You'll see an **API Key** and a **Secret Key** — copy both.

### 2. Clone / download this project

```bash
cd ~/Downloads/algo-fun   # or wherever you put it
```

### 3. Create a virtual environment (recommended)

A virtual environment keeps this project's packages separate from the rest of
your system.

```bash
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

This installs two packages:
- **requests** — makes HTTP calls to Binance
- **python-dotenv** — reads your `.env` file so secrets stay out of code

### 5. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` in any editor and paste the API Key and Secret Key you copied
from the testnet page:

```
BINANCE_API_KEY=abc123...
BINANCE_API_SECRET=xyz789...
USE_MAINNET=false
```

### 6. Run the demo

```bash
python main.py
```

You should see output like:

```
2025-01-01 12:00:00  INFO      Using TESTNET
2025-01-01 12:00:00  INFO      Symbol: BTCUSDT

============================================================
  Current price
============================================================
{
  "symbol": "BTCUSDT",
  "price": "67000.00000000"
}

...placing order...listing...cancelling...

2025-01-01 12:00:01  INFO      Done. All steps completed successfully.
```

If you see errors, check:
- Are the keys pasted correctly (no extra spaces)?
- Is your internet connection working?
- Did you activate the virtual environment?

**Quick test without API keys** (only checks that the network and testnet URL work):

```bash
.venv/bin/python -c "from market_data import get_price; print(get_price())"
```

You should see a JSON line with `"symbol": "BTCUSDT"` and a `"price"`.

**If `main.py` prints price and order book but fails on placing an order (HTTP 401):**
- Keys must come from **https://testnet.binance.vision/** while `USE_MAINNET=false` (main site keys do not work on testnet).
- On the testnet key, enable **Spot trading** (or equivalent) if there is a permissions toggle.
- If you enabled **IP restriction** on the key, the machine running the script must use that same public IP (VPNs change it).

The script logs Binance’s response body on HTTP errors so you can read the exact reason (for example invalid key or permissions).

---

## How the code works

### Authentication (`auth.py`)

Binance uses **HMAC-SHA256** to verify that a request really came from you.
The process:

1. Build a query string from all your parameters + a millisecond timestamp.
2. Hash that string with your secret key → this produces the **signature**.
3. Append `&signature=<hex>` to the request.
4. Send your API key in the `X-MBX-APIKEY` header.

The `signed_request()` function does all of this automatically.

### Market data (`market_data.py`)

These are **public** endpoints — no keys needed.

- `get_price()` — latest price for a symbol
- `get_orderbook()` — bid/ask levels (who wants to buy and sell, and at
  what price)
- `get_exchange_info()` — trading rules like minimum order size and price
  precision

### Trading (`trading.py`)

These are **private** endpoints — every call is signed.

- `place_limit_order(side, quantity, price)` — submit a limit order
- `get_open_orders()` — list orders that haven't filled yet
- `cancel_order(order_id)` — pull an order off the book
- `get_account()` — see your balances

---

## Key concepts for beginners

### Limit order vs market order
- **Limit**: "I want to buy at *this* price or better." It waits on the
  order book.
- **Market**: "Buy now at whatever the current price is." Executes
  immediately but you might get a worse price on thin books.

This starter only uses limit orders because they're safer for a bot — you
always know the price you'll pay.

### Testnet vs mainnet
- **Testnet**: fake money, safe to experiment. Keys come from
  `testnet.binance.vision`.
- **Mainnet**: real money. Keys come from your actual Binance account.

The `USE_MAINNET` flag in `.env` controls which one the bot talks to.

### Order book
The order book is a live list of all open orders:
- **Bids** (buy orders): sorted highest price first
- **Asks** (sell orders): sorted lowest price first
- The **spread** is the gap between the best bid and best ask

### GTC (Good Till Cancelled)
When you place a limit order with `timeInForce=GTC`, it stays open until
either someone fills it or you cancel it. Other options exist (IOC, FOK)
but GTC is the simplest starting point.

---

## Running the live bot

Once `main.py` works (proves your keys and network are fine), start the
live bot:

```bash
source .venv/bin/activate
python bot.py
```

You'll see a live dashboard that refreshes every few seconds:

```
 ALGO-FUN  BTCUSDT  TESTNET           uptime 01h 23m 45s
──────────────────────────────────────────────────────────
 PRICE     66,881.75    MA(20)  66,920.12    diff  -0.06%
 STATE     WATCHING     cycles  12

 OPEN ORDER
  (none)

 P&L
  starting    500.00 USDT
  current     508.34 USDT   (+1.67%)
  fees paid     0.91 USDT

 LAST TRADES
  #12  BUY  66801.20 → SELL 67001.60  +0.30%  +1.50 USDT  14:32
  #11  BUY  66750.00 → SELL 66950.10  +0.30%  +1.50 USDT  14:18
──────────────────────────────────────────────────────────
 Ctrl+C to stop
```

Press **Ctrl+C** to stop.  The bot cancels any open order on exit.

A log file (`bot.log`) is written alongside the dashboard so you have a
persistent record of every action.

### How the strategy works

The bot uses **mean reversion** — it assumes the price will bounce back
toward a short-term average after dipping away from it.

1. It keeps a rolling window of recent prices (default: last 20 polls).
2. When the current price is **0.15%** below the moving average, it
   places a limit **BUY** at the current price.
3. Once the buy fills, it immediately places a limit **SELL** at
   entry price **+ 0.30%**.
4. When the sell fills, the cycle is complete.  Net profit after fees
   is roughly **+0.15%** per cycle.

If an order doesn't fill within 120 seconds (configurable), it's
cancelled and the bot re-evaluates.

### Tuning the strategy

All parameters are in `.env` (see `.env.example` for descriptions):

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `SYMBOL` | `BTCUSDT` | Spot pair (no slash), e.g. `BNBUSDT`, `ETHUSDT` |
| `TRADE_QUANTITY` | `0.001` | Size per order in the **base** asset (BTC, BNB, …) |
| `POLL_INTERVAL` | `3` | Seconds between price checks |
| `MA_WINDOW` | `20` | Price samples in the moving average |
| `BUY_DIP_PCT` | `0.15` | % below MA to trigger a buy |
| `SELL_TARGET_PCT` | `0.30` | % above entry to place the sell |
| `STALE_ORDER_SEC` | `120` | Cancel unfilled orders after this |

**Trading pair:** set `SYMBOL=BNBUSDT` (or any other USDT spot pair).
`TRADE_QUANTITY` is always in the base coin. Binance still enforces
**~5 USDT minimum notional** on most USDT pairs — e.g. BNB near 650 USDT
needs at least about **0.008 BNB** per order (use **0.01** to be safe).

**Tips:**
- Smaller `BUY_DIP_PCT` = more trades but smaller edge per cycle.
- Larger `MA_WINDOW` = smoother average, slower to react.
- In trending markets (not ranging), mean reversion loses money —
  the bot buys dips that keep dipping.

### Limitations

- **Testnet liquidity is thin** — fills may be slow or at unrealistic
  prices.
- **Polling, not streaming** — the bot checks price every N seconds.
  A WebSocket upgrade would react faster.
- **No persistence** — trade history lives in memory and resets on
  restart (the log file survives though).
- **Single position** — the bot runs one cycle at a time, no overlapping
  orders.

---

## Next steps

1. **Add WebSocket streaming** — replace polling with a real-time price
   feed for faster reactions
2. **Add trade persistence** — save cycles to a JSON or SQLite file so
   history survives restarts
3. **Add multiple pairs** — run independent strategy instances on
   different symbols
4. **Add risk limits** — max daily loss, max open position value, etc.

---

## Safety reminders

- **Start on testnet.** Always.
- **Never commit `.env`** — it's in `.gitignore` for a reason.
- **Use IP restrictions** on mainnet keys when you have a fixed server.
- **Start with tiny sizes** when you move to real money.
- **Automated trading can lose money fast.** Understand what your code does
  before letting it run unsupervised.

---

## LIFO Tranche Grid (unified strategy)

The new LIFO grid is the canonical trading engine. It is **venue-free** —
one pure state machine in `api/lifo_grid.py` drives four deployments:

| Deployment          | Channel          | Venue (`api/venues/…`)  | Account     | Fee model        |
|---------------------|------------------|-------------------------|-------------|------------------|
| Binance Live        | `live`           | `binance.binance_live`  | mainnet     | BNB-subsidized   |
| Binance Paper       | `binance-demo`   | `binance.binance_testnet` | testnet   | BNB-subsidized   |
| Revolut Live        | `revolut-live`   | `revolut.revolut_live`  | production  | deducted from base |
| Revolut Paper       | `revolut-paper`  | `revolut.revolut_paper` | in-memory   | deducted (simulated) |

### Why Revolut gets its own parameter block

Revolut X has no testnet, uses `post_only` (not `LIMIT_MAKER`), caps writes
at **1 000/day**, and takes fees out of the base asset instead of offering
a BNB-style rebate. Those constraints mean:

- A 0.71% take-profit net of two 0.15% legs is marginal at best — so
  `LIFO_REVOLUT_TP_PCT` defaults to **1.20%**, `LIFO_REVOLUT_DIP_PCT` to
  **1.00%**, and `LIFO_REVOLUT_TRAIL_STEP_PCT` to **0.30%** to keep write
  activity under the daily ceiling.
- Sells use `qty * (1 - fee_rate)` (what actually landed), not the gross
  requested qty — so the engine cannot be told "sell the full bag" on
  Revolut. Handled internally by the `deducted` fee model.
- Revolut Paper is an in-memory fill simulator that uses **real RevX
  prices** (fetched from `/tickers`) so param tuning is meaningful before
  you flip `LIFO_REVOLUT_LIVE_ENABLED=true`.

### Revolut X API key scopes (important gotcha)

Revolut X keys have **two independent scopes**, and both must be enabled
for the live runner:

| Scope          | Needed for                                            |
|----------------|-------------------------------------------------------|
| **Spot view**  | `GET /tickers`, `/balances`, `/orders/active`, etc.  |
| **Spot trade** | `POST /orders`, `DELETE /orders/{id}`                |

If only "Spot view" is ticked, the bot will place orders and get back
`403 "This action is forbidden"` with the bot otherwise looking healthy
(reads succeed, prices stream, trailing logic runs). The fix is not in
code — re-open the key in [exchange.revolut.com](https://exchange.revolut.com/)
→ Profile → API keys and enable **Spot trade**. If the key is also
IP-whitelisted, make sure the server's egress IP is on the list (Railway
shows it under `outbound_ip` in `/api/health`).

A quick local diagnostic:

```bash
python revolut_x_trade.py balances   # needs Spot view  + whitelisted IP
```

If that works but placing a tiny limit buy still 403s, "Spot trade"
isn't ticked. No code change required.

### State persistence & startup recovery

Each runner writes its state machine to `state_lifo_<venue>.json` every
~5 seconds and on clean shutdown.

On boot the runner reconciles persisted state with the live exchange. For
every tracked order id that is **not** in `/openOrders` it asks the
exchange for the real status (`Venue.get_order_status`) and acts on it:

| Tracked entity | Exchange status   | Action                                                                     |
|----------------|-------------------|----------------------------------------------------------------------------|
| `resting_buy`  | FILLED / PARTIAL  | Treat as a buy fill — create the bag, place the TP sell.                   |
| `resting_buy`  | CANCELED          | Clear it; engine re-arms on next tick.                                     |
| `resting_buy`  | OPEN              | Leave it — the open-orders snapshot was just stale.                        |
| `bag.sell_*`   | FILLED / PARTIAL  | Close the bag at the TP price (LIFO replacement fires).                    |
| `bag.sell_*`   | CANCELED          | Re-place the TP for that bag — we still hold the BTC.                      |
| `bag.sell_*`   | OPEN              | Leave it.                                                                  |
| any            | UNKNOWN           | Safe default: treat missing buy as cancelled, missing sell as filled.      |

This means a fill that happened while the bot was offline is recovered
without losing bag identity (no FIFO averaging, no orphaned BTC).
Paper venues report `UNKNOWN` after restart — they fall back to the
"assume filled" heuristic, which is fine for simulations.

### Progressive scaling protocol

Capital grows without code changes — edit `.env` and restart:

#### Phase 1 — Widen the net ($60 → $400)
Keep `LIFO_BULLET_SIZE_USDT=10.0` (matches Revolut and stays safely
above Binance's $5 MIN_NOTIONAL even if a TP rounds to a single step).
Bump `LIFO_MAX_BULLETS` as new USDT lands:

| Total capital | `LIFO_MAX_BULLETS` | Drawdown survived |
|---------------|--------------------|-------------------|
| $60           | 6                  | 4.5%              |
| $100          | 10                 | 7.5%              |
| $200          | 20                 | 15%               |
| $300          | 30                 | 22.5%             |
| $400          | 40                 | 30% (hard cap)    |

#### Phase 2 — Increase cash flow (above $400)
`LIFO_MAX_BULLETS` is **locked at 40** permanently. All future capital
grows `LIFO_BULLET_SIZE_USDT`:

| Total capital | `LIFO_BULLET_SIZE_USDT` |
|---------------|--------------------------|
| $800          | $20                      |
| $2 000        | $50                      |
| $2 800        | $70                      |

State file is compatible across config bumps — the engine adapts on
restart without losing open bags.

### Env cheat-sheet

```dotenv
# Enable / disable runners individually
LIFO_ENABLED=true
LIFO_BINANCE_LIVE_ENABLED=true
LIFO_BINANCE_PAPER_ENABLED=true
LIFO_REVOLUT_LIVE_ENABLED=false
LIFO_REVOLUT_PAPER_ENABLED=true

# Binance params (live + testnet-paper)
LIFO_BULLET_SIZE_USDT=6.0
LIFO_MAX_BULLETS=10
LIFO_DIP_PCT=0.75
LIFO_TP_PCT=0.71
LIFO_TRAIL_STEP_PCT=0.15

# Paper-only overrides (A/B test smaller tweaks without touching live)
# If unset, the LIFO_* values above are used.
LIFO_PAPER_BULLET_SIZE_USDT=6.0
LIFO_PAPER_TP_PCT=0.71
LIFO_PAPER_DIP_PCT=0.75

# Revolut overrides (wider to clear deducted fees)
LIFO_REVOLUT_FEE_RATE=0.0015
LIFO_REVOLUT_BULLET_SIZE_USDT=10.0
LIFO_REVOLUT_MAX_BULLETS=6
LIFO_REVOLUT_DIP_PCT=1.0
LIFO_REVOLUT_TP_PCT=1.2
LIFO_REVOLUT_TRAIL_STEP_PCT=0.30
LIFO_REVOLUT_PAPER_STARTING_USDT=1000
```

### Binance BNB fee mandate

The live engine assumes **"Use BNB for fees"** is enabled on your Binance
account. Keep at least $2 of BNB in your spot wallet at all times — if
the BNB balance hits zero, Binance will start deducting fees from the
BTC leg instead, and the engine's `filled_qty_after_fees()` will return
the wrong number on the sell leg.
