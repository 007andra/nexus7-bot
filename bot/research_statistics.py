"""Deterministic, dependency-light research statistics for NEXUS.

All inputs are decimal returns (0.01 == +1%). Research-only; no runtime authority.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def _returns(values) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    if arr.ndim != 1:
        raise ValueError("returns must be one-dimensional")
    if arr.size and not np.isfinite(arr).all():
        raise ValueError("returns contain non-finite values")
    return arr


def equity_curve(returns) -> np.ndarray:
    arr = _returns(returns)
    if not arr.size:
        return np.asarray([1.0])
    if np.any(arr <= -1.0):
        raise ValueError("return <= -100% is not supported")
    return np.concatenate(([1.0], np.cumprod(1.0 + arr)))


def max_drawdown(returns) -> float:
    curve = equity_curve(returns)
    peaks = np.maximum.accumulate(curve)
    dd = 1.0 - curve / peaks
    return float(np.max(dd)) if dd.size else 0.0


def cvar(returns, alpha: float = 0.05) -> float:
    arr = _returns(returns)
    if not arr.size:
        return 0.0
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    cutoff = float(np.quantile(arr, alpha))
    tail = arr[arr <= cutoff]
    return float(np.mean(tail)) if tail.size else cutoff


def performance_metrics(returns, periods_per_year: float = 1.0) -> dict:
    """Per-trade metrics; pass a measured annual frequency to annualize ratios."""
    arr = _returns(returns)
    if not arr.size:
        return {
            "trades": 0, "expectancy": 0.0, "win_rate": 0.0,
            "profit_factor": 0.0, "sharpe": 0.0, "sortino": 0.0,
            "max_drawdown": 0.0, "cvar_5": 0.0,
        }
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gross_win = float(np.sum(wins)) if wins.size else 0.0
    gross_loss = abs(float(np.sum(losses))) if losses.size else 0.0
    std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    downside = arr[arr < 0]
    downside_std = float(np.std(downside, ddof=1)) if downside.size > 1 else 0.0
    annualizer = math.sqrt(float(periods_per_year))
    mean = float(np.mean(arr))
    return {
        "trades": int(arr.size),
        "expectancy": mean,
        "win_rate": float(np.mean(arr > 0)),
        "profit_factor": (
            gross_win / gross_loss
            if gross_loss > 0
            else (math.inf if gross_win > 0 else 0.0)
        ),
        "sharpe": mean / std * annualizer if std > 0 else 0.0,
        "sortino": mean / downside_std * annualizer if downside_std > 0 else 0.0,
        "max_drawdown": max_drawdown(arr),
        "cvar_5": cvar(arr, 0.05),
    }


def bootstrap_mean_ci(
    returns,
    *,
    confidence: float = 0.95,
    n_bootstrap: int = 5000,
    seed: int = 42,
) -> dict:
    arr = _returns(returns)
    if not arr.size:
        return {"mean": 0.0, "low": 0.0, "high": 0.0, "n": 0}
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    if n_bootstrap <= 0:
        raise ValueError("n_bootstrap must be positive")
    rng = np.random.default_rng(seed)
    samples = rng.choice(arr, size=(int(n_bootstrap), arr.size), replace=True)
    means = np.mean(samples, axis=1)
    tail = (1.0 - confidence) / 2.0
    return {
        "mean": float(np.mean(arr)),
        "low": float(np.quantile(means, tail)),
        "high": float(np.quantile(means, 1.0 - tail)),
        "n": int(arr.size),
    }


@dataclass(frozen=True)
class MonteCarloSummary:
    paths: int
    trades_per_path: int
    median_terminal_return: float
    p05_terminal_return: float
    p95_terminal_return: float
    median_max_drawdown: float
    p95_max_drawdown: float


def monte_carlo_trade_paths(
    returns,
    *,
    paths: int = 5000,
    trades_per_path: int | None = None,
    seed: int = 42,
) -> MonteCarloSummary:
    """Bootstrap whole-trade returns to estimate path and drawdown dispersion."""
    arr = _returns(returns)
    if not arr.size:
        return MonteCarloSummary(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if np.any(arr <= -1.0):
        raise ValueError("return <= -100% is not supported")
    if paths <= 0:
        raise ValueError("paths must be positive")
    horizon = int(trades_per_path or arr.size)
    if horizon <= 0:
        raise ValueError("trades_per_path must be positive")
    rng = np.random.default_rng(seed)
    samples = rng.choice(arr, size=(int(paths), horizon), replace=True)
    paths_curve = np.cumprod(1.0 + samples, axis=1)
    terminal = paths_curve[:, -1] - 1.0
    curves = np.concatenate((np.ones((int(paths), 1)), paths_curve), axis=1)
    peaks = np.maximum.accumulate(curves, axis=1)
    drawdowns = np.max(1.0 - curves / peaks, axis=1)
    return MonteCarloSummary(
        paths=int(paths),
        trades_per_path=horizon,
        median_terminal_return=float(np.median(terminal)),
        p05_terminal_return=float(np.quantile(terminal, 0.05)),
        p95_terminal_return=float(np.quantile(terminal, 0.95)),
        median_max_drawdown=float(np.median(drawdowns)),
        p95_max_drawdown=float(np.quantile(drawdowns, 0.95)),
    )
