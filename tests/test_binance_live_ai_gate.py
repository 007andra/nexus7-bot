"""NEXUS AI gate on the production venue (Binance USD-M, LIVE pilot path).

Port of tests/test_ai_gate.py, which only exercised the KuCoin core engine.
Here the production engine class, runtime bootstrap, real BinanceClient and
real _nexus_validate run; only ``nexus_ai.decide`` is substituted. AI01 is the
positive control proving the same fixture does reach dispatch, so every
negative case below is a real block, not an unreachable path.
"""
from tests.binance_engine_fixture import (  # noqa: I001  (sets env first)
    BinanceEngineFixture, approval,
)

import asyncio
import time
import unittest


class BinanceLiveAIGateTests(BinanceEngineFixture):
    async def test_AI01_valid_approval_dispatches_once(self):
        await self.attempt(approval())
        self.client.place_order.assert_awaited_once()
        call = self.client.place_order.call_args
        self.assertEqual(call.kwargs.get("symbol", call.args[0] if call.args else None),
                         "TESTUSDT")

    async def test_AI02_rejection_never_dispatches(self):
        await self.attempt(approval(allowed=False))
        self.client.place_order.assert_not_awaited()

    async def test_AI03_missing_decision_never_dispatches(self):
        await self.attempt(None)
        self.client.place_order.assert_not_awaited()

    async def test_AI04_decision_exception_never_dispatches(self):
        self.decide.side_effect = RuntimeError("fixture")
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    async def test_AI05_decision_timeout_never_dispatches(self):
        self.replace("bot.engine._NEXUS_TIMEOUT_S", 0.01)

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return approval()
        self.decide.side_effect = slow
        await self.engine._open(self.sig)
        await asyncio.sleep(0.25)          # let the worker thread finish
        self.client.place_order.assert_not_awaited()

    async def test_AI06_truthy_non_bool_permission_never_dispatches(self):
        for value in ("false", "true", "yes", "1", 1):
            with self.subTest(value=value):
                await self.attempt(approval(allowed=value))
                self.client.place_order.assert_not_awaited()

    async def test_AI07_malformed_decision_never_dispatches(self):
        for value in ({"execution_allowed": True}, "", object()):
            with self.subTest(value=type(value).__name__):
                await self.attempt(value)
                self.client.place_order.assert_not_awaited()

    async def test_AI08_nexus_disabled_never_dispatches(self):
        self.replace("bot.engine._NEXUS_ENABLED", False)
        await self.attempt(approval())
        self.client.place_order.assert_not_awaited()

    async def test_rejection_is_logged_with_source(self):
        await self.attempt(approval(allowed=False))
        self.assertTrue(any(
            "[AI_DECISION]" in str(c) and "REJECT" in str(c)
            and "decision_source=nexus_ai" in str(c)
            for c in self.logs.method_calls))

    async def test_wrong_symbol_or_nonfinite_confidence_never_dispatches(self):
        bad_symbol = approval("OTHERUSDT")
        nan_conf = approval()
        nan_conf.confidence = float("nan")
        for decision in (bad_symbol, nan_conf):
            with self.subTest(symbol=decision.symbol):
                await self.attempt(decision)
                self.client.place_order.assert_not_awaited()

    async def test_rejected_decision_causes_zero_exchange_mutations(self):
        await self.attempt(approval(allowed=False))
        self.assertEqual(self.mutations(), [])

    async def test_approved_decision_mutates_only_entry_and_its_protection(self):
        await self.attempt(approval())
        paths = sorted(c[1] for c in self.mutations())
        self.assertEqual(paths, ["/fapi/v1/algoOrder", "/fapi/v1/algoOrder",
                                 "/fapi/v1/order"])


if __name__ == "__main__":
    unittest.main()
