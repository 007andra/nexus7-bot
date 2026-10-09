"""No-production tests for cancellation attribution (issue #628)."""
import asyncio
import unittest

from bot import engine_cancel_origin_observer_v1 as obs


class CancellationOriginTests(unittest.TestCase):
    def test_task_api_stable_values(self):
        class Task:
            def __init__(self, v):
                self.v = v
            def cancelling(self):
                return self.v

        expected = [
            (0, "PROPAGATED_AWAIT_CANCEL_OR_UNATTRIBUTED", False),
            (1, "TASK_CANCEL_REQUEST_PENDING", True),
            (2, "TASK_CANCEL_REQUEST_PENDING", True),
            (-1, "UNKNOWN_TASK_CANCEL_API", None),
            (None, "UNKNOWN_TASK_CANCEL_API", None),
            (True, "UNKNOWN_TASK_CANCEL_API", None),
        ]
        for count, kind, requested in expected:
            with self.subTest(count=count):
                data = obs.snapshot(Task(count))
                self.assertEqual(data["kind"], kind)
                self.assertIs(data["pending_cancel_requests"], requested)
                msg = obs.format_event(data)
                self.assertIn("actual_cancel_initiator=UNKNOWN", msg)
                self.assertIn("risk_unchanged=true", msg)
                self.assertIn("backup_gate_effect=NONE", msg)
                self.assertIn("execution_effect=NONE", msg)

    def test_bad_task_api_fails_safe(self):
        class BadTask:
            def cancelling(self):
                raise RuntimeError("private:DO_NOT_LOG")
        data = obs.snapshot(BadTask())
        self.assertEqual(data["kind"], "CLASSIFIER_ERROR")
        self.assertNotIn("DO_NOT_LOG", obs.format_event(data))

    def test_unknown_task_and_malicious_kind_redacted(self):
        self.assertEqual(obs.snapshot(object())["kind"], "UNKNOWN_TASK_CANCEL_API")
        self.assertIn(
            "kind=CLASSIFIER_ERROR",
            obs.format_event({"kind": "api_key=SECRETS", "pending_cancel_requests": None}),
        )
        self.assertNotIn(
            "SECRETS",
            obs.format_event({"kind": "api_key=SECRETS", "pending_cancel_requests": None}),
        )


class RealAsyncioCancellationProof(unittest.IsolatedAsyncioTestCase):
    async def test_child_cancelled_error_does_not_imply_parent_cancel(self):
        async def child():
            raise asyncio.CancelledError()
        try:
            await child()
        except asyncio.CancelledError:
            data = obs.snapshot()
            self.assertEqual(data["kind"], "PROPAGATED_AWAIT_CANCEL_OR_UNATTRIBUTED")
            self.assertIs(data["pending_cancel_requests"], False)
        else:
            self.fail("Expected propagated asyncio.CancelledError")

    async def test_explicit_parent_cancel_is_classified(self):
        records = []
        async def worker():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                records.append(obs.snapshot())
                raise
        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "TASK_CANCEL_REQUEST_PENDING")
        self.assertIs(records[0]["pending_cancel_requests"], True)


if __name__ == "__main__":
    unittest.main()
