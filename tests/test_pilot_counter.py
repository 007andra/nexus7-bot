"""Two pilot submissions, including ambiguous/error outcomes and concurrency."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from tests.test_pilot_minimum import PilotFixture
from tests.test_ai_gate import approval
from bot.strategy import Signal
from bot.kucoin import KuCoinClient
from bot.order_state import OrderState
from tests.execution_test_context import ValidExecutionTestContext


class PilotCounterTests(PilotFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client.place_order = AsyncMock(return_value={'orderId': 'mock'})
        # This suite counts pilot submissions with a client that has no exchange;
        # F-013 post-fill exchange reconciliation is isolated (own tests cover it).
        from unittest.mock import patch
        patcher = patch("bot.postfill_geometry.reconcile_after_open", AsyncMock(return_value="UNCONFIRMED"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.wait_for_fill = AsyncMock(
            return_value={
                'filled': True,
                'status': {'dealSize': 1, 'dealValue': 100},
                'timed_out': False,
            }
        )

    async def second_symbol(self):
        sig = Signal('OTHERUSDT', 'LONG', 100., 99., 103., 80., 'offline', 90)
        self.engine._nexus_validate.return_value = approval('OTHERUSDT')
        await self.engine._open(sig)

    def count(self):
        return self.engine.pilot.state.new_order_submissions_this_session

    async def test_PILOT03_two_submissions_consume_session(self):
        await self.engine._open(self.sig)
        self.assertEqual(self.count(), 1)
        await self.second_symbol()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.client.place_order.await_count, 2)

    async def test_PILOT04_third_submission_is_blocked(self):
        await self.engine._open(self.sig)
        await self.second_symbol()
        await self.engine._open(self.sig)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.client.place_order.await_count, 2)

    async def test_PILOT05_lost_response_consumes_one_slot_and_blocks_followup(self):
        self.client.place_order.side_effect = [TimeoutError('lost response'), {'orderId': 'mock'}]
        self.client._position_exists = AsyncMock(return_value=False)
        await self.engine._open(self.sig)
        await self.second_symbol()
        # Ambiguous/lost response fail-closed: the reservation is consumed,
        # but no second live submission is allowed while the first outcome is unresolved.
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.client.place_order.await_count, 1)

    async def test_PILOT06_partial_fill_consumes_one_slot_and_blocks_followup(self):
        self.client.place_order.side_effect = [
            {'orderId': 'mock-1'},
            {'orderId': 'mock-2'},
        ]
        self.client.wait_for_fill = AsyncMock(
            side_effect=[
                {'filled': False, 'status': {'dealSize': .5}, 'timed_out': True},
                {'filled': True, 'status': {'dealSize': 1}, 'timed_out': False},
            ]
        )
        self.engine._reconcile_exchange_positions = AsyncMock(return_value=[])
        await self.engine._open(self.sig)
        await self.second_symbol()
        # Partial/unconfirmed fill triggers immediate reconciliation and blocks
        # further entries until the execution state is resolved.
        self.assertEqual(self.client.place_order.await_count, 1)
        self.assertEqual(self.count(), 1)

    async def test_PILOT07_protection_failure_consumes_one_slot_and_blocks_followup(self):
        self.client.place_order.side_effect = [
            {'orderId': 'mock-1', 'sl_tp_failed': True},
            {'orderId': 'mock-2', 'sl_tp_failed': True},
        ]
        self.client.set_position_stops = AsyncMock(return_value=False)
        await self.engine._open(self.sig)
        await self.second_symbol()
        # Protection failure closes/blocks the affected execution path; it must
        # not silently permit another entry until protection state is resolved.
        entries = [
            c for c in self.client.place_order.await_args_list
            if not c.kwargs.get('reduce_only')
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.count(), 1)

    async def test_PILOT08_pending_other_symbol_blocks(self):
        pending, _ = self.engine.orders.get_or_create('pending', 'OTHERUSDT', 'Buy', 1.)
        pending.transition(OrderState.SUBMITTING)
        await self.engine._open(self.sig)
        self.client.place_order.assert_not_awaited()

    async def test_PILOT09_reduce_only_not_counted(self):
        self.client.place_order.return_value = {'orderId': 'mock', 'sl_tp_failed': True}
        self.client.set_position_stops = AsyncMock(return_value=False)
        await self.engine._open(self.sig)
        self.assertEqual(self.client.place_order.await_count, 2)
        self.assertTrue(self.client.place_order.await_args_list[1].kwargs['reduce_only'])
        self.assertEqual(self.count(), 1)

    async def test_PILOT10_concurrent_open(self):
        arrivals = 0
        barrier = asyncio.Event()

        async def decide(sig):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await barrier.wait()
            return approval(sig.symbol)

        self.engine._nexus_validate.side_effect = decide
        sig = Signal('OTHERUSDT', 'LONG', 100., 99., 103., 80., 'offline', 90)
        await asyncio.wait_for(
            asyncio.gather(self.engine._open(self.sig), self.engine._open(sig)),
            1,
        )
        self.assertEqual(self.client.place_order.await_count, 2)
        self.assertEqual(self.count(), 2)

    async def test_client_does_not_fallback_to_another_submission(self):
        self.client._post = AsyncMock(return_value={})
        self.client._position_exists = AsyncMock(return_value=False)
        async with ValidExecutionTestContext(self.client, self.engine.instruments):
            await KuCoinClient.place_order(
                self.client, 'TESTUSDT', 'Buy', 1., single_submission=True
            )
        self.client._post.assert_awaited_once()
        self.assertTrue(self.client._post.call_args.kwargs['single_attempt'])
        self.client._position_exists.assert_not_awaited()

    async def test_single_attempt_transport_timeout_no_resend(self):
        class LostResponse:
            async def __aenter__(self):
                raise TimeoutError('offline lost response')

            async def __aexit__(self, *args):
                pass

        self.client._ensure_session = AsyncMock()
        self.client._throttle = AsyncMock()
        self.client._auth_headers = Mock(return_value={})
        self.client._recover_ambiguous_order = AsyncMock(return_value=None)
        session = Mock()
        session.post.return_value = LostResponse()
        self.client._session = session
        self.client._execution_ownership = object()
        # READY_FOR_NEW_ENTRIES is enforced at the transport boundary (F-002).
        ready = ValidExecutionTestContext(self.client, self.engine.instruments).engine
        ready._execution_ownership_valid = True
        try:
            # Isolates lost-response handling with a synthetic body; the F-003
            # transport barrier is covered by tests/test_risk_budget_sizing.py.
            with patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()), \
                    patch("bot.risk_budget.assert_transport_dispatch"):
                result = await self.client._post(
                    '/api/v1/orders', {'clientOid': 'offline'}, single_attempt=True
                )
            session.post.assert_called_once()
            self.assertTrue(result['_ambiguous'])
        finally:
            self.client._session = None


if __name__ == '__main__':
    unittest.main()
