"""Offline regressions for live NEXUS execution-cost calibration.

These tests prove cost-context propagation without sending orders, changing
Railway variables, or requiring KuCoin credentials.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from bot import nexus_live_cost_calibration as calibration


def test_ticker_spread_slippage_major_is_half_spread_plus_impact_floor():
    slip, source, spread_bps = calibration.slippage_from_ticker(
        {"bid": 99.99, "ask": 100.01}, "BTCUSDT"
    )
    assert 0.00019 < slip < 0.00021
    assert source == "ticker_half_spread_plus_impact"
    assert 1.9 < spread_bps < 2.1


def test_ticker_spread_slippage_alt_has_larger_impact_floor():
    major, _, _ = calibration.slippage_from_ticker(
        {"bid": 99.99, "ask": 100.01}, "BTCUSDT"
    )
    alt, _, _ = calibration.slippage_from_ticker(
        {"bid": 99.99, "ask": 100.01}, "XRPUSDT"
    )
    assert alt > major
    assert 0.00029 < alt < 0.00031


def test_invalid_ticker_falls_back_to_legacy_five_bps():
    for ticker in (None, {}, {"bid": 0, "ask": 100}, {"bid": 101, "ask": 100}):
        slip, source, spread_bps = calibration.slippage_from_ticker(ticker, "BTCUSDT")
        assert slip == calibration.DEFAULT_SLIPPAGE == 0.0005
        assert source == "legacy_fallback"
        assert spread_bps is None


def test_wider_spread_never_reduces_estimated_slippage():
    narrow, _, _ = calibration.slippage_from_ticker(
        {"bid": 99.995, "ask": 100.005}, "BTCUSDT"
    )
    wide, _, _ = calibration.slippage_from_ticker(
        {"bid": 99.90, "ask": 100.10}, "BTCUSDT"
    )
    assert wide > narrow


class _FakeClient:
    def __init__(self):
        self.fee_reads = 0

    def get_cached_ticker(self, symbol):
        assert symbol == "BTCUSDT"
        return {"bid": 99.99, "ask": 100.01, "lastPrice": 100.0}

    async def _get(self, endpoint, params=None, auth=False):
        assert endpoint == "/api/v1/trade-fees"
        assert auth is True
        self.fee_reads += 1
        return {"takerFeeRate": "0.00042", "makerFeeRate": "0.0002"}


class _FakeEngine:
    def __init__(self, nexus_module):
        self.client = _FakeClient()
        self._nexus_module = nexus_module

    async def _nexus_validate(self, sig):
        return await asyncio.to_thread(
            self._nexus_module.expected_value,
            0.60, 100.0, 99.0, 102.0,
        )


def _fake_nexus_module():
    def expected_value(win_prob, entry, sl, tp, taker_fee=0.0006, slippage=0.0005):
        return {
            "fee_seen": taker_fee,
            "slippage_seen": slippage,
            "cost": 2 * taker_fee + 2 * slippage,
        }

    return SimpleNamespace(expected_value=expected_value)


class _Log:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def test_cost_context_propagates_through_asyncio_to_thread():
    calibration._FEE_CACHE.clear()
    nexus = _fake_nexus_module()

    class Engine(_FakeEngine):
        pass

    calibration.install(Engine, nexus, _Log())
    engine = Engine(nexus)
    sig = SimpleNamespace(symbol="BTCUSDT")
    result = asyncio.run(engine._nexus_validate(sig))

    assert abs(result["fee_seen"] - 0.00042) < 1e-12
    assert 0.00019 < result["slippage_seen"] < 0.00021
    assert result["cost_source"] == "kucoin_actual_fee+ticker_half_spread_plus_impact"
    assert engine.client.fee_reads == 1


def test_fee_reader_is_cached_across_candidates():
    calibration._FEE_CACHE.clear()
    nexus = _fake_nexus_module()

    class Engine(_FakeEngine):
        pass

    calibration.install(Engine, nexus, _Log())
    engine = Engine(nexus)
    sig = SimpleNamespace(symbol="BTCUSDT")
    asyncio.run(engine._nexus_validate(sig))
    asyncio.run(engine._nexus_validate(sig))
    assert engine.client.fee_reads == 1


def test_dict_decision_handoff_is_optional_and_never_breaks_validation():
    ctx = calibration.NexusCostContext(
        symbol="BTCUSDT",
        taker_fee=0.00042,
        slippage=0.0002,
        fee_source="test",
        slippage_source="test",
        spread_bps=2.0,
    )
    decision = {"decision": "WAIT"}
    before = dict(decision)
    assert calibration._attach_cost_context(decision, ctx, _Log()) is False
    assert decision == before


def test_candidate_lineage_conflict_is_observational_only():
    snapshot = SimpleNamespace(candidate_id="candidate-from-cost")
    ctx = calibration.NexusCostContext(
        symbol="BTCUSDT",
        taker_fee=0.00042,
        slippage=0.0002,
        fee_source="test",
        slippage_source="test",
        spread_bps=2.0,
        snapshot=snapshot,
    )
    decision = SimpleNamespace(_bgx_candidate_id="candidate-from-core")
    assert calibration._attach_cost_context(decision, ctx, _Log()) is False
    assert decision._bgx_candidate_id == "candidate-from-core"



def test_expected_value_outside_candidate_context_keeps_legacy_defaults():
    calibration._FEE_CACHE.clear()
    nexus = _fake_nexus_module()

    class Engine(_FakeEngine):
        pass

    calibration.install(Engine, nexus, _Log())
    result = nexus.expected_value(0.60, 100.0, 99.0, 102.0)
    assert result["fee_seen"] == 0.0006
    assert result["slippage_seen"] == 0.0005
    assert "cost_source" not in result


def test_calibration_does_not_modify_live_thresholds_or_leverage():
    source = inspect.getsource(calibration)
    forbidden_assignments = (
        "NEXUS_MIN_RR_NET =",
        "MIN_RR_RATIO =",
        "MIN_ENTRY_SCORE =",
        "NEXUS_MIN_SCORE =",
        "LEVERAGE =",
    )
    for assignment in forbidden_assignments:
        assert assignment not in source
    assert "original_validate" in source
    assert "legacy_fallback" in source
