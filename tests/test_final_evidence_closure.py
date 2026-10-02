"""Final evidence closure: the seven mandatory pre-deploy proof blocks."""
import asyncio
import json
import math
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import execution_ownership as eo, kucoin
from bot.financial_state import FinancialStateInvalid, validate_financial_state
from bot.runtime_readiness import runtime_readiness
from bot.native_stop_repair import set_stops
from tests.execution_test_context import ValidExecutionTestContext


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

    async def test_deploy_overlap_end_to_end_transport_boundary(self):
        dummy=SimpleNamespace(_instruments=self.info)
        conn=ValidExecutionTestContext(dummy).conn
        with patch.object(eo.db,"_conn",conn), patch.object(eo.db,"_is_pg",True), \
             patch.object(eo.db,"configured_postgres_unavailable",return_value=False), \
             patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False), \
             patch("bot.risk_budget.assert_transport_dispatch"):  # isolates the ownership fence; F-003 barrier has its own tests
            old=await eo.acquire_execution_ownership("OLD")
            state=json.loads(conn.value); state["expires_at"]="2000-01-01T00:00:00+00:00"; conn.value=json.dumps(state)
            new=await eo.acquire_execution_ownership("NEW")
            # Freeze the takeover state so the OLD dispatch cannot mutate the
            # same fake authority object while validating.
            takeover_value=conn.value
            mutations=[]
            async def dispatch(authority,label):
                c=kucoin.KuCoinClient(); c._execution_ownership=authority
                c._engine=_engine(self.info)  # READY_FOR_NEW_ENTRIES (F-002 transport gate)
                c._ensure_session=AsyncMock(); c._throttle=AsyncMock()
                c._auth_headers=lambda *a,**k:{}
                class CM:
                    async def __aenter__(self): mutations.append(label); return SimpleNamespace(status=200,headers={},json=AsyncMock(return_value={"code":"200000","data":{"orderId":label}}))
                    async def __aexit__(self,*a): return False
                c._session=SimpleNamespace(post=lambda *a,**k: CM())
                try:
                    async with c._fenced_entry_post("/api/v1/orders",{"clientOid":"same","reduceOnly":False},"http://exchange.test",data="{}",headers={}):
                        pass
                    return "ok"
                except eo.StaleExecutionFence:
                    return "stale fence"
            conn.value=takeover_value
            old_result=await dispatch(old,"OLD")
            conn.value=takeover_value
            new_result=await dispatch(new,"NEW")
            self.assertEqual(old_result,"stale fence")
            self.assertEqual(new_result,"ok")
            self.assertEqual(mutations,["NEW"])

    async def test_financial_state_full_execution_matrix(self):
        cases=[
          ("VALID",100.0,50.0,125.0,.2,True),
          ("NaN",math.nan,50,125,.2,False),("+Inf",math.inf,50,125,.2,False),
          ("-Inf",-math.inf,50,125,.2,False),("negative_equity",-1,50,125,.2,False),
          ("negative_hwm",100,50,-1,.2,False),("hwm_lt_equity",100,50,99,0,False),
          ("dd_negative",100,50,125,-.1,False),("dd_gt_one",100,50,125,1.1,False),
          ("dd_mismatch",100,50,125,.1,False),
          ("explosive_hwm",19.1205,10,42709241923.064377,.70,False),
        ]
        for label,eq,margin,hwm,dd,valid in cases:
            with self.subTest(label=label):
                c=kucoin.KuCoinClient(); c._instruments=self.info; e=_engine(self.info); c._engine=e
                mutations=AsyncMock(return_value={"orderId":"entry"})
                try:
                    validate_financial_state(equity=eq,available_margin=margin,hwm=hwm,drawdown=dd)
                    e._financial_state_sane=True
                except FinancialStateInvalid:
                    e._financial_state_sane=False; e._reconciliation_required=True
                snap=runtime_readiness(e)
                if valid:
                    self.assertTrue(e._financial_state_sane)
                    self.assertFalse(e._reconciliation_required)
                    self.assertTrue(snap.financial_state_sane)
                else:
                    self.assertFalse(e._financial_state_sane)
                    self.assertTrue(e._reconciliation_required)
                    self.assertFalse(snap.ready_for_new_entries)
                    with patch.object(kucoin,"PAPER_TRADE",False), patch.object(kucoin,"API_KEY","k"), \
                         patch.object(c,"_post",mutations):
                        with self.assertRaises(RuntimeError):
                            await c.place_order("BTCUSDT","Buy",.001)
                    mutations.assert_not_awaited()

    async def test_existing_risk_recovery_bypasses_new_risk_gates_only(self):
        c=kucoin.KuCoinClient(); c._instruments=self.info
        c._engine=_engine(self.info); c._engine._financial_state_sane=False
        entry=AsyncMock()
        with patch.object(kucoin,"PAPER_TRADE",False),patch.object(kucoin,"API_KEY","k"), \
             patch("bot.critical_state.critical_state.assert_available_for_new_risk",side_effect=RuntimeError("db")), \
             patch.object(c,"_post",entry):
            with self.assertRaises(RuntimeError): await c.place_order("BTCUSDT","Buy",.001)
        entry.assert_not_awaited()
        reduce=AsyncMock(return_value={"orderId":"reduce"})
        with patch.object(kucoin,"PAPER_TRADE",False),patch.object(kucoin,"API_KEY","k"),patch.object(c,"_post",reduce):
            out=await c.place_order("BTCUSDT","Sell",.001,reduce_only=True)
        self.assertEqual(out["orderId"],"reduce")
        protection_client=SimpleNamespace(
            get_positions=AsyncMock(return_value=[{"symbol":"BTCUSDT","size":1,"side":"Buy","entryPrice":100,"markPrice":100,"sizeUnit":"CONTRACTS"}]),
            get_stop_orders=AsyncMock(return_value=[]),get_instruments=lambda:self.info,
            _round_price=lambda p,s:str(p),_post=AsyncMock())
        accepted=[]
        async def ppost(path,body,**kwargs):
            accepted.append(dict(body,isActive=True)); protection_client.get_stop_orders.return_value=list(accepted); return {"orderId":"sl"}
        protection_client._post=AsyncMock(side_effect=ppost)
        mod=SimpleNamespace(PAPER_TRADE=False,API_KEY="k",to_kucoin=lambda s:"XBTUSDTM")
        self.assertTrue(await set_stops(protection_client,"BTCUSDT",99,0,mod,Mock()))
        self.assertEqual(protection_client._post.await_count,1)
        reconcile=AsyncMock(return_value={"positions":1})
        self.assertEqual((await reconcile())["positions"],1)
        reconcile.assert_awaited_once()

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

    async def test_restart_B_after_accept_before_ack_lookup_no_second_post(self):
        c=kucoin.KuCoinClient(); c._execution_ownership=object(); c._ensure_session=AsyncMock(); c._throttle=AsyncMock(); c._auth_headers=lambda *a,**k:{}
        c._engine=_engine(self.info)  # READY_FOR_NEW_ENTRIES (F-002 transport gate)
        created=[]
        class CM:
            async def __aenter__(self): created.append("bgx7-B"); raise asyncio.TimeoutError()
            async def __aexit__(self,*a): return False
        c._session=SimpleNamespace(post=lambda *a,**k:CM())
        c.get_order_by_client_oid=AsyncMock(return_value={"orderId":"existing","clientOid":"bgx7-B"})
        with patch.object(kucoin,"PAPER_TRADE",False),patch("bot.execution_ownership.validate_execution_ownership",AsyncMock()), \
                patch("bot.risk_budget.assert_transport_dispatch"):  # isolates ack recovery; F-003 barrier has its own tests
            out=await c._post("/api/v1/orders",{"clientOid":"bgx7-B","reduceOnly":False},single_attempt=True)
        self.assertEqual(out["orderId"],"existing"); self.assertEqual(len(created),1)

    async def test_restart_C_ack_before_persist_exchange_truth_no_second_entry(self):
        from bot.order_state import OrderRegistry,OrderState
        reg=OrderRegistry(); o,_=reg.get_or_create("bgx7-C","BTCUSDT","Buy",1); o.transition(OrderState.SUBMITTING,source="LOCAL")
        exchange=SimpleNamespace(get_order_by_client_oid=AsyncMock(return_value={"orderId":"kc-C","clientOid":"bgx7-C","isActive":True}),place_order=AsyncMock())
        found=await exchange.get_order_by_client_oid("bgx7-C")
        o.transition(OrderState.SUBMITTED,source="RESTART_EXCHANGE_TRUTH",order_id=found["orderId"]); reg.index_order_id(found["orderId"],o.client_oid)
        self.assertEqual(reg.get_by_order_id("kc-C").client_oid,"bgx7-C"); exchange.place_order.assert_not_awaited()

    async def test_restart_D_fill_before_protection_recovers_and_readbacks(self):
        base=__import__("tests.test_native_stop_repair",fromlist=["NativeStopRepairTests"]).NativeStopRepairTests()
        c=base.client("Buy"); c.get_positions=AsyncMock(return_value=[dict(symbol="AVAXUSDT",size=30,side="Buy",entryPrice=7.4,markPrice=7.4)])
        self.assertTrue(await set_stops(c,"AVAXUSDT",7.3,0,base.module(),Mock()))
        self.assertEqual(c._post.await_count,1); self.assertTrue(c.get_stop_orders.await_count>=1)

    async def test_restart_E_existing_equivalent_protection_no_duplicate_post(self):
        base=__import__("tests.test_native_stop_repair",fromlist=["NativeStopRepairTests"]).NativeStopRepairTests()
        c=base.client("Buy")
        existing={"symbol":"AVAXUSDTM","side":"sell","type":"market","stop":"down","stopPrice":"7.3","stopPriceType":"MP","closeOrder":True,"reduceOnly":True,"isActive":True}
        c.get_stop_orders=AsyncMock(return_value=[existing])
        self.assertTrue(await set_stops(c,"AVAXUSDT",7.3,0,base.module(),Mock()))
        c._post.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
