"""Regression proof for prospective OOS continuity after the LIVE hard gate clears."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bot import hard_gate_shadow_scan as shadow
from bot import prospective_oos_cohort_v1 as oos


SOURCE = (
    Path(__file__).resolve().parents[1] / "bot" / "hard_gate_shadow_scan.py"
).read_text(encoding="utf-8")


class DB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    async def _exec(self, sql, args=()):
        self.conn.execute(sql, args)
        self.conn.commit()
        return True

    async def _fetchall(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()


class CacheClient:
    def __init__(self):
        self.bars = [
            {"ts": ts, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0}
            for ts in (2700, 3600, 4500, 5400)
        ]

    def get_cached_klines(self, symbol, interval, limit):
        self.last_request = (symbol, interval, limit)
        return list(self.bars)


def _engine():
    return NS(client=CacheClient(), risk=NS(drawdown=0.10))


class GateClearContinuity(unittest.IsolatedAsyncioTestCase):
    async def test_gate_clear_continuity_matures_existing_oos_only(self):
        db = DB()
        self.addCleanup(db.conn.close)

        await db._exec(oos._META)
        baseline = {
            "cohort_id": oos.COHORT_ID,
            "started_epoch": 1000.0,
            "discovery_cutoff_epoch": 1000.0,
            "hypothesis": oos.FROZEN_HYPOTHESIS,
            "hypothesis_frozen": True,
            "reset_allowed": False,
        }
        await db._exec(
            "INSERT INTO prospective_oos_cohort_v1 "
            "(cohort_id,started_epoch,payload) VALUES (?,?,?)",
            (oos.COHORT_ID, 1000.0, json.dumps(baseline)),
        )
        await db._exec(shadow._TABLE)
        await db._exec(shadow._OUTCOMES)

        cid = "HARD_GATE_SHADOW:FILUSDT:LONG:BOS_BREAK:proof"
        candidate = {
            "candidate_id": cid,
            "captured_epoch": 1801.0,
            "symbol": "FILUSDT",
            "side": "LONG",
            "entry": 100.0,
            "population": shadow.POPULATION,
            "shadow_only": True,
            "live_eligible": False,
            "counterfactual_nexus_v1": {
                "cohort": oos.COHORT,
                "candidate_id": cid,
                "risk_epoch_traversal_credit": False,
                "execution_allowed": True,
            },
        }
        await db._exec(
            "INSERT INTO hard_gate_shadow_candidates_v1 "
            "(candidate_id,captured_epoch,symbol,population,payload) VALUES (?,?,?,?,?)",
            (
                cid,
                candidate["captured_epoch"],
                candidate["symbol"],
                shadow.POPULATION,
                json.dumps(candidate),
            ),
        )

        report = {
            "cohort_id": oos.COHORT_ID,
            "enrolled_candidates": 1,
            "observed_60m": 1,
            "observed_240m": 0,
        }
        from bot import prospective_oos_enrollment_audit_v1 as audit
        from bot import prospective_oos_first_approval_review_v1 as first
        from bot import prospective_oos_maturation_review_v1 as maturation

        forbidden = Mock(
            side_effect=AssertionError("candidate generation/execution reached")
        )
        with patch.object(shadow.time, "time", return_value=7000.0), \
             patch.object(oos, "snapshot", new_callable=AsyncMock, return_value=report), \
             patch.object(oos, "format_summary", return_value="[OOS]"), \
             patch.object(oos, "format_concentration", return_value="[OOS_CONC]"), \
             patch.object(
                 audit,
                 "snapshot",
                 new_callable=AsyncMock,
                 return_value={"integrity_pass": True},
             ), \
             patch.object(audit, "format_log", return_value="[AUDIT]"), \
             patch.object(audit, "format_distribution", return_value="[DIST]"), \
             patch.object(first, "snapshot", new_callable=AsyncMock, return_value={}), \
             patch.object(first, "format_log", return_value="[FIRST]"), \
             patch.object(
                 maturation,
                 "snapshot",
                 new_callable=AsyncMock,
                 return_value={},
             ), \
             patch.object(maturation, "format_log", return_value="[MAT]"), \
             patch.object(maturation, "format_candidate_rows", return_value=[]), \
             patch.object(
                 maturation,
                 "format_concentration",
                 return_value="[MAT_CONC]",
             ), \
             patch("bot.strategy.Analyzer.analyze_mtf", forbidden), \
             patch("bot.nexus_ai.decide", forbidden):
            result = await shadow._mature_existing_prospective_oos_when_gate_clear(
                _engine(), db
            )

        self.assertEqual(result["status"], "EXISTING_OOS_CONTINUITY")
        self.assertEqual(result["new_candidates"], 0)
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["observed_60m"], 1)
        self.assertEqual(result["observed_240m"], 0)
        self.assertTrue(result["audit_pass"])
        self.assertFalse(result["promotion_allowed"])
        self.assertFalse(result["live_allowed"])
        self.assertEqual(result["decision_effect"], "NONE")
        self.assertEqual(result["execution_effect"], "NONE")
        self.assertEqual(forbidden.call_count, 0)

        rows = await db._fetchall(
            "SELECT candidate_id,horizon,payload FROM hard_gate_shadow_outcomes_v1"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_id"], cid)
        self.assertEqual(rows[0]["horizon"], 60)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["outcome"], "OBSERVED")
        self.assertEqual(payload["observation_start"], 2700)

    async def test_scan_if_enabled_routes_gate_clear_to_continuity_without_scan(self):
        engine = _engine()
        with patch.object(shadow, "enabled", return_value=True), \
             patch.object(
                 shadow,
                 "gate_snapshot",
                 return_value={"live_entries_blocked": False},
             ), \
             patch.object(
                 shadow,
                 "_mature_existing_prospective_oos_when_gate_clear",
                 new_callable=AsyncMock,
                 return_value={"status": "EXISTING_OOS_CONTINUITY"},
             ) as continuity, \
             patch.object(shadow, "scan", new_callable=AsyncMock) as scan:
            result = await shadow.scan_if_enabled(engine)

        self.assertEqual(result["status"], "EXISTING_OOS_CONTINUITY")
        continuity.assert_awaited_once()
        scan.assert_not_awaited()


class GateClearContinuityStatic(unittest.TestCase):
    def test_gate_clear_continuity_is_research_only_and_cannot_generate_candidates(self):
        start = SOURCE.index(
            "async def _mature_existing_prospective_oos_when_gate_clear"
        )
        end = SOURCE.index("async def scan_if_enabled", start)
        segment = SOURCE[start:end]

        required = (
            '"existing_candidates_only": True',
            '"new_candidates": 0',
            '"candidate_generation_unchanged": True',
            '"thresholds_unchanged": True',
            '"risk_unchanged": True',
            '"sizing_unchanged": True',
            '"leverage_unchanged": True',
            '"current_hard_gate_unchanged": True',
            '"automatic_promotion": False',
            '"promotion_allowed": False',
            '"live_allowed": False',
            '"decision_effect": "NONE"',
            '"execution_effect": "NONE"',
        )
        for marker in required:
            self.assertIn(marker, segment)

        forbidden = (
            "Analyzer(",
            ".analyze_mtf(",
            "nexus_ai.decide",
            ".place_order(",
            "submission_committed",
            "dispatch_order(",
            "MAX_DRAWDOWN =",
            "LEVERAGE =",
            "persist_candidate(",
        )
        for marker in forbidden:
            self.assertNotIn(marker, segment)

    def test_gate_clear_cache_gap_is_deferred_and_cohort_metadata_is_frozen(self):
        start = SOURCE.index(
            "async def _mature_existing_prospective_oos_when_gate_clear"
        )
        end = SOURCE.index("async def scan_if_enabled", start)
        segment = SOURCE[start:end]

        guard = 'if outcome.get("outcome") != "OBSERVED":'
        write = '"INSERT INTO hard_gate_shadow_outcomes_v1 "'
        self.assertIn(guard, segment)
        self.assertIn('stats["cache_gap_deferred"] += 1', segment)
        self.assertLess(segment.index(guard), segment.index(write))
        self.assertIn('baseline.get("cohort_id") != oos.COHORT_ID', segment)
        self.assertIn('baseline.get("hypothesis_frozen") is not True', segment)
        self.assertIn('baseline.get("reset_allowed") is not False', segment)


if __name__ == "__main__":
    unittest.main()
