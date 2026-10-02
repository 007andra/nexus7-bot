"""Kronos-inspired probabilistic forecast features for NEXUS-7 shadow research."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import math
from statistics import median, pstdev
from typing import Iterable, Mapping, Sequence

@dataclass(frozen=True)
class ForecastFeatures:
    sample_count: int
    reference_price: float
    horizon_steps: int
    probability_up: float
    probability_down: float
    median_return: float
    mean_return: float
    return_dispersion: float
    median_max_upside: float
    median_max_downside: float
    confidence: float

    def as_dict(self) -> dict:
        return asdict(self)

def _finite_positive(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and > 0")
    return result

def summarize_forecast_paths(paths: Iterable[Sequence[Mapping[str, object]]], *, reference_price: float) -> ForecastFeatures:
    """Reduce probabilistic OHLC forecast paths to execution-neutral features."""
    ref = _finite_positive(reference_price, "reference_price")
    normalized = list(paths)
    if not normalized:
        raise ValueError("at least one forecast path is required")
    horizon = len(normalized[0])
    if horizon <= 0:
        raise ValueError("forecast paths must not be empty")
    if any(len(path) != horizon for path in normalized):
        raise ValueError("all forecast paths must share the same horizon")

    terminal_returns, max_upsides, max_downsides = [], [], []
    for path in normalized:
        closes, highs, lows = [], [], []
        for candle in path:
            close = _finite_positive(candle.get("close"), "close")
            high = _finite_positive(candle.get("high", close), "high")
            low = _finite_positive(candle.get("low", close), "low")
            if high < low:
                raise ValueError("forecast candle high must be >= low")
            closes.append(close); highs.append(high); lows.append(low)
        terminal_returns.append(closes[-1] / ref - 1.0)
        max_upsides.append(max(highs) / ref - 1.0)
        max_downsides.append(min(lows) / ref - 1.0)

    n = len(terminal_returns)
    p_up = sum(ret > 0 for ret in terminal_returns) / n
    p_down = sum(ret < 0 for ret in terminal_returns) / n
    dispersion = pstdev(terminal_returns) if n > 1 else 0.0
    consensus = max(p_up, p_down)
    scale = abs(median(terminal_returns))
    dispersion_penalty = dispersion / (dispersion + scale + 1e-12)
    confidence = max(0.0, min(1.0, consensus * (1.0 - dispersion_penalty)))
    return ForecastFeatures(n, ref, horizon, p_up, p_down, median(terminal_returns),
        sum(terminal_returns)/n, dispersion, median(max_upsides), median(max_downsides), confidence)

def shadow_record(symbol: str, timeframe: str, features: ForecastFeatures) -> dict:
    """Build telemetry only; this function cannot return an order decision."""
    return {"schema":"nexus.kronos_shadow.v1","mode":"shadow","execution_authority":False,
            "symbol":str(symbol).upper(),"timeframe":str(timeframe),"forecast":features.as_dict()}
