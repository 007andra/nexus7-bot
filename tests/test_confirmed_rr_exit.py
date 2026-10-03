import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.confirmed_rr_exit import check


class ConfirmedRRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = {}
        async def load(key, **kwargs):
            return self.store.get(key)
        async def save(key, value, **kwargs):
            self.store[key] = value
            return True
        for name, fn in [('load_key_value', load), ('save_key_value', save)]:
            p = patch('bot.confirmed_rr_exit.db.' + name, AsyncMock(side_effect=fn))
            p.start()
            self.addCleanup(p.stop)
        # Q-01: a Position always carries its immutable initial stop.
        self.position = SimpleNamespace(entry=100, sl=99, initial_sl=99, current_price=103,
            qty=1, direction='LONG', _forensic_lineage={'order_id': 'opening-1'})
        self.client = SimpleNamespace(build_client_oid=Mock(return_value='unique-1'),
            place_order=AsyncMock(return_value={'orderId': 'exit-1'}),
            get_order_by_client_oid=AsyncMock(return_value={}),
            wait_for_fill=AsyncMock(return_value={'filled': False}),
            get_positions=AsyncMock(return_value=[]))
        self.engine = SimpleNamespace(positions={'ETHUSDT': self.position},
            client=self.client, instruments={}, _sync_positions=AsyncMock())

    async def test_ack_is_not_a_fill_and_repeated_cycle_never_resubmits(self):
        await check(self.engine)
        await check(self.engine)
        self.client.place_order.assert_awaited_once()
        self.engine._sync_positions.assert_not_awaited()
        self.assertIs(self.engine.positions['ETHUSDT'], self.position)
        self.assertTrue(self.client.place_order.call_args.kwargs['single_submission'])
        self.assertTrue(self.client.place_order.call_args.kwargs['reduce_only'])

    async def test_ambiguous_submission_survives_restart_and_recovers_by_oid(self):
        self.client.place_order.side_effect = TimeoutError
        await check(self.engine)
        restarted = SimpleNamespace(**vars(self.engine))
        self.client.get_order_by_client_oid.return_value = {'orderId': 'exit-1'}
        self.client.wait_for_fill.return_value = {'filled': True}
        await check(restarted)
        self.client.place_order.assert_awaited_once()
        self.client.get_order_by_client_oid.assert_awaited_once_with('unique-1')
        self.engine._sync_positions.assert_awaited_once()

    async def test_fill_with_residual_or_unconfirmed_position_is_not_close(self):
        self.client.wait_for_fill.return_value = {'filled': True}
        for result in (None, [{'symbol': 'ETHUSDT', 'size': .1}],
                       [{'symbol': 'ETHUSDT', 'size': float('nan')}],
                       [{'symbol': 'ETHUSDT'}]):
            self.client.get_positions.return_value = result
            await check(self.engine)
        self.engine._sync_positions.assert_not_awaited()
        self.client.place_order.assert_awaited_once()

    async def test_persistence_failure_prevents_submission(self):
        with patch('bot.confirmed_rr_exit.db.save_key_value', AsyncMock(return_value=False)):
            await check(self.engine)
        self.client.place_order.assert_not_awaited()

    async def test_missing_lineage_and_invalid_direction_cannot_submit(self):
        self.position._forensic_lineage = {}
        await check(self.engine)
        self.position._forensic_lineage = {'order_id': 'opening-1'}
        self.position.direction = 'invalid'
        await check(self.engine)
        self.client.place_order.assert_not_awaited()

    async def test_below_threshold_cannot_submit(self):
        self.position.current_price = 100.5
        await check(self.engine)
        self.client.place_order.assert_not_awaited()

    async def test_filled_and_flat_uses_existing_accounting_owner(self):
        self.client.wait_for_fill.return_value = {'filled': True}
        await check(self.engine)
        self.engine._sync_positions.assert_awaited_once()
        self.assertIs(self.engine.positions['ETHUSDT'], self.position)
