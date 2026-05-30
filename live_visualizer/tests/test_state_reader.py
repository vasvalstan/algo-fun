from live_visualizer.state.reader import normalize_state


def test_normalize_shared_state():
    state = normalize_state(
        {
            "symbol": "BTCUSDC",
            "last_price": 100,
            "regime": "RANGE",
            "support_level": 95,
            "resistance_level": 105,
            "active_orders": [{"side": "BUY", "price": 96, "qty": 0.1}],
            "open_bags": [{"entry_price": 98, "qty": 0.1, "tp_price": 101}],
        },
        default_symbol="BTCUSDC",
    )
    assert state.symbol == "BTCUSDC"
    assert state.support_level == 95
    assert state.active_orders[0].side == "BUY"


def test_normalize_existing_lifo_snapshot():
    state = normalize_state(
        {
            "symbol": "BTCUSDC",
            "price": 100,
            "grid": {
                "status": "ACTIVE",
                "resting_buy": {"order_id": "b1", "price": 97, "kind": "LIFO_REARM"},
                "total_pnl": 3.5,
            },
            "strategy": {"macro_regime": "LIFO ACTIVE", "market_mode": "1/6 bags"},
            "positions": [
                {
                    "slot_id": 7,
                    "entry_price": 98,
                    "slot_qty": 0.2,
                    "tp_price": 101,
                    "sell_order_id": "s1",
                    "unrealized_usdt": 0.4,
                }
            ],
        },
        default_symbol="BTCUSDC",
    )
    assert state.regime == "LIFO ACTIVE"
    assert state.support_level == 97
    assert state.resistance_level == 101
    assert len(state.active_orders) == 2
    assert state.position_qty == 0.2

