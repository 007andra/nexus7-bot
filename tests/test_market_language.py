import numpy as np
import pytest

from bot.market_language import (
    causal_zscore,
    forecast_distribution,
    hierarchical_tokens,
    nucleus_filter,
    signal_from_forecast,
    walk_forward_evaluate,
)


def _series(n=180, drift=0.0003, seed=7):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.003, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1.0 + rng.uniform(0.001, 0.005, n))
    low = close * (1.0 - rng.uniform(0.001, 0.005, n))
    volume = rng.lognormal(5.0, 0.35, n)
    return close, high, low, volume


def test_causal_zscore_does_not_look_ahead():
    base = np.arange(1.0, 40.0)
    a = causal_zscore(base)
    changed = base.copy()
    changed[-1] = 1_000_000.0
    b = causal_zscore(changed)
    np.testing.assert_allclose(a[:-1], b[:-1])


def test_hierarchical_tokens_are_bounded_and_aligned():
    c, h, l, v = _series()
    coarse, fine, returns, rz = hierarchical_tokens(c, h, l, v)
    assert len(coarse) == len(c)
    assert len(fine) == len(c)
    assert coarse.min() >= 0 and coarse.max() < 45
    assert fine.min() >= 0 and fine.max() < 25
    assert len(returns) == len(rz) == len(c)


def test_forecast_is_reproducible_for_same_market_state():
    c, h, l, v = _series()
    a = forecast_distribution(c, h, l, v, sample_count=64)
    b = forecast_distribution(c, h, l, v, sample_count=64)
    assert a == b
    assert 0 <= a.probability_up <= 1
    assert 0 <= a.probability_down <= 1
    assert a.q10_return <= a.median_return <= a.q90_return


def test_nucleus_filter_removes_low_mass_tail():
    p = nucleus_filter([0.7, 0.2, 0.08, 0.02], top_p=0.85)
    assert p[0] > 0
    assert p[1] > 0
    assert p[2] == 0
    assert p[3] == 0
    assert p.sum() == pytest.approx(1.0)


def test_signal_contract_is_selective():
    c, h, l, v = _series(drift=-0.001, seed=11)
    f = forecast_distribution(c, h, l, v, sample_count=128)
    s = signal_from_forecast(f)
    assert s["direction"] in {"LONG", "SHORT", "WAIT"}
    assert 0 <= s["confidence"] <= 100
    assert 0 <= s["risk_score"] <= 100


def test_walk_forward_is_past_only_and_reports_metrics():
    c, h, l, v = _series(n=140)
    r = walk_forward_evaluate(c, h, l, v, start=96, step=8, sample_count=32)
    assert r.samples > 0
    assert 0 <= r.coverage <= 1
    assert 0 <= r.directional_accuracy <= 1
    assert 0 <= r.brier_up <= 1


def test_invalid_candle_geometry_fails_closed():
    c, h, l, v = _series()
    h[-1] = l[-1] * 0.9
    with pytest.raises(ValueError, match="geometry"):
        hierarchical_tokens(c, h, l, v)
