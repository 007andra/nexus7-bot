import asyncio
from decimal import Decimal

from bot import binance_exit_forensics as forensic


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params or {}), auth))
        return self.response


def run(coro):
    return asyncio.run(coro)


def test_collect_force_orders_is_authenticated_get_and_sanitized():
    client = FakeClient([
        {
            "symbol": "AVAXUSDT",
            "orderId": 77,
            "clientOrderId": "autoclose-abc",
            "status": "FILLED",
            "side": "SELL",
            "autoCloseType": "LIQUIDATION",
            "avgPrice": "11.50",
            "executedQty": "25",
            "time": 1500,
            "secretNoise": "discard-me",
        }
    ])
    rows = run(forensic.collect_force_orders(client, "AVAXUSDT", 1000, 2000))
    assert client.calls == [
        (
            "/fapi/v1/forceOrders",
            {
                "symbol": "AVAXUSDT",
                "startTime": 1000,
                "endTime": 2000,
                "limit": 100,
            },
            True,
        )
    ]
    assert rows[0]["orderId"] == 77
    assert "secretNoise" not in rows[0]


def _base_trades(close_order_id="22", close_price="11.515", realized="2.05"):
    return [
        {
            "symbol": "AVAXUSDT",
            "id": 1,
            "orderId": "11",
            "side": "BUY",
            "price": "11.433",
            "qty": "25",
            "realizedPnl": "0",
            "commission": "0.1429125",
            "commissionAsset": "USDT",
            "time": 1000,
        },
        {
            "symbol": "AVAXUSDT",
            "id": 2,
            "orderId": close_order_id,
            "side": "SELL",
            "price": close_price,
            "qty": "25",
            "realizedPnl": realized,
            "commission": "0.1439375",
            "commissionAsset": "USDT",
            "time": 2000,
        },
    ]


def _orders(close_order_id="22", client_id="generated-close"):
    return [
        {
            "symbol": "AVAXUSDT",
            "orderId": "11",
            "clientOrderId": "bgx7-entry",
        },
        {
            "symbol": "AVAXUSDT",
            "orderId": close_order_id,
            "clientOrderId": client_id,
        },
    ]


def test_missing_algo_link_keeps_cause_unattributed_but_fill_pnl_exact():
    receipt = forensic.reconcile_exit_evidence(
        symbol="AVAXUSDT",
        opening_order_id="11",
        trades=_base_trades(),
        orders=_orders(),
        algo_orders=[
            {
                "symbol": "AVAXUSDT",
                "clientAlgoId": "bgx7-stop",
                "actualOrderId": "",
                "orderType": "STOP_MARKET",
            }
        ],
        force_orders=[],
        income=[
            {
                "symbol": "AVAXUSDT",
                "incomeType": "REALIZED_PNL",
                "income": "2.05",
                "tradeId": "2",
                "time": 2000,
            },
            {
                "symbol": "AVAXUSDT",
                "incomeType": "FUNDING_FEE",
                "income": "-0.02572",
                "tradeId": "",
                "time": 1500,
            },
        ],
    )
    assert receipt["status"] == "RECONCILED"
    assert receipt["cause"] == "UNATTRIBUTED"
    assert receipt["cause_authority"] is False
    assert receipt["pnl_fill_authority"] is True
    assert receipt["realized_income_crosscheck"] is True
    assert receipt["close_vwap"] == "11.515"
    assert Decimal(receipt["net_after_funding"]) == Decimal("1.73743")


def test_algo_actual_order_id_proves_protective_close_identity():
    receipt = forensic.reconcile_exit_evidence(
        symbol="AVAXUSDT",
        opening_order_id="11",
        trades=_base_trades(),
        orders=[_orders()[0]],
        algo_orders=[
            {
                "symbol": "AVAXUSDT",
                "clientAlgoId": "bgx7-stop",
                "actualOrderId": "22",
                "orderType": "STOP_MARKET",
            }
        ],
        force_orders=[],
        income=[],
    )
    assert receipt["cause"] == "BGX_ALGO_STOP_MARKET"
    assert receipt["cause_authority"] is True
    assert receipt["close_identity"] == {"22": "BGX_ALGO_STOP_MARKET"}


def test_force_orders_proves_liquidation_without_guessing_from_price():
    receipt = forensic.reconcile_exit_evidence(
        symbol="AVAXUSDT",
        opening_order_id="11",
        trades=_base_trades(close_order_id="99"),
        orders=_orders(close_order_id="99"),
        algo_orders=[],
        force_orders=[
            {
                "symbol": "AVAXUSDT",
                "orderId": "99",
                "clientOrderId": "autoclose-123",
                "autoCloseType": "LIQUIDATION",
            }
        ],
        income=[],
    )
    assert receipt["cause"] == "EXCHANGE_LIQUIDATION"
    assert receipt["cause_authority"] is True


def test_forensic_module_contains_no_exchange_mutation_calls():
    import inspect

    source = inspect.getsource(forensic)
    forbidden = (
        "place_order(",
        "cancel_order(",
        "set_leverage(",
        "set_position_stops(",
        "._post(",
        "._delete(",
        "LIVE_RISK_OVERRIDE_APPROVED",
    )
    for token in forbidden:
        assert token not in source
