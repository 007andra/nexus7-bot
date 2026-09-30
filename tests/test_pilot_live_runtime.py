import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import pilot_live_runtime as live
from bot.engine import TradingEngine
from bot.financial_state import validate_financial_state


class _Risk:
    def __init__(self):
        self._ready = False
        self.balance = 0.0
        self.peak_balance = 0.0
        self.drawdown = 0.0
        self.balance_confirmed = False

    def init(self, value):
        self._ready = True
        self.balance = float(value)
        self.peak_balance = float(value)
        self.drawdown = 0.0

    def update(self, value):
        value = float(value)
        self.balance = value
        self.peak_balance = max(self.peak_balance, value)
        self.drawdown = (
            (self.peak_balance - value) / self.peak_balance
            if self.peak_balance > 0 else 0.0
        )


class _Integrity:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.assess = AsyncMock(return_value=None)

    def can_open_new(self):
        return self.allowed

    def block_reason(self):
        return "blocked"


class _Log:
    info = staticmethod(lambda *a, **k: None)
    warning = staticmethod(lambda *a, **k: None)
    critical = staticmethod(lambda *a, **k: None)


class PilotLiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_uses_equity_for_risk_and_tracks_available_separately(self):
        engine = SimpleNamespace(
            client=SimpleNamespace(),
            risk=_Risk(),
        )
        state = {
            "equity": 100.0,
            "available": 25.0,
            "available_source": "availableMargin",
            "accountEquity": "100",
            "availableMargin": "25",
        }
        with patch.object(
            live.account_semantics, "read_account_state", AsyncMock(return_value=state)
        ), patch.object(
            live.capital_flows,
            "reconcile_external_capital_flows",
            AsyncMock(return_value={"applied": 0, "bootstrap": False}),
        ) as reconcile, patch.object(
            live.hwm_incident_repair,
            "repair_if_needed",
            AsyncMock(return_value={"status": "NOT_MATCHED"}),
        ) as repair, patch.object(
            live, "restore_update_real_account_peak", AsyncMock(return_value=None)
        ):
            out = await live._refresh_account(engine, _Log())

        self.assertIs(out, state)
        reconcile.assert_awaited_once_with(engine.client, engine.risk, 100.0, strict=True)
        repair.assert_awaited_once_with(engine.risk, 100.0, strict=True)
        self.assertEqual(engine.risk.balance, 100.0)
        self.assertEqual(engine._pilot_account_equity, 100.0)
        self.assertEqual(engine._pilot_available_balance, 25.0)
        self.assertEqual(
            engine.client._last_account_overview_snapshot["availableMargin"], "25"
        )
        self.assertIn("_observed_at", engine.client._last_account_overview_snapshot)

    async def test_zero_equity_keeps_readonly_runtime_alive_but_capital_unconfirmed(self):
        engine = SimpleNamespace(
            client=SimpleNamespace(),
            risk=_Risk(),
        )
        engine.risk.init(7.4078)
        state = {
            "equity": 0.0,
            "available": 0.0,
            "available_source": "availableBalance",
            "accountEquity": "0",
            "availableBalance": "0",
        }

        async def preserve_zero(risk, strict=True):
            self.assertTrue(strict)
            risk.balance = 0.0
            risk.peak_balance = 8.8015
            risk.drawdown = 1.0
            risk.balance_confirmed = False
            return 8.8015

        with patch.object(
            live.account_semantics,
            "read_account_state",
            AsyncMock(return_value=state),
        ), patch.object(
            live,
            "restore_zero_equity_peak_fail_closed",
            AsyncMock(side_effect=preserve_zero),
        ) as preserve, patch.object(
            live.capital_flows,
            "reconcile_external_capital_flows",
            AsyncMock(),
        ) as reconcile, patch.object(
            live.external_performance,
            "evaluate",
            AsyncMock(),
        ) as external:
            out = await live._refresh_account(engine, _Log())

        self.assertIs(out, state)
        preserve.assert_awaited_once_with(engine.risk, strict=True)
        reconcile.assert_not_awaited()
        external.assert_not_awaited()
        self.assertEqual(engine._pilot_account_equity, 0.0)
        self.assertEqual(engine._pilot_available_balance, 0.0)
        self.assertEqual(engine.risk.drawdown, 1.0)
        self.assertFalse(engine.risk.balance_confirmed)
        self.assertTrue(
            engine.client._last_account_overview_snapshot[
                "_zero_equity_fail_closed"
            ]
        )

    async def test_preflight_block_prevents_original_open(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.integrity = _Integrity()
                self.instruments = {"BTCUSDT": {}}

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                self.original_open_called = True
                return "opened"

        live.install(Engine, _Log())
        engine = Engine()
        engine.original_open_called = False
        with patch.object(
            live, "_run_readonly_preflight", AsyncMock(return_value=False)
        ):
            result = await engine._open(SimpleNamespace(symbol="BTCUSDT"))

        self.assertIsNone(result)
        self.assertFalse(engine.original_open_called)
        engine.integrity.assess.assert_not_awaited()

    async def test_integrity_block_prevents_original_open(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.integrity = _Integrity(allowed=False)
                self.instruments = {"BTCUSDT": {}}

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                self.original_open_called = True
                return "opened"

        live.install(Engine, _Log())
        engine = Engine()
        engine.original_open_called = False
        with patch.object(
            live, "_run_readonly_preflight", AsyncMock(return_value=True)
        ):
            result = await engine._open(SimpleNamespace(symbol="BTCUSDT"))

        self.assertIsNone(result)
        self.assertFalse(engine.original_open_called)
        engine.integrity.assess.assert_awaited_once()

    async def test_entry_refresh_keeps_equity_authoritative_during_open(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.risk.init(100.0)
                self.integrity = _Integrity()
                self.instruments = {"BTCUSDT": {}}
                self._pilot_open_in_progress = True
                self._pilot_available_balance = 25.0

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                return None

        live.install(Engine, _Log())
        engine = Engine()
        state = {
            "equity": 100.0,
            "available": 25.0,
            "available_source": "availableMargin",
        }
        with patch.object(
            live, "_refresh_account", AsyncMock(return_value=state)
        ):
            ok = await engine._refresh_entry_balance()

        self.assertTrue(ok)
        self.assertEqual(engine.risk.balance, 100.0)
        self.assertEqual(engine._pilot_available_balance, 25.0)
        self.assertTrue(engine.risk.balance_confirmed)

    async def test_historical_alias_transition_is_eliminated_at_source(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace()
                self.risk = _Risk()
                self.risk._ready = True
                self.risk.balance = 21.5075351411
                self.risk.peak_balance = 63.7942573
                self.risk.drawdown = 0.6628609525155488
                self.integrity = _Integrity()
                self.instruments = {"DOGEUSDT": {}}
                self._pilot_open_in_progress = True
                self._pilot_available_balance = 11.9815151411

            async def _connect(self):
                self.connected = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                return None

        live.install(Engine, _Log())
        engine = Engine()
        state = {
            "equity": 21.5075351411,
            "available": 11.9815151411,
            "available_source": "availableMargin",
        }
        with patch.object(
            live, "_refresh_account", AsyncMock(return_value=state)
        ):
            ok = await engine._refresh_entry_balance()

        # The fixture's durable drawdown is 66.3% (>= MAX_DRAWDOWN), so the
        # pre-dispatch hard gate added by audit P0-7 blocks the entry. The
        # equity/available semantics under test are unchanged.
        self.assertFalse(ok)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)
        self.assertAlmostEqual(engine._pilot_available_balance, 11.9815151411)
        validate_financial_state(
            equity=engine.risk.balance,
            available_margin=engine._pilot_available_balance,
            hwm=engine.risk.peak_balance,
            drawdown=engine.risk.drawdown,
        )

    def test_core_affordability_uses_pilot_collateral_without_relabeling_equity(self):
        engine = TradingEngine.__new__(TradingEngine)
        engine.pilot = SimpleNamespace(enabled=True)
        engine.risk = SimpleNamespace(balance=21.5075351411)
        engine._pilot_available_balance = 11.9815151411
        self.assertAlmostEqual(engine._entry_available_funds(), 11.9815151411)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)

    def test_core_affordability_falls_back_to_risk_balance_outside_pilot(self):
        engine = TradingEngine.__new__(TradingEngine)
        engine.pilot = SimpleNamespace(enabled=False)
        engine.risk = SimpleNamespace(balance=21.5075351411)
        self.assertAlmostEqual(engine._entry_available_funds(), 21.5075351411)


    def test_core_affordability_missing_pilot_collateral_fails_closed_without_equity_fallback(self):
        engine = TradingEngine.__new__(TradingEngine)
        engine.pilot = SimpleNamespace(enabled=True)
        engine.risk = SimpleNamespace(balance=21.5075351411)
        self.assertEqual(engine._entry_available_funds(), 0.0)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)

    def test_core_affordability_zero_pilot_collateral_remains_zero(self):
        engine = TradingEngine.__new__(TradingEngine)
        engine.pilot = SimpleNamespace(enabled=True)
        engine.risk = SimpleNamespace(balance=21.5075351411)
        engine._pilot_available_balance = 0.0
        self.assertEqual(engine._entry_available_funds(), 0.0)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)

    async def test_non_pilot_startup_balance_keeps_legacy_get_balance_behavior(self):
        class NonPilotEngine(TradingEngine):
            _pilot_live_runtime_patched = False

        engine = NonPilotEngine.__new__(NonPilotEngine)
        engine.paper_trade = False
        engine.client = SimpleNamespace(get_balance=AsyncMock(return_value=17.0))
        value = await TradingEngine._startup_risk_balance(engine)
        self.assertEqual(value, 17.0)
        engine.client.get_balance.assert_awaited_once()

    async def test_controlled_live_connect_uses_account_equity_from_first_risk_write(self):
        class Client:
            def __init__(self):
                self.get_balance = AsyncMock(return_value=17.0)
                self.ping = AsyncMock(return_value=True)
                self.start_websocket = AsyncMock(return_value=None)
                self.start_private_websocket = Mock(return_value=None)
                self._instruments = {}

            def get_instruments(self):
                self._instruments = {
                    "BTCUSDT": {
                        "minQty": 1,
                        "lotSize": 1,
                        "qtyStep": 1,
                        "multiplier": 1.0,
                        "tickSize": 0.01,
                        "minNotional": 0.0,
                    }
                }
                return self._instruments

        class Engine(TradingEngine):
            _pilot_live_runtime_patched = False

            async def _filter_viable_symbols(self):
                self.viable_symbols = ["BTCUSDT"]

            async def _load_existing_positions(self):
                return None

        client = Client()
        engine = Engine(client)
        engine.paper_trade = False

        init_values = []
        update_values = []
        original_init = engine.risk.init
        original_update = engine.risk.update

        def record_init(value):
            init_values.append(float(value))
            return original_init(value)

        def record_update(value):
            update_values.append(float(value))
            return original_update(value)

        engine.risk.init = record_init
        engine.risk.update = record_update

        state = {
            "equity": 21.5075351411,
            "available": 11.9815151411,
            "available_source": "availableMargin",
            "accountEquity": "21.5075351411",
            "availableBalance": "17.0",
            "availableMargin": "11.9815151411",
            "marginBalance": "18.8363",
            "positionMargin": "12.1972",
            "orderMargin": "0",
        }

        async def restore_hwm(risk, equity, strict=True):
            risk.peak_balance = 63.7942573
            risk.drawdown = (63.7942573 - float(equity)) / 63.7942573
            return 63.7942573

        live.install(Engine, _Log())
        with patch.object(
            live.account_semantics, "read_account_state", AsyncMock(return_value=state)
        ) as read_state, patch.object(
            live.capital_flows,
            "reconcile_external_capital_flows",
            AsyncMock(return_value={"applied": 0, "bootstrap": False}),
        ), patch.object(
            live.hwm_incident_repair,
            "repair_if_needed",
            AsyncMock(return_value={"status": "NOT_MATCHED"}),
        ), patch.object(
            live, "restore_update_real_account_peak", AsyncMock(side_effect=restore_hwm)
        ), patch.object(
            live, "_run_readonly_preflight", AsyncMock(return_value=True)
        ), patch(
            "bot.engine.notify", AsyncMock()
        ):
            await engine._connect()

        self.assertGreaterEqual(read_state.await_count, 2)
        client.get_balance.assert_not_awaited()
        self.assertTrue(init_values)
        self.assertTrue(update_values)
        self.assertEqual(set(init_values + update_values), {21.5075351411})
        self.assertNotIn(17.0, init_values + update_values)
        self.assertNotIn(11.9815151411, init_values + update_values)
        self.assertAlmostEqual(engine.risk.balance, 21.5075351411)
        self.assertAlmostEqual(engine._pilot_available_balance, 11.9815151411)
        self.assertAlmostEqual(engine.risk.peak_balance, 63.7942573)

    async def test_controlled_live_connect_without_canonical_equity_fails_closed(self):
        class Engine:
            _pilot_live_runtime_patched = False

            def __init__(self):
                self.paper_trade = False
                self.client = SimpleNamespace(get_balance=AsyncMock(return_value=17.0))
                self.risk = _Risk()
                self.connected = False

            async def _connect(self):
                self.connected = True
                self.original_connect_called = True

            async def _update_balance(self):
                return None

            async def _refresh_entry_balance(self):
                return True

            async def _open(self, sig):
                return None

        live.install(Engine, _Log())
        engine = Engine()
        engine.original_connect_called = False
        with patch.object(
            live.account_semantics,
            "read_account_state",
            AsyncMock(side_effect=RuntimeError("account overview unavailable")),
        ):
            result = await engine._connect()

        self.assertIsNone(result)
        self.assertFalse(engine.connected)
        self.assertFalse(engine.original_connect_called)
        engine.client.get_balance.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
