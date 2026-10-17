"""Research metrics spanning execution quality, excursions and risk paths."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import random
from typing import Sequence


@dataclass(frozen=True)
class Excursion:
    mfe_r: float
    mae_r: float


def mfe_mae(entry: float, stop: float, prices: Sequence[float], side: str) -> Excursion:
    entry, stop = float(entry), float(stop)
    risk = abs(entry - stop)
    if risk <= 0 or not prices:
        raise ValueError("positive risk and prices required")
    direction = 1.0 if side.upper() in {"LONG", "BUY"} else -1.0
    rs = [direction * (float(p) - entry) / risk for p in prices]
    if not all(isfinite(v) for v in rs):
        raise ValueError("non-finite price")
    return Excursion(mfe_r=max(rs), mae_r=min(rs))


def margin_efficiency(expected_net_r: float, risk_usdt: float, required_margin_usdt: float) -> float:
    if required_margin_usdt <= 0:
        raise ValueError("required_margin_usdt must be positive")
    return float(expected_net_r) * float(risk_usdt) / float(required_margin_usdt)


def execution_quality_score(expected_entry: float, actual_fill: float, expected_slippage_bps: float, protection_latency_ms: float) -> dict[str, float]:
    expected_entry = float(expected_entry)
    if expected_entry <= 0:
        raise ValueError("expected_entry must be positive")
    realized_bps = abs(float(actual_fill) - expected_entry) / expected_entry * 10_000.0
    slip_error = abs(realized_bps - float(expected_slippage_bps))
    slip_component = max(0.0, 100.0 - min(100.0, slip_error * 4.0))
    latency_component = max(0.0, 100.0 - min(100.0, max(0.0, float(protection_latency_ms) - 250.0) / 20.0))
    score = 0.7 * slip_component + 0.3 * latency_component
    return {"score": round(score, 4), "realized_slippage_bps": realized_bps, "slippage_error_bps": slip_error}


def monte_carlo_paths(r_multiples: Sequence[float], *, risk_fraction: float, starting_equity: float = 1.0, trades_per_path: int = 200, paths: int = 2000, seed: int = 7, ruin_fraction: float = 0.5) -> dict[str, float]:
    rs = [float(x) for x in r_multiples]
    if not rs or not all(isfinite(x) for x in rs):
        raise ValueError("finite r_multiples required")
    if not 0 < risk_fraction < 1 or starting_equity <= 0 or not 0 < ruin_fraction < 1:
        raise ValueError("invalid simulation parameters")
    if paths <= 0 or trades_per_path <= 0:
        raise ValueError("paths and trades_per_path must be positive")
    rng = random.Random(seed)
    max_drawdowns: list[float] = []
    finals: list[float] = []
    ruins = 0
    for _ in range(paths):
        equity = float(starting_equity)
        peak = equity
        max_dd = 0.0
        ruined = False
        for _ in range(trades_per_path):
            r = rs[rng.randrange(len(rs))]
            equity *= max(0.0, 1.0 + risk_fraction * r)
            peak = max(peak, equity)
            dd = 0.0 if peak <= 0 else 1.0 - equity / peak
            max_dd = max(max_dd, dd)
            if equity <= starting_equity * ruin_fraction:
                ruined = True
        max_drawdowns.append(max_dd)
        finals.append(equity)
        ruins += int(ruined)
    max_drawdowns.sort()
    finals.sort()

    def q(vals: Sequence[float], p: float) -> float:
        return vals[min(len(vals) - 1, int(p * (len(vals) - 1)))]

    return {
        "ruin_probability": ruins / paths,
        "median_final_equity": q(finals, 0.5),
        "p05_final_equity": q(finals, 0.05),
        "median_max_drawdown": q(max_drawdowns, 0.5),
        "p95_max_drawdown": q(max_drawdowns, 0.95),
        "paths": float(paths),
    }
