"""F-003 risk-budget sizing: invariants, barrier, open-risk cap, property tests (offline)."""
import math
import random
import unittest
from types import SimpleNamespace

from bot import risk_budget as rb
from bot.config import cfg
from bot.professional_risk import CapitalState, stop_risk_size

COST_MAJOR, COST_ALT = 0.0022, 0.0032


def _size(equity, available, entry, stop, mult, risk_pct=0.01, leverage=10, cost=COST_MAJOR,
          margin_pct=0.10):
    fee = 0.0006
    return stop_risk_size(capital=CapitalState(equity, available), entry=entry, stop=stop,
                          risk_pct=risk_pct, leverage=leverage, qty_step=mult, min_qty=mult,
                          max_margin_pct=margin_pct, fee_rate_per_side=fee,
                          expected_slippage_pct=cost - 2 * fee)


def _loss(qty, entry, stop, cost):
    return qty * (abs(entry - stop) + entry * cost)


class CanonicalCostTests(unittest.TestCase):
    def test_alt_is_never_costed_as_major(self):
        self.assertAlmostEqual(rb.cost_fraction("SOLUSDT"), COST_MAJOR)
        self.assertAlmostEqual(rb.cost_fraction("BTCUSDT"), COST_MAJOR)
        self.assertAlmostEqual(rb.cost_fraction("AVAXUSDT"), COST_ALT)
        self.assertAlmostEqual(rb.cost_fraction("DOGEUSDT"), COST_ALT)

    def test_same_stop_alt_gets_fewer_or_equal_contracts(self):
        major = _size(1000, 1000, 30.0, 29.7, 0.1, cost=rb.cost_fraction("SOLUSDT"))
        alt = _size(1000, 1000, 30.0, 29.7, 0.1, cost=rb.cost_fraction("AVAXUSDT"))
        self.assertLess(alt.qty, major.qty)


class InvariantTests(unittest.TestCase):
    def _assert(self, **over):
        kw = dict(symbol="SOLUSDT", contracts=5, multiplier=0.1, entry=150.0, stop=148.5,
                  direction="LONG", cost_fraction=COST_MAJOR, equity=100.0, risk_pct=0.01)
        kw.update(over)
        return rb.assert_projected_loss_within_budget(**kw)

    def test_example_one_percent_of_100(self):
        self.assertLessEqual(self._assert()["projected_loss"], 1.0)
        with self.assertRaises(rb.RiskBudgetRefused) as ctx:
            self._assert(contracts=33, stop=144.0)      # the old SOL 33c @ 4% stop
        self.assertEqual(ctx.exception.reason, "PROJECTED_LOSS_EXCEEDS_RISK_BUDGET")

    def test_invalid_inputs_fail_closed(self):
        cases = [dict(equity=float("nan")), dict(equity=float("inf")), dict(equity=0),
                 dict(equity=-1), dict(entry=0), dict(entry=-150), dict(multiplier=0),
                 dict(multiplier=float("nan")), dict(contracts=0), dict(contracts=1.5),
                 dict(stop=150.0), dict(stop=151.0), dict(risk_pct=0), dict(risk_pct=-0.01),
                 dict(risk_pct=0.06), dict(risk_pct=0.5), dict(risk_pct=float("nan")),
                 dict(cost_fraction=-0.001), dict(cost_fraction=float("inf")),
                 dict(direction="FLAT"), dict(equity=True)]
        for case in cases:
            with self.assertRaises(rb.RiskBudgetRefused, msg=case):
                self._assert(**case)

    def test_hard_ceiling_is_structural(self):
        self.assertEqual(rb.validate_risk_pct(0.05), 0.05)
        with self.assertRaises(rb.RiskBudgetRefused) as ctx:
            rb.validate_risk_pct(0.0500001)
        self.assertEqual(ctx.exception.reason, "RISK_PCT_ABOVE_HARD_CEILING")


class RoundingAndMinContractTests(unittest.TestCase):
    def test_fine_contract_lands_just_under_budget(self):
        # AVAX: 0.1 x 30 per contract; stop 2% -> 0.0696 USDT/contract -> 14 contracts.
        out = _size(100, 100, 30.0, 29.4, 0.1, cost=COST_ALT, margin_pct=1.0)
        self.assertAlmostEqual(out.qty, 1.4)
        self.assertLessEqual(_loss(out.qty, 30.0, 29.4, COST_ALT), 1.0)
        self.assertGreater(_loss(out.qty + 0.1, 30.0, 29.4, COST_ALT), 1.0, "one more step breaks it")

    def test_btc_one_contract_above_budget_is_no_trade(self):
        out = _size(50, 50, 60000.0, 59400.0, 0.001)
        self.assertEqual((out.qty, out.rejection_reason), (0.0, rb.MIN_CONTRACT_EXCEEDS_RISK_BUDGET))

    def test_never_rounds_up(self):
        out = _size(100, 100, 150.0, 148.5, 0.1)
        self.assertAlmostEqual(out.qty, 0.5)                # raw 5.46 contracts -> 5


class MarginAndLeverageTests(unittest.TestCase):
    def test_margin_only_reduces(self):
        free = _size(1000, 1000, 150.0, 149.625, 0.1, margin_pct=1.0)
        tight = _size(1000, 100, 150.0, 149.625, 0.1)
        self.assertEqual(tight.binding_constraint, "AVAILABLE_COLLATERAL")
        self.assertLess(tight.qty, free.qty)
        self.assertLess(tight.projected_stop_loss, free.projected_stop_loss)

    def test_leverage_stable_loss_when_trade_fits_all(self):
        losses = {lev: _size(1000, 5000, 150.0, 148.5, 0.1, leverage=lev).projected_stop_loss
                  for lev in (5, 10, 20, 50)}
        self.assertEqual(len({round(v, 9) for v in losses.values()}), 1, losses)
        margins = {lev: _size(1000, 5000, 150.0, 148.5, 0.1, leverage=lev).required_margin
                   for lev in (5, 10, 20, 50)}
        self.assertGreater(margins[5], margins[50])


class OpenRiskCapTests(unittest.TestCase):
    def _engine(self, positions, external=()):
        return SimpleNamespace(positions=positions, _external_position_symbols=set(external))

    def test_two_reservations_of_one_percent_block_any_additional_risk(self):
        engine = self._engine({"BTCUSDT": SimpleNamespace(_risk_reserved_usdt=1.0),
                               "ETHUSDT": SimpleNamespace(_risk_reserved_usdt=1.0)})
        with self.assertRaises(rb.RiskBudgetRefused) as ctx:
            rb.assert_open_risk_within_cap(engine, symbol="SOLUSDT", proposed=0.01,
                                           equity=100, risk_pct=0.01, open_pct=0.02)
        self.assertEqual(ctx.exception.reason, "OPEN_RISK_CAP_EXCEEDED")
        one = self._engine({"BTCUSDT": SimpleNamespace(_risk_reserved_usdt=1.0)})
        self.assertEqual(rb.assert_open_risk_within_cap(
            one, symbol="SOLUSDT", proposed=1.0, equity=100, risk_pct=0.01, open_pct=0.02),
            (1.0, 2.0))

    def test_reservation_is_not_released_by_trailing_to_breakeven(self):
        pos = SimpleNamespace(_risk_reserved_usdt=1.0, qty=0.5, entry=150.0, sl=150.0)
        self.assertEqual(rb.position_reserved_risk(pos, equity=100, risk_pct=0.01), 1.0)

    def test_unstamped_position_reserves_at_least_full_budget(self):
        restored = SimpleNamespace(qty=33 * 0.1, entry=150.0, initial_sl=144.0, sl=150.0,
                                   symbol="SOLUSDT")
        self.assertGreater(rb.position_reserved_risk(restored, equity=100, risk_pct=0.01), 20.0)
        small = SimpleNamespace(qty=0.1, entry=150.0, initial_sl=None, sl=149.0, symbol="SOLUSDT")
        self.assertEqual(rb.position_reserved_risk(small, equity=100, risk_pct=0.01), 1.0)

    def test_unresolvable_position_fails_closed(self):
        engine = self._engine({"SOLUSDT": SimpleNamespace(qty=None, entry=150.0, sl=149.0)})
        with self.assertRaises(rb.RiskBudgetRefused) as ctx:
            rb.reserved_open_risk(engine, equity=100, risk_pct=0.01)
        self.assertEqual(ctx.exception.reason, "OPEN_RISK_UNRESOLVED")

    def test_external_position_has_no_invented_budget(self):
        engine = self._engine({"XRPUSDT": SimpleNamespace(qty=1.0, entry=1.0, sl=0.9)},
                              external={"XRPUSDT"})
        self.assertEqual(rb.reserved_open_risk(engine, equity=100, risk_pct=0.01), (0.0, {}))

    def test_policy_never_reserves_more_than_daily_stop(self):
        self.assertLess(cfg.MAX_RISK_PCT, cfg.MAX_OPEN_RISK_PCT + 1e-12)
        self.assertLess(cfg.MAX_OPEN_RISK_PCT, cfg.DAILY_STOP_LOSS_PCT)
        self.assertLessEqual(cfg.MAX_POSITIONS * cfg.MAX_RISK_PCT, cfg.MAX_OPEN_RISK_PCT + 1e-12)


class TransportBarrierTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(_instruments={"SOLUSDT": {"multiplier": 0.1}})
        self.auth = rb.RiskAuthorization(
            symbol="SOLUSDT", side="buy", direction="LONG", contracts=5, multiplier=0.1,
            entry=150.0, stop=148.5, cost_fraction=COST_MAJOR, equity=100.0, risk_pct=0.01,
            risk_budget=1.0, projected_loss=0.915, reserved_before=0.0, leverage=10.0)

    def _body(self, **over):
        body = {"symbol": "SOLUSDTM", "side": "buy", "size": 5, "reduceOnly": False,
                "triggerStopDownPrice": "148.5", "triggerStopUpPrice": "154.5"}
        body.update(over)
        return body

    def _dispatch(self, body, auth=True, endpoint="/api/v1/st-orders"):
        token = rb.authorize(self.auth if auth else None)
        try:
            return rb.assert_transport_dispatch(self.client, endpoint, body)
        finally:
            rb.reset_authorization(token)

    def _refused(self, reason, *args, **kwargs):
        with self.assertRaises(rb.RiskBudgetRefused) as ctx:
            self._dispatch(*args, **kwargs)
        self.assertEqual(ctx.exception.reason, reason)

    def test_pass_and_reduce_only_untouched(self):
        self.assertTrue(self._dispatch(self._body()))
        self.assertIsNone(self._dispatch(self._body(reduceOnly=True), auth=False))
        self.assertIsNone(self._dispatch({"closeOrder": True, "symbol": "SOLUSDTM"}, auth=False,
                                         endpoint="/api/v1/orders"))

    def test_every_refusal_reason(self):
        self._refused("NO_RISK_AUTHORIZATION", self._body(), auth=False)
        self._refused("RISK_AUTHORIZATION_MISMATCH", self._body(symbol="XBTUSDTM"))
        self._refused("RISK_AUTHORIZATION_MISMATCH", self._body(side="sell"))
        self._refused("CONTRACTS_ABOVE_AUTHORIZATION", self._body(size=6))
        self._refused("NATIVE_STOP_MISSING", self._body(triggerStopDownPrice=None))
        self._refused("NATIVE_STOP_MISSING", self._body(), endpoint="/api/v1/orders")
        self._refused("PROJECTED_LOSS_EXCEEDS_RISK_BUDGET", self._body(triggerStopDownPrice="146"))
        self.client._instruments["SOLUSDT"]["multiplier"] = 1.0
        self._refused("MULTIPLIER_MISMATCH", self._body())

    def test_worse_fresh_entry_blocks_without_resizing(self):
        token = rb.authorize(self.auth)
        try:
            rb.update_entry_price(150.2)    # 5 x 0.1 x (1.7 + 0.33) = 1.015 > 1
            with self.assertRaises(rb.RiskBudgetRefused):
                rb.assert_transport_dispatch(self.client, "/api/v1/st-orders", self._body())
        finally:
            rb.reset_authorization(token)


class PropertyTests(unittest.TestCase):
    def test_accepted_never_exceeds_budget_and_leverage_never_raises_loss_above_budget(self):
        rng = random.Random(3003)
        checked = 0
        for _ in range(5000):
            equity = rng.choice([5, 10, 25, 50, 100, 500, 1000, 7777.7])
            available = equity * rng.choice([0.2, 0.5, 1.0])
            entry = rng.choice([0.15, 0.6, 30.0, 150.0, 3000.0, 60000.0]) * rng.uniform(0.8, 1.2)
            stop_pct = rng.choice([0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.05])
            stop = entry * (1 - stop_pct)
            mult = rng.choice([0.001, 0.01, 0.1, 1.0, 10.0, 100.0])
            cost = rng.choice([COST_MAJOR, COST_ALT])
            risk_pct = rng.choice([0.0025, 0.005, 0.01, 0.02, 0.05])
            budget = equity * risk_pct
            unlimited = _size(equity, 1e12, entry, stop, mult, risk_pct, 10, cost, 1.0)
            previous = None
            for lev in (5, 10, 20, 50):
                out = _size(equity, available, entry, stop, mult, risk_pct, lev, cost)
                loss = _loss(out.qty, entry, stop, cost)
                self.assertLessEqual(loss, budget * (1 + 1e-9))               # INV-RISK-SIZING-001
                self.assertLessEqual(out.qty, unlimited.qty + 1e-12)          # INV-RISK-LEVERAGE-001
                self.assertLessEqual(out.required_margin, available * 0.10 * (1 + 1e-9))
                if out.qty > 0:
                    checked += 1
                    contracts = round(out.qty / mult)
                    self.assertTrue(math.isclose(contracts * mult, out.qty, rel_tol=1e-9))
                if previous is not None and previous.binding_constraint == "RISK_BUDGET":
                    self.assertAlmostEqual(out.qty, previous.qty)            # stable once risk-bound
                previous = out
        self.assertGreater(checked, 3000)


if __name__ == "__main__":
    unittest.main()
