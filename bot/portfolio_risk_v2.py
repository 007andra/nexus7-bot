"""Research-only portfolio risk diagnostics v2.

Implements shrinkage covariance, factor beta, marginal risk contribution,
cluster concentration and historical Expected Shortfall. No LIVE sizing or
entry authority is exposed here.
"""
from __future__ import annotations

from math import isfinite, sqrt
from typing import Mapping, Sequence


def _validate_matrix(returns: Mapping[str, Sequence[float]]) -> tuple[list[str], int]:
    symbols = sorted(returns)
    if not symbols:
        raise ValueError("returns are required")
    n = len(returns[symbols[0]])
    if n < 3:
        raise ValueError("at least 3 observations are required")
    for sym in symbols:
        vals = list(map(float, returns[sym]))
        if len(vals) != n or not all(isfinite(v) for v in vals):
            raise ValueError("return series must be finite and aligned")
    return symbols, n


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def covariance_matrix(returns: Mapping[str, Sequence[float]], shrinkage: float = 0.15) -> dict[str, dict[str, float]]:
    symbols, n = _validate_matrix(returns)
    lam = float(shrinkage)
    if not 0.0 <= lam <= 1.0:
        raise ValueError("shrinkage must be in [0,1]")
    means = {s: _mean(list(map(float, returns[s]))) for s in symbols}
    raw: dict[str, dict[str, float]] = {s: {} for s in symbols}
    variances: dict[str, float] = {}
    for a in symbols:
        for b in symbols:
            cov = sum((float(returns[a][i]) - means[a]) * (float(returns[b][i]) - means[b]) for i in range(n)) / (n - 1)
            raw[a][b] = cov
        variances[a] = raw[a][a]
    avg_var = _mean(list(variances.values()))
    out: dict[str, dict[str, float]] = {s: {} for s in symbols}
    for a in symbols:
        for b in symbols:
            target = avg_var if a == b else 0.0
            out[a][b] = (1.0 - lam) * raw[a][b] + lam * target
    return out


def beta(asset: Sequence[float], benchmark: Sequence[float]) -> float:
    a, b = list(map(float, asset)), list(map(float, benchmark))
    if len(a) != len(b) or len(a) < 3:
        raise ValueError("aligned samples required")
    ma, mb = _mean(a), _mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)
    var = sum((y - mb) ** 2 for y in b) / (len(b) - 1)
    return 0.0 if var <= 0 else cov / var


def risk_contributions(weights: Mapping[str, float], covariance: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    symbols = sorted(weights)
    w = {s: float(weights[s]) for s in symbols}
    marginal = {s: sum(float(covariance[s][t]) * w[t] for t in symbols) for s in symbols}
    variance = sum(w[s] * marginal[s] for s in symbols)
    if variance <= 0:
        return {s: 0.0 for s in symbols}
    return {s: (w[s] * marginal[s]) / variance for s in symbols}


def expected_shortfall(portfolio_returns: Sequence[float], confidence: float = 0.95) -> float:
    vals = sorted(map(float, portfolio_returns))
    if not vals:
        raise ValueError("returns are required")
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be between .5 and 1")
    tail_n = max(1, int(round(len(vals) * (1.0 - confidence))))
    return -_mean(vals[:tail_n])


def cluster_concentration(weights: Mapping[str, float], clusters: Mapping[str, str]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for symbol, weight in weights.items():
        cluster = clusters.get(symbol, symbol)
        totals[cluster] = totals.get(cluster, 0.0) + abs(float(weight))
    gross = sum(totals.values())
    return {k: (v / gross if gross else 0.0) for k, v in sorted(totals.items())}


def portfolio_snapshot(returns: Mapping[str, Sequence[float]], weights: Mapping[str, float], *, btc_returns: Sequence[float] | None = None, eth_returns: Sequence[float] | None = None, clusters: Mapping[str, str] | None = None, shrinkage: float = 0.15) -> dict[str, object]:
    cov = covariance_matrix(returns, shrinkage=shrinkage)
    symbols, n = _validate_matrix(returns)
    if set(symbols) != set(weights):
        raise ValueError("weights must cover exactly the return symbols")
    portfolio = [sum(float(weights[s]) * float(returns[s][i]) for s in symbols) for i in range(n)]
    variance = sum(float(weights[a]) * float(weights[b]) * cov[a][b] for a in symbols for b in symbols)
    return {
        "volatility": sqrt(max(0.0, variance)),
        "expected_shortfall_95": expected_shortfall(portfolio, 0.95),
        "risk_contribution": risk_contributions(weights, cov),
        "cluster_concentration": cluster_concentration(weights, clusters or {}),
        "beta_btc": {s: beta(returns[s], btc_returns) for s in symbols} if btc_returns is not None else None,
        "beta_eth": {s: beta(returns[s], eth_returns) for s in symbols} if eth_returns is not None else None,
        "covariance": cov,
        "promotion_authority": False,
        "execution_effect": "NONE",
    }
