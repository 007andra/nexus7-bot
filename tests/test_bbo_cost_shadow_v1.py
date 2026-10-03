"""Binance BBO cost shadow v1: feed, cache, cost model, runtime wiring.

Every test here also guards the authority contract: shadow_only=true,
decision_effect=NONE, execution_effect=NONE, live_authority_unchanged=true.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("EXCHANGE", "binance")

from bot import bbo_cost_shadow_runtime as rt  # noqa: E402
from bot import bbo_cost_shadow_v1 as model  # noqa: E402
from bot import binance_bbo_feed as feed  # noqa: E402
from bot import execution_cost  # noqa: E402

SYMS = ("SOLUSDT", "BNBUSDT", "AVAXUSDT")
NOW_MS = 1_791_053_988_000
NOW_NS = 5_000_000_000_000


class _Log:
    def __init__(self):
        self.rows = []

    def __getattr__(self, level):
        def emit(msg, *args, **_kw):
            self.rows.append((level, msg % args if args else str(msg)))
        return emit


def _frame(symbol="SOLUSDT", b="119.88", a="119.89", B="50", A="40", u=10, T=NOW_MS - 20, E=None,
           wrap=True):
    data = {"e": "bookTicker", "u": u, "s": symbol, "b": b, "B": B, "a": a, "A": A, "T": T,
            "E": E if E is not None else T}
    return {"stream": f"{symbol.lower()}@bookTicker", "data": data} if wrap else data


def _cache(gen_frames=True):
    cache = feed.BBOCache(SYMS)
    gen = cache.begin_generation()
    return cache, gen


def _ingest(cache, gen, frame, wall=NOW_MS, mono=NOW_NS):
    return cache.ingest(frame, generation=gen, received_wall_ms=wall, received_mono_ns=mono)


def _parse(frame, wall=NOW_MS):
    return feed.parse_book_ticker(frame, generation=1, received_wall_ms=wall,
                                  received_mono_ns=NOW_NS, known_symbols=frozenset(SYMS))


def _reason(frame, wall=NOW_MS):
    try:
        _parse(frame, wall)
    except feed.BBOParseError as exc:
        return exc.reason
    return "OK"


# ── 1-8: parse / validation / freshness ────────────────────────────────────

def test_01_valid_book_ticker_parses_all_fields():
    q = _parse(_frame(u=77))
    assert (q.symbol, q.bid, q.ask, q.bid_qty, q.ask_qty, q.update_id) == ("SOLUSDT", 119.88, 119.89, 50.0, 40.0, 77)
    assert q.exchange_ms == NOW_MS - 20 and q.received_wall_ms == NOW_MS and q.received_mono_ns == NOW_NS
    assert _parse(_frame(wrap=False)).symbol == "SOLUSDT"   # raw (non-combined) payload


def test_02_03_non_positive_prices_rejected():
    assert _reason(_frame(b="0")) == "INVALID_BOOK"
    assert _reason(_frame(b="-1")) == "INVALID_BOOK"
    assert _reason(_frame(a="0")) == "INVALID_BOOK"
    assert _reason(_frame(B="0")) == "INVALID_BOOK"


def test_04_crossed_book_rejected():
    assert _reason(_frame(b="119.90", a="119.89")) == "INVALID_BOOK"


def test_05_nan_and_inf_rejected():
    for bad in ("NaN", "nan", "inf", "-inf", "Infinity"):
        assert _reason(_frame(b=bad)) == "INVALID_BOOK", bad
        assert _reason(_frame(A=bad)) == "INVALID_BOOK", bad


def test_06_missing_book():
    cache, _ = _cache()
    view = cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS)
    assert (view.valid, view.reason, view.quote) == (False, "MISSING_BOOK", None)


def test_07_stale_book_connection_silent_and_symbol_age():
    cache, gen = _cache()
    assert _ingest(cache, gen, _frame()) == (True, "OK")
    later = NOW_NS + (feed.BBO_CONN_SILENCE_MS + 1) * 1_000_000
    view = cache.snapshot("SOLUSDT", now_mono_ns=later)
    assert (view.valid, view.reason, view.detail) == (False, "STALE", "CONN_SILENT")
    # connection alive (other symbol frames) but this symbol untouched > cap
    much_later = NOW_NS + (feed.BBO_SYMBOL_MAX_AGE_MS + 1) * 1_000_000
    cache.note_frame(gen, much_later)
    view = cache.snapshot("SOLUSDT", now_mono_ns=much_later)
    assert (view.valid, view.reason, view.detail) == (False, "STALE", "SYMBOL_AGE")
    assert _reason(_frame(T=NOW_MS - feed.BBO_MAX_EXCHANGE_LAG_MS - 1)) == "STALE_ON_ARRIVAL"


def test_08_fresh_book_is_valid():
    cache, gen = _cache()
    _ingest(cache, gen, _frame())
    view = cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS + 500_000_000)
    assert view.valid and view.reason == "OK" and view.age_ms == 500
    # quiet symbol stays current while the connection is alive (bookTicker pushes on change)
    cache.note_frame(gen, NOW_NS + 10_000_000_000)
    assert cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS + 10_000_000_000).valid


# ── 9-11: reconnect / generation / duplicates ──────────────────────────────

def test_09_reconnect_invalidates_previous_cache_until_new_frame():
    cache, g1 = _cache()
    _ingest(cache, g1, _frame(u=5))
    cache.end_generation(g1)
    assert cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS).detail == "DISCONNECTED"
    g2 = cache.begin_generation()
    view = cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS)
    assert (view.valid, view.reason, view.detail) == (False, "STALE", "RECONNECT")
    # a new connection may restart update ids: lower u in the NEW generation is accepted
    assert _ingest(cache, g2, _frame(u=1)) == (True, "OK")
    assert cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS).valid


def test_10_old_generation_never_overwrites_new_generation():
    cache, g1 = _cache()
    g2 = cache.begin_generation()
    assert _ingest(cache, g2, _frame(b="100", a="100.1", u=3)) == (True, "OK")
    # race: a late frame still being processed from the old socket
    assert _ingest(cache, g1, _frame(b="50", a="50.1", u=999)) == (False, "OLD_GENERATION")
    q = cache.snapshot("SOLUSDT", now_mono_ns=NOW_NS).quote
    assert (q.bid, q.generation) == (100.0, g2)


def test_11_duplicate_subscription_and_duplicate_frames_have_no_double_effect():
    url = feed.stream_url("wss://fstream.binance.com", ["SOLUSDT", "solusdt", "BNBUSDT"])
    assert url == "wss://fstream.binance.com/public/stream?streams=bnbusdt@bookTicker/solusdt@bookTicker"
    cache, gen = _cache()
    assert _ingest(cache, gen, _frame(u=7)) == (True, "OK")
    assert _ingest(cache, gen, _frame(u=7)) == (False, "NON_MONOTONIC")
    assert _ingest(cache, gen, _frame(u=6)) == (False, "NON_MONOTONIC")
    assert cache.stats["accepted"] == 1

    async def scenario():
        client = SimpleNamespace()
        log = _Log()

        def never_connect(*a, **k):
            raise ConnectionError("offline test")
        first = rt.start_feed(client, SYMS, log, connect=never_connect, ws_base="wss://x")
        second = rt.start_feed(client, SYMS, log, connect=never_connect, ws_base="wss://x")
        assert first is not None and second is None
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())


# ── 12-18: cost model ──────────────────────────────────────────────────────

def test_12_13_14_mid_spread_half_spread():
    m = model.book_metrics(119.88, 119.90)
    assert abs(m.mid - 119.89) < 1e-12
    assert abs(m.spread_abs - 0.02) < 1e-9
    assert abs(m.spread_bps - 0.02 / 119.89 * 1e4) < 1e-9
    assert abs(m.half_spread_bps - m.spread_bps / 2) < 1e-12


def test_15_fee_spread_impact_are_separate():
    book = model.book_metrics(11.113, 11.114)
    c = model.cost_breakdown(symbol="AVAXUSDT", taker_fee=0.0005, entry_slippage=0.001,
                             exit_slippage=0.001, book=book)
    assert (c.static_fee_bps, c.static_slippage_bps, c.static_total_cost_bps) == (10.0, 20.0, 30.0)
    assert c.live_fee_bps == 10.0
    assert c.estimated_impact_bps is None and c.impact_model == "UNPROVEN"
    assert abs(c.live_spread_only_cost_bps - (10.0 + book.spread_bps)) < 1e-9
    assert abs(c.live_plus_static_impact_cost_bps - (c.live_spread_only_cost_bps + 4.0)) < 1e-9
    assert c.sizing_slippage_floor_bps == 10.0
    no_book = model.cost_breakdown(symbol="AVAXUSDT", taker_fee=0.0005, entry_slippage=0.001,
                                   exit_slippage=0.001, book=None)
    assert no_book.live_spread_bps is None and no_book.live_plus_static_impact_cost_bps is None
    assert model.top_book_coverage("LONG", 2.0, 50.0, 40.0) == 20.0   # descriptive only


def test_16_17_rr_net_and_ev_match_nexus_expected_value():
    from bot import nexus_ai
    for e, s, t, p, cost_bps in ((119.89, 119.419031, 120.831938, 0.356, 20.0),
                                  (11.114, 10.994095, 11.353811, 0.39, 30.0),
                                  (100.0, 99.0, 103.0, 0.45, 14.0)):
        g = model.nexus_ev_rr_gate(entry=e, stop=s, target=t, win_prob=p,
                                   round_trip_cost_bps=cost_bps, rr_floor=1.6)
        ref = nexus_ai.expected_value(p, e, s, t, taker_fee=0.0, slippage=cost_bps / 2 / 1e4)
        assert abs(g.rr_net - ref["rr_net"]) < 1e-3
        assert abs(g.ev_pct - ref["ev_pct"]) < 1e-3
        assert g.allowed == (ref["valid"] and ref["rr_net"] >= 1.6)
    assert model.nexus_ev_rr_gate(entry=1, stop=0.9, target=1.2, win_prob=None,
                                  round_trip_cost_bps=10, rr_floor=1.6) is None


def _sig(symbol="AVAXUSDT", entry=11.114, sl=10.994095, tp=11.353811, slip=0.001):
    sig = SimpleNamespace(symbol=symbol, direction="LONG", entry=entry, sl=sl, tp=tp,
                          entry_type="PULLBACK", _bgx_setup_id=f"{symbol}:LONG:PULLBACK:1990054",
                          score=60)
    snap = execution_cost.ExecutionCostSnapshot(
        snapshot_id="cost-test", candidate_id=sig._bgx_setup_id, exchange="binance", symbol=symbol,
        entry_reference=entry, taker_fee=0.0005, maker_fee=0.0002, entry_slippage=slip,
        exit_slippage=slip, spread_bps=None, fee_source="binance_commission_rate",
        slippage_source="static_symbol_fallback", observed_at=time.time())
    execution_cost.attach_snapshot(sig, snap)
    return sig


def _decision(confidence=20.35, rr=1.347):
    d = SimpleNamespace(decision="WAIT", execution_allowed=False, confidence=0.0)
    d._bgx_score_snapshot = {"fusion_confidence": confidence, "rr_net": rr, "ev_pct": -0.11}
    return d


def _valid_view(symbol="AVAXUSDT", bid=11.1135, ask=11.1145, qty=500.0):
    q = feed.BBOQuote(symbol, bid, ask, qty, qty, 9, NOW_MS, NOW_MS, NOW_NS, 1)
    return feed.BBOView(symbol, True, "OK", "OK", q, 12)


def test_18_would_change_decision_counterfactual_reproduces_production_avax():
    rec = rt.build_record(_sig(), _decision(), _valid_view(), floor=1.6)
    assert rec.status == "OK" and rec.static_parity is True        # matches logged rr_net 1.347
    assert abs(rec.rr_net_static - 1.347) < 0.002
    assert rec.static_allowed is False and rec.shadow_allowed is True
    assert rec.would_change_decision is True
    line = model.format_record(rec)
    for token in ("[COST_SHADOW_BBO]", "candidate_id=AVAXUSDT:LONG:PULLBACK:1990054",
                  "bbo_valid=true", "would_change_decision=true", "decision_scope=EV_RR_GATE",
                  "estimated_impact_bps=NA impact_model=UNPROVEN",
                  "shadow_only=true decision_effect=NONE execution_effect=NONE"):
        assert token in line, token
    sol = rt.build_record(_sig("SOLUSDT", 119.89, 119.419031, 120.831938, 0.0005),
                          _decision(43.4, 1.001), _valid_view("SOLUSDT", 119.88, 119.89), floor=1.6)
    assert sol.would_change_decision is False   # cheaper cost alone does not reach the floor


# ── 19-30: authority / isolation / fail-open ───────────────────────────────

class _Engine:
    _bbo_cost_shadow_installed = False

    def __init__(self, decision):
        self.client = object()
        self._decision = decision

    async def _nexus_validate(self, sig):
        return self._decision


class _Client:
    async def start_websocket(self, symbols, intervals=None):
        return "original-start"

    async def _handle_ws_message(self, message):
        return None


def _install(decision=None, flags=None):
    engine_cls = type("E", (_Engine,), {"_bbo_cost_shadow_installed": False})
    client_cls = type("C", (_Client,), {})
    log = _Log()
    with patch.dict(os.environ, flags or {}, clear=False):
        rt.install(engine_cls, client_cls, log)
    return engine_cls, client_cls, log


def test_19_champion_decision_object_is_returned_unchanged():
    decision = _decision()
    before = copy.deepcopy(vars(decision))
    engine_cls, _, _ = _install()
    sig = _sig()
    sig_before = {k: v for k, v in vars(sig).items()}

    async def run():
        with patch.object(rt, "persist", side_effect=RuntimeError("db down")):
            out = await engine_cls(decision)._nexus_validate(sig)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return out
    assert asyncio.run(run()) is decision
    assert vars(decision) == before
    assert {k: v for k, v in vars(sig).items()} == sig_before


def test_20_21_22_no_private_exchange_order_or_position_access():
    class _Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"exchange client touched: {name}")
    engine_cls, _, _ = _install()

    async def run():
        engine = engine_cls(_decision())
        engine.client = _Forbidden()          # cache_for() falls back: no registered feed
        with patch.object(rt, "persist", return_value=True):
            out = await engine._nexus_validate(_sig())
            await asyncio.sleep(0.01)
        return out
    assert asyncio.run(run()).decision == "WAIT"
    for mod in (rt, model, feed):
        public = {n.lower() for n, v in vars(mod).items()
                  if not n.startswith("_") and getattr(v, "__module__", None) == mod.__name__}
        assert not any(v in n for n in public for v in
                       ("order", "position", "cancel", "dispatch", "leverage", "listen"))


def test_23_24_25_no_sizing_config_risk_recovery_leverage_change():
    from bot.config import cfg
    before = {k: getattr(cfg, k) for k in dir(cfg) if k.isupper()}
    env = dict(os.environ)
    boom = AssertionError("trading math must not be called by the shadow")
    with patch("bot.sizing_decomposition.decompose", side_effect=boom), \
         patch("bot.professional_risk.stop_risk_size", side_effect=boom), \
         patch("bot.drawdown_recovery.recovery_size_multiplier", side_effect=boom):
        rec = rt.build_record(_sig(), _decision(), _valid_view(), floor=rt.rr_floor())
    assert rec.status == "OK"
    assert {k: getattr(cfg, k) for k in dir(cfg) if k.isupper()} == before
    assert dict(os.environ) == env


def test_26_persistence_failure_never_affects_champion():
    class _DB:
        async def _exec(self, *a, **k):
            raise RuntimeError("database unavailable")
    log = _Log()
    rec = asyncio.run(rt.observe(_sig(), _decision(), _valid_view(), log, rt.EmitDeduper(), db=_DB()))
    assert rec is not None and rec.status == "OK"
    assert any("SHADOW_DATA_UNAVAILABLE stage=emit" in t for _, t in log.rows)


def test_26b_persistence_is_append_only():
    calls = []

    class _DB:
        async def _exec(self, sql, params=()):
            calls.append((sql, params))
            return True
    asyncio.run(rt.observe(_sig(), _decision(), _valid_view(), _Log(), rt.EmitDeduper(), db=_DB()))
    sqls = " ".join(s for s, _ in calls).upper()
    assert "ON CONFLICT(OBSERVATION_ID) DO NOTHING" in sqls
    assert "UPDATE " not in sqls and "DELETE" not in sqls and "DROP" not in sqls
    row = calls[-1][1]
    assert json.loads(row[-2]) == rt.AUTHORITY and len(row) == 23


def test_27_28_missing_or_stale_bbo_never_blocks_champion():
    stale = feed.BBOView("AVAXUSDT", False, "STALE", "CONN_SILENT", None, 9000)
    for view in (None, stale):
        rec = rt.build_record(_sig(), _decision(), view, floor=1.6)
        assert rec.bbo_valid is False and rec.shadow_allowed is None
        assert rec.would_change_decision is None and rec.status.startswith("BBO_")
        assert rec.static_allowed is False                     # static side still observed
    engine_cls, _, _ = _install()

    async def run():
        with patch.object(rt, "persist", return_value=True):
            return await engine_cls(_decision())._nexus_validate(_sig())
    assert asyncio.run(run()).decision == "WAIT"


def test_29_install_idempotent_and_flag_off():
    engine_cls, client_cls, log = _install()
    v1, s1 = engine_cls._nexus_validate, client_cls.start_websocket
    rt.install(engine_cls, client_cls, log)
    assert engine_cls._nexus_validate is v1 and client_cls.start_websocket is s1
    off_engine, off_client, _ = _install(flags={"NEXUS_BBO_COST_SHADOW": "false"})
    assert off_engine._nexus_validate is _Engine._nexus_validate
    assert off_client.start_websocket is _Client.start_websocket


def test_30_runtime_contract_surfaces_untouched_and_start_websocket_identity():
    engine_cls, client_cls, _ = _install()
    assert client_cls._handle_ws_message is _Client._handle_ws_message   # protected handler untouched

    async def run():
        with patch.object(rt, "start_feed", side_effect=RuntimeError("feed broken")):
            return await client_cls().start_websocket(["SOLUSDT"])
    assert asyncio.run(run()) == "original-start"                          # fail-open, original result
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "bot" / "runtime_contract_guard.py").read_text()
    assert "bbo" not in text.lower()
    # Only the shadow modules read the BBO cache.
    root = pathlib.Path(__file__).resolve().parents[1] / "bot"
    readers = [p.name for p in root.glob("*.py")
               if ("cache_for(" in p.read_text(encoding="utf-8") or "BBOCache" in p.read_text(encoding="utf-8"))]
    assert sorted(readers) == ["bbo_cost_shadow_runtime.py", "binance_bbo_feed.py"]


# ── feed loop end-to-end (fake socket) ─────────────────────────────────────

class _FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._frames.pop(0)


def test_feed_loop_reconnect_generations_end_to_end():
    now = int(time.time() * 1000)
    conns = [
        [json.dumps(_frame(u=5, T=now)), "not-json", json.dumps(_frame(b="0", T=now))],
        [json.dumps(_frame(b="120", a="120.01", u=1, T=now))],
    ]
    cache = feed.BBOCache(SYMS)

    def connect(url, **kw):
        assert url.startswith("wss://x/public/stream?streams=")
        return _FakeWS(conns.pop(0) if conns else [])

    async def run():
        task = asyncio.create_task(feed.run_feed(cache, "wss://x/public/stream?streams=solusdt@bookTicker",
                                                 _Log(), connect=connect))
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    assert cache.generation >= 2
    assert cache.stats["rejected_JSON"] == 1 and cache.stats["rejected_INVALID_BOOK"] == 1
    q = cache._quotes["SOLUSDT"]
    assert (q.bid, q.update_id) == (120.0, 1) and q.generation >= 2


# ── adversarial / mutation ─────────────────────────────────────────────────

def test_adversarial_inputs_are_rejected_or_contained():
    assert _reason(_frame(b="119.89", a="119.88")) == "INVALID_BOOK"            # swapped bid/ask
    assert _reason(_frame(T=NOW_MS + feed.BBO_MAX_FUTURE_SKEW_MS + 5)) == "INVALID_TIMESTAMP"
    assert _reason(_frame(T=NOW_MS - 86_400_000)) == "STALE_ON_ARRIVAL"         # very old
    assert _reason(_frame(T=0)) == "INVALID_TIMESTAMP"
    assert _reason(_frame(b="1e12", a="1e12")) == "INVALID_BOOK"                # absurd price
    assert _reason(_frame(B="1e20")) == "INVALID_BOOK"                          # absurd qty
    assert _reason(_frame(b="abc")) == "INVALID_BOOK"                           # non-numeric string
    assert _reason(_frame(b=True)) == "INVALID_BOOK"                            # bool, not a number
    assert _reason(_frame(b=[1])) == "INVALID_BOOK"
    assert _reason(_frame(symbol="SHIBUSDT")) == "UNKNOWN_SYMBOL"
    assert _reason(_frame(u=0)) == "INVALID_UPDATE_ID"
    assert _reason({"data": {"e": "24hrTicker", "s": "SOLUSDT"}}) == "NOT_BOOK_TICKER"
    cache, gen = _cache()
    assert _ingest(cache, gen + 7, _frame()) == (False, "OLD_GENERATION")       # wrong generation
    assert _ingest(cache, gen, None)[0] is False                                 # garbage contained
    assert _ingest(cache, gen, {"data": 5})[0] is False
    # numeric types accepted as well as strings
    assert _parse(_frame(b=119.88, a=119.89, B=1, A=2)).bid == 119.88
    for bad in (float("nan"), float("inf"), -1.0, 0.0):
        try:
            model.book_metrics(bad, 1.0)
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        model.book_metrics(2.0, 1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("crossed book accepted")


def test_dedup_emits_once_per_candidate_unless_material_spread_move():
    d = rt.EmitDeduper()
    r1 = rt.build_record(_sig(), _decision(), _valid_view(bid=11.1135, ask=11.1145), floor=1.6)
    r2 = rt.build_record(_sig(), _decision(), _valid_view(bid=11.1136, ask=11.1146), floor=1.6)
    r3 = rt.build_record(_sig(), _decision(), _valid_view(bid=11.1100, ask=11.1180), floor=1.6)
    r4 = rt.build_record(_sig(), _decision(), None, floor=1.6)
    assert [d.should_emit(r) for r in (r1, r2, r3, r4)] == [True, False, True, True]
    assert math.isfinite(r3.spread_bps) and r3.spread_bps - r1.spread_bps >= rt.SPREAD_REEMIT_BPS


# ── replay (research only) ─────────────────────────────────────────────────

def test_replay_reproduces_population_and_never_claims_observed_bbo():
    import pathlib
    from bot import bbo_cost_shadow_replay as replay
    root = pathlib.Path(__file__).resolve().parents[1]
    pop = json.loads((root / "docs/research/bbo_cost_shadow_v1/replay_population.json").read_text())
    res = replay.evaluate(pop, 1.6)
    assert res["total"] == 86 and res["parity_ok"] == 86          # logged rr_net reproduced
    assert res["observed"] == {"valid": 0, "invalid": 0, "stale": 0, "missing": 86}
    one_tick = res["scenarios"]["COUNTERFACTUAL_1_TICK"]
    assert one_tick["gate_changes_unique"] == {"AVAXUSDT:LONG:PULLBACK:1990054"}
    assert one_tick["final_sizing_cf"] == 0                        # gate change != executable
    text = replay.render(pop, res, 1.6)
    assert "UNPROVEN" in text and "| 86 | 0 | 0 | 0 | 86 | NA | NA | NA |" in text


def test_research_and_replay_modules_not_imported_by_trading_files():
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "bot"
    allowed = {"bbo_cost_shadow_runtime.py", "bbo_cost_shadow_replay.py", "runtime_bootstrap.py",
               "bbo_cost_shadow_v1.py", "binance_bbo_feed.py"}
    for path in root.glob("*.py"):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            assert not any(n.split(".")[-1] in {"bbo_cost_shadow_runtime", "bbo_cost_shadow_v1",
                                                "binance_bbo_feed", "bbo_cost_shadow_replay"}
                           for n in names), path.name
    boot = (root / "runtime_bootstrap.py").read_text(encoding="utf-8")
    assert "bbo_cost_shadow_replay" not in boot
