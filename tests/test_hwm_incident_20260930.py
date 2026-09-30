import json
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import cash_flow_ledger as ledger
from bot import database as db


def incident_record(*, repaired=False):
    previous = (
        ledger._INCIDENT_20260930_LAST_GOOD_HWM
        if repaired
        else ledger._INCIDENT_20260930_BAD_PREVIOUS_HWM
    )
    adjusted = (
        ledger._INCIDENT_20260930_REPAIRED_HWM
        if repaired
        else ledger._INCIDENT_20260930_BAD_ADJUSTED_HWM
    )
    return {
        "reconciliation_id": ledger._INCIDENT_20260930_RECONCILIATION_ID,
        "method": "LEDGER_RECONSTRUCTED",
        "identities": [
            "1790718360000:416195536884",
            "1790781257000:416435307318",
        ],
        "tran_ids": list(ledger._INCIDENT_20260930_TRAN_IDS),
        "income_types": ["TRANSFER"],
        "net_amount": 11.7808,
        "gross_in": 19.18862133,
        "gross_out": 7.40782133,
        "direction": "DEPOSIT",
        "flow_time_first_ms": 1790718360000,
        "flow_time_last_ms": 1790781257000,
        "pre_flow_equity": ledger._INCIDENT_20260930_PRE_EQUITY,
        "post_flow_equity": ledger._INCIDENT_20260930_POST_EQUITY,
        "previous_hwm": previous,
        "adjusted_hwm": adjusted,
        "equity_at_reconciliation": ledger._INCIDENT_20260930_POST_EQUITY,
        "trading_drawdown_after": ledger.trading_drawdown(
            ledger._INCIDENT_20260930_POST_EQUITY, adjusted
        ),
        "reason": "automatic ledger reconstruction",
        "evidence_ref": "runtime",
        "recorded_at_ms": 1790782370000,
    }


def incident_doc(*, repaired=False):
    return {
        "version": 1,
        "pending": [],
        "applied": [
            {
                "reconciliation_id": "older-record",
                "method": "LEDGER_RECONSTRUCTED",
                "identities": ["older"],
                "tran_ids": ["older"],
                "net_amount": -4.0,
            },
            incident_record(repaired=repaired),
        ],
    }


class HwmIncident20260930Tests(unittest.IsolatedAsyncioTestCase):
    def test_repaired_hwm_preserves_last_legitimate_drawdown(self):
        before = ledger.trading_drawdown(
            ledger._INCIDENT_20260930_PRE_EQUITY,
            ledger._INCIDENT_20260930_LAST_GOOD_HWM,
        )
        after = ledger.trading_drawdown(
            ledger._INCIDENT_20260930_POST_EQUITY,
            ledger._INCIDENT_20260930_REPAIRED_HWM,
        )
        self.assertAlmostEqual(before, 0.15834558541157764, places=12)
        self.assertAlmostEqual(
            ledger._INCIDENT_20260930_REPAIRED_HWM,
            22.798693855106116,
            places=12,
        )
        self.assertAlmostEqual(before, after, places=12)

    async def test_exact_incident_repairs_hwm_ledger_and_provenance_atomically(self):
        doc = incident_doc()
        raw = ledger._dump(doc)
        load_values = {
            ledger._INCIDENT_20260930_REPAIR_KEY: None,
            "peak-key": format(ledger._INCIDENT_20260930_BAD_ADJUSTED_HWM, ".17g"),
            "prov-key": "old-provenance",
        }

        async def load(key, strict=False):
            return load_values.get(key)

        risk = SimpleNamespace()
        with patch.object(
            ledger.db, "load_key_value", AsyncMock(side_effect=load)
        ), patch.object(
            ledger, "save_key_values_atomic_cas", AsyncMock(return_value=True)
        ) as save, patch(
            "bot.drawdown_persistence.DURABLE_EQUITY_PEAK_KEY", "peak-key"
        ), patch(
            "bot.hwm_namespace.provenance_key", return_value="prov-key"
        ), patch(
            "bot.drawdown_persistence.install_reconciled_peak"
        ) as install:
            out = await ledger.repair_known_20260930_hwm_incident(
                risk,
                ledger._INCIDENT_20260930_POST_EQUITY,
                ledger=doc,
                ledger_raw=raw,
                strict=True,
            )

        self.assertEqual(out["status"], "APPLIED")
        self.assertAlmostEqual(
            out["repaired_hwm"], ledger._INCIDENT_20260930_REPAIRED_HWM, places=12
        )
        repaired = ledger._incident_20260930_record(out["ledger"])
        self.assertAlmostEqual(
            repaired["previous_hwm"],
            ledger._INCIDENT_20260930_LAST_GOOD_HWM,
            places=12,
        )
        self.assertAlmostEqual(
            repaired["adjusted_hwm"],
            ledger._INCIDENT_20260930_REPAIRED_HWM,
            places=12,
        )
        self.assertEqual(
            repaired["incident_repair"]["id"], "2026-09-30-cashflow-order"
        )
        # Unrelated ledger history must remain intact.
        self.assertEqual(out["ledger"]["applied"][0]["reconciliation_id"], "older-record")

        save.assert_awaited_once()
        items = dict(save.await_args.args[0])
        expected = save.await_args.kwargs["expected"]
        self.assertEqual(
            float(items["peak-key"]), ledger._INCIDENT_20260930_REPAIRED_HWM
        )
        self.assertIn(ledger._INCIDENT_20260930_REPAIR_KEY, items)
        self.assertEqual(expected[ledger.LEDGER_KEY], raw)
        self.assertEqual(
            float(expected["peak-key"]), ledger._INCIDENT_20260930_BAD_ADJUSTED_HWM
        )
        self.assertEqual(expected["prov-key"], "old-provenance")
        self.assertIsNone(expected[ledger._INCIDENT_20260930_REPAIR_KEY])
        install.assert_called_once_with(
            risk,
            ledger._INCIDENT_20260930_REPAIRED_HWM,
            ledger._INCIDENT_20260930_POST_EQUITY,
        )

    async def test_repair_is_idempotent_when_marker_and_ledger_are_already_repaired(self):
        doc = incident_doc(repaired=True)
        raw = ledger._dump(doc)
        marker = json.dumps({
            "incident": "2026-09-30-cashflow-order",
            "reconciliation_id": ledger._INCIDENT_20260930_RECONCILIATION_ID,
        })
        with patch.object(
            ledger.db,
            "load_key_value",
            AsyncMock(return_value=marker),
        ), patch.object(
            ledger, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            out = await ledger.repair_known_20260930_hwm_incident(
                SimpleNamespace(),
                ledger._INCIDENT_20260930_POST_EQUITY,
                ledger=doc,
                ledger_raw=raw,
                strict=True,
            )
        self.assertEqual(out["status"], "ALREADY_REPAIRED")
        save.assert_not_awaited()

    async def test_signature_mismatch_fails_closed_without_write(self):
        doc = incident_doc()
        doc["applied"][-1]["tran_ids"] = ["unexpected"]
        raw = ledger._dump(doc)
        with patch.object(
            ledger.db, "load_key_value", AsyncMock(return_value=None)
        ), patch.object(
            ledger, "save_key_values_atomic_cas", AsyncMock()
        ) as save:
            with self.assertRaises(db.PersistenceError):
                await ledger.repair_known_20260930_hwm_incident(
                    SimpleNamespace(),
                    ledger._INCIDENT_20260930_POST_EQUITY,
                    ledger=doc,
                    ledger_raw=raw,
                    strict=True,
                )
        save.assert_not_awaited()

    async def test_matching_ledger_but_different_durable_peak_fails_closed(self):
        doc = incident_doc()
        raw = ledger._dump(doc)
        values = iter([None, "40.0"])

        async def load(_key, strict=False):
            return next(values)

        with patch.object(
            ledger.db, "load_key_value", AsyncMock(side_effect=load)
        ), patch.object(
            ledger, "save_key_values_atomic_cas", AsyncMock()
        ) as save, patch(
            "bot.drawdown_persistence.DURABLE_EQUITY_PEAK_KEY", "peak-key"
        ):
            with self.assertRaises(db.PersistenceError):
                await ledger.repair_known_20260930_hwm_incident(
                    SimpleNamespace(),
                    ledger._INCIDENT_20260930_POST_EQUITY,
                    ledger=doc,
                    ledger_raw=raw,
                    strict=True,
                )
        save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
