"""NEXUS-native market-language engine.

Concepts adapted from the public MIT-licensed Kronos project:
hierarchical candle tokenization, causal sequence modeling, probabilistic
sampling, and walk-forward evaluation.

This implementation is original NEXUS code. It does not import Kronos,
download model weights, call Hugging Face, or require PyTorch.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np


MIN_SEQUENCE = 64
DEFAULT_HORIZON = 4
DEFAULT_SAMPLES = 128
DEFAULT_TOP_P = 0.90


@dataclass(frozen=True)
class MarketLanguageForecast:
    probability_up: float
    probability_down: float
    probability_flat: float
    median_return: float
    q10_return: float
    q90_return: float
    dispersion: float
    entropy: float
    evidence: int
    horizon: int
    sample_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class WalkForwardReport:
    samples: int
    predicted: int
    coverage: float
    directional_accuracy: float
    brier_up: float
    median_abs_error: float

    def to_dict(self) -> dict:
        return asdict(self)


def causal_zscore(
    values: Sequence[float],
    *,
    window: int = 64,
    min_history: int = 8,
    clip: float = 5.0,
) -> np.ndarray:
    """Past-only z-score: observation i is normalized using observations < i."""
    x = np.asarray(values, dtype=float)
    out = np.zeros_like(x, dtype=float)
    for i in range(len(x)):
        if i < min_history:
            continue
        hist = x[max(0, i - window):i]
        hist = hist[np.isfinite(hist)]
        if len(hist) < min_history:
            continue
        mean = float(np.mean(hist))
        std = float(np.std(hist))
        if not math.isfinite(std) or std < 1e-12:
            continue
        out[i] = float(np.clip((x[i] - mean) / std, -clip, clip))
    return out


def _bucket5(z: float) -> int:
    if z <= -1.0:
        return 0
    if z <= -0.25:
        return 1
    if z < 0.25:
        return 2
    if z < 1.0:
        return 3
    return 4


def _bucket3(z: float, *, low: float = -0.5, high: float = 0.75) -> int:
    if z < low:
        return 0
    if z > high:
        return 2
    return 1


def hierarchical_tokens(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Encode each candle into coarse/fine discrete market-language tokens."""
    c = np.asarray(closes, dtype=float)
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    v = np.asarray(volumes, dtype=float)
    n = len(c)

    if not (len(h) == len(l) == len(v) == n):
        raise ValueError("OHLCV series must have the same length")
    if n < 2:
        raise ValueError("at least two candles are required")
    if np.any(~np.isfinite(c)) or np.any(~np.isfinite(h)) or np.any(~np.isfinite(l)):
        raise ValueError("OHLC contains non-finite values")
    if np.any(c <= 0) or np.any(h <= 0) or np.any(l <= 0):
        raise ValueError("OHLC values must be positive")
    if np.any(h < l) or np.any(c > h) or np.any(c < l):
        raise ValueError("invalid candle geometry")

    previous = np.r_[c[0], c[:-1]]
    returns = np.zeros(n, dtype=float)
    returns[1:] = np.log(c[1:] / c[:-1])

    range_ratio = (h - l) / np.maximum(previous, 1e-12)
    location = np.where(h > l, ((c - l) / (h - l)) * 2.0 - 1.0, 0.0)

    safe_volume = np.maximum(v, 0.0)
    log_volume = np.log1p(safe_volume)
    volume_change = np.zeros(n, dtype=float)
    volume_change[1:] = log_volume[1:] - log_volume[:-1]

    return_z = causal_zscore(returns)
    range_z = causal_zscore(range_ratio)
    volume_z = causal_zscore(volume_change)

    coarse: List[int] = []
    fine: List[int] = []
    for i in range(n):
        direction = _bucket5(float(return_z[i]))
        range_bucket = _bucket3(float(range_z[i]))
        if location[i] < -1.0 / 3.0:
            close_location = 0
        elif location[i] > 1.0 / 3.0:
            close_location = 2
        else:
            close_location = 1
        volume_bucket = _bucket5(float(volume_z[i]))

        # 45 coarse states = direction(5) x range(3) x close-location(3)
        coarse.append(direction * 9 + range_bucket * 3 + close_location)
        # 25 fine states = volume(5) x return-detail(5)
        fine.append(volume_bucket * 5 + direction)

    return (
        np.asarray(coarse, dtype=int),
        np.asarray(fine, dtype=int),
        returns,
        return_z,
    )


def nucleus_filter(probabilities: Sequence[float], top_p: float = DEFAULT_TOP_P) -> np.ndarray:
    """Keep the smallest high-probability set whose cumulative mass reaches top_p."""
    p = np.asarray(probabilities, dtype=float)
    if p.ndim != 1 or len(p) == 0:
        raise ValueError("probabilities must be a non-empty 1D sequence")
    if not 0.0 < top_p <= 1.0:
        raise ValueError("top_p must be in (0, 1]")

    p = np.clip(p, 0.0, None)
    total = float(p.sum())
    if total <= 0:
        return np.ones_like(p) / len(p)
    p = p / total

    order = np.argsort(p)[::-1]
    keep = np.zeros(len(p), dtype=bool)
    cumulative = 0.0
    for idx in order:
        keep[idx] = True
        cumulative += float(p[idx])
        if cumulative >= top_p:
            break

    filtered = np.where(keep, p, 0.0)
    return filtered / float(filtered.sum())


def _stable_seed(coarse: np.ndarray, fine: np.ndarray) -> int:
    seed = 2166136261
    for c, f in zip(coarse[-32:], fine[-32:]):
        seed ^= int(c) * 131 + int(f)
        seed = (seed * 16777619) & 0xFFFFFFFF
    return int(seed)


def _transition_tables(
    coarse: np.ndarray,
    fine: np.ndarray,
    return_z: np.ndarray,
    returns: np.ndarray,
):
    return_bucket = np.asarray([_bucket5(float(x)) for x in return_z], dtype=int)
    exact: Dict[Tuple[int, int], List[int]] = {}
    coarse_only: Dict[int, List[int]] = {}
    direction_only: Dict[int, List[int]] = {}
    by_bucket: Dict[int, List[float]] = {i: [] for i in range(5)}

    for t in range(8, len(coarse)):
        bucket = int(return_bucket[t])
        by_bucket[bucket].append(float(returns[t]))
        key = (int(coarse[t - 1]), int(fine[t - 1]))
        exact.setdefault(key, []).append(bucket)
        coarse_key = int(coarse[t - 1])
        coarse_only.setdefault(coarse_key, []).append(bucket)
        direction_only.setdefault(coarse_key // 9, []).append(bucket)

    return exact, coarse_only, direction_only, by_bucket, list(return_bucket[8:])


def forecast_distribution(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    *,
    horizon: int = DEFAULT_HORIZON,
    sample_count: int = DEFAULT_SAMPLES,
    top_p: float = DEFAULT_TOP_P,
) -> MarketLanguageForecast:
    """Generate a deterministic Monte-Carlo distribution from native token transitions."""
    if horizon <= 0:
        raise ValueError("horizon must be > 0")
    if sample_count < 16:
        raise ValueError("sample_count must be >= 16")

    coarse, fine, returns, return_z = hierarchical_tokens(closes, highs, lows, volumes)
    if len(coarse) < MIN_SEQUENCE:
        raise ValueError(f"at least {MIN_SEQUENCE} candles are required")

    exact, coarse_only, direction_only, by_bucket, unconditional = _transition_tables(
        coarse, fine, return_z, returns
    )
    if not unconditional:
        raise ValueError("insufficient transition history")

    rng = np.random.default_rng(_stable_seed(coarse, fine))
    terminal_returns: List[float] = []
    first_step_evidence = 0

    for _ in range(sample_count):
        current_coarse = int(coarse[-1])
        current_fine = int(fine[-1])
        cumulative_log_return = 0.0

        for step in range(horizon):
            source = exact.get((current_coarse, current_fine), [])
            if len(source) < 3:
                source = coarse_only.get(current_coarse, [])
            if len(source) < 4:
                source = direction_only.get(current_coarse // 9, [])
            if len(source) < 6:
                source = unconditional

            if step == 0:
                first_step_evidence = len(source)

            counts = np.bincount(source, minlength=5).astype(float) + 0.25
            probabilities = nucleus_filter(counts, top_p=top_p)
            bucket = int(rng.choice(5, p=probabilities))

            pool = by_bucket.get(bucket, [])
            if pool:
                sampled_return = float(pool[int(rng.integers(0, len(pool)))])
            else:
                fallback_std = float(np.std(returns[-32:]))
                sampled_return = (bucket - 2) * fallback_std

            cumulative_log_return += sampled_return

            # Autoregressive state update: sampled direction becomes the next
            # coarse/fine direction while candle-range/location/volume state persists.
            residual_coarse = current_coarse % 9
            current_coarse = bucket * 9 + residual_coarse
            volume_bucket = current_fine // 5
            current_fine = volume_bucket * 5 + bucket

        terminal_returns.append(math.exp(cumulative_log_return) - 1.0)

    paths = np.asarray(terminal_returns, dtype=float)
    p_up = float(np.mean(paths > 0))
    p_down = float(np.mean(paths < 0))
    p_flat = max(0.0, 1.0 - p_up - p_down)

    categorical = np.asarray([p_down, p_flat, p_up], dtype=float)
    nonzero = categorical[categorical > 0]
    entropy = (
        float(-np.sum(nonzero * np.log(nonzero)) / math.log(3.0))
        if len(nonzero)
        else 1.0
    )

    return MarketLanguageForecast(
        probability_up=p_up,
        probability_down=p_down,
        probability_flat=p_flat,
        median_return=float(np.median(paths)),
        q10_return=float(np.quantile(paths, 0.10)),
        q90_return=float(np.quantile(paths, 0.90)),
        dispersion=float(np.std(paths)),
        entropy=entropy,
        evidence=int(first_step_evidence),
        horizon=int(horizon),
        sample_count=int(sample_count),
    )


def signal_from_forecast(
    forecast: MarketLanguageForecast,
    *,
    probability_threshold: float = 0.62,
    min_confidence: float = 20.0,
) -> dict:
    """Translate a forecast distribution into a selective research signal."""
    major = max(forecast.probability_up, forecast.probability_down)
    edge = abs(forecast.probability_up - forecast.probability_down)
    evidence_factor = min(1.0, math.log1p(forecast.evidence) / math.log(21.0))
    confidence = (
        100.0
        * edge
        * (0.65 + 0.35 * (1.0 - forecast.entropy))
        * evidence_factor
    )

    direction = "WAIT"
    if (
        forecast.probability_up >= probability_threshold
        and forecast.median_return > 0
        and confidence >= min_confidence
    ):
        direction = "LONG"
    elif (
        forecast.probability_down >= probability_threshold
        and forecast.median_return < 0
        and confidence >= min_confidence
    ):
        direction = "SHORT"

    interval_width = max(0.0, forecast.q90_return - forecast.q10_return)
    move_scale = max(abs(forecast.median_return), 1e-6)
    dispersion_ratio = min(1.0, interval_width / (4.0 * move_scale))
    risk_score = 100.0 * min(
        1.0, 0.65 * forecast.entropy + 0.35 * dispersion_ratio
    )

    return {
        "direction": direction,
        "confidence": round(float(confidence), 2),
        "risk_score": round(float(risk_score), 2),
        "major_probability": round(float(major), 4),
    }


def walk_forward_evaluate(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    *,
    start: int = 96,
    horizon: int = DEFAULT_HORIZON,
    step: int = 4,
    sample_count: int = 64,
) -> WalkForwardReport:
    """Past-only evaluation of the native market-language forecast."""
    c = np.asarray(closes, dtype=float)
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    v = np.asarray(volumes, dtype=float)

    probabilities: List[float] = []
    outcomes: List[float] = []
    abs_errors: List[float] = []
    correct = 0
    predicted = 0

    for end in range(max(start, MIN_SEQUENCE), len(c) - horizon, step):
        forecast = forecast_distribution(
            c[:end], h[:end], l[:end], v[:end],
            horizon=horizon, sample_count=sample_count,
        )
        actual_return = float(c[end + horizon - 1] / c[end - 1] - 1.0)
        outcome_up = 1.0 if actual_return > 0 else 0.0
        probabilities.append(forecast.probability_up)
        outcomes.append(outcome_up)
        abs_errors.append(abs(forecast.median_return - actual_return))

        signal = signal_from_forecast(forecast)
        if signal["direction"] != "WAIT":
            predicted += 1
            if (
                signal["direction"] == "LONG" and actual_return > 0
            ) or (
                signal["direction"] == "SHORT" and actual_return < 0
            ):
                correct += 1

    total = len(probabilities)
    if total == 0:
        return WalkForwardReport(0, 0, 0.0, 0.0, 0.0, 0.0)

    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return WalkForwardReport(
        samples=total,
        predicted=predicted,
        coverage=predicted / total,
        directional_accuracy=(correct / predicted) if predicted else 0.0,
        brier_up=float(np.mean((p - y) ** 2)),
        median_abs_error=float(np.median(abs_errors)),
    )
