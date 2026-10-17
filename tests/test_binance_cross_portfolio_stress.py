import asyncio
from types import SimpleNamespace

from bot import binance_cross_portfolio_stress as stress


def run(coro):
    return asyncio.run(coro)


def bracket_payload(symbol="BTCUSDT", first_max_leverage=125):
    return {
        "symbol": symbol,
        "source": "BINANCE_FAPI_LEVERAGE_BRACKET",
        "brackets": [
            {
                "bracket": 1,
                "initialLeverage": first_max_leverage,
                "notionalFloor": 0.0,
                "notionalCap": 1000.0,
                "maintMarginRatio": 0.005,
                "cum": 0.0,
            },
            {
                "bracket": 2,
                "initialLeverage": 50,
                "notionalFloor": 1000.0,
                "notionalCap": 10000.0,
                "maintMarginRatio": 0.01,
                "cum": 5.0,
            },
        ],
    }


class FakeClient:
    def __init__(self, *, multi_assets=False, positions=None, order_margin=0.0):
        self.multi_assets = multi_assets
        self.positions = positions or []
        self.order_margin = order_margin

    async def get_leverage_brackets(self, symbol):
        return bracket_payload(str(symbol).upper())

    async def get_account_state(self):
        return {
            "crossWalletBalance": 1000.0,
            "orderMargin": self.order_margin,
            "multiAssetsMargin": self.multi_assets,
            "canTrade": True,
        }

    async def get_positions(self):
        return list(self.positions)


def make_position(
    symbol="BTCUSDT",
    direction="LONG",
    entry=100.0,
    current_price=105.0,
    stop=95.0,
    qty=1.0,
):
    return SimpleNamespace(
        symbol=symbol,
        direction=direction,
        entry=entry,
        current_price=current_price,
        sl=stop,
        trailing_sl=stop,
        qty=qty,
    )


def make_signal(
    symbol="ETHUSDT",
    direction="LONG",
    entry=100.0,
    stop=90.0,
):
    return SimpleNamespace(
        symbol=symbol,
        direction=direction,
        entry=entry,
        sl=stop,
    )


def test_select_bracket_uses_returned_notional_ranges():
    payload = bracket_payload()
    assert stress.select_bracket(payload, 999.0)["bracket"] == 1
    assert stress.select_bracket(payload, 1000.0)["bracket"] == 2


def test_paper_cross_stress_uses_simulated_wallet_and_positions():
    engine = SimpleNamespace(
        paper_trade=True,
        risk=SimpleNamespace(balance=1000.0),
        positions={"BTCUSDT": make_position()},
        client=FakeClient(),
        instruments={"BTCUSDT": {"qtyStep": 0.001}, "ETHUSDT": {"qtyStep": 0.001}},
    )
    result = run(stress.evaluate(engine, make_signal(), 1.0))
    assert result.allowed is True
    assert result.mode == "PAPER"
    assert result.existing_positions == 1
    assert result.risk_rate < stress.MAX_STOP_STRESS_RISK_RATE


def test_paper_stress_fails_closed_if_existing_stop_is_not_adverse():
    engine = SimpleNamespace(
        paper_trade=True,
        risk=SimpleNamespace(balance=1000.0),
        positions={
            "BTCUSDT": make_position(current_price=90.0, stop=95.0)
        },
        client=FakeClient(),
        instruments={"BTCUSDT": {"qtyStep": 0.001}, "ETHUSDT": {"qtyStep": 0.001}},
    )
    result = run(stress.evaluate(engine, make_signal(), 1.0))
    assert result.allowed is False
    assert "invalid_local_position_geometry" in result.reason


def test_live_stress_blocks_multi_assets_mode():
    engine = SimpleNamespace(
        paper_trade=False,
        positions={},
        client=FakeClient(multi_assets=True),
        instruments={"ETHUSDT": {"qtyStep": 0.001}},
    )
    result = run(stress.evaluate(engine, make_signal(), 1.0))
    assert result.allowed is False
    assert "multi_assets_margin_unsupported" in result.reason


def test_live_stress_blocks_external_position_mismatch():
    exchange_position = {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": 1.0,
        "entryPrice": 100.0,
        "markPrice": 105.0,
        "marginType": "cross",
    }
    engine = SimpleNamespace(
        paper_trade=False,
        positions={},
        client=FakeClient(positions=[exchange_position]),
        instruments={"BTCUSDT": {"qtyStep": 0.001}, "ETHUSDT": {"qtyStep": 0.001}},
    )
    result = run(stress.evaluate(engine, make_signal(), 1.0))
    assert result.allowed is False
    assert "exchange_local_position_count_mismatch" in result.reason
