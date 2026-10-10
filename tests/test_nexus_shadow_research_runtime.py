import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import nexus_shadow_research_runtime as runtime


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(("info", args))

    def warning(self, *args):
        self.lines.append(("warning", args))


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def _exec(self, sql, params=()):
        self.calls.append((sql, params))
        return True


def _signal():
    return SimpleNamespace(
        symbol="SOLUSDT",
        direction="LONG",
        entry_type="PULLBACK",
        score=71.0,
        entry=100.0,
        sl=98.0,
        tp=104.0,
        _bgx_setup_id="SOLUSDT:LONG:PULLBACK:123",
    )


def _decision():
    return SimpleNamespace(
        decision="LONG",
        execution_allowed=True,
        setup_quality=74.0,
        confidence=65.0,
        expected_value=0.42,
        risk_reward=1.8,
        market_regime="TREND",
    )


class ShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_returns_exact_same_champion_object(self):
        champion = _decision()

        class Engine:
            async def _nexus_validate(self, sig, *args, **kwargs):
                return champion

        log = _Log()
        with patch.object(runtime, "_capture_safe", new=AsyncMock(return_value=None)) as capture:
            runtime.install(Engine, log)
            engine = Engine()
            result = await engine._nexus_validate(_signal())
            await asyncio.sleep(0)
            self.assertIs(result, champion)
            capture.assert_awaited_once()

    async def test_research_task_failure_cannot_change_decision(self):
        champion = _decision()

        class Engine:
            async def _nexus_validate(self, sig):
                return champion

        log = _Log()
        with patch.object(
            runtime,
            "_capture_safe",
            new=AsyncMock(side_effect=RuntimeError("research unavailable")),
        ):
            runtime.install(Engine, log)
            result = await Engine()._nexus_validate(_signal())
            await asyncio.sleep(0)
            self.assertIs(result, champion)

    async def test_original_champion_exception_still_propagates(self):
        class Engine:
            async def _nexus_validate(self, sig):
                raise RuntimeError("champion failure")

        log = _Log()
        with patch.object(runtime, "_capture_safe", new=AsyncMock()) as capture:
            runtime.install(Engine, log)
            with self.assertRaisesRegex(RuntimeError, "champion failure"):
                await Engine()._nexus_validate(_signal())
            capture.assert_not_awaited()

    async def test_persistence_is_append_only_and_has_no_exchange_mutation(self):
        db = _FakeDB()
        row = runtime.build_snapshot(
            _signal(), _decision(), captured_epoch=123.0
        )
        before = dict(row)
        ok = await runtime.persist_snapshot(db, row)
        self.assertTrue(ok)
        self.assertEqual(row, before)
        combined = " ".join(sql.upper() for sql, _ in db.calls)
        self.assertIn("ON CONFLICT(CANDIDATE_ID) DO NOTHING", combined)
        self.assertNotIn("UPDATE ", combined)
        self.assertNotIn("DELETE ", combined)
        for forbidden in (
            "PLACE_ORDER",
            "CANCEL_ORDER",
            "SET_LEVERAGE",
            "CLOSE_POSITION",
        ):
            self.assertNotIn(forbidden, combined)

    def test_snapshot_builder_does_not_mutate_signal_or_decision(self):
        sig = _signal()
        decision = _decision()
        sig_before = dict(vars(sig))
        decision_before = dict(vars(decision))
        row = runtime.build_snapshot(sig, decision, captured_epoch=1.0)
        self.assertEqual(vars(sig), sig_before)
        self.assertEqual(vars(decision), decision_before)
        self.assertEqual(row["challenger_status"], "AWAITING_OOS_OUTCOME_AND_CALIBRATION")
        self.assertIn('"execution_effect":"NONE"', row["authority_json"])


if __name__ == "__main__":
    unittest.main()
