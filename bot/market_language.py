"""Native NEXUS market-language model.

Independent NEXUS implementation of useful market-sequence ideas:
hierarchical candle states, causal normalization, temporal context,
autoregressive next-state forecasting, nucleus sampling and walk-forward
evaluation. No Kronos package, weights, Hugging Face service or runtime
dependency is used.

The model has no execution authority. It returns a normal ModelOutput and all
existing NEXUS score/risk/execution gates remain downstream.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
import random
from statistics import median, pstdev
from typing import Iterable, Mapping, Sequence

from bot.nexus_types import Decision, ModelOutput


@dataclass(frozen=True)
class TokenObservation:
    token: int
    coarse: int
    fine: int
    realized_return: float
    range_return: float
    timestamp_s: float | None


@dataclass(frozen=True)
class ForecastDistribution:
    available: bool
    reason: str
    sample_count: int
    horizon: int
    probability_up: float
    probability_down: float
    mean_return: float
    median_return: float
    q10_return: float
    q90_return: float
    dispersion: float
    median_max_upside: float
    median_max_downside: float
    entropy: float
    confidence: float
    token_count: int
    unique_tokens: int

    def as_dict(self) -> dict:
        return asdict(self)


def _f(value: object, *, default: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError("non-numeric candle value")
    if not math.isfinite(result):
        if default is not None:
            return default
        raise ValueError("non-finite candle value")
    return result


def _timestamp_seconds(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    if ts > 1e11:
        ts /= 1000.0
    return ts


def _quantize(value: float, edges: Sequence[float]) -> int:
    for idx, edge in enumerate(edges):
        if value < edge:
            return idx
    return len(edges)


def temporal_context(timestamp_s: float | None) -> tuple[int, int, int, int, int] | None:
    """UTC minute/hour/weekday/day/month context; deterministic and timezone-safe."""
    if timestamp_s is None:
        return None
    dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
    return (dt.minute, dt.hour, dt.weekday(), dt.day, dt.month)


def _robust_scale(values: Sequence[float], floor: float) -> float:
    if not values:
        return floor
    med = median(values)
    deviations = [abs(v - med) for v in values]
    mad = median(deviations) if deviations else 0.0
    return max(floor, mad * 1.4826, abs(med) * 0.25)


def tokenize_candles(
    candles: Sequence[Mapping[str, object]],
    *,
    window: int = 64,
) -> list[TokenObservation]:
    """Causally tokenize candles into coarse/fine market states.

    Every observation at index i is normalized only with data from indexes < i.
    Mutating any future candle therefore cannot alter an already-produced token.
    """
    if window < 8:
        raise ValueError("window must be >= 8")
    rows = list(candles)
    if len(rows) < 2:
        return []

    closes: list[float] = []
    volumes: list[float] = []
    raw_returns: list[float] = []
    out: list[TokenObservation] = []

    first_close = _f(rows[0].get("c", rows[0].get("close")))
    if first_close <= 0:
        raise ValueError("close must be > 0")
    closes.append(first_close)
    volumes.append(max(0.0, _f(rows[0].get("v", rows[0].get("volume", 0.0)), default=0.0)))

    for i in range(1, len(rows)):
        row = rows[i]
        prev_close = closes[-1]
        close = _f(row.get("c", row.get("close")))
        open_ = _f(row.get("o", row.get("open", prev_close)), default=prev_close)
        high = _f(row.get("h", row.get("high", max(open_, close))), default=max(open_, close))
        low = _f(row.get("l", row.get("low", min(open_, close))), default=min(open_, close))
        volume = max(0.0, _f(row.get("v", row.get("volume", 0.0)), default=0.0))
        if min(prev_close, open_, close, high, low) <= 0 or high < low:
            raise ValueError("invalid OHLC geometry")
        if close > high or close < low or open_ > high or open_ < low:
            raise ValueError("open/close outside candle range")

        realized = math.log(close / prev_close)
        range_ret = (high - low) / prev_close
        body_ret = (close - open_) / prev_close
        upper_wick = max(0.0, high - max(open_, close)) / prev_close
        lower_wick = max(0.0, min(open_, close) - low) / prev_close

        hist_returns = raw_returns[max(0, len(raw_returns) - window):]
        ret_scale = _robust_scale([abs(x) for x in hist_returns], 1e-4)
        ret_norm = realized / ret_scale
        range_norm = range_ret / ret_scale

        hist_volumes = volumes[max(0, len(volumes) - window):]
        if len(hist_volumes) >= 8:
            vol_med = median(hist_volumes)
            vol_scale = _robust_scale(hist_volumes, max(1e-9, abs(vol_med) * 0.05))
            volume_z = (volume - vol_med) / vol_scale
        else:
            volume_z = 0.0

        direction_bin = _quantize(ret_norm, (-0.35, 0.35))
        volatility_bin = _quantize(range_norm, (1.25, 2.75))
        volume_bin = _quantize(volume_z, (-0.75, 0.75))
        coarse = direction_bin * 9 + volatility_bin * 3 + volume_bin

        candle_range = max(high - low, 1e-12)
        wick_skew = (upper_wick - lower_wick) * prev_close / candle_range
        body_strength = abs(body_ret) / max(range_ret, 1e-12)
        return_bin = _quantize(ret_norm, (-2.0, -1.0, -0.35, 0.35, 1.0, 2.0))
        wick_bin = _quantize(wick_skew, (-0.55, -0.15, 0.15, 0.55))
        body_bin = _quantize(body_strength, (0.35, 0.70))
        fine = return_bin * 15 + wick_bin * 3 + body_bin
        token = coarse * 128 + fine

        out.append(
            TokenObservation(
                token=token,
                coarse=coarse,
                fine=fine,
                realized_return=realized,
                range_return=range_ret,
                timestamp_s=_timestamp_seconds(
                    row.get("ts", row.get("time", row.get("timestamp")))
                ),
            )
        )
        closes.append(close)
        volumes.append(volume)
        raw_returns.append(realized)

    return out


class MarketLanguageModel:
    """Small causal autoregressive transition model over hierarchical states."""

    def __init__(self, observations: Sequence[TokenObservation], *, order: int = 3):
        if order < 1 or order > 6:
            raise ValueError("order must be between 1 and 6")
        self.observations = list(observations)
        self.order = order
        self.transitions: dict[tuple, Counter[int]] = defaultdict(Counter)
        self.token_returns: dict[int, list[float]] = defaultdict(list)
        self.token_ranges: dict[int, list[float]] = defaultdict(list)
        self.unconditional: Counter[int] = Counter()
        self.step_seconds = self._infer_step_seconds()
        self._fit()

    def _infer_step_seconds(self) -> float:
        stamps = [o.timestamp_s for o in self.observations if o.timestamp_s is not None]
        deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        return median(deltas) if deltas else 900.0

    def _fit(self) -> None:
        obs = self.observations
        for item in obs:
            self.token_returns[item.token].append(item.realized_return)
            self.token_ranges[item.token].append(item.range_return)
            self.unconditional[item.token] += 1
        for i in range(1, len(obs)):
            next_token = obs[i].token
            temporal = temporal_context(obs[i].timestamp_s)
            temporal_key = None if temporal is None else (temporal[1], temporal[2])
            max_order = min(self.order, i)
            for size in range(1, max_order + 1):
                context = tuple(x.token for x in obs[i-size:i])
                if temporal_key is not None:
                    self.transitions[(size, context, temporal_key)][next_token] += 1
                self.transitions[(size, context, None)][next_token] += 1

    def next_counts(
        self,
        history: Sequence[int],
        *,
        timestamp_s: float | None,
    ) -> Counter[int]:
        temporal = temporal_context(timestamp_s)
        temporal_key = None if temporal is None else (temporal[1], temporal[2])
        max_order = min(self.order, len(history))
        for size in range(max_order, 0, -1):
            context = tuple(history[-size:])
            if temporal_key is not None:
                counts = self.transitions.get((size, context, temporal_key))
                if counts:
                    return counts.copy()
            counts = self.transitions.get((size, context, None))
            if counts:
                return counts.copy()
        return self.unconditional.copy()


def _nucleus_distribution(
    counts: Counter[int],
    *,
    temperature: float,
    top_p: float,
) -> list[tuple[int, float]]:
    if not counts:
        return []
    temperature = max(0.05, float(temperature))
    top_p = min(1.0, max(0.05, float(top_p)))
    weighted = []
    for token, count in counts.items():
        weight = float(count) ** (1.0 / temperature)
        weighted.append((token, weight))
    total = sum(w for _, w in weighted)
    if total <= 0:
        return []
    ranked = sorted(((t, w / total) for t, w in weighted), key=lambda x: x[1], reverse=True)
    kept: list[tuple[int, float]] = []
    cumulative = 0.0
    for item in ranked:
        kept.append(item)
        cumulative += item[1]
        if cumulative >= top_p:
            break
    kept_total = sum(p for _, p in kept)
    return [(t, p / kept_total) for t, p in kept]


def _sample(distribution: Sequence[tuple[int, float]], rng: random.Random) -> int:
    r = rng.random()
    cumulative = 0.0
    for token, probability in distribution:
        cumulative += probability
        if r <= cumulative:
            return token
    return distribution[-1][0]


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _stable_seed(observations: Sequence[TokenObservation], horizon: int, sample_count: int) -> int:
    material = ",".join(str(x.token) for x in observations[-64:])
    material += f"|{horizon}|{sample_count}|{observations[-1].timestamp_s if observations else 0}"
    digest = hashlib.blake2b(material.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def forecast_market_language(
    candles: Sequence[Mapping[str, object]],
    *,
    horizon: int = 4,
    sample_count: int = 96,
    order: int = 3,
    temperature: float = 0.85,
    top_p: float = 0.90,
    min_tokens: int = 80,
) -> ForecastDistribution:
    if horizon < 1 or horizon > 48:
        raise ValueError("horizon must be between 1 and 48")
    if sample_count < 16 or sample_count > 2048:
        raise ValueError("sample_count must be between 16 and 2048")

    observations = tokenize_candles(candles)
    if len(observations) < min_tokens:
        return ForecastDistribution(
            available=False,
            reason=f"DATA_UNAVAILABLE: tokens={len(observations)}<{min_tokens}",
            sample_count=0,
            horizon=horizon,
            probability_up=0.0,
            probability_down=0.0,
            mean_return=0.0,
            median_return=0.0,
            q10_return=0.0,
            q90_return=0.0,
            dispersion=0.0,
            median_max_upside=0.0,
            median_max_downside=0.0,
            entropy=1.0,
            confidence=0.0,
            token_count=len(observations),
            unique_tokens=len({x.token for x in observations}),
        )

    model = MarketLanguageModel(observations, order=order)
    history0 = [x.token for x in observations]
    rng = random.Random(_stable_seed(observations, horizon, sample_count))
    terminal_returns: list[float] = []
    max_upsides: list[float] = []
    max_downsides: list[float] = []
    first_distribution: list[tuple[int, float]] = []
    last_ts = observations[-1].timestamp_s

    for sample_idx in range(sample_count):
        history = list(history0)
        cumulative_log_return = 0.0
        path_returns: list[float] = []
        for step in range(1, horizon + 1):
            future_ts = None if last_ts is None else last_ts + model.step_seconds * step
            counts = model.next_counts(history, timestamp_s=future_ts)
            distribution = _nucleus_distribution(
                counts, temperature=temperature, top_p=top_p
            )
            if not distribution:
                break
            if sample_idx == 0 and step == 1:
                first_distribution = distribution
            token = _sample(distribution, rng)
            returns = model.token_returns.get(token) or [0.0]
            realized = returns[rng.randrange(len(returns))]
            # Bound a single synthetic step to protect against corrupt/extreme
            # historical samples dominating the scenario distribution.
            realized = max(-0.20, min(0.20, realized))
            cumulative_log_return += realized
            simple_return = math.exp(cumulative_log_return) - 1.0
            path_returns.append(simple_return)
            history.append(token)
        if not path_returns:
            continue
        terminal_returns.append(path_returns[-1])
        max_upsides.append(max(path_returns))
        max_downsides.append(min(path_returns))

    if not terminal_returns:
        return ForecastDistribution(
            available=False,
            reason="NO_FORECAST_PATHS",
            sample_count=0,
            horizon=horizon,
            probability_up=0.0,
            probability_down=0.0,
            mean_return=0.0,
            median_return=0.0,
            q10_return=0.0,
            q90_return=0.0,
            dispersion=0.0,
            median_max_upside=0.0,
            median_max_downside=0.0,
            entropy=1.0,
            confidence=0.0,
            token_count=len(observations),
            unique_tokens=len({x.token for x in observations}),
        )

    n = len(terminal_returns)
    p_up = sum(x > 0 for x in terminal_returns) / n
    p_down = sum(x < 0 for x in terminal_returns) / n
    mean_ret = sum(terminal_returns) / n
    med_ret = median(terminal_returns)
    dispersion = pstdev(terminal_returns) if n > 1 else 0.0

    entropy = 1.0
    if first_distribution:
        raw_entropy = -sum(p * math.log(max(p, 1e-12)) for _, p in first_distribution)
        entropy = raw_entropy / max(math.log(max(2, len(first_distribution))), 1e-12)
        entropy = max(0.0, min(1.0, entropy))

    dominant = max(p_up, p_down)
    directional_edge = max(0.0, (dominant - 0.5) * 2.0)
    signal_to_noise = abs(med_ret) / (dispersion + abs(med_ret) + 1e-12)
    confidence = 100.0 * directional_edge * (0.55 + 0.45 * signal_to_noise) * (1.0 - 0.35 * entropy)
    confidence = max(0.0, min(100.0, confidence))

    min_material_move = max(0.0004, dispersion * 0.08)
    available = (
        dominant >= 0.58
        and abs(med_ret) >= min_material_move
        and confidence >= 12.0
    )
    reason = "OK" if available else (
        f"ABSTAIN: dominant={dominant:.3f} median={med_ret:+.5f} "
        f"dispersion={dispersion:.5f} confidence={confidence:.1f}"
    )

    return ForecastDistribution(
        available=available,
        reason=reason,
        sample_count=n,
        horizon=horizon,
        probability_up=p_up,
        probability_down=p_down,
        mean_return=mean_ret,
        median_return=med_ret,
        q10_return=_quantile(terminal_returns, 0.10),
        q90_return=_quantile(terminal_returns, 0.90),
        dispersion=dispersion,
        median_max_upside=median(max_upsides),
        median_max_downside=median(max_downsides),
        entropy=entropy,
        confidence=confidence,
        token_count=len(observations),
        unique_tokens=len({x.token for x in observations}),
    )


def model_market_language(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    *,
    opens: Sequence[float] | None = None,
    timestamps: Sequence[object] | None = None,
) -> ModelOutput:
    """MODEL H: native market-language vote with explicit low-edge abstention."""
    m = ModelOutput(name="MARKET_LANGUAGE")
    n = min(len(closes), len(highs), len(lows), len(volumes))
    if n < 81:
        m.available = False
        m.reason = f"DATA_UNAVAILABLE: candles={n}<81"
        return m

    opens = list(opens) if opens is not None else []
    timestamps = list(timestamps) if timestamps is not None else []
    candles = []
    for i in range(n):
        candles.append({
            "o": opens[i] if i < len(opens) else (closes[i - 1] if i > 0 else closes[i]),
            "h": highs[i],
            "l": lows[i],
            "c": closes[i],
            "v": volumes[i],
            "ts": timestamps[i] if i < len(timestamps) else None,
        })

    try:
        forecast = forecast_market_language(candles)
    except Exception as exc:
        m.available = False
        m.reason = f"market-language error: {type(exc).__name__}"
        return m

    m.details = forecast.as_dict()
    if not forecast.available:
        m.available = False
        m.reason = forecast.reason
        return m

    if forecast.probability_up > forecast.probability_down:
        m.direction = Decision.LONG
    elif forecast.probability_down > forecast.probability_up:
        m.direction = Decision.SHORT
    else:
        m.available = False
        m.reason = "ABSTAIN: balanced distribution"
        return m

    m.confidence = forecast.confidence
    tail_width = max(0.0, forecast.q90_return - forecast.q10_return)
    m.risk_score = max(0.0, min(100.0, 100.0 * min(1.0, tail_width / 0.06)))
    m.reason = (
        f"Pup={forecast.probability_up:.2f} Pdown={forecast.probability_down:.2f} "
        f"median={forecast.median_return:+.3%} q10={forecast.q10_return:+.3%} "
        f"q90={forecast.q90_return:+.3%}"
    )
    return m


def forecast_batch(
    series: Mapping[str, Sequence[Mapping[str, object]]],
    **kwargs,
) -> dict[str, ForecastDistribution]:
    """Independent multi-asset batch helper; one symbol cannot contaminate another."""
    return {
        str(symbol): forecast_market_language(candles, **kwargs)
        for symbol, candles in series.items()
    }


def walk_forward_evaluate(
    candles: Sequence[Mapping[str, object]],
    *,
    warmup: int = 120,
    horizon: int = 1,
    step: int = 1,
    sample_count: int = 48,
) -> dict:
    """Strict prefix-only walk-forward diagnostics for offline evidence."""
    rows = list(candles)
    if warmup < 81:
        raise ValueError("warmup must be >= 81")
    if step < 1:
        raise ValueError("step must be >= 1")
    predictions = []
    hits = 0
    brier = 0.0
    covered = 0

    end = warmup
    while end + horizon <= len(rows):
        prefix = rows[:end]
        forecast = forecast_market_language(
            prefix, horizon=horizon, sample_count=sample_count
        )
        start_close = _f(prefix[-1].get("c", prefix[-1].get("close")))
        future_close = _f(rows[end + horizon - 1].get("c", rows[end + horizon - 1].get("close")))
        actual_up = 1.0 if future_close > start_close else 0.0
        brier += (forecast.probability_up - actual_up) ** 2
        if forecast.available:
            covered += 1
            predicted_up = forecast.probability_up > forecast.probability_down
            if predicted_up == bool(actual_up):
                hits += 1
        predictions.append({
            "end_index": end,
            "p_up": forecast.probability_up,
            "available": forecast.available,
            "actual_up": actual_up,
        })
        end += step

    n = len(predictions)
    return {
        "samples": n,
        "coverage": covered / n if n else 0.0,
        "directional_accuracy_when_available": hits / covered if covered else 0.0,
        "brier_up": brier / n if n else 0.0,
        "predictions": predictions,
    }
