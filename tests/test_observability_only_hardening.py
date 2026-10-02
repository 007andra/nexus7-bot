import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import risk_budget
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
    """F-003: the loss-budget invariant exposes its full arithmetic (equity basis)."""

    def test_block_exposes_normalized_arithmetic_and_reason(self):
        with patch("bot.risk_budget.log") as log:
            with self.assertRaises(risk_budget.RiskBudgetRefused) as ctx:
                risk_budget.assert_projected_loss_within_budget(
                    symbol="ADAUSDT", contracts=50, multiplier=1, entry=100, stop=99.12,
                    direction="LONG", cost_fraction=.0032, equity=100, risk_pct=.01,
                    stage="FINAL_SIZING", leverage=50)
        self.assertEqual(ctx.exception.reason, "PROJECTED_LOSS_EXCEEDS_RISK_BUDGET")
        fmt, *args = log.critical.call_args.args
        msg = fmt % tuple(args)
        for field in ("[PREDISPATCH_RISK_INVARIANT]", "result=BLOCK", "risk_budget=1.000000",
                      "projected_loss=60.000000", "contracts=50", "stop_pct=0.00880",
                      "cost_fraction=0.00320", "leverage=50"):
            self.assertIn(field, msg)

    def test_pass_metrics_are_exact(self):
        metrics = risk_budget.assert_projected_loss_within_budget(
            symbol="SOLUSDT", contracts=5, multiplier=.1, entry=150, stop=148.5,
            direction="LONG", cost_fraction=.0022, equity=100, risk_pct=.01, leverage=10)
        self.assertAlmostEqual(metrics["projected_loss"], 0.915)
        self.assertAlmostEqual(metrics["projected_loss_pct"], 0.00915)
        self.assertAlmostEqual(metrics["margin"], 7.5)

    def test_telemetry_failure_fails_closed(self):
        # A barrier whose evidence cannot be emitted refuses (never silently passes).
        with patch("bot.risk_budget.log") as log:
            log.info.side_effect = RuntimeError("logging down")
            with self.assertRaises(RuntimeError):
                risk_budget.assert_projected_loss_within_budget(
                    symbol="SOLUSDT", contracts=5, multiplier=.1, entry=150, stop=148.5,
                    direction="LONG", cost_fraction=.0022, equity=100, risk_pct=.01)


class FinalSizingTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.old = (cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_OPEN_RISK_PCT, cfg.MAX_MARGIN_PCT)
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_OPEN_RISK_PCT, cfg.MAX_MARGIN_PCT = 10, .01, .02, .10

    def tearDown(self):
        cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_OPEN_RISK_PCT, cfg.MAX_MARGIN_PCT = self.old

    def _exercise(self, stop, equity=100.0):
        from bot.professional_risk import CapitalState
        from bot.professional_risk_adapter import ProfessionalRiskAdapter
        from bot.risk import RiskManager

        class EngineModule:
            pass

        info = {"multiplier": "0.1", "lotSize": "1", "minQty": "1", "minNotional": "0"}
        EngineModule.minimum_base_quantity = lambda info, price: .001
        EngineModule._final_sizing_invariants_installed = False
        risk = ProfessionalRiskAdapter(RiskManager())
        risk.update_capital(CapitalState(equity, equity))
        risk.set_plan(symbol="SOLUSDT", entry=150.0, stop=stop, risk_pct=.01)
        engine = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                                 _pilot_available_balance=equity, risk=risk,
                                 instruments={"SOLUSDT": info}, positions={})
        signal = SimpleNamespace(sl=stop, direction="LONG", _bgx_setup_id="SOLUSDT:LONG:MOMENTUM:1")
        log = _Log()
        final_sizing.install(EngineModule, pilot_cap, log)
        tokens = (pilot_cap._PILOT_ENGINE.set(engine), pilot_cap._PILOT_SYMBOL.set("SOLUSDT"),
                  pilot_cap._PILOT_FINAL_QTY.set(None), pilot_cap._PILOT_SIGNAL.set(signal))
        auth = risk_budget.authorize(None)
        try:
            qty = EngineModule.minimum_base_quantity(info, 150.0)
            stored = pilot_cap._PILOT_FINAL_QTY.get()
        finally:
            risk_budget.reset_authorization(auth)
            pilot_cap._PILOT_SIGNAL.reset(tokens[3])
            pilot_cap._PILOT_FINAL_QTY.reset(tokens[2])
            pilot_cap._PILOT_SYMBOL.reset(tokens[1])
            pilot_cap._PILOT_ENGINE.reset(tokens[0])
        return qty, stored, log

    def test_final_sizing_block_logs_reason(self):
        qty, stored, log = self._exercise(148.5, equity=10.0)
        self.assertEqual((qty, stored), (0.0, 0.0))
        msg = [m for _, m in log.rendered() if "[RISK_BUDGET_REJECTED]" in m][0]
        self.assertIn("reason=MIN_CONTRACT_EXCEEDS_RISK_BUDGET", msg)

    def test_final_sizing_pass_logs_full_risk_arithmetic(self):
        qty, stored, log = self._exercise(148.5)
        self.assertAlmostEqual(qty, 0.5)
        self.assertAlmostEqual(stored, 0.5)
        msg = [m for _, m in log.rendered() if "[RISK_SIZING]" in m][0]
        for field in ("result=PASS", "equity=100.000000", "risk_pct=0.0100", "risk_budget=1.000000",
                      "final_contracts=5", "projected_loss=0.915000", "aggregate_reserved_pct=0.01000",
                      "authority=RISK_BUDGET_V3", "leverage=10x"):
            self.assertIn(field, msg)


if __name__ == "__main__":
    unittest.main()
