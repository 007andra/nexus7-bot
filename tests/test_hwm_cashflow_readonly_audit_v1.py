"""#601 redacted financial provenance review; synthetic local fixtures only."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from bot import hwm_cashflow_readonly_audit_v1 as audit

PEAK_KEY = "risk:account_equity_peak:v3:test"
PROV_KEY = "risk:account_equity_peak:provenance:v3:test"


def fixture():
    record = {
        "reconciliation_id": "PRIVATE_INTERNAL_ID",
        "identities": ["PRIVATE_ID_1", "PRIVATE_ID_2"],
        "tran_ids": ["EXCHANGE_TX_PRIVATE_A", "EXCHANGE_TX_PRIVATE_B"],
        "net_amount": 2,
        "gross_in": 2, "gross_out": 0, "direction": "DEPOSIT",
        "pre_flow_equity": 10, "post_flow_equity": 12,
        "previous_hwm": 9, "adjusted_hwm": 12,
        "equity_at_reconciliation": 12, "trading_drawdown_after": 0.0,
    }
    return {
        audit.LEDGER_KEY: json.dumps({"version": 1, "pending": [],
                                     "applied": [record]}),
        audit.CURSOR_KEY: json.dumps({"version": 1, "seen": ["PRIVATE_ID_1",
                                                            "PRIVATE_ID_2"]}),
        audit.WALLET_KEY: json.dumps({"version": 1, "wallet": "12",
                                     "anchor_ms": 1_000_000}),
        PEAK_KEY: "12",
        PROV_KEY: json.dumps({
            "version": 1, "reason": "external_capital_flow_rebase",
            "new_peak": 12, "old_peak": 9,
            "evidence_ref": "SECRET_LEDGER_IDENTITY",
        }),
    }


class HwmReviewTests(unittest.TestCase):
    def review(self, d):
        return audit.review_values(d, peak_key=PEAK_KEY, provenance_key=PROV_KEY)

    def test_structural_checks_pass_but_authority_never_passes(self):
        row = self.review(fixture())
        self.assertEqual(row["status"],
                         "STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING")
        self.assertEqual(row["applied_reconciliation_records"], 1)
        self.assertEqual(row["applied_transfer_events"], 2)
        self.assertEqual(row["applied_external_net_usdt"], 2)
        self.assertEqual(row["pending_transfer_events"], 0)
        self.assertFalse(row["exchange_income_verified"])
        self.assertFalse(row["full_hwm_history_verified"])
        self.assertFalse(row["live_allowed"])
        self.assertFalse(row["promotion_allowed"])
        self.assertEqual(row["execution_effect"], "NONE")

    def test_never_output_exchange_or_ledger_ids(self):
        report = json.dumps(self.review(fixture()))
        for secret in ("PRIVATE_ID_1", "PRIVATE_ID_2", "EXCHANGE_TX_PRIVATE",
                       "SECRET_LEDGER_IDENTITY", "risk:account_equity_peak:v3:test"):
            self.assertNotIn(secret, report)

    def test_duplicate_applied_id_fails_closed(self):
        data = fixture()
        obj = json.loads(data[audit.LEDGER_KEY])
        obj["applied"].append(obj["applied"][0])
        data[audit.LEDGER_KEY] = json.dumps(obj)
        result = self.review(data)
        self.assertIn("DUPLICATE_APPLIED_FLOW", result["blockers"])
        self.assertIn("RECONCILIATION_ID_INVALID_OR_DUPLICATE", result["blockers"])

    def test_wrong_twr_ratio_is_not_accepted(self):
        data = fixture()
        obj = json.loads(data[audit.LEDGER_KEY])
        obj["applied"][0]["adjusted_hwm"] = 99
        data[audit.LEDGER_KEY] = json.dumps(obj)
        self.assertIn("TWR_REBASE_MISMATCH", self.review(data)["blockers"])

    def test_recorded_gross_in_out_must_balance_signed_net(self):
        data = fixture()
        doc = json.loads(data[audit.LEDGER_KEY])
        doc["applied"][0]["gross_in"] = 100
        data[audit.LEDGER_KEY] = json.dumps(doc)
        row = self.review(data)
        self.assertIn("GROSS_FLOW_TOTALS_MISMATCH", row["blockers"])
        self.assertFalse(row["live_allowed"])
        self.assertNotIn("EXCHANGE_TX_PRIVATE_A", json.dumps(row))

    def test_direction_and_transaction_id_integrity(self):
        data = fixture()
        doc = json.loads(data[audit.LEDGER_KEY])
        doc["applied"][0]["direction"] = "WITHDRAWAL"
        doc["applied"][0]["tran_ids"] = ["EXCHANGE_TX_PRIVATE_A"] * 2
        data[audit.LEDGER_KEY] = json.dumps(doc)
        blockers = self.review(data)["blockers"]
        self.assertIn("FLOW_DIRECTION_MISMATCH", blockers)
        self.assertIn("FLOW_TRANSACTION_IDS_INVALID", blockers)

    def test_recorded_drawdown_cannot_disagree_with_twr_hwm(self):
        data = fixture()
        doc = json.loads(data[audit.LEDGER_KEY])
        doc["applied"][0]["trading_drawdown_after"] = 0.75
        data[audit.LEDGER_KEY] = json.dumps(doc)
        self.assertIn("FLOW_DRAWDOWN_RECORD_MISMATCH",
                      self.review(data)["blockers"])

    def test_pending_duplicate_and_invalid_amount_fail_closed(self):
        data = fixture()
        doc = json.loads(data[audit.LEDGER_KEY])
        doc["pending"] = [
            {"identity": "PRIVATE_PENDING", "amount": 2},
            {"identity": "PRIVATE_PENDING", "amount": 0},
        ]
        data[audit.LEDGER_KEY] = json.dumps(doc)
        blockers = self.review(data)["blockers"]
        self.assertIn("PENDING_DUPLICATE_FLOW", blockers)
        self.assertIn("PENDING_AMOUNT_INVALID", blockers)

    def test_positive_existing_drawdown_reconciles_correctly(self):
        data = fixture()
        doc = json.loads(data[audit.LEDGER_KEY])
        doc["applied"][0].update({
            "previous_hwm": 20,
            "adjusted_hwm": 24,
            "trading_drawdown_after": 0.5,
        })
        data[audit.LEDGER_KEY] = json.dumps(doc)
        data[PEAK_KEY] = "24"
        prov = json.loads(data[PROV_KEY])
        prov["new_peak"] = 24
        data[PROV_KEY] = json.dumps(prov)
        row = self.review(data)
        self.assertEqual(
            row["status"],
            "STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING",
        )
        self.assertFalse(row["exchange_income_verified"])

    def test_pending_overlaps_applied_blocks(self):
        data = fixture()
        obj = json.loads(data[audit.LEDGER_KEY])
        obj["pending"] = [{"identity": "PRIVATE_ID_1", "amount": 1}]
        data[audit.LEDGER_KEY] = json.dumps(obj)
        self.assertIn("PENDING_ALREADY_APPLIED", self.review(data)["blockers"])

    def test_mismatched_hwm_provenance_not_silently_rebased(self):
        data = fixture()
        data[PEAK_KEY] = "22.7986938551"
        self.assertIn("HWM_PROVENANCE_PEAK_MISMATCH",
                      self.review(data)["blockers"])

    def test_missing_raw_chain_blocks(self):
        data = fixture()
        del data[PROV_KEY]
        self.assertIn("PROVENANCE_MISSING_OR_INVALID", self.review(data)["blockers"])
        self.assertFalse(self.review(data)["live_allowed"])

    def test_invalid_wallet_anchor_and_cursor_blocks(self):
        data = fixture()
        data[audit.WALLET_KEY] = json.dumps({"version": 1, "wallet": 12,
                                              "anchor_ms": "invalid"})
        data[audit.CURSOR_KEY] = json.dumps({"version": 1, "seen": "oops"})
        result = self.review(data)
        self.assertIn("WALLET_BASELINE_UNVERIFIABLE", result["blockers"])
        self.assertIn("CURSOR_IDENTITIES_MALFORMED", result["blockers"])

    def test_nonfinite_hwm_not_reported_as_balance(self):
        data = fixture()
        data[PEAK_KEY] = "nan"
        result = self.review(data)
        self.assertIn("HWM_MISSING_OR_INVALID", result["blockers"])
        self.assertIsNone(result["durable_hwm_usdt"])

    def test_connection_settings_mandate_ro_and_bounded(self):
        self.assertEqual(audit.SETTINGS["default_transaction_read_only"], "on")
        self.assertEqual(audit.SETTINGS["statement_timeout"], "700")
        self.assertEqual(audit.SELECT_SQL.strip().split()[0].upper(), "SELECT")
        self.assertNotIn("UPDATE", audit.SELECT_SQL.upper())
        self.assertNotIn("CREATE", audit.SELECT_SQL.upper())

    def test_reader_uses_one_read_only_select_and_redacts(self):
        class Conn:
            def __init__(self):
                self.calls = []
            async def fetch(self, sql, keys):
                self.calls.append((sql, keys))
                data = fixture()
                return [{"key": k, "value": v} for k,v in data.items()]
        conn = Conn()
        report = asyncio.run(audit.collect_once(conn, peak_key=PEAK_KEY,
                                                 provenance_key=PROV_KEY))
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(conn.calls[0][0], audit.SELECT_SQL)
        self.assertFalse(report["exchange_income_verified"])

    def test_generic_cli_failure_never_logs_dsn(self):
        from io import StringIO
        from contextlib import redirect_stdout
        async def fail():
            raise RuntimeError("postgresql://USER:SECRET@host")
        with patch.object(audit, "_main", fail):
            out = StringIO()
            with redirect_stdout(out):
                code = audit.main()
        self.assertEqual(code, 2)
        self.assertNotIn("SECRET", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["status"],
                         "UNAVAILABLE_OR_FAILED_CLOSED")

    def test_no_database_initializer_dependency(self):
        # Standalone module does not use the bot.database.init DDL path.
        import inspect
        source = inspect.getsource(audit)
        self.assertNotIn("db.init(", source)
        self.assertNotIn("conn.execute(", source)



LEGACY_PEAK_KEY = "private:legacy:peak:DO_NOT_DISCLOSE"
LEGACY_PROV_KEY = "private:legacy:provenance:DO_NOT_DISCLOSE"


class LegacyNamespaceReviewTests(unittest.TestCase):
    class Conn:
        def __init__(self, values):
            self.values = values
            self.calls = []

        async def fetch(self, sql, keys):
            self.calls.append((sql, list(keys)))
            return [
                {"key": k, "value": self.values[k]}
                for k in keys if k in self.values
            ]

    def collect(self, data, *, use_legacy=True):
        conn = self.Conn(data)
        result = asyncio.run(audit.collect_once(
            conn, peak_key=PEAK_KEY, provenance_key=PROV_KEY,
            legacy_peak_key=LEGACY_PEAK_KEY if use_legacy else None,
            legacy_provenance_key=LEGACY_PROV_KEY if use_legacy else None,
        ))
        return result, conn

    @staticmethod
    def legacy_only():
        data = fixture()
        data[LEGACY_PEAK_KEY] = data.pop(PEAK_KEY)
        data[LEGACY_PROV_KEY] = data.pop(PROV_KEY)
        return data

    def test_legacy_only_matches_authenticated_production_probe_shape(self):
        data = self.legacy_only()
        result, conn = self.collect(data)
        self.assertEqual(
            result["status"],
            "STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING",
        )
        self.assertEqual(result["hwm_source"], "LEGACY")
        self.assertEqual(result["provenance_source"], "LEGACY")
        self.assertEqual(result["durable_hwm_usdt"], 12)
        self.assertFalse(result["exchange_income_verified"])
        self.assertFalse(result["full_hwm_history_verified"])
        self.assertFalse(result["live_allowed"])
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(conn.calls[0][0], audit.SELECT_SQL)
        self.assertEqual(len(conn.calls[0][1]), 7)
        for secret in (LEGACY_PEAK_KEY, LEGACY_PROV_KEY, "PRIVATE_ID_1",
                       "SECRET_LEDGER_IDENTITY", "EXCHANGE_TX_PRIVATE_A"):
            self.assertNotIn(secret, json.dumps(result))

    def test_current_pair_precedes_stale_legacy_pair(self):
        data = fixture()
        data[LEGACY_PEAK_KEY] = "999999999"
        data[LEGACY_PROV_KEY] = json.dumps({"version": 1, "new_peak": 999999999})
        result, _ = self.collect(data)
        self.assertEqual(result["hwm_source"], "CURRENT")
        self.assertEqual(result["provenance_source"], "CURRENT")
        self.assertEqual(result["durable_hwm_usdt"], 12)
        self.assertEqual(
            result["status"],
            "STRUCTURAL_CHECKS_PASSED_INDEPENDENT_EXCHANGE_AUDIT_PENDING",
        )

    def test_invalid_current_value_must_not_be_overwritten_by_legacy(self):
        data = fixture()
        data[PEAK_KEY] = "corrupt-HWM"
        data[LEGACY_PEAK_KEY] = "12"
        data[LEGACY_PROV_KEY] = data[PROV_KEY]
        result, _ = self.collect(data)
        self.assertEqual(result["hwm_source"], "CURRENT")
        self.assertIn("HWM_MISSING_OR_INVALID", result["blockers"])
        self.assertEqual(result["status"], "EVIDENCE_INCONSISTENT_OR_INCOMPLETE")
        self.assertIsNone(result["durable_hwm_usdt"])

    def test_legacy_provenance_corruption_fails_closed(self):
        data = self.legacy_only()
        prov = json.loads(data[LEGACY_PROV_KEY])
        prov["new_peak"] = 23
        data[LEGACY_PROV_KEY] = json.dumps(prov)
        result, _ = self.collect(data)
        self.assertIn("HWM_PROVENANCE_PEAK_MISMATCH", result["blockers"])
        self.assertEqual(result["status"], "EVIDENCE_INCONSISTENT_OR_INCOMPLETE")

    def test_legacy_provenance_missing_stays_incomplete(self):
        data = self.legacy_only()
        del data[LEGACY_PROV_KEY]
        result, _ = self.collect(data)
        self.assertEqual(result["hwm_source"], "LEGACY")
        self.assertEqual(result["provenance_source"], "MISSING")
        self.assertIn("PROVENANCE_MISSING_OR_INVALID", result["blockers"])
        self.assertFalse(result["promotion_allowed"])

    def test_mixed_namespace_pair_requires_manual_review(self):
        data = self.legacy_only()
        data[PEAK_KEY] = data[LEGACY_PEAK_KEY]
        result, _ = self.collect(data)
        self.assertEqual(result["hwm_source"], "CURRENT")
        self.assertEqual(result["provenance_source"], "LEGACY")
        self.assertIn("NAMESPACE_SOURCES_MIXED_MANUAL_REVIEW", result["blockers"])
        self.assertEqual(result["status"], "EVIDENCE_INCONSISTENT_OR_INCOMPLETE")
        self.assertFalse(result["live_allowed"])

    def test_no_fallback_only_when_explicitly_disabled(self):
        data = self.legacy_only()
        result, conn = self.collect(data, use_legacy=False)
        self.assertEqual(result["hwm_source"], "MISSING")
        self.assertEqual(result["provenance_source"], "MISSING")
        self.assertIn("HWM_MISSING_OR_INVALID", result["blockers"])
        self.assertEqual(len(conn.calls[0][1]), 5)

    def test_untrusted_legacy_key_fails_before_database_query(self):
        conn = self.Conn(fixture())
        with self.assertRaisesRegex(ValueError, "INVALID_READONLY_LEDGER_KEYS"):
            asyncio.run(audit.collect_once(
                conn, peak_key=PEAK_KEY, provenance_key=PROV_KEY,
                legacy_peak_key="",
            ))
        self.assertEqual(conn.calls, [])



if __name__ == "__main__":
    unittest.main()
