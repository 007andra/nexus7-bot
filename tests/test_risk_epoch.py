"""Offline contract tests for the operational risk epoch (no database)."""
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import risk_epoch
from tests.risk_epoch_fixtures import EPOCH_ENV, TEST_EPOCH_ID, active_state


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class RiskEpochConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = risk_epoch.config_from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "disabled")

    def test_limit_defaults_to_30pct_and_accepts_percent_form(self):
        with patch.dict(os.environ, {**EPOCH_ENV, risk_epoch.MAX_DRAWDOWN_ENV: ""}, clear=True):
            self.assertEqual(risk_epoch.config_from_env().limit, 0.30)
        with patch.dict(os.environ, {**EPOCH_ENV, risk_epoch.MAX_DRAWDOWN_ENV: "30"}, clear=True):
            self.assertAlmostEqual(risk_epoch.config_from_env().limit, 0.30)
        with patch.dict(os.environ, {**EPOCH_ENV, risk_epoch.MAX_DRAWDOWN_ENV: "0.2"}, clear=True):
            self.assertAlmostEqual(risk_epoch.config_from_env().limit, 0.20)

    def test_limit_above_hard_cap_or_malformed_is_invalid(self):
        for raw in ("0.3000001", "0.5", "31", "0", "-0.1", "nan", "abc"):
            with patch.dict(os.environ, {**EPOCH_ENV, risk_epoch.MAX_DRAWDOWN_ENV: raw}, clear=True):
                config = risk_epoch.config_from_env()
            self.assertFalse(config.valid, raw)
            self.assertEqual(config.reason, "invalid_epoch_limit", raw)

    def test_epoch_id_must_be_explicit_and_well_formed(self):
        for raw in ("", "short", "lower_case_epoch", "BAD ID WITH SPACES"):
            with patch.dict(os.environ, {**EPOCH_ENV, risk_epoch.EPOCH_ID_ENV: raw}, clear=True):
                self.assertEqual(risk_epoch.config_from_env().reason, "invalid_epoch_id", raw)


class RiskEpochMathTests(unittest.TestCase):
    def test_epoch_drawdown_and_floor(self):
        self.assertEqual(risk_epoch.epoch_drawdown(5.3177, 5.3177), 0.0)
        self.assertAlmostEqual(risk_epoch.epoch_drawdown(6.0, 4.2), 0.30)
        self.assertEqual(risk_epoch.epoch_drawdown(6.0, 7.0), 0.0)
        self.assertAlmostEqual(risk_epoch.floor_equity(5.3177, 0.30), 3.72239)
        with self.assertRaises(ValueError):
            risk_epoch.epoch_drawdown(0.0, 1.0)
        with self.assertRaises(ValueError):
            risk_epoch.epoch_drawdown(1.0, float("nan"))

    def test_digest_covers_baseline_but_not_observations(self):
        record = {name: 1 for name in risk_epoch._IMMUTABLE_FIELDS}
        digest = risk_epoch.baseline_digest(record)
        self.assertEqual(digest, risk_epoch.baseline_digest(dict(record, epoch_peak_equity=99)))
        self.assertNotEqual(digest, risk_epoch.baseline_digest(dict(record, start_equity=2)))
        self.assertNotEqual(digest, risk_epoch.baseline_digest(dict(record, epoch_drawdown_limit=0.5)))

    def test_cash_flow_fingerprint_is_order_independent_and_sensitive(self):
        r1 = {"reconciliation_id": "a", "identities": ["1"], "net_amount": 5.0}
        r2 = {"reconciliation_id": "b", "identities": ["2"], "net_amount": 2.7808}
        a = {"applied": [r1, r2]}
        b = {"applied": [r2, r1]}
        c = {"applied": [r1, r2, {"reconciliation_id": "c", "identities": ["3"], "net_amount": 1}]}
        d = {"applied": [r1, dict(r2, net_amount=12.7808)]}
        e = {"applied": [r1, r2], "pending": [{"identity": "9", "amount": 1.0}]}
        fp = risk_epoch.cash_flow_fingerprint
        self.assertEqual(fp(a), fp(b))
        for other in (c, d, e):
            self.assertNotEqual(fp(a), fp(other))


class RiskEpochGateTests(unittest.TestCase):
    def test_disabled_epoch_never_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(risk_epoch.blocks_new_entries(None), (False, "epoch_disabled"))

    def test_enabled_epoch_blocks_unless_active_for_the_configured_id(self):
        with patch.dict(os.environ, EPOCH_ENV, clear=True):
            self.assertTrue(risk_epoch.blocks_new_entries(None)[0])
            self.assertFalse(risk_epoch.blocks_new_entries(active_state())[0])
            for status in ("PENDING_BASELINE", "BREACHED", "INVALID", "FLOW_CHANGED",
                           "UNKNOWN", "CONFIG_INVALID"):
                state = dict(active_state(), status=status)
                self.assertTrue(risk_epoch.blocks_new_entries(state)[0], status)
            self.assertTrue(
                risk_epoch.blocks_new_entries(active_state(epoch_id="OTHER_EPOCH_ID"))[0]
            )

    def test_observe_without_database_fails_closed(self):
        engine = SimpleNamespace(paper_trade=False, positions={}, risk=None)
        with patch.dict(os.environ, EPOCH_ENV, clear=True), \
             patch("bot.database._conn", None):
            import asyncio
            state = asyncio.run(risk_epoch.observe(engine))
            blocked, _ = risk_epoch.blocks_new_entries(state)
        self.assertEqual(state["status"], risk_epoch.UNKNOWN)
        self.assertTrue(blocked)

    def test_disabled_observe_does_no_io(self):
        engine = SimpleNamespace()
        with patch.dict(os.environ, {}, clear=True), \
             patch("bot.database._load_key_value_raw") as raw:
            import asyncio
            state = asyncio.run(risk_epoch.observe(engine))
        raw.assert_not_called()
        self.assertEqual(state["status"], risk_epoch.DISABLED)


class RiskEpochTelemetryTests(unittest.TestCase):
    def test_log_line_names_historical_and_epoch_separately(self):
        logger = logging.getLogger("test.risk_epoch.telemetry")
        handler = _ListHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        engine = SimpleNamespace(_risk_epoch_state=active_state(equity=5.0))
        try:
            risk_epoch.emit(engine, logger, force=True)
        finally:
            logger.removeHandler(handler)
        line = handler.lines[-1]
        self.assertTrue(line.startswith("[RISK_EPOCH_V1] "))
        self.assertIn("historical_drawdown=0.76333669401", line)
        self.assertIn("historical_drawdown_limit=", line)
        self.assertIn("epoch_drawdown=", line)
        self.assertIn("epoch_drawdown_limit=0.3", line)
        self.assertIn(f"epoch_id={TEST_EPOCH_ID}", line)
        self.assertIn("authority=TIGHTEN_ONLY", line)
        self.assertIn("historical_hwm_written=false", line)
        self.assertIn("live_authorization=NONE", line)
        # The epoch figure is never published under the historical name.
        self.assertNotIn("historical_drawdown=0.073", line)

    def test_status_api_exposes_both_metrics(self):
        from bot import status_observability

        engine = SimpleNamespace(
            risk=SimpleNamespace(drawdown=0.7667548284),
            _risk_epoch_state=active_state(start_equity=5.3177, equity=5.0),
        )
        with patch.dict(os.environ, EPOCH_ENV, clear=True), \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.30):
            out = status_observability.risk_drawdown_observability(engine)
        self.assertAlmostEqual(out["historical_drawdown"], 0.7667548284)
        self.assertEqual(out["historical_drawdown_limit"], 0.30)
        self.assertAlmostEqual(out["epoch_drawdown"], 1 - 5.0 / 5.3177)
        self.assertEqual(out["epoch_drawdown_limit"], 0.30)
        self.assertFalse(out["epoch_blocks_new_entries"])

    def test_execution_observability_reports_epoch_gate(self):
        from bot import status_observability

        engine = SimpleNamespace(
            paper_trade=False, pilot=None, connected=True, active=True,
            risk=SimpleNamespace(drawdown=0.0),
            _risk_epoch_state=dict(active_state(), status="BREACHED"),
        )
        with patch.dict(os.environ, EPOCH_ENV, clear=True), \
             patch("bot.config.cfg.MAX_DRAWDOWN", 0.30):
            out = status_observability.execution_observability(engine)
        self.assertFalse(out["new_entries_allowed"])
        self.assertIn("RISK_EPOCH_GATE", out["execution_blockers"])

    def test_startup_log(self):
        with patch.dict(os.environ, EPOCH_ENV, clear=True):
            line = risk_epoch.startup_log()
        self.assertIn("enabled=true", line)
        self.assertIn("epoch_drawdown_limit=0.3", line)
        self.assertIn("historical_gate_unchanged=true", line)
        self.assertIn("live_authorization=NONE", line)


if __name__ == "__main__":
    unittest.main()
