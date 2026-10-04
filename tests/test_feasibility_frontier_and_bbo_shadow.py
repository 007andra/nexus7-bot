"""Research/shadow modules: feasibility frontier and STATIC vs LIVE_BBO cost.

Neither module is registered by the runtime; these tests pin their arithmetic
to the runtime's own formulas so the research cannot drift from production.
"""
from __future__ import annotations

import os

os.environ.setdefault("EXCHANGE", "binance")

from bot import bbo_cost_shadow as shadow  # noqa: E402
from bot import feasibility_frontier as ff  # noqa: E402
from bot import nexus_ai  # noqa: E402

SOL = ff.Instrument("SOLUSDT", "0.01", "0.01", "5", "INFERRED")
ARB = ff.Instrument("ARBUSDT", "0.1", "0.1", "5", "INFERRED")
EQ, RISK = 8.7583, 0.005


def _sol(**kw):
    args = dict(price=119.89, stop_pct=0.0039283426, gross_rr=2.0, equity=EQ, risk_pct=RISK,
                cost=ff.CostModel.static_for("SOLUSDT"), win_prob=0.45)
    args.update(kw)
    return ff.evaluate(SOL, **args)


def test_frontier_reproduces_production_sol_veto():
    cell = _sol()
    assert abs(cell.rr_net - 0.988) < 0.002           # logged rr_net=0.988
    assert cell.min_order_ok is True                   # logged MIN_ORDER PASS
    assert abs(cell.min_valid_qty - 0.05) < 1e-12      # logged min_valid_qty=0.05
    assert cell.nexus_rr_ok is False and cell.all_ok is False


def test_static_costs_match_runtime_model():
    assert abs(ff.CostModel.static_for("SOLUSDT").round_trip - 0.002) < 1e-15
    assert abs(ff.CostModel.static_for("BNBUSDT").round_trip - 0.003) < 1e-15
    assert abs(ff.CostModel.static_for("AVAXUSDT").round_trip - 0.003) < 1e-15


def test_min_stop_for_rr_is_the_exact_boundary():
    floor = ff.nexus_min_rr_net()
    for r in (2.0, 2.5, 3.0, 4.0):
        s = ff.min_stop_pct_for_rr(r, 0.003, floor)
        rr = (r * s - 0.003) / (s + 0.003)
        assert abs(rr - floor) < 1e-12
    assert ff.min_stop_pct_for_rr(1.5, 0.003, floor) is None


def test_ev_boundary_matches_nexus_expected_value():
    s = ff.min_stop_pct_for_ev(3.0, 0.003, 0.45)
    above = nexus_ai.expected_value(0.45, 100.0, 100 * (1 - s * 1.01), 100 * (1 + 3 * s * 1.01),
                                    taker_fee=0.0005, slippage=0.001)
    below = nexus_ai.expected_value(0.45, 100.0, 100 * (1 - s * 0.99), 100 * (1 + 3 * s * 0.99),
                                    taker_fee=0.0005, slippage=0.001)
    assert above["valid"] is True and below["valid"] is False


def test_sizing_keeps_runtime_slippage_floor_even_for_cheap_cost():
    cheap = ff.CostModel(0.0005, 0.00002)
    assert ff.sizing_slippage_allowance(cheap) == 0.001
    cell = ff.evaluate(ARB, price=0.19869, stop_pct=0.005, gross_rr=3.5, equity=EQ,
                       risk_pct=RISK, cost=cheap, win_prob=0.45)
    expected = cell.min_valid_qty * 0.19869 * (0.005 + 0.001 + 0.001)
    assert abs(cell.risk_at_min_qty - expected) < 1e-9


def test_more_equity_never_removes_a_passing_cell():
    for r in (2.0, 3.0, 4.0):
        for s in (0.0025, 0.005, 0.01, 0.02):
            prev = False
            for eq in (EQ, 10, 15, 20, 25, 50):
                ok = ff.evaluate(ARB, price=0.19869, stop_pct=s, gross_rr=r, equity=eq,
                                 risk_pct=RISK, cost=ff.CostModel.static_for("ARBUSDT"),
                                 win_prob=0.45).all_ok
                assert ok or not prev
                prev = ok


def test_leverage_does_not_change_frontier_risk():
    a = _sol(leverage=5)
    b = _sol(leverage=50)
    assert (a.risk_at_min_qty, a.risk_budget, a.min_order_ok) == (b.risk_at_min_qty, b.risk_budget, b.min_order_ok)
    assert abs(a.margin_at_min_qty - 10 * b.margin_at_min_qty) < 1e-12


def test_min_equity_matches_decomposition():
    eq = ff.min_equity_for(SOL, price=119.89, stop_pct=0.0039283426, risk_pct=RISK,
                           cost=ff.CostModel.static_for("SOLUSDT"))
    assert _sol(equity=eq * 1.0001).min_order_ok is True
    assert _sol(equity=eq * 0.95).min_order_ok is False


def _bbo(bid=11.1135, ask=11.1145, qty=1e6, event=1000, recv=1000):
    return shadow.BBOSnapshot("AVAXUSDT", bid, ask, qty, qty, event, recv)


def test_bbo_validation_fails_closed():
    assert _bbo().validate(now_ms=1000) == (True, "OK")
    assert _bbo(bid=0).validate(now_ms=1000)[1] == "BBO_NON_POSITIVE"
    assert _bbo(bid=12, ask=11).validate(now_ms=1000)[1] == "BBO_CROSSED"
    assert _bbo(qty=0).validate(now_ms=1000)[1] == "BBO_EMPTY_SIDE"
    assert _bbo().validate(now_ms=4000)[1] == "BBO_STALE_LOCAL"
    assert _bbo(event=0, recv=5000).validate(now_ms=5000)[1] == "BBO_STALE_EXCHANGE_LAG"
    assert _bbo(bid=float("nan")).validate(now_ms=1000)[1] == "BBO_NON_NUMERIC"


def test_book_ticker_payloads_parse_ws_and_rest():
    ws = {"e": "bookTicker", "u": 1, "E": 1700, "T": 1699, "s": "AVAXUSDT",
          "b": "11.113", "B": "50", "a": "11.114", "A": "40"}
    rest = {"symbol": "AVAXUSDT", "bidPrice": "11.113", "bidQty": "50",
            "askPrice": "11.114", "askQty": "40", "time": 1699}
    for payload in (ws, rest):
        snap = shadow.BBOSnapshot.from_book_ticker(payload, received_ms=1705)
        assert (snap.bid, snap.ask, snap.ask_qty, snap.event_ms) == (11.113, 11.114, 40.0, 1699)


def test_impact_separates_spread_from_depth_walk():
    snap = _bbo(qty=1.0)                     # top-of-book ~11.11 USDT
    small = shadow.estimated_impact(snap, "LONG", 5.0)
    large = shadow.estimated_impact(snap, "LONG", 33.35)
    assert small == shadow.impact_floor("AVAXUSDT")
    assert large > small


def test_compare_reproduces_production_avax_flip_and_is_shadow_only():
    r = shadow.compare(symbol="AVAXUSDT", side="LONG", stop_frac=0.0107886450,
                       target_frac=0.0215773799, win_prob=0.30 + 0.45 * 0.2035,
                       taker_fee=0.0005, static_slippage_per_side=0.001, order_notional=11.0,
                       rr_floor=ff.nexus_min_rr_net(), bbo=_bbo(), now_ms=1000)
    assert abs(r.net_rr_static - 1.347) < 0.002
    assert r.net_rr_live > r.net_rr_static and r.would_change_decision is True
    line = r.format()
    assert "shadow_only=true decision_effect=NONE execution_effect=NONE" in line
    unavailable = shadow.compare(symbol="AVAXUSDT", side="LONG", stop_frac=0.01, target_frac=0.02,
                                 win_prob=0.45, taker_fee=0.0005, static_slippage_per_side=0.001,
                                 order_notional=11.0, rr_floor=1.6, bbo=None)
    assert unavailable.status == "BBO_UNAVAILABLE" and unavailable.would_change_decision is None
    stale = shadow.compare(symbol="AVAXUSDT", side="LONG", stop_frac=0.01, target_frac=0.02,
                           win_prob=0.45, taker_fee=0.0005, static_slippage_per_side=0.001,
                           order_notional=11.0, rr_floor=1.6, bbo=_bbo(), now_ms=99_999)
    assert stale.status == "BBO_STALE_LOCAL" and stale.net_rr_live is None


def test_static_branch_equals_nexus_expected_value():
    r = shadow.compare(symbol="SOLUSDT", side="LONG", stop_frac=0.004, target_frac=0.008,
                       win_prob=0.45, taker_fee=0.0005, static_slippage_per_side=0.0005,
                       order_notional=6.0, rr_floor=1.6, bbo=None)
    ev = nexus_ai.expected_value(0.45, 100.0, 99.6, 100.8, taker_fee=0.0005, slippage=0.0005)
    assert abs(r.net_rr_static - ev["rr_net"]) < 1e-3
    assert abs(r.ev_static - ev["ev_pct"]) < 1e-3


def test_research_modules_are_not_registered_by_runtime():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "bot"
    for name in ("runtime_bootstrap.py", "runtime_overlays.py", "engine.py", "execution_cost.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "feasibility_frontier" not in text and "bbo_cost_shadow" not in text


# ── RESEARCH_ONLY / SHADOW_ONLY contract (operator checklist 1-12) ──────────

import ast  # noqa: E402
import dataclasses  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH = {"bot.feasibility_frontier", "bot.feasibility_frontier_report", "bot.bbo_cost_shadow"}
DATASET = ROOT / "docs" / "research" / "feasibility_frontier_v1" / "dataset.json"


def test_1_no_runtime_file_imports_research_modules_ast():
    sources = [p for p in (ROOT / "bot").rglob("*.py")
               if p.stem not in {"feasibility_frontier", "feasibility_frontier_report", "bbo_cost_shadow"}]
    sources += [ROOT / "main.py", ROOT / "main_hardened.py", ROOT / "sitecustomize.py"]
    offenders = []
    for path in sources:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = [mod] + [f"{mod}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]          # importlib / __import__ strings
            if any(n in RESEARCH or n.split(".")[-1] in {"feasibility_frontier", "feasibility_frontier_report", "bbo_cost_shadow"}
                   for n in names):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_1b_importing_live_entry_modules_never_loads_research_modules():
    code = ("import sys, bot.runtime_bootstrap, bot.runtime_overlays, bot.engine;"
            f"print(sorted(m for m in sys.modules if m in {sorted(RESEARCH)!r}))")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    env.update(EXCHANGE="binance", PAPER_TRADE="true", LOG_LEVEL="ERROR",
               NEXUS_TELEGRAM="false", PYTHONPATH=str(ROOT) + os.pathsep + os.pathsep.join(sys.path))
    out = subprocess.run([sys.executable, "-S", "-c", code], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"


def _run_full_research():
    from bot import feasibility_frontier_report as report
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    res = report.build(dataset)
    text = report.render(dataset, res)
    snap = shadow.BBOSnapshot("SOLUSDT", 119.88, 119.89, 50.0, 50.0, 1000, 1000)
    rec = shadow.compare(symbol="SOLUSDT", side="LONG", stop_frac=0.004, target_frac=0.008,
                         win_prob=0.45, taker_fee=0.0005, static_slippage_per_side=0.0005,
                         order_notional=6.0, rr_floor=1.6, bbo=snap, now_ms=1000)
    return dataset, res, text, rec


def test_2_3_4_no_exchange_order_or_position_path_is_loaded_or_called():
    banned = ("bot.binance", "bot.kucoin", "bot.exchange", "bot.database", "bot.engine",
              "bot.pilot", "bot.order_state", "bot.durable_execution", "aiohttp", "asyncpg",
              "websockets", "requests", "httpx")
    code = ("import os, sys, json; os.environ.setdefault('EXCHANGE','binance');"
            "import socket\n"
            "def _no_net(*a, **k): raise AssertionError('network touched')\n"
            "socket.socket.connect = _no_net; socket.create_connection = _no_net\n"
            "from bot import feasibility_frontier_report as r, bbo_cost_shadow as s\n"
            f"d=json.load(open({str(DATASET)!r})); r.render(d, r.build(d))\n"
            f"print(sorted(m for m in sys.modules if m in {banned!r}))")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    env.update(PYTHONPATH=str(ROOT) + os.pathsep + os.pathsep.join(sys.path), LOG_LEVEL="ERROR",
               NEXUS_TELEGRAM="false")
    out = subprocess.run([sys.executable, "-S", "-c", code], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"
    # The research API exposes no order/position/exchange verb at all.
    for mod in (ff, shadow):
        public = {n.lower() for n in dir(mod) if not n.startswith("_")}
        assert not any(v in n for n in public for v in
                       ("order", "position", "submit", "cancel", "dispatch", "place", "client"))


def test_5_live_config_and_environment_are_unchanged():
    from bot.config import cfg
    before_cfg = {k: getattr(cfg, k) for k in dir(cfg) if k.isupper()}
    before_env = dict(os.environ)
    _run_full_research()
    assert {k: getattr(cfg, k) for k in dir(cfg) if k.isupper()} == before_cfg
    assert dict(os.environ) == before_env


def test_6_7_decision_and_execution_effect_none_everywhere():
    _, _, text, rec = _run_full_research()
    for mod in (ff, shadow):
        assert "decision_effect=NONE" in (mod.__doc__ or "") or "decision_effect=NONE" in pathlib.Path(mod.__file__).read_text()
        assert "execution_effect=NONE" in pathlib.Path(mod.__file__).read_text()
    assert "decision_effect=NONE execution_effect=NONE" in text
    assert rec.format().endswith("shadow_only=true decision_effect=NONE execution_effect=NONE")
    assert dataclasses.is_dataclass(rec) and not hasattr(rec, "apply")


# Real production NEXUS evaluations ([TECHNICAL_STOP_POLICY] + [NEXUS_SCORE_DECOMP], 2026-10-03).
PRODUCTION_NEXUS = (
    ("SOLUSDT", 0.39283426, 0.78566853, 5.0, 5.0, 0.988),
    ("BNBUSDT", 0.23662604, 0.70987825, 5.0, 10.0, 0.764),
    ("AVAXUSDT", 1.07886450, 2.15773799, 5.0, 10.0, 1.347),
)


def test_8_reproduces_real_production_rr_net():
    for sym, stop_pct, target_pct, taker_bps, slip_bps, logged in PRODUCTION_NEXUS:
        r = shadow.compare(symbol=sym, side="LONG", stop_frac=stop_pct / 100, target_frac=target_pct / 100,
                           win_prob=0.45, taker_fee=taker_bps / 1e4, static_slippage_per_side=slip_bps / 1e4,
                           order_notional=6.0, rr_floor=ff.nexus_min_rr_net(), bbo=None)
        assert abs(r.net_rr_static - logged) < 0.002, (sym, r.net_rr_static, logged)


def test_9_reproduces_real_min_order_log_exactly():
    # [MIN_ORDER_FEASIBILITY] SOLUSDT 18:59:48 PASS risk_budget=0.04379150180
    # min_valid_qty=0.05 risk_at_min_valid_qty=0.03553745 (entry 119.89, stop 119.419031).
    cell = ff.evaluate(SOL, price=119.89, stop_pct=0.470969 / 119.89, gross_rr=2.0,
                       equity=8.75830036, risk_pct=RISK, cost=ff.CostModel.static_for("SOLUSDT"),
                       win_prob=0.45)
    assert abs(cell.risk_budget - 0.0437915018) < 1e-12
    assert abs(cell.min_valid_qty - 0.05) < 1e-12
    assert abs(cell.risk_at_min_qty - 0.03553745) < 1e-9
    assert cell.min_order_ok is True


def test_10_stale_or_invalid_bbo_never_produces_a_live_cost():
    bad = (
        _bbo(bid=0), _bbo(bid=12, ask=11), _bbo(qty=0), _bbo(bid=float("inf")),
        _bbo(recv=1000), _bbo(event=0, recv=10_000),
    )
    for snap, now in zip(bad, (1000, 1000, 1000, 1000, 50_000, 10_000)):
        r = shadow.compare(symbol="AVAXUSDT", side="LONG", stop_frac=0.01, target_frac=0.02,
                           win_prob=0.45, taker_fee=0.0005, static_slippage_per_side=0.001,
                           order_notional=11.0, rr_floor=1.6, bbo=snap, now_ms=now)
        assert r.status != "OK"
        assert (r.live_total_cost_bps, r.net_rr_live, r.ev_live, r.decision_live) == (None, None, None, None)
        assert r.would_change_decision is None and "would_change_decision=NA" in r.format()


ASSUMED_SYMBOLS = {"ETHUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "DOTUSDT", "SUIUSDT",
                   "APTUSDT", "UNIUSDT", "INJUSDT", "SEIUSDT"}


def test_11_unknown_filters_stay_assumed_in_dataset_and_engine():
    dataset, res, _, _ = _run_full_research()
    prov = {s: v["provenance"] for s, v in dataset["symbols"].items()}
    assert {s for s, p in prov.items() if p == "ASSUMED"} == ASSUMED_SYMBOLS
    assert set(prov.values()) <= {"OBSERVED", "INFERRED", "ASSUMED"} and len(prov) == 25
    for sym in ASSUMED_SYMBOLS:
        variants = res["symbols"][sym]["variants"]
        assert set(variants) == {"N5", "N20"}                      # never a single "fact"
        assert all(v["ins"].provenance == "ASSUMED" for v in variants.values())
    for sym in set(prov) - ASSUMED_SYMBOLS:
        assert set(res["symbols"][sym]["variants"]) == {""}


def test_12_no_assumed_result_is_presented_as_fact():
    _, _, text, _ = _run_full_research()
    assert "hypotheses, never facts" in text
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cell = line.split("|")[1].strip()
        sym = cell.split("(")[0]
        if sym in ASSUMED_SYMBOLS:
            assert cell.endswith("(N5)") or cell.endswith("(N20)"), line
    readme = (DATASET.parent / "README.md").read_text(encoding="utf-8")
    for line in readme.splitlines():
        if "ETH" in line and ("R bruto 3" in line or "7 de 9" in line or "7,1" in line):
            assert "ASSUMED" in line, line
