"""NOVO-01 on the composed production LIVE-pilot runtime (offline).

Final ``nexus_runtime_engine.TradingEngine`` (KuCoinPositionUnitAdapter), the
real F-014 normalizer behind ``KuCoinClient.get_positions``, the final
``place_order`` chain, ``main_hardened.app`` /api/close-all, and a stateful
fake KuCoin session whose ETH row can be malformed. REST base 127.0.0.1:1, no
credentials; only DB key/value and the distributed lease are faked.
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
import json  # noqa: E402
import unittest  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from urllib.parse import urlparse  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import database as db  # noqa: E402
from bot import engine as core  # noqa: E402
from bot import kucoin  # noqa: E402
from bot import trade_lifecycle as tl  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402
from bot.protection_readiness import refresh_protection_readiness  # noqa: E402
from bot.strategy import Signal  # noqa: E402
from tests.stale_protection_fake import Resp, StopExchange  # noqa: E402

SYMS = {"BTCUSDT": ("XBTUSDTM", 0.001), "ETHUSDT": ("ETHUSDTM", 0.01), "SOLUSDT": ("SOLUSDTM", 0.1)}
OK_HEADERS = {"Authorization": "Bearer test-bearer", "X-Confirm-Action": "CLOSE_ALL_POSITIONS"}


def _info(kc, mult):
    return {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.1,
            "multiplier": mult, "minNotional": 0, "kucoinSymbol": kc}


class _PartialExchange(StopExchange):
    """KuCoin rows; ``bad`` symbols get a malformed avgEntryPrice."""

    def __init__(self, positions, stops=(), bad=(), unidentified=0):
        super().__init__(positions, stops)
        self.bad, self.unidentified = set(bad), unidentified

    def get(self, url, **kw):
        if urlparse(url).path.endswith("/api/v1/positions"):
            self.calls.append(("GET", url, None))
            rows = []
            for kc, c in self.positions.items():
                if c:
                    rows.append({"symbol": kc, "currentQty": c, "markPrice": 100.0,
                                 "avgEntryPrice": "abc" if kc in self.bad else 100.0})
            rows += [{"symbol": None, "currentQty": 1}] * self.unidentified
            return Resp({"code": "200000", "data": rows})
        return super().get(url, **kw)

    def post(self, url, **kw):
        body = json.loads(kw.get("data") or "{}")
        if "stop" in body:                          # native stop order: register it
            self.calls.append(("POST", url, body))
            sid = f"st-{len(self.stops) + 1}"
            self.stops.append(dict(body, id=sid, status="active"))
            return Resp({"code": "200000", "data": {"orderId": sid}})
        return super().post(url, **kw)

    def orders_for(self, kc):
        return [p for p in self.posts() if p.get("symbol") == kc]

    def opening(self):
        return [p for p in self.posts() if p.get("reduceOnly") is not True]


class _Base:
    def _setup_stack(self):
        self.stack = ExitStack()
        self.store = {}

        async def load(key, strict=False):
            return self.store.get(key)

        async def save(key, value, strict=False):
            self.store[key] = value
            return True
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(db, "load_key_value", AsyncMock(side_effect=load)))
        self.stack.enter_context(patch.object(db, "save_key_value", AsyncMock(side_effect=save)))
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

    async def _local(self, engine, sym, direction, qty, oid):
        entry, sl, tp = (100.0, 98.0, 104.0) if direction == "LONG" else (100.0, 102.0, 96.0)
        pos = core.Position(Signal(sym, direction, entry, sl, tp, .8, "t", 80), qty)
        pos._forensic_lineage = {"order_id": oid, "client_oid": f"bgx7-{oid}", "version": 2}
        engine.positions[sym] = pos
        await tl.open_trade(pos, qty)
        return pos


class ComposedPartialSnapshotTests(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._setup_stack()

    def tearDown(self):
        self.stack.close()

    async def test_r_main_btc_sol_closed_eth_unknown_then_flat(self):
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3, "SOLUSDTM": 7}, bad={"ETHUSDTM"})
        raw, engine = self._engine(ex)
        out = await engine.close_all_positions()
        posts = {p["symbol"]: (p["side"], p["size"], p["reduceOnly"]) for p in ex.posts()}
        self.assertEqual(posts, {"XBTUSDTM": ("sell", "5", True), "SOLUSDTM": ("sell", "7", True)})
        self.assertEqual(ex.orders_for("ETHUSDTM"), [], "no order for UNKNOWN ETH")
        self.assertEqual(ex.positions, {"XBTUSDTM": 0, "ETHUSDTM": -3, "SOLUSDTM": 0})
        self.assertEqual({r["symbol"]: r["result"] for r in out["results"]},
                         {"BTCUSDT": "CLOSED", "SOLUSDT": "CLOSED"})
        self.assertEqual((out["status"], out["unknown_symbols"], out["snapshot_state"]),
                         ("UNKNOWN_REMAINS", ["ETHUSDT"], "PARTIAL_INVALID"))
        self.assertTrue(engine._running, "engine keeps managing")
        self.assertTrue(engine.entries_paused and raw.entries_paused)

        ex.bad.clear()                               # ETH row becomes valid
        out = await engine.close_all_positions()
        self.assertEqual([(p["side"], p["size"]) for p in ex.orders_for("ETHUSDTM")], [("buy", "3")])
        self.assertEqual(out["status"], "FLAT")
        self.assertEqual(len(ex.posts()), 3, "no duplicate BTC/SOL orders")

    async def test_c_unidentified_row_composed(self):
        ex = _PartialExchange({"XBTUSDTM": 5}, unidentified=1)
        raw, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual([p["symbol"] for p in ex.posts()], ["XBTUSDTM"])
        self.assertEqual(ex.positions["XBTUSDTM"], 0)
        self.assertEqual((out["status"], out["remaining_positions"]), ("UNKNOWN_REMAINS", ["UNIDENTIFIED"]))

    async def test_d_e_all_malformed_or_read_failure_zero_orders(self):
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, bad={"XBTUSDTM", "ETHUSDTM"})
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual((ex.posts(), out["status"]), ([], "UNKNOWN_REMAINS"))
        ex = _PartialExchange({"XBTUSDTM": 5})
        ex.get = lambda url, **kw: Resp({"code": "429000", "msg": "too many requests"})
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual((ex.posts(), out["status"], out["remaining_positions"]),
                         ([], "FAILED", ["UNKNOWN"]))

    async def test_k_l_concurrency_readiness_false_and_entries_blocked_reduce_only_reaches_http(self):
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, bad={"ETHUSDTM"})
        raw, engine = self._engine(ex)
        engine._initial_reconciliation_complete = True
        engine._protection_system_ready = True
        engine._execution_ownership_valid = True
        engine._execution_ownership_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        engine._financial_state_sane = True
        engine._durable_state_ok = True
        raw._execution_ownership = object()
        with patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()), \
                patch("bot.pilot_submission_counter.reserve_submission", AsyncMock(return_value=(True, 1))):
            flatten = asyncio.create_task(engine.close_all_positions())
            await asyncio.sleep(0)
            entry = asyncio.create_task(raw.place_order(
                "SOLUSDT", "Buy", 0.3, sl=90.0, tp=120.0, idem_key="concurrent-entry",
                single_submission=True))
            out, _ = await asyncio.gather(flatten, entry, return_exceptions=True)
            self.assertEqual(out["status"], "UNKNOWN_REMAINS")
            self.assertEqual(ex.opening(), [], "no new-risk POST during flatten")
            self.assertEqual([p["reduceOnly"] for p in ex.orders_for("XBTUSDTM")], [True])

            # K: BTC closed and protected nothing left on it, ETH still UNKNOWN.
            self.assertFalse(await refresh_protection_readiness(engine))
            self.assertFalse(engine._protection_system_ready)
            # L: even if the operator resumes entries, a new entry has zero HTTP.
            engine.resume_entries()
            try:
                await raw.place_order("SOLUSDT", "Buy", 0.3, sl=90.0, tp=120.0,
                                      idem_key="after-resume", single_submission=True)
            except Exception:
                pass
        self.assertEqual(ex.opening(), [], "F-002: new entry = zero HTTP while UNKNOWN exists")

    async def test_i_j_p_local_eth_never_phantom_closed_lineage_only_for_proven_flat(self):
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, bad={"ETHUSDTM"})
        _, engine = self._engine(ex)
        await self._local(engine, "BTCUSDT", "LONG", 0.005, "open-btc")
        await self._local(engine, "ETHUSDT", "SHORT", 0.03, "open-eth")
        trades_before = len(engine.stats.trades)
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "UNKNOWN_REMAINS")
        await engine._sync_positions()
        self.assertIn("ETHUSDT", engine.positions, "I: no phantom close of UNKNOWN ETH")
        self.assertEqual((await tl.load("open-btc"))["status"], "CLOSED", "P: BTC proven flat")
        self.assertEqual((await tl.load("open-eth"))["status"], "OPEN", "P: ETH stays OPEN")
        eth_trades = [t for t in engine.stats.trades[trades_before:] if t.symbol == "ETHUSDT"]
        self.assertEqual(eth_trades, [], "J: zero accounting for UNKNOWN ETH")
        self.assertFalse(any("ETHUSDT" in k and "CLOSED" in str(v) for k, v in self.store.items()))

        ex.bad.clear()
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FLAT")
        self.assertEqual((await tl.load("open-eth"))["status"], "CLOSED")

    async def test_m_naked_guard_protects_valid_btc_despite_malformed_eth(self):
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, bad={"ETHUSDTM"})
        _, engine = self._engine(ex)
        await self._local(engine, "BTCUSDT", "LONG", 0.005, "open-btc")
        await self._local(engine, "ETHUSDT", "SHORT", 0.03, "open-eth")
        await engine._guard_naked_positions()
        btc = ex.orders_for("XBTUSDTM")
        self.assertEqual([(p["side"], p["stop"], p["stopPrice"], p["closeOrder"]) for p in btc],
                         [("sell", "down", "98", True)], "BTC protected at its local SL")
        self.assertEqual([s["symbol"] for s in ex.active_stops()], ["XBTUSDTM"])
        self.assertEqual(ex.orders_for("ETHUSDTM"), [], "UNKNOWN ETH never mutated")
        self.assertEqual(ex.opening(), [])

    async def test_o_stale_cleanup_only_for_symbol_proven_flat(self):
        stops = [
            {"id": "btc-sl", "clientOid": "", "symbol": "XBTUSDTM", "side": "sell", "stop": "down",
             "stopPrice": 95.0, "stopPriceType": "TP", "closeOrder": True, "reduceOnly": True},
            {"id": "eth-sl", "clientOid": "", "symbol": "ETHUSDTM", "side": "buy", "stop": "up",
             "stopPrice": 105.0, "stopPriceType": "TP", "closeOrder": True, "reduceOnly": True},
        ]
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, stops, bad={"ETHUSDTM"})
        _, engine = self._engine(ex)
        out = await engine.close_all_positions()
        self.assertEqual(ex.deletes(), ["btc-sl"], "only BTC (proven flat) protection retired")
        self.assertEqual([s["id"] for s in ex.active_stops()], ["eth-sl"])
        self.assertEqual(out["status"], "UNKNOWN_REMAINS")


class HttpPartialCloseAllTests(_Base, unittest.TestCase):
    def setUp(self):
        self._setup_stack()
        import main
        import main_hardened
        from starlette.testclient import TestClient
        self.main = main
        self.client = TestClient(main_hardened.app)
        self.previous = getattr(main.app.state, "engine", None)
        main._rate_counters.clear()

    def tearDown(self):
        self.main.app.state.engine = self.previous
        self.stack.close()

    def test_a_h_q_http_known_closed_unknown_remains_not_success(self):
        ex = _PartialExchange({"XBTUSDTM": 5, "ETHUSDTM": -3}, bad={"ETHUSDTM"})
        _, engine = self._engine(ex)
        self.main.app.state.engine = engine
        r = self.client.post("/api/close-all", headers=OK_HEADERS)
        self.assertEqual(r.status_code, 503, r.text)
        body = r.json()
        self.assertEqual((body["status"], body["unknown_symbols"], body["remaining_positions"]),
                         ("UNKNOWN_REMAINS", ["ETHUSDT"], ["ETHUSDT"]))
        self.assertEqual([(p["symbol"], p["side"], p["size"], p["reduceOnly"]) for p in ex.posts()],
                         [("XBTUSDTM", "sell", "5", True)])
        self.assertTrue(engine._running)
        r2 = self.client.post("/api/close-all", headers=OK_HEADERS)
        self.assertEqual((r2.status_code, r2.json()["status"]), (503, "UNKNOWN_REMAINS"))
        self.assertEqual(len(ex.posts()), 1, "Q: no duplicate BTC order")
        ex.bad.clear()
        r3 = self.client.post("/api/close-all", headers=OK_HEADERS)
        self.assertEqual((r3.status_code, r3.json()["status"]), (200, "FLAT"))
        self.assertEqual(ex.positions, {"XBTUSDTM": 0, "ETHUSDTM": 0})


if __name__ == "__main__":
    unittest.main()
