"""V3.1 offline manifest chain tests. Never trade or connect to an exchange."""
import json
import unittest
from dataclasses import replace

from bot import short_down_bos_v31_manifest as ledger
from bot.short_down_bos_v31_readonly_enrollment import (
    EnrollmentAuthority, PREREG_EPOCH, RosterEvidenceError,
)


def authority():
    return EnrollmentAuthority(
        implementation_sha="e" * 40,
        implementation_commit_epoch=PREREG_EPOCH + 100,
        authorized_capture_epoch=PREREG_EPOCH + 200,
        authorization_reference="independent-review-protocol-v31",
    )


def candidate(i, symbol="BTCUSDT"):
    return {
        "candidate_id": "PRIVATE_EVENT_" + str(i),
        "captured_epoch": PREREG_EPOCH + 300 + i,
        "symbol": symbol, "entry": 100.0, "stop": 102.0, "target": 96.0,
        "population": "HARD_GATE_SHADOW", "side": "SHORT",
        "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "nexus_called": True, "nexus_allowed": True,
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
    }


class PureLedgerTests(unittest.TestCase):
    def setUp(self):
        self.auth = authority()
        self.root = ledger.empty_manifest(self.auth)

    def test_header_cutoff_hash_and_no_live(self):
        result = ledger.verify_manifest(self.root, self.auth)
        self.assertEqual(result["enrolled"], 0)
        self.assertEqual(result["observed_events"], 0)
        self.assertEqual(len(result["head_digest"]), 64)
        self.assertFalse(result["roster_durable"])
        self.assertFalse(result["live_allowed"])
        self.assertFalse(result["promotion_allowed"])

    def test_append_is_immutable_and_idempotent(self):
        first = ledger.append_candidate(self.root, candidate(1), self.auth)
        self.assertEqual(len(self.root.events), 0)
        self.assertEqual(len(first.events), 1)
        same = ledger.append_candidate(first, candidate(1), self.auth)
        self.assertIs(same, first)
        info = ledger.verify_manifest(same, self.auth)
        self.assertEqual(info["enrolled"], 1)

    def test_event_cannot_rewrite_stop_or_target(self):
        first = ledger.append_candidate(self.root, candidate(1), self.auth)
        bad = candidate(1)
        bad["stop"] = 103
        with self.assertRaisesRegex(RosterEvidenceError, "CONFLICTING_CANDIDATE"):
            ledger.append_candidate(first, bad, self.auth)
        bad = candidate(1)
        bad["target"] = 95
        with self.assertRaisesRegex(RosterEvidenceError, "CONFLICTING_CANDIDATE"):
            ledger.append_candidate(first, bad, self.auth)

    def test_late_arrival_fails_closed_even_before_40(self):
        first = ledger.append_candidate(self.root, candidate(9), self.auth)
        with self.assertRaisesRegex(RosterEvidenceError, "LATE_ARRIVAL"):
            ledger.append_candidate(first, candidate(1), self.auth)

    def test_old_and_noncanonical_are_excluded(self):
        pre = candidate(1)
        pre["captured_epoch"] = PREREG_EPOCH + 150
        with self.assertRaisesRegex(RosterEvidenceError, "NOT_AFTER_EFFECTIVE"):
            ledger.append_candidate(self.root, pre, self.auth)
        fake = candidate(1)
        fake["nexus_called"] = False
        with self.assertRaisesRegex(RosterEvidenceError, "NON_CANONICAL"):
            ledger.append_candidate(self.root, fake, self.auth)

    def test_cap_skip_is_recorded_and_never_replaced(self):
        root = self.root
        for i in range(12):
            root = ledger.append_candidate(root, candidate(i), self.auth)
        self.assertEqual(len(root.events), 12)
        self.assertEqual([x.disposition for x in root.events][-3:],
                         ["ENROLLED", "SKIPPED_SYMBOL_CAP", "SKIPPED_SYMBOL_CAP"])
        self.assertEqual(ledger.verify_manifest(root, self.auth)["enrolled"], 10)
        # Later symbol can enroll but never reinterpret previous skipped event.
        root = ledger.append_candidate(root, candidate(12, "ETHUSDT"), self.auth)
        self.assertEqual(ledger.verify_manifest(root, self.auth)["enrolled"], 11)

    def test_40_fixed_6_symbols_and_post_target_skips(self):
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "UNIUSDT",
                   "AVAXUSDT", "LINKUSDT")
        state = self.root
        for i in range(43):
            state = ledger.append_candidate(state, candidate(i, symbols[i % 6]), self.auth)
        result = ledger.verify_manifest(state, self.auth)
        self.assertEqual(result["enrolled"], 40)
        self.assertEqual(result["distinct_symbols"], 6)
        self.assertEqual(result["largest_symbol_count"], 7)
        self.assertEqual(state.events[-1].disposition, "SKIPPED_AFTER_40")
        self.assertFalse(result["net_evidence_ready"])

    def test_forged_event_or_sequence_breaks_chain(self):
        state = ledger.append_candidate(self.root, candidate(1), self.auth)
        e = state.events[0]
        forged = replace(state, events=(replace(e, stop="105"),))
        with self.assertRaisesRegex(RosterEvidenceError, "EVENT_HASH"):
            ledger.verify_manifest(forged, self.auth)
        seq = replace(state, events=(replace(e, sequence=2),))
        with self.assertRaisesRegex(RosterEvidenceError, "SEQUENCE"):
            ledger.verify_manifest(seq, self.auth)

    def test_missing_event_breaks_sequence_and_external_anchor(self):
        state = self.root
        for i in range(3):
            state = ledger.append_candidate(state, candidate(i), self.auth)
        verified = ledger.verify_manifest(state, self.auth)
        partial = replace(state, events=state.events[1:])
        with self.assertRaisesRegex(RosterEvidenceError, "SEQUENCE"):
            ledger.verify_manifest(partial, self.auth)
        with self.assertRaisesRegex(RosterEvidenceError, "EXTERNAL_ANCHOR"):
            ledger.verify_manifest(state, self.auth, trusted_head_digest="f" * 64)
        good = ledger.verify_manifest(state, self.auth,
                                     trusted_head_digest=verified["head_digest"])
        self.assertTrue(good["verified_against_external_anchor"])

    def test_header_authority_mutation_rejected(self):
        state = ledger.append_candidate(self.root, candidate(0), self.auth)
        change = replace(state, effective_cutoff_epoch=PREREG_EPOCH)
        with self.assertRaisesRegex(RosterEvidenceError, "AUTHORITY_CONFLICT"):
            ledger.verify_manifest(change, self.auth)

    def test_outcome_not_used_and_replay_order(self):
        start = candidate(1)
        start["future_return"] = 999999.0
        start["outcome"] = "OBSERVED"
        x = ledger.append_candidate(self.root, start, self.auth)
        y = ledger.append_candidate(self.root, candidate(1), self.auth)
        self.assertEqual(x.events[0].digest, y.events[0].digest)
        self.assertNotIn("future_return", json.dumps(x.events[0].__dict__))
        self.assertNotIn("OBSERVED", json.dumps(x.events[0].__dict__))

    def test_no_external_anchor_is_not_durable_proof(self):
        state = ledger.append_candidate(self.root, candidate(1), self.auth)
        result = ledger.verify_manifest(state, self.auth)
        self.assertFalse(result["verified_against_external_anchor"])
        self.assertFalse(result["roster_durable"])
        self.assertFalse(result["net_evidence_ready"])


if __name__ == "__main__":
    unittest.main()
