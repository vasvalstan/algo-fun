# OPERATIONS — LIFO Grid Bot Runbook

> **When the bot misbehaves in production, start here.** This is the
> "I see weird errors at 2am" runbook for the LIFO grid runners on
> Binance and Revolut. For initial deployment see
> [`RAILWAY_DEPLOYMENT.md`](./RAILWAY_DEPLOYMENT.md). For fee/spread
> assumptions see [`EXECUTION_COSTS.md`](./EXECUTION_COSTS.md). For
> what the strategy actually does see
> [`LIFO_GRID_STRATEGY.md`](./LIFO_GRID_STRATEGY.md).

---

## TL;DR cheat sheet

| Symptom | Most likely cause | Action |
|---|---|---|
| `place_sell bag#X failed: -2010 insufficient balance` (repeating) | Phantom bags — bot tracks BTC the wallet doesn't have | [State reset](#state-reset-procedure) |
| `place_sell bag#X failed: -2010 would immediately match and take` (one-off) | Price moved past TP between order build and submit | None — bot auto-backs off 120s and re-tries |
| `place_sell bag#X failed: -2010 would immediately match and take` (every tick on every bag) | Bot's `sell_target_price` is stale (manual price intervention?) | [Diagnose with probe](#diagnostics) then likely [reset](#state-reset-procedure) |
| Dashboard shows `MAX_AMMO N/N` for hours but TPs never fire | Same as above (phantom bags) — sells silently rejected | [State reset](#state-reset-procedure) |
| `PHANTOM BAGS DETECTED` in logs / Telegram | Bot detected the divergence itself | [State reset](#state-reset-procedure) |
| Telegram alerts stop arriving | `TELEGRAM_BOT_TOKEN` rotated, or the bot's ws is hung | Re-check env var, then `railway redeploy --service backend` |
| `[venue-name] venue not ready (missing credentials?)` | API keys missing/wrong on Railway | Re-set the API-key env vars on the service |

---

## State reset procedure

The bot's state lives in `/app/data/state_lifo_<venue>.json` on Railway's
**persistent volume**. That volume survives `railway up`, container
restarts, even service deletions on the same volume mount. So if your
bot has gotten into a stuck/divergent state, the file is the thing that
needs to go — but you cannot just `rm` it from `railway ssh`, because:

1. The running bot persists in-memory state every ~10s
   (`_maybe_persist`) — your `rm` loses the race almost immediately.
2. `kill -9 1` from `railway ssh` doesn't kill the bot process; it kills
   PID 1 of the SSH session's own PID namespace, not the uvicorn
   process.

**The race-free fix is built into the bot.** It checks an env var at
boot, and if set, deletes its own state file *before* loading anything.

### When to use it

- Wallet BTC and bot-tracked BTC have diverged (you sold/converted BTC
  off-bot, or migrated symbols).
- You see `PHANTOM BAGS DETECTED` in logs or Telegram (the bot
  auto-detects this in its orphan-BTC sweep at startup).
- You want to abandon all open bags and start a fresh hunt cycle.

### Procedure (Binance live)

```bash
# 1. Set the per-venue reset flag
railway variables --set "LIFO_RESET_BINANCE_LIVE=1" --service backend

# 2. Trigger a redeploy (so the new boot picks up the flag)
railway up --service backend --detach

# 3. Wait ~60s for the build + container swap, then verify the purge fired
railway logs --service backend | grep -E "LIFO_RESET|LIFO boot|sweep" | tail -10
# Expected smoking-gun line:
#   WARNING [binance-live] LIFO_RESET_BINANCE_LIVE=1 detected — purged
#           /app/data/state_lifo_binance_live.json. Unset the env var
#           after this boot to avoid wiping legitimate state on every
#           restart.
# Followed by:
#   INFO LIFO boot anchor=<spot> high=<spot> bags=0
#   INFO sweep: wallet BTC=<actual>, tracked=0.00000000, ...

# 4. CRITICAL — remove the env var so the next restart doesn't wipe again
echo "y" | railway variable delete LIFO_RESET_BINANCE_LIVE --service backend

# 5. Confirm it's gone
railway variables --service backend | grep LIFO_RESET
# (no output = success)
```

### Per-venue env-var names

The env var name is derived from `venue.spec.name`:
`<NAME>` is uppercased and `-` becomes `_`.

| Venue | `venue.spec.name` | Env var |
|---|---|---|
| Binance live | `binance-live` | `LIFO_RESET_BINANCE_LIVE` |
| Binance paper | `binance-paper` | `LIFO_RESET_BINANCE_PAPER` |
| Revolut live | `revolut-live` | `LIFO_RESET_REVOLUT_LIVE` |
| Revolut paper | `revolut-paper` | `LIFO_RESET_REVOLUT_PAPER` |

Truthy values: `1`, `true`, `yes`, `on` (case-insensitive). Anything
else (including unset) is a no-op — the bot loads its state file
normally.

### What the reset does (and doesn't) do

| Does | Does NOT do |
|---|---|
| Delete `state_lifo_<venue>.json` from disk | Cancel any open orders on the exchange |
| Reset `bag_seq` to 1 | Refund or move any actual BTC/USDC balance |
| Clear all in-memory bags | Touch any *other* venue's state file |
| Re-anchor at current spot on next tick | Affect paper-mode runners (different state files) |

So if the divergence was caused by leftover open SELL orders on the
exchange, **cancel them manually in the Binance/Revolut UI before
resetting**, otherwise the orphan-BTC sweep will see locked balance and
get confused. Use `scripts/probe_binance_state.py` (below) to enumerate
what's actually open.

---

## Diagnostics

All probe scripts are **read-only** — they make no orders, no cancels,
no transfers. Run from the repo root with the local `.env` loaded.

### `scripts/probe_binance_state.py`

Cross-checks Binance ground-truth against the local state file.
Run this **first** when a Binance-side error storm starts.

```bash
python -m scripts.probe_binance_state
```

Outputs:
- BTC / USDC / USDT / BNB free + locked balances.
- All open orders on `BTCUSDC` and `BTCUSDT` (the latter catches
  zombies left from the symbol migration).
- Bag count, `bag_seq`, total tracked BTC from
  `./data/state_lifo_binance_live.json`.
- Cross-check: `sum(bag.btc_amount)` vs wallet BTC (`free + locked`).
  A delta larger than dust = phantom bags.

> **Path**: the script reads `LIFO_STATE_DIR` if set, else `./data/`.
> When run locally it'll usually print "no state file found" because the
> live state lives on Railway's volume — the wallet/open-orders sections
> are still useful by themselves.

### `scripts/probe_binance_fees.py`

Verifies Binance Spot maker/taker fees by inspecting recent
`/api/v3/myTrades` rows (`isMaker`, `commission`, `commissionAsset`).
Useful after a VIP-tier change or to confirm BNB-fee discount is on.

### `scripts/probe_revolut_fees.py`

Empirically derives Revolut X taker fee from quantity differences in
round-trip BUY/SELL pairs (Revolut doesn't expose `fee` fields in
order responses). Confirms the 0% maker / 0.09% taker numbers in
`EXECUTION_COSTS.md`.

### `scripts/probe_revolut_endpoints.py`

Sweeps Revolut X endpoints to discover which return data and which
404. Useful when Revolut adds/removes endpoints — the bot uses
`/orders/historical`, which was discovered by this script.

---

## Reading the dashboard / logs

Each tick the runner emits one structured status line per venue. Decoding it:

```
[binance-live] MAX_AMMO 6/6 · spot $75,547 · avg entry $74,053 ·
   UR +2.02% ($+1.17) · oldest bag held 50.2m · next TP (LIFO #9)
   $73,367 (-2.88% away) · realized $+0.14 · new buys disabled until a TP fills
```

| Field | Meaning |
|---|---|
| `[binance-live]` | Venue label (matches state file + env-var suffix) |
| `MAX_AMMO 6/6` | All 6 bags filled — no new buys until one TP closes (LIFO frees ammo) |
| `ARMED 0/6` | No bags open; resting BUY is hunting |
| `ACTIVE N/M` | Some bags open, capacity for more |
| `spot $X` | Current spot (from venue's WS feed when alive, REST fallback otherwise) |
| `avg entry $X` | Volume-weighted average buy price across all open bags |
| `UR +X% ($+Y)` | **U**nrealized profit (% of total cost basis, $ in quote currency) |
| `oldest bag held Tm` | Wall-clock minutes since the oldest open bag entered |
| `next TP (LIFO #N) $X (±Y% away)` | Sell target of the most-recently-opened bag |
| `resting BUY $X` | Where the hunt limit-buy is currently sitting (only when `ARMED`/`ACTIVE`) |
| `realized $+X` | Cumulative closed P&L since the state file was last reset |

**Key red flags:**

- `MAX_AMMO N/N` for >1 hour with no `next TP` movement → bot is stuck;
  the TP either can't be filled (price too far) or sells are being
  silently rejected. Run `probe_binance_state.py`.
- `UR -X%` deeper than your max-drawdown plan with no protective stop →
  this strategy has no stop-loss; bags will sit until TP or you
  intervene. By design, but worth knowing.
- `realized $+0.0000` for days while bags churn → check that TPs are
  actually being filled and not silently rejected.

---

## Railway commands cheat sheet

```bash
# Tail live logs from one service
railway logs --service backend
railway logs --service backend | tail -100

# Search logs for a pattern
railway logs --service backend | grep -E "PHANTOM|LIFO_RESET|place_sell"

# Push code and trigger a deploy (does NOT push to git)
railway up --service backend --detach

# Set / list / delete env vars
railway variables --set "KEY=value" --service backend
railway variables --service backend
echo "y" | railway variable delete KEY --service backend

# Open an interactive shell on the running container (rarely useful for
# state changes — see the "you cannot just rm" note above)
railway ssh --service backend

# List the persistent-volume contents
railway ssh --service backend "ls -la /app/data"
```

---

## Why "just delete the state file" doesn't work

If you ever try to do this manually, here's what happens and why we
don't recommend it:

1. `railway ssh` opens a shell **alongside** the running uvicorn
   process, not inside it. The state file is owned by uvicorn.
2. `rm /app/data/state_lifo_binance_live.json` succeeds — for ~10s.
3. Within 10 seconds, `_maybe_persist` fires and recreates the file
   from in-memory state. You're back where you started.
4. To stop the persist, you need to kill uvicorn. But `kill -9 1` in
   the SSH session targets PID 1 of the SSH namespace (the shell
   itself), not uvicorn. `pkill uvicorn` works, **but Railway will
   immediately respawn the container, which will re-load the (just-
   recreated) state file before the next ssh session can rm it again.**
5. End result: a race between you and Railway's restart loop, and
   Railway always wins.

The env-var-gated purge sidesteps all of this by deleting the file
**from inside the bot's own boot path**, before `_load_state` runs.
There is no race because the bot is single-threaded at that point.

---

## Adding new runbook entries

When you hit a new failure mode worth documenting, add a row to the
TL;DR table at the top, and a section below with:

- The exact log line(s) that fired.
- The probe command used to confirm the diagnosis.
- The minimal fix.
- Whether code-side automation is possible (and a TODO if so).
