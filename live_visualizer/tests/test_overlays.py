from live_visualizer.data_feed.candle_buffer import Candle
from live_visualizer.overlays.builders import build_overlays
from live_visualizer.state.models import ActiveOrder, OpenBag, SharedBotState


def test_overlay_payload_contains_lines_orders_and_bags():
    candles = [
        Candle(time=1, open=99, high=101, low=98, close=100, volume=1),
        Candle(time=2, open=100, high=103, low=99, close=102, volume=1),
    ]
    state = SharedBotState(
        symbol="BTCUSDC",
        regime="RANGE",
        support_level=95,
        resistance_level=105,
        avg_entry_price=100,
        active_orders=[ActiveOrder(side="SELL", price=105, qty=0.1)],
        open_bags=[OpenBag(id="1", entry_price=100, qty=0.1, tp_price=105)],
    )
    overlays = build_overlays(state, candles)
    assert {line["id"] for line in overlays["price_lines"]} == {"support", "resistance", "avg-entry"}
    assert overlays["orders"][0]["side"] == "SELL"
    assert overlays["bags"][0]["tp_price"] == 105

