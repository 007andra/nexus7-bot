import unittest

from bot.binance_usdm_simulator import (
    FundingEvent,
    SimBar,
    SymbolRules,
    cross_liquidation_price,
    funding_cashflow,
    normalize_market_qty,
    simulate_trade_path,
    vwap_market_fill,
)
from bot.funding_aware_ev import (
    ExpectedFunding,
    adjust_ev_for_funding,
    funding_fraction_for_hold,
)
from bot.liquidity_depth_model import depth_capacity, liquidity_capped_qty


def _brackets():
    return {
        "brackets": [
            {
                "bracket": 1,
                "initialLeverage": 50,
                "notionalFloor": 0.0,
                "notionalCap": 100000.0,
                "maintMarginRatio": 0.01,
                "cum": 0.0,
            }
        ]
    }


class FilterAndDepthTests(unittest.TestCase):
    def test_filters_floor_quantity_and_enforce_notional(self):
        rules = SymbolRules(0.1, 0.01, 0.01, 5.0)
        self.assertEqual(normalize_market_qty(1.239, 100.0, rules), 1.23)
        with self.assertRaisesRegex(ValueError, "MIN_NOTIONAL"):
            normalize_market_qty(0.01, 100.0, rules)

    def test_vwap_partial_fill_and_impact(self):
        book = {
            "a": [["100", "1"], ["101", "1"]],
            "b": [["99", "1"], ["98", "1"]],
        }
        full = vwap_market_fill(book, "BUY", 2.0)
        self.assertFalse(full.partial)
        self.assertAlmostEqual(full.average_price, 100.5)
        self.assertGreater(full.impact_bps, 0)
        partial = vwap_market_fill(book, "BUY", 3.0)
        self.assertTrue(partial.partial)
        self.assertEqual(partial.filled_qty, 2.0)

    def test_depth_capacity_and_binding(self):
        book = {
            "a": [["100", "1"], ["100.1", "2"], ["105", "10"]],
            "b": [["99.9", "3"]],
        }
        cap = depth_capacity(book, order_side="BUY", max_impact_bps=10)
        self.assertGreaterEqual(cap["capacity_qty"], 1.0)
        qty = liquidity_capped_qty(
            risk_budget_qty=5,
            margin_cap_qty=4,
            depth_cap_qty=3,
        )
        self.assertEqual(qty["qty"], 3)
        self.assertEqual(qty["binding"], "liquidity_cap")


class FundingAndLiquidationTests(unittest.TestCase):
    def test_funding_direction_is_correct(self):
        self.assertAlmostEqual(
            funding_cashflow("LONG", 1, 100, 0.001),
            -0.1,
        )
        self.assertAlmostEqual(
            funding_cashflow("SHORT", 1, 100, 0.001),
            0.1,
        )

    def test_funding_ev_only_counts_crossed_settlements(self):
        events = [
            ExpectedFunding(1000, 0.001),
            ExpectedFunding(2000, -0.002),
        ]
        pnl = funding_fraction_for_hold(
            side="LONG",
            opened_at=500,
            expected_hold_seconds=700,
            events=events,
        )
        self.assertAlmostEqual(pnl, -0.001)
        adjusted = adjust_ev_for_funding(
            base_ev_pct=0.50,
            funding_pnl_fraction=pnl,
        )
        self.assertAlmostEqual(adjusted["funding_adjusted_ev_pct"], 0.40)
        self.assertEqual(adjusted["execution_effect"], "NONE")

    def test_cross_liquidation_price_is_below_long_entry(self):
        price = cross_liquidation_price(
            wallet_balance=10,
            entry=100,
            qty=4,
            position_side="LONG",
            bracket_payload=_brackets(),
            taker_fee_rate=0.0005,
        )
        self.assertIsNotNone(price)
        self.assertLess(price, 100)


class PathSimulationTests(unittest.TestCase):
    def test_stop_first_same_bar(self):
        bars = [
            SimBar(1000, 100, 105, 95, 102),
        ]
        result = simulate_trade_path(
            side="LONG",
            qty=1,
            entry_reference=100,
            stop_loss=98,
            take_profit=104,
            bars=bars,
            taker_fee_rate=0,
            entry_slippage_rate=0,
            exit_slippage_rate=0,
        )
        self.assertEqual(result.exit_reason, "STOP_MARKET")
        self.assertEqual(result.exit_fill, 98)

    def test_gap_through_stop_uses_gap_open_plus_adverse_slippage(self):
        bars = [
            SimBar(1000, 95, 97, 94, 96),
        ]
        result = simulate_trade_path(
            side="LONG",
            qty=1,
            entry_reference=100,
            stop_loss=98,
            take_profit=104,
            bars=bars,
            taker_fee_rate=0,
            entry_slippage_rate=0,
            exit_slippage_rate=0.001,
        )
        self.assertEqual(result.exit_reason, "STOP_MARKET")
        self.assertAlmostEqual(result.exit_fill, 95 * 0.999)

    def test_funding_and_fees_flow_into_net_pnl(self):
        bars = [
            SimBar(1000, 100, 101, 99, 100),
            SimBar(2000, 100, 104, 99, 104),
        ]
        result = simulate_trade_path(
            side="LONG",
            qty=1,
            entry_reference=100,
            stop_loss=98,
            take_profit=104,
            bars=bars,
            taker_fee_rate=0.001,
            entry_slippage_rate=0,
            exit_slippage_rate=0,
            funding_events=[FundingEvent(1500, 0.001, 101)],
        )
        self.assertEqual(result.exit_reason, "TAKE_PROFIT_MARKET")
        self.assertLess(result.net_pnl, result.gross_pnl)
        self.assertLess(result.funding_pnl, 0)
        self.assertEqual(result.execution_effect, "NONE")


if __name__ == "__main__":
    unittest.main()
