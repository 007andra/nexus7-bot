import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.initial_reconciliation import finalize_initial_reconciliation
from bot.runtime_readiness import runtime_readiness


class _Orders:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def snapshot(self):
        return list(self._rows)


class _Client:
    def __init__(self, positions=None, active=None):
        self.positions = list(positions or [])
        self.active = list(active or [])

    async def get_positions(self):
        return list(self.positions)

    async def _get(self, path, params=None, auth=False):
        assert path == "/api/v1/orders"
        assert params == {"status": "active"}
        assert auth is True
        return {"items": list(self.active)}


class _NativeOrdersClient(_Client):
    def __init__(self, positions=None, active=None):
        super().__init__(positions=positions, active=active)
        self.native_calls = 0

    async def get_open_orders(self):
        self.native_calls += 1
        return list(self.active)

    async def _get(self, *args, **kwargs):
        raise AssertionError("legacy KuCoin active-order endpoint must not be used")


class InitialReconciliationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_state_ready_is_not_reconciliation_complete(self):
        engine = SimpleNamespace(
            instruments={"BTCUSDT": {}},
            _durable_state_ok=True,
            _financial_state_sane=True,
            _execution_ownership_valid=True,
            connected=True,
            _market_data_ready=True,
            _protection_system_ready=True,
        )
        self.assertFalse(runtime_readiness(engine).initial_reconciliation_complete)

    async def test_zero_position_zero_order_reconciliation_can_complete(self):
        engine = SimpleNamespace(
            connected=True,
            paper_trade=False,
            client=_Client(),
            orders=_Orders(),
            positions={},
            _recovered_position_symbols=set(),
            _external_position_symbols=set(),
            _durable_state_ok=True,
        )
        result = await finalize_initial_reconciliation(
            engine, orders_reconciled=True
        )
        self.assertTrue(result)
        self.assertTrue(all(engine._initial_reconciliation_evidence.values()))

    async def test_unresolved_durable_orders_fail_closed(self):
        engine = SimpleNamespace(
            connected=True,
            paper_trade=False,
            client=_Client(),
            orders=_Orders(),
            positions={},
            _recovered_position_symbols=set(),
            _external_position_symbols=set(),
            _durable_state_ok=True,
        )
        result = await finalize_initial_reconciliation(
            engine, orders_reconciled=False
        )
        self.assertFalse(result)
        self.assertFalse(engine._initial_reconciliation_complete)

    async def test_paper_reconciliation_never_requires_private_active_orders(self):
        client = _Client()

        async def _private_read_must_not_run(*args, **kwargs):
            raise AssertionError("private exchange active-order read executed in PAPER")

        client._get = AsyncMock(side_effect=_private_read_must_not_run)
        engine = SimpleNamespace(
            connected=True,
            paper_trade=True,
            client=client,
            orders=_Orders(),
            positions={},
            _durable_state_ok=True,
        )
        result = await finalize_initial_reconciliation(
            engine, orders_reconciled=True
        )
        self.assertTrue(result)
        client._get.assert_not_awaited()
        self.assertTrue(all(engine._initial_reconciliation_evidence.values()))

    async def test_native_open_orders_path_is_preferred_over_legacy_endpoint(self):
        client = _NativeOrdersClient(active=[])
        engine = SimpleNamespace(
            connected=True,
            paper_trade=False,
            client=client,
            orders=_Orders(),
            positions={},
            _recovered_position_symbols=set(),
            _external_position_symbols=set(),
            _durable_state_ok=True,
        )
        result = await finalize_initial_reconciliation(
            engine, orders_reconciled=True
        )
        self.assertTrue(result)
        self.assertEqual(client.native_calls, 1)
        self.assertTrue(all(engine._initial_reconciliation_evidence.values()))

    async def test_bgx_active_order_without_durable_match_fails_closed(self):
        engine = SimpleNamespace(
            connected=True,
            paper_trade=False,
            client=_Client(
                active=[{"orderId": "123", "clientOid": "bgx7-orphan"}]
            ),
            orders=_Orders(),
            positions={},
            _recovered_position_symbols=set(),
            _external_position_symbols=set(),
            _durable_state_ok=True,
        )
        result = await finalize_initial_reconciliation(
            engine, orders_reconciled=True
        )
        self.assertFalse(result)

if __name__ == "__main__":
    unittest.main()
