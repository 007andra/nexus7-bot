import pytest

from bot.research_statistics import (
    bootstrap_mean_ci,
    max_drawdown,
    monte_carlo_trade_paths,
    performance_metrics,
)


def test_bootstrap_is_deterministic():
    values = [0.01, -0.005, 0.02, -0.01, 0.015] * 10
    a = bootstrap_mean_ci(values, n_bootstrap=1000, seed=7)
    b = bootstrap_mean_ci(values, n_bootstrap=1000, seed=7)
    assert a == b
    assert a["low"] <= a["mean"] <= a["high"]


def test_monte_carlo_is_deterministic_and_bounded():
    values = [0.01, -0.005, 0.02, -0.01] * 20
    a = monte_carlo_trade_paths(values, paths=500, seed=9)
    b = monte_carlo_trade_paths(values, paths=500, seed=9)
    assert a == b
    assert 0.0 <= a.median_max_drawdown < 1.0
    assert a.p05_terminal_return <= a.median_terminal_return <= a.p95_terminal_return


def test_performance_metrics_include_tail_and_drawdown():
    metrics = performance_metrics([0.10, -0.05, 0.02, -0.03])
    assert metrics["trades"] == 4
    assert metrics["profit_factor"] > 1.0
    assert metrics["max_drawdown"] > 0.0
    assert metrics["cvar_5"] <= 0.0


def test_return_below_minus_100_rejected():
    with pytest.raises(ValueError):
        max_drawdown([0.1, -1.1])
