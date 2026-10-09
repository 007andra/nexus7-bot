"""#596: phase-only correlation for research snapshot timeouts, with no I/O changes."""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import risk_epoch_shadow as epoch


BASE = {"started_epoch": 1234.0, "epoch_id": "REENTRY_SAMPLE"}
ENGINE = SimpleNamespace()
CANDIDATE = {
    "candidate_id": "PRIVATE_ID_NOT_EXPORTED",
    "population": "HARD_GATE_SHADOW",
    "captured_epoch": 1235.0,
    "nexus_called": False,
}


class FakeDB:
    def __init__(self, error_at=None):
        self.error_at = error_at
        self.operations = []

    async def _fetchall(self, sql, args=()):
        kind = ("OUTCOMES" if "SELECT o.candidate_id" in sql else
                "CANDIDATES" if "SELECT payload FROM hard_gate_shadow_candidates_v1" in sql
                else "UNKNOWN")
        self.operations.append(kind)
        if kind == self.error_at:
            raise asyncio.TimeoutError()
        if kind == "CANDIDATES":
            return [(json.dumps(CANDIDATE),)]
        if kind == "OUTCOMES":
            return []
        raise AssertionError("UNEXPECTED_QUERY")

    async def _exec(self, *a, **kw):
        raise AssertionError("NO_SCHEMA_WRITE_IN_TEST")


class RiskEpochPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_in_baseline_is_marked(self):
        marker = {}
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1":"true"}), patch.object(
            epoch, "_ensure_epoch", AsyncMock(side_effect=asyncio.TimeoutError)
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await epoch.snapshot(FakeDB(), ENGINE, start_equity=5.0, phase_marker=marker)
        self.assertEqual(marker, {"phase": "BASELINE"})

    async def test_failure_in_candidates_is_marked(self):
        marker, db = {}, FakeDB(error_at="CANDIDATES")
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1":"true"}), patch.object(
            epoch, "_ensure_epoch", AsyncMock(return_value=BASE)
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await epoch.snapshot(db, ENGINE, start_equity=5.0, phase_marker=marker)
        self.assertEqual(marker, {"phase":"CANDIDATES"})
        self.assertEqual(db.operations, ["CANDIDATES"])

    async def test_failure_in_outcomes_is_marked(self):
        marker, db = {}, FakeDB(error_at="OUTCOMES")
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1":"true"}), patch.object(
            epoch, "_ensure_epoch", AsyncMock(return_value=BASE)
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await epoch.snapshot(db, ENGINE, start_equity=5.0, phase_marker=marker)
        self.assertEqual(marker, {"phase":"OUTCOMES"})
        self.assertEqual(db.operations, ["CANDIDATES", "OUTCOMES"])

    async def test_normal_snapshot_matches_untraced_snapshot(self):
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1":"true"}), patch.object(
            epoch, "_ensure_epoch", AsyncMock(return_value=BASE)
        ):
            marker = {}
            old = await epoch.snapshot(FakeDB(), ENGINE, start_equity=5.0)
            new = await epoch.snapshot(FakeDB(), ENGINE, start_equity=5.0, phase_marker=marker)
        self.assertEqual(new, old)
        self.assertEqual(marker, {"phase":"COMPUTE"})
        self.assertFalse(new["promotion_allowed"])
        self.assertEqual(new["execution_effect"], "NONE")
        self.assertNotIn("PRIVATE_ID_NOT_EXPORTED", json.dumps(marker))

    async def test_parent_wait_for_keeps_child_phase_on_cancellation(self):
        marker = {}
        class SlowDB(FakeDB):
            async def _fetchall(self, sql, args=()):
                await asyncio.sleep(30)
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1":"true"}), patch.object(
            epoch, "_ensure_epoch", AsyncMock(return_value=BASE)
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    epoch.snapshot(SlowDB(), ENGINE, start_equity=5.0, phase_marker=marker),
                    timeout=0.02,
                )
        self.assertEqual(marker, {"phase":"CANDIDATES"})

    async def test_disabled_is_inert_no_phase_change(self):
        marker={}
        with patch.dict(os.environ, {"RISK_EPOCH_SHADOW_V1":"false"}):
            result=await epoch.snapshot(FakeDB(), ENGINE, start_equity=5, phase_marker=marker)
        self.assertEqual(marker,{})
        self.assertEqual(result["status"],"DISABLED")


if __name__ == "__main__":
    unittest.main()
