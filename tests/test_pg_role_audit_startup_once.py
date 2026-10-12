"""Offline regression proof for opt-in startup PostgreSQL privilege observer."""
import asyncio
import ast
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from bot import pg_role_audit_startup_once as observer


class PgRoleStartupOnceTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_by_default_and_requires_exact_flag(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(observer.enabled())
        for setting in ("", "false", "TRUE", "1", "yes", " true "):
            with self.subTest(setting=setting), patch.dict(
                os.environ, {observer.FLAG: setting}, clear=True
            ):
                self.assertFalse(observer.enabled())
        with patch.dict(os.environ, {observer.FLAG: "true"}, clear=True):
            self.assertTrue(observer.enabled())

    async def test_waits_for_bootstrap_without_blocking_it(self):
        future = asyncio.get_running_loop().create_future()
        audit = AsyncMock(return_value=0)
        output = StringIO()
        with redirect_stdout(output):
            task = asyncio.create_task(observer.run_after_bootstrap(future, runner=audit))
            await asyncio.sleep(0)
            audit.assert_not_awaited()
            self.assertFalse(task.done())
            future.set_result(None)
            result = await asyncio.wait_for(task, 1)
        self.assertTrue(result)
        audit.assert_awaited_once()
        self.assertIn("status=PASS", output.getvalue())
        self.assertIn("execution_effect=NONE", output.getvalue())

    async def test_failing_audit_remains_failing(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        output = StringIO()
        audit = AsyncMock(return_value=1)
        with redirect_stdout(output):
            result = await observer.run_after_bootstrap(future, runner=audit)
        self.assertFalse(result)
        self.assertIn("status=FAIL", output.getvalue())
        self.assertNotIn("READ_ONLY_ROLE_VERIFIED=true", output.getvalue())

    async def test_inconclusive_audit_is_not_approved(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        output = StringIO()
        with redirect_stdout(output):
            result = await observer.run_after_bootstrap(
                future, runner=AsyncMock(return_value=2))
        self.assertFalse(result)
        self.assertIn("status=INCONCLUSIVE", output.getvalue())

    async def test_unexpected_code_fails_closed(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        output = StringIO()
        with redirect_stdout(output):
            result = await observer.run_after_bootstrap(
                future, runner=AsyncMock(return_value=99))
        self.assertFalse(result)
        self.assertIn("unexpected_exit_code", output.getvalue())
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output.getvalue())

    async def test_bootstrap_failure_no_audit_and_no_secret_leak(self):
        future = asyncio.get_running_loop().create_future()
        future.set_exception(RuntimeError("password=SECRET_DSN"))
        runner = AsyncMock()
        output = StringIO()
        with redirect_stdout(output):
            result = await observer.run_after_bootstrap(future, runner=runner)
        self.assertFalse(result)
        runner.assert_not_awaited()
        self.assertIn("bootstrap_failed", output.getvalue())
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output.getvalue())
        self.assertNotIn("SECRET_DSN", output.getvalue())

    async def test_timeout_does_not_cancel_live_bootstrap(self):
        future = asyncio.get_running_loop().create_future()
        runner = AsyncMock()
        output = StringIO()
        with redirect_stdout(output):
            result = await observer.run_after_bootstrap(
                future, runner=runner, bootstrap_timeout_s=0.001)
        self.assertFalse(result)
        self.assertFalse(future.cancelled())
        runner.assert_not_awaited()
        self.assertIn("bootstrap_timeout", output.getvalue())
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output.getvalue())
        future.set_result(None)

    async def test_audit_exception_is_redacted(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        audit = AsyncMock(side_effect=RuntimeError("DSN=SECRET_CONNECTION"))
        output = StringIO()
        with redirect_stdout(output):
            result = await observer.run_after_bootstrap(future, runner=audit)
        self.assertFalse(result)
        self.assertIn("audit_exception", output.getvalue())
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output.getvalue())
        self.assertNotIn("SECRET_CONNECTION", output.getvalue())

    async def test_shutdown_cancels_observer_not_bootstrap(self):
        future = asyncio.get_running_loop().create_future()
        output = StringIO()
        with redirect_stdout(output):
            task = asyncio.create_task(observer.run_after_bootstrap(future))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(future.cancelled())
        self.assertIn("reason=shutdown", output.getvalue())
        self.assertIn("READ_ONLY_ROLE_VERIFIED=false", output.getvalue())
        future.set_result(None)

    def test_production_lifespan_integration_keeps_task_in_shutdown(self):
        # Contract-only inspection: importing main executes runtime side effects.
        main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(main_source)
        lifespan = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
        segment = ast.get_source_segment(main_source, lifespan)
        self.assertIn('os.environ.get("PG_ROLE_AUDIT_STARTUP_ONCE", "") == "true"',
                      segment)
        self.assertIn("run_after_bootstrap(app.state.bootstrap_task)", segment)
        self.assertIn('"pg_role_audit_task"', segment)
        self.assertIn("'pg_role_audit_task'", segment)


if __name__ == "__main__":
    unittest.main()
