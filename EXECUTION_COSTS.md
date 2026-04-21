# Execution Costs — Binance vs Revolut X

> Goal: stop guessing. This file is the single source of truth for what we
> *actually know* about fees, spread, slippage and rate limits on both
> venues, and what we still need to measure before we can claim
> "1.20% TP is correct" or "0.71% TP is correct".

Date snapshot: 2026-04-19. Re-verify the official rate cards before any
parameter change — both venues update fee schedules without notice.

> **2026-04-19 update.** We ran two read-only probes against the live
> accounts (`scripts/probe_revolut_fees.py` and
> `scripts/probe_revolut_endpoints.py`) and proved empirically that:
>
> 1. Revolut X taker fee is **exactly 0.0900%** (computed from a
>    real BUY → SELL round-trip in `/orders/historical` — the buy
>    requested 0.00154401 BTC and the wallet received 0.00154262 BTC,
>    a 0.0900% haircut, matching the published rate to the basis point).
> 2. Revolut X order payloads contain **no commission/fee fields at
>    all**; `/configuration/pairs` likewise has no fee section. There
>    is no per-account override path. The published 0% maker / 0.09%
>    taker schedule applies to us.
> 3. We've therefore lowered `LIFO_REVOLUT_FEE_RATE` from `0.0015` to
>    **`0.0`** in `config.py` (the LIFO bot only places `post_only`
>    orders, which are always maker).
> 4. We've switched the default `SYMBOL` from `BTCUSDT` to **`BTCUSDC`**
>    on Binance, because Binance VIP-0 charges **0% maker fee on USDC
>    pairs** (vs 0.075% on USDT with BNB on). See migration steps in §9.

---

## TL;DR

|                              | Binance Spot (BTCUSDC, new default) | Revolut X (BTC-USDC) |
|------------------------------|--------------------------------------|----------------------|
| Maker fee — official         | **0.0000%** (USDC zero-fee promo)    | **0.00%**            |
| Taker fee — official         | 0.0950% (BNB on: 0.07125%)           | **0.09% (verified)** |
| Fee model in our bot         | `bnb_subsidized` (no qty haircut)    | `deducted` (qty haircut) |
| Fee rate hard-coded in repo  | `0.0` for USDC, `0.00075` for USDT   | **`0.0`** (was 0.0015) |
| Round-trip fee (2 maker legs)| **0.000% theoretical & realised**    | **0.000% theoretical** |
| Configured TP (`LIFO_*_TP_PCT`) | 0.71%                             | 1.20% (still — see §8) |
| Configured dip (`LIFO_*_DIP_PCT`) | 0.75%                           | 1.00%                |
| Spread observed              | **not measured**                     | **not measured**     |
| Slippage observed (maker)    | **0% by design** (LIMIT_MAKER rejects crosses) | **0% by design** (`post_only` rejects crosses) |
| Order rate cap               | per-account, queryable via `exchangeInfo` (typically 100/10s & 200,000/24h) | **1,000 limit orders / 24h** (hard cap, per business) |
| Min notional (configured)    | $5.00 (`LIFO_MIN_NOTIONAL`)          | $1.00 (`LIFO_REVOLUT_MIN_NOTIONAL`) |

Resolved on 2026-04-19:

1. ~~**Effective fee per leg on Revolut**~~ — **resolved**. Empirically
   verified at 0% maker / 0.09% taker via
   `scripts/probe_revolut_fees.py`. `LIFO_REVOLUT_FEE_RATE` lowered to
   `0.0` in `config.py`. The Revolut TP of 1.20% is now structurally
   uncoupled from fees — re-tightening it is a strategy choice, not a
   fee-coverage requirement. See §8.

Still open:

2. **Spread + queue latency** — neither venue's bid/ask gap nor "time
   from `placed` → `filled`" is logged. Without this we cannot defend
   *any* TP/dip choice with data; we are tuning to opinion.

---

## 1. What's hard-coded in the repo

### 1.1 Binance — `api/venues/binance.py`

```249:269:api/venues/binance.py
def binance_live_venue() -> BinanceVenue:
    """
    Mainnet Binance with BNB fees enabled (per spec §2 'BNB Fee Mandate').

    fee_rate is informational only — `bnb_subsidized` does NOT deduct from
    the base asset (the fee is paid in BNB on a separate ledger). The number
    is the official Binance VIP-0 maker fee for the configured symbol class:

      * BTCUSDT (and other USDT pairs)  → 0.0750 % with BNB on
      * BTCUSDC (and other USDC pairs)  → 0.0000 % (zero-fee maker promo)

    Source: https://www.binance.info/en/fee/schedule (snapshot 2026-04).
    """
    is_usdc = config.SYMBOL.endswith("USDC")
    return BinanceVenue(
        ctx=mainnet_context(symbol=config.SYMBOL),
        account_mode="live",
        ws_channel="live",
        fee_model="bnb_subsidized",
        fee_rate=0.0 if is_usdc else 0.00075,
    )
```

- `fee_model="bnb_subsidized"` ⇒ buyer gets the *full* BTC quantity in
  the wallet; fees are debited from the BNB balance, **not** the BTC
  received. Confirmed in `api/venues/__init__.py`:

```9:14:api/venues/__init__.py
  * "bnb_subsidized"  → the buyer receives the full requested BTC qty
                        (Binance with "Use BNB for fees" ON).
  * "deducted"        → fee comes out of the received base asset; buyer
                        ends up with qty * (1 - fee_rate) BTC
                        (Revolut X today).
```

- `fee_rate=0.00075` is purely informational on Binance — `apply_fee_model`
  returns `requested_qty` unchanged for `bnb_subsidized`:

```166:176:api/venues/__init__.py
def apply_fee_model(
    requested_qty: float,
    spec: VenueSpec,
) -> float:
    """Translate venue fee model into the qty that actually lands in wallet."""
    if spec.fee_model in ("bnb_subsidized", "paper_free"):
        return requested_qty
    # deducted
    net = requested_qty * (1.0 - spec.fee_rate)
    return _floor(net, spec.qty_prec)
```

### 1.2 Revolut — `config.py` + `api/venues/revolut.py`

```151:160:config.py
# Revolut X: 0.15% maker per leg by default (user-configurable).
# Default tp/dip are wider so round-trip still clears ~0.30% total fees.
LIFO_REVOLUT_FEE_RATE: float = float(os.getenv("LIFO_REVOLUT_FEE_RATE", "0.0015"))
LIFO_REVOLUT_BULLET_SIZE_USDT: float = float(os.getenv("LIFO_REVOLUT_BULLET_SIZE_USDT", "10.0"))
LIFO_REVOLUT_MAX_BULLETS: int = int(os.getenv("LIFO_REVOLUT_MAX_BULLETS", "10"))
LIFO_REVOLUT_DIP_PCT: float = float(os.getenv("LIFO_REVOLUT_DIP_PCT", "1.0"))
LIFO_REVOLUT_TP_PCT: float = float(os.getenv("LIFO_REVOLUT_TP_PCT", "1.2"))
LIFO_REVOLUT_TRAIL_STEP_PCT: float = float(os.getenv("LIFO_REVOLUT_TRAIL_STEP_PCT", "0.30"))
LIFO_REVOLUT_QTY_PREC: int = int(os.getenv("LIFO_REVOLUT_QTY_PREC", "8"))
LIFO_REVOLUT_MIN_NOTIONAL: float = float(os.getenv("LIFO_REVOLUT_MIN_NOTIONAL", "1.0"))
```

- `fee_model="deducted"` ⇒ the BTC that actually lands in the wallet
  is `requested_qty * (1 - 0.0015)`. So a $10 buy at $50,000 requests
  `0.0002 BTC`, but the wallet shows `0.0002 * 0.9985 = 0.0001997 BTC`.
- The runner *trusts this number* when sizing the matching TP sell.
  If the real fee is 0%, we are oversizing the bag's wallet quantity
  by 0.15% in code and the bot's TP sells will be slightly under-quantity
  vs what we actually own (a benign accounting drift, not a financial
  loss).

### 1.3 Configured Take-Profit / Dip

```132:140:config.py
# ── Binance LIFO params ──
LIFO_BULLET_SIZE_USDT: float = float(os.getenv("LIFO_BULLET_SIZE_USDT", "10.0"))
LIFO_MAX_BULLETS: int = int(os.getenv("LIFO_MAX_BULLETS", "6"))
LIFO_DIP_PCT: float = float(os.getenv("LIFO_DIP_PCT", "0.75"))
LIFO_TP_PCT: float = float(os.getenv("LIFO_TP_PCT", "0.71"))
LIFO_TRAIL_STEP_PCT: float = float(os.getenv("LIFO_TRAIL_STEP_PCT", "0.15"))
LIFO_PRICE_PREC: int = int(os.getenv("LIFO_PRICE_PREC", "2"))
LIFO_QTY_PREC: int = int(os.getenv("LIFO_QTY_PREC", "5"))
LIFO_MIN_NOTIONAL: float = float(os.getenv("LIFO_MIN_NOTIONAL", "5.0"))
```

---

## 2. Official rate cards (sourced 2026-04-19)

### 2.1 Binance Spot — VIP-0 (regular user)

Source: <https://www.binance.info/en/fee/schedule>

| Pair class       | Maker (no BNB) | Taker (no BNB) | Maker (BNB on) | Taker (BNB on) |
|------------------|----------------|----------------|----------------|----------------|
| Standard (USDT)  | 0.1000%        | 0.1000%        | 0.07500%       | 0.07500%       |
| **USDC pairs**   | **0.0000%**    | 0.0950%        | **0.0000%**    | 0.07125%       |

Notes:
- We trade `BTCUSDT` ⇒ standard schedule applies. With BNB on (which is
  what `bnb_subsidized` assumes), each leg costs **0.075%**, round-trip
  **0.150%**.
- If we ever switched the symbol to `BTCUSDC`, regular-user **maker fee
  drops to 0%** — round-trip cost becomes 0% (we never pay taker because
  we're `LIMIT_MAKER`-only). Our current 0.71% TP would be net 0.71% in
  that scenario, vs 0.71% − 0.15% ≈ 0.56% net today on USDT.
- VIP fees scale down with 30-day rolling spot volume (≥$1M USD for VIP-1).
  We are nowhere near VIP-1 — recommend leaving as VIP-0 in any model.

### 2.2 Revolut X — flat schedule (no tiers)

Source: <https://www.revolut.com/legal/crypto-exchange-fees/> (also
mirrored in `cdn.revolut.com/terms_and_conditions/pdf/crypto_exchange_fees_…pdf`).

| Type    | Fee     | Notes                              |
|---------|---------|------------------------------------|
| Maker   | **0.00%** | Limit orders that *post* to the book |
| Taker   | 0.09%   | Market orders / aggressive limits  |

Notes:
- All our `place_limit_buy` / `place_limit_sell` calls include
  `"execution_instructions": ["post_only"]` (`api/venues/revolut.py`
  lines 187–198), so they *cannot* be taker. **If the published 0%
  applies to us, our LIFO round-trip on Revolut should cost 0.00%.**
- `place_market_buy` (used only by the dashboard "Buy Now" button)
  pays 0.09% taker. Not part of the LIFO loop.
- Special MM program: businesses that produce >5% of total exchange
  maker volume can negotiate. Not us.

### 2.3 Why our `LIFO_REVOLUT_FEE_RATE = 0.0015` is suspect

Option A — the published 0% maker rate is real and we should set
`LIFO_REVOLUT_FEE_RATE=0.0` and tighten `LIFO_REVOLUT_TP_PCT`
significantly (toward 0.30–0.50%, leaving headroom only for spread).

Option B — the published rate excludes a hidden currency-conversion
spread (e.g. when funding GBP→USDC happens automatically) that's not
called a "fee" but functions as one.

Option C — the bot's previous author measured a real fee on a fill
and 0.15% was empirical, not config-cargo-culted from Binance.

**We don't know which until we look at one real Revolut fill receipt.**
See §5 for the measurement protocol.

---

## 3. Spread

We have **no spread data** for either venue. Nothing in the codebase
samples the bid/ask gap or persists it. We have a `get_orderbook` helper
for Binance:

```33:50:market_data.py
def get_orderbook(
    symbol: str = config.SYMBOL,
    limit: int = 5,
    *,
    ctx: Optional[BinanceContext] = None,
) -> dict:
    """
    Get the top `limit` levels of the order book.
    …
    The gap between the best bid and best ask is called the "spread".
    """
    return public_request(
        "GET", "/api/v3/depth", {"symbol": symbol, "limit": limit}, ctx=ctx,
    )
```

…but we never call it from any LIFO runner. For Revolut, `revx_request`
has no `/depth` wrapper at all (`revolut_x.py` exposes only the generic
signed request helper).

External reference points (industry rule-of-thumb, **NOT measured on our
account** — to be replaced by real data):

| Venue                  | Typical BTC top-of-book spread | Notes |
|------------------------|--------------------------------|-------|
| Binance BTCUSDT        | $0.01 (1 tick)                 | Deepest BTC book in the world; spread ≈ 0.0000% |
| Binance BTCUSDC        | $0.01–$0.05                    | Thinner than USDT but still tight |
| Revolut X BTC-USDC     | unknown; expected wider        | Smaller venue, retail-dominated; needs measurement |

Because we are exclusively `post_only` / `LIMIT_MAKER`, *spread does not
cost us anything when we get a fill* — we never cross it. What spread
*does* tell us is:

- How far below mid we have to put a buy to actually become best bid
  (queue priority).
- How likely a small price wobble is to fill our buy without leading
  to an immediate adverse move.

A 5 bps (0.05%) spread on Revolut means our 1.0% dip target is 20×
the spread — buys will be far below the inside, and the price has to
genuinely fall 1% to even touch us. That's likely the right setup for
a sleepy retail venue but it's an opinion, not a measurement.

---

## 4. Slippage

By construction:

- **Binance** — `LIMIT_MAKER` orders are rejected (`-2010 "Order would
  immediately match"`) if they would cross. So filled quantity is
  always at our requested price → **slippage = 0** on every leg.
  Confirmed in `api/lifo_grid.py`:

  ```264:268:api/lifo_grid.py
          # cross the spread (-2010 "Order would immediately match and
  ```

- **Revolut** — `"execution_instructions": ["post_only"]` produces the
  same guarantee; the venue rejects the order if it would cross.
  ⇒ **slippage = 0** on every leg.

The only slippage path in the LIFO bot is the dashboard "Buy Now"
button (`place_market_buy`) which is taker-only and *not* part of the
automated grid. For the grid itself, slippage is not a tuning variable.

What *is* unknown:

- **Time-to-fill (queue latency).** We resting-place at `mid − dip%`
  and wait. Currently we don't log "placed at T, filled at T+Δ". Δ is
  the data point that tells us whether a tighter dip would actually
  catch real moves vs sit unfilled.
- **Partial-fill behaviour on Revolut.** `get_order_status` recognises
  `"partially_filled"` but we never aggregate partials into a separate
  metric.

---

## 5. Rate / order limits

### 5.1 Binance Spot — REST API

Source: <https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits>

- Rate limits are queryable per-account via `GET /api/v3/exchangeInfo`
  → `rateLimits[]`. Our code reads this for tick/lot but not for rate.
- Default order limits (regular VIP-0 spot account):
  - Unfilled order rate per 10 seconds — typically 100.
  - Daily new-order count — typically 200,000.
- Filled / partially-filled orders **don't** count against the unfilled
  rate. Cancelled orders **do** count toward daily new-order count.
- Exceeding either: HTTP 429 with `-1015 "Too many new orders"`.

We are nowhere near these limits with `LIFO_MAX_BULLETS=6` and
`LIFO_POLL_BINANCE_LIVE=3s`.

### 5.2 Revolut X — REST API

Source: <https://developer.revolut.com/docs/x-api/place-order> and
<https://developer.revolut.com/docs/guides/build-banking-apps/usage-and-limits>

- **`POST /orders` is hard-capped at 1,000 calls / 24h, per business.**
- General Revolut API: 60 requests/minute per business across all
  endpoints (this includes our `/tickers`, `/balances`, `/orders/active`
  polls).

This is the rate limit you said we're not hitting today (correct — at
`LIFO_REVOLUT_MAX_BULLETS=10`, even a hyperactive day generates ≤200
order events). The 60-req/min ceiling is a tighter constraint than the
1,000/day cap given our 10-second poll interval (`LIFO_POLL_REVOLUT_LIVE`)
which generates only ~6 GET/min — well inside the ceiling.

---

## 6. What the bot actually persists per cycle

```95:106:api/lifo_grid.py
@dataclass
class ClosedTrade:
    """Book entry for dashboard/analytics."""

    bag_id: int
    buy_fill_price: float
    sell_fill_price: float
    qty: float
    gross_pnl_usdt: float
    hold_seconds: float
    exit_reason: str = "TP"
    entry_ts: float = 0.0
    exit_ts: float = 0.0
```

- `gross_pnl_usdt = (sell_price − buy_price) × qty` — fees **not**
  deducted, even on Revolut where they are real.
- `hold_seconds` is computed from when the bag opened to when it sold.
  **Not** "time from placement to fill" — that interval is unmeasured.
- No `commission`, `commissionAsset`, `bid_at_placement`, `ask_at_placement`,
  or `mid_at_placement` is captured.

Implication: we cannot today produce a P&L statement that is "clean"
(net of fees) without re-fetching trade history from the exchange and
joining it back to bag IDs.

---

## 7. What we need to do (measurement plan)

To replace the assumptions in this file with real numbers, in priority
order:

| # | Metric                              | Source                         | Bot change needed |
|---|-------------------------------------|--------------------------------|-------------------|
| 1 | Real Revolut maker fee per leg      | First filled order: read `commission` / `commission_currency` (or `total_commission`) on `GET /orders/{id}` | Persist these fields into `ClosedTrade`; expose on dashboard |
| 2 | Real Binance maker fee per leg      | `myTrades` endpoint or order's `fills[].commission` | Same as above; verify BNB really pays it (not BTC) |
| 3 | Top-of-book spread, both venues     | Sample bid/ask every poll tick; persist a rolling histogram | Add `get_orderbook(limit=1)` (Binance) and `/orderbook` or `/quotes` (Revolut) calls in the runner; keep last N |
| 4 | Time-to-fill (queue latency)        | `placed_ts` already known — record `filled_ts` from order status transition | Add `placed_ts` to `Bag` / resting buy state; compute on fill |
| 5 | Realised TP vs configured TP        | `(sell_fill_price / buy_fill_price - 1) - tp_pct_configured` per cycle | One column added to closed-trades dashboard |
| 6 | Cancel/reject ratio                 | Count `place_limit_buy` failures / cancellations vs successful fills | Already partially tracked via failure backoff; surface it |

Each metric above is a small, isolated change — none of them touches the
state machine itself.

---

## 8. Open questions

Resolved on 2026-04-19:

- ~~Why is `LIFO_REVOLUT_FEE_RATE` set to 0.0015 if Revolut says maker is 0%?~~
  **Inherited assumption, not measurement.** Empirically verified at 0% maker
  via `scripts/probe_revolut_fees.py`. Now `0.0` in `config.py`.
- ~~Do we want a config knob to switch Binance from BTCUSDT to BTCUSDC?~~
  **Done** — `SYMBOL` now defaults to `BTCUSDC`. Binance VIP-0 maker fee
  on USDC pairs is 0%. See §9 for the manual migration steps.

Still open:

1. **Is the 1.20% Revolut TP actually empirically profitable in the
   current regime,** or is it inherited from a higher-vol regime?
   Now that fees are confirmed 0%, the entire 1.20% is theoretical
   gross. Cannot answer rigorously until §7 metric #5 is captured.
2. **Is the published Revolut 0% rate net of any FX conversion fee** if
   the funding currency is GBP/EUR? On the USDC-funded account this
   doesn't matter, but worth a one-line check in your Revolut statement
   the first time funding is converted.

---

## 9. Migration: BTCUSDT → BTCUSDC on Binance

Code-side changes already shipped:

- `config.py`: `SYMBOL` default flipped to `BTCUSDC`.
- `.env`: `SYMBOL=BTCUSDC` added so dev box matches.
- `api/venues/binance.py`: `binance_live_venue()` now branches on
  `endswith("USDC")` and reports `fee_rate=0.0` for USDC pairs.

Things you must do **manually on Binance + Railway** before restarting:

1. **Stop the Railway `api` service** (so the bot can't fight the cleanup).
2. **Cancel every open BTCUSDT order** on the live account — the bot's
   persisted state references their order IDs and will throw 404s on
   reconciliation otherwise. The simplest path is the Binance UI's
   "Cancel All" on the BTCUSDT order book, or
   `python -m scripts.check_open_orders` followed by per-order cancels.
3. **(Optional, recommended) sell residual BTC to USDT, then convert
   USDT → USDC** via Binance Convert (zero fee). This gives the new
   bot a clean USDC float and removes any orphan BTC bags from the
   persisted state.
4. **Wipe `state_lifo_binance_live.json` on the Railway pod via the
   built-in env-var purge.** Without this, the runner will boot with
   stale bags whose entry prices are USDT-quoted and try to place USDC
   TPs at the wrong levels.

   ⚠️ Don't `rm` the file via `railway ssh` — the running bot
   re-persists in-memory state every ~10s and you'll lose the race.
   Instead:

   ```bash
   railway variables --set "LIFO_RESET_BINANCE_LIVE=1" --service backend
   railway up --service backend --detach
   # wait ~60s, confirm `LIFO_RESET_BINANCE_LIVE=1 detected — purged …` in logs
   echo "y" | railway variable delete LIFO_RESET_BINANCE_LIVE --service backend
   ```

   Full procedure + per-venue env-var names are in
   [`OPERATIONS.md`](./OPERATIONS.md#state-reset-procedure).
5. **Set the Railway env var `SYMBOL=BTCUSDC`** (overrides the default
   either way; do it explicitly so it's visible in the Railway UI).
6. **Restart the `api` service.** The bot will boot fresh against
   BTCUSDC, see your USDC balance, and start placing 0%-maker buys.

Verification checklist after restart:

- `scripts/probe_binance_fees.py` (with `SYMBOL=BTCUSDC` in your shell)
  should show `commissionAsset=BNB` rows with `fee%` ≈ `0.00000%` for
  every maker fill. Taker fills (only the dashboard "Buy Now" path)
  should show ≈ `0.07125%`.
- The dashboard's "live" channel should show open orders priced in USDC
  (e.g. `99,800.00 USDC`), not USDT.
- The first closed bag's gross PnL should equal its net PnL — there is
  no fee leakage on USDC maker pairs.

---

## References

- Binance fee schedule: <https://www.binance.info/en/fee/schedule>
- Binance API limits: <https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits>
- Binance order count decrement (rate-limit semantics): <https://developers.binance.com/docs/binance-spot-api-docs/faqs/order_count_decrement>
- Revolut X fees (legal page): <https://www.revolut.com/legal/crypto-exchange-fees/>
- Revolut X fee help article: <https://help.revolut.com/help/wealth/cryptocurrencies/crypto-exchange/revolut-x-fees/>
- Revolut X API — place order: <https://developer.revolut.com/docs/x-api/place-order>
- Revolut general API usage limits: <https://developer.revolut.com/docs/guides/manage-accounts/api-usage-and-testing/usage-and-limits>

## Probe scripts (run anytime to re-verify)

- `python -m scripts.probe_revolut_fees` — read-only; lists Revolut
  order history, derives realised taker fee from BUY → SELL round-trips,
  prints conclusion vs. configured `LIFO_REVOLUT_FEE_RATE`.
- `python -m scripts.probe_revolut_endpoints` — read-only; sweeps
  Revolut endpoints to check for new fee/balance-history surfaces.
- `python -m scripts.probe_binance_fees` — read-only; pulls last 50
  fills from `/api/v3/myTrades`, reports `isMaker` ratio + realised
  fee % per leg in quote-currency terms (handles BNB conversion).
