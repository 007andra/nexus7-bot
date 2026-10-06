"""Offline LIVE bootstrap proof. No exchange or production credentials are used."""
import asyncio
import builtins
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.run_offline import install_network_guard

install_network_guard()

# tests.run_offline imports this module in its own -S child after its network
# audit hook. Set the synthetic release environment BEFORE importing the bot.
os.environ.update(
    EXCHANGE="binance", PAPER_TRADE="false", REAL_TRADING_PILOT="true",
    LIVE_TRADING_CONFIRMED="I_UNDERSTAND_THE_RISK",
    PILOT_ACCOUNT_CONFIRMED="true",
    PILOT_RELEASE_APPROVED="I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    VALIDATION_LOCK_RELEASE_APPROVED="I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    BINANCE_LIVE_MIGRATION_READY="true", NEXUS_TELEGRAM="false",
    BGX_RUNTIME_TRUTH_ENABLED="false", LEVERAGE="50", MAX_RISK_PCT="0.01", MAX_DRAWDOWN="0.10",
)

from bot import runtime_bootstrap

runtime_bootstrap.install()

from bot import engine as core
from bot import binance_cross_portfolio_stress as stress
from bot import pilot_risk_cap_hardening as context
from bot.binance import BinanceClient
from bot.config import cfg
from bot.nexus_types import NexusDecision
from bot.nexus_runtime_engine import TradingEngine as RuntimeTradingEngine
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.strategy import Signal


def chain(fn):
    """The legacy wrappers lack __wrapped__; recover captured original funcs."""
    result = []
    seen = set()
    while callable(fn) and id(fn) not in seen:
        seen.add(id(fn))
        # functools.wraps can copy __module__/__name__ from an inner function.
        # Code origin is the effective owner, not copied display metadata.
        source = os.path.splitext(os.path.basename(fn.__code__.co_filename))[0]
        result.append(("bot." + source, fn.__code__.co_name))
        closure = inspect.getclosurevars(fn).nonlocals
        candidates = [v for k, v in closure.items()
                      if (k.startswith("original") or k.startswith("previous"))
                      and inspect.isfunction(v) and v.__name__ != "_guard"]
        fn = getattr(fn, "__wrapped__", None) or (candidates[0] if candidates else None)
    return result


class DispatchProof(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.patches = []
        self.scenario = "pass"
        self.stage = "FINAL_PREDISPATCH"
        self.evaluations = []
        self.requests = []
        self.client = BinanceClient()
        self.engine = RuntimeTradingEngine(self.client)
        self.engine.paper_trade = False
        self.engine._durable_state_enforced = True
        self.engine._durable_state_ok = True
        self.engine._durable_state_errors = set()
        self.engine._durable_order_lock = asyncio.Lock()
        self.engine.viable_symbols = ["ETHUSDT"]
        self.engine.instruments = {"ETHUSDT": {
            "quantityUnit": "BASE_ASSET", "qtyStep": "0.001", "minQty": "0.001",
            "minNotional": "20", "tickSize": "0.01", "multiplier": 1.0,
        }}
        self.client._instruments = self.engine.instruments
        self.engine.risk.init(1000)
        self.assertIsInstance(self.engine.risk, ProfessionalRiskAdapter)
        self.engine.risk.set_plan(symbol="ETHUSDT", entry=100, stop=99.5, risk_pct=0.01)
        self.engine.risk.update_capital(CapitalState(1000, 1000))
        self.signal = Signal("ETHUSDT", "LONG", 100, 99.5, 104, 80, "offline", 90)
        decision = NexusDecision(symbol="ETHUSDT", decision="LONG", execution_allowed=True,
                                 confidence=90, setup_quality=90, entry=100,
                                 stop_loss=99.5, take_profit=104, expected_value=1,
                                 risk_reward=8, reasoning=["offline proof"])
        self.engine._nexus_validate = AsyncMock(return_value=decision)
        self.engine.pilot.can_open_pilot = lambda *a: True
        self.engine.integrity.assess = AsyncMock()
        self.engine.integrity.can_open_new = lambda: True
        self.client._prelive_private_ws_probe_ok = True
        self.client.private_stream_health = None
        self.client.get_positions = AsyncMock(return_value=[])
        self.client.get_open_orders = AsyncMock(return_value=[])
        self.client.get_account_state = AsyncMock(side_effect=self.account)
        self.client.get_leverage_brackets = AsyncMock(side_effect=self.brackets)
        self.client.get_position_mode = AsyncMock(return_value="ONE_WAY")
        self.client.get_symbol_config = AsyncMock(return_value={"marginType": "CROSSED", "leverage": 50})
        self.client.get_cached_klines = lambda *a: [{"c": 100, "h": 101, "l": 99, "v": 1000}] * 50
        self.client._request = AsyncMock(side_effect=self.request)
        self.client.set_position_stops = AsyncMock(return_value=True)
        self.client.wait_for_fill = AsyncMock(return_value={
            "filled": False, "isActive": False, "status": "NEW", "timed_out": True,
        })
        self.client.get_order_by_client_oid = AsyncMock(return_value={})
        self.client._execution_ownership = SimpleNamespace(expires_at="2099-01-01T00:00:00+00:00")
        self.replace("bot.engine._NEXUS_ENABLED", True)
        for name in ("bot.engine.notify", "bot.engine.notify_nexus", "bot.database.save_signal",
                     "bot.database.save_snapshot", "bot.database.save_key_value",
                     "bot.pilot_live_runtime.capital_flows.reconcile_external_capital_flows",
                     "bot.pilot_live_runtime.restore_update_real_account_peak"):
            self.replace(name, AsyncMock())
        self.replace(
            "bot.pilot_live_runtime.hwm_incident_repair.repair_if_needed",
            AsyncMock(return_value={"status": "NOT_MATCHED"}),
        )
        self.replace(
            "bot.pilot_live_runtime.external_performance.evaluate",
            AsyncMock(return_value="NORMAL"),
        )
        self.replace("bot.engine.scoring.calculate", AsyncMock(return_value={"aprovado": True, "total": 90}))
        self.replace("bot.execution_cost.snapshot_for", AsyncMock(side_effect=ValueError("offline fallback")))
        self.replace("bot.pilot_risk_cap_hardening.live_microstructure_recheck", AsyncMock(
            return_value=SimpleNamespace(allowed=True, metrics={"executable_price": 100}, blockers=[])))
        self.replace("bot.binance._assert_signing_credentials_available", lambda: None)
        self.replace("bot.live_execution_fence.acquire", AsyncMock(side_effect=self.fence))
        self.replace("bot.execution_ownership.validate_execution_ownership", AsyncMock(side_effect=self.ownership))
        self.replace("bot.runtime_readiness.assert_ready_for_new_entries", lambda e: self.events.append("READINESS"))
        self.replace("bot.critical_state.critical_state.assert_available_for_new_risk", lambda: None)
        self.real_evaluate = stress.evaluate
        self.replace("bot.binance_cross_portfolio_stress.evaluate", self.evaluate)
        sizing = core.minimum_base_quantity

        def size(*a, **k):
            self.events.append("FINAL_SIZING_ENTER")
            qty = sizing(*a, **k)
            self.events.append("FINAL_SIZING_RETURN")
            self.sized_qty = qty
            if self.scenario == "quantity_missing":
                context._PILOT_FINAL_QTY.set(None)
            elif self.scenario == "quantity_zero":
                context._PILOT_FINAL_QTY.set(0)
            elif self.scenario == "quantity_negative":
                context._PILOT_FINAL_QTY.set(-qty)
            elif self.scenario == "signal_missing":
                context._PILOT_SIGNAL.set(None)
            elif self.scenario == "quantity_drift":
                context._PILOT_FINAL_QTY.set(qty + 0.001)
            return qty

        self.replace("bot.engine.minimum_base_quantity", size)
        from bot import final_loss_budget
        diagnose = final_loss_budget.diagnose

        def loss(*a, **k):
            self.events.append("FINAL_LOSS_BUDGET")
            return diagnose(*a, **k)

        self.replace("bot.final_loss_budget.diagnose", loss)
        dispatch = self.client.place_order

        async def observed_dispatch(*a, **k):
            self.events.append("PLACE_ORDER")
            self.dispatch_qty = k["qty"]
            return await dispatch(*a, **k)

        self.client.place_order = AsyncMock(side_effect=observed_dispatch)

    def replace(self, name, value):
        p = patch(name, value)
        self.patches.append(p)
        return p.start()

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()

    async def account(self):
        self.events.append("ACCOUNT_REFRESH")
        return {"equity": 1000, "available": 1000, "crossWalletBalance": 1000,
                "orderMargin": 0, "canTrade": True, "multiAssetsMargin": False}

    async def brackets(self, symbol):
        return {"symbol": symbol, "brackets": [{"bracket": 1, "notionalFloor": 0,
                "notionalCap": 100000, "initialLeverage": 50, "maintMarginRatio": 0.005, "cum": 0}]}

    async def fence(self, *a):
        self.events.append("FENCE")
        return True

    async def ownership(self, *a):
        self.events.append("OWNERSHIP")

    async def request(self, method, endpoint, params=None, **kwargs):
        self.requests.append((method, endpoint, params))
        self.events.append("FAKE_HTTP")
        return {"orderId": "proof-ack", "clientOrderId": (params or {}).get("newClientOrderId")}

    async def evaluate(self, engine, sig, qty):
        stage = "PRE_ORDER" if not self.evaluations else "FINAL_PREDISPATCH"
        self.events.append("STRESS_" + stage)
        self.evaluations.append((stage, qty))
        if stage == self.stage:
            if self.scenario == "block":
                return stress.StressResult(False, "injected_block")
            if self.scenario == "exception":
                raise RuntimeError("injected_stress_exception")
        if stage == self.stage:
            account = await self.account()
            brackets = await self.brackets(sig.symbol)
            row = brackets["brackets"][0]
            changes = {}
            if self.scenario == "timeout":
                changes["get_account_state"] = AsyncMock(side_effect=asyncio.TimeoutError("offline timeout"))
            elif self.scenario == "account_missing":
                changes["get_account_state"] = AsyncMock(return_value=None)
            elif self.scenario == "account_malformed":
                changes["get_account_state"] = AsyncMock(return_value={"canTrade": True, "crossWalletBalance": "bad"})
            elif self.scenario == "position_divergence":
                changes["get_positions"] = AsyncMock(return_value=[{"symbol": "BTCUSDT", "size": 1}])
            elif self.scenario == "open_orders":
                account["orderMargin"] = 1
            elif self.scenario == "bracket_missing":
                brackets = {}
            elif self.scenario == "bracket_invalid":
                row["notionalCap"] = "bad"
            elif self.scenario == "bracket_50x":
                row["initialLeverage"] = 20
            elif self.scenario == "maintenance_invalid":
                row["maintMarginRatio"] = float("nan")
            elif self.scenario == "margin_nonpositive":
                account["crossWalletBalance"] = 0.01
            elif self.scenario == "risk_rate":
                row["maintMarginRatio"] = 0.99
            elif self.scenario == "cross_unconfirmed":
                changes["get_symbol_config"] = AsyncMock(return_value={})
            elif self.scenario == "leverage_mismatch_lower":
                changes["get_symbol_config"] = AsyncMock(return_value={"marginType": "CROSSED", "leverage": 20})
            elif self.scenario == "leverage_mismatch_higher":
                changes["get_symbol_config"] = AsyncMock(return_value={"marginType": "CROSSED", "leverage": 75})
            elif self.scenario == "leverage_missing":
                changes["get_symbol_config"] = AsyncMock(return_value={"marginType": "CROSSED"})
            elif self.scenario == "leverage_invalid":
                changes["get_symbol_config"] = AsyncMock(return_value={"marginType": "CROSSED", "leverage": "bad"})
            elif self.scenario == "leverage_read_exception":
                changes["get_symbol_config"] = AsyncMock(side_effect=RuntimeError("symbolConfig read failed"))
            elif self.scenario == "internal_error":
                changes["get_leverage_brackets"] = AsyncMock(side_effect=RuntimeError("unexpected internal error"))
            changes.setdefault("get_account_state", AsyncMock(return_value=account))
            changes.setdefault("get_leverage_brackets", AsyncMock(return_value=brackets))
            from contextlib import ExitStack
            with ExitStack() as stack:
                for name, value in changes.items():
                    stack.enter_context(patch.object(self.client, name, value))
                result = await self.real_evaluate(engine, sig, qty)
            self.last_result = result
            return result
        return await self.real_evaluate(engine, sig, qty)

    async def test_positive_control(self):
        self.assertEqual(cfg.LEVERAGE, 50)
        await self.engine._open(self.signal)
        self.assertEqual(self.client.place_order.call_count, 1, self.events)
        # P1-OPEN-1: the stale-protection inventory (read-only GET) precedes
        # the single entry POST; exactly one exchange mutation.
        self.assertEqual([(m, e) for m, e, _ in self.requests],
                         [("GET", "/fapi/v1/openAlgoOrders"), ("POST", "/fapi/v1/order")], self.events)
        self.assertEqual(self.evaluations, [("PRE_ORDER", self.sized_qty), ("FINAL_PREDISPATCH", self.sized_qty)])
        self.assertEqual(self.dispatch_qty, self.sized_qty)
        self.assertEqual(float(self.requests[-1][2]["quantity"]), self.sized_qty)
        critical = [e for e in self.events if e in {
            "FINAL_SIZING_ENTER", "FINAL_LOSS_BUDGET", "FINAL_SIZING_RETURN",
            "STRESS_PRE_ORDER", "STRESS_FINAL_PREDISPATCH", "PLACE_ORDER", "FENCE", "OWNERSHIP", "FAKE_HTTP"}]
        self.assertEqual(critical, ["FINAL_SIZING_ENTER", "FINAL_LOSS_BUDGET", "FINAL_SIZING_RETURN",
                                   "STRESS_PRE_ORDER", "FINAL_LOSS_BUDGET", "STRESS_FINAL_PREDISPATCH",
                                   "PLACE_ORDER", "FENCE", "OWNERSHIP", "FAKE_HTTP", "FAKE_HTTP"])
        between = self.events[self.events.index("STRESS_PRE_ORDER") + 1:self.events.index("STRESS_FINAL_PREDISPATCH")]
        self.assertIn("ACCOUNT_REFRESH", between)
        self.assertNotIn("FINAL_SIZING_ENTER", self.events[self.events.index("STRESS_PRE_ORDER"):])

    async def test_controlled_reentry_post_nexus_replay_stops_before_http(self):
        """Replay the first R3 ARB geometry with a synthetic NEXUS approval.

        This proves the post-NEXUS chain against the real runtime wrappers:
        fresh capital -> final sizing -> absolute 0.10 USDT loss ceiling ->
        PRE_ORDER CROSS -> fresh refresh -> FINAL_PREDISPATCH CROSS -> transport
        boundary. The exchange POST is deliberately denied at the final
        one-shot boundary, so this test can never create a real order.
        """
        from bot import controlled_live_reentry_v1 as controlled
        from bot import final_loss_budget
        from bot import pilot as pilot_module

        equity = 5.39561426
        symbol = "ARBUSDT"
        self.engine.viable_symbols = [symbol]
        self.engine.instruments = {
            symbol: {
                "quantityUnit": "BASE_ASSET",
                "qtyStep": "0.1",
                "minQty": "0.1",
                "minNotional": "5",
                "tickSize": "0.00001",
                "multiplier": 1.0,
            }
        }
        self.client._instruments = self.engine.instruments
        self.engine.risk.init(equity)
        self.engine.risk.update_capital(CapitalState(equity, equity))
        self.engine._pilot_live_prelive_ready = True
        self.signal = Signal(
            symbol, "SHORT", 0.20053, 0.203079, 0.195433, 60,
            "R3 ARBUSDT replay", 90,
        )
        decision = NexusDecision(
            symbol=symbol,
            decision="SHORT",
            execution_allowed=True,
            confidence=90,
            setup_quality=90,
            entry=0.20053,
            stop_loss=0.203079,
            take_profit=0.195433,
            expected_value=1,
            risk_reward=2,
            reasoning=["synthetic approval for offline post-NEXUS proof"],
        )
        # Preserve RuntimeTradingEngine._nexus_validate so the real
        # post-decision wrapper executes _prepare_professional_risk(). Mock only
        # the CoreTradingEngine decision source; replacing the instance method
        # would bypass the RiskManagerV3 plan and make final sizing fail for a
        # reason that cannot occur on the canonical approved path.
        self.engine._nexus_validate = RuntimeTradingEngine._nexus_validate.__get__(
            self.engine, RuntimeTradingEngine
        )

        async def production_account():
            self.events.append("ACCOUNT_REFRESH")
            return {
                "equity": equity,
                "available": equity,
                "crossWalletBalance": equity,
                "orderMargin": 0,
                "positionMargin": 0,
                "canTrade": True,
                "multiAssetsMargin": False,
            }

        self.client.get_account_state = AsyncMock(side_effect=production_account)

        captured_loss = []
        current_diagnose = final_loss_budget.diagnose

        def capture_loss(*args, **kwargs):
            result = current_diagnose(*args, **kwargs)
            captured_loss.append(result)
            return result

        stop_before_http = AsyncMock(
            return_value=(False, "OFFLINE_PROOF_STOP_BEFORE_HTTP")
        )
        controlled_env = {
            controlled.ENABLED_ENV: "true",
            controlled.EPISODE_ENV: "OFFLINE_R3_ARBUSDT_POST_NEXUS_PROOF",
            controlled.ARM_ENV: controlled.ARM_TOKEN,
            controlled.LOSS_BUDGET_ENV: "0.10",
            controlled.MAX_RISK_PCT_ENV: "0.02",
        }

        with patch.dict(os.environ, controlled_env, clear=False), \
             patch.object(
                 core.TradingEngine, "_nexus_validate",
                 AsyncMock(return_value=decision),
             ), \
             patch.object(pilot_module, "PILOT_MAX_CONCURRENT_POSITIONS", 1), \
             patch.object(pilot_module, "MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION", 1), \
             patch.object(final_loss_budget, "diagnose", side_effect=capture_loss), \
             patch(
                 "bot.controlled_live_reentry_v1.consume_dispatch_once",
                 stop_before_http,
             ):
            await self.engine._open(self.signal)

        self.assertGreater(self.sized_qty, 0.0, self.events)
        self.assertEqual(
            self.evaluations,
            [("PRE_ORDER", self.sized_qty), ("FINAL_PREDISPATCH", self.sized_qty)],
            self.events,
        )
        self.assertTrue(getattr(self.last_result, "allowed", False), self.events)
        self.assertGreaterEqual(len(captured_loss), 2, self.events)
        projected = [
            float(metrics["projected_loss"])
            for result, _reason, metrics in captured_loss
            if isinstance(metrics, dict) and "projected_loss" in metrics
        ]
        self.assertTrue(projected, captured_loss)
        self.assertLessEqual(max(projected), 0.10 + 1e-9, captured_loss)

        # place_order was reached, but the final one-shot authorization boundary
        # denied the attempt before any exchange order POST.
        self.assertEqual(self.client.place_order.call_count, 1, self.events)
        self.assertEqual(stop_before_http.await_count, 1, self.events)
        self.assertFalse(
            any(method == "POST" and endpoint == "/fapi/v1/order"
                for method, endpoint, _params in self.requests),
            self.requests,
        )
        self.assertIn("FINAL_SIZING_ENTER", self.events)
        self.assertIn("FINAL_SIZING_RETURN", self.events)
        self.assertIn("STRESS_PRE_ORDER", self.events)
        self.assertIn("STRESS_FINAL_PREDISPATCH", self.events)
        self.assertIn("PLACE_ORDER", self.events)

    async def test_pre_order_block(self):
        self.stage, self.scenario = "PRE_ORDER", "block"
        await self.engine._open(self.signal)
        self.assertEqual(len(self.evaluations), 1)
        self.assertEqual(self.client.place_order.call_count, 0)
        self.assertEqual(self.requests, [])

    async def test_pre_order_exception(self):
        self.stage, self.scenario = "PRE_ORDER", "exception"
        await self.engine._open(self.signal)
        self.assertEqual(len(self.evaluations), 1)
        self.assertEqual(self.client.place_order.call_count, 0)
        self.assertEqual(self.requests, [])

    async def test_block_at_final_boundary(self):
        self.scenario = "block"
        await self.engine._open(self.signal)
        self.assertEqual(len(self.evaluations), 2, self.events)
        self.assertEqual(self.client.place_order.call_count, 0)
        self.assertEqual(self.requests, [])

    async def test_exception_at_final_boundary(self):
        self.scenario = "exception"
        await self.engine._open(self.signal)
        self.assertEqual(len(self.evaluations), 2, self.events)
        self.assertEqual(self.client.place_order.call_count, 0)
        self.assertEqual(self.requests, [])


class BootstrapContract(unittest.TestCase):
    def test_optional_truth_overlay_preserves_cross_owner(self):
        code = """
from tests.run_offline import install_network_guard
install_network_guard()
import sitecustomize
import builtins, inspect
from bot.engine import TradingEngine
from bot.binance import BinanceClient
assert builtins._nexus_sitecustomize_status == 'ok'
assert TradingEngine._refresh_entry_balance.__module__ == 'bot.binance_cross_portfolio_stress'
assert TradingEngine._open.__module__ == 'bot.runtime_truth_hooks'
assert inspect.getclosurevars(TradingEngine._open).nonlocals['original_open'].__module__ == 'bot.post_trade_forensics'
assert BinanceClient.place_order.__module__ == 'bot.runtime_truth_hooks'
assert inspect.getclosurevars(BinanceClient.place_order).nonlocals['original_place_order'].__module__ == 'bot.live_execution_fence'
"""
        env = dict(os.environ, BGX_RUNTIME_TRUTH_ENABLED="true")
        with tempfile.TemporaryDirectory(prefix="binance-truth-proof-") as cwd:
            result = subprocess.run([sys.executable, "-S", "-c", code],
                                    env=env, cwd=cwd, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_removing_cross_wrapper_breaks_runtime_contract(self):
        from bot import runtime_contract_guard, nexus_ai
        from bot.pilot import PilotGuard
        from bot.logger import log
        fn = core.TradingEngine._refresh_entry_balance
        inner = inspect.getclosurevars(fn).nonlocals["original_refresh"]
        with patch.object(builtins, "_nexus_runtime_contract_status", None), patch.object(
            core.TradingEngine, "_refresh_entry_balance", inner
        ):
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_CONTRACT_DRIFT"):
                runtime_contract_guard.install(core.TradingEngine, PilotGuard, nexus_ai, core, log)

    def test_final_refresh_owner(self):
        self.assertTrue(core.TradingEngine._binance_cross_portfolio_stress_installed)
        self.assertEqual(core.TradingEngine._refresh_entry_balance.__module__, "bot.binance_cross_portfolio_stress")
        self.assertEqual(getattr(builtins, "_nexus_runtime_contract_status", None), "ok")

    def test_effective_wrapper_order(self):
        self.assertEqual([m for m, _ in chain(core.TradingEngine._open)], [
            "bot.post_trade_forensics", "bot.operator_loss_policy", "bot.legacy_pretrade_advisory",
            "bot.min_order_feasibility", "bot.pilot_risk_cap_hardening",
            "bot.pilot_live_runtime", "bot.binance_protection_failclosed", "bot.engine"])
        self.assertEqual([m for m, _ in chain(core.TradingEngine._refresh_entry_balance)], [
            "bot.binance_cross_portfolio_stress", "bot.pilot_risk_cap_hardening", "bot.pilot_live_runtime",
            "bot.paper_wallet", "bot.engine"])


SCENARIOS = (
    "block", "exception", "timeout", "account_missing", "account_malformed",
    "position_divergence", "open_orders", "bracket_missing", "bracket_invalid",
    "bracket_50x", "maintenance_invalid", "margin_nonpositive", "risk_rate",
    "quantity_missing", "quantity_zero", "quantity_negative", "signal_missing",
    "symbol_unknown", "cross_unconfirmed", "internal_error", "quantity_drift",
    "leverage_mismatch_lower", "leverage_mismatch_higher", "leverage_missing",
    "leverage_invalid", "leverage_read_exception",
)


def _scenario_test(scenario):
    async def check(self):
        self.scenario = scenario
        if scenario == "symbol_unknown":
            self.signal.symbol = "UNKNOWNUSDT"
        await self.engine._open(self.signal)
        if scenario not in {"symbol_unknown", "quantity_zero", "quantity_negative"}:
            self.assertGreaterEqual(len(self.evaluations), 1, self.events)
        if scenario not in {"symbol_unknown", "quantity_zero", "quantity_negative",
                            "quantity_missing", "signal_missing", "quantity_drift"}:
            self.assertEqual(len(self.evaluations), 2, self.events)
            if scenario not in {"block", "exception"}:
                self.assertFalse(self.last_result.allowed, self.last_result)
        self.assertEqual(self.client.place_order.call_count, 0, (scenario, self.events))
        self.assertEqual(self.requests, [], scenario)
    return check


for _scenario in SCENARIOS:
    setattr(DispatchProof, "test_fail_closed_" + _scenario, _scenario_test(_scenario))


class _OpenNewRiskInvariantMatrix:
    """OPEN_NEW_RISK mutation matrix through the real engine._open chain.

    Each test perturbs exactly one authority relative to the positive control
    (which dispatches exactly once) and requires zero exchange HTTP requests.
    """

    async def _assert_no_exchange_request(self, label, *, evidence=None):
        import logging as _logging
        with self.assertLogs("kakazito-trade", level="DEBUG") as logs:
            _logging.getLogger("kakazito-trade").debug("[MATRIX] %s", label)
            await self.engine._open(self.signal)
        self.log_output = "\n".join(logs.output)
        mutations = [r for r in self.requests if r[0] != "GET"]
        self.assertEqual(mutations, [], (label, self.events))
        if evidence:
            self.assertTrue(any(e in self.log_output or e in self.events for e in evidence),
                            (label, self.events, self.log_output[-3000:]))

    async def test_inv01_04_unresolved_durable_state_blocks(self):
        self.engine._durable_state_ok = False
        self.engine._durable_state_errors = {"orders"}
        await self._assert_no_exchange_request("durable_state_unresolved", evidence=["durable", "DURABLE"])
        self.assertEqual(self.client.place_order.call_count, 0)

    async def test_inv02_invalid_ownership_blocks_at_transport(self):
        from bot.execution_ownership import StaleExecutionFence

        async def invalid(*a):
            self.events.append("OWNERSHIP_INVALID")
            raise StaleExecutionFence("REJECTED_STALE_FENCE superseded token")

        self.replace("bot.execution_ownership.validate_execution_ownership", AsyncMock(side_effect=invalid))
        await self._assert_no_exchange_request("ownership_invalid", evidence=["OWNERSHIP_INVALID"])

    async def test_inv03_stale_fence_blocks_at_transport(self):
        async def stale(*a):
            self.events.append("FENCE_STALE")
            return False

        self.replace("bot.live_execution_fence.acquire", AsyncMock(side_effect=stale))
        await self._assert_no_exchange_request("fence_stale", evidence=["FENCE_STALE"])

    async def test_inv09_drawdown_at_hard_limit_blocks(self):
        async def drawn_down():
            self.events.append("ACCOUNT_REFRESH")
            return {"equity": 899.0, "available": 899.0, "crossWalletBalance": 899.0,
                    "orderMargin": 0, "canTrade": True, "multiAssetsMargin": False}

        self.client.get_account_state = AsyncMock(side_effect=drawn_down)
        self.engine.risk._legacy.peak_balance = 1000.0
        with patch.dict(os.environ, {"LIVE_RISK_OVERRIDE_APPROVED": ""}):
            await self._assert_no_exchange_request("drawdown_limit", evidence=["DRAWDOWN", "drawdown"])
        self.assertEqual(self.client.place_order.call_count, 0)

    async def test_inv10_external_position_conflict_blocks(self):
        self.client.get_positions = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "size": 1, "side": "Buy", "positionAmt": "1"}])
        await self._assert_no_exchange_request("external_position", evidence=["position_divergence", "EXTERNAL", "external"])
        self.assertEqual(self.client.place_order.call_count, 0)

    async def test_inv11_invalid_top_of_book_blocks(self):
        self.replace("bot.pilot_risk_cap_hardening.live_microstructure_recheck", AsyncMock(
            return_value=SimpleNamespace(allowed=False, metrics={}, blockers=["stale_book_ticker"])))
        await self._assert_no_exchange_request("top_of_book", evidence=["stale_book_ticker"])
        self.assertEqual(self.client.place_order.call_count, 0)

    async def test_inv12_missing_instrument_metadata_blocks(self):
        self.engine.instruments.pop("ETHUSDT")
        await self._assert_no_exchange_request("instrument_metadata", evidence=["ETHUSDT"])
        self.assertEqual(self.client.place_order.call_count, 0)

    async def test_inv13_readiness_failure_blocks(self):
        def not_ready(engine):
            self.events.append("READINESS_BLOCK")
            raise RuntimeError("protection_system_ready=false")

        self.replace("bot.runtime_readiness.assert_ready_for_new_entries", not_ready)
        await self._assert_no_exchange_request("readiness")
        self.assertIn("READINESS_BLOCK", self.events)

    async def test_inv14_ambiguous_transport_never_resubmits(self):
        async def ambiguous(method, endpoint, params=None, **kwargs):
            self.requests.append((method, endpoint, params))
            raise asyncio.TimeoutError("response lost after possible acceptance")

        self.client._request = AsyncMock(side_effect=ambiguous)
        await self.engine._open(self.signal)
        posts = [r for r in self.requests if r[0] == "POST" and r[1] == "/fapi/v1/order"]
        self.assertLessEqual(len(posts), 1, self.events)
        pending = [o for o in self.engine.orders.pending_orders() if o.symbol == "ETHUSDT"]
        self.assertTrue(all(not o.is_terminal for o in pending))


for _name, _fn in list(vars(_OpenNewRiskInvariantMatrix).items()):
    if _name.startswith("test_") or _name.startswith("_assert_no_exchange"):
        setattr(DispatchProof, _name, _fn)
