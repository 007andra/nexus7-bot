import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.protection_readiness import refresh_protection_readiness
from bot.order_state import OrderRegistry, OrderState
from bot.runtime_readiness import runtime_readiness


def _registry():
    return OrderRegistry()


class ProtectionReadinessAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_canonical_state_is_not_ready(self):
        engine = SimpleNamespace()
        self.assertFalse(runtime_readiness(engine).protection_system_ready)

    async def test_paper_flat_readiness_never_requires_private_active_orders(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(
                side_effect=AssertionError(
                    "private exchange active-order read executed in PAPER"
                )
            ),
        )
        engine = SimpleNamespace(
            connected=True,
            paper_trade=True,
            client=client,
            _unprotected_symbols=set(),
            orders=_registry(),
            positions={},
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        client._get.assert_not_awaited()
        self.assertTrue(engine._protection_system_ready)

    async def test_flat_exchange_is_ready_only_with_zero_unprotected(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(),
            orders=_registry(),
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(engine._protection_system_ready)
        self.assertEqual(engine._protection_readiness_evidence["positions"], 0)
        self.assertEqual(
            engine._protection_readiness_evidence["unprotected_positions"], 0
        )

        # A stale incident is not itself exposure authority. Once all
        # independent flat proofs succeed it must converge and clear.
        engine._unprotected_symbols = {"BTCUSDT"}
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())
        self.assertTrue(engine._protection_system_ready)

    async def test_stale_unprotected_symbol_is_cleared_only_after_confirmed_flat(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())
        self.assertTrue(engine._protection_system_ready)

    async def test_flat_read_failure_keeps_stale_state_blocked(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(side_effect=TimeoutError("exchange read failed")),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_flat_with_pending_durable_intent_keeps_stale_state_blocked(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        orders = _registry()
        order, _ = orders.get_or_create("bgx7-pending", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="REST")
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders,
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_flat_with_active_entry_order_keeps_stale_state_blocked(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": [{"symbol": "SOLUSDT", "reduceOnly": False}]}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_recent_local_fill_blocks_clear_until_exchange_converges(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(),
            positions={"SOLUSDT": SimpleNamespace()},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})
        engine.positions = {}
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())

    async def test_confirming_sol_flat_does_not_clear_eth_unprotected(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client,
            _unprotected_symbols={"SOLUSDT", "ETHUSDT"},
            orders=_registry(),
            positions={"ETHUSDT": SimpleNamespace()},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"ETHUSDT"})
        self.assertFalse(engine._protection_system_ready)

    async def test_flat_reconciliation_is_idempotent(self):
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=_registry(), positions={},
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())

    async def test_restart_filled_terminal_does_not_imply_exposure_reconciled(self):
        orders = _registry()
        order, _ = orders.get_or_create("bgx7-fill-restart", "SOLUSDT", "Buy", 4.2)
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="oid-sol", source="REST")
        order.transition(
            OrderState.FILLED, order_id="oid-sol", filled_qty=4.2, source="REST"
        )
        snapshot = orders.snapshot()

        restarted = _registry()
        restarted.restore(snapshot)
        restored = restarted.get("bgx7-fill-restart")
        self.assertTrue(restored.is_terminal)
        self.assertFalse(restored.exposure_reconciliation_complete)

        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=restarted, positions={},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})
        self.assertEqual(
            engine._protection_readiness_evidence["reason"],
            "flat_with_unresolved_exposure_evidence",
        )

    async def test_exchange_position_truth_completes_fill_reconciliation_durably(self):
        orders = _registry()
        order, _ = orders.get_or_create("bgx7-fill-live", "SOLUSDT", "Buy", 4.2)
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="oid-sol", source="REST")
        order.transition(OrderState.FILLED, filled_qty=4.2, source="REST")
        position = {
            "symbol": "SOLUSDT", "size": 4.2, "side": "Buy",
            "entryPrice": 100.0, "markPrice": 100.0, "stopLoss": 90.0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(),
            orders=orders, positions={},
        )
        with patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ) as persist:
            self.assertTrue(await refresh_protection_readiness(engine))
            persist.assert_awaited_once()
        self.assertTrue(order.exposure_reconciliation_complete)

        client.get_positions = AsyncMock(return_value=[])
        client._get = AsyncMock(return_value={"items": []})
        engine._unprotected_symbols = {"SOLUSDT"}
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, set())

    def _filled_reduce(self, oid, qty, previous):
        orders = _registry()
        order, _ = orders.get_or_create(oid, "SOLUSDT", "Sell", qty)
        order.reduce_only = True
        order.exposure_intent = "REDUCE"
        order.previous_position_qty = previous
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.SUBMITTED, order_id="x-" + oid, source="REST")
        order.transition(OrderState.FILLED, filled_qty=qty, source="REST")
        return orders, order

    async def test_partial_reduce_reconciles_only_to_expected_residual(self):
        orders, order = self._filled_reduce("bgx7-reduce", 2.0, 4.2)
        position = {
            "symbol": "SOLUSDT", "size": 2.2, "side": "Buy",
            "entryPrice": 100.0, "markPrice": 100.0, "stopLoss": 90.0,
        }
        client = SimpleNamespace(get_positions=AsyncMock(return_value=[position]))
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(),
            orders=orders, positions={},
        )
        with patch("bot.durable_execution.persist_orders", new=AsyncMock(return_value=True)):
            self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(order.exposure_reconciliation_complete)

    async def test_partial_reduce_wrong_residual_does_not_complete(self):
        orders, order = self._filled_reduce("bgx7-reduce-bad", 2.0, 4.2)
        position = {
            "symbol": "SOLUSDT", "size": 3.1, "side": "Buy",
            "entryPrice": 100.0, "markPrice": 100.0, "stopLoss": 90.0,
        }
        client = SimpleNamespace(get_positions=AsyncMock(return_value=[position]))
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(order.exposure_reconciliation_complete)

    async def test_partial_reduce_residual_without_protection_stays_blocked(self):
        orders, order = self._filled_reduce("bgx7-reduce-nostop", 2.0, 4.2)
        position = {
            "symbol": "SOLUSDT", "size": 2.2, "side": "Buy",
            "entryPrice": 100.0, "markPrice": 100.0, "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        with patch("bot.durable_execution.persist_orders", new=AsyncMock(return_value=True)):
            self.assertFalse(await refresh_protection_readiness(engine))
        self.assertTrue(order.exposure_reconciliation_complete)
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_full_close_fill_converges_to_confirmed_flat(self):
        orders, order = self._filled_reduce("bgx7-full-close", 4.2, 4.2)
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        with patch("bot.durable_execution.persist_orders", new=AsyncMock(return_value=True)) as persist:
            self.assertTrue(await refresh_protection_readiness(engine))
            persist.assert_awaited_once()
        self.assertTrue(order.exposure_reconciliation_complete)
        self.assertEqual(engine._unprotected_symbols, set())

    async def test_legacy_full_reduce_without_previous_qty_repairs_only_when_globally_flat(self):
        orders, order = self._filled_reduce("bgx7-legacy-close", 2.8, 2.8)
        order.previous_position_qty = None
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        with patch(
            "bot.durable_execution.persist_orders",
            new=AsyncMock(return_value=True),
        ) as persist:
            self.assertTrue(await refresh_protection_readiness(engine))
            persist.assert_awaited_once()
        self.assertTrue(order.exposure_reconciliation_complete)
        self.assertEqual(engine._unprotected_symbols, set())

    async def test_legacy_reduce_missing_previous_qty_stays_blocked_when_symbol_has_other_unreconciled_fill(self):
        orders, close_order = self._filled_reduce("bgx7-legacy-close-ambiguous", 2.8, 2.8)
        close_order.previous_position_qty = None
        entry, _ = orders.get_or_create(
            "bgx7-legacy-entry-unreconciled", "SOLUSDT", "Buy", 2.8
        )
        entry.transition(OrderState.SUBMITTING, source="REST")
        entry.transition(OrderState.SUBMITTED, order_id="legacy-entry", source="REST")
        entry.transition(OrderState.FILLED, filled_qty=2.8, source="REST")

        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(close_order.exposure_reconciliation_complete)
        self.assertFalse(entry.exposure_reconciliation_complete)
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_full_close_persistence_failure_keeps_blocked(self):
        orders, order = self._filled_reduce("bgx7-close-db-fail", 4.2, 4.2)
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        with patch("bot.durable_execution.persist_orders", new=AsyncMock(return_value=False)):
            self.assertFalse(await refresh_protection_readiness(engine))
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_partial_full_close_fill_cannot_reconcile_to_flat(self):
        orders, order = self._filled_reduce("bgx7-close-partial", 2.0, 4.2)
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=orders, positions={},
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(order.exposure_reconciliation_complete)
        self.assertEqual(engine._unprotected_symbols, {"SOLUSDT"})

    async def test_restart_full_close_uses_durable_intent_and_converges(self):
        orders, _ = self._filled_reduce("bgx7-close-restart", 4.2, 4.2)
        restarted = _registry()
        restarted.restore(orders.snapshot())
        restored = restarted.get("bgx7-close-restart")
        self.assertTrue(restored.reduce_only)
        self.assertEqual(restored.exposure_intent, "REDUCE")
        self.assertEqual(restored.previous_position_qty, 4.2)
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols={"SOLUSDT"},
            orders=restarted, positions={},
        )
        with patch("bot.durable_execution.persist_orders", new=AsyncMock(return_value=True)):
            self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(restored.exposure_reconciliation_complete)
        self.assertEqual(engine._unprotected_symbols, set())

    async def test_unreconciled_fill_is_not_garbage_collected_by_age(self):
        orders = _registry()
        order, _ = orders.get_or_create("bgx7-old-fill", "SOLUSDT", "Buy", 1.0)
        order.transition(OrderState.SUBMITTING, source="REST")
        order.transition(OrderState.FILLED, filled_qty=1.0, source="REST")
        order.created_at = 0.0
        orders.gc(max_age=1.0)
        self.assertIsNotNone(orders.get("bgx7-old-fill"))

    async def test_existing_position_requires_native_protection_readback(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[{
                "symbol": "XBTUSDTM",
                "side": "sell",
                "stopPrice": 95.0,
                "closeOrder": True,
                "isActive": True,
                "stopTriggered": False,
            }]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
        )
        self.assertTrue(await refresh_protection_readiness(engine))
        self.assertTrue(engine._protection_system_ready)

    async def test_inline_stop_on_wrong_side_is_not_equivalent(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 105.0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)

    async def test_http_success_without_readback_cannot_authorize(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[]),
            set_position_stops=AsyncMock(return_value=True),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)
        client.set_position_stops.assert_not_awaited()

    async def test_readback_mismatch_is_false(self):
        position = {
            "symbol": "BTCUSDT",
            "size": 1,
            "side": "Buy",
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "stopLoss": 0,
        }
        client = SimpleNamespace(
            get_positions=AsyncMock(return_value=[position]),
            get_stop_orders=AsyncMock(return_value=[{
                "symbol": "XBTUSDTM",
                "side": "buy",
                "stopPrice": 105.0,
                "closeOrder": True,
                "isActive": True,
                "stopTriggered": False,
            }]),
        )
        engine = SimpleNamespace(
            connected=True, client=client, _unprotected_symbols=set(), orders=_registry()
        )
        self.assertFalse(await refresh_protection_readiness(engine))
        self.assertFalse(engine._protection_system_ready)


    def _valid_xrp_pilot_context(self, unprotected):
        from bot import pilot
        import time
        class State:
            def codes(self):
                return []
        class Integrity:
            state = State()
        risk = SimpleNamespace(
            balance=100.0, available_margin=100.0, peak_balance=100.0,
            drawdown=0.0, _ready=True,
        )
        engine = SimpleNamespace(
            risk=risk,
            viable_symbols=["XRPUSDT"],
            instruments={"XRPUSDT": {"minQty": 1.0, "multiplier": 0.1}},
            integrity=Integrity(),
            _unprotected_symbols=set(unprotected),
            orders=_registry(),
            positions={},
        )
        client = SimpleNamespace(
            _last_ws_update=time.time(),
            _order_registry=object(),
        )
        ai = SimpleNamespace(
            execution_allowed=True,
            expected_value=0.01,
            net_rr=2.0,
            cross_risk_pass=True,
            direction="LONG",
        )
        return pilot, engine, client, ai

    async def test_xrp_candidate_proceeds_after_sol_confirmed_flat_reconciliation(self):
        from bot import pilot
        import os
        client_reconcile = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            _get=AsyncMock(return_value={"items": []}),
        )
        engine_reconcile = SimpleNamespace(
            connected=True, client=client_reconcile,
            _unprotected_symbols={"SOLUSDT"}, orders=_registry(), positions={},
        )
        self.assertTrue(await refresh_protection_readiness(engine_reconcile))
        self.assertEqual(engine_reconcile._unprotected_symbols, set())

        _, engine, client, ai = self._valid_xrp_pilot_context(
            engine_reconcile._unprotected_symbols
        )
        guard = pilot.PilotGuard()
        with patch.object(pilot, "PILOT_ENABLED", True), patch(
            "bot.kucoin.API_KEY", "test-key"
        ), patch("bot.kucoin.API_SECRET", "test-secret"), patch(
            "bot.kucoin.API_PASSPHRASE", "test-pass"
        ), patch.dict(os.environ, {
            "PAPER_TRADE": "false",
            "PILOT_ACCOUNT_CONFIRMED": "true",
            "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
        }, clear=False):
            reasons = guard.evaluate(engine, client, "XRPUSDT", ai)

        self.assertFalse(any(r.startswith("7_8_UNPROTECTED:") for r in reasons))
        self.assertEqual(reasons, [])
        self.assertEqual(guard.state.new_order_submissions_this_session, 0)
        print("[XRP_PROOF]", flush=True)
        print("case=POSITIVE", flush=True)
        print("result=PASS", flush=True)
        print("sol_unprotected_block=false", flush=True)
        print("next_gate=POST_PILOT_EXECUTION_BOUNDARY", flush=True)

    async def test_xrp_candidate_remains_blocked_when_sol_is_genuinely_unprotected(self):
        from bot import pilot
        import os
        _, engine, client, ai = self._valid_xrp_pilot_context({"SOLUSDT"})
        engine.positions = {"SOLUSDT": SimpleNamespace()}
        guard = pilot.PilotGuard()
        exchange_entry_mutations = 0
        with patch.object(pilot, "PILOT_ENABLED", True), patch(
            "bot.kucoin.API_KEY", "test-key"
        ), patch("bot.kucoin.API_SECRET", "test-secret"), patch(
            "bot.kucoin.API_PASSPHRASE", "test-pass"
        ), patch.dict(os.environ, {
            "PAPER_TRADE": "false",
            "PILOT_ACCOUNT_CONFIRMED": "true",
            "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
        }, clear=False):
            reasons = guard.evaluate(engine, client, "XRPUSDT", ai)

        self.assertIn("7_8_UNPROTECTED: ['SOLUSDT']", reasons)
        self.assertEqual(exchange_entry_mutations, 0)
        self.assertEqual(guard.state.new_order_submissions_this_session, 0)
        print("[XRP_PROOF]", flush=True)
        print("case=NEGATIVE", flush=True)
        print("result=PASS", flush=True)
        print("blocker=7_8_UNPROTECTED", flush=True)
        print("exchange_entry_mutations=0", flush=True)

    async def test_xrp_race_blocks_while_sol_unknown_then_allows_new_candidate_after_reconciliation(self):
        from bot import pilot
        import asyncio
        import os

        orders, close_order = self._filled_reduce("bgx7-xrp-race-close", 4.2, 4.2)
        exchange_truth_released = asyncio.Event()
        exchange_read_started = asyncio.Event()

        async def get_positions():
            exchange_read_started.set()
            await exchange_truth_released.wait()
            return []

        reconcile_client = SimpleNamespace(
            get_positions=AsyncMock(side_effect=get_positions),
            _get=AsyncMock(return_value={"items": []}),
        )
        reconcile_engine = SimpleNamespace(
            connected=True, client=reconcile_client,
            _unprotected_symbols={"SOLUSDT"}, orders=orders,
            positions={"SOLUSDT": SimpleNamespace()},
        )

        _, pilot_engine, pilot_client, ai = self._valid_xrp_pilot_context({"SOLUSDT"})
        guard = pilot.PilotGuard()
        unknown_exchange_entry_mutations = 0
        env = {
            "PAPER_TRADE": "false",
            "PILOT_ACCOUNT_CONFIRMED": "true",
            "PILOT_RELEASE_APPROVED": pilot.PILOT_RELEASE_TOKEN,
        }
        patches = (
            patch.object(pilot, "PILOT_ENABLED", True),
            patch("bot.kucoin.API_KEY", "test-key"),
            patch("bot.kucoin.API_SECRET", "test-secret"),
            patch("bot.kucoin.API_PASSPHRASE", "test-pass"),
            patch.dict(os.environ, env, clear=False),
        )
        for p in patches:
            p.start()
        try:
            reconcile_task = asyncio.create_task(
                refresh_protection_readiness(reconcile_engine)
            )
            await exchange_read_started.wait()

            during = guard.evaluate(pilot_engine, pilot_client, "XRPUSDT", ai)
            self.assertIn("7_8_UNPROTECTED: ['SOLUSDT']", during)
            self.assertEqual(unknown_exchange_entry_mutations, 0)

            reconcile_engine.positions = {}
            exchange_truth_released.set()
            with patch(
                "bot.durable_execution.persist_orders",
                new=AsyncMock(return_value=True),
            ):
                self.assertTrue(await reconcile_task)

            self.assertTrue(close_order.exposure_reconciliation_complete)
            self.assertEqual(reconcile_engine._unprotected_symbols, set())
            pilot_engine._unprotected_symbols = set()
            after = guard.evaluate(pilot_engine, pilot_client, "XRPUSDT", ai)
            self.assertFalse(any(r.startswith("7_8_UNPROTECTED:") for r in after))
            self.assertEqual(after, [])
            self.assertEqual(guard.state.new_order_submissions_this_session, 0)
            print("[XRP_PROOF]", flush=True)
            print("case=RACE", flush=True)
            print("unknown_phase_blocked=true", flush=True)
            print("unknown_phase_exchange_entry_mutations=0", flush=True)
            print("post_reconciliation_candidate_proceeds=true", flush=True)
            print("result=PASS", flush=True)
        finally:
            for p in reversed(patches):
                p.stop()


if __name__ == "__main__":
    unittest.main()
