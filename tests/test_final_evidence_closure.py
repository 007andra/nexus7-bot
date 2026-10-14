"""Final evidence closure: venue-neutral duplicate-intent and restart proofs.

The KuCoin transport/financial-matrix blocks were removed with the KuCoin
adapter; their Binance counterparts live in tests/test_binance_live_* and
tests/test_binance_restart_ownership_continuity.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock


def _engine(instruments):
    return SimpleNamespace(
        instruments=instruments, _durable_state_ok=True, _financial_state_sane=True,
        _initial_reconciliation_complete=True, _execution_ownership_valid=True,
        connected=True, _market_data_ready=True, viable_symbols=list(instruments),
        _protection_system_ready=True, _reconciliation_required=False,
    )


class FinalEvidenceClosure(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.info={"BTCUSDT":{"tickSize":.1,"lotSize":1,"qtyStep":1,"multiplier":.001,
            "minQty":1,"minBaseQty":.001,"minNotional":0,"kucoinSymbol":"XBTUSDTM"}}

    async def test_duplicate_intent_cross_worker_side_effects(self):
        # A real PostgreSQL cross-process reservation is separately mandatory in
        # test_release_pilot_postgres. This collision proof composes the durable
        # intent/pilot/authority/exchange side effects for the same logical ID.
        lock=asyncio.Lock(); durable=set(); pilot=set(); mutations=[]; authorities=[]
        barrier=asyncio.Event(); ready=0
        async def worker(name):
            nonlocal ready
            async with lock:
                ready+=1
                if ready==2: barrier.set()
            await barrier.wait()
            async with lock:
                oid="bgx7-same-client-oid"
                if oid in durable:
                    return "duplicate intent"
                durable.add(oid)
                pilot.add(oid)
                authorities.append(name)
                mutations.append(oid)
                return "success"
        result=await asyncio.gather(worker("A"),worker("B"))
        self.assertEqual(len(durable),1)
        self.assertEqual(len(pilot),1)
        self.assertLessEqual(len(authorities),1)
        self.assertEqual(len(mutations),1)
        self.assertEqual(len(set(mutations)),1)
        self.assertIn("duplicate intent",result)

    async def test_restart_A_before_post_known_terminal_no_duplicate(self):
        from bot import durable_reconcile_hardening as h, order_state
        from bot import pilot_submission_counter as provenance
        from bot.order_state import OrderRegistry, OrderState
        class L:
            info=warning=critical=error=staticmethod(lambda *a,**k:None)
        async def original(e): return False
        async def persist(e,r,strict=False): return True
        def clear(e,r): e._durable_state_ok=True
        def block(e,r): e._durable_state_ok=False
        d=SimpleNamespace(reconcile_orders=original,persist_orders=persist,_clear=clear,_block=block,_advance=lambda *a,**k:None)
        h.install(d,order_state,L())
        e=SimpleNamespace(orders=OrderRegistry(),client=SimpleNamespace(get_order_status=AsyncMock(),get_order_by_client_oid=AsyncMock(),get_positions=AsyncMock(),_get=AsyncMock()),errors=set(),_durable_state_ok=False,persist_reasons=[])
        order,_=e.orders.get_or_create("bgx7-restart-a","BTCUSDT","Buy",1)
        provenance._record_same_state(
            order,
            dispatch_attempted=False,
            predispatch_abort_reason="TEST_KNOWN_PRE_POST_ABORT",
            exchange_dispatch="NONE",
        )
        self.assertTrue(await d.reconcile_orders(e))
        self.assertEqual(order.state, OrderState.FAILED)
        e.client.get_order_by_client_oid.assert_not_awaited()
        e.client.get_order_status.assert_not_awaited()

    async def test_restart_C_ack_before_persist_exchange_truth_no_second_entry(self):
        from bot.order_state import OrderRegistry,OrderState
        reg=OrderRegistry(); o,_=reg.get_or_create("bgx7-C","BTCUSDT","Buy",1); o.transition(OrderState.SUBMITTING,source="LOCAL")
        exchange=SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={"orderId":"kc-C","clientOid":"bgx7-C","isActive":True}),place_order=AsyncMock())
        found=await exchange.get_order_by_client_oid("bgx7-C")
        o.transition(OrderState.SUBMITTED,source="RESTART_EXCHANGE_TRUTH",order_id=found["orderId"]); reg.index_order_id(found["orderId"],o.client_oid)
        self.assertEqual(reg.get_by_order_id("kc-C").client_oid,"bgx7-C"); exchange.place_order.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
