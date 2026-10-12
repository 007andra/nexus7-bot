"""Offline V3.1 prospective roster safety proof; no Binance or Railway calls."""
import json
import unittest
from unittest.mock import patch

from bot import short_down_bos_v31_readonly_enrollment as v31


def authority():
    return v31.EnrollmentAuthority(
        implementation_sha="a" * 40,
        implementation_commit_epoch=v31.PREREG_EPOCH + 60,
        authorized_capture_epoch=v31.PREREG_EPOCH + 120,
        authorization_reference="approved-research-operator-20261012",
    )


def candidate(i, symbol="BTCUSDT", *, captured=None):
    return {
        "population": "HARD_GATE_SHADOW", "side": "SHORT",
        "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "nexus_called": True, "nexus_allowed": True,
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
        "candidate_id": "PRIVATE_ID_" + str(i),
        "symbol": symbol,
        "captured_epoch": (v31.PREREG_EPOCH + 180 + i
                           if captured is None else captured),
        "entry": 100.0, "stop": 102.0, "target": 96.0,
    }


class ProtocolTests(unittest.TestCase):
    def test_immutable_prereg_and_authority_required(self):
        self.assertEqual(v31.PREREG_EPOCH, 1791833992.0)
        bad = v31.EnrollmentAuthority("a" * 40, v31.PREREG_EPOCH - 1,
                                      v31.PREREG_EPOCH + 150, "safe-review")
        with self.assertRaisesRegex(v31.RosterEvidenceError, "CHRONOLOGICAL"):
            v31.propose_roster([candidate(0)], authority=bad)
        missing = v31.EnrollmentAuthority("not_sha", v31.PREREG_EPOCH + 1,
                                          v31.PREREG_EPOCH + 2, "safe-review")
        with self.assertRaises(v31.RosterEvidenceError):
            v31.propose_roster([candidate(0)], authority=missing)

    def test_future_strict_cutoff_old_cohort_excluded(self):
        rows = [candidate(0, captured=v31.PREREG_EPOCH - 10),
                candidate(1, captured=authority().effective_cutoff()),
                candidate(2)]
        report = v31.propose_roster(rows, authority=authority(), private=True)
        self.assertEqual(report["enrolled"], 1)
        self.assertEqual(report["private_candidate_ids"], ("PRIVATE_ID_2",))
        self.assertIn("TARGET_40_NOT_REACHED", report["blockers"])
        self.assertFalse(report["live_allowed"])
        self.assertFalse(report["roster_durable"])

    def test_nexus_counterfactual_and_live_dont_count(self):
        rows = [candidate(i) for i in range(5)]
        rows[0]["nexus_called"] = False
        rows[1]["counterfactual_nexus_v1"] = {"execution_allowed": True}
        rows[1]["nexus_allowed"] = False
        rows[2]["live_eligible"] = True
        rows[3]["execution_effect"] = "SUBMIT_ORDER"
        report = v31.propose_roster(rows, authority=authority())
        self.assertEqual(report["enrolled"], 1)
        self.assertFalse(report["promotion_allowed"])

    def test_40_chronological_first_and_symbol_cap(self):
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOTUSDT",
                   "AVAXUSDT", "LINKUSDT")
        # 60 candidates in shuffled deterministic reverse input order.
        rows = [candidate(i, symbols[i % len(symbols)]) for i in range(60)]
        report = v31.propose_roster(list(reversed(rows)),
                                     authority=authority(), private=True)
        self.assertEqual(report["enrolled"], 40)
        self.assertEqual(report["distinct_symbols"], 6)
        self.assertTrue(report["six_symbol_requirement_met"])
        self.assertEqual(report["largest_symbol_count"], 7)
        self.assertEqual(report["private_candidate_ids"],
                         tuple("PRIVATE_ID_" + str(i) for i in range(40)))
        self.assertEqual(report["status"], "ROSTER_PROVISIONAL_NOT_PROVEN")
        self.assertIn("IMMUTABLE_MANIFEST_NOT_ATTESTED", report["blockers"])
        self.assertNotIn("private_candidate_ids", v31.public_summary(report))

    def test_no_replacement_after_symbol_cap_even_if_diversity_fails(self):
        rows = [candidate(i, "BTCUSDT") for i in range(20)]
        rows += [candidate(i + 20, "ETHUSDT") for i in range(15)]
        report = v31.propose_roster(rows, authority=authority())
        self.assertEqual(report["enrolled"], 20)
        self.assertGreater(report["symbol_cap_rejections"], 0)
        self.assertIn("TARGET_40_NOT_REACHED", report["blockers"])

    def test_duplicate_conflicts_fail_closed(self):
        rows = [candidate(1), candidate(1, "ETHUSDT")]
        with self.assertRaisesRegex(v31.RosterEvidenceError, "CONFLICTING"):
            v31.propose_roster(rows, authority=authority())

    def test_duplicate_identical_is_idempotent(self):
        x = candidate(1)
        y = v31.propose_roster([x, dict(x)], authority=authority())
        self.assertEqual(y["enrolled"], 1)

    def test_malformed_protected_geometry_is_not_replaced(self):
        x = candidate(1)
        x["stop"] = 96.0
        with self.assertRaisesRegex(v31.RosterEvidenceError, "GEOMETRY"):
            v31.propose_roster([x, candidate(2)], authority=authority())

    def test_scan_limit_and_missing_records_never_look_ready(self):
        report = v31.propose_roster([candidate(1)], authority=authority(),
                                     scan_truncated=True)
        self.assertEqual(report["status"], "SCAN_TRUNCATED_NOT_PROVEN")
        self.assertFalse(report["net_evidence_ready"])

    def test_public_summary_never_exposes_candidate_identifiers(self):
        a = v31.propose_roster([candidate(1)], authority=authority(), private=True)
        msg = json.dumps(v31.public_summary(a))
        self.assertNotIn("PRIVATE_ID", msg)
        self.assertNotIn("candidate_ids", msg)
        self.assertNotIn("implementation_sha", msg)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_db_rejected_without_query(self):
        class TradingDB:
            async def _fetchall(self, *args):
                raise AssertionError("SHARED_TRADING_DATABASE_USED")
        with self.assertRaisesRegex(v31.RosterEvidenceError, "DEDICATED_READONLY"):
            await v31.snapshot_readonly(TradingDB(), authority=authority())

    async def test_single_bounded_select_and_payload_consistency(self):
        class FakeDedicated:
            async def _fetchall(self, sql, params):
                self.sql, self.params = sql, params
                x = candidate(1)
                return [{
                    "candidate_id": x["candidate_id"],
                    "captured_epoch": x["captured_epoch"],
                    "payload": json.dumps(x),
                }]
        fake = FakeDedicated()
        with patch("bot.oos_readonly_connection_v1.OOSReadOnlyConnection", FakeDedicated):
            report = await v31.snapshot_readonly(fake, authority=authority())
        self.assertTrue(fake.sql.startswith("SELECT candidate_id"))
        self.assertIn("ORDER BY captured_epoch,candidate_id LIMIT ?", fake.sql)
        self.assertEqual(fake.params,
                         ("HARD_GATE_SHADOW", authority().effective_cutoff(),
                          v31.MAX_SCAN))
        self.assertEqual(report["enrolled"], 1)
        self.assertFalse(report["live_allowed"])

    async def test_table_payload_mismatch_fails_closed(self):
        class FakeDedicated:
            async def _fetchall(self, sql, params):
                x = candidate(1)
                return [{
                    "candidate_id": "FORGED_UNRELATED_ID",
                    "captured_epoch": x["captured_epoch"],
                    "payload": json.dumps(x),
                }]
        fake = FakeDedicated()
        with patch("bot.oos_readonly_connection_v1.OOSReadOnlyConnection", FakeDedicated):
            with self.assertRaisesRegex(v31.RosterEvidenceError, "TABLE_PAYLOAD"):
                await v31.snapshot_readonly(fake, authority=authority())


if __name__ == "__main__":
    unittest.main()
