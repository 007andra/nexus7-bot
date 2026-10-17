import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import main
import main_hardened
from bot.binance import BinanceClient
from bot.nexus_runtime_engine import TradingEngine
from bot.runtime_readiness import runtime_readiness


def _raw_exchange_client(engine):
    client = engine.client
    return getattr(client, "_client", client)


def _make_canonical_engine():
    raw = BinanceClient()
    engine = TradingEngine(raw)
    return raw, engine


def _set_all_ready(engine):
    engine.instruments = {"BTCUSDT": {"symbol": "BTCUSDT"}}
    engine._durable_state_ok = True
    engine._financial_state_sane = True
    engine._initial_reconciliation_complete = True
    engine._execution_ownership_valid = True
    engine._execution_ownership_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    engine.connected = True
    engine.viable_symbols = ["BTCUSDT"]
    engine._market_data_ready = True
    engine._protection_system_ready = True


async def _ready():
    response = await main_hardened.readiness()
    return response.status_code, response.body.decode("utf-8")


class CanonicalHttpReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = getattr(main.app.state, "engine", None)
        self.previous_ready = getattr(main.app.state, "ready", None)
        os.environ["EXECUTION_CAPABILITY"] = "LIVE"
        main_hardened._http_readiness_last_signature = None

    async def asyncTearDown(self):
        main.app.state.engine = self.previous
        if self.previous_ready is not None:
            main.app.state.ready = self.previous_ready

    async def test_http_readiness_fails_when_canonical_engine_unavailable(self):
        main.app.state.engine = None
        status, body = await _ready()
        self.assertEqual(status, 503)
        self.assertIn("engine_unavailable", body)

    async def test_http_and_execution_boundary_share_engine_authority(self):
        raw, engine = _make_canonical_engine()
        main.app.state.engine = engine
        self.assertIs(main.app.state.engine, engine)
        self.assertIs(_raw_exchange_client(engine), raw)
        self.assertIs(raw._engine, engine)
        self.assertIsInstance(main.app.state.engine, TradingEngine)

    async def test_service_ready_does_not_authorize_financial_execution(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        main.app.state.ready = True
        main.app.state.blocked = False
        main.app.state.engine_task = asyncio.create_task(asyncio.sleep(3600))
        engine._running = True
        engine._durable_state_enforced = True
        engine._durable_state_ok = True
        engine.instruments = {"BTCUSDT": {"symbol": "BTCUSDT"}}
        engine._execution_ownership_valid = False
        engine._execution_ownership_expires_at = None
        try:
            service = await main_hardened.service_readiness()
            financial = await main_hardened.readiness()
            self.assertEqual(service.status_code, 200)
            self.assertEqual(financial.status_code, 503)
            service_payload = json.loads(service.body)
            self.assertFalse(service_payload["financial_ready"])
        finally:
            main.app.state.engine_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await main.app.state.engine_task

    async def test_service_ready_requires_durable_initialization(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        main.app.state.ready = True
        main.app.state.blocked = False
        main.app.state.engine_task = asyncio.create_task(asyncio.sleep(3600))
        engine._running = True
        engine.instruments = {"BTCUSDT": {"symbol": "BTCUSDT"}}
        engine._durable_state_enforced = False
        engine._durable_state_ok = True
        try:
            response = await main_hardened.service_readiness()
            self.assertEqual(response.status_code, 503)
            self.assertIn("durable_state_unhealthy", response.body.decode("utf-8"))
        finally:
            main.app.state.engine_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await main.app.state.engine_task

    async def test_service_ready_route_uses_railway_compatible_path(self):
        paths = {route.path for route in main_hardened.app.routes}
        self.assertIn("/service_ready", paths)
        self.assertNotIn("/service-ready", paths)

    async def test_startup_incomplete_is_503_even_when_engine_is_published(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        status, _ = await _ready()
        self.assertEqual(status, 503)
        self.assertFalse(runtime_readiness(engine).ready_for_new_entries)

    async def test_durable_ready_cannot_substitute_for_initial_reconciliation(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)

        # Exact BGX-READY-002 pre-fix reproduction: durable DB health alone
        # must never stand in for startup reconciliation proof.
        engine._durable_state_ok = True
        engine._initial_reconciliation_complete = False
        status, body = await _ready()
        payload = json.loads(body)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready_for_new_entries"])
        self.assertIn("initial_reconciliation_complete", payload["blockers"])

        # Once the canonical reconciliation authority has completed, readiness
        # may converge if every other independent gate remains true.
        engine._initial_reconciliation_complete = True
        status, body = await _ready()
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready_for_new_entries"])
        self.assertEqual(payload["blockers"], [])

    async def test_canonical_engine_startup_binds_http_readiness(self):
        raw, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)
        self.assertIs(raw._engine, main.app.state.engine)
        status, body = await _ready()
        self.assertEqual(status, 200)
        self.assertIn('"ready":true', body)
        self.assertTrue(runtime_readiness(engine).ready_for_new_entries)

    async def test_http_readiness_tracks_ownership_loss_and_recovery(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)
        self.assertEqual((await _ready())[0], 200)
        engine._execution_ownership_valid = False
        engine._execution_ownership_expires_at = None
        self.assertEqual((await _ready())[0], 503)
        engine._execution_ownership_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        engine._execution_ownership_valid = True
        self.assertEqual((await _ready())[0], 200)

    async def test_false_200_matrix(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        cases = (
            ("_execution_ownership_valid", False),
            ("connected", False),
            ("_durable_state_ok", False),
            ("_financial_state_sane", False),
        )
        for field, value in cases:
            with self.subTest(field=field):
                _set_all_ready(engine)
                setattr(engine, field, value)
                self.assertEqual((await _ready())[0], 503)

    async def test_reconciliation_and_protection_values_reach_snapshot(self):
        _, engine = _make_canonical_engine()
        _set_all_ready(engine)
        engine._initial_reconciliation_complete = False
        engine._protection_system_ready = False
        snap = runtime_readiness(engine)
        self.assertFalse(snap.initial_reconciliation_complete)
        self.assertFalse(snap.protection_system_ready)

    async def test_shutdown_invalidates_published_authority_and_restart_uses_new_engine(self):
        raw_a, engine_a = _make_canonical_engine()
        main.app.state.engine = engine_a
        _set_all_ready(engine_a)
        self.assertEqual((await _ready())[0], 200)

        engine_a._execution_ownership_valid = False
        engine_a._execution_ownership_expires_at = None
        main.app.state.ready = False
        main.app.state.engine = None
        engine_a.stop()
        self.assertEqual((await _ready())[0], 503)

        raw_b, engine_b = _make_canonical_engine()
        main.app.state.engine = engine_b
        _set_all_ready(engine_b)
        self.assertIsNot(engine_a, engine_b)
        self.assertIs(main.app.state.engine, engine_b)
        self.assertIs(raw_b._engine, engine_b)
        self.assertEqual((await _ready())[0], 200)

    async def test_http_readiness_observability_single_blocker(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)
        engine._execution_ownership_valid = False
        engine._execution_ownership_expires_at = None
        with patch.object(main_hardened.log, "warning") as warning:
            status, body = await _ready()
        payload = json.loads(body)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready_for_new_entries"])
        self.assertEqual(payload["blockers"], ["execution_ownership_valid"])
        http_calls = [call for call in warning.call_args_list if "[HTTP_READINESS]" in str(call)]
        self.assertEqual(len(http_calls), 1)
        self.assertEqual(http_calls[0].args[-1], ["execution_ownership_valid"])

    async def test_http_readiness_observability_multiple_blockers(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)
        engine._durable_state_ok = False
        engine._market_data_ready = False
        engine._execution_ownership_valid = False
        engine._execution_ownership_expires_at = None
        status, body = await _ready()
        payload = json.loads(body)
        self.assertEqual(status, 503)
        self.assertEqual(payload["blockers"], ["critical_database_ready", "execution_ownership_valid", "market_data_ready"])
        self.assertTrue(payload["instruments_ready"])
        self.assertTrue(payload["financial_state_sane"])
        self.assertTrue(payload["exchange_ready"])

    async def test_http_readiness_observability_ready_and_http_snapshot_consistency(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)
        with patch.object(main_hardened.log, "info") as info:
            status, body = await _ready()
        payload = json.loads(body)
        snap = runtime_readiness(engine)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready_for_new_entries"])
        self.assertEqual(payload["blockers"], [])
        self.assertTrue(snap.ready_for_new_entries)
        self.assertEqual(status == 200, snap.ready_for_new_entries)
        http_calls = [call for call in info.call_args_list if "[HTTP_READINESS]" in str(call)]
        self.assertEqual(len(http_calls), 1)
        engine.connected = False
        status, body = await _ready()
        payload = json.loads(body)
        snap = runtime_readiness(engine)
        self.assertEqual(status, 503)
        self.assertFalse(payload["ready_for_new_entries"])
        self.assertEqual(status == 200, snap.ready_for_new_entries)
        self.assertEqual(payload["blockers"], ["exchange_ready"])

    async def test_http_readiness_transition_logging_deduplicates_identical_state(self):
        _, engine = _make_canonical_engine()
        main.app.state.engine = engine
        _set_all_ready(engine)
        engine.connected = False
        with patch.object(main_hardened.log, "warning") as warning:
            self.assertEqual((await _ready())[0], 503)
            self.assertEqual((await _ready())[0], 503)
        http_calls = [call for call in warning.call_args_list if "[HTTP_READINESS]" in str(call)]
        self.assertEqual(len(http_calls), 1)
        engine._market_data_ready = False
        with patch.object(main_hardened.log, "warning") as warning_changed:
            self.assertEqual((await _ready())[0], 503)
        http_changed = [call for call in warning_changed.call_args_list if "[HTTP_READINESS]" in str(call)]
        self.assertEqual(len(http_changed), 1)
    async def test_real_lifespan_publishes_the_exact_engine_created_for_execution(self):
        created = {}
        real_engine_cls = main.TradingEngine

        class CapturingEngine(real_engine_cls):
            def __init__(self, client):
                super().__init__(client)
                created["engine"] = self

            async def run(self):
                self._running = True
                try:
                    await asyncio.Event().wait()
                finally:
                    self._running = False

        with patch.object(main, "TradingEngine", CapturingEngine), \
             patch.object(main.ExchangeClient, "load_instruments", AsyncMock(return_value=None)), \
             patch("bot.notifier.test_telegram", AsyncMock(return_value={"ok": True})), \
             patch("bot.notifier.notify", AsyncMock()):
            async with main.lifespan(main.app):
                await asyncio.sleep(0)
                engine = created["engine"]
                self.assertIs(main.app.state.engine, engine)
                self.assertIs(_raw_exchange_client(engine)._engine, engine)
                self.assertIsInstance(engine, real_engine_cls)
            self.assertIsNone(main.app.state.engine)
            self.assertFalse(engine._execution_ownership_valid)


if __name__ == "__main__":
    unittest.main()
