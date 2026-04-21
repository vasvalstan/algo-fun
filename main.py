"""
Entry point — runs a demo flow on Binance testnet.

What this script does, step by step:
  1. Fetches the current BTC price so you can see the connection works.
  2. Shows the top 5 levels of the order book.
  3. Places a tiny limit BUY order far below the current price
     (so it will NOT fill — it just sits on the book as a demo).
  4. Lists your open orders to prove the order is there.
  5. Cancels the order immediately.

Run it with:  python main.py
"""

import json
import logging
import sys

import requests

import config
import market_data
import trading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)


def pp(label: str, data) -> None:
    """Pretty-print a labelled JSON blob."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(data, indent=2))


def main() -> None:
    log.info("Using %s", "MAINNET" if config.USE_MAINNET else "TESTNET")
    log.info("Symbol: %s", config.SYMBOL)

    # --- Step 1: current price -------------------------------------------
    price_data = market_data.get_price()
    pp("Current price", price_data)
    current_price = float(price_data["price"])

    # --- Step 2: order book ----------------------------------------------
    book = market_data.get_orderbook()
    pp("Order book (top 5)", book)

    # --- Step 3: place a test limit order --------------------------------
    # We set the price to 50 % of the current price so it will never fill.
    test_price = f"{current_price * 0.5:.2f}"
    test_qty = "0.001"

    log.info(
        "Placing test BUY limit order: %s %s @ %s",
        test_qty, config.SYMBOL, test_price,
    )
    order = trading.place_limit_order(
        side="BUY",
        quantity=test_qty,
        price=test_price,
    )
    pp("Order response", order)
    order_id = order["orderId"]

    # --- Step 4: list open orders ----------------------------------------
    open_orders = trading.get_open_orders()
    pp("Open orders", open_orders)

    # --- Step 5: cancel the test order -----------------------------------
    log.info("Cancelling order %s ...", order_id)
    cancel_resp = trading.cancel_order(order_id)
    pp("Cancel response", cancel_resp)

    log.info("Done. All steps completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        resp = exc.response
        if resp is not None:
            log.error("Binance HTTP %s — body: %s", resp.status_code, resp.text)
        else:
            log.error("Something went wrong: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Something went wrong: %s", exc)
        sys.exit(1)
