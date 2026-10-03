import unittest
from unittest.mock import patch

from bot.candidate_trace import (
    attach_decision,
    bind_managed_order,
    build_candidate_id,
    ensure_candidate_id,
)
from bot.order_state import ManagedOrder
from bot.execution_cost import ExecutionCostSnapshot


class _Signal:
    symbol = "BTCUSDT"
    direction = "LONG"
    entry = 100.0
    sl = 98.0
    tp = 104.0
    entry_type = "PULLBACK"
    regime = "TRENDING_UP"
    score = 73
    _bgx_formation_bucket = 123456


class _Decision:
    pass


class CandidateTraceTests(unittest.TestCase):
    def test_same_setup_has_same_candidate_id(self):
        a = _Signal()
        b = _Signal()
        self.assertEqual(build_candidate_id(a), build_candidate_id(b))
        self.assertTrue(build_candidate_id(a).startswith("nx7-"))

    def test_different_formation_bucket_changes_candidate_id(self):
        a = _Signal()
        b = _Signal()
        b._bgx_formation_bucket = a._bgx_formation_bucket + 1
        self.assertNotEqual(build_candidate_id(a), build_candidate_id(b))

    def test_different_geometry_changes_candidate_id(self):
        a = _Signal()
        b = _Signal()
        b.tp = 105.0
        self.assertNotEqual(build_candidate_id(a), build_candidate_id(b))

    def test_existing_candidate_id_is_preserved(self):
        sig = _Signal()
        sig._bgx_setup_id = "existing-candidate"
        self.assertEqual(ensure_candidate_id(sig), "existing-candidate")

    def test_decision_and_managed_order_share_candidate_id(self):
        sig = _Signal()
        decision = _Decision()
        cid = attach_decision(decision, sig)
        order = ManagedOrder("bgx7-test", "BTCUSDT", "Buy", 0.01)
        self.assertEqual(bind_managed_order(order, sig), cid)
        self.assertEqual(decision._bgx_candidate_id, cid)
        self.assertEqual(order.candidate_id, cid)

    def test_cost_decision_and_durable_order_share_one_candidate(self):
        sig = _Signal()
        cid = ensure_candidate_id(sig)
        snapshot = ExecutionCostSnapshot(
            snapshot_id="cost-test",
            candidate_id=cid,
            exchange="binance",
            symbol=sig.symbol,
            entry_reference=sig.entry,
            taker_fee=0.0005,
            maker_fee=0.0002,
            entry_slippage=0.0001,
            exit_slippage=0.0001,
            spread_bps=1.0,
            fee_source="test",
            slippage_source="test",
            observed_at=1.0,
        )
        sig._bgx_cost_snapshot = snapshot

        decision = _Decision()
        decision._bgx_nexus_cost_context = type(
            "Ctx", (), {"snapshot": snapshot}
        )()
        self.assertEqual(attach_decision(decision, sig), cid)

        order = ManagedOrder("bgx7-test", "BTCUSDT", "Buy", 0.01)
        self.assertEqual(bind_managed_order(order, sig), cid)
        self.assertEqual(snapshot.candidate_id, decision._bgx_candidate_id)
        self.assertEqual(decision._bgx_candidate_id, order.candidate_id)

    def test_managed_order_candidate_survives_restart_roundtrip(self):
        sig = _Signal()
        order = ManagedOrder("bgx7-test", "BTCUSDT", "Buy", 0.01)
        cid = bind_managed_order(order, sig)
        restored = ManagedOrder.from_record(order.to_record())
        self.assertEqual(restored.candidate_id, cid)

    def test_legacy_managed_order_without_candidate_remains_compatible(self):
        order = ManagedOrder("bgx7-test", "BTCUSDT", "Buy", 0.01)
        record = order.to_record()
        record.pop("candidate_id", None)
        restored = ManagedOrder.from_record(record)
        self.assertIsNone(restored.candidate_id)

    def test_candidate_conflict_fails_closed(self):
        sig = _Signal()
        order = ManagedOrder("bgx7-test", "BTCUSDT", "Buy", 0.01)
        order.candidate_id = "different"
        with self.assertRaises(ValueError):
            bind_managed_order(order, sig)


if __name__ == "__main__":
    unittest.main()
