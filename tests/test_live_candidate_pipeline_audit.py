"""Regression tests for the LIVE candidate pipeline audit (scanner -> place_order).

Covers PULLBACK_CONFIRMATION semantics and refresh, MIN_ORDER_FEASIBILITY /
stop-risk sizing arithmetic (units, stepSize, minNotional, leverage
separation), fail-closed unit errors, the observability-only feasibility
matrix wiring and the passive CANDIDATE_TERMINAL telemetry.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
from contextlib import contextmanager, redirect_stdout
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import candidate_terminal_telemetry as terminal
from bot import min_order_feasibility as gate
from bot import pullback_confirmation_hardening as pullback
from bot import viability_fail_closed_hardening as viability
from bot.entry_latency_observability import EntryLatencyCollector
from bot.final_sizing_invariants import _select_final_quantity
from bot.professional_risk import CapitalState, stop_risk_size
from bot.quantity import quantity_rules, validate_base_quantity
from bot.sizing_decomposition import decompose


# ── helpers ────────────────────────────────────────────────────────────────


class _Caught:
    value = None


@contextmanager
def _raises(exc_type):
    caught = _Caught()
    try:
        yield caught
    except exc_type as exc:  # noqa: PERF203 - test helper
        caught.value = exc
        return
    raise AssertionError(f"{exc_type.__name__} not raised")


def _bar(ts, o, c):
    return {"ts": ts, "o": o, "h": max(o, c) + 0.2, "l": min(o, c) - 0.2, "c": c, "v": 100.0}


def _uptrend(n=40, start=100.0):
    bars, price = [], start
    for i in range(n):
        bars.append(_bar(i, price, price + 1.0))
        price += 1.0
    return bars, price


def _with_forming(bars):
    """Append the disposable forming bar that every analyzer drops."""
    last = bars[-1]
    return bars + [_bar(last["ts"] + 1, last["c"], last["c"])]


def _binance(step, min_qty, min_notional):
    return {"quantityUnit": "BASE_ASSET", "qtyStep": str(step), "minQty": str(min_qty),
            "minNotional": str(min_notional), "multiplier": 1.0, "tickSize": "0.01"}


BNB = _binance("0.01", "0.01", "5")
# Production SOL/BNB cost snapshot: taker 5 bps/side, slippage 5+5 bps.
FEE, SLIP = Decimal("0.0005"), Decimal("0.001")
EQUITY = Decimal("8.75830036")
RISK_PCT = Decimal("0.005")


def _decompose(info=BNB, *, equity=EQUITY, entry="1000", stop="996.202308",
               risk_pct=RISK_PCT, leverage=50, available=None, max_margin_pct="0.8"):
    return decompose(info=info, equity=equity, available=equity if available is None else available,
                     entry=entry, stop=stop, risk_pct=risk_pct, leverage=leverage,
                     max_margin_pct=max_margin_pct, fee_rate_per_side=FEE, slippage_pct=SLIP)


class _Log:
    def __init__(self):
        self.rows = []

    def __getattr__(self, level):
        def emit(msg, *args, **_kw):
            self.rows.append((level, msg % args if args else str(msg)))
        return emit


# ── A. PULLBACK_CONFIRMATION ───────────────────────────────────────────────

def test_1_two_votes_block_exactly_like_production_bnb_case():
    bars, price = _uptrend()
    pulled = bars + [_bar(40, price, price - 1.5)]          # red, lower close
    m = pullback._pullback_metrics(_with_forming(pulled), "LONG")
    assert m["vote_count"] == 2
    assert m["votes"] == {"body_aligned": False, "price_progress": False,
                          "ema9_reclaim": True, "macd_turn": False, "rsi_recovered": True}
    assert m["ok"] is False and m["reason"] == "insufficient_reversal_votes"
    assert m["rsi"] > 70  # overbought trend: RSI vote is near-certain, not evidence


def test_2_sufficient_votes_pass_on_closed_candle():
    bars, price = _uptrend()
    reversal = bars + [_bar(40, price, price - 1.5), _bar(41, price - 1.5, price + 0.5)]
    m = pullback._pullback_metrics(_with_forming(reversal), "LONG")
    assert m["vote_count"] >= 4 and m["ok"] is True and m["reason"] == "confirmed"


def test_2b_gate_uses_closed_bars_only_forming_bar_cannot_flip_result():
    bars, price = _uptrend()
    pulled = bars + [_bar(40, price, price - 1.5)]
    # A huge green forming bar must not create reversal evidence on the closed path.
    forming = _bar(41, price - 1.5, price + 50.0)
    assert pullback._pullback_metrics(pulled + [forming], "LONG")["ok"] is False


def test_3_new_closed_candle_refreshes_setup_and_verdict():
    bars, price = _uptrend()
    pulled = bars + [_bar(40, price, price - 1.5)]
    reversal = pulled + [_bar(41, price - 1.5, price + 0.5)]
    seq = [_with_forming(pulled), _with_forming(reversal)]

    class Analyzer:
        calls = 0

        def analyze_mtf(self, symbol, k15, k1h, k4h, *a, **kw):
            Analyzer.calls += 1
            last_closed = k15[-2]
            return SimpleNamespace(symbol=symbol, direction="LONG", entry_type="PULLBACK",
                                   _bgx_formation_timestamp=float(last_closed["ts"] * 900 + 900),
                                   _bgx_formation_bucket=int(last_closed["ts"] + 1000))

    pullback.install(Analyzer, _Log())
    first = Analyzer().analyze_mtf("BNBUSDT", seq[0], [], [])
    second = Analyzer().analyze_mtf("BNBUSDT", seq[1], [], [])
    assert first is None                      # blocked on the pullback bar
    assert second is not None                 # same structure, next closed bar confirms
    assert second._bgx_setup_id != "BNBUSDT:LONG:PULLBACK:1040"
    assert second._bgx_setup_id.endswith(":1041")


def test_4_setup_id_is_bucketed_and_never_sticky():
    a = SimpleNamespace(symbol="BNBUSDT", direction="LONG", entry_type="PULLBACK",
                        _bgx_formation_timestamp=900.0 * 10, _bgx_formation_bucket=10)
    b = SimpleNamespace(symbol="BNBUSDT", direction="LONG", entry_type="PULLBACK",
                        _bgx_formation_timestamp=900.0 * 11, _bgx_formation_bucket=11)
    assert pullback._strategy_setup_id(a) != pullback._strategy_setup_id(b)
    # Re-evaluating identical candles is deterministic (no hidden state/cache).
    bars, price = _uptrend()
    data = _with_forming(bars + [_bar(40, price, price - 1.5)])
    assert pullback._pullback_metrics(data, "LONG") == pullback._pullback_metrics(data, "LONG")


# ── B. MIN_ORDER_FEASIBILITY / stop-risk arithmetic ────────────────────────

def test_5_min_qty_above_budget_blocks_with_production_numbers():
    d = _decompose()
    assert d["risk_budget"] == Decimal("0.0437915018")          # log: 0.04379150180
    assert d["loss_per_unit"] == Decimal("5.797692")            # 3.797692 + 1.0 fee + 1.0 slip
    assert d["min_valid_qty"] == Decimal("0.01")
    assert d["risk_at_min_valid_qty"] == Decimal("0.05797692")  # log: 0.05797692
    assert d["required_equity_at_min_valid_qty"] == Decimal("11.595384")  # log: 11.595384
    assert (d["result"], d["reason"], d["binding"]) == ("BLOCK", "INSUFFICIENT_RISK_BUDGET", "MIN_QTY_BINDING")


def test_6_min_qty_exactly_at_budget_passes():
    d = _decompose(equity=Decimal("11.595384"))
    assert d["risk_budget"] == d["risk_at_min_valid_qty"]
    assert d["rounded_qty"] == Decimal("0.01")
    assert (d["result"], d["binding"]) == ("PASS", "RISK_BUDGET")


def test_7_step_size_rounds_down_never_up():
    # budget/loss = 0.0199.. -> floor to 0.01 (never 0.02)
    d = _decompose(equity=Decimal("23.07"))
    assert Decimal("0.019") < d["raw_qty_risk"] < Decimal("0.02")
    assert d["rounded_qty"] == Decimal("0.01")
    assert d["rounded_qty"] * d["loss_per_unit"] <= d["risk_budget"]


def test_8_min_notional_binds_and_rounds_up_to_whole_step():
    d = _decompose(info=_binance("0.1", "0.1", "5"), equity=Decimal("1000"),
                   entry="3", stop="2.9")
    assert d["min_valid_qty"] == Decimal("1.7")       # 5/3 = 1.666.. -> 1.7
    assert d["min_valid_qty"] * Decimal("3") >= Decimal("5")
    blocked = _decompose(info=_binance("0.1", "0.1", "5"), equity=Decimal("1"),
                         entry="3", stop="2.9")
    assert blocked["binding"] == "MIN_NOTIONAL_BINDING" and blocked["result"] == "BLOCK"


def test_9_leverage_changes_margin_only_never_stop_risk():
    lo, hi = _decompose(equity=Decimal("50"), leverage=5), _decompose(equity=Decimal("50"), leverage=50)
    for key in ("risk_budget", "loss_per_unit", "raw_qty_risk", "risk_at_min_valid_qty",
                "rounded_qty", "required_equity_at_min_valid_qty"):
        assert lo[key] == hi[key], key
    assert hi["margin_at_min_valid_qty"] * 10 == lo["margin_at_min_valid_qty"]
    # Final sizing path agrees: projected stop loss identical at 5x and 50x.
    cap = CapitalState(equity=50.0, available_collateral=50.0)
    kw = dict(capital=cap, entry=1000.0, stop=996.202308, risk_pct=0.005, qty_step=0.01,
              min_qty=0.01, max_margin_pct=0.8, fee_rate_per_side=0.0005, expected_slippage_pct=0.001)
    low = stop_risk_size(leverage=5, **kw).projected_stop_loss
    high = stop_risk_size(leverage=50, **kw).projected_stop_loss
    assert abs(low - high) < 1e-12


def test_10_no_quantity_ever_exceeds_risk_budget_property():
    rng = random.Random(466)
    for _ in range(2000):
        entry = Decimal(str(round(rng.uniform(0.05, 5000), 4)))
        stop = entry * (Decimal(1) - Decimal(str(round(rng.uniform(0.001, 0.05), 5))))
        step = Decimal(rng.choice(["0.001", "0.01", "0.1", "1"]))
        info = _binance(step, step * rng.randint(1, 5), rng.choice(["0", "5", "20", "100"]))
        equity = Decimal(str(round(rng.uniform(1, 5000), 4)))
        d = _decompose(info=info, equity=equity, entry=str(entry), stop=str(stop),
                       leverage=rng.choice([1, 5, 20, 50, 125]))
        assert d["rounded_qty"] <= d["raw_qty"]
        assert d["rounded_qty"] % step == 0
        if d["result"] == "PASS":
            assert d["rounded_qty"] * d["loss_per_unit"] <= d["risk_budget"] * (1 + Decimal("1e-20"))
            assert d["rounded_qty"] >= d["min_valid_qty"]
    # min(stop_risk_qty, operator_margin_cap_qty) is the final authority.
    assert _select_final_quantity(target_qty=0.5, risk_qty=0.02) == 0.02
    assert _select_final_quantity(target_qty=0.01, risk_qty=0.02) == 0.01
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        assert _select_final_quantity(target_qty=bad, risk_qty=0.02) == 0.0


class _NoMutationClient:
    """Any exchange mutation path is a test failure."""

    def __getattr__(self, name):
        if name in {"_post", "_delete", "_put", "place_order", "cancel_order", "cancel_all_orders",
                    "set_leverage", "set_position_stops", "set_sl", "cancel_algo_order"}:
            raise AssertionError(f"exchange mutation touched: {name}")
        raise AttributeError(name)


def test_11_audit_and_shadow_paths_never_mutate_exchange():
    eng = SimpleNamespace(paper_trade=False, pilot=SimpleNamespace(enabled=True),
                          instruments={"BNBUSDT": BNB}, client=_NoMutationClient(),
                          risk=SimpleNamespace(drawdown=0.6158, balance=8.7583),
                          _pilot_available_balance=8.7583, _effective_risk_pct=lambda: 0.01)
    sig = SimpleNamespace(symbol="BNBUSDT", entry=1000.0, sl=996.202308, direction="LONG")
    snap = SimpleNamespace(symbol="BNBUSDT", taker_fee=0.0005, slippage_allowance=0.001)
    capital = SimpleNamespace(capital=CapitalState(equity=8.7583, available_collateral=8.7583))
    with patch.object(gate.execution_cost, "reusable_snapshot", return_value=snap), \
         patch.object(gate, "read_account_capital", AsyncMock(return_value=capital)), \
         patch.object(gate, "recovery_size_multiplier", return_value=0.5):
        decision = asyncio.run(gate.evaluate_candidate(eng, sig))
    assert decision.allowed is False and decision.reason == "INSUFFICIENT_RISK_BUDGET"

    from bot import min_order_feasibility_matrix as matrix
    log = _Log()
    with patch.object(matrix, "recovery_size_multiplier", return_value=0.5):
        matrix.log_once(eng, {"BNBUSDT": 1000.0}, log)
    assert any("MIN_ORDER_FEASIBILITY_MATRIX_SUMMARY" in text for _, text in log.rows)


def test_12_unit_errors_fail_closed_across_the_chain():
    # Metadata whose minQty is not a whole number of steps is rejected.
    with _raises(ValueError):
        quantity_rules(_binance("0.01", "0.015", "5"))
    assert _decompose(info=_binance("0.01", "0.015", "5"))["result"] == "BLOCK"
    # Off-step or sub-notional quantities are never valid exchange orders.
    with _raises(ValueError):
        validate_base_quantity(0.015, BNB, 1000.0)
    with _raises(ValueError):
        validate_base_quantity(0.001, _binance("0.001", "0.001", "5"), 1000.0)
    # Non-finite / degenerate geometry never sizes.
    with _raises(ValueError):
        _decompose(entry="NaN")
    assert _decompose(stop="1000")["result"] == "BLOCK"
    cap = CapitalState(equity=10.0, available_collateral=10.0)
    with _raises(ValueError):
        stop_risk_size(capital=cap, entry=1000.0, stop=float("nan"), risk_pct=0.005, leverage=50,
                       qty_step=0.01, min_qty=0.01, max_margin_pct=0.8)
    # The early gate only defers ambiguity; it never authorizes execution itself.
    eng = SimpleNamespace(instruments={"BNBUSDT": _binance("0.01", "0.015", "5")},
                          client=_NoMutationClient(), risk=SimpleNamespace(drawdown=0.1),
                          _effective_risk_pct=lambda: 0.005)
    sig = SimpleNamespace(symbol="BNBUSDT", entry=1000.0, sl=996.0)
    capital = SimpleNamespace(capital=CapitalState(equity=8.75, available_collateral=8.75))
    with patch.object(gate, "read_account_capital", AsyncMock(return_value=capital)), \
         patch.object(gate.execution_cost, "reusable_snapshot", return_value=None), \
         patch.object(gate, "recovery_size_multiplier", return_value=1.0):
        decision = asyncio.run(gate.evaluate_candidate(eng, sig))
    assert decision.proven is False and decision.reason.startswith("DEFER_")


# ── C. feasibility matrix wiring (observability bug) ───────────────────────

def test_matrix_runs_from_the_production_viability_override_without_changing_viability():
    engine = SimpleNamespace(
        instruments={"BTCUSDT": {"minQty": 1, "multiplier": 0.001}}, viable_symbols=[],
        risk=SimpleNamespace(balance=100.0),
        client=SimpleNamespace(get_all_tickers=AsyncMock(return_value=[{"symbol": "BTCUSDT", "lastPrice": "10000"}]),
                               get_cached_ticker=Mock(return_value={})))
    calls = []
    with patch("bot.min_order_feasibility_matrix.log_once", side_effect=lambda *a: calls.append(a)):
        ok = asyncio.run(viability._filter_viable_symbols_fail_closed(engine))
    assert ok is True and "BTCUSDT" in engine.viable_symbols
    assert len(calls) == 1 and calls[0][1] == {"BTCUSDT": 10000.0}

    before = list(engine.viable_symbols)
    with patch("bot.min_order_feasibility_matrix.log_once", side_effect=RuntimeError("boom")):
        assert asyncio.run(viability._filter_viable_symbols_fail_closed(engine)) is True
    assert engine.viable_symbols == before


def test_entry_latency_recognises_current_pullback_and_binance_order_formats():
    c = EntryLatencyCollector()
    c.observe("[BNBUSDT] ✅ SINAL LONG score=76/100 RR=2.0 entry=PULLBACK", 1)
    out = c.observe("[PULLBACK_CONFIRMATION] setup_id=BNBUSDT:LONG:PULLBACK:1 symbol=BNBUSDT "
                    "side=LONG result=BLOCKED reason=insufficient_reversal_votes", 2)
    assert any("stage=pullback" in line for line in out)
    assert any("terminal=pullback" in line for line in out)
    c.observe("[SOLUSDT] ✅ SINAL LONG score=70/100 RR=2.0 entry=MOMENTUM", 3)
    c.observe("📡 _open SOLUSDT tentativa 1/1 | side=Buy qty=0.05", 4)
    out = c.observe("📤 [BINANCE_ORDER] clientOid=bgx7-x orderId=9 symbol=SOLUSDT side=BUY qty=0.05", 5)
    assert any("stage=exchange_ack" in line for line in out)


# ── E. CANDIDATE_TERMINAL telemetry ────────────────────────────────────────

def _trace(*messages):
    t = terminal.CandidateTrace(candidate_id="SOLUSDT:LONG:MOMENTUM:1990058",
                                symbol="SOLUSDT", side="LONG", score="63")
    for m in messages:
        terminal.observe_open_message(t, m)
    return t


def test_terminal_classifies_production_nexus_ev_veto():
    t = _trace(
        "[MIN_ORDER_FEASIBILITY] symbol=SOLUSDT result=PASS reason=SIZED risk_budget=0.04379150180",
        "[PILOT_LIVE_PREFLIGHT] result=PASS exposure_verified=True",
        "[NEXUS_COST] symbol=SOLUSDT taker_bps=5.000",
        "[NEXUS_ZERO] symbol=SOLUSDT stage=EV_RR_GATE score=0 confidence=0 reason=EV negativo",
    )
    assert terminal.classify(t) == ("NEXUS", "EV_RR_GATE")
    line = terminal.format_record(t, *terminal.classify(t))
    assert "nexus_called=true final_sizing_reached=false cross_reached=false" in line
    assert line.endswith("execution_effect=NONE")


def test_terminal_min_order_block_and_downstream_stages():
    t = _trace("[MIN_ORDER_FEASIBILITY] symbol=BNBUSDT result=BLOCK reason=INSUFFICIENT_RISK_BUDGET "
               "risk_budget=0.0437 binding=MIN_QTY_BINDING")
    assert terminal.classify(t) == ("MIN_ORDER_FEASIBILITY", "INSUFFICIENT_RISK_BUDGET")
    t = _trace("[NEXUS_COST] symbol=X", "[AI_DECISION] symbol=X side=LONG decision=APPROVE approved=True",
               "[FINAL_SIZING_INVARIANT] symbol=X result=PASS", "[BINANCE_CROSS_STRESS] symbol=X result=BLOCK reason=risk_rate")
    assert terminal.classify(t) == ("CROSS_STRESS", "risk_rate")
    t = _trace("[NEXUS_COST] symbol=X", "[RISK_V3_CORE] symbol=X qty=0.05",
               "[PILOT_PREDISPATCH_RECOVERY] result=BLOCK episode=r4 reason=durable_episode_mismatch")
    assert terminal.classify(t) == ("PREDISPATCH", "durable_episode_mismatch")
    t = _trace("📡 _open X tentativa 1/1", "📤 [BINANCE_ORDER] clientOid=bgx7 orderId=1 symbol=X side=BUY qty=1")
    assert terminal.classify(t) == ("SUBMISSION", "ORDER_ACCEPTED")
    assert terminal.classify(_trace("[NEXUS_COST] symbol=X")) == ("NEXUS", "NO_TERMINAL_LOG")


def test_terminal_collector_replays_production_sol_sequence_passively():
    c = terminal.CandidateTerminalCollector()
    lines = [
        "[SOLUSDT] ✅ SINAL LONG score=63/100 RR=2.0 entry=MOMENTUM",
        "✅ [SOLUSDT] CANDIDATO: LONG score=63 R:R=2.00 PnL_est=+0.64%",
        "[TECHNICAL_STOP_POLICY] symbol=SOLUSDT result=PASS stop_distance_pct=0.39283 "
        "candidate_id=SOLUSDT:LONG:MOMENTUM:1990058 cost_snapshot_id=cost-1",
        "[MIN_ORDER_FEASIBILITY] symbol=SOLUSDT result=PASS reason=SIZED risk_budget=0.04379150180",
        "[PILOT_LIVE_PREFLIGHT] result=PASS exposure_verified=True",
        "[NEXUS_COST] symbol=SOLUSDT taker_bps=5.000",
        "[NEXUS_SCORE_DECOMP] symbol=UNIUSDT decision=SHORT final=81.23",   # other symbol: ignored
        "[NEXUS_ZERO] symbol=SOLUSDT stage=EV_RR_GATE score=0 reason=EV negativo",
    ]
    out = [rec for line in lines for rec in c.observe(line)]
    assert out == [
        "[CANDIDATE_TERMINAL] candidate_id=SOLUSDT:LONG:MOMENTUM:1990058 symbol=SOLUSDT side=LONG "
        "score=63 terminal_stage=NEXUS terminal_reason=EV_RR_GATE nexus_called=true "
        "final_sizing_reached=false cross_reached=false predispatch_reached=false "
        "submission_reached=false telemetry_only=true execution_effect=NONE"
    ]
    assert c.active is None


def test_terminal_collector_flushes_trace_without_terminal_log_and_never_wraps():
    c = terminal.CandidateTerminalCollector()
    assert c.observe("[MIN_ORDER_FEASIBILITY] symbol=TRXUSDT result=PASS reason=SIZED") == []
    assert c.observe("[NEXUS_COST] symbol=TRXUSDT taker_bps=5") == []
    out = c.observe("[MIN_ORDER_FEASIBILITY] symbol=BNBUSDT result=BLOCK reason=INSUFFICIENT_RISK_BUDGET")
    assert "symbol=TRXUSDT" in out[0] and "terminal_stage=NEXUS terminal_reason=NO_TERMINAL_LOG" in out[0]
    assert "symbol=BNBUSDT" in out[1] and "terminal_stage=MIN_ORDER_FEASIBILITY" in out[1]
    # install() attaches a handler only: TradingEngine._open is never touched.
    log = logging.getLogger("candidate_terminal_install_test")
    collector = terminal.install(log)
    assert terminal.install(log) is collector
    buf = io.StringIO()
    with redirect_stdout(buf):
        log.warning("[MIN_ORDER_FEASIBILITY] symbol=BNBUSDT result=BLOCK reason=INSUFFICIENT_RISK_BUDGET")
    assert "[CANDIDATE_TERMINAL]" in buf.getvalue()


def test_scan_stage_pullback_terminal_is_deduplicated_per_setup():
    dedupe = terminal.ScanTerminalDeduper()
    msg = ("[PULLBACK_CONFIRMATION] setup_id=BNBUSDT:LONG:PULLBACK:1990062 symbol=BNBUSDT side=LONG "
           "result=BLOCKED reason=insufficient_reversal_votes votes={} vote_count=2")
    first = terminal.observe_scan_message(msg, dedupe)
    assert first and "terminal_stage=PULLBACK_CONFIRMATION" in first
    assert "terminal_reason=insufficient_reversal_votes" in first and "nexus_called=false" in first
    assert all(terminal.observe_scan_message(msg, dedupe) is None for _ in range(53))
    nxt = msg.replace("1990062", "1990063")
    assert terminal.observe_scan_message(nxt, dedupe) is not None
    funnel = "⛔ [SOLUSDT] REJEITADO pelo regime: LONG não permitido em RANGING (score era 70)"
    assert "regime_disallows_direction" in terminal.observe_scan_message(funnel, dedupe, now=900.0)
    assert terminal.observe_scan_message(funnel, dedupe, now=901.0) is None
