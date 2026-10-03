"""F-001A — stale protections after emergency flatten (composed LIVE runtime).

Production LIVE-pilot composition, ``nexus_runtime_engine.TradingEngine`` and
the real close/cancel paths over ``tests.stale_protection_fake.StopExchange``
(offline; REST base 127.0.0.1:1; no credentials).
"""
import os

os.environ.update({
    "PAPER_TRADE": "false",
    "LIVE_TRADING_CONFIRMED": "I_UNDERSTAND_THE_RISK",
    "REAL_TRADING_PILOT": "true",
    "PILOT_ACCOUNT_CONFIRMED": "true",
    "PILOT_RELEASE_APPROVED": "I_APPROVE_TWO_LIVE_PILOT_ORDERS",
    "VALIDATION_LOCK_RELEASE_APPROVED": "I_APPROVE_CONTROLLED_LIVE_PILOT_EXECUTION",
    "KUCOIN_REST_BASE": "http://127.0.0.1:1",
    "KUCOIN_API_KEY": "", "KUCOIN_API_SECRET": "", "KUCOIN_API_PASSPHRASE": "",
    "NEXUS_TELEGRAM": "false",
    "BOT_API_SECRET": "test-bearer",
})
os.environ.pop("EXECUTION_CAPABILITY", None)

import asyncio  # noqa: E402
import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import kucoin  # noqa: E402
from bot.conditional_stop_protection import conditional_stop_confirmed  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.protection_readiness import refresh_protection_readiness  # noqa: E402
from tests.stale_protection_fake import StopExchange, native_and_bgx_stops  # noqa: E402

SYMS = {"BTCUSDT": ("XBTUSDTM", 0.001), "ETHUSDT": ("ETHUSDTM", 0.01)}
OK_HEADERS = {"Authorization": "Bearer test-bearer", "X-Confirm-Action": "CLOSE_ALL_POSITIONS"}


def _info(kc, mult):
    return {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.1,
            "multiplier": mult, "minNotional": 0, "kucoinSymbol": kc}


class _Base:
    def _setup_stack(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        for std, (kc, _) in SYMS.items():
            self.stack.enter_context(patch.dict(kucoin.SYMBOL_MAP, {std: kc}))
            self.stack.enter_context(patch.dict(kucoin.SYMBOL_MAP_REV, {kc: std}))

    def _engine(self, exchange):
        raw = kucoin.KuCoinClient()
        raw._session = exchange
        raw._instruments = {s: _info(kc, m) for s, (kc, m) in SYMS.items()}
        engine = TradingEngine(raw)
        engine.instruments = dict(raw._instruments)
        engine._running = True
        engine.active = True
        engine.connected = True
        engine.viable_symbols = list(SYMS)
        return raw, engine

    @staticmethod
    def _b_row(side="Buy"):
        return {"symbol": "BTCUSDT", "side": side, "size": 5, "entryPrice": 100.0, "markPrice": 100.0}


class StaleProtectionTests(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._setup_stack()

    def tearDown(self):
        self.stack.close()

    async def test_a_flatten_retires_native_and_bgx_protections(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops())
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FLAT")
        self.assertEqual((out["stale_protections_found"], out["stale_protections_cancelled"],
                          out["remaining_stale_protections"]), (3, 3, []))
        self.assertEqual(ex.active_stops(), [])
        self.assertEqual(engine._stale_protection_pending, set())

    async def test_a_exchange_auto_cancel_variant_needs_no_action(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops(), auto_cancel_on_flat=True)
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual((out["status"], out["stale_protections_found"], ex.deletes()), ("FLAT", 0, []))

    async def test_b_already_flat_with_stale_protection_is_detected_and_retired(self):
        ex = StopExchange({}, native_and_bgx_stops())
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "ALREADY_FLAT")
        self.assertEqual(out["stale_protections_cancelled"], 3)
        self.assertEqual(ex.posts(), [])
        self.assertEqual(ex.active_stops(), [])

    async def test_c_cleanup_is_idempotent(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops())
        _, engine = self._engine(ex)
        outs = [await engine.close_all_positions() for _ in range(10)]
        self.assertEqual(outs[0]["status"], "FLAT")
        self.assertEqual({o["status"] for o in outs[1:]}, {"ALREADY_FLAT"})
        self.assertEqual(len(ex.deletes()), 3, "each stale order cancelled once")
        self.assertEqual(len([p for p in ex.posts() if p.get("reduceOnly")]), 1)

    async def test_d_cleanup_failure_is_reported_and_keeps_entries_paused(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops(), cancel_fail={"XBTUSDTM-A-SL"})
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FLAT_BUT_PROTECTION_CLEANUP_FAILED")
        self.assertEqual(out["remaining_stale_protections"], ["BTCUSDT:XBTUSDTM-A-SL"])
        self.assertEqual((out["stale_protections_cancelled"], out["stale_protections_failed"]), (2, 1))
        self.assertTrue(engine.entries_paused)
        self.assertEqual(engine._stale_protection_pending, {"BTCUSDT"})
        ex.cancel_fail.clear()
        again = await engine.close_all_positions()
        self.assertEqual((again["status"], again["stale_protections_cancelled"]), ("ALREADY_FLAT", 1))
        self.assertEqual(engine._stale_protection_pending, set())

    async def test_e_multi_symbol_cleanup_never_touches_open_position_protection(self):
        stops = native_and_bgx_stops("XBTUSDTM") + [
            dict(s, side="buy", stop="up" if s["stop"] == "down" else "down")
            for s in native_and_bgx_stops("ETHUSDTM")]
        ex = StopExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, stops)
        _, engine = self._engine(ex)
        with patch("bot.emergency_flatten.time.time", return_value=1_000_000.0):
            # ETH close rejected by the exchange -> ETH stays open
            original = ex.post

            def post(url, **kw):
                import json
                if json.loads(kw.get("data") or "{}").get("symbol") == "ETHUSDTM":
                    ex.calls.append(("POST", url, json.loads(kw["data"])))
                    from tests.stale_protection_fake import Resp
                    return Resp({"code": "300009", "msg": "rejected"})
                return original(url, **kw)
            ex.post = post
            out = await engine.close_all_positions()
        self.assertEqual(out["status"], "PARTIAL_FAILURE")
        self.assertEqual(out["remaining_positions"], ["ETHUSDT"])
        self.assertEqual(ex.active_stops("XBTUSDTM"), [])
        self.assertEqual(len(ex.active_stops("ETHUSDTM")), 3, "open ETH keeps its protection")
        self.assertTrue(all(d.startswith("XBTUSDTM") for d in ex.deletes()))

    async def test_f_old_protection_cannot_modify_new_position(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops())
        raw, engine = self._engine(ex)
        self.assertEqual((await engine.close_all_positions())["status"], "FLAT")
        # operator resumes; position B LONG 5 opens WITHOUT (yet) its own stop
        ex.positions["XBTUSDTM"] = 5
        protected, evidence = await conditional_stop_confirmed(raw, self._b_row())
        self.assertFalse(protected, "stale A-SL must not count as B's protection")
        ex.tick("XBTUSDTM", 95.0)    # old A-SL level
        ex.tick("XBTUSDTM", 110.0)   # old A-TP level
        self.assertEqual(ex.positions["XBTUSDTM"], 5, "A's protections cannot close B")

    async def test_g_stop_fires_while_flatten_is_closing(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops())
        ex.stop_fires_on_close.add("XBTUSDTM")
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual(ex.positions["XBTUSDTM"], 0, "never reversed")
        self.assertEqual(out["status"], "FLAT")
        self.assertEqual(ex.active_stops(), [])

    async def test_new_entry_blocked_while_cleanup_runs(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops())
        raw, engine = self._engine(ex)
        seen = []
        original = ex.delete

        def delete(url, **kw):
            seen.append(bool(raw.entries_paused))
            return original(url, **kw)
        ex.delete = delete
        await engine.close_all_positions()
        self.assertTrue(seen and all(seen))
        with self.assertRaisesRegex(ValueError, "operator pause"):
            raw._entry_safe_post("/api/v1/st-orders", {"side": "buy"}, "http://127.0.0.1:1/x")

    async def test_fence_invalid_owner_cannot_cancel(self):
        ex = StopExchange({}, native_and_bgx_stops())
        _, engine = self._engine(ex)
        engine._durable_order_lock = asyncio.Lock()      # production runtime marker
        engine._execution_ownership_valid = False
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FLAT_BUT_PROTECTION_CLEANUP_FAILED")
        self.assertEqual(ex.deletes(), [])
        self.assertEqual(len(ex.active_stops()), 3)

    async def test_h_restart_operator_flatten_rediscovers_orphans_from_exchange(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops(), cancel_fail={"XBTUSDTM-A-TP"})
        _, first = self._engine(ex)
        self.assertEqual((await first.close_all_positions())["status"],
                         "FLAT_BUT_PROTECTION_CLEANUP_FAILED")
        ex.cancel_fail.clear()
        _, restarted = self._engine(ex)                    # new process, empty memory
        out = await restarted.close_all_positions()
        self.assertEqual((out["status"], out["stale_protections_cancelled"]), ("ALREADY_FLAT", 1))
        self.assertEqual(ex.active_stops(), [])

    @unittest.expectedFailure
    async def test_h_restart_readiness_alone_does_not_detect_native_orphans(self):
        """Known gap (documented, not fixed here): after a restart without an
        operator close-all, the readiness flat sweep retires only bgx-stop-
        orders and reports ready while a native st-orders leg is still active."""
        ex = StopExchange({}, native_and_bgx_stops())
        _, engine = self._engine(ex)
        ready = await refresh_protection_readiness(engine)
        self.assertFalse(ready and ex.active_stops())


class ResumeGateHttpTests(_Base, unittest.TestCase):
    def setUp(self):
        self._setup_stack()
        import main
        import main_hardened
        from starlette.testclient import TestClient
        self.main = main
        self.client = TestClient(main_hardened.app)
        self.saved = {k: getattr(main.app.state, k, None) for k in ("engine", "ready", "blocked")}
        main._rate_counters.clear()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.main.app.state, k, v)
        self.stack.close()

    def test_i_resume_refused_while_stale_protection_pending(self):
        ex = StopExchange({"XBTUSDTM": 5}, native_and_bgx_stops(), cancel_fail={"XBTUSDTM-A-SL"})
        _, engine = self._engine(ex)
        self.main.app.state.engine = engine
        self.main.app.state.ready, self.main.app.state.blocked = True, False
        r = self.client.post("/api/close-all", headers=OK_HEADERS)
        self.assertEqual((r.status_code, r.json()["status"]), (503, "FLAT_BUT_PROTECTION_CLEANUP_FAILED"))
        resume = self.client.post("/api/resume", headers={"Authorization": "Bearer test-bearer"})
        self.assertEqual(resume.status_code, 409)
        self.assertTrue(engine.entries_paused)
        ex.cancel_fail.clear()
        r2 = self.client.post("/api/close-all", headers=OK_HEADERS)
        self.assertEqual((r2.status_code, r2.json()["status"]), (200, "ALREADY_FLAT"))
        resume2 = self.client.post("/api/resume", headers={"Authorization": "Bearer test-bearer"})
        self.assertNotEqual(resume2.status_code, 409)


if __name__ == "__main__":
    unittest.main()
