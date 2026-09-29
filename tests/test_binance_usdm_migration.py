"""Offline Binance USD-M migration regressions.

No Binance credentials and no network access are used.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import binance as bn
from bot.conditional_stop_protection import conditional_stop_confirmed


def run(coro):
    return asyncio.run(coro)


class FakeBinance(bn.BinanceClient):
    def __init__(self, responses=None):
        super().__init__()
        self.responses = responses or {}
        self.calls = []

    async def _get(self, endpoint, params=None, auth=False):
        self.calls.append(("GET", endpoint, params or {}, auth))
        value = self.responses.get(endpoint, {})
        if callable(value):
            value = value(params or {}, auth)
        return value

    async def close(self):
        return None


class BinanceMigrationTests(unittest.TestCase):
    def test_margin_rejection_is_durable_and_identity_scoped(self):
        import json
        from bot import durable_execution as durable
        from bot.order_state import OrderRegistry, OrderState

        async def scenario():
            engine = SimpleNamespace(orders=OrderRegistry(), _durable_order_lock=asyncio.Lock(),
                                     _durable_state_ok=True, _durable_state_errors=set())
            order, _ = engine.orders.get_or_create("bgx7-test", "LINKUSDT", "Buy", 19.34)
            order.transition(OrderState.SUBMITTING)
            params = {"newClientOrderId": order.client_oid, "symbol": "LINKUSDT",
                      "side": "BUY", "quantity": "19.34"}
            for exc in (
                RuntimeError("Binance POST /fapi/v1/order HTTP 400 code=-2019"),
                bn.BinanceAPIError("POST", "/fapi/v1/order", 503, -2019, "unknown", params),
                bn.BinanceAPIError("POST", "/fapi/v1/order", 400, -1007, "timeout", params),
                bn.BinanceAPIError("POST", "/fapi/v1/order", 400, -4116, "duplicate", params),
                bn.BinanceAPIError("POST", "/fapi/v1/order", 400, -2019, "margin", dict(params, newClientOrderId="other")),
                bn.BinanceAPIError("POST", "/fapi/v1/order", 400, -2019, "margin", dict(params, quantity="20")),
            ):
                self.assertFalse(await durable.record_binance_margin_rejection(engine, order, exc))
                self.assertEqual(order.state, OrderState.SUBMITTING)
            exc = bn.BinanceAPIError("POST", "/fapi/v1/order", 400, -2019, "margin", params)
            with patch("bot.database.save_key_value", AsyncMock()) as save:
                self.assertTrue(await durable.record_binance_margin_rejection(engine, order, exc))
            restored = OrderRegistry()
            restored.restore(json.loads(save.call_args.args[1])["orders"])
            self.assertEqual(restored.pending_orders(), [])
            self.assertTrue(durable.can_open(engine))

            other, _ = engine.orders.get_or_create("bgx7-other", "LINKUSDT", "Buy", 19.34)
            other.transition(OrderState.SUBMITTING)
            failed, _ = engine.orders.get_or_create("bgx7-failed", "LINKUSDT", "Buy", 19.34)
            failed.transition(OrderState.SUBMITTING)
            exc = bn.BinanceAPIError("POST", "/fapi/v1/order", 400, -2019, "margin", dict(params, newClientOrderId=failed.client_oid))
            with patch("bot.database.save_key_value", AsyncMock(side_effect=RuntimeError("offline"))):
                self.assertFalse(await durable.record_binance_margin_rejection(engine, failed, exc))
            self.assertFalse(durable.can_open(engine))
            self.assertEqual(other.state, OrderState.SUBMITTING)
        run(scenario())

    def test_required_engine_interface_is_present(self):
        required = {
            "get_balance", "load_instruments", "get_instruments",
            "set_leverage", "place_order", "set_position_stops", "set_sl",
            "cancel_all_orders", "get_klines", "get_cached_klines",
            "get_ticker", "get_cached_ticker", "get_all_tickers",
            "get_open_interest", "get_funding_rate", "get_order_status",
            "wait_for_fill", "get_orderbook", "get_positions",
            "start_websocket", "start_private_websocket", "get_cache_stats",
            "sync_time", "ping", "close",
        }
        missing = sorted(name for name in required if not hasattr(bn.BinanceClient, name))
        self.assertEqual(missing, [])

    def test_exchange_info_maps_base_quantity_units(self):
        exchange_info = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "symbol": "ETHUSDT",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
            ]
        }
        client = FakeBinance({
            "/fapi/v1/time": {"serverTime": 1_800_000_000_000},
            "/fapi/v1/exchangeInfo": exchange_info,
        })
        instruments = run(client.load_instruments())
        self.assertEqual(instruments["BTCUSDT"]["multiplier"], 1.0)
        self.assertEqual(instruments["BTCUSDT"]["quantityUnit"], "BASE_ASSET")
        self.assertEqual(instruments["BTCUSDT"]["qtyStep"], 0.001)
        self.assertEqual(instruments["BTCUSDT"]["tickSize"], 0.1)

    def test_quantity_floors_to_step_and_price_aligns_to_tick(self):
        client = FakeBinance()
        client._instruments = {
            "BTCUSDT": {
                "qtyStep": 0.001,
                "minQty": 0.001,
                "tickSize": 0.1,
            }
        }
        self.assertEqual(client._round_qty(0.00199, "BTCUSDT"), "0.001")
        self.assertEqual(client._round_price(101.24, "BTCUSDT"), "101.2")
        with self.assertRaises(ValueError):
            client._round_qty(0.0009, "BTCUSDT")

    def test_positions_are_normalized_as_base_asset_and_hedge_duplicates_block(self):
        rows = [
            {
                "symbol": "BTCUSDT", "positionAmt": "0.015",
                "entryPrice": "60000", "markPrice": "60100",
                "unRealizedProfit": "1.5", "leverage": "10",
                "liquidationPrice": "52000", "isolatedMargin": "0",
                "positionSide": "BOTH", "marginType": "cross",
            }
        ]
        client = FakeBinance({"/fapi/v3/positionRisk": rows})
        positions = run(client.get_positions())
        self.assertEqual(positions[0]["size"], 0.015)
        self.assertEqual(positions[0]["sizeUnit"], "BASE_ASSET")
        self.assertEqual(positions[0]["side"], "Buy")

        client.responses["/fapi/v3/positionRisk"] = [
            rows[0],
            dict(rows[0], positionAmt="-0.010", positionSide="SHORT"),
        ]
        with self.assertRaisesRegex(RuntimeError, "HEDGE_MODE_UNSUPPORTED"):
            run(client.get_positions())

    def test_klines_are_chronological_and_normalized(self):
        raw = [
            [2_000, "2", "3", "1", "2.5", "20", 0, 0, 0, 0, 0, 0],
            [1_000, "1", "2", "0.5", "1.5", "10", 0, 0, 0, 0, 0, 0],
        ]
        client = FakeBinance({"/fapi/v1/klines": raw})
        rows = run(client.get_klines("BTCUSDT", "15", 10))
        self.assertEqual([row["ts"] for row in rows], [1_000, 2_000])
        self.assertEqual(rows[1]["v"], 20.0)

    def test_account_state_uses_margin_equity_and_available_balance(self):
        client = FakeBinance({
            "/fapi/v3/account": {
                "totalMarginBalance": "125.50",
                "totalWalletBalance": "120.00",
                "availableBalance": "80.25",
                "totalUnrealizedProfit": "5.50",
                "totalPositionInitialMargin": "30",
                "totalOpenOrderInitialMargin": "2",
            },
            "/fapi/v1/accountConfig": {"canTrade": True, "multiAssetsMargin": False},
        })
        state = run(client.get_account_state())
        self.assertEqual(state["equity"], 125.5)
        self.assertEqual(state["available"], 80.25)
        self.assertEqual(state["available_source"], "availableBalance")
        self.assertIs(state["canTrade"], True)
        self.assertIs(state["multiAssetsMargin"], False)
        self.assertIn(("GET", "/fapi/v1/accountConfig", {}, True), client.calls)

    def test_account_config_is_authoritative_over_v3_flags_and_fresh(self):
        client = FakeBinance({
            "/fapi/v3/account": {"totalMarginBalance": "6", "availableBalance": "6",
                                 "canTrade": True, "multiAssetsMargin": False},
            "/fapi/v1/accountConfig": {"canTrade": False, "multiAssetsMargin": True},
        })
        state = run(client.get_account_state())
        self.assertIs(state["canTrade"], False)
        self.assertIs(state["multiAssetsMargin"], True)
        client.responses["/fapi/v1/accountConfig"] = {"canTrade": True, "multiAssetsMargin": False}
        self.assertIs(run(client.get_account_state())["canTrade"], True)
        self.assertEqual(sum(call[1] == "/fapi/v1/accountConfig" for call in client.calls), 2)

    def test_account_config_missing_or_nonboolean_flags_fail_closed(self):
        for field in ("canTrade", "multiAssetsMargin"):
            for value in (None, "true", "false", 0, 1, [], {}):
                config = {"canTrade": True, "multiAssetsMargin": False, field: value}
                with self.subTest(field=field, value=value):
                    client = FakeBinance({"/fapi/v1/accountConfig": config})
                    with self.assertRaisesRegex(RuntimeError, "CONFIG_INVALID_" + field):
                        run(client.get_account_state())
            config = {"canTrade": True, "multiAssetsMargin": False}
            del config[field]
            with self.assertRaisesRegex(RuntimeError, "CONFIG_MISSING_" + field):
                run(FakeBinance({"/fapi/v1/accountConfig": config}).get_account_state())

    def test_account_config_failure_never_falls_back_to_v3_permission(self):
        def unavailable(*args):
            raise TimeoutError("config request failed")
        for config in (None, [], unavailable):
            with self.subTest(config=config):
                client = FakeBinance({
                    "/fapi/v3/account": {"canTrade": True, "multiAssetsMargin": False},
                    "/fapi/v1/accountConfig": config,
                })
                with self.assertRaises((RuntimeError, TimeoutError)):
                    run(client.get_account_state())

    def test_real_adapter_feeds_cross_stress_without_assuming_permission(self):
        from bot import binance_cross_portfolio_stress as stress
        from bot.config import cfg
        client = FakeBinance({
            "/fapi/v3/account": {"totalMarginBalance": "6", "totalWalletBalance": "6",
                                 "availableBalance": "6"},
            "/fapi/v1/accountConfig": {"canTrade": True, "multiAssetsMargin": False},
        })
        client.get_positions = AsyncMock(return_value=[])
        client.get_symbol_config = AsyncMock(return_value={"marginType": "CROSS"})
        client.get_leverage_brackets = AsyncMock(return_value={"brackets": [{
            "bracket": 1, "notionalFloor": 0, "notionalCap": 10000,
            "maintMarginRatio": 0.01, "initialLeverage": 125,
        }]})
        engine = SimpleNamespace(client=client, paper_trade=False, positions={})
        signal = SimpleNamespace(symbol="LINKUSDT", direction="LONG", entry=15.0, sl=14.9)
        with patch.object(cfg, "LEVERAGE", 50):
            self.assertTrue(run(stress.evaluate(engine, signal, 1.0)).allowed)
            config = client.responses["/fapi/v1/accountConfig"]
            config["canTrade"] = False
            result = run(stress.evaluate(engine, signal, 1.0))
            self.assertEqual(result.reason, "state_account_trading_disabled")
            self.assertFalse(result.allowed)
            config["canTrade"] = True
            config["multiAssetsMargin"] = True
            self.assertEqual(run(stress.evaluate(engine, signal, 1.0)).reason,
                             "state_multi_assets_margin_unsupported")
            del config["canTrade"]
            result = run(stress.evaluate(engine, signal, 1.0))
            self.assertFalse(result.allowed)
            self.assertIn("CONFIG_MISSING_canTrade", result.reason)

    def test_conditional_close_stop_is_recognized_without_contract_conversion(self):
        client = FakeBinance({
            "/fapi/v1/openAlgoOrders": [
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "algoStatus": "NEW",
                    "triggerPrice": "59000",
                    "closePosition": True,
                    "algoId": 99,
                    "clientAlgoId": "bgx7-stop",
                }
            ]
        })
        client._instruments = {
            "BTCUSDT": {
                "multiplier": 1.0, "minQty": 0.001,
                "qtyStep": 0.001, "tickSize": 0.1,
            }
        }
        position = {
            "symbol": "BTCUSDT", "side": "Buy", "size": 0.01,
            "sizeUnit": "BASE_ASSET", "entryPrice": 60000,
            "markPrice": 60010, "stopLoss": 0,
        }
        protected, evidence = run(conditional_stop_confirmed(client, position))
        self.assertTrue(protected)
        self.assertEqual(evidence, "conditional_close_order")

    def test_paper_order_is_synthetic_and_never_calls_network(self):
        client = FakeBinance()
        result = run(client.place_order(
            "BTCUSDT", "Buy", 0.01, sl=59000, tp=62000
        ))
        self.assertTrue(str(result["orderId"]).startswith("paper_"))
        self.assertTrue(result["clientOid"].startswith("bgx7-"))
        self.assertLessEqual(len(result["clientOid"]), 36)
        self.assertEqual(client.calls, [])

    def test_private_algo_update_is_cached_without_managed_order_transition(self):
        client = FakeBinance()
        client._order_registry = None
        run(client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 2001,
            "T": 2000,
            "o": {
                "caid": "bgx7-stop-btc",
                "aid": "90",
                "s": "BTCUSDT",
                "S": "SELL",
                "ps": "BOTH",
                "o": "STOP_MARKET",
                "X": "WORKING",
                "tp": "59000",
                "q": "0",
                "aq": "0",
            },
        }))
        row = client._algo_order_cache["bgx7-stop-btc"]
        self.assertEqual(row["algoId"], "90")
        self.assertEqual(row["algoStatus"], "WORKING")
        self.assertEqual(row["symbol"], "BTCUSDT")
        self.assertEqual(row["orderType"], "STOP_MARKET")
        self.assertEqual(client._client_oid_symbol["bgx7-stop-btc"], "BTCUSDT")
        self.assertEqual(client.calls, [])

    def test_private_algo_actual_order_is_correlated_with_normal_order_update(self):
        client = FakeBinance()
        run(client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 3001,
            "T": 3000,
            "o": {
                "caid": "bgx7-stop-btc",
                "aid": "90",
                "ai": "777",
                "s": "BTCUSDT",
                "S": "SELL",
                "ps": "BOTH",
                "o": "STOP_MARKET",
                "X": "TRIGGERED",
                "tp": "59000",
                "q": "0",
                "aq": "0",
            },
        }))
        run(client._handle_private_order_event({
            "e": "ORDER_TRADE_UPDATE",
            "E": 3010,
            "T": 3009,
            "o": {
                "c": "autogenerated-conditional-order",
                "i": 777,
                "s": "BTCUSDT",
                "S": "SELL",
                "q": "0.01",
                "z": "0.01",
                "ap": "58995",
                "X": "FILLED",
            },
        }))
        row = client._algo_order_cache["bgx7-stop-btc"]
        self.assertEqual(row["actualOrderId"], "777")
        self.assertEqual(row["actualOrderStatus"], "FILLED")
        self.assertEqual(row["actualExecutedQty"], "0.01")
        self.assertEqual(row["actualAvgPrice"], "58995")
        self.assertEqual(
            client._algo_actual_order_client["777"],
            "bgx7-stop-btc",
        )
        self.assertIsNone(client._order_registry)

    def test_private_algo_update_ignores_non_bgx_conditional_order(self):
        client = FakeBinance()
        run(client._handle_private_order_event({
            "e": "ALGO_UPDATE",
            "E": 4001,
            "T": 4000,
            "o": {
                "caid": "manual-stop",
                "aid": "91",
                "s": "BTCUSDT",
                "S": "SELL",
                "X": "WORKING",
            },
        }))
        self.assertEqual(client._algo_order_cache, {})
        self.assertNotIn("manual-stop", client._client_oid_symbol)

    def test_conditional_protection_retry_reuses_existing_sl_and_only_posts_tp(self):
        client = FakeBinance()
        client._instruments = {
            "BTCUSDT": {
                "multiplier": 1.0,
                "minQty": 0.001,
                "qtyStep": 0.001,
                "tickSize": 0.1,
            }
        }
        existing = [{
            "symbol": "BTCUSDT",
            "side": "sell",
            "status": "NEW",
            "type": "STOP_MARKET",
            "stopPrice": "59000",
            "closeOrder": True,
            "reduceOnly": False,
            "size": 0,
            "sizeUnit": "BASE_ASSET",
            "orderId": "90",
            "clientOid": "bgx7-existing-stop",
            "isActive": True,
        }]
        with patch.object(bn, "PAPER_TRADE", False), patch.object(
            bn, "_live_migration_ready", return_value=True
        ), patch.object(
            client, "_active_position_for_symbol",
            AsyncMock(return_value={"symbol": "BTCUSDT", "side": "Buy", "size": 0.01}),
        ), patch.object(
            client, "get_stop_orders", AsyncMock(return_value=existing)
        ), patch.object(
            client, "_post",
            AsyncMock(return_value={"algoId": 91, "clientAlgoId": "bgx7-new-tp"}),
        ) as post:
            ok = run(client.set_position_stops("BTCUSDT", sl=59000, tp=62000))

        self.assertTrue(ok)
        post.assert_awaited_once()
        endpoint, params = post.await_args.args[:2]
        self.assertEqual(endpoint, "/fapi/v1/algoOrder")
        self.assertEqual(params["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(params["triggerPrice"], "62000")

    def test_live_entry_propagates_unconfirmed_protection_to_engine(self):
        client = FakeBinance()
        client._instruments = {
            "BTCUSDT": {
                "multiplier": 1.0,
                "minQty": 0.001,
                "qtyStep": 0.001,
                "tickSize": 0.1,
            }
        }
        client._engine = SimpleNamespace(positions={})
        client._execution_ownership = object()

        with patch.object(bn, "PAPER_TRADE", False), patch.object(
            bn, "_assert_signing_credentials_available", return_value=None
        ), patch.object(
            client, "_assert_live_account_mode", AsyncMock(return_value=None)
        ), patch.object(
            client, "_post",
            AsyncMock(return_value={
                "orderId": 123,
                "clientOrderId": "bgx7-entry-protection-test",
                "status": "FILLED",
            }),
        ), patch.object(
            client, "set_position_stops", AsyncMock(return_value=False)
        ), patch(
            "bot.critical_state.critical_state.assert_available_for_new_risk",
            Mock(return_value=None),
        ), patch(
            "bot.execution_ownership.validate_execution_ownership",
            AsyncMock(return_value=True),
        ), patch(
            "bot.execution_ownership.publish_valid_execution_ownership",
            Mock(return_value=None),
        ), patch(
            "bot.runtime_readiness.assert_ready_for_new_entries",
            Mock(return_value=None),
        ):
            result = run(client.place_order(
                "BTCUSDT", "Buy", 0.01, sl=59000, tp=62000
            ))

        self.assertEqual(str(result["orderId"]), "123")
        self.assertTrue(result["sl_tp_failed"])

    def test_order_endpoints_force_single_transport_dispatch(self):
        client = FakeBinance()

        for endpoint, body, response in (
            (
                "/fapi/v1/order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "0.01",
                    "newClientOrderId": "bgx7-single-entry",
                },
                {"orderId": "123", "clientOrderId": "bgx7-single-entry"},
            ),
            (
                "/fapi/v1/algoOrder",
                {
                    "algoType": "CONDITIONAL",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "triggerPrice": "59000",
                    "closePosition": "true",
                    "clientAlgoId": "bgx7-single-stop",
                },
                {"algoId": "456", "clientAlgoId": "bgx7-single-stop"},
            ),
        ):
            with self.subTest(endpoint=endpoint), patch.object(
                client,
                "_request",
                AsyncMock(return_value=response),
            ) as request:
                result = run(
                    client._post(
                        endpoint,
                        body,
                        single_attempt=False,
                    )
                )

            self.assertEqual(result, response)
            request.assert_awaited_once_with(
                "POST",
                endpoint,
                body,
                auth=True,
                mutation=True,
                single_attempt=True,
            )

    def test_ambiguous_entry_response_recovers_by_client_order_id(self):
        client = FakeBinance()
        body = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "newClientOrderId": "bgx7-ambiguous-recover",
        }
        recovered = {
            "orderId": "12345",
            "clientOid": "bgx7-ambiguous-recover",
            "symbol": "BTCUSDT",
            "status": "FILLED",
        }
        with patch.object(
            client,
            "_request",
            AsyncMock(
                side_effect=RuntimeError(
                    "Binance POST /fapi/v1/order network failure"
                )
            ),
        ) as request, patch.object(
            client,
            "_recover_ambiguous_order",
            AsyncMock(return_value=recovered),
        ) as recover:
            result = run(
                client._post(
                    "/fapi/v1/order",
                    body,
                    single_attempt=True,
                )
            )

        request.assert_awaited_once()
        recover.assert_awaited_once_with("/fapi/v1/order", body)
        self.assertEqual(result["orderId"], "12345")
        self.assertTrue(result["recoveredByClientOid"])

    def test_duplicate_client_order_id_reconciles_instead_of_failing_clean(self):
        """Audit P1-4: -4116 proves an order with this id exists (restart/retry race)."""
        client = FakeBinance()
        body = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "newClientOrderId": "bgx7-duplicate-id",
        }
        existing = {"orderId": "777", "clientOid": "bgx7-duplicate-id",
                    "symbol": "BTCUSDT", "status": "FILLED"}
        error = RuntimeError(
            "Binance POST /fapi/v1/order HTTP 400 code=-4116 msg=ClientOrderId is duplicated."
        )
        self.assertTrue(client._ambiguous_order_submission_error(error))
        with patch.object(client, "_request", AsyncMock(side_effect=error)) as request, \
                patch.object(client, "_recover_ambiguous_order",
                             AsyncMock(return_value=existing)) as recover:
            result = run(client._post("/fapi/v1/order", body, single_attempt=True))
        request.assert_awaited_once()
        recover.assert_awaited_once_with("/fapi/v1/order", body)
        self.assertEqual(result["orderId"], "777")

    def test_unresolved_ambiguous_entry_never_requests_blind_resubmission(self):
        client = FakeBinance()
        body = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "newClientOrderId": "bgx7-ambiguous-unresolved",
        }
        with patch.object(
            client,
            "_request",
            AsyncMock(
                side_effect=RuntimeError(
                    "Binance POST /fapi/v1/order HTTP 504 "
                    "code=-1007 msg=Timeout waiting for response"
                )
            ),
        ) as request, patch.object(
            client,
            "_recover_ambiguous_order",
            AsyncMock(return_value={}),
        ) as recover:
            result = run(
                client._post(
                    "/fapi/v1/order",
                    body,
                    single_attempt=True,
                )
            )

        request.assert_awaited_once()
        recover.assert_awaited_once_with("/fapi/v1/order", body)
        self.assertTrue(result["_ambiguous"])
        self.assertEqual(result.get("orderId", ""), "")
        self.assertEqual(
            result["clientOid"],
            "bgx7-ambiguous-unresolved",
        )

    def test_definitive_binance_order_error_is_not_treated_as_ambiguous(self):
        client = FakeBinance()
        body = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "newClientOrderId": "bgx7-bad-precision",
        }
        error = RuntimeError(
            "Binance POST /fapi/v1/order HTTP 400 "
            "code=-1111 msg=Precision is over the maximum"
        )
        with patch.object(
            client, "_request", AsyncMock(side_effect=error)
        ), patch.object(
            client,
            "_recover_ambiguous_order",
            AsyncMock(return_value={}),
        ) as recover:
            with self.assertRaisesRegex(RuntimeError, "code=-1111"):
                run(
                    client._post(
                        "/fapi/v1/order",
                        body,
                        single_attempt=True,
                    )
                )

        recover.assert_not_awaited()

    def test_binance_live_migration_gate_fails_closed(self):
        client = FakeBinance()
        old = bn.PAPER_TRADE
        try:
            bn.PAPER_TRADE = False
            with patch.dict(os.environ, {"BINANCE_LIVE_MIGRATION_READY": "false"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "BINANCE_LIVE_MIGRATION_NOT_RELEASED"):
                    run(client.set_leverage("BTCUSDT", 10))
        finally:
            bn.PAPER_TRADE = old

    def test_signing_credential_gate_accepts_ed25519_without_hmac_secret(self):
        old_key = bn.API_KEY
        old_secret = bn.API_SECRET
        old_method = bn.SIGNING_METHOD
        try:
            bn.API_KEY = "public-api-key"
            bn.API_SECRET = ""
            bn.SIGNING_METHOD = "ed25519"
            with patch.object(
                bn, "_load_ed25519_private_key", return_value=object()
            ) as loader:
                bn._assert_signing_credentials_available()
                loader.assert_called_once_with()
        finally:
            bn.API_KEY = old_key
            bn.API_SECRET = old_secret
            bn.SIGNING_METHOD = old_method

    def test_signing_credential_gate_still_requires_hmac_secret(self):
        old_key = bn.API_KEY
        old_secret = bn.API_SECRET
        old_method = bn.SIGNING_METHOD
        try:
            bn.API_KEY = "public-api-key"
            bn.API_SECRET = ""
            bn.SIGNING_METHOD = "hmac"
            with self.assertRaisesRegex(
                RuntimeError, "BINANCE_API_SECRET_UNAVAILABLE"
            ):
                bn._assert_signing_credentials_available()
        finally:
            bn.API_KEY = old_key
            bn.API_SECRET = old_secret
            bn.SIGNING_METHOD = old_method

    def test_durable_order_identity_maps_rehydrate_after_restart(self):
        client = FakeBinance()
        client._instruments = {
            "BTCUSDT": {
                "multiplier": 1.0,
                "minQty": 0.001,
                "qtyStep": 0.001,
                "tickSize": 0.1,
            }
        }
        restored = client.rehydrate_order_identity_maps([
            {
                "symbol": "BTCUSDT",
                "client_oid": "bgx7-entry-restart",
                "order_id": "12345",
            }
        ])
        self.assertEqual(restored, 1)
        self.assertEqual(
            client._client_oid_symbol["bgx7-entry-restart"], "BTCUSDT"
        )
        self.assertEqual(client._order_id_symbol["12345"], "BTCUSDT")

    def test_binance_hardened_entrypoint_imports_in_paper(self):
        env = os.environ.copy()
        env.update({
            "EXCHANGE": "binance",
            "PAPER_TRADE": "true",
            "PAPER_INITIAL_BALANCE": "1000",
            "BINANCE_LIVE_MIGRATION_READY": "false",
            "LIVE_TRADING_CONFIRMED": "",
            "REAL_TRADING_PILOT": "false",
            "LOG_LEVEL": "ERROR",
        })
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import builtins, main_hardened; "
                    "assert getattr(builtins, '_nexus_sitecustomize_status', None) == 'ok'; "
                    "assert getattr(builtins, '_nexus_runtime_contract_status', None) == 'ok'; "
                    "print('binance-paper-import-ok')"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode, 0,
            msg=(proc.stdout + "\n" + proc.stderr)[-5000:],
        )
        self.assertIn("binance-paper-import-ok", proc.stdout)

    def test_funding_open_interest_and_ticker_normalization(self):
        client = FakeBinance({
            "/fapi/v1/ticker/24hr": {
                "lastPrice": "100",
                "volume": "20", "quoteVolume": "2000",
            },
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1",
            },
            "/fapi/v1/openInterest": {"openInterest": "12.5"},
            "/fapi/v1/premiumIndex": {"lastFundingRate": "0.0001"},
        })
        ticker = run(client.get_ticker("BTCUSDT"))
        oi = run(client.get_open_interest("BTCUSDT"))
        funding = run(client.get_funding_rate("BTCUSDT"))
        self.assertEqual(ticker["turnover"], 2000.0)
        self.assertEqual((ticker["bid"], ticker["ask"]), (99.9, 100.1))
        self.assertEqual(float(oi["openInterestValue"]), 1250.0)
        self.assertEqual(funding, 0.0001)

    def test_fresh_binance_ticker_reaches_microstructure_guard(self):
        from bot.pre_dispatch_guard import live_microstructure_recheck

        client = FakeBinance({
            "/fapi/v1/ticker/24hr": {"lastPrice": "100", "volume": "20", "quoteVolume": "2000"},
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT", "bidPrice": "99.99", "askPrice": "100.01",
            },
            "/fapi/v1/depth": {"bids": [["99.99", "100"]], "asks": [["100.01", "100"]]},
        })

        def check():
            return run(live_microstructure_recheck(
                client=client, instruments={"BTCUSDT": {"multiplier": 1}},
                symbol="BTCUSDT", signal_entry=100, side="BUY", qty=1,
            ))

        self.assertTrue(check().allowed)
        cached = dict(client.get_cached_ticker("BTCUSDT"))
        for book in (None, [], {}, {"symbol": "ETHUSDT", "bidPrice": "99", "askPrice": "100"}):
            with self.subTest(book=book):
                client.responses["/fapi/v1/ticker/bookTicker"] = book
                self.assertFalse(check().allowed)
                self.assertEqual(client.get_cached_ticker("BTCUSDT"), cached)
        for bid, ask in ((None, "100"), ("nan", "100"), ("99", "inf"), ("0", "100"), ("101", "100"), ("bad", "100")):
            with self.subTest(bid=bid, ask=ask):
                client.responses["/fapi/v1/ticker/bookTicker"] = {
                    "symbol": "BTCUSDT", "bidPrice": bid, "askPrice": ask,
                }
                self.assertFalse(check().allowed)
        def timeout(*args):
            raise asyncio.TimeoutError()
        client.responses["/fapi/v1/ticker/bookTicker"] = timeout
        self.assertFalse(check().allowed)
        client.responses["/fapi/v1/ticker/bookTicker"] = {
            "symbol": "BTCUSDT", "bidPrice": "99", "askPrice": "101",
        }
        self.assertIn("SPREAD_TOO_WIDE", check().blockers)
        self.assertEqual(client.get_cached_ticker("BTCUSDT")["ask"], 101)


if __name__ == "__main__":
    unittest.main()
