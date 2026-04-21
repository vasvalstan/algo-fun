"""
Live trading bot — top-down trend-aware maker strategy.

  python bot.py

On startup it:
  1. Queries exchange info to learn price/quantity precision rules.
  2. Reads your USDT balance as the starting capital number.
  3. Adopts any existing open orders from Binance.
  4. Bootstraps historical klines for all timeframes (5m → 1M) and runs
     full top-down analysis before treating wallet BTC (default: analyze
     first, then allow new buys when ENTRY_READY; see WALLET_BTC_POLICY).
  5. Enters an infinite poll loop (Ctrl+C to stop).

On shutdown (Ctrl+C) it cancels any open order so nothing is left
on the book.
"""

import logging
import sys
import time
from typing import Tuple

import requests

import config
import dashboard
import market_data
import state_writer
from strategy import TrendAwareMakerStrategy, State

logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


def _get_precision() -> Tuple[int, int, float]:
    """Fetch price/qty precision and min notional (USDT) from exchange info."""
    info = market_data.get_exchange_info()
    price_prec = 2
    qty_prec = 5
    min_notional = 5.0
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = f["tickSize"]
            price_prec = max(0, len(tick.rstrip("0").split(".")[-1]))
        elif f["filterType"] == "LOT_SIZE":
            step = f["stepSize"]
            qty_prec = max(0, len(step.rstrip("0").split(".")[-1]))
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            raw = f.get("minNotional") or f.get("notional")
            if raw is not None:
                min_notional = float(raw)
    return price_prec, qty_prec, min_notional


def _floor_qty(amount: float, qty_prec: int) -> float:
    if qty_prec <= 0:
        return float(int(amount))
    step = 10.0 ** (-qty_prec)
    return int(amount / step + 1e-12) * step


def _get_balances() -> dict:
    from trading import get_account

    base_asset = config.SYMBOL.replace("USDT", "")
    want = {base_asset, "USDT"}
    result = {}
    acct = get_account()
    for b in acct.get("balances", []):
        if b["asset"] in want:
            result[b["asset"]] = {
                "free": float(b["free"]),
                "locked": float(b["locked"]),
            }
    return result


def _portfolio_equity_usdt(spot_price: float) -> float:
    balances = _get_balances()
    base = config.SYMBOL.replace("USDT", "")
    u = balances.get("USDT", {"free": 0.0, "locked": 0.0})
    b = balances.get(base, {"free": 0.0, "locked": 0.0})
    return (u["free"] + u["locked"]) + (b["free"] + b["locked"]) * spot_price


def main() -> None:
    env_label = "MAINNET" if config.USE_MAINNET else "TESTNET"
    log.info("Starting bot on %s  symbol=%s", env_label, config.SYMBOL)

    # ── startup checks ───────────────────────────────────────────────
    price_prec, qty_prec, min_notional = _get_precision()
    log.info(
        "Precision: price=%d  qty=%d  min_notional=%.2f USDT",
        price_prec, qty_prec, min_notional,
    )

    price_now = float(market_data.get_price()["price"])
    balances = _get_balances()

    base_asset = config.SYMBOL.replace("USDT", "")
    usdt_free = balances.get("USDT", {}).get("free", 0.0)
    usdt_locked = balances.get("USDT", {}).get("locked", 0.0)
    base_free = balances.get(base_asset, {}).get("free", 0.0)
    base_locked = balances.get(base_asset, {}).get("locked", 0.0)

    starting_value = (usdt_free + usdt_locked) + (base_free + base_locked) * price_now
    log.info(
        "Starting value: %.4f USDT  (%.4f+%.4f USDT  %.8f+%.8f %s @ %.2f)",
        starting_value, usdt_free, usdt_locked,
        base_free, base_locked, base_asset, price_now,
    )

    strat = TrendAwareMakerStrategy(
        price_precision=price_prec,
        qty_precision=qty_prec,
        starting_balance=starting_value,
    )

    # ── recover existing open orders from Binance ────────────────────
    from trading import get_open_orders

    try:
        existing_orders = get_open_orders()
    except Exception as exc:
        existing_orders = []
        log.warning("Could not fetch open orders: %s", exc)

    for o in existing_orders:
        action = strat.recover_open_order(
            order_id=o["orderId"],
            side=o["side"],
            price=float(o["price"]),
            quantity=float(o["origQty"]),
        )
        if action:
            log.info("RECOVERY: %s", action)
        else:
            log.warning("No slot for order %s %s @ %s", o["side"], o["origQty"], o["price"])
        break  # only one slot

    # ── bootstrap klines + analysis BEFORE wallet BTC policy ───────────
    print("Bootstrapping historical klines (this may take a few seconds)...")
    boot_msg = strat.bootstrap()
    log.info(boot_msg)
    print(f"  {boot_msg}")

    print("\n── Market analysis (after loading history) ──")
    print(strat.format_analysis_summary())
    print("──\n")

    # Free BTC: analyze-first default — no forced sell until you opt in
    if strat.positions[0].state == State.WATCHING and base_free > 0:
        floored = _floor_qty(base_free, qty_prec)
        notion = floored * price_now
        if floored > 0 and notion >= min_notional:
            wmsg = strat.reconcile_wallet_btc(floored, price_now, min_notional)
            if wmsg:
                log.info("Wallet BTC: %s", wmsg)
                print(f"  {wmsg}")
        elif floored > 0:
            log.warning(
                "Free %s %.8f (~%.2f USDT) below min notional %.2f",
                base_asset, base_free, notion, min_notional,
            )

    print("Starting trading loop...\n")

    start_time = time.time()
    last_action = "starting up…"

    # ── main loop ────────────────────────────────────────────────────
    try:
        while True:
            try:
                price_data = market_data.get_price()
                price = float(price_data["price"])

                action = strat.tick(price)
                if action:
                    last_action = action
                    log.info(action)

                try:
                    strat.session_equity_usdt = _portfolio_equity_usdt(price)
                except Exception as exc:
                    log.debug("equity refresh failed: %s", exc)

                dashboard.render(strat, start_time, last_action)
                state_writer.write_state(strat, start_time, last_action)

            except requests.HTTPError as exc:
                resp = exc.response
                body = resp.text if resp is not None else str(exc)
                msg = f"HTTP {resp.status_code if resp else '?'}: {body}"
                log.warning(msg)
                last_action = msg
                strat.errors.append(msg)
            except requests.ConnectionError:
                msg = "connection error, retrying…"
                log.warning(msg)
                last_action = msg
            except Exception as exc:
                msg = f"unexpected: {exc}"
                log.error(msg, exc_info=True)
                last_action = msg
                strat.errors.append(msg)

            time.sleep(config.POLL_INTERVAL)

    except KeyboardInterrupt:
        sys.stdout.write("\n")
        log.info("Shutting down (Ctrl+C)")

        if strat.positions[0].open_order is not None:
            log.info("Cancelling open order")
            strat.cancel_all_open_orders()
            print("Open order cancelled.")

        print(
            f"Ran for {int(time.time() - start_time)}s, "
            f"{len(strat.cycles)} cycles completed."
        )
        print("Goodbye.")


if __name__ == "__main__":
    main()
