import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import binance_hwm_incident_repair as repair
from bot import cash_flow_ledger
from bot import database as db
from bot import drawdown_persistence as ddp
from bot import hwm_namespace


def _ledger_raw():
    return json.dumps(
        {
            "version": 1,
            "pending": [],
            "applied": [
                {
                    "reconciliation_id": repair.RECONCILIATION_ID,
                    "method": "LEDGER_RECONSTRUCTED",
                    "identities": list(repair.EXPECTED_IDENTITIES),
                    "tran_ids": list(repair.EXPECTED_TRAN_IDS),
                    "pre_flow_equity": repair.PRE_FLOW_EQUITY,
                    "post_flow_equity": repair.POST_FLOW_EQUITY,
                    "previous_hwm": repair.BAD_PREVIOUS_HWM,
                    "adjusted_hwm": repair.BAD_ADJUSTED_HWM,
                    "equity_at_reconciliation": repair.POST_FLOW_EQUITY,
                    "reason": "flat_account_no_performance_income_after_flow",
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _provenance_raw():
    return json.dumps(
        {
            "version": 1,
            "reason": "external_capital_flow_rebase",
            "old_peak": repair.BAD_PREVIOUS_HWM,
            "new_peak": repair.BAD_ADJUSTED_HWM,
            "account_equity": repair.POST_FLOW_EQUITY,
            "evidence_ref": (
                "cash_flow_ledger:LEDGER_RECONSTRUCTED:"
                + repair.RECONCILIATION_ID
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class HwmIncidentRepairTests(unittest.IsolatedAsyncioTestCase):
    def _risk(self):
        return SimpleNamespace(
            peak_balance=repair.BAD_ADJUSTED_HWM,
            drawdown=0.0,
        )

    async def test_exact_incident_repairs_to_twr_peak_and_keeps_15_83pct_drawdown(self):
        risk = self._risk()
        peak_raw = format(repair.BAD_ADJUSTED_HWM, ".17g")
        provenance_raw = _provenance_raw()

        async def load(key, strict=True):
            if key == ddp.DURABLE_EQUITY_PEAK_KEY:
                return peak_raw
            if key == repair.MARKER_KEY:
                return None
            if key == cash_flow_ledger.LEDGER_KEY:
                return _ledger_raw()
            if key == hwm_namespace.provenance_key():
                return provenance_raw
            raise AssertionError(key)

        with patch.object(
            repair.db, "load_key_value", AsyncMock(side_effect=load)
        ), patch.object(
            repair,
            "save_key_values_atomic_cas",
            AsyncMock(return_value=True),
        ) as save:
            result = await repair.repair_if_needed(
                risk, repair.POST_FLOW_EQUITY, strict=True
            )

        self.assertEqual(result["status"], "REPAIRED")
        self.assertAlmostEqual(result["new_peak"], 22.798693855106116, places=10)
        self.assertAlmostEqual(result["drawdown"], 0.15834558541157764, places=10)
        self.assertAlmostEqual(risk.peak_balance, result["new_peak"], places=10)
        save.assert_awaited_once()
        items = dict(save.await_args.args[0])
        self.assertAlmostEqual(
            float(items[ddp.DURABLE_EQUITY_PEAK_KEY]),
            22.798693855106116,
            places=10,
        )
        marker = json.loads(items[repair.MARKER_KEY])
        self.assertEqual(marker["incident_id"], repair.INCIDENT_ID)
        self.assertEqual(marker["reconciliation_id"], repair.RECONCILIATION_ID)

    async def test_unrelated_peak_is_noop(self):
        risk = self._risk()

        async def load(key, strict=True):
            if key == ddp.DURABLE_EQUITY_PEAK_KEY:
                return "22.0"
            if key == repair.MARKER_KEY:
                return None
            raise AssertionError(key)

        with patch.object(
            repair.db, "load_key_value", AsyncMock(side_effect=load)
        ), patch.object(
            repair, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            result = await repair.repair_if_needed(risk, 19.18862133)

        self.assertEqual(result["status"], "NOT_MATCHED")
        save.assert_not_awaited()

    async def test_bad_incident_signature_with_wrong_ledger_fails_closed(self):
        risk = self._risk()
        bad_ledger = json.loads(_ledger_raw())
        bad_ledger["applied"][0]["tran_ids"] = ["tampered"]

        async def load(key, strict=True):
            if key == ddp.DURABLE_EQUITY_PEAK_KEY:
                return format(repair.BAD_ADJUSTED_HWM, ".17g")
            if key == repair.MARKER_KEY:
                return None
            if key == cash_flow_ledger.LEDGER_KEY:
                return json.dumps(bad_ledger)
            if key == hwm_namespace.provenance_key():
                return _provenance_raw()
            raise AssertionError(key)

        with patch.object(
            repair.db, "load_key_value", AsyncMock(side_effect=load)
        ), patch.object(
            repair, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            with self.assertRaises(db.PersistenceError):
                await repair.repair_if_needed(risk, repair.POST_FLOW_EQUITY)

        save.assert_not_awaited()

    async def test_marker_makes_repair_idempotent(self):
        risk = SimpleNamespace(
            peak_balance=repair.REPAIRED_HWM,
            drawdown=0.0,
        )
        marker = json.dumps(
            {
                "incident_id": repair.INCIDENT_ID,
                "new_peak": repair.REPAIRED_HWM,
            }
        )

        async def load(key, strict=True):
            if key == ddp.DURABLE_EQUITY_PEAK_KEY:
                return format(repair.REPAIRED_HWM, ".17g")
            if key == repair.MARKER_KEY:
                return marker
            raise AssertionError(key)

        with patch.object(
            repair.db, "load_key_value", AsyncMock(side_effect=load)
        ), patch.object(
            repair, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            result = await repair.repair_if_needed(
                risk, repair.POST_FLOW_EQUITY
            )

        self.assertEqual(result["status"], "ALREADY_REPAIRED")
        self.assertAlmostEqual(risk.peak_balance, repair.REPAIRED_HWM)
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
