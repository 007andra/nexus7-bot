import json
import math
import unittest
from unittest.mock import AsyncMock, patch

from bot import database as db
from bot import hwm_provenance
from bot import hwm_namespace
from bot.drawdown_persistence import (
    DURABLE_EQUITY_PEAK_KEY,
    rebase_real_account_peak_for_external_flow,
    restore_update_real_account_peak,
    restore_zero_equity_peak_fail_closed,
)
from bot.professional_risk import CapitalState
from bot.professional_risk_adapter import ProfessionalRiskAdapter
from bot.risk import RiskManager
from bot.risk_manager_v3 import RiskManagerV3


class DurableDrawdownPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.legacy = RiskManager()
        self.legacy.init(100.0)
        self.risk = ProfessionalRiskAdapter(self.legacy)
        self.risk.update_capital(CapitalState(100.0, 80.0))

    def _assert_atomic_write(self, save, expected_peak: float, expected_reason: str):
        save.assert_awaited_once()
        items = list(save.await_args.args[0])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][0], DURABLE_EQUITY_PEAK_KEY)
        self.assertTrue(math.isclose(float(items[0][1]), expected_peak, rel_tol=0.0, abs_tol=1e-9))
        self.assertEqual(items[1][0], hwm_namespace.provenance_key())
        payload = json.loads(items[1][1])
        self.assertEqual(payload["reason"], expected_reason)
        self.assertTrue(math.isclose(payload["new_peak"], expected_peak, rel_tol=0.0, abs_tol=1e-9))
        self.assertTrue(save.await_args.kwargs["strict"])

    async def test_missing_state_bootstraps_verified_equity_atomically(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value=None)) as load, \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(self.risk, 100.0, strict=True)
        self.assertEqual(peak, 100.0)
        self.assertEqual(self.legacy.drawdown, 0.0)
        load.assert_awaited_once_with(DURABLE_EQUITY_PEAK_KEY, strict=True)
        self._assert_atomic_write(save, 100.0, "bootstrap")

    async def test_restart_restores_higher_peak_without_write(self):
        fresh_legacy = RiskManager()
        fresh_legacy.init(80.0)
        fresh = ProfessionalRiskAdapter(fresh_legacy)
        fresh.update_capital(CapitalState(80.0, 60.0))
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(fresh, 80.0, strict=True)
        self.assertEqual(peak, 100.0)
        self.assertAlmostEqual(fresh_legacy.drawdown, 0.20)
        self.assertAlmostEqual(fresh.professional_snapshot.drawdown, 0.20)
        save.assert_not_awaited()

    async def test_new_high_persists_peak_and_provenance_in_one_atomic_call(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await restore_update_real_account_peak(self.risk, 125.0, strict=True)
        self.assertEqual(peak, 125.0)
        self.assertEqual(self.legacy.peak_balance, 125.0)
        self._assert_atomic_write(save, 125.0, "new_equity_high")

    async def test_atomic_write_failure_fails_closed_before_cache_or_risk_mutation(self):
        original_peak = self.legacy.peak_balance
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="100")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(side_effect=db.PersistenceError("rollback"))):
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(self.risk, 125.0, strict=True)
        self.assertEqual(self.legacy.peak_balance, original_peak)
        self.assertIsNone(getattr(self.risk, "_durable_account_equity_peak", None))

    async def test_verified_withdrawal_preserves_drawdown_and_writes_atomically(self):
        legacy = RiskManager()
        legacy.init(20.8664)
        risk = ProfessionalRiskAdapter(legacy)
        risk.update_capital(CapitalState(20.8664, 20.8664))
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="38.2593")), \
             patch("bot.drawdown_persistence.save_key_values_atomic", AsyncMock(return_value=True)) as save:
            peak = await rebase_real_account_peak_for_external_flow(
                risk, 20.8664, pre_flow_equity=34.8664, post_flow_equity=20.8664,
                flow_type="TransferOut", flow_amount=14.0, flow_offset="12345", strict=True,
            )
        expected_peak = 38.2593 * (20.8664 / 34.8664)
        pre_flow_dd = (38.2593 - 34.8664) / 38.2593
        self.assertAlmostEqual(peak, expected_peak, places=6)
        self.assertAlmostEqual(legacy.drawdown, pre_flow_dd, places=6)
        self._assert_atomic_write(save, expected_peak, "external_capital_flow_rebase")

    async def test_malformed_durable_state_fails_closed(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(return_value="nan")):
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(self.risk, 100.0, strict=True)

    async def test_persistence_read_failure_propagates(self):
        with patch("bot.drawdown_persistence.db.load_key_value", AsyncMock(side_effect=db.PersistenceError("offline"))):
            with self.assertRaises(db.PersistenceError):
                await restore_update_real_account_peak(self.risk, 100.0, strict=True)

    async def test_nonfinite_nonpositive_and_boolean_equity_rejected(self):
        for value in (0.0, -1.0, math.inf, math.nan, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await restore_update_real_account_peak(self.risk, value, strict=True)


    async def test_zero_equity_preserves_highest_known_peak_and_forces_100pct_drawdown(self):
        self.legacy.balance_confirmed = True
        # setUp initializes the in-memory peak at 100.0. A lower persisted
        # value must never lower that known HWM during a zero-equity event.
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value="8.8015"),
        ), patch(
            "bot.drawdown_persistence.save_key_values_atomic",
            AsyncMock(return_value=True),
        ) as save:
            peak = await restore_zero_equity_peak_fail_closed(
                self.risk, strict=True
            )

        self.assertEqual(peak, 100.0)
        self.assertEqual(self.legacy.balance, 0.0)
        self.assertEqual(self.legacy.peak_balance, 100.0)
        self.assertEqual(self.legacy.drawdown, 1.0)
        self.assertFalse(self.legacy.balance_confirmed)
        self.assertFalse(self.risk.professional_snapshot.confirmed)
        self.assertEqual(self.risk.professional_snapshot.peak_equity, 100.0)
        save.assert_not_awaited()

    async def test_zero_equity_without_durable_peak_fails_closed(self):
        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(return_value=None),
        ):
            with self.assertRaises(db.PersistenceError):
                await restore_zero_equity_peak_fail_closed(
                    self.risk, strict=True
                )


    async def test_20260930_zero_crossing_repair_is_evidence_bound_and_preserves_drawdown(self):
        import bot.cash_flow_ledger as cfl

        pre = 7.40782133
        post = 19.18862133
        last_good_peak = 8.8015
        bad_peak = post * post / pre
        repaired = post * last_good_peak / pre
        ledger = {
            "version": 1,
            "pending": [],
            "applied": [{
                "reconciliation_id": "auto-176be1f3ca8bbe91",
                "method": "LEDGER_RECONSTRUCTED",
                "tran_ids": ["416195536884", "416435307318"],
                "pre_flow_equity": pre,
                "post_flow_equity": post,
                "previous_hwm": post,
                "adjusted_hwm": bad_peak,
                "equity_at_reconciliation": post,
            }],
        }
        ledger_raw = json.dumps(ledger)
        provenance_raw = hwm_provenance.build_hwm_provenance(
            reason="external_capital_flow_rebase",
            old_peak=post,
            new_peak=bad_peak,
            account_equity=post,
            evidence_ref=(
                "cash_flow_ledger:LEDGER_RECONSTRUCTED:"
                "auto-176be1f3ca8bbe91"
            ),
        )
        peak_raw = format(bad_peak, ".17g")

        async def load(key, strict=True):
            if key == DURABLE_EQUITY_PEAK_KEY:
                return peak_raw
            if key == cfl.LEDGER_KEY:
                return ledger_raw
            if key == hwm_namespace.provenance_key():
                return provenance_raw
            if key == "risk:hwm_repair:2026-09-30-zero-crossing:v1":
                return None
            raise AssertionError(key)

        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(side_effect=load),
        ), patch(
            "bot.drawdown_persistence.save_key_values_atomic_cas",
            AsyncMock(return_value=True),
        ) as cas:
            peak = await restore_update_real_account_peak(
                self.risk, post, strict=True
            )

        self.assertAlmostEqual(peak, repaired, places=10)
        self.assertAlmostEqual(
            self.legacy.drawdown,
            (last_good_peak - pre) / last_good_peak,
            places=10,
        )
        self.assertAlmostEqual(self.legacy.peak_balance, repaired, places=10)
        cas.assert_awaited_once()
        expected = cas.await_args.kwargs["expected"]
        self.assertEqual(expected[DURABLE_EQUITY_PEAK_KEY], peak_raw)
        self.assertEqual(expected[cfl.LEDGER_KEY], ledger_raw)
        self.assertIsNone(
            expected["risk:hwm_repair:2026-09-30-zero-crossing:v1"]
        )

    async def test_20260930_zero_crossing_repair_refuses_evidence_mismatch(self):
        import bot.cash_flow_ledger as cfl

        pre = 7.40782133
        post = 19.18862133
        bad_peak = post * post / pre
        peak_raw = format(bad_peak, ".17g")
        wrong_ledger = json.dumps({
            "version": 1,
            "pending": [],
            "applied": [{
                "reconciliation_id": "auto-176be1f3ca8bbe91",
                "method": "LEDGER_RECONSTRUCTED",
                "tran_ids": ["WRONG", "416435307318"],
                "pre_flow_equity": pre,
                "post_flow_equity": post,
                "previous_hwm": post,
                "adjusted_hwm": bad_peak,
                "equity_at_reconciliation": post,
            }],
        })
        provenance_raw = hwm_provenance.build_hwm_provenance(
            reason="external_capital_flow_rebase",
            old_peak=post,
            new_peak=bad_peak,
            account_equity=post,
            evidence_ref=(
                "cash_flow_ledger:LEDGER_RECONSTRUCTED:"
                "auto-176be1f3ca8bbe91"
            ),
        )

        async def load(key, strict=True):
            if key == DURABLE_EQUITY_PEAK_KEY:
                return peak_raw
            if key == cfl.LEDGER_KEY:
                return wrong_ledger
            if key == hwm_namespace.provenance_key():
                return provenance_raw
            if key == "risk:hwm_repair:2026-09-30-zero-crossing:v1":
                return None
            raise AssertionError(key)

        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(side_effect=load),
        ), patch(
            "bot.drawdown_persistence.save_key_values_atomic_cas",
            AsyncMock(return_value=True),
        ) as cas, patch(
            "bot.drawdown_persistence.save_key_values_atomic",
            AsyncMock(return_value=True),
        ):
            peak = await restore_update_real_account_peak(
                self.risk, post, strict=True
            )

        self.assertAlmostEqual(peak, bad_peak, places=10)
        cas.assert_not_awaited()

    async def test_20260930_zero_crossing_repair_marker_makes_repair_one_shot(self):
        import bot.cash_flow_ledger as cfl

        pre = 7.40782133
        post = 19.18862133
        bad_peak = post * post / pre
        peak_raw = format(bad_peak, ".17g")
        ledger_raw = json.dumps({
            "version": 1,
            "pending": [],
            "applied": [{
                "reconciliation_id": "auto-176be1f3ca8bbe91",
                "method": "LEDGER_RECONSTRUCTED",
                "tran_ids": ["416195536884", "416435307318"],
                "pre_flow_equity": pre,
                "post_flow_equity": post,
                "previous_hwm": post,
                "adjusted_hwm": bad_peak,
                "equity_at_reconciliation": post,
            }],
        })
        provenance_raw = hwm_provenance.build_hwm_provenance(
            reason="external_capital_flow_rebase",
            old_peak=post,
            new_peak=bad_peak,
            account_equity=post,
            evidence_ref=(
                "cash_flow_ledger:LEDGER_RECONSTRUCTED:"
                "auto-176be1f3ca8bbe91"
            ),
        )

        async def load(key, strict=True):
            if key == DURABLE_EQUITY_PEAK_KEY:
                return peak_raw
            if key == cfl.LEDGER_KEY:
                return ledger_raw
            if key == hwm_namespace.provenance_key():
                return provenance_raw
            if key == "risk:hwm_repair:2026-09-30-zero-crossing:v1":
                return '{"already":"done"}'
            raise AssertionError(key)

        with patch(
            "bot.drawdown_persistence.db.load_key_value",
            AsyncMock(side_effect=load),
        ), patch(
            "bot.drawdown_persistence.save_key_values_atomic_cas",
            AsyncMock(return_value=True),
        ) as cas, patch(
            "bot.drawdown_persistence.save_key_values_atomic",
            AsyncMock(return_value=True),
        ):
            peak = await restore_update_real_account_peak(
                self.risk, post, strict=True
            )

        self.assertAlmostEqual(peak, bad_peak, places=10)
        cas.assert_not_awaited()


class RiskManagerV3PeakRestoreTests(unittest.TestCase):
    def test_restore_peak_does_not_confirm_capital(self):
        risk = RiskManagerV3()
        self.assertFalse(risk.confirmed)
        self.assertEqual(risk.restore_peak_equity(100.0), 100.0)
        self.assertFalse(risk.confirmed)
        risk.update_capital(CapitalState(80.0, 60.0))
        self.assertAlmostEqual(risk.drawdown, 0.20)

    def test_restore_peak_never_lowers_existing_high(self):
        risk = RiskManagerV3()
        risk.restore_peak_equity(100.0)
        self.assertEqual(risk.restore_peak_equity(90.0), 100.0)

    def test_cashflow_rebase_can_lower_peak_without_confirming_capital(self):
        risk = RiskManagerV3()
        risk.restore_peak_equity(100.0)
        self.assertFalse(risk.confirmed)
        self.assertEqual(risk.rebase_peak_equity(75.0), 75.0)
        self.assertFalse(risk.confirmed)

    def test_invalid_peak_rejected(self):
        risk = RiskManagerV3()
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    risk.restore_peak_equity(value)
                with self.assertRaises(ValueError):
                    risk.rebase_peak_equity(value)


if __name__ == "__main__":
    unittest.main()
