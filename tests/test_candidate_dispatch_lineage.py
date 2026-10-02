import inspect
import unittest

from bot.engine import TradingEngine


class CandidateDispatchLineageTests(unittest.TestCase):
    def test_candidate_identity_orders_dispatch_pipeline(self):
        source = inspect.getsource(TradingEngine._open)

        candidate = source.index("ensure_candidate_id(sig)")
        idem = source.index('_idem = f"candidate:{_candidate_id}"')
        client_oid = source.index("build_client_oid")
        bind = source.index("bind_managed_order(_managed, sig)")
        durable = source.index('"before_dispatch"')
        dispatch = source.index("self.client.place_order(")

        self.assertLess(candidate, idem)
        self.assertLess(idem, client_oid)
        self.assertLess(client_oid, bind)
        self.assertLess(bind, durable)
        self.assertLess(durable, dispatch)

    def test_minute_based_entry_idempotency_is_removed(self):
        source = inspect.getsource(TradingEngine._open)
        self.assertNotIn(
            'f"{sig.symbol}_{side}_{qty}_{int(time.time()//60)}"',
            source,
        )
        self.assertIn('_idem = f"candidate:{_candidate_id}"', source)


if __name__ == "__main__":
    unittest.main()
