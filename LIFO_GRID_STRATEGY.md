# LIFO Tranche Grid — Live Strategy (Binance + Revolut)

This document explains the live trading strategy that runs on both
**Binance** and **Revolut X**, the rules it follows, and which files in
the repo collaborate to make it work.

> **TL;DR** — The bot is a *single venue-free state machine*
> (`api/lifo_grid.py`). It does not know whether it is talking to
> Binance or Revolut. Each "venue" is a small adapter
> (`api/venues/binance.py`, `api/venues/revolut.py`) that translates the
> engine's intents (place buy / place sell / cancel) into REST calls
> and translates fills back into engine events. **Yes, the strategy is
> the same on both venues** — only the parameters and fee model
> differ.

---

# Plain-English version (read this first)

## Part 1 — Why the bot doesn't "run away" from the price

Imagine Bitcoin is a guy climbing up and down a ladder.
Your bot is holding a **"Catching Net"** one rung below him.

Here is the single rule that makes it work:

- **When Bitcoin climbs UP a full rung:** the bot moves the net UP to
  stay right behind him.
- **When Bitcoin steps DOWN (or just wiggles):** the bot **FREEZES**.
  It does not move the net down. It leaves the net exactly where it
  is.

> **One small precision:** the bot only bothers shuffling the net up
> *after Bitcoin has climbed at least a full rung* (about **+0.15%**
> on Binance, **+0.30%** on Revolut). Tiny wiggles don't count —
> otherwise we'd be re-tying the rope every second.
>
> And "one rung below" is a **percentage**, not a fixed dollar gap:
> **0.75% below** on Binance, **1.00% below** on Revolut. So as the
> ladder gets taller, the rung spacing in dollars grows too — but it
> always stays the same in percent.

Walk through the example (75k → 74k):

1. Bitcoin climbs to **$75,000**. The bot moves the net up to
   **~$74,437** (0.75% below).
2. Bitcoin slips and drops to **$74,800**.
3. Because Bitcoin is going **down**, the bot **FREEZES**. The net
   stays tied at $74,437.
4. Bitcoin keeps slipping, and falls through **$74,437**.
5. **Boom.** Bitcoin falls directly into the frozen net. You just
   bought the dip.

**The secret:** the bot is programmed to *close its eyes* when the
price drops. It only updates the net when the price goes UP. That's
how it forces the falling price to crash right into your trap.

## Part 2 — How the bot turns each catch into profit

Catching the dip is only half the trick. Here's what happens *the
instant* a coin lands in your net:

### Rule 1 — Every catch comes pre-sold

The moment the net catches Bitcoin, the bot **immediately puts that
exact coin up for sale at a fixed mark-up**:

- **+0.71% above your buy** on Binance
- **+1.20% above your buy** on Revolut (wider because Revolut takes
  its fee out of your Bitcoin instead of giving you a discount)

You don't have to watch the screen. You don't have to decide when to
take profit. The sell ticket is on the board the second you bought.

> **Caught at $74,437 → sell ticket auto-placed at ~$74,966
> (Binance).** When the price climbs back up and hits that ticket, you
> bank the profit and the coin leaves your wallet. Net effect: you
> cashed in a small profit per catch, after fees, in pure USDT.

### Rule 2 — One catch, then drop the next net lower

Right after a catch, the bot doesn't stop. It **drops a *new* net one
rung below the price you just bought at**, ready for the next dip.

So if Bitcoin keeps falling, you keep catching — and each catch is
cheaper than the last:

- Net 1 catches at $74,437 → sell ticket at $74,966
- Net 2 drops to $73,879 → if it catches, sell ticket at $74,403
- Net 3 drops to $73,325 → if it catches, sell ticket at $73,846
- … and so on.

Each catch is its own little trade with its own pre-set profit ticket.
They don't interfere with each other.

### Rule 3 — There's a hard limit on how many nets are out

The bot only ever has a fixed number of nets in play at once:

- **10 nets max** on Binance
- **6 nets max** on Revolut

Once that's full ("MAX_AMMO"), the bot stops dropping new nets. It
just **patiently waits for one of the existing sell tickets to get
hit**. This is the safety belt — it stops the bot from happily
catching all the way down to zero in a real crash.

### Rule 4 — When a sell hits, the winning rung gets re-armed at the *exact* same price

This is the clever bit. If your $74,437 net catches and then the sell
ticket at $74,966 fires (cha-ching), the bot doesn't average or guess.
It **drops a fresh net at exactly $74,437 again** — the same rung that
just won, ready to catch the same dip if it comes back.

Winning rungs get re-used. Nothing gets averaged or smoothed out.

### Rule 5 — If everything is flat, go back to climbing the ladder

If every net's coin has been sold and you have zero out, the bot
resets — it re-anchors at wherever Bitcoin is *right now* and goes
back to following him up the ladder, one rung behind, waiting for the
next dip.

## The whole thing in one sentence

> The bot **trails Bitcoin up the ladder with a net one rung below**,
> **freezes the net every time he slips**, **automatically puts every
> catch up for sale at a small mark-up**, **drops a new net one rung
> lower after each catch**, **caps how many nets it'll deploy**, and
> **re-uses winning rungs at exactly the same price** — over and over,
> day after day.

---

# Technical version

## 1. What the strategy does, in one paragraph

The bot watches BTC price. When it has nothing on the book it sits in
**HUNTING** mode and trails the market with a *single resting limit
buy* placed `dip_pct` below an internal `anchor`. The anchor only
climbs (never drifts down on its own); each time the running high
advances by `trail_step_pct`, the resting buy is cancelled and
re-placed lower, so the bot is always one dip away from buying. When a
buy fills, the bot creates a **bag** (a tracked tranche), brackets it
with a limit *take-profit sell* at `+tp_pct` above the entry, and (if
it still has ammo) places the next grid buy `dip_pct` below the just-
filled price. When a TP sell fills, the bot removes that *exact* bag
(LIFO, by `bag_id`) and replaces the resting buy at *exactly* that
sold bag's entry price — so the next buy slot reuses the same rung
that just won. If all bags are flat, it returns to HUNTING anchored at
the sell fill price.

It is a **dip-buy / take-profit grid**, not a martingale. Position
size per buy is fixed (`bullet_size_usdt`), bag count is hard-capped
(`max_bullets`), and every buy has a matching sell sitting on the book
the moment it fills.

---

## 2. The state machine (engine bytes — identical on every venue)

Source: `api/lifo_grid.py` → class `LifoGridState`.

### States

| State      | Condition                              | Behaviour                                                                 |
|------------|----------------------------------------|---------------------------------------------------------------------------|
| `HUNTING`  | 0 bags held                            | Maintain ONE resting BUY at `anchor * (1 - dip_pct)`. Trail anchor up.    |
| `ACTIVE`   | 1 ≤ bags < `max_bullets`               | TP sell on every bag. ONE resting BUY at last fill `* (1 - dip_pct)`.     |
| `MAX_AMMO` | bags == `max_bullets`                  | TP sells only — new buys disabled until a TP fills.                       |

### Transitions

#### HUNTING → ACTIVE — `on_buy_filled(order_id, fill_price, filled_qty)`

1. Stop trailing.
2. Create a `Bag(bag_id=N, buy_fill_price, btc_amount, sell_target_price=fill * (1 + tp_pct))`.
3. Emit `PlaceSellIntent` for that bag at the TP price.
4. If `len(bags) < max_bullets`, also emit a `PlaceBuyIntent` at
   `fill_price * (1 - dip_pct)` (tag `NEXT_GRID`).

#### ACTIVE → ACTIVE / HUNTING — `on_sell_filled(order_id, fill_price)`

1. Find the bag whose `sell_order_id == order_id` and remove it.
   (LIFO is enforced by *identity* — we close the bag whose specific
   TP filled, not the oldest one.)
2. Realise P&L: `(fill_price - bag.buy_fill_price) * bag.btc_amount`.
3. **If bags remain**: cancel the current resting buy (if any) and
   place a new BUY at *exactly* the sold bag's `buy_fill_price`
   (tag `LIFO_REPLACE`). This is the "exact rung replacement"
   property of the grid.
4. **If no bags remain**: re-anchor at `fill_price`, return to
   HUNTING with a fresh BUY at `fill_price * (1 - dip_pct)`.

#### Trailing tick — `tick_trailing(price)` (HUNTING only)

* Track `internal_high_record` in RAM (no API call).
* If `internal_high_record >= anchor * (1 + trail_step_pct)`:
  emit `CancelIntent(resting_buy)` + `PlaceBuyIntent` at
  `new_high * (1 - dip_pct)` (tag `TRAIL_REPRICE`), then advance
  the anchor.
* **Trail-DOWN safety re-anchor** — if we wake up HUNTING with no
  resting buy and spot is already at-or-below the dip target (e.g.
  post-redeploy), re-seat the anchor at current spot so the new
  target lands safely below the bid. Otherwise post-only orders
  would cross the spread and bounce back forever.

### Invariants

* **0 or 1 resting buys** at any moment.
* **Every bag has a `sell_order_id`** once placement succeeds (the
  reconciliation loop re-places it if missing).
* **`anchor_price` is climb-only** — only resets on HUNTING re-entry.

### Inputs the engine emits (Intents)

The engine never calls the network. It returns one or more of:

* `PlaceBuyIntent(price, bullet_size_usdt, tag)`
* `PlaceSellIntent(bag_id, price, qty)`
* `CancelIntent(order_id, reason)`

The runner (next section) is what turns those into REST calls.

---

## 3. Parameters

Defaults live in `LifoGridParams` (`api/lifo_grid.py`); per-deployment
values come from `config.py` driven by `.env`.

| Parameter            | Meaning                                     | Binance default | Revolut default |
|----------------------|---------------------------------------------|-----------------|-----------------|
| `bullet_size_usdt`   | Cash spent per BUY                          | `6.0` (USDT)    | `10.0` (USDC)   |
| `max_bullets`        | Max concurrent bags                         | `10`            | `6`             |
| `dip_pct`            | How far below anchor the BUY sits           | `0.75 %`        | `1.00 %`        |
| `tp_pct`             | TP distance above each fill                 | `0.71 %`        | `1.20 %`        |
| `trail_step_pct`     | High-advance needed before reprice          | `0.15 %`        | `0.30 %`        |
| `min_notional`       | Minimum order value (venue-enforced)        | `5.0` USDT      | `5.0` USDC      |
| Fee model            |                                             | `bnb_subsidized` | `deducted` (0.15 %) |

**Why Revolut runs wider than Binance:**

* Revolut X has no BNB-style fee rebate; the **fee comes out of the
  base asset** (you pay 0.15 % of BTC on each leg). A 0.71 % TP net of
  two 0.15 % legs is marginal — `tp_pct` is widened to **1.20 %** so
  every cycle stays clearly positive after fees.
* Revolut X caps writes at **1 000 / day**. Wider `dip_pct` and
  `trail_step_pct` reduce reprice traffic so the bot stays well under
  the ceiling.
* Revolut sells use `qty * (1 - fee_rate)` (the qty that actually
  landed) — so the engine never tries to "sell the full bag" gross.
  That subtraction lives in `apply_fee_model()` and `filled_qty_after_fees()`.

---

## 4. The runner — engine ↔ exchange glue

Source: `api/runners/lifo_runner.py` → class `LifoRunner`.

One `LifoRunner` per deployment (Binance live, Binance paper, Revolut
live, Revolut paper). Same code path for all four — only the `Venue`
adapter and the `LifoGridParams` differ.

### What it does on a tick (`run()` → loop)

1. Ask the venue for the current price.
2. Ask the venue for the set of open order IDs.
3. **Detect fills** (`_detect_fills`):
   * If `state.resting_buy.order_id` disappeared → call `state.on_buy_filled(...)`.
   * If any `bag.sell_order_id` disappeared → call `state.on_sell_filled(...)`.
4. Hand the price to `state.tick_trailing(price)` to compute trailing
   intents.
5. **Apply intents** (`_apply_intents`):
   * Cancels first → then BUYs → then SELLs.
   * Each buy goes through `_place_buy()` which floors qty to venue
     precision, checks `min_notional`, and respects a **failure
     backoff** (30 s default, 120 s for `-2010 insufficient balance`,
     300 s for HTTP `403 / forbidden`).
6. Build a WebSocket snapshot and broadcast it to the dashboard.
7. Persist state every ~5 s (`_maybe_persist`).
8. Emit a "thoughts" heartbeat into the live log every ~3 s
   (`_compose_thoughts`) so the dashboard always shows what the bot
   is reasoning about.

### Boot sequence

1. `_load_state()` — load `state_lifo_<venue>.json` if present.
2. `_fetch_price_with_retry()` — wait for a non-zero price.
3. **If resumed**: `_reconcile_with_exchange()` — for every tracked
   order ID that isn't in `/openOrders`, ask the venue for its real
   status:

| Tracked entity | Exchange status   | Action                                                                 |
|----------------|-------------------|------------------------------------------------------------------------|
| `resting_buy`  | FILLED / PARTIAL  | Treat as a buy fill — create the bag, place the TP sell.               |
| `resting_buy`  | CANCELED          | Clear it; engine re-arms on next tick.                                 |
| `resting_buy`  | OPEN              | Leave it.                                                              |
| `bag.sell_*`   | FILLED / PARTIAL  | Close the bag at the TP price.                                         |
| `bag.sell_*`   | CANCELED          | Re-place the TP for that bag — we still hold the BTC.                  |
| `bag.sell_*`   | OPEN              | Leave it.                                                              |
| any            | UNKNOWN           | Safe default: missing buy → cancelled, missing sell → assume filled.   |

   This means a fill that happened while the bot was offline is
   recovered without losing bag identity (no FIFO averaging, no
   orphaned BTC). It also tries to **adopt** existing open SELLs
   that match a bag (within 1 % qty / 5 % price tolerance) instead
   of placing a duplicate that would 422 with "insufficient balance".

4. **If fresh start**: capture starting equity, call `state.on_startup(price)`.

5. `_sweep_orphan_btc_if_any()` — cancel orphan orders, then MARKET
   sell any base asset in the wallet that isn't accounted for by a
   tracked bag (e.g. state-loss across redeploys, manual deposits,
   fee dust). Only runs on Binance — Revolut does not yet expose
   `place_market_sell`.

6. Send the `🧱 LIFO Grid started` Telegram with wallet, P&L, and open
   orders block.

7. Enter the main tick loop.

### Manual market buy (dashboard "Buy Now" button)

`force_market_buy(amount_usdt)` places a MARKET BUY through the venue,
then **threads the fill through the engine identically to an organic
buy** — a new bag is opened, its TP is placed, and (if room) the next
grid buy is queued. Honours `MAX_AMMO`, the failure backoff, and the
runner's `_lock` so it cannot race the polling loop.

---

## 5. Venues — the per-exchange adapter

All venues implement the same `Venue` protocol (`api/venues/__init__.py`):

```python
class Venue(Protocol):
    spec: VenueSpec                                  # name, symbol, fees, precision
    def get_price() -> float: ...
    def get_balances() -> dict[str, float]: ...
    def get_open_order_ids() -> set[str]: ...
    def get_open_orders_detail() -> list[dict]: ...
    def place_limit_buy(price, qty) -> PlacedOrder: ...
    def place_limit_sell(price, qty) -> PlacedOrder: ...
    def place_market_buy(quote_amount) -> PlacedOrder: ...
    def cancel(order_id) -> None: ...
    def get_order_status(order_id) -> tuple[OrderStatus, float]: ...
    def filled_qty_after_fees(requested_qty) -> float: ...
    def is_ready() -> bool: ...
```

### Binance adapter — `api/venues/binance.py`

* `place_limit_buy/sell` → `trading.place_maker_order` (LIMIT_MAKER).
* `place_market_buy` → uses `quoteOrderQty` so a $6–$10 order works
  even though BTC is expensive.
* `place_market_sell` → used only by the orphan-BTC sweep at boot.
* `filled_qty_after_fees` returns the requested qty as-is (BNB pays
  the fee on Binance live; testnet is fee-free).
* Reads precision from `/exchangeInfo` filters (`PRICE_FILTER`,
  `LOT_SIZE`, `NOTIONAL`).
* **Mainnet vs testnet** is encoded in `BinanceContext`
  (`api/exchange_context.py`) — different base URL + keys, same code.

### Revolut adapter — `api/venues/revolut.py`

* All limit orders are sent with `execution_instructions: ["post_only"]`
  (Revolut's equivalent of LIMIT_MAKER).
* `filled_qty_after_fees` returns `requested_qty * (1 - 0.0015)`,
  floored to qty precision — fees come out of the BTC, not USDC.
* `place_market_buy` sizes against current spot with a 0.5 % headroom
  haircut, since Revolut wants `base_size`, not quote amount.
* No `place_market_sell` → the orphan-BTC sweep is a no-op on Revolut.
* `RevolutPaperVenue` is an in-memory simulator that uses **real RevX
  prices** from `/tickers` — fills happen when the price range between
  ticks crosses an order's price.

---

## 6. Where everything lives — file map

```
api/
├── lifo_grid.py              ← The strategy itself.
│                                Pure state machine, zero I/O.
│                                Functions called: tick_trailing,
│                                on_buy_filled, on_sell_filled.
│
├── runners/
│   ├── lifo_runner.py        ← The runner. Glues engine + venue +
│   │                            persistence + WebSocket together.
│   │                            One LifoRunner instance per
│   │                            deployment. Drives the tick loop.
│   │
│   └── lifo_launcher.py      ← Spawns four asyncio tasks (one per
│                                runner) at FastAPI startup, gated by
│                                LIFO_*_ENABLED env vars.
│
├── venues/
│   ├── __init__.py           ← Venue protocol + VenueSpec dataclass +
│   │                            apply_fee_model() helper.
│   ├── binance.py            ← BinanceVenue (live + testnet).
│   └── revolut.py            ← RevolutLiveVenue + RevolutPaperVenue.
│
├── lifo_state_store.py       ← Atomic JSON persistence
│                                (state_lifo_<venue>.json).
│
├── exchange_context.py       ← BinanceContext: base URL + keys for
│                                mainnet vs testnet.
│
├── ws_manager.py             ← Per-channel WebSocket broadcaster
│                                (live, binance_demo, revolut_live,
│                                 revolut_paper).
│
├── notifications.py          ← Telegram fan-out for fills, starts,
│                                stops, sweeps.
│
├── log_buffer.py             ← In-memory log ring buffer; tags every
│                                line with the runner channel so the
│                                dashboard can filter per venue.
│
└── main.py                   ← FastAPI lifespan that calls
                                 lifo_launcher.spawn_all().

# Top-level support modules (used by the Binance venue)
trading.py                    ← Binance signed REST calls
                                (place_maker_order, get_open_orders,
                                 cancel_order, get_order, get_account,
                                 place_market_quote_order, place_market_order).
market_data.py                ← Binance public REST calls
                                (get_price, get_orderbook, get_exchange_info).
auth.py                       ← HMAC-SHA256 request signing.
config.py                     ← Loads .env, exposes all LIFO_* params,
                                 plus runners' enable flags + poll
                                 intervals + Revolut overrides.

# Top-level Revolut helper (used by the Revolut venue)
revolut_x.py / revolut_x_trade.py
                              ← `revx_request()` — JWT-signed Revolut X
                                 REST client (GET /tickers, /balances,
                                 /orders/active, POST /orders, …).
```

---

## 7. Interaction diagram (one tick, one venue)

```
        ┌────────────────────────────────────────────┐
        │           FastAPI lifespan                 │
        │  api/main.py → lifo_launcher.spawn_all()   │
        └──────────────────────┬─────────────────────┘
                               │ asyncio.create_task per venue
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                  LifoRunner.run()  (one per venue)              │
│  api/runners/lifo_runner.py                                     │
│                                                                 │
│   loop every poll_interval seconds:                             │
│                                                                 │
│     ┌───────────────────────────────────────────────────┐       │
│     │ 1. price       = venue.get_price()                │       │
│     │ 2. open_ids    = venue.get_open_order_ids()       │──┐    │
│     │ 3. _detect_fills(open_ids):                       │  │    │
│     │       state.on_buy_filled / on_sell_filled        │  │    │
│     │ 4. intents     = state.tick_trailing(price)       │  │    │
│     │ 5. _apply_intents(intents):                       │  │    │
│     │       venue.cancel / place_limit_buy / sell       │──┤    │
│     │ 6. ws_manager.broadcast(snapshot)                 │  │    │
│     │ 7. lifo_state_store.save(snapshot)  (every 5 s)   │  │    │
│     │ 8. notifications.send(...)  (on fills, live only) │  │    │
│     └───────────────────────────────────────────────────┘  │    │
│                                                            │    │
└────────────────────────────────────────────────────────────┼────┘
                                                             │
                                ┌────────────────────────────┘
                                ▼
                   ┌─────────────────────────────┐
                   │   Venue (per exchange)      │
                   │  api/venues/binance.py  OR  │
                   │  api/venues/revolut.py      │
                   │                             │
                   │  Binance → trading.py +     │
                   │            market_data.py + │
                   │            auth.py          │
                   │  Revolut → revolut_x.py     │
                   └─────────────┬───────────────┘
                                 │ HTTPS
                                 ▼
                          ┌──────────────┐
                          │  Exchange    │
                          │  REST API    │
                          └──────────────┘
```

The engine state on the left (`state.tick_trailing` etc.) is the
**same code** for every venue. The only thing that changes between
Binance and Revolut is the `Venue` adapter on the right and the
parameter block (`tp_pct`, `dip_pct`, `bullet_size_usdt`, ...) the
runner was constructed with.

---

## 8. Persistence

* Each runner writes its full state to
  `state_lifo_<venue>.json` every ~5 s and on clean shutdown.
* Atomic writes (`tempfile + os.replace`) so a crash mid-save can
  never corrupt the file.
* On boot, persisted state is reconciled against the live exchange
  (`_reconcile_with_exchange`), so fills that happened while the bot
  was offline are recovered without losing bag identity. This is what
  guarantees that a Railway redeploy in the middle of a cycle doesn't
  lose money or duplicate orders.

---

## 9. Quick mental model

> The engine is a **vending machine for tranches**. At any time it
> has at most 1 BUY on the book (the next dip), and one SELL per bag
> (the matching profit-take). Buys feed bags; sells consume them.
> Bags are LIFO by identity — when a TP fills, the BUY rung is
> re-armed at *exactly* that bag's entry price, not averaged. The
> engine doesn't know what exchange it's on; the venue adapter
> translates the same intents into Binance LIMIT_MAKER orders or
> Revolut post-only orders, with the only material difference being
> the fee model (BNB-subsidised vs deducted from base) and the wider
> Revolut parameters that compensate for that.
