"""Regression tests for the pre-NEXUS minimum-order feasibility gate.

The gate is a necessary-condition filter only. It must never round exposure up,
change policy, or become execution authority.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import cfg
from bot.professional_risk import CapitalState
from bot import min_order_feasibility as gate


def _binance(step, min_qty, min_notional):
    return {
        "quantityUnit": "BASE_ASSET",
        "qtyStep": str(step),
        "minQty": str(min_qty),
        "minNotional": str(min_notional),
        "multiplier": 1.0,
        "tickSize": "0.0001",
    }


class _CapitalSnapshot:
    def __init__(self, equity=8.75830036, available=8.75830036):
        self.capital = CapitalState(
            equity=equity,
            available_collateral=available,
        )


class _Log:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def emit(msg, *args, **_kwargs):
            self.records.append((level, msg % args if args else str(msg)))
        return emit


def _engine(symbol, info, *, equity=8.75830036, risk_pct=0.005):
    return SimpleNamespace(
        paper_trade=False,
        pilot=SimpleNamespace(enabled=True),
        instruments={symbol: info},
        client=SimpleNamespace(),
        risk=SimpleNamespace(drawdown=0.10),
        _pilot_available_balance=equity,
        _effective_risk_pct=lambda: risk_pct,
    )


def _snapshot(symbol, *, taker=0.0005, slippage_allowance=0.002):
    return SimpleNamespace(
        symbol=symbol,
        taker_fee=taker,
        slippage_allowance=slippage_allowance,
    )


def _evaluate(symbol, info, entry, stop, *, equity=8.75830036, risk_pct=0.005):
    eng = _engine(symbol, info, equity=equity, risk_pct=risk_pct)
    sig = SimpleNamespace(symbol=symbol, entry=entry, sl=stop, direction="LONG")
    with patch.object(gate.execution_cost, "reusable_snapshot", return_value=_snapshot(symbol)), \
         patch.object(gate, "read_account_capital", AsyncMock(return_value=_CapitalSnapshot(equity))), \
         patch.object(gate, "recovery_size_multiplier", return_value=1.0):
        return asyncio.run(gate.evaluate_candidate(eng, sig))


def test_aave_incident_is_blocked_before_nexus_by_min_qty():
    decision = _evaluate(
        "AAVEUSDT",
        _binance("0.1", "0.1", "5"),
        173.8,
        171.92629,
    )
    assert decision.allowed is False
    assert decision.reason == "INSUFFICIENT_RISK_BUDGET"
    assert str(decision.detail["binding"]) == "MIN_QTY_BINDING"
    assert float(decision.detail["min_valid_qty"]) == 0.1
    assert float(decision.detail["risk_at_min_valid_qty"]) > float(decision.detail["risk_budget"])
    assert float(decision.detail["rounded_qty"]) < float(decision.detail["min_valid_qty"])


def test_ltc_incident_is_blocked_before_nexus_by_min_notional():
    decision = _evaluate(
        "LTCUSDT",
        _binance("0.001", "0.001", "20"),
        69.05,
        68.6561,
    )
    assert decision.allowed is False
    assert decision.reason == "INSUFFICIENT_RISK_BUDGET"
    assert str(decision.detail["binding"]) == "MIN_NOTIONAL_BINDING"
    assert abs(float(decision.detail["min_valid_qty"]) - 0.290) < 1e-12
    assert float(decision.detail["risk_at_min_valid_qty"]) > float(decision.detail["risk_budget"])


def test_historical_atom_20261001_catastrophic_trade_is_blocked_by_reentry_v1():
    # Exact production setup that opened 570.55 ATOM on 2026-10-01.
    # Under Re-entry v1, equity * 0.25% cannot fund Binance's minimum
    # notional once stop distance + round-trip costs are included.
    decision = _evaluate(
        "ATOMUSDT",
        _binance("0.01", "0.01", "5"),
        1.693,
        1.70922,
        equity=19.99813533,
        risk_pct=0.0025,
    )
    assert decision.allowed is False
    assert decision.reason == "INSUFFICIENT_RISK_BUDGET"
    assert str(decision.detail["binding"]) == "MIN_NOTIONAL_BINDING"
    assert abs(float(decision.detail["risk_budget"]) - 0.049995338325) < 1e-12
    assert abs(float(decision.detail["min_valid_qty"]) - 2.96) < 1e-12
    assert float(decision.detail["risk_at_min_valid_qty"]) > float(decision.detail["risk_budget"])
    assert float(decision.detail["rounded_qty"]) < float(decision.detail["min_valid_qty"])


def test_exchange_valid_small_order_can_pass_feasibility_without_authorizing_execution():
    decision = _evaluate(
        "DOGEUSDT",
        _binance("1", "1", "5"),
        0.20,
        0.199,
    )
    assert decision.allowed is True
    assert decision.reason == "SIZED"
    assert float(decision.detail["rounded_qty"]) >= float(decision.detail["min_valid_qty"])
    # PASS is only a necessary condition; the result contains no execution flag.
    assert not hasattr(decision, "execution_allowed")


def test_recovery_multiplier_is_applied_to_same_risk_budget_math():
    symbol = "DOGEUSDT"
    info = _binance("1", "1", "5")
    eng = _engine(symbol, info, risk_pct=0.01)
    sig = SimpleNamespace(symbol=symbol, entry=0.20, sl=0.199, direction="LONG")
    with patch.object(gate.execution_cost, "reusable_snapshot", return_value=_snapshot(symbol)), \
         patch.object(gate, "read_account_capital", AsyncMock(return_value=_CapitalSnapshot())), \
         patch.object(gate, "recovery_size_multiplier", return_value=0.5):
        decision = asyncio.run(gate.evaluate_candidate(eng, sig))
    assert abs(float(decision.detail["risk_pct"]) - 0.005) < 1e-12


def test_blocked_candidate_never_reaches_original_open_or_nexus():
    class Engine:
        _min_order_feasibility_installed = False

        def __init__(self):
            self.paper_trade = False
            self.pilot = SimpleNamespace(enabled=True)
            self.nexus_calls = 0
            self.order_calls = 0

        async def _open(self, sig):
            self.nexus_calls += 1
            self.order_calls += 1
            return "should-not-run"

    log = _Log()
    gate.install(Engine, log)
    engine = Engine()
    blocked = gate.FeasibilityDecision(
        False,
        "INSUFFICIENT_RISK_BUDGET",
        {"risk_budget": 0.04, "min_valid_qty": 1, "risk_at_min_valid_qty": 0.10,
         "required_equity_at_min_valid_qty": 20, "binding": "MIN_QTY_BINDING"},
    )
    with patch.object(gate, "evaluate_candidate", AsyncMock(return_value=blocked)):
        result = asyncio.run(engine._open(SimpleNamespace(symbol="TESTUSDT")))

    assert result is None
    assert engine.nexus_calls == 0
    assert engine.order_calls == 0
    assert any("nexus_called=false" in text for _, text in log.records)


def test_pass_only_delegates_to_existing_chain():
    class Engine:
        _min_order_feasibility_installed = False

        def __init__(self):
            self.paper_trade = False
            self.pilot = SimpleNamespace(enabled=True)
            self.existing_chain_calls = 0

        async def _open(self, sig):
            self.existing_chain_calls += 1
            return "existing-chain"

    gate.install(Engine, _Log())
    engine = Engine()
    allowed = gate.FeasibilityDecision(
        True,
        "SIZED",
        {"risk_budget": 0.04, "min_valid_qty": 1, "risk_at_min_valid_qty": 0.02},
    )
    with patch.object(gate, "evaluate_candidate", AsyncMock(return_value=allowed)):
        result = asyncio.run(engine._open(SimpleNamespace(symbol="TESTUSDT")))

    assert result == "existing-chain"
    assert engine.existing_chain_calls == 1


def test_ambiguous_feasibility_failure_defers_to_existing_fail_closed_chain():
    class Engine:
        _min_order_feasibility_installed = False

        def __init__(self):
            self.paper_trade = False
            self.pilot = SimpleNamespace(enabled=True)
            self.existing_chain_calls = 0

        async def _open(self, sig):
            self.existing_chain_calls += 1
            return "downstream-authority"

    log = _Log()
    gate.install(Engine, log)
    engine = Engine()
    with patch.object(gate, "evaluate_candidate", AsyncMock(side_effect=RuntimeError("read failed"))):
        result = asyncio.run(engine._open(SimpleNamespace(symbol="TESTUSDT")))
    assert result == "downstream-authority"
    assert engine.existing_chain_calls == 1
    assert any("result=DEFER" in text for _, text in log.records)


def test_margin_only_block_is_deferred_not_stolen_from_downstream_authority():
    symbol = "ETHUSDT"
    info = _binance("0.001", "0.001", "20")
    eng = _engine(symbol, info, equity=1000, risk_pct=0.01)
    sig = SimpleNamespace(symbol=symbol, entry=100.0, sl=99.5, direction="LONG")
    # Fresh account shape normalizes to zero available collateral; the already
    # confirmed V3 snapshot supplies the capital proof used by the real sizing
    # path. Then force a tiny margin cap so decomposition reaches MARGIN_CAP.
    v3_capital = CapitalState(equity=1000, available_collateral=0.01)
    eng.risk.professional_snapshot = SimpleNamespace(
        confirmed=True,
        capital=v3_capital,
    )
    with patch.object(gate.execution_cost, "reusable_snapshot", return_value=None), \
         patch.object(gate.execution_cost, "fallback_taker_fee", return_value=0.0006), \
         patch.object(gate, "read_account_capital", AsyncMock(side_effect=RuntimeError("partial"))), \
         patch.object(gate, "recovery_size_multiplier", return_value=1.0):
        decision = asyncio.run(gate.evaluate_candidate(eng, sig))
    assert decision.allowed is True
    assert decision.proven is False
    assert decision.reason == "DEFER_MARGIN_CAP_BINDING"


def test_paper_and_nonpilot_paths_are_unchanged():
    class Engine:
        _min_order_feasibility_installed = False

        def __init__(self, *, paper, pilot):
            self.paper_trade = paper
            self.pilot = SimpleNamespace(enabled=pilot)
            self.calls = 0

        async def _open(self, sig):
            self.calls += 1
            return "unchanged"

    gate.install(Engine, _Log())
    evaluate = AsyncMock(side_effect=AssertionError("gate must not run"))
    with patch.object(gate, "evaluate_candidate", evaluate):
        paper = Engine(paper=True, pilot=True)
        nonpilot = Engine(paper=False, pilot=False)
        assert asyncio.run(paper._open(SimpleNamespace(symbol="X"))) == "unchanged"
        assert asyncio.run(nonpilot._open(SimpleNamespace(symbol="X"))) == "unchanged"
    assert paper.calls == 1
    assert nonpilot.calls == 1
    assert evaluate.await_count == 0




def test_controlled_one_shot_budget_can_make_exchange_minimum_feasible_without_authorizing():
    symbol = "ADAUSDT"
    info = _binance("1", "1", "5")
    eng = _engine(symbol, info, equity=5.39561426, risk_pct=0.0025)
    sig = SimpleNamespace(
        symbol=symbol,
        entry=0.2786,
        sl=0.2760,
        direction="LONG",
    )
    with patch.object(gate.execution_cost, "reusable_snapshot", return_value=_snapshot(symbol)), \
         patch.object(gate, "read_account_capital", AsyncMock(return_value=_CapitalSnapshot(5.39561426))), \
         patch(
             "bot.controlled_live_reentry_v1.candidate_risk_pct",
             return_value=0.10 / 5.39561426,
         ), \
         patch.object(gate, "recovery_size_multiplier", side_effect=AssertionError("controlled path must not use recovery")):
        decision = asyncio.run(gate.evaluate_candidate(eng, sig))

    assert decision.allowed is True
    assert decision.reason == "SIZED"
    assert abs(float(decision.detail["risk_budget"]) - 0.10) < 1e-9
    assert float(decision.detail["rounded_qty"]) >= float(decision.detail["min_valid_qty"])
    assert not hasattr(decision, "execution_allowed")


def test_gate_does_not_change_trading_thresholds_or_leverage():
    before = (cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_DRAWDOWN, cfg.MAX_POSITIONS)
    # Merely importing/evaluating the module must not mutate policy.
    _ = gate.FeasibilityDecision(True, "SIZED", {})
    after = (cfg.LEVERAGE, cfg.MAX_RISK_PCT, cfg.MAX_DRAWDOWN, cfg.MAX_POSITIONS)
    assert after == before
