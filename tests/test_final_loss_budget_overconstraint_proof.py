import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import binance_cross_portfolio_stress as cross_stress
from bot import final_loss_budget
from bot.config import cfg
from bot.final_sizing_invariants import _operator_target_quantity, _select_final_quantity
from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3


ENTRY = 100.0
EQUITY = 1000.0
AVAILABLE = 1000.0
RISK_PCT = 0.01
LEVERAGE = 50.0
FEE_PER_SIDE = 0.0006
ROUND_TRIP_SLIPPAGE = 0.0020
STRESS_COST_FRACTION = 2.0 * FEE_PER_SIDE + ROUND_TRIP_SLIPPAGE

INFO = {
    "quantityUnit": "BASE_ASSET",
    "qtyStep": "0.001",
    "minQty": "0.001",
    "minNotional": "5",
}


class FakeBinanceClient:
    async def get_account_state(self):
        return {
            "crossWalletBalance": EQUITY,
            "orderMargin": 0.0,
            "multiAssetsMargin": False,
            "canTrade": True,
        }

    async def get_positions(self):
        return []

    async def get_symbol_config(self, symbol):
        return {"symbol": symbol, "marginType": "CROSSED"}

    async def get_leverage_brackets(self, symbol):
        return {
            "symbol": symbol,
            "source": "BINANCE_FAPI_LEVERAGE_BRACKET",
            "brackets": [
                {
                    "bracket": 1,
                    "initialLeverage": 125,
                    "notionalFloor": 0.0,
                    "notionalCap": 1_000_000.0,
                    "maintMarginRatio": 0.005,
                    "cum": 0.0,
                }
            ],
        }


def signal(stop_pct):
    return SimpleNamespace(
        symbol="ALTUSDT",
        direction="LONG",
        entry=ENTRY,
        sl=ENTRY * (1.0 - stop_pct),
    )


def risk_sizing(stop_pct, leverage=LEVERAGE):
    risk = RiskManagerV3()
    risk.update_capital(CapitalState(EQUITY, AVAILABLE))
    return risk.size_for_stop(
        symbol="ALTUSDT",
        entry=ENTRY,
        stop=ENTRY * (1.0 - stop_pct),
        instruments={"ALTUSDT": INFO},
        risk_pct=RISK_PCT,
        leverage=leverage,
        max_margin_pct=0.80,
        fee_rate_per_side=FEE_PER_SIDE,
        expected_slippage_pct=ROUND_TRIP_SLIPPAGE,
    )


class FinalLossBudgetOverconstraintProof(unittest.IsolatedAsyncioTestCase):
    async def test_risk_and_cross_pass_while_final_loss_budget_blocks_wider_stops(self):
        engine = SimpleNamespace(
            paper_trade=False,
            positions={},
            client=FakeBinanceClient(),
            instruments={"ALTUSDT": INFO},
        )

        blocked_stop_pcts = (0.007, 0.010, 0.020, 0.030, 0.050)

        with patch.object(cfg, "LEVERAGE", int(LEVERAGE)):
            for stop_pct in blocked_stop_pcts:
                with self.subTest(stop_pct=stop_pct):
                    sizing = risk_sizing(stop_pct)

                    # RiskManagerV3 is the monetary-risk authority. The final
                    # quantity remains within the configured 1% equity budget.
                    self.assertGreater(sizing.qty, 0.0)
                    self.assertLessEqual(
                        sizing.projected_stop_loss,
                        sizing.risk_budget * 1.000001,
                    )
                    self.assertAlmostEqual(sizing.risk_budget, EQUITY * RISK_PCT)

                    # The 50%-available operator margin cap does not bind this
                    # setup; final sizing is therefore exactly the risk quantity.
                    operator_qty = _operator_target_quantity(
                        INFO, ENTRY, AVAILABLE, LEVERAGE
                    )
                    final_qty = _select_final_quantity(
                        target_qty=operator_qty,
                        risk_qty=sizing.qty,
                    )
                    self.assertEqual(final_qty, sizing.qty)
                    self.assertLessEqual(
                        (final_qty * ENTRY) / LEVERAGE,
                        AVAILABLE * 0.50,
                    )

                    # Binance CROSS solvency/liquidation stress independently
                    # passes the exact risk-sized candidate.
                    cross = await cross_stress.evaluate(
                        engine, signal(stop_pct), final_qty
                    )
                    self.assertTrue(cross.allowed, cross)
                    self.assertLess(
                        cross.risk_rate,
                        cross_stress.MAX_STOP_STRESS_RISK_RATE,
                    )

                    # Despite both current risk authorities passing, the legacy
                    # projected-loss ceiling blocks solely on stop geometry:
                    # stop_fraction + 0.32% > 0.5 / 50 = 1.00%.
                    with self.assertRaisesRegex(
                        ValueError,
                        "projected loss exceeds 50pct entry margin",
                    ):
                        final_loss_budget.validate(
                            final_qty,
                            ENTRY,
                            signal(stop_pct).sl,
                            "LONG",
                            LEVERAGE,
                            STRESS_COST_FRACTION,
                        )

    async def test_narrow_control_passes_all_three_layers(self):
        stop_pct = 0.006  # 0.60% + 0.32% costs = 0.92% < 1.00%.
        sizing = risk_sizing(stop_pct)
        self.assertGreater(sizing.qty, 0.0)

        operator_qty = _operator_target_quantity(INFO, ENTRY, AVAILABLE, LEVERAGE)
        final_qty = _select_final_quantity(
            target_qty=operator_qty,
            risk_qty=sizing.qty,
        )
        engine = SimpleNamespace(
            paper_trade=False,
            positions={},
            client=FakeBinanceClient(),
            instruments={"ALTUSDT": INFO},
        )

        with patch.object(cfg, "LEVERAGE", int(LEVERAGE)):
            cross = await cross_stress.evaluate(engine, signal(stop_pct), final_qty)

        self.assertTrue(cross.allowed, cross)
        projected, ceiling = final_loss_budget.validate(
            final_qty,
            ENTRY,
            signal(stop_pct).sl,
            "LONG",
            LEVERAGE,
            STRESS_COST_FRACTION,
        )
        self.assertLess(projected, ceiling)

    def test_loss_budget_changes_with_leverage_while_risk_budget_does_not(self):
        stop_pct = 0.020
        at_10x = risk_sizing(stop_pct, leverage=10.0)
        at_50x = risk_sizing(stop_pct, leverage=50.0)

        # When collateral is not binding, leverage does not change monetary
        # stop-risk sizing or the configured equity risk budget.
        self.assertEqual(at_10x.qty, at_50x.qty)
        self.assertEqual(at_10x.risk_budget, at_50x.risk_budget)
        self.assertLessEqual(at_10x.projected_stop_loss, at_10x.risk_budget * 1.000001)
        self.assertLessEqual(at_50x.projected_stop_loss, at_50x.risk_budget * 1.000001)

        # The legacy loss budget is different: the same 2% technical geometry
        # passes at 10x (5% of notional ceiling) and fails at 50x (1% ceiling).
        final_loss_budget.validate(
            at_10x.qty,
            ENTRY,
            signal(stop_pct).sl,
            "LONG",
            10.0,
            STRESS_COST_FRACTION,
        )
        with self.assertRaisesRegex(
            ValueError,
            "projected loss exceeds 50pct entry margin",
        ):
            final_loss_budget.validate(
                at_50x.qty,
                ENTRY,
                signal(stop_pct).sl,
                "LONG",
                50.0,
                STRESS_COST_FRACTION,
            )

    def test_quantity_cancels_from_loss_budget_boundary(self):
        stop_pct = 0.020
        stop = signal(stop_pct).sl
        for qty in (0.001, 1.0, 1000.0):
            with self.subTest(qty=qty):
                with self.assertRaisesRegex(
                    ValueError,
                    "projected loss exceeds 50pct entry margin",
                ):
                    final_loss_budget.validate(
                        qty,
                        ENTRY,
                        stop,
                        "LONG",
                        LEVERAGE,
                        STRESS_COST_FRACTION,
                    )


if __name__ == "__main__":
    unittest.main()
