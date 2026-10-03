import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperatorRuntimeContractTests(unittest.TestCase):
    def test_live_operator_contract_keeps_leverage_and_risk_budget(self):
        """Configured leverage is respected; sizing is the F-003 risk budget (no margin target)."""
        env = dict(os.environ)
        env["LEVERAGE"] = "50"
        code = (
            "from bot.config import cfg; "
            "import bot.operator_runtime_policy as p; "
            "assert cfg.LEVERAGE == 50, cfg.LEVERAGE; "
            "assert cfg.MAX_RISK_PCT == 0.01 and cfg.MAX_OPEN_RISK_PCT == 0.02; "
            "assert not hasattr(p, 'MARGIN_FRACTION'); "
            "print('operator contract ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("operator contract ok", proc.stdout)

    def test_drawdown_is_hard_gate_by_default_with_explicit_override(self):
        source = (ROOT / "bot" / "operator_runtime_policy.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("drawdown_default=hard_gate", source)
        self.assertIn('RISK_OVERRIDE_ENV = "LIVE_RISK_OVERRIDE_APPROVED"', source)
        self.assertIn("override=false entries_blocked=true", source)
        self.assertIn("override=true entries_blocked=false", source)
        self.assertIn("active_restored=true override=true", source)


if __name__ == "__main__":
    unittest.main()
