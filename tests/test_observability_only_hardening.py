import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import final_loss_budget as loss_budget
from bot import final_sizing_invariants as final_sizing
from bot import pilot_risk_cap_hardening as pilot_cap
from bot import pullback_confirmation_hardening as pullback
from bot.config import cfg


class _Log:
    def __init__(self):
        self.rows = []

    def _add(self, level, *args):
        self.rows.append((level, args))

    def info(self, *args, **kwargs):
        self._add("INFO", *args)

    def warning(self, *args, **kwargs):
        self._add("WARNING", *args)

    def critical(self, *args, **kwargs):
        self._add("CRITICAL", *args)

    def rendered(self):
        out = []
        for level, args in self.rows:
            fmt = args[0]
            try:
                msg = fmt % tuple(args[1:]) if len(args) > 1 else str(fmt)
            except Exception:
                msg = str(args)
            out.append((level, msg))
        return out


def _strategy_signal(entry_type="PULLBACK"):
    sig = SimpleNamespace(
        symbol="ADAUSDT",
        direction="LONG",
        entry_type=entry_type,
        entry=100.0,
        sl=99.12,
        tp=102.0,
        score=69,
        regime="TRENDING_UP",
        _bgx_atr_15m=0.40,
        _bgx_atr_1h=0.60,
        _bgx_adjusted_atr=0.44,
        _bgx_formation_timestamp=1800000000.0,
        _bgx_formation_bucket=2000000,
        _bgx_4h_bias="LONG",
        _bgx_1h_bias="LONG",
        _bgx_15m_bias="LONG",
    )
    return sig


class StrategyStopGeometryObservabilityTests(unittest.TestCase):
    def setUp(self):
        pullback._STRATEGY_STOP_GEOMETRY_LAST.clear()

    def test_pullback_block_still_emits_complete_geometry_and_linked_setup_id(self):
        sig = _strategy_signal("PULLBACK")

        class Analyzer:
            def analyze_mtf(self, *args, **kwargs):
                return sig

        metrics = {
            "ok": False,
            "reason": "opposite_structure_not_reversed",
            "votes": {"rsi_recovered": True},
            "vote_count": 1,
            "structure": "DOWNTREND",
            "bos": False,
            "bos_dir": "NONE",
            "rsi": 45.0,
            "macd_hist": -0.1,
            "macd_prev": -0.05,
            "ema20_side": False,
        }
        log = _Log()
        with patch("bot.pullback_confirmation_hardening.time.time", return_value=1800000000.0), \
             patch.object(pullback, "_pullback_metrics", return_value=metrics):
            pullback.install(Analyzer, log)
            result = Analyzer().analyze_mtf("ADAUSDT", [{}] * 40, [], [])

        self.assertIsNone(result)
        rendered = [m for _, m in log.rendered()]
        geometry = [m for m in rendered if "[STRATEGY_STOP_GEOMETRY]" in m]
        blocked = [m for m in rendered if "[PULLBACK_CONFIRMATION]" in m and "result=BLOCKED" in m]
        self.assertEqual(len(geometry), 1)
        self.assertEqual(len(blocked), 1)
        setup_id = sig._bgx_setup_id
        self.assertIn(f"setup_id={setup_id}", geometry[0])
        self.assertIn(f"setup_id={setup_id}", blocked[0])
        self.assertIn("atr_15m=0.4", geometry[0])
        self.assertIn("atr_1h=0.6", geometry[0])
        self.assertIn("adjusted_atr=0.44", geometry[0])
        self.assertIn("original_stop_pct=0.88000000", geometry[0])
        self.assertIn("target_pct=2.00000000", geometry[0])

    def test_same_setup_is_deduplicated_without_random_identity(self):
        sig = _strategy_signal("MOMENTUM")

        class Analyzer:
            def analyze_mtf(self, *args, **kwargs):
                return sig

        log = _Log()
        with patch("bot.pullback_confirmation_hardening.time.time", return_value=1800000000.0):
            pullback.install(Analyzer, log)
            a = Analyzer()
            self.assertIs(a.analyze_mtf("ADAUSDT", [], [], []), sig)
            self.assertIs(a.analyze_mtf("ADAUSDT", [], [], []), sig)

        geometry = [m for _, m in log.rendered() if "[STRATEGY_STOP_GEOMETRY]" in m]
        self.assertEqual(len(geometry), 1)
        self.assertEqual(sig._bgx_setup_id, "ADAUSDT:LONG:MOMENTUM:2000000")

    def test_geometry_event_covers_all_entry_types(self):
        for idx, entry_type in enumerate(("BOS_BREAK", "MOMENTUM", "PULLBACK")):
            pullback._STRATEGY_STOP_GEOMETRY_LAST.clear()
            sig = _strategy_signal(entry_type)

            class Analyzer:
                def analyze_mtf(self, *args, **kwargs):
                    return sig

            log = _Log()
            with patch(
                "bot.pullback_confirmation_hardening.time.time",
                return_value=1800000000.0 + idx * 900,
            ), patch.object(
                pullback, "_pullback_metrics",
                return_value={
                    "ok": True, "votes": {}, "vote_count": 5,
                    "structure": "UPTREND", "bos": True,
                    "bos_dir": "BULLISH", "rsi": 60,
                },
            ):
                pullback.install(Analyzer, log)
                result = Analyzer().analyze_mtf("ADAUSDT", [{}] * 40, [], [])
            self.assertIs(result, sig)
            geometry = [m for _, m in log.rendered() if "[STRATEGY_STOP_GEOMETRY]" in m]
            self.assertEqual(len(geometry), 1)
            self.assertIn(f"entry_type={entry_type}", geometry[0])


class FinalLossObservabilityTests(unittest.TestCase):
    @staticmethod
    def _legacy_validate(qty, entry, stop, direction, leverage, cost_fraction):
        values = (qty, entry, stop, leverage, cost_fraction)
        if any(isinstance(v, bool) or not math.isfinite(float(v)) for v in values):
            raise ValueError("nonfinite loss budget")
        qty, entry, stop, leverage, cost_fraction = map(float, values)
        if min(qty, entry, stop, leverage) <= 0 or cost_fraction < 0:
            raise ValueError("invalid loss budget")
        if not ((direction == "LONG" and stop < entry) or (direction == "SHORT" and stop > entry)):
            raise ValueError("invalid stop direction")
        margin = qty * entry / leverage
        projected = qty * (abs(entry - stop) + entry * cost_fraction)
        limit = margin * 0.50
        if projected > limit + max(1e-12, limit * 1e-12):
            raise ValueError("projected loss exceeds 50pct entry margin")
        return projected, limit

    def test_validate_is_behaviorally_equivalent_to_pre_patch_formula(self):
        cases = [
            (5, 100, 99.5027, "LONG", 50, .0022),
            (5, 100, 99.12, "LONG", 50, .0032),
            (.001, 100, 98, "LONG", 50, .0022),
            (7, 100, 100.78, "SHORT", 50, .0022),
            (5, 100, 101, "LONG", 50, .0022),
        ]
        for args in cases:
            try:
                expected = self._legacy_validate(*args)
                expected_error = None
            except ValueError as exc:
                expected = None
                expected_error = str(exc)
            try:
                actual = loss_budget.validate(*args)
                actual_error = None
            except ValueError as exc:
                actual = None
                actual_error = str(exc)
            self.assertEqual(actual_error, expected_error)
            if expected is not None:
                self.assertAlmostEqual(actual[0], expected[0])
                self.assertAlmostEqual(actual[1], expected[1])

    def test_ada_like_block_exposes_normalized_arithmetic(self):
        metrics = loss_budget.measure(5, 100, 99.12, "LONG", 50, .0032)
        self.assertAlmostEqual(metrics["stop_fraction"] * 100, .88, places=8)
        self.assertAlmostEqual(metrics["projected_loss_pct_notional"], 1.20, places=8)
        self.assertAlmostEqual(metrics["allowed_loss_pct_notional"], 1.00, places=8)
        self.assertAlmostEqual(metrics["headroom_pct"], -.20, places=8)
        with self.assertRaisesRegex(ValueError, "projected loss exceeds 50pct entry margin"):
            loss_budget.validate(5, 100, 99.12, "LONG", 50, .0032)

        log = _Log()
        loss_budget.emit_telemetry(
            log, symbol="ADAUSDT", setup_id="ADAUSDT:LONG:MOMENTUM:1",
            stage="FINAL_SIZING_INVARIANT", qty=5, entry=100, stop=99.12,
            direction="LONG", leverage=50, cost_fraction=.0032,
            result="BLOCK",
            specific_reason="projected_loss_exceeds_50pct_entry_margin",
            risk_v3_advisory_qty=.25,
        )
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("result=BLOCK", msg)
        self.assertIn("specific_reason=projected_loss_exceeds_50pct_entry_margin", msg)
        self.assertIn("qty=5", msg)
        self.assertIn("stop_risk_qty=0.25", msg)
        self.assertIn("allowed_loss_pct_notional=1.00000000", msg)
        self.assertIn("projected_loss_pct_notional=1.20000000", msg)
        self.assertIn("headroom_pct=-0.20000000", msg)

    def test_major_like_pass_exposes_positive_headroom(self):
        metrics = loss_budget.measure(5, 100, 99.5027, "LONG", 50, .0022)
        self.assertAlmostEqual(metrics["projected_loss_pct_notional"], .7173, places=8)
        self.assertAlmostEqual(metrics["allowed_loss_pct_notional"], 1.0, places=8)
        self.assertGreater(metrics["headroom_pct"], 0)
        before = loss_budget.validate(5, 100, 99.5027, "LONG", 50, .0022)

        log = _Log()
        loss_budget.emit_telemetry(
            log, symbol="BTCUSDT", setup_id="BTCUSDT:LONG:MOMENTUM:1",
            stage="FRESH_PREDISPATCH_RECHECK", qty=5, entry=100, stop=99.5027,
            direction="LONG", leverage=50, cost_fraction=.0022,
            result="PASS", specific_reason="within_50pct_entry_margin",
        )
        after = loss_budget.validate(5, 100, 99.5027, "LONG", 50, .0022)
        self.assertEqual(before, after)
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("stage=FRESH_PREDISPATCH_RECHECK", msg)
        self.assertIn("result=PASS", msg)
        self.assertIn("headroom_pct=0.28270000", msg)

    def test_telemetry_failure_does_not_change_validation(self):
        class BrokenLog:
            def info(self, *args, **kwargs):
                raise RuntimeError("logging down")
            def warning(self, *args, **kwargs):
                raise RuntimeError("logging down")

        expected = loss_budget.validate(5, 100, 99.5027, "LONG", 50, .0022)
        loss_budget.emit_telemetry(
            BrokenLog(), symbol="BTCUSDT", setup_id="x",
            stage="FINAL_SIZING_INVARIANT", qty=5, entry=100, stop=99.5027,
            direction="LONG", leverage=50, cost_fraction=.0022,
            result="PASS", specific_reason="within_50pct_entry_margin",
        )
        self.assertEqual(
            loss_budget.validate(5, 100, 99.5027, "LONG", 50, .0022),
            expected,
        )


class FinalSizingTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.old_leverage = cfg.LEVERAGE
        cfg.LEVERAGE = 50

    def tearDown(self):
        cfg.LEVERAGE = self.old_leverage

    def _exercise(self, stop):
        class EngineModule:
            pass

        info = {"multiplier": "0.001", "lotSize": "1", "minQty": "1", "minNotional": "0"}
        EngineModule.minimum_base_quantity = lambda info, price: .001
        EngineModule._final_sizing_invariants_installed = False
        risk = SimpleNamespace(size=lambda *args, **kwargs: .25)
        engine = SimpleNamespace(
            paper_trade=False,
            pilot=SimpleNamespace(enabled=True),
            _pilot_available_balance=20.0,
            risk=risk,
            instruments={},
            positions={},
        )
        signal = SimpleNamespace(
            sl=stop, direction="LONG",
            _bgx_setup_id="TESTUSDT:LONG:MOMENTUM:1",
        )
        log = _Log()
        final_sizing.install(EngineModule, pilot_cap, log)
        token_engine = pilot_cap._PILOT_ENGINE.set(engine)
        token_symbol = pilot_cap._PILOT_SYMBOL.set("TESTUSDT")
        token_qty = pilot_cap._PILOT_FINAL_QTY.set(None)
        token_signal = pilot_cap._PILOT_SIGNAL.set(signal)
        try:
            qty = EngineModule.minimum_base_quantity(info, 100.0)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
        finally:
            pilot_cap._PILOT_SIGNAL.reset(token_signal)
            pilot_cap._PILOT_FINAL_QTY.reset(token_qty)
            pilot_cap._PILOT_SYMBOL.reset(token_symbol)
            pilot_cap._PILOT_ENGINE.reset(token_engine)
        return qty, stored, log

    # The final quantity is min(stop_risk_qty, operator_margin_cap_qty), but
    # that positive quantity is still subject to the independent final
    # loss-budget gate. WARN rejects only this candidate; runtime remains active.
    def test_final_sizing_warn_blocks_candidate_and_preserves_risk_telemetry(self):
        qty, stored, log = self._exercise(99.12)
        self.assertEqual(qty, 0.0)
        self.assertEqual(stored, 0.0)
        rendered = log.rendered()
        msg = [m for _, m in rendered if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("stage=FINAL_SIZING_INVARIANT", msg)
        self.assertIn("result=WARN", msg)
        self.assertIn("projected_loss_exceeds_50pct_entry_margin", msg)
        self.assertIn("execution_effect=OBSERVABILITY_ONLY", msg)
        self.assertIn("qty=0.25", msg)
        self.assertIn("qty_authority=FINAL_SIZING_INVARIANT", msg)
        self.assertIn("stop_risk_qty=0.25", msg)
        self.assertIn("risk_v3_qty_authority=BINDING_UPPER_BOUND", msg)
        self.assertNotIn("NON_AUTHORITATIVE", msg)
        block = [m for _, m in rendered if "[FINAL_SIZING_INVARIANT]" in m and "result=BLOCK" in m][0]
        self.assertIn("candidate_only=true", block)
        self.assertIn("runtime_paused=false", block)

    def test_final_sizing_pass_uses_stop_risk_quantity_below_cap(self):
        qty, stored, log = self._exercise(99.6)
        self.assertEqual(qty, 0.25)
        self.assertEqual(stored, 0.25)
        msg = [m for _, m in log.rendered() if "[FINAL_LOSS_BUDGET]" in m][0]
        self.assertIn("stage=FINAL_SIZING_INVARIANT", msg)
        self.assertIn("result=PASS", msg)
        self.assertIn("qty=0.25", msg)
        sizing = [m for _, m in log.rendered() if "[FINAL_SIZING_INVARIANT]" in m and "result=PASS" in m][0]
        self.assertIn("binding=RISK_BUDGET", sizing)
        self.assertIn("risk_authority=RiskManagerV3", sizing)
        self.assertIn("final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)", sizing)


if __name__ == "__main__":
    unittest.main()
