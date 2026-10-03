import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot.operator_loss_policy import stop_price, required_target_price, install


class LossPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_opportunity_cost_is_decision_snapshot(self):
        from bot.missed_opportunity_audit import _decision_cost_pct, _persisted_cost_pct
        decision = SimpleNamespace(_bgx_nexus_cost_context=SimpleNamespace(taker_fee=.0006, slippage=.0001))
        self.assertAlmostEqual(_decision_cost_pct(decision, "BTCUSDT"), .14)
        self.assertAlmostEqual(_persisted_cost_pct('{"estimated_round_trip_cost_pct":0.14}', "BTCUSDT"), .14)

    def test_latest_derivatives_source_not_first_row(self):
        from bot.derivatives_news_freshness_hardening import _latest_source_row
        rows = [{"timestamp": 1000, "buySellRatio": 1}, {"timestamp": 2000, "buySellRatio": 2}]
        self.assertEqual(_latest_source_row(rows)["buySellRatio"], 2)
        self.assertEqual(_latest_source_row(list(reversed(rows)))["buySellRatio"], 2)

    async def test_technical_stop_reaches_normal_entry_gate_unchanged(self):
        from unittest.mock import patch
        from bot.config import cfg
        calls = []
        class Engine:
            async def _open(self, sig):
                calls.append((sig.sl, sig.tp, sig.tp1, sig.tp2))
                return "normal_gate"
            _check_stagnation_and_invalidation = AsyncMock()
        log = SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
        install(Engine, log)
        engine = Engine()
        engine.paper_trade = False
        engine.pilot = SimpleNamespace(enabled=True)
        sig = SimpleNamespace(
            symbol="ETHUSDT",
            entry=2500.,
            direction="LONG",
            sl=2450.,
            tp=2625.,
            tp1=2575.,
            tp2=2625.,
        )
        before = (sig.sl, sig.tp, sig.tp1, sig.tp2)
        with patch.object(cfg, "LEVERAGE", 50):
            self.assertEqual(await engine._open(sig), "normal_gate")
        self.assertEqual(calls[0], before)
        self.assertEqual((sig.sl, sig.tp, sig.tp1, sig.tp2), before)

    def test_long_short_cost_budget(self):
        for side in ("LONG", "SHORT"):
            stop = stop_price(2500., side, 50., .0022)
            self.assertAlmostEqual((abs(stop - 2500) / 2500 + .0022) * 50, .5)
        with self.assertRaises(ValueError):
            stop_price(2500., "LONG", 50., .02)

    def test_required_target_meets_nexus_net_rr(self):
        for side in ("LONG", "SHORT"):
            entry = 2500.0
            cost = .0022
            stop = stop_price(entry, side, 50., cost)
            target = required_target_price(entry, stop, side, cost, 1.60)
            stop_distance = abs(entry - stop) / entry
            target_distance = abs(target - entry) / entry
            net_rr = (target_distance - cost) / (stop_distance + cost)
            self.assertAlmostEqual(net_rr, 1.60, places=10)
            if side == "LONG":
                self.assertTrue(stop < entry < target)
            else:
                self.assertTrue(target < entry < stop)

    async def test_farther_existing_target_is_not_reduced(self):
        from unittest.mock import patch
        from bot.config import cfg
        class Engine:
            async def _open(self, sig):
                return sig.tp
            _check_stagnation_and_invalidation = AsyncMock()
        install(Engine, SimpleNamespace(info=lambda *a: None, error=lambda *a: None))
        engine = Engine()
        engine.paper_trade = False
        engine.pilot = SimpleNamespace(enabled=True)
        sig = SimpleNamespace(symbol="ETHUSDT", entry=2500., direction="LONG", sl=2490., tp=2700., tp1=2600., tp2=2700.)
        with patch.object(cfg, "LEVERAGE", 50):
            result = await engine._open(sig)
        self.assertEqual(result, 2700.)
        self.assertEqual(sig.tp, 2700.)

    def test_margin_rounds_down(self):
        # F-003: quantity comes from the risk budget and is floored to whole lots;
        # the margin ceiling can only reduce it.
        from bot.professional_risk import CapitalState, stop_risk_size
        out = stop_risk_size(capital=CapitalState(23.0667, 23.0667), entry=2531.41, stop=2506.10,
                             risk_pct=0.01, leverage=50, qty_step=0.01, min_qty=0.01,
                             max_margin_pct=0.10, fee_rate_per_side=0.0006,
                             expected_slippage_pct=0.001)
        self.assertEqual(out.qty, 0.0, "one ETH lot loses more than 1% of 23 USDT")
        self.assertEqual(out.rejection_reason, "MIN_CONTRACT_EXCEEDS_RISK_BUDGET")
        out = stop_risk_size(capital=CapitalState(2306.67, 2306.67), entry=2531.41, stop=2506.10,
                             risk_pct=0.01, leverage=50, qty_step=0.01, min_qty=0.01,
                             max_margin_pct=0.10, fee_rate_per_side=0.0006,
                             expected_slippage_pct=0.001)
        self.assertLessEqual(out.projected_stop_loss, 23.0667 + 1e-9)
        self.assertLessEqual(out.required_margin, 2306.67 * 0.10 + 1e-9)

    async def test_live_discretionary_exit_disabled_paper_retained(self):
        original = AsyncMock()
        class Engine:
            _open = AsyncMock()
            _check_stagnation_and_invalidation = original
        install(Engine, SimpleNamespace())
        engine = Engine()
        engine.paper_trade = False
        engine.pilot = SimpleNamespace(enabled=True)
        await engine._check_stagnation_and_invalidation()
        original.assert_not_called()
        engine.paper_trade = True
        await engine._check_stagnation_and_invalidation()
        original.assert_awaited_once()
