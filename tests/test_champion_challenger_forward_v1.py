import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import champion_challenger_forward_v1 as forward


class _DB:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or []

    async def _exec(self, sql, params=()):
        self.calls.append((sql, params))
        return True

    async def _fetchall(self, sql, params=()):
        self.calls.append((sql, params))
        return list(self.rows)


class _Log:
    def info(self, *args):
        pass


def _sig():
    return SimpleNamespace(
        symbol="SOLUSDT",
        direction="LONG",
        entry=100.0,
        sl=98.0,
        tp=104.0,
        _bgx_setup_id="SOLUSDT:LONG:MOMENTUM:forward-1",
    )


def _decision(*, allowed, score=55.0, ev=0.50, rr=1.80):
    return SimpleNamespace(
        execution_allowed=allowed,
        setup_quality=score,
        expected_value=ev,
        risk_reward=rr,
    )


class ForwardStudyTests(unittest.IsolatedAsyncioTestCase):
    def test_final_score_reject_becomes_challenger_only(self):
        sig = _sig()
        decision = _decision(allowed=False, score=55.0, ev=0.50, rr=1.80)
        sig_before = dict(vars(sig))
        decision_before = dict(vars(decision))
        with patch.dict(os.environ, {"NEXUS_MIN_RR_NET": "1.60"}, clear=False):
            row = forward.build_forward_record(sig, decision, captured_epoch=1.0)
        self.assertEqual(row["champion_allowed"], 0)
        self.assertEqual(row["challenger_eligible"], 1)
        self.assertEqual(row["challenger_approved"], 1)
        self.assertEqual(
            row["selection_reason"], "CHALLENGER_ONLY_FINAL_SCORE_IGNORED"
        )
        self.assertEqual(vars(sig), sig_before)
        self.assertEqual(vars(decision), decision_before)
        authority = json.loads(row["authority_json"])
        self.assertEqual(authority["decision_effect"], "NONE")
        self.assertEqual(authority["execution_effect"], "NONE")

    def test_early_reject_is_not_challenger_eligible(self):
        row = forward.build_forward_record(
            _sig(),
            _decision(allowed=False, score=0.0, ev=0.0, rr=0.0),
            captured_epoch=1.0,
        )
        self.assertEqual(row["challenger_eligible"], 0)
        self.assertEqual(row["challenger_approved"], 0)
        self.assertEqual(
            row["selection_reason"],
            "BOTH_REJECT_NOT_FINAL_ECONOMICALLY_ELIGIBLE",
        )

    def test_champion_approval_remains_approval_for_challenger(self):
        with patch.dict(os.environ, {"NEXUS_MIN_RR_NET": "1.60"}, clear=False):
            row = forward.build_forward_record(
                _sig(),
                _decision(allowed=True, score=66.0, ev=0.50, rr=1.80),
                captured_epoch=1.0,
            )
        self.assertEqual(row["champion_allowed"], 1)
        self.assertEqual(row["challenger_approved"], 1)
        self.assertEqual(row["selection_reason"], "BOTH_APPROVE_FINAL_SCORE_PASSED")

    async def test_persistence_is_append_only(self):
        db = _DB()
        row = forward.build_forward_record(
            _sig(), _decision(allowed=False), captured_epoch=1.0
        )
        ok = await forward.persist_forward_record(db, row)
        self.assertTrue(ok)
        joined = " ".join(sql.upper() for sql, _ in db.calls)
        self.assertIn("ON CONFLICT(CANDIDATE_ID) DO NOTHING", joined)
        self.assertNotIn("UPDATE ", joined)
        self.assertNotIn("DELETE ", joined)
        for forbidden in ("PLACE_ORDER", "CANCEL_ORDER", "SET_LEVERAGE"):
            self.assertNotIn(forbidden, joined)

    async def test_paired_report_uses_identical_prospective_population(self):
        rows = [
            ("a", 1.0, 1, 65.0, 1, 0.25, 1.0),
            ("b", 2.0, 0, 55.0, 1, 0.20, -0.5),
            ("c", 3.0, 0, 0.0, 0, 0.0, None),
        ]
        db = _DB(rows)
        report = await forward.prospective_report(db, horizon="240m")
        self.assertEqual(report["candidate_population"], 3)
        self.assertEqual(report["known_outcomes"], 2)
        self.assertEqual(report["decision_disagreements"], 1)
        self.assertEqual(report["champion_selected"], 1)
        self.assertEqual(report["challenger_selected"], 2)
        self.assertFalse(report["readout_ready"])
        self.assertTrue(report["prospective_only"])
        self.assertEqual(report["decision_effect"], "NONE")
        self.assertEqual(report["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
