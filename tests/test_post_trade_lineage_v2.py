import time
import unittest
from types import SimpleNamespace

from bot.post_trade_forensics import (
    _decision_snapshot,
    _lineage,
    _opening_order_for_position,
)


class PostTradeLineageV2Tests(unittest.IsolatedAsyncioTestCase):
    def test_final_nexus_object_normalized_without_mutation(self):
        decision = SimpleNamespace(
            execution_allowed=True, setup_quality=84.5, confidence=0.91
        )
        snapshot = _decision_snapshot(decision)
        self.assertEqual(snapshot['decision'], 'APPROVE')
        self.assertIs(snapshot['execution_allowed'], True)
        self.assertEqual(snapshot['setup_quality'], 84.5)
        self.assertEqual(snapshot['confidence'], 0.91)
        self.assertTrue(decision.execution_allowed)

    def test_dict_snapshot_normalizes_direction_to_authorization_outcome(self):
        snapshot = _decision_snapshot({
            'decision': 'LONG',
            'execution_allowed': True,
            'setup_quality': 78.56,
            'confidence': 59.57,
        })
        self.assertEqual(snapshot['decision'], 'APPROVE')
        self.assertIs(snapshot['execution_allowed'], True)

    def test_lineage_contains_durable_opening_order_identity(self):
        now = time.time()
        sig = SimpleNamespace(
            symbol='LTCUSDT', direction='LONG', entry=54.37, score=73,
            regime='RANGE', entry_type='PULLBACK'
        )
        order = {
            'order_id': '488650607045902336',
            'client_oid': 'bgx7-example',
            'created_at': now,
        }
        value = _lineage(sig, {'decision': 'APPROVE'}, order)
        self.assertEqual(value['version'], 2)
        self.assertEqual(value['order_id'], '488650607045902336')
        self.assertEqual(value['client_oid'], 'bgx7-example')
        self.assertEqual(value['nexus'], 'APPROVE')
        self.assertGreater(value['captured_at_ms'], 0)

    def test_opening_order_selection_rejects_stale_same_symbol_trade(self):
        now = time.time()
        rows = [
            {'symbol': 'LTCUSDT', 'state': 'FILLED', 'order_id': 'old',
             'client_oid': 'bgx7-old', 'created_at': now - 500},
            {'symbol': 'LTCUSDT', 'state': 'FILLED', 'order_id': 'new',
             'client_oid': 'bgx7-new', 'created_at': now + 0.01},
        ]
        engine = SimpleNamespace(orders=SimpleNamespace(snapshot=lambda: rows))
        found = _opening_order_for_position(engine, 'LTCUSDT', now)
        self.assertEqual(found['order_id'], 'new')
