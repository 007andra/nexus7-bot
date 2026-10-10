import asyncio
import unittest
from bot.engine_cancel_causal_diag_v1 import cancellation_snapshot, format_cancellation


class CancellationDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_task_observation_only(self):
        state = cancellation_snapshot()
        self.assertTrue(state["task_present"])
        self.assertEqual(state["initiator"], "UNKNOWN")
        self.assertEqual(state["execution_effect"], "NONE")

    async def test_pending_cancel_is_not_attributed(self):
        async def target():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return cancellation_snapshot()
        task = asyncio.create_task(target(), name="probe")
        await asyncio.sleep(0)
        task.cancel()
        state = await task
        self.assertGreaterEqual(state["cancel_pending"], 1)
        self.assertEqual(state["initiator"], "UNKNOWN")

    async def test_log_format(self):
        self.assertIn("ENGINE_CANCEL_CAUSAL_DIAG_V1", format_cancellation())


if __name__ == "__main__":
    unittest.main()
