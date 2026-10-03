"""F-003 on the composed production LIVE-pilot runtime (offline, fake exchange).

SIGNAL -> final composed sizing (final_sizing_invariants -> RiskManagerV3)
-> contracts -> F-003 authorization -> final ``place_order`` chain (native
TP/SL, pilot counter, fence) -> transport barrier -> recording fake session.
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_live_entry_readiness_gate import _engine, _FakeSession  # composed LIVE env

from bot import engine as engine_module  # noqa: E402
from bot import kucoin  # noqa: E402
from bot import pilot_risk_cap_hardening as pilot_cap  # noqa: E402
from bot import risk_budget  # noqa: E402
from bot.config import cfg  # noqa: E402
from bot.order_state import OrderRegistry, OrderState  # noqa: E402
from bot.professional_risk import CapitalState  # noqa: E402
from bot.professional_risk_adapter import ProfessionalRiskAdapter  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
from bot.strategy import Signal  # noqa: E402

SOL = {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.001, "multiplier": 0.1,
       "minNotional": 0, "kucoinSymbol": "SOLUSDTM"}


class ComposedRiskBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()))
        self.stack.enter_context(patch("bot.execution_ownership.publish_valid_execution_ownership", Mock()))
        self.stack.enter_context(patch("bot.critical_state.critical_state.assert_available_for_new_risk", Mock()))
        self.stack.enter_context(patch("bot.pilot_submission_counter.reserve_submission",
                                       AsyncMock(return_value=(True, 1))))
        self.stack.enter_context(patch("bot.durable_execution.persist_orders", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))

    def tearDown(self):
        self.stack.close()

    async def _flow(self, *, equity, entry, stop, available=None, positions=None, inflate=0,
                    leverage=10):
        available = equity if available is None else available
        readiness = _engine(instruments={"SOLUSDT": dict(SOL)}, viable_symbols=["SOLUSDT"])
        client = kucoin.KuCoinClient()
        client._session = _FakeSession()
        client._instruments = {"SOLUSDT": dict(SOL)}
        client._engine = readiness
        client._execution_ownership = object()
        client._order_registry = OrderRegistry()

        risk = ProfessionalRiskAdapter(RiskManager())
        risk.update_capital(CapitalState(equity, available))
        risk.set_plan(symbol="SOLUSDT", entry=entry, stop=stop, risk_pct=cfg.MAX_RISK_PCT)
        sizing_engine = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                                        _pilot_available_balance=available, risk=risk,
                                        instruments={"SOLUSDT": dict(SOL)},
                                        positions=positions or {})
        sig = Signal("SOLUSDT", "LONG", entry, stop, entry + 3 * (entry - stop), .8, "t", 80)
        tokens = (pilot_cap._PILOT_ENGINE.set(sizing_engine), pilot_cap._PILOT_SYMBOL.set("SOLUSDT"),
                  pilot_cap._PILOT_SIGNAL.set(sig), pilot_cap._PILOT_FINAL_QTY.set(None))
        auth_token = risk_budget.authorize(None)
        old_lev = cfg.LEVERAGE
        cfg.LEVERAGE = leverage
        try:
            qty = engine_module.minimum_base_quantity(SOL, entry)           # final composed sizing
            auth = risk_budget.current_authorization()
            error = None
            if qty > 0:
                send = round(qty / 0.1 + inflate) * 0.1
                oid = client.build_client_oid("SOLUSDT", "Buy", send, "f003")
                order, _ = client._order_registry.get_or_create(
                    oid, "SOLUSDT", "Buy", float(client._round_qty(send, "SOLUSDT")))
                order.transition(OrderState.SUBMITTING, source="REST")
                try:
                    await client.place_order("SOLUSDT", "Buy", send, sl=sig.sl, tp=sig.tp,
                                             idem_key="f003", single_submission=True)
                except risk_budget.RiskBudgetRefused as exc:
                    error = exc
        finally:
            cfg.LEVERAGE = old_lev
            risk_budget.reset_authorization(auth_token)
            for var, tok in zip((pilot_cap._PILOT_FINAL_QTY, pilot_cap._PILOT_SIGNAL,
                                 pilot_cap._PILOT_SYMBOL, pilot_cap._PILOT_ENGINE), reversed(tokens)):
                var.reset(tok)
        posts = client._session.posts("/api/v1/st-orders", "/api/v1/orders")
        return qty, auth, posts, error

    async def test_signal_to_exchange_respects_one_percent(self):
        qty, auth, posts, error = await self._flow(equity=100.0, entry=150.0, stop=148.5)
        self.assertIsNone(error)
        self.assertEqual(len(posts), 1)
        body = posts[0][2]
        self.assertEqual((int(body["size"]), float(body["triggerStopDownPrice"])), (5, 148.5))
        self.assertFalse(body["reduceOnly"])
        loss = int(body["size"]) * 0.1 * (150.0 - 148.5 + 150.0 * 0.0022)
        captured = dict(equity=auth.equity, risk_pct=auth.risk_pct, risk_budget=auth.risk_budget,
                        technical_stop=auth.stop, contracts=auth.contracts,
                        notional=auth.contracts * 0.1 * 150.0,
                        margin=auth.contracts * 0.1 * 150.0 / auth.leverage,
                        projected_loss=auth.projected_loss,
                        aggregate_reserved_risk=auth.reserved_before + auth.risk_budget)
        self.assertEqual(captured, dict(equity=100.0, risk_pct=0.01, risk_budget=1.0,
                                        technical_stop=148.5, contracts=5, notional=75.0,
                                        margin=7.5, projected_loss=captured["projected_loss"],
                                        aggregate_reserved_risk=1.0))
        self.assertAlmostEqual(loss, captured["projected_loss"])
        self.assertLessEqual(loss, 1.0)

    async def test_old_sol_33_contracts_at_4pct_is_impossible(self):
        qty, auth, posts, _ = await self._flow(equity=100.0, entry=150.0, stop=144.0)
        self.assertLess(auth.contracts, 33)
        self.assertLessEqual(int(posts[0][2]["size"]) * 0.1 * (6.0 + 0.33), 1.0)

    async def test_m2_upstream_inflated_contracts_cannot_escape(self):
        qty, auth, posts, error = await self._flow(equity=100.0, entry=150.0, stop=148.5, inflate=28)
        self.assertIsInstance(error, risk_budget.RiskBudgetRefused)
        self.assertEqual(error.reason, "CONTRACTS_ABOVE_AUTHORIZATION")
        self.assertEqual(posts, [], "zero exchange POST")

    async def test_third_risk_unit_rejected_even_with_free_margin(self):
        full = {"BTCUSDT": SimpleNamespace(_risk_reserved_usdt=1.0),
                "ETHUSDT": SimpleNamespace(_risk_reserved_usdt=1.0)}
        qty, auth, posts, _ = await self._flow(equity=100.0, entry=150.0, stop=148.5,
                                               available=100000.0, positions=full)
        self.assertEqual((qty, auth, posts), (0.0, None, []))

    async def test_min_contract_above_budget_is_no_trade_zero_http(self):
        qty, auth, posts, _ = await self._flow(equity=10.0, entry=150.0, stop=148.5)
        self.assertEqual((qty, auth, posts), (0.0, None, []))

    async def test_leverage_does_not_change_projected_loss(self):
        sizes = set()
        for lev in (5, 10, 20, 50):
            _, auth, posts, _ = await self._flow(equity=100.0, available=1000.0, entry=150.0,
                                                 stop=148.5, leverage=lev)
            sizes.add((auth.contracts, round(auth.projected_loss, 9), int(posts[0][2]["size"])))
        self.assertEqual(len(sizes), 1, sizes)


if __name__ == "__main__":
    unittest.main()
