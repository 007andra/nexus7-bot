"""Offline regressions for KuCoin backtest execution parity.

No exchange credentials or network calls are used.
"""
from __future__ import annotations

import asyncio
import inspect

from bot import backtest
from bot import missed_opportunity_audit as opportunity
from bot.execution_model import (
    adverse_fill,
    fee_return_fraction,
    funding_return_fraction,
)
from bot.kucoin_execution_model import (
    fetch_actual_taker_fee,
    fetch_public_funding_history,
)


def test_market_slippage_is_always_adverse():
    slip = 0.001
    assert adverse_fill(100.0, "LONG", is_entry=True, slippage_rate=slip) == 100.1
    assert adverse_fill(100.0, "LONG", is_entry=False, slippage_rate=slip) == 99.9
    assert adverse_fill(100.0, "SHORT", is_entry=True, slippage_rate=slip) == 99.9
    assert adverse_fill(100.0, "SHORT", is_entry=False, slippage_rate=slip) == 100.1


def test_fee_fraction_uses_entry_and_actual_exit_notional():
    fee = fee_return_fraction(100.0, [(110.0, 0.5), (90.0, 0.5)], 0.0006)
    # Entry fee 0.0006 + weighted exit fee 0.0006 * 1.0.
    assert abs(fee - 0.0012) < 1e-12


def test_funding_is_zero_when_no_settlement_is_crossed():
    events = [{"timepoint": 20_000, "fundingRate": 0.001}]
    pnl, count = funding_return_fraction(events, "LONG", 10_000, 19_999, 100.0)
    assert pnl == 0.0
    assert count == 0


def test_positive_funding_long_pays_short_receives():
    events = [{"timepoint": 20_000, "fundingRate": 0.001}]
    long_pnl, long_count = funding_return_fraction(events, "LONG", 10_000, 30_000, 100.0)
    short_pnl, short_count = funding_return_fraction(events, "SHORT", 10_000, 30_000, 100.0)
    assert abs(long_pnl + 0.001) < 1e-12
    assert abs(short_pnl - 0.001) < 1e-12
    assert long_count == short_count == 1


def test_negative_funding_reverses_payer_and_receiver():
    events = [{"timepoint": 20_000, "fundingRate": -0.002}]
    long_pnl, _ = funding_return_fraction(events, "LONG", 10_000, 30_000, 100.0)
    short_pnl, _ = funding_return_fraction(events, "SHORT", 10_000, 30_000, 100.0)
    assert abs(long_pnl - 0.002) < 1e-12
    assert abs(short_pnl + 0.002) < 1e-12


def test_funding_after_partial_exit_charges_only_remaining_half():
    events = [
        {"timepoint": 20_000, "fundingRate": 0.001},
        {"timepoint": 40_000, "fundingRate": 0.001},
    ]
    pnl, count = funding_return_fraction(
        events, "LONG", 10_000, 50_000, 100.0, partial_after_ts_ms=30_000
    )
    assert abs(pnl - (-0.0015)) < 1e-12
    assert count == 2


class _FakeKuCoinCostClient:
    async def _get(self, endpoint, params=None, auth=False):
        if endpoint == "/api/v1/trade-fees":
            assert auth is True
            return {"symbol": "XBTUSDTM", "takerFeeRate": "0.00042", "makerFeeRate": "0.0002"}
        if endpoint == "/api/v1/contract/funding-rates":
            assert auth is False
            return [
                {"timepoint": 20_000, "fundingRate": 0.001},
                {"timepoint": 20_000, "fundingRate": 0.001},
                {"timepoint": 30_000, "fundingRate": -0.0005},
            ]
        raise AssertionError(endpoint)


def test_actual_fee_reader_uses_private_kucoin_rate():
    rate, source = asyncio.run(fetch_actual_taker_fee(_FakeKuCoinCostClient(), "BTCUSDT"))
    assert abs(rate - 0.00042) < 1e-12
    assert source == "kucoin_actual_fee"


def test_public_funding_reader_dedupes_by_settlement_timestamp():
    rows = asyncio.run(
        fetch_public_funding_history(_FakeKuCoinCostClient(), "BTCUSDT", 10_000, 50_000)
    )
    assert [row["timepoint"] for row in rows] == [20_000, 30_000]
    assert rows[1]["fundingRate"] == -0.0005


def test_backtest_starts_execution_on_decision_candle_and_has_no_synthetic_funding_floor():
    source = inspect.getsource(backtest._run_strategy)
    assert "for j in range(i," in source
    assert "max(1, hold * 15 / 480)" not in source
    assert "KUCOIN_MARKET_PROXY_V1" in source


def test_opportunity_blocker_classifies_observed_runtime_vetoes():
    assert opportunity._blocker_class("R:R líquido 1.35 < mínimo líquido 1.60") == "EV_RR"
    assert opportunity._blocker_class("SHORT incompatível com regime TRENDING_BULL (compat=25)") == "REGIME"
    assert opportunity._blocker_class("REJEITADO: score 58.0 < 60") == "SCORE"
    assert opportunity._blocker_class("Conflito entre timeframes") == "MTF"


def test_opportunity_audit_no_longer_uses_hardcoded_022_percent_cost():
    source = inspect.getsource(opportunity)
    assert "_ESTIMATED_ROUND_TRIP_COST_PCT = 0.22" not in source
    assert "estimated_round_trip_cost_pct" in source
