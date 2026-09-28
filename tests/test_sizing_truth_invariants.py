"""End-to-end sizing invariants for the executed LIVE-pilot quantity hook.

Exercises the real chain used in production: ``final_sizing_invariants`` (the
last ``minimum_base_quantity`` hook) -> ``ProfessionalRiskAdapter.size`` ->
``RiskManagerV3.size_for_stop`` -> ``final_loss_budget.diagnose``.

Contract (2026-09-26 audit P0-1):
    final_qty = min(stop_risk_qty, operator_margin_cap_qty)
"""
import logging
import math
import unittest
from types import SimpleNamespace

from bot.config import cfg
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter

# Binance USD-M style metadata normalized to the base-asset quantity contract:
# stepSize 0.01 base, min 1 lot, minNotional 5 USDT.
INFO = {"multiplier": "0.01", "lotSize": "1", "minQty": "1", "minNotional": "5"}
SYMBOL = "TESTUSDT"
# Binance USD-M max initial leverage currently offered on majors is 125x.
LEVERAGES = (1, 10, 20, 50, 75, 100, 125)


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


class _EngineModule:
    pass


class SizingTruthInvariantTests(unittest.TestCase):
    def setUp(self):
        self.old = (cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT)
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_MARGIN_PCT = self.old
        logging.disable(logging.NOTSET)

    def _adapter(self, *, equity, available, entry, stop, risk_pct, confirm=True):
        legacy = SimpleNamespace(balance=available, balance_confirmed=True, _ready=True)
        adapter = ProfessionalRiskAdapter(legacy)
        adapter.set_plan(symbol=SYMBOL, entry=entry, stop=stop, risk_pct=risk_pct)
        if confirm:
            adapter.update_capital(CapitalState(equity=equity, available_collateral=available))
        return adapter

    def _run(self, *, leverage, equity=10.0, available=10.0, entry=100.0,
             stop_pct=0.004, risk_pct=0.01, info=INFO, confirm=True, direction="LONG"):
        cfg.LEVERAGE = leverage
        stop = entry * (1 - stop_pct) if direction == "LONG" else entry * (1 + stop_pct)
        adapter = self._adapter(
            equity=equity, available=available, entry=entry, stop=stop,
            risk_pct=risk_pct, confirm=confirm,
        )
        module = _EngineModule()
        module.minimum_base_quantity = lambda _info, _price: 999.0
        module._final_sizing_invariants_installed = False
        log = _Log()
        final_sizing.install(module, pilot_cap, log)
        engine = SimpleNamespace(
            paper_trade=False, pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=available, risk=adapter,
            instruments={SYMBOL: info}, positions={},
        )
        signal = SimpleNamespace(sl=stop, direction=direction, _bgx_setup_id="T:1")
        tokens = (
            pilot_cap._PILOT_ENGINE.set(engine),
            pilot_cap._PILOT_SYMBOL.set(SYMBOL),
            pilot_cap._PILOT_FINAL_QTY.set(None),
            pilot_cap._PILOT_SIGNAL.set(signal),
        )
        try:
            final_qty = module.minimum_base_quantity(info, entry)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
        finally:
            pilot_cap._PILOT_SIGNAL.reset(tokens[3])
            pilot_cap._PILOT_FINAL_QTY.reset(tokens[2])
            pilot_cap._PILOT_SYMBOL.reset(tokens[1])
            pilot_cap._PILOT_ENGINE.reset(tokens[0])

        stop_risk_qty = adapter.size(SYMBOL, entry, {SYMBOL: info}) if confirm else 0.0
        try:
            cap_qty = final_sizing._operator_target_quantity(info, entry, available, float(leverage))
        except (KeyError, ValueError, ArithmeticError):
            cap_qty = 0.0
        return SimpleNamespace(
            final_qty=final_qty, stored=stored, stop_risk_qty=stop_risk_qty,
            cap_qty=cap_qty, adapter=adapter, entry=entry, stop=stop, log=log,
        )

    def _effective_loss_per_unit(self, adapter, entry, stop):
        plan = adapter._plans[SYMBOL]
        return abs(entry - stop) + entry * plan.fee_rate() * 2 + entry * plan.slippage()

    def test_invariants_across_leverage_matrix(self):
        for leverage in LEVERAGES:
            for direction in ("LONG", "SHORT"):
                with self.subTest(leverage=leverage, direction=direction):
                    r = self._run(leverage=leverage, direction=direction)
                    self.assertEqual(r.final_qty, r.stored)
                    self.assertTrue(math.isfinite(r.final_qty))
                    self.assertGreaterEqual(r.final_qty, 0.0)
                    if r.final_qty == 0.0:
                        continue  # fail-closed block is always allowed
                    tol = 1e-9
                    self.assertLessEqual(r.final_qty, r.stop_risk_qty + tol)
                    self.assertLessEqual(r.final_qty, r.cap_qty + tol)
                    budget = 10.0 * 0.01
                    projected = r.final_qty * self._effective_loss_per_unit(r.adapter, r.entry, r.stop)
                    self.assertLessEqual(projected, budget * 1.000001)
                    required_margin = r.final_qty * r.entry / leverage
                    self.assertLessEqual(required_margin, 10.0 * 0.50 + tol)

    def test_matrix_is_not_vacuous(self):
        """Pin which leverages size vs block, so the invariants above are exercised."""
        sized = {lev: self._run(leverage=lev).final_qty for lev in LEVERAGES}
        # 1x: RiskManagerV3 collateral cap (MAX_MARGIN_PCT) cannot meet minNotional.
        self.assertEqual(sized[1], 0.0)
        for lev in (10, 20, 50):
            self.assertGreater(sized[lev], 0.0, lev)
        # The historical final_loss_budget ceiling is diagnostic-only.
        # Higher leverage therefore does not create a second geometry veto;
        # RiskManagerV3/operator margin remain the binding sizing authorities.
        for lev in (75, 100, 125):
            self.assertGreater(sized[lev], 0.0, lev)
        self.assertEqual(
            len({sized[lev] for lev in (20, 50, 75, 100, 125)}),
            1,
            sized,
        )

    def test_leverage_never_multiplies_the_loss_budget(self):
        """Once collateral is not binding, qty and projected loss are leverage-invariant."""
        qtys = {}
        for leverage in (20, 50, 75):
            r = self._run(leverage=leverage, stop_pct=0.003)
            qtys[leverage] = r.final_qty
            self.assertGreater(r.final_qty, 0.0, leverage)
        self.assertEqual(len(set(qtys.values())), 1, qtys)

    def test_higher_leverage_only_shrinks_or_blocks_never_grows_loss(self):
        losses = []
        for leverage in LEVERAGES:
            r = self._run(leverage=leverage)
            losses.append(r.final_qty * self._effective_loss_per_unit(r.adapter, r.entry, r.stop))
        self.assertLessEqual(max(losses), 10.0 * 0.01 * 1.000001)

    def test_risk_binds_below_operator_cap_at_50x(self):
        r = self._run(leverage=50)
        self.assertGreater(r.cap_qty, r.stop_risk_qty)
        self.assertEqual(r.final_qty, r.stop_risk_qty)
        sizing = [t for _, t in r.log.records if "[FINAL_SIZING_INVARIANT]" in t and "result=PASS" in t]
        self.assertTrue(sizing)
        self.assertIn("binding=RISK_BUDGET", sizing[0])
        self.assertIn("risk_authority=RiskManagerV3", sizing[0])

    def test_invalid_capital_yields_zero(self):
        r = self._run(leverage=50, confirm=False)
        self.assertEqual(r.final_qty, 0.0)
        self.assertEqual(r.stored, 0.0)

    def test_nonpositive_available_yields_zero(self):
        r = self._run(leverage=50, available=0.0)
        self.assertEqual(r.final_qty, 0.0)

    def test_invalid_metadata_yields_zero(self):
        for info in ({}, {"multiplier": "0", "lotSize": "1", "minQty": "1"},
                     {"multiplier": "0.01", "lotSize": "0.5", "minQty": "1"}):
            with self.subTest(info=info):
                r = self._run(leverage=50, info=info)
                self.assertEqual(r.final_qty, 0.0)

    def test_exchange_minimum_incompatible_with_budget_yields_zero(self):
        # min notional 5 USDT; a 1% budget on 1 USDT equity over a 2% stop
        # admits ~0.3 USDT notional => cannot meet the exchange minimum.
        r = self._run(leverage=50, equity=1.0, available=1.0, stop_pct=0.02)
        self.assertEqual(r.stop_risk_qty, 0.0)
        self.assertEqual(r.final_qty, 0.0)


class SelectFinalQuantityTests(unittest.TestCase):
    def test_min_and_fail_closed(self):
        sel = final_sizing._select_final_quantity
        self.assertEqual(sel(target_qty=5.0, risk_qty=0.25), 0.25)
        self.assertEqual(sel(target_qty=0.25, risk_qty=5.0), 0.25)
        for bad in (0.0, -1.0, float("nan"), float("inf"), None, "x"):
            self.assertEqual(sel(target_qty=5.0, risk_qty=bad), 0.0)
            self.assertEqual(sel(target_qty=bad, risk_qty=5.0), 0.0)

    def test_binding_names(self):
        self.assertEqual(final_sizing.binding_constraint(target_qty=5, risk_qty=1), "RISK_BUDGET")
        self.assertEqual(final_sizing.binding_constraint(target_qty=1, risk_qty=5), "OPERATOR_MARGIN_CAP")


class NoTruthAlteringLogNormalizationTests(unittest.TestCase):
    def test_sizing_log_rewriter_removed(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("bot.sizing_semantics_log_hardening"))
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn("sizing_semantics", (root / "sitecustomize.py").read_text())

    def test_no_stale_authority_labels_in_runtime_sources(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "bot"
        stale = ("OPERATOR_50PCT_EQUITY", "risk_manager_role=VALIDATION_GATE",
                 "NON_AUTHORITATIVE", "final_authority=PILOT_MARGIN_SIZING",
                 "RiskManagerV3_plus_operator_50pct_margin_cap")
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for label in stale:
                self.assertNotIn(label, text, f"{path.name}: {label}")


if __name__ == "__main__":
    unittest.main()
