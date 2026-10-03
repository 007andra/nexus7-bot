import builtins
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import runtime_contract_guard as guard


ROOT = Path(__file__).resolve().parents[1]


def _owned(filename: str):
    ns = {}
    exec(compile("def fn(*args, **kwargs):\n    return None\n", filename, "exec"), ns)
    return ns["fn"]


class RuntimeContractGuardTests(unittest.TestCase):
    def test_verify_accepts_expected_source_files(self):
        items = (
            guard.ContractItem("a", _owned("mod_a.py"), "mod_a.py"),
            guard.ContractItem("b", _owned("mod_b.py"), "mod_b.py"),
        )
        ok, errors = guard.verify(items)
        self.assertIs(ok, True)
        self.assertEqual(errors, ())

    def test_verify_rejects_late_wrapper_drift(self):
        items = (
            guard.ContractItem("critical", _owned("unexpected.py"), "reviewed.py"),
        )
        ok, errors = guard.verify(items)
        self.assertIs(ok, False)
        self.assertEqual(len(errors), 1)
        self.assertIn("critical", errors[0])
        self.assertIn("unexpected.py", errors[0])
        self.assertIn("reviewed.py", errors[0])

    def test_verify_markers_requires_exact_true(self):
        ok, errors = guard.verify_markers(
            (
                guard.MarkerItem("good", True),
                guard.MarkerItem("missing", False),
                guard.MarkerItem("truthy_not_bool", 1),
            )
        )
        self.assertIs(ok, False)
        self.assertEqual(len(errors), 2)
        self.assertIn("missing", errors[0])
        self.assertIn("truthy_not_bool", errors[1])

    def test_install_fails_closed_when_final_runtime_source_drifted(self):
        class DummyLog:
            def critical(self, *args, **kwargs):
                return None

        class TradingEngine:
            run = _owned("wrong.py")
            _update_balance = _owned("operator_runtime_policy.py")
            _manage_partial_tp = _owned("partial_tp_execution_hardening.py")
            _partial_tp_execution_hardening_installed = True

        class PilotGuard:
            evaluate = _owned("pilot_exposure_capacity.py")

        nexus_ai = SimpleNamespace(
            regime_compatibility=_owned("nexus_regime_transition_consistency.py")
        )
        engine_module = SimpleNamespace(
            minimum_base_quantity=_owned("final_sizing_invariants.py"),
            _final_sizing_invariants_installed=True,
        )

        import bot.risk as risk
        import bot.risk_manager_v3 as risk_v3

        with patch.object(
            risk.RiskManager,
            "can_open",
            _owned("operator_runtime_policy.py"),
        ), patch.object(
            risk_v3.RiskManagerV3,
            "can_open",
            _owned("operator_runtime_policy.py"),
        ), patch.object(
            builtins,
            "_nexus_runtime_contract_status",
            None,
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_CONTRACT_DRIFT"):
                guard.install(
                    TradingEngine,
                    PilotGuard,
                    nexus_ai,
                    engine_module,
                    DummyLog(),
                )
            self.assertEqual(builtins._nexus_runtime_contract_status, "failed")

    def test_runtime_contract_guard_is_last_installer_in_runtime_overlays(self):
        source = (ROOT / "bot" / "runtime_overlays.py").read_text(encoding="utf-8")
        guard_call = source.index("runtime_contract_guard.install(")
        tail = source[guard_call + len("runtime_contract_guard.install("):]
        self.assertNotIn(".install(", tail)

    def test_guard_contract_explicitly_preserves_operator_policy_and_final_sizing(self):
        source = (ROOT / "bot" / "runtime_contract_guard.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"operator_runtime_policy.py"', source)
        self.assertIn('"final_sizing_invariants.py"', source)
        self.assertIn("engine.minimum_base_quantity", source)
        self.assertIn("RiskManager.can_open", source)
        self.assertIn("RiskManagerV3.can_open", source)
        self.assertIn("_final_sizing_invariants_installed", source)
        self.assertIn("risk_authority=RiskManagerV3 final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty)", source)
        self.assertIn("entry_authorization_unchanged=true", source)

    def test_guard_covers_live_execution_chain(self):
        source = (ROOT / "bot" / "runtime_contract_guard.py").read_text(
            encoding="utf-8"
        )
        expected = (
            "ExchangeClient.place_order",
            "live_execution_fence.py",
            "ExchangeClient._post",
            "partial_tp_execution_hardening.py",
            "ExchangeClient.wait_for_fill",
            "order_visibility_race_hardening.py",
            "ExchangeClient.get_order_status",
            "ExchangeClient.set_position_stops",
            "durable_execution.reconcile_orders",
            "durable_reconcile_hardening.py",
            "_pilot_durable_submission_counter_installed",
            "_live_execution_fence_installed",
            "exchange_runtime.is_kucoin()",
            "_native_tpsl_entry_installed",
            "_fill_normalization_installed",
            "binance.paper_parity_client",
            "execution_chain=idempotency>distributed_fence>dispatch>fill>protection>reconcile",
        )
        for text in expected:
            self.assertIn(text, source)

    def test_bootstrap_preserves_durable_counter_inside_distributed_fence(self):
        source = (ROOT / "bot" / "runtime_bootstrap.py").read_text(encoding="utf-8")
        counter = source.index("_pilot_submission_counter.install(")
        fence = source.index("_live_execution_fence.install(")
        self.assertLess(counter, fence)


if __name__ == "__main__":
    unittest.main()
