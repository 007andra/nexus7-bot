"""Forensic proof: Binance LIVE sizing vs exchange minimum order (2026-09-27).

Production (read-only logs, deployment 1fa6e5db): FIL/APT/NEAR/UNI approved by
NEXUS were blocked with ``[RISK_V3_CORE] qty=0 binding=MINIMUM_ORDER`` and
``[FINAL_SIZING_INVARIANT] reason=invalid_quantity stop_risk_qty=0`` at equity
5.8827 USDT (risk budget 0.058827 = 1%). ADAUSDT at the same equity sized to
qty=23 (projected loss 0.057006) and passed.

Every test drives the REAL chain used in production:
    final_sizing_invariants hook -> ProfessionalRiskAdapter.size
    -> RiskManagerV3.size_for_stop -> quantity.quantity_rules /
       minimum_base_quantity -> professional_risk.stop_risk_size
    -> final_loss_budget.diagnose
with the production cost inputs (taker 5 bps/side, slippage allowance 20 bps).
No exchange client exists in these tests: no order can be sent.
"""
import logging
import math
import random
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import cfg
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot.professional_risk import CapitalState, _floor_step, stop_risk_size
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.quantity import minimum_base_quantity, validate_base_quantity
from bot.sizing_decomposition import decompose, format_log

EQUITY = 5.8827
BUDGET = EQUITY * 0.01  # 0.058827 USDT
TAKER = 0.0005          # production [RISK_V3_CORE] taker_bps=5.000
SLIPPAGE = 0.002        # production slippage_allowance_bps=20.000


def binance(step, min_qty, min_notional):
    return {"quantityUnit": "BASE_ASSET", "qtyStep": str(step), "minQty": str(min_qty),
            "minNotional": str(min_notional), "multiplier": 1.0, "tickSize": "0.0001"}


# Binance USD-M filters (MARKET_LOT_SIZE stepSize/minQty, MIN_NOTIONAL notional)
# consistent with production evidence: FIL/ARB caps 127.5/640.2 (step 0.1),
# ATOM qty 3.08 (step 0.01), ADA qty 23 notional 5.9685 PASS (step 1, 5 USDT).
UNI = binance("1", "1", "5")
FIL = binance("0.1", "0.1", "5")
APT = binance("0.1", "0.1", "5")
NEAR = binance("1", "1", "5")
ADA = binance("1", "1", "5")
BTC = binance("0.001", "0.001", "100")
ETH = binance("0.001", "0.001", "20")

# (symbol, metadata, entry, stop) — UNI from the incident report; others from
# production [STRATEGY_STOP_GEOMETRY] lines of the same session.
CANDIDATES = {
    "UNIUSDT": (UNI, 10.13, 10.013261),
    "FILUSDT": (FIL, 1.1438, 1.121739),
    "APTUSDT": (APT, 0.8704, 0.856937),
    "NEARUSDT": (NEAR, 5.334, 5.174647),
}


class _Log:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *args, **_kwargs):
        try:
            text = msg % args if args else str(msg)
        except (TypeError, ValueError):
            text = str(msg)
        self.records.append((level, text))

    def __getattr__(self, name):
        return lambda msg, *a, **k: self._add(name, msg, *a, **k)


class RuntimeHarness(unittest.TestCase):
    def setUp(self):
        self.old = (cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT)
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT = 50, 0.01, 0.10
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT = self.old
        logging.disable(logging.NOTSET)

    def adapter(self, symbol, entry, stop, *, equity=EQUITY, available=None):
        available = equity if available is None else available
        legacy = SimpleNamespace(balance=available, balance_confirmed=True, _ready=True)
        adapter = ProfessionalRiskAdapter(legacy)
        snap = SimpleNamespace(symbol=symbol, taker_fee=TAKER, slippage_allowance=SLIPPAGE,
                               snapshot_id=f"cost-{symbol}-test")
        adapter.set_plan(symbol=symbol, entry=entry, stop=stop, risk_pct=0.01, cost_snapshot=snap)
        adapter.update_capital(CapitalState(equity=equity, available_collateral=available))
        return adapter

    def run_hook(self, symbol, info, entry, stop, *, equity=EQUITY, available=None, direction="LONG"):
        """Execute the production final_sizing_invariants hook; return (final, risk_qty, log)."""
        available = equity if available is None else available
        adapter = self.adapter(symbol, entry, stop, equity=equity, available=available)
        module = SimpleNamespace(minimum_base_quantity=lambda _i, _p: 999.0,
                                 _final_sizing_invariants_installed=False)
        log = _Log()
        final_sizing.install(module, pilot_cap, log)
        client = SimpleNamespace(place_order=AsyncMock(), set_position_stops=AsyncMock())
        engine = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                                 _pilot_available_balance=available, risk=adapter,
                                 instruments={symbol: info}, positions={}, client=client)
        signal = SimpleNamespace(sl=stop, direction=direction, _bgx_setup_id=f"{symbol}:T")
        tokens = (pilot_cap._PILOT_ENGINE.set(engine), pilot_cap._PILOT_SYMBOL.set(symbol),
                  pilot_cap._PILOT_FINAL_QTY.set(None), pilot_cap._PILOT_SIGNAL.set(signal))
        try:
            final = module.minimum_base_quantity(info, entry)
        finally:
            pilot_cap._PILOT_SIGNAL.reset(tokens[3])
            pilot_cap._PILOT_FINAL_QTY.reset(tokens[2])
            pilot_cap._PILOT_SYMBOL.reset(tokens[1])
            pilot_cap._PILOT_ENGINE.reset(tokens[0])
        client.place_order.assert_not_awaited()      # (28) nothing is ever sent
        client.set_position_stops.assert_not_awaited()
        risk_qty = self.adapter(symbol, entry, stop, equity=equity, available=available).size(
            symbol, entry, {symbol: info})
        return final, risk_qty, log

    def loss_per_unit(self, entry, stop):
        return abs(entry - stop) + entry * TAKER * 2 + entry * SLIPPAGE

    def decomposition(self, info, entry, stop, *, equity=EQUITY, available=None):
        return decompose(info=info, equity=equity, available=equity if available is None else available,
                         entry=entry, stop=stop, risk_pct=0.01, leverage=50, max_margin_pct=0.10,
                         fee_rate_per_side=TAKER, slippage_pct=SLIPPAGE)


class IncidentReproductionTests(RuntimeHarness):
    """(3)-(6): the four observed candidates, end to end."""

    def assert_correct_block(self, symbol, expected_binding):
        info, entry, stop = CANDIDATES[symbol]
        final, risk_qty, log = self.run_hook(symbol, info, entry, stop)
        self.assertEqual(risk_qty, 0.0)          # stop_risk_qty=0
        self.assertEqual(final, 0.0)             # FINAL_SIZING_INVARIANT invalid_quantity
        blocks = [t for _, t in log.records if "reason=invalid_quantity" in t]
        self.assertTrue(blocks, log.records)
        self.assertIn("stop_risk_qty=0", blocks[0])
        d = self.decomposition(info, entry, stop)
        self.assertEqual((d["result"], d["reason"], d["binding"]),
                         ("BLOCK", "INSUFFICIENT_RISK_BUDGET", expected_binding))
        # The smallest exchange-valid order would lose more than 1% of equity.
        min_valid = Decimal(str(minimum_base_quantity(info, entry)))
        self.assertEqual(d["min_valid_qty"], min_valid)
        self.assertGreater(d["risk_at_min_valid_qty"], Decimal(str(BUDGET)))
        # And a positive risk-sized quantity below it is never rounded up.
        self.assertLess(d["rounded_qty"], d["min_valid_qty"])
        return d

    def test_03_uni_min_qty_binding(self):
        d = self.assert_correct_block("UNIUSDT", "MIN_QTY_BINDING")
        self.assertEqual(d["risk_per_unit"], Decimal("0.116739"))
        self.assertEqual(d["loss_per_unit"], Decimal("0.147129"))
        self.assertTrue(d["rounding_to_zero"])
        self.assertEqual(d["min_valid_qty"], Decimal("1"))
        self.assertEqual(d["risk_at_min_valid_qty"], Decimal("0.147129"))

    def test_04_fil_min_notional_binding(self):
        d = self.assert_correct_block("FILUSDT", "MIN_NOTIONAL_BINDING")
        self.assertEqual(d["rounded_qty"], Decimal("2.3"))
        self.assertEqual(d["min_valid_qty"], Decimal("4.4"))

    def test_05_apt_min_notional_binding(self):
        d = self.assert_correct_block("APTUSDT", "MIN_NOTIONAL_BINDING")
        self.assertEqual(d["min_valid_qty"], Decimal("5.8"))

    def test_06_near_min_qty_binding(self):
        d = self.assert_correct_block("NEARUSDT", "MIN_QTY_BINDING")
        self.assertEqual(d["min_valid_qty"], Decimal("1"))

    def test_apt_and_near_block_with_either_plausible_step(self):
        for symbol, alt in (("APTUSDT", binance("1", "1", "5")), ("NEARUSDT", binance("0.1", "0.1", "5"))):
            _, entry, stop = CANDIDATES[symbol]
            final, risk_qty, _ = self.run_hook(symbol, alt, entry, stop)
            self.assertEqual((final, risk_qty), (0.0, 0.0), symbol)

    def test_13_ada_reproduces_production_pass_exactly(self):
        # Production: qty=23 projected_stop_loss=0.057006 required_margin=0.119370 binding=RISK_BUDGET
        final, risk_qty, log = self.run_hook("ADAUSDT", ADA, 0.2595, 0.2578)
        self.assertEqual((risk_qty, final), (23.0, 23.0))
        self.assertAlmostEqual(23 * self.loss_per_unit(0.2595, 0.2578), 0.057006, places=6)
        self.assertTrue(any("result=PASS" in t and "binding=RISK_BUDGET" in t for _, t in log.records))

    def test_minimum_required_equity_is_the_exact_threshold(self):
        # Capital sufficiency proof on the real risk path (stop_risk_qty).
        for symbol, (info, entry, stop) in CANDIDATES.items():
            d = self.decomposition(info, entry, stop)
            need = float(d["required_equity_at_min_valid_qty"])
            below = self.adapter(symbol, entry, stop, equity=need * 0.999).size(symbol, entry, {symbol: info})
            above = self.adapter(symbol, entry, stop, equity=need * 1.001).size(symbol, entry, {symbol: info})
            self.assertEqual(below, 0.0, symbol)
            self.assertEqual(above, float(d["min_valid_qty"]), symbol)

    def test_final_loss_budget_warn_blocks_only_candidate_at_50x(self):
        # RiskManagerV3 may produce a positive stop-risk quantity, but the final
        # loss-budget geometry is now an independent candidate-level gate.
        # A WARN rejects this candidate without pausing the runtime/scanner.
        for symbol, (info, entry, stop) in CANDIDATES.items():
            final, risk_qty, log = self.run_hook(symbol, info, entry, stop, equity=1000.0)
            self.assertGreater(risk_qty, 0.0, symbol)
            self.assertEqual(final, 0.0, symbol)
            self.assertTrue(
                any(
                    "result=WARN" in t
                    and "projected_loss_exceeds_50pct_entry_margin" in t
                    and "execution_effect=OBSERVABILITY_ONLY" in t
                    for _, t in log.records
                ),
                symbol,
            )
            self.assertTrue(
                any(
                    "[FINAL_SIZING_INVARIANT]" in t
                    and "result=BLOCK" in t
                    and "candidate_only=true" in t
                    and "runtime_paused=false" in t
                    for _, t in log.records
                ),
                symbol,
            )


class VenueMetadataTests(RuntimeHarness):
    def test_01_btc_fractional_step(self):
        final, risk_qty, _ = self.run_hook("BTCUSDT", BTC, 65000.0, 64700.0)
        self.assertEqual((final, risk_qty), (0.0, 0.0))  # min 0.002 BTC notional 130 > risk
        final, risk_qty, _ = self.run_hook("BTCUSDT", BTC, 65000.0, 64700.0, equity=200.0)
        self.assertEqual(risk_qty, 0.004)
        validate_base_quantity(final, BTC, 65000.0)
        self.assertLessEqual(final * self.loss_per_unit(65000.0, 64700.0), 2.0 + 1e-9)

    def test_02_eth_fractional_step(self):
        final, risk_qty, _ = self.run_hook("ETHUSDT", ETH, 3500.0, 3485.0)
        self.assertEqual(risk_qty, 0.0)
        final, risk_qty, _ = self.run_hook("ETHUSDT", ETH, 3500.0, 3485.0, equity=50.0)
        self.assertEqual(risk_qty, 0.019)
        self.assertEqual(final, 0.019)
        validate_base_quantity(final, ETH, 3500.0)

    def test_07_08_integer_lot_and_below_one_step(self):
        d = self.decomposition(UNI, 10.13, 10.013261)
        self.assertLess(d["raw_qty"], 1)
        self.assertEqual(d["rounded_qty"], 0)
        self.assertTrue(d["rounding_to_zero"])

    def test_09_11_below_min_qty_binding(self):
        info = binance("0.1", "1", "0")
        # loss/unit = 0.05 price + 0.01 fees + 0.02 slippage = 0.08 -> raw 0.735
        entry, stop = 10.0, 9.95
        d = self.decomposition(info, entry, stop)
        self.assertEqual((d["rounded_qty"], d["min_valid_qty"]), (Decimal("0.7"), Decimal("1")))
        self.assertEqual((d["reason"], d["binding"]), ("INSUFFICIENT_RISK_BUDGET", "MIN_QTY_BINDING"))
        _, risk_qty, _ = self.run_hook("TSTUSDT", info, entry, stop)
        self.assertEqual(risk_qty, 0.0)

    def test_10_min_notional_binding(self):
        d = self.decomposition(FIL, 1.1438, 1.121739)
        self.assertEqual(d["binding"], "MIN_NOTIONAL_BINDING")
        self.assertGreater(d["min_valid_qty"], d["min_qty"])

    def test_12_margin_cap_binding(self):
        d = self.decomposition(ADA, 0.2595, 0.2578, available=0.05)
        self.assertEqual((d["result"], d["reason"]), ("BLOCK", "MARGIN_CAP_BINDING"))
        final, risk_qty, _ = self.run_hook("ADAUSDT", ADA, 0.2595, 0.2578, available=0.05)
        self.assertEqual((final, risk_qty), (0.0, 0.0))

    def test_19_20_malformed_or_missing_metadata_fail_closed(self):
        bad = [binance("0.3", "0.1", "5"), binance("-1", "1", "5"), binance("0", "1", "5"),
               {"quantityUnit": "BASE_ASSET", "minQty": "1"}, {}]
        for info in bad:
            adapter = self.adapter("XUSDT", 1.0, 0.99)
            self.assertEqual(adapter.size("XUSDT", 1.0, {"XUSDT": info}), 0.0, info)
            self.assertEqual(self.decomposition(info, 1.0, 0.99)["reason"], "INVALID_METADATA", info)
        self.assertEqual(self.adapter("XUSDT", 1.0, 0.99).size("XUSDT", 1.0, {}), 0.0)

    def test_21_zero_stop_distance_fails_closed(self):
        with self.assertRaises(ValueError):
            self.adapter("XUSDT", 1.0, 1.0)
        with self.assertRaises(ValueError):
            stop_risk_size(capital=CapitalState(10, 10), entry=1.0, stop=1.0, risk_pct=0.01,
                           leverage=50, qty_step=1, min_qty=1, max_margin_pct=0.1)

    def test_22_invalid_equity_fails_closed(self):
        for equity in (-1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                CapitalState(equity, 1.0).validate()
        legacy = SimpleNamespace(balance=5.0, balance_confirmed=True, _ready=True)
        adapter = ProfessionalRiskAdapter(legacy)
        adapter.set_plan(symbol="ADAUSDT", entry=0.2595, stop=0.2578, risk_pct=0.01)
        self.assertEqual(adapter.size("ADAUSDT", 0.2595, {"ADAUSDT": ADA}), 0.0)  # unconfirmed
        self.assertEqual(adapter.size("ADAUSDT", 0.2595, {"ADAUSDT": ADA}), 0.0)


class RoundingAndLeverageTests(RuntimeHarness):
    def core(self, *, equity, entry, stop, step, min_qty, leverage=50, available=None, fee=0.0, slip=0.0):
        return stop_risk_size(capital=CapitalState(equity, equity if available is None else available),
                              entry=entry, stop=stop, risk_pct=0.01, leverage=leverage, qty_step=step,
                              min_qty=min_qty, max_margin_pct=0.10,
                              fee_rate_per_side=fee, expected_slippage_pct=slip)

    def test_14_exact_boundary(self):
        # risk_budget 1.00 / loss 0.50 per unit = exactly 2.0 units
        self.assertEqual(self.core(equity=100, entry=10, stop=9.5, step=1, min_qty=1).qty, 2.0)
        self.assertEqual(self.core(equity=99.999, entry=10, stop=9.5, step=1, min_qty=1).qty, 1.0)
        # raw exactly at minQty passes; just below it blocks
        self.assertEqual(self.core(equity=50, entry=10, stop=9.5, step=1, min_qty=1).qty, 1.0)
        res = self.core(equity=49.999, entry=10, stop=9.5, step=1, min_qty=1)
        self.assertEqual((res.qty, res.binding_constraint), (0.0, "MINIMUM_ORDER"))
        # immediately above minQty (step 0.1)
        self.assertEqual(self.core(equity=55, entry=10, stop=9.5, step=0.1, min_qty=1).qty, 1.1)

    def test_15_decimal_epsilon(self):
        self.assertEqual(_floor_step(0.1 + 0.2, 0.1), 0.3)          # 0.30000000000000004
        self.assertEqual(_floor_step(2.9999999999999996, 1.0), 2.0)  # never rounded up
        self.assertEqual(_floor_step(0.0999999999, 0.1), 0.0)
        self.assertEqual(_floor_step(0.1, 0.1), 0.1)

    def test_16_17_leverage_changes_margin_never_stop_loss(self):
        results = [self.core(equity=1000, entry=100, stop=99, step=0.001, min_qty=0.001,
                             leverage=lev, available=1_000_000) for lev in (1, 10, 50, 125)]
        # risk-bound at every leverage: identical qty and identical loss at SL
        self.assertEqual({r.qty for r in results}, {results[0].qty})
        self.assertEqual({r.projected_stop_loss for r in results}, {results[0].projected_stop_loss})
        self.assertLessEqual(results[0].projected_stop_loss, 10.0 + 1e-9)
        # margin = notional / leverage
        for lev, r in zip((1, 10, 50, 125), results):
            self.assertAlmostEqual(r.required_margin, r.notional / lev)
        # with little collateral, leverage raises only the margin ceiling, never above risk qty
        low = [self.core(equity=1000, entry=100, stop=99, step=0.001, min_qty=0.001,
                         leverage=lev, available=5.0) for lev in (1, 125)]
        self.assertLess(low[0].qty, low[1].qty)
        self.assertLessEqual(low[1].qty, results[0].qty)
        self.assertLessEqual(low[1].projected_stop_loss, 10.0 + 1e-9)

    def test_18_23_24_26_property_never_above_budget_and_exchange_valid(self):
        rng = random.Random(20260927)
        for _ in range(3000):
            step = rng.choice([0.001, 0.01, 0.1, 1.0])
            min_qty = step * rng.choice([1, 1, 2, 10])
            entry = rng.uniform(0.05, 70000)
            stop = entry * (1 - rng.uniform(0.0005, 0.05))
            equity = rng.uniform(1, 5000)
            r = self.core(equity=equity, entry=entry, stop=stop, step=step, min_qty=min_qty,
                          fee=TAKER, slip=SLIPPAGE)
            if r.qty == 0:
                continue
            loss = r.qty * self.loss_per_unit(entry, stop)
            self.assertLessEqual(loss, equity * 0.01 * (1 + 1e-6))
            units = Decimal(str(r.qty)) / Decimal(str(step))
            self.assertEqual(units, units.to_integral_value())
            self.assertGreaterEqual(r.qty, min_qty - 1e-12)

    def test_25_27_pass_path_satisfies_notional_and_operator_cap(self):
        final, risk_qty, log = self.run_hook("ADAUSDT", ADA, 0.2595, 0.2578)
        validate_base_quantity(final, ADA, 0.2595)
        self.assertGreaterEqual(final * 0.2595, 5.0)
        cap = float(next(t for _, t in log.records
                         if "[FINAL_SIZING_INVARIANT]" in t and "operator_margin_cap_qty=" in t)
                    .split("operator_margin_cap_qty=")[1].split()[0])
        self.assertLessEqual(final, cap)
        self.assertEqual(final, min(risk_qty, cap))


class ObservabilityOnlyTests(RuntimeHarness):
    def test_decomposition_log_is_emitted_and_never_changes_quantity(self):
        info, entry, stop = CANDIDATES["UNIUSDT"]
        with self.assertLogs("kakazito-trade", level="WARNING") as logs:
            logging.disable(logging.NOTSET)
            qty = self.adapter("UNIUSDT", entry, stop).size("UNIUSDT", entry, {"UNIUSDT": info})
        self.assertEqual(qty, 0.0)
        line = next(x for x in logs.output if "[SIZING_DECOMPOSITION]" in x)
        for fragment in ("symbol=UNIUSDT", "risk_budget=0.058827", "min_valid_qty=1",
                         "binding=MIN_QTY_BINDING", "reason=INSUFFICIENT_RISK_BUDGET",
                         "result=BLOCK", "decision_effect=NONE"):
            self.assertIn(fragment, line)
        # A failing decomposition can never change the sizing result.
        with patch("bot.sizing_decomposition.decompose", side_effect=RuntimeError("boom")):
            again = self.adapter("UNIUSDT", entry, stop).size("UNIUSDT", entry, {"UNIUSDT": info})
        self.assertEqual(again, qty)

    def test_29_30_rules_and_thresholds_unchanged(self):
        self.assertEqual(final_sizing.MARGIN_FRACTION, 0.50)
        self.assertEqual(final_sizing.FINAL_QUANTITY_POLICY, "min(stop_risk_qty,operator_margin_cap_qty)")
        before = (cfg.MAX_RISK_PCT, cfg.LEVERAGE, cfg.MAX_DRAWDOWN)
        self.run_hook("ADAUSDT", ADA, 0.2595, 0.2578)
        self.assertEqual((cfg.MAX_RISK_PCT, cfg.LEVERAGE, cfg.MAX_DRAWDOWN), before)

    def test_decomposition_log_contains_no_secret_like_fields(self):
        d = self.decomposition(UNI, 10.13, 10.013261)
        line = format_log("UNIUSDT", d)
        for banned in ("key", "secret", "signature", "token"):
            self.assertNotIn(banned, line.lower())

    def test_drawdown_headroom_arithmetic(self):
        peak, equity, limit = Decimal("6.4680"), Decimal("5.8827"), Decimal("0.10")
        hard_stop_equity = peak * (1 - limit)
        self.assertEqual(hard_stop_equity, Decimal("5.82120"))
        self.assertEqual(equity - hard_stop_equity, Decimal("0.06150"))
        self.assertTrue(math.isclose(float((equity - hard_stop_equity) / Decimal(str(BUDGET))), 1.0454, rel_tol=1e-3))


if __name__ == "__main__":
    unittest.main()
