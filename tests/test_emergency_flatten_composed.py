"""F-001 on the composed production LIVE-pilot runtime + real HTTP endpoint.

Final ``nexus_runtime_engine.TradingEngine`` (with KuCoinPositionUnitAdapter),
final ``KuCoinClient.place_order`` chain (fence, pilot counter, native TP/SL,
transport readiness/pause gates) and ``main_hardened.app`` over a stateful fake
HTTP session. REST base is 127.0.0.1:1; no credentials; offline runner blocks
non-loopback sockets. Only DB-backed leases are mocked.
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
from urllib.parse import parse_qs, urlparse  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import sitecustomize  # noqa: E402,F401
from bot import kucoin  # noqa: E402
from bot import live_execution_fence  # noqa: E402
from bot.kucoin_position_units import KuCoinPositionUnitAdapter  # noqa: E402
from bot.nexus_runtime_engine import TradingEngine  # noqa: E402

SYMS = {"BTCUSDT": ("XBTUSDTM", 0.001), "ETHUSDT": ("ETHUSDTM", 0.01), "SOLUSDT": ("SOLUSDTM", 0.1)}


def _info(kc, mult):
    return {"minQty": 1, "lotSize": 1, "qtyStep": 1, "tickSize": 0.001,
            "multiplier": mult, "minNotional": 0, "kucoinSymbol": kc}


class _Resp:
    def __init__(self, payload, fail=None):
        self.status, self.headers, self._payload, self._fail = 200, {}, payload, fail

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        if self._fail:
            raise self._fail
        return self

    async def __aexit__(self, *exc):
        return False


class _Exchange:
    """Stateful fake: positions in native contracts keyed by KuCoin symbol."""
    closed = False

    def __init__(self, positions, modes=None):
        self.positions = dict(positions)
        self.modes = modes or {}
        self.calls, self.orders = [], {}

    def get(self, url, **kw):
        self.calls.append(("GET", url, None))
        path = urlparse(url).path
        if path.endswith("/api/v1/positions"):
            rows = [{"symbol": s, "currentQty": c, "avgEntryPrice": 100.0, "markPrice": 100.0}
                    for s, c in self.positions.items() if c]
            return _Resp({"code": "200000", "data": rows})
        if path.endswith("/byClientOid"):
            oid = parse_qs(urlparse(url).query).get("clientOid", [""])[0]
            order = self.orders.get(oid)
            return _Resp({"code": "200000", "data": dict(order) if order else {}})
        if "/api/v1/orders/" in path:
            return _Resp({"code": "200000", "data": {"isActive": False, "filledSize": "1",
                                                     "cancelExist": False}})
        if "getMarginMode" in path:
            return _Resp({"code": "200000", "data": {"marginMode": "CROSS"}})
        return _Resp({"code": "200000", "data": []})

    def post(self, url, **kw):
        body = json.loads(kw.get("data") or "{}")
        self.calls.append(("POST", url, body))
        sym = body.get("symbol")
        mode = self.modes.get(sym, "ok")
        if body.get("reduceOnly") is True and mode in ("ok", "timeout"):
            cur = self.positions.get(sym, 0)
            size = int(body["size"])
            same_dir_close = (body["side"] == "sell") == (cur > 0)
            if cur and same_dir_close and size <= abs(cur):
                self.positions[sym] = cur - size if cur > 0 else cur + size
            oid = body["clientOid"]
            self.orders[oid] = {"id": f"kc-{len(self.orders) + 1}", "clientOid": oid}
            if mode == "timeout":
                return _Resp(None, fail=asyncio.TimeoutError("response lost after accept"))
            return _Resp({"code": "200000", "data": {"orderId": self.orders[oid]["id"]}})
        if mode == "error":
            return _Resp({"code": "300009", "msg": "rejected"})
        return _Resp({"code": "200000", "data": {"orderId": "kc-open"}})

    def posts(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


class _Base:
    def _setup_stack(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(kucoin, "API_KEY", "test-key"))
        self.stack.enter_context(patch("asyncio.sleep", AsyncMock()))
        self.stack.enter_context(patch("bot.live_execution_fence.acquire", AsyncMock(return_value=True)))
        for std, (kc, _) in SYMS.items():
            self.stack.enter_context(patch.dict(kucoin.SYMBOL_MAP, {std: kc}))
            self.stack.enter_context(patch.dict(kucoin.SYMBOL_MAP_REV, {kc: std}))

    def _engine(self, positions, modes=None):
        raw = kucoin.KuCoinClient()
        raw._session = _Exchange(positions, modes)
        raw._instruments = {s: _info(kc, m) for s, (kc, m) in SYMS.items()}
        engine = TradingEngine(raw)
        engine.instruments = dict(raw._instruments)
        engine._running = True
        engine.active = True
        return raw, engine


class ComposedFlattenTests(_Base, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._setup_stack()

    def tearDown(self):
        self.stack.close()

    async def test_n_production_engine_flattens_long_short_multi(self):
        raw, engine = self._engine({"XBTUSDTM": 5, "ETHUSDTM": -3, "SOLUSDTM": 7})
        self.assertIsInstance(engine.client, KuCoinPositionUnitAdapter)
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FLAT", out)
        posts = {p["symbol"]: p for p in raw._session.posts()}
        self.assertEqual({s: (p["side"], p["size"], p["reduceOnly"]) for s, p in posts.items()},
                         {"XBTUSDTM": ("sell", "5", True), "ETHUSDTM": ("buy", "3", True),
                          "SOLUSDTM": ("sell", "7", True)})
        self.assertTrue(engine._running)

    async def test_e_new_entry_readiness_false_does_not_block_reduce_only(self):
        raw, engine = self._engine({"XBTUSDTM": 5})
        engine._initial_reconciliation_complete = False
        engine._protection_system_ready = False
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FLAT")

    async def test_f_no_opening_order_while_flatten_runs(self):
        raw, engine = self._engine({"XBTUSDTM": 5})
        # Make the new-entry readiness authority fully satisfied so only the
        # emergency pause can stop the concurrent entry.
        engine._initial_reconciliation_complete = True
        engine._protection_system_ready = True
        engine._execution_ownership_valid = True
        engine._execution_ownership_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        engine._financial_state_sane = True
        engine._durable_state_ok = True
        engine.connected = True
        engine.viable_symbols = ["BTCUSDT"]
        raw._execution_ownership = object()
        with patch("bot.execution_ownership.validate_execution_ownership", AsyncMock()), \
                patch("bot.pilot_submission_counter.reserve_submission", AsyncMock(return_value=(True, 1))):
            flatten = asyncio.create_task(engine.close_all_positions())
            await asyncio.sleep(0)
            entry = asyncio.create_task(raw.place_order(
                "ETHUSDT", "Buy", 0.03, sl=90.0, tp=120.0, idem_key="concurrent-entry",
                single_submission=True))
            out, _ = await asyncio.gather(flatten, entry, return_exceptions=True)
        self.assertEqual(out["status"], "FLAT")
        opening = [p for p in raw._session.posts() if p.get("reduceOnly") is not True]
        self.assertEqual(opening, [], "no new-risk POST reached the exchange")

    async def test_g_partial_failure_reported(self):
        raw, engine = self._engine({"XBTUSDTM": 5, "ETHUSDTM": -3, "SOLUSDTM": 7},
                                   modes={"ETHUSDTM": "error"})
        out = await engine.close_all_positions()
        self.assertEqual(out["status"], "PARTIAL_FAILURE")
        self.assertEqual(out["remaining_positions"], ["ETHUSDT"])
        self.assertTrue(engine._running)

    async def test_h_timeout_after_submit_is_recovered_without_second_order(self):
        raw, engine = self._engine({"XBTUSDTM": 5}, modes={"XBTUSDTM": "timeout"})
        out = await engine.close_all_positions()
        posts = raw._session.posts()
        self.assertEqual(len(posts), 1, "no blind resubmission")
        self.assertEqual(out["status"], "FLAT")
        self.assertTrue(out["results"][0]["order_id"])

    async def test_i_j_repeated_and_concurrent_calls(self):
        raw, engine = self._engine({"XBTUSDTM": 5, "ETHUSDTM": -3})
        outs = await asyncio.gather(*(engine.close_all_positions() for _ in range(3)))
        outs += [await engine.close_all_positions() for _ in range(3)]
        self.assertEqual(len(raw._session.posts()), 2)
        self.assertEqual([o["status"] for o in outs].count("FLAT"), 1)
        self.assertEqual(raw._session.positions, {"XBTUSDTM": 0, "ETHUSDTM": 0})

    async def test_fence_other_owner_blocks_without_bypass(self):
        raw, engine = self._engine({"XBTUSDTM": 5})
        with patch("bot.live_execution_fence.acquire", AsyncMock(return_value=False)), \
                patch.object(live_execution_fence, "_ownership_failure", "another_owner"):
            out = await engine.close_all_positions()
        self.assertEqual(out["status"], "FAILED")
        self.assertEqual(raw._session.posts(), [])
        self.assertEqual(out["remaining_positions"], ["BTCUSDT"])


class HttpCloseAllTests(_Base, unittest.TestCase):
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

    def _call(self, headers):
        return self.client.post("/api/close-all", headers=headers)

    def test_o_http_endpoint_flattens_and_reports(self):
        raw, engine = self._engine({"XBTUSDTM": 5, "ETHUSDTM": -3})
        self.main.app.state.engine = engine
        ok = {"Authorization": "Bearer test-bearer", "X-Confirm-Action": "CLOSE_ALL_POSITIONS"}
        r = self._call(ok)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["status"], body["positions_found"], body["positions_closed"]),
                         ("FLAT", 2, 2))
        self.assertTrue(engine._running, "endpoint must not stop management")
        r2 = self._call(ok)
        self.assertEqual((r2.status_code, r2.json()["status"]), (200, "ALREADY_FLAT"))
        self.assertEqual(len(raw._session.posts()), 2)

    def test_o_http_partial_failure_is_not_success(self):
        raw, engine = self._engine({"XBTUSDTM": 5, "ETHUSDTM": -3}, modes={"ETHUSDTM": "error"})
        self.main.app.state.engine = engine
        r = self._call({"Authorization": "Bearer test-bearer", "X-Confirm-Action": "CLOSE_ALL_POSITIONS"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "PARTIAL_FAILURE")
        self.assertEqual(r.json()["remaining_positions"], ["ETHUSDT"])

    def test_o_http_guards_unchanged(self):
        raw, engine = self._engine({"XBTUSDTM": 5})
        self.main.app.state.engine = engine
        self.assertEqual(self._call({"Authorization": "Bearer test-bearer"}).status_code, 428)
        self.assertEqual(self._call({"X-Confirm-Action": "CLOSE_ALL_POSITIONS"}).status_code, 401)
        self.assertEqual(raw._session.posts(), [])
        self.assertFalse(getattr(engine, "entries_paused", False))


if __name__ == "__main__":
    unittest.main()
