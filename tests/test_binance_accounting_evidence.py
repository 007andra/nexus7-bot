import asyncio
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance_accounting_evidence as accounting


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append((endpoint, dict(params or {}), auth))
        value = self.responses[endpoint]
        if callable(value):
            return value(dict(params or {}))
        return value


def run(coro):
    return asyncio.run(coro)


def test_collect_user_trades_normalizes_and_requires_identity():
    client = FakeClient({
        "/fapi/v1/userTrades": [
            {
                "symbol": "BTCUSDT",
                "id": 7,
                "orderId": 11,
                "side": "BUY",
                "price": "100",
                "qty": "0.1",
                "quoteQty": "10",
                "realizedPnl": "0",
                "commission": "0.004",
                "commissionAsset": "USDT",
                "time": 1500,
                "buyer": True,
                "maker": False,
                "positionSide": "BOTH",
                "ignored": "secret-noise",
            }
        ]
    })
    rows = run(accounting.collect_user_trades(client, "BTCUSDT", 1000, 2000))
    assert rows == [{
        "symbol": "BTCUSDT",
        "id": 7,
        "orderId": 11,
        "side": "BUY",
        "price": "100",
        "qty": "0.1",
        "quoteQty": "10",
        "realizedPnl": "0",
        "commission": "0.004",
        "commissionAsset": "USDT",
        "time": 1500,
        "buyer": True,
        "maker": False,
        "positionSide": "BOTH",
    }]
    assert client.calls[0][2] is True


def test_collect_user_trades_refuses_unproven_full_page():
    rows = [
        {"symbol": "BTCUSDT", "id": i, "orderId": i, "time": 1500}
        for i in range(1000)
    ]
    client = FakeClient({"/fapi/v1/userTrades": rows})
    with unittest.TestCase().assertRaisesRegex(ValueError, "coverage"):
        run(accounting.collect_user_trades(client, "BTCUSDT", 1000, 2000))


def test_collect_income_pages_and_deduplicates():
    def response(params):
        if params["page"] == 1:
            return [
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "REALIZED_PNL",
                    "income": "1.25",
                    "asset": "USDT",
                    "info": "",
                    "time": 1500,
                    "tranId": 9,
                    "tradeId": "7",
                }
            ]
        return []

    client = FakeClient({"/fapi/v1/income": response})
    rows = run(accounting.collect_income(client, 1000, 2000))
    assert len(rows) == 1
    assert rows[0]["incomeType"] == "REALIZED_PNL"


def test_transfer_minute_evidence_uses_only_usdt_and_never_leaks_ids():
    minute = int(datetime(2026, 10, 11, 19, 41, tzinfo=timezone.utc).timestamp() * 1000)
    secret = "private-transaction-identifier"
    rows = [
        {"incomeType": "TRANSFER", "asset": "USDT", "income": "-5.31768526",
         "time": minute + 19000, "tranId": secret, "info": secret},
        {"incomeType": "TRANSFER", "asset": "BNB", "income": "-80",
         "time": minute, "tranId": "bnb-private"},
        {"incomeType": "REALIZED_PNL", "asset": "USDT", "income": "-4",
         "time": minute, "tranId": "pnl-private"},
        {"incomeType": "TRANSFER", "asset": "USDT", "income": "1.25000000",
         "time": minute + 62000, "tranId": "internal-private"},
    ]
    result = accounting.summarize_usdt_transfer_timing(rows)
    assert result == {
        "first_utc_minute": "2026-10-11T19:41Z",
        "last_utc_minute": "2026-10-11T19:42Z",
        "count": 2,
        "outflow_count": 1,
        "inflow_count": 1,
        "net_usdt": "-4.06768526",
    }
    assert secret not in repr(result)
    assert "bnb-private" not in repr(result)
    assert "pnl-private" not in repr(result)
    assert accounting.summarize_usdt_transfer_timing([
        {"incomeType": "FUNDING_FEE", "asset": "USDT",
         "income": "-1", "time": minute}
    ]) is None


def test_transfer_minute_evidence_refuses_unconfirmed_values():
    for amount, timestamp in (("nan", 1000), ("inf", 1000), ("1", 0)):
        with unittest.TestCase().assertRaises(ValueError):
            accounting.summarize_usdt_transfer_timing([{
                "incomeType": "TRANSFER", "asset": "USDT",
                "income": amount, "time": timestamp,
            }])


def test_audit_ledger_logs_transfer_minute_without_raw_identity_or_mutation():
    now_ms = int(time.time() * 1000)
    secret = "private-transfer-id-never-log"
    payload = [{
        "incomeType": "TRANSFER", "asset": "USDT", "income": "-5.31768526",
        "time": now_ms - 120000, "tranId": secret, "info": secret,
    }]
    client = FakeClient({"/fapi/v1/income": payload})
    with (
        patch.object(accounting.db, "load_key_value", AsyncMock(return_value=None)),
        patch.object(accounting.db, "save_key_value", AsyncMock(return_value=True)) as save,
        patch.object(accounting.log, "warning") as log,
    ):
        rows = run(accounting.audit_ledger(SimpleNamespace(client=client)))
        observed = [call for call in log.call_args_list
                    if "[BINANCE_TRANSFER_MINUTE_EVIDENCE]" in call.args[0]]
        assert len(observed) == 1
        emitted = observed[0]
        assert "destination=UNVERIFIED" in emitted.args[0]
        assert "authority=false" in emitted.args[0]
        assert "execution_effect=NONE" in emitted.args[0]
        assert emitted.args[1:4] == (1, 1, 0)
        assert emitted.args[4] == "-5.31768526"
        assert secret not in repr(log.call_args_list)
        assert len(rows) == 1
        assert save.await_count == 1
    assert client.calls
    assert all(endpoint == "/fapi/v1/income" and auth
               for endpoint, _, auth in client.calls)


def test_audit_ledger_on_no_transfers_emits_no_transfer_provenance():
    client = FakeClient({"/fapi/v1/income": []})
    with patch.object(accounting.log, "warning") as log:
        assert run(accounting.audit_ledger(SimpleNamespace(client=client))) == []
    assert not any("[BINANCE_TRANSFER_MINUTE_EVIDENCE]" in call.args[0]
                   for call in log.call_args_list)


def test_bgx_order_evidence_never_becomes_bgx_confirmed():
    trade = {"symbol": "BTCUSDT", "orderId": 11, "side": "BUY"}
    order = {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "bgx7-entry-abc"}
    registry = [{"order_id": 11, "symbol": "BTCUSDT"}]
    origin, reason = accounting.classify_trade_origin(trade, order, registry)
    assert origin == "BGX_ORDER_EVIDENCE"
    assert origin != "BGX_CONFIRMED"
    assert reason == "BGX_CLIENT_ORDER_ID_AND_DURABLE_ORDER"


def test_non_bgx_order_without_durable_registry_is_manual_external():
    trade = {"symbol": "BTCUSDT", "orderId": 22, "side": "SELL"}
    order = {"symbol": "BTCUSDT", "orderId": 22, "clientOrderId": "manual-order"}
    origin, reason = accounting.classify_trade_origin(trade, order, [])
    assert origin == "MANUAL_EXTERNAL"
    assert reason == "NON_BGX_CLIENT_ORDER_ID_CONFIRMED"


def test_durable_order_with_non_bgx_client_id_fails_closed():
    trade = {"symbol": "BTCUSDT", "orderId": 33, "side": "SELL"}
    order = {"symbol": "BTCUSDT", "orderId": 33, "clientOrderId": "manual-order"}
    registry = [{"order_id": 33, "symbol": "BTCUSDT"}]
    origin, reason = accounting.classify_trade_origin(trade, order, registry)
    assert origin == "UNKNOWN_UNATTRIBUTED"
    assert reason == "DURABLE_ORDER_CONFLICTS_WITH_NON_BGX_CLIENT_ID"


def test_invalid_window_over_seven_days_fails_closed():
    client = FakeClient({"/fapi/v1/userTrades": []})
    with unittest.TestCase().assertRaisesRegex(ValueError, "window"):
        run(accounting.collect_user_trades(
            client, "BTCUSDT", 1000, 1000 + 7 * 86400000 + 1
        ))

def _entry_registry(order_id=11):
    return {
        "order_id": str(order_id),
        "client_oid": "bgx7-entry",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "state": "FILLED",
        "reduce_only": False,
        "exposure_intent": "INCREASE",
        "previous_position_qty": 0.0,
    }


def _close_registry(order_id=22):
    return {
        "order_id": str(order_id),
        "client_oid": "bgx7-close",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "state": "FILLED",
        "reduce_only": True,
        "exposure_intent": "REDUCE",
        "previous_position_qty": 1.0,
    }


def _lineage(order_id=11):
    return {
        "version": 2,
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "order_id": str(order_id),
        "client_oid": "bgx7-entry",
        "order_created_at_ms": 900,
        "captured_at_ms": 1100,
    }


def _orders():
    return [
        {
            "symbol": "BTCUSDT", "orderId": 11,
            "clientOrderId": "bgx7-entry", "side": "BUY",
            "positionSide": "BOTH", "status": "FILLED",
        },
        {
            "symbol": "BTCUSDT", "orderId": 22,
            "clientOrderId": "bgx7-close", "side": "SELL",
            "positionSide": "BOTH", "status": "FILLED",
            "reduceOnly": True,
        },
    ]


def _open_fills():
    return [
        {
            "symbol": "BTCUSDT", "id": 1, "orderId": 11, "side": "BUY",
            "price": "100", "qty": "0.4", "realizedPnl": "0",
            "commission": "0.02", "commissionAsset": "USDT",
            "time": 1000, "positionSide": "BOTH",
        },
        {
            "symbol": "BTCUSDT", "id": 2, "orderId": 11, "side": "BUY",
            "price": "101", "qty": "0.6", "realizedPnl": "0",
            "commission": "0.03", "commissionAsset": "USDT",
            "time": 1001, "positionSide": "BOTH",
        },
    ]


def test_collect_algo_orders_requires_identity_and_preserves_actual_order_link():
    client = FakeClient({
        "/fapi/v1/allAlgoOrders": [{
            "symbol": "BTCUSDT", "algoId": 9, "clientAlgoId": "bgx7-stop",
            "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
            "side": "SELL", "positionSide": "BOTH", "quantity": "0",
            "algoStatus": "FINISHED", "actualOrderId": "33",
            "actualPrice": "95", "triggerPrice": "95", "closePosition": True,
            "reduceOnly": False, "createTime": 1200, "updateTime": 1500,
            "triggerTime": 1490,
        }]
    })
    rows = run(accounting.collect_algo_orders(client, "BTCUSDT", 1000, 2000))
    assert rows[0]["clientAlgoId"] == "bgx7-stop"
    assert rows[0]["actualOrderId"] == "33"
    assert client.calls[0][0] == "/fapi/v1/allAlgoOrders"
    assert client.calls[0][2] is True


def test_reconstruct_complete_bgx_lifecycle_from_flat_to_flat():
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [_entry_registry(), _close_registry()],
        {"11": _lineage()},
    )
    assert len(cycles) == 1
    row = cycles[0]["row"]
    receipt = cycles[0]["receipt"]
    assert row["side"] == "LONG"
    assert row["openPrice"] == "100.6"
    assert row["closePrice"] == "110"
    assert row["tradeFee"] == "0.10"
    assert row["pnl"] == "9.30"
    assert receipt["ownership"] == "BGX_ORDER_IDS"
    assert receipt["fills_reconciled"] is True
    assert receipt["lineage_reconciled"] is True
    assert receipt["accounting_authority"] is False


def test_reconstruct_partial_close_remains_unconfirmed():
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "0.5", "realizedPnl": "4.7",
        "commission": "0.025", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [_entry_registry(), _close_registry()],
        {"11": _lineage()},
    )
    assert cycles == []


def test_reconstruct_reversal_through_zero_fails_closed():
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.2", "realizedPnl": "9.4",
        "commission": "0.06", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [_entry_registry(), _close_registry()],
        {"11": _lineage()},
    )
    assert cycles == []


def test_reconstruct_requires_zero_pre_entry_exposure_proof():
    entry = _entry_registry()
    entry["previous_position_qty"] = None
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [entry, _close_registry()],
        {"11": _lineage()},
    )
    assert cycles == []


def test_reconstruct_accepts_bgx_algo_close_via_actual_order_id():
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 33, "side": "SELL",
        "price": "95", "qty": "1.0", "realizedPnl": "-5.6",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    algo = [{
        "symbol": "BTCUSDT", "algoId": 90, "clientAlgoId": "bgx7-stop",
        "side": "SELL", "positionSide": "BOTH", "actualOrderId": "33",
        "algoStatus": "FINISHED", "closePosition": True,
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, [_orders()[0]], algo, [_entry_registry()],
        {"11": _lineage()},
    )
    assert len(cycles) == 1
    assert cycles[0]["row"]["pnl"] == "-5.70"
    assert cycles[0]["receipt"]["close_identity"] == ["BGX_ALGO_CLOSE_ORDER"]

def test_reconstruct_missing_lineage_fails_closed():
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [_entry_registry(), _close_registry()], {}
    )
    assert cycles == []


def test_reconstruct_hedge_mode_fill_fails_closed():
    trades = [dict(row) for row in _open_fills()]
    trades[0]["positionSide"] = "LONG"
    trades.append({
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    })
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [_entry_registry(), _close_registry()],
        {"11": _lineage()},
    )
    assert cycles == []


def test_reconstruct_non_usdt_commission_fails_closed():
    trades = [dict(row) for row in _open_fills()]
    trades[0]["commissionAsset"] = "BNB"
    trades.append({
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    })
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [_entry_registry(), _close_registry()],
        {"11": _lineage()},
    )
    assert cycles == []


def test_reconstruct_manual_close_fails_closed():
    orders = _orders()
    orders[1] = dict(orders[1], clientOrderId="manual-close")
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, orders, [], [_entry_registry()],
        {"11": _lineage()},
    )
    assert cycles == []


def test_reconstruct_nonzero_pre_entry_exposure_fails_closed():
    entry = _entry_registry()
    entry["previous_position_qty"] = 0.25
    trades = _open_fills() + [{
        "symbol": "BTCUSDT", "id": 3, "orderId": 22, "side": "SELL",
        "price": "110", "qty": "1.0", "realizedPnl": "9.4",
        "commission": "0.05", "commissionAsset": "USDT",
        "time": 2000, "positionSide": "BOTH",
    }]
    cycles = accounting.reconstruct_bgx_lifecycles(
        trades, _orders(), [], [entry, _close_registry()],
        {"11": _lineage()},
    )
    assert cycles == []

