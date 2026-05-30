from live_visualizer.data_feed.candle_buffer import CandleBuffer


def test_candle_buffer_merges_same_kline_and_limits_size():
    buf = CandleBuffer(limit=2)
    buf.update_from_kline({"k": {"t": 1000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "x": False}})
    buf.update_from_kline({"k": {"t": 1000, "o": "1", "h": "3", "l": "0.5", "c": "2.5", "v": "11", "x": True}})
    buf.update_from_kline({"k": {"t": 61000, "o": "2", "h": "4", "l": "1", "c": "3", "v": "12", "x": True}})
    buf.update_from_kline({"k": {"t": 121000, "o": "3", "h": "5", "l": "2", "c": "4", "v": "13", "x": False}})

    candles = buf.as_list()
    assert len(candles) == 2
    assert candles[0].time == 61
    assert candles[1].close == 4.0

