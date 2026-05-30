from live_visualizer.data_feed.binance import BinanceMarketFeed
from live_visualizer.data_feed.candle_buffer import CandleBuffer


def test_stream_url_defaults_to_btcusdc_kline_and_book():
    feed = BinanceMarketFeed(symbol="BTCUSDC", interval="1m", candle_buffer=CandleBuffer())
    url = feed.stream_url()
    assert "btcusdc%40kline_1m%2Fbtcusdc%40bookTicker" in url


def test_handle_book_ticker_and_kline_messages():
    feed = BinanceMarketFeed(symbol="BTCUSDC", interval="1m", candle_buffer=CandleBuffer())
    feed.handle_message({"data": {"e": "bookTicker", "b": "100", "a": "100.1", "B": "1", "A": "2"}})
    assert feed.book.bid == 100
    feed.handle_message({"data": {"e": "kline", "k": {"t": 1000, "o": "1", "h": "2", "l": "1", "c": "2", "v": "3"}}})
    assert feed.candle_buffer.latest().close == 2


def test_rest_seed_parses_klines(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'[[1000,"1","2","0.5","1.5","10"]]'

    monkeypatch.setattr("live_visualizer.data_feed.binance.urlopen", lambda *_args, **_kwargs: Resp())
    feed = BinanceMarketFeed(symbol="BTCUSDC", interval="1m", candle_buffer=CandleBuffer())
    assert feed.seed_from_rest() == 1
    assert feed.candle_buffer.latest().close == 1.5
