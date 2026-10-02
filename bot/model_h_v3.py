"""MODEL H V3: causal multiscale market-language + analog memory research architecture.

This module is evidence-only. It is intentionally disconnected from NEXUS runtime,
score aggregation, risk, sizing, and exchange execution.

V3 adds information that V2 did not use:
- 15m / 1h / 4h market-language forecasts derived causally from closed 15m bars;
- normalized multiscale momentum/volatility/volume/range features;
- causal nearest-analog memory using only historical outcomes already knowable at
  each decision timestamp;
- mixture-of-experts profiles selected on a development universe;
- calibration learned on development symbols only;
- final transfer test on entirely unseen holdout symbols.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean

from bot.backtest import fetch_history
from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
from bot.market_language import forecast_market_language
from bot.model_h_oos_evidence import PublicKuCoinFuturesClient


DEV_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
HOLDOUT_SYMBOLS = ("LINKUSDT", "ADAUSDT", "AVAXUSDT", "LTCUSDT", "BCHUSDT")
HORIZONS = (1, 2, 4, 8)


@dataclass(frozen=True)
class V3Profile:
    name: str
    w_15m: float
    w_1h: float
    w_4h: float
    w_analog: float
    min_edge: float
    min_agree: int
    cost_mult: float
    analog_k: int
    regime_match: bool


PROFILES = (
    V3Profile("BALANCED", 0.40, 0.20, 0.10, 0.30, 0.12, 3, 1.00, 24, False),
    V3Profile("ANALOG_HEAVY", 0.25, 0.15, 0.05, 0.55, 0.12, 3, 1.20, 32, True),
    V3Profile("MTF_HEAVY", 0.35, 0.30, 0.20, 0.15, 0.10, 3, 1.00, 24, False),
    V3Profile("STRICT_CONSENSUS", 0.35, 0.20, 0.10, 0.35, 0.16, 4, 1.40, 32, True),
)


@dataclass(frozen=True)
class AnalogPoint:
    index: int
    features: tuple[float, ...]
    regime: str
    future_return: float


@dataclass(frozen=True)
class ComponentForecast:
    symbol: str
    index: int
    horizon: int
    p_15m: float
    p_1h: float
    p_4h: float
    median_15m: float
    features: tuple[float, ...]
    regime: str
    actual_return: float
    actual_up: bool


@dataclass(frozen=True)
class V3Observation:
    symbol: str
    index: int
    raw_p_up: float
    p_up: float
    predicted_up: bool
    actual_up: bool
    actual_return: float
    gross_return: float
    net_return: float
    signaled: bool
    agreement: int
    expected_move: float
    regime: str


@dataclass(frozen=True)
class Calibrator:
    global_rate: float
    bin_rates: tuple[float, ...]
    bins: int = 5
    prior_strength: float = 12.0

    def apply(self, p: float) -> float:
        p = max(0.0, min(1.0, float(p)))
        idx = min(self.bins - 1, int(p * self.bins))
        return max(0.01, min(0.99, float(self.bin_rates[idx])))


def _safe_mean(values) -> float:
    vals = list(values)
    return mean(vals) if vals else 0.0


def _ts_ms(row: dict) -> int:
    ts = int(float(row.get("ts", row.get("time", row.get("timestamp", 0))) or 0))
    return ts * 1000 if 0 < ts < 100_000_000_000 else ts


def aggregate_closed(candles: list[dict], factor: int) -> list[dict]:
    """Aggregate complete UTC-aligned groups only; incomplete final buckets vanish."""
    if factor < 1:
        raise ValueError("factor must be >= 1")
    if factor == 1:
        return [dict(x) for x in candles]
    interval_ms = 15 * 60 * 1000
    bucket_ms = interval_ms * factor
    groups: dict[int, list[dict]] = {}
    for row in candles:
        ts = _ts_ms(row)
        if ts <= 0:
            continue
        groups.setdefault(ts // bucket_ms, []).append(row)

    out = []
    for key in sorted(groups):
        grp = sorted(groups[key], key=_ts_ms)
        stamps = [_ts_ms(x) for x in grp]
        complete = (
            len(grp) == factor
            and len(set(stamps)) == factor
            and all(b - a == interval_ms for a, b in zip(stamps, stamps[1:]))
        )
        if not complete:
            continue
        out.append({
            "ts": stamps[0],
            "o": float(grp[0]["o"]),
            "h": max(float(x["h"]) for x in grp),
            "l": min(float(x["l"]) for x in grp),
            "c": float(grp[-1]["c"]),
            "v": sum(float(x.get("v", 0.0) or 0.0) for x in grp),
        })
    return out


def feature_vector(prefix: list[dict]) -> tuple[float, ...]:
    if len(prefix) < 65:
        raise ValueError("need at least 65 candles")
    rows = prefix[-65:]
    closes = [float(x["c"]) for x in rows]
    volumes = [max(0.0, float(x.get("v", 0.0) or 0.0)) for x in rows]
    ranges = [(float(x["h"]) - float(x["l"])) / max(float(x["c"]), 1e-12) for x in rows]
    log_r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(log_r) < 32:
        raise ValueError("insufficient returns")
    vol32 = math.sqrt(_safe_mean(x * x for x in log_r[-32:])) + 1e-8
    vol8 = math.sqrt(_safe_mean(x * x for x in log_r[-8:])) + 1e-8
    r1 = log_r[-1] / vol32
    r4 = sum(log_r[-4:]) / (vol32 * 2.0)
    r16 = sum(log_r[-16:]) / (vol32 * 4.0)
    vol_ratio = vol8 / vol32
    range_ratio = _safe_mean(ranges[-8:]) / max(_safe_mean(ranges[-32:]), 1e-8)
    volume_ratio = _safe_mean(volumes[-8:]) / max(_safe_mean(volumes[-32:]), 1e-8)
    body_bias = _safe_mean(
        (float(x["c"]) - float(x["o"])) / max(float(x["h"]) - float(x["l"]), 1e-12)
        for x in rows[-8:]
    )
    return (
        max(-8.0, min(8.0, r1)),
        max(-8.0, min(8.0, r4)),
        max(-8.0, min(8.0, r16)),
        max(0.0, min(4.0, vol_ratio)),
        max(0.0, min(4.0, range_ratio)),
        max(0.0, min(4.0, volume_ratio)),
        max(-1.0, min(1.0, body_bias)),
    )


def regime_tag(features: tuple[float, ...]) -> str:
    r16, vr = features[2], features[3]
    trend = "UP" if r16 > 1.0 else "DOWN" if r16 < -1.0 else "RANGE"
    vol = "HIGH" if vr > 1.20 else "LOW" if vr < 0.80 else "NORMAL"
    return f"{trend}|{vol}"


def build_analog_index(candles: list[dict], horizon: int, *, stride: int = 2) -> list[AnalogPoint]:
    points = []
    for end in range(96, len(candles) - horizon + 1, stride):
        try:
            feats = feature_vector(candles[:end])
        except ValueError:
            continue
        start = float(candles[end - 1]["c"])
        finish = float(candles[end + horizon - 1]["c"])
        if start <= 0:
            continue
        points.append(AnalogPoint(
            index=end,
            features=feats,
            regime=regime_tag(feats),
            future_return=finish / start - 1.0,
        ))
    return points


def _distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def analog_forecast(
    points: list[AnalogPoint],
    *,
    decision_index: int,
    features: tuple[float, ...],
    regime: str,
    horizon: int,
    k: int,
    regime_match: bool,
) -> tuple[float, float, int]:
    """Nearest historical analogs whose forward outcome was known by decision time."""
    eligible = [p for p in points if p.index + horizon <= decision_index]
    if regime_match:
        matched = [p for p in eligible if p.regime == regime]
        if len(matched) >= max(8, k // 2):
            eligible = matched
    if not eligible:
        return 0.5, 0.0, 0
    ranked = sorted(((_distance(features, p.features), p) for p in eligible), key=lambda x: x[0])[:k]
    weights = [1.0 / (0.20 + d) for d, _ in ranked]
    total = sum(weights) or 1.0
    p_up = sum(w * float(p.future_return > 0) for w, (_, p) in zip(weights, ranked)) / total
    expected = sum(w * p.future_return for w, (_, p) in zip(weights, ranked)) / total
    return p_up, expected, len(ranked)


def _forecast_prob(prefix: list[dict], *, horizon: int, min_tokens: int) -> tuple[float, float]:
    if len(prefix) < min_tokens + 1:
        return 0.5, 0.0
    try:
        f = forecast_market_language(
            prefix,
            horizon=max(1, min(8, horizon)),
            sample_count=24,
            order=4,
            temperature=0.85,
            top_p=0.90,
            min_tokens=min_tokens,
        )
    except Exception:
        return 0.5, 0.0
    if f.sample_count <= 0:
        return 0.5, 0.0
    return float(f.probability_up), float(f.median_return)


def compute_components(
    symbol: str,
    candles: list[dict],
    indices: list[int],
    *,
    horizon: int,
) -> list[ComponentForecast]:
    out = []
    for end in indices:
        if end < 160 or end + horizon > len(candles):
            continue
        prefix = candles[:end]
        feats = feature_vector(prefix)
        p15, med15 = _forecast_prob(prefix, horizon=horizon, min_tokens=100)

        h1 = aggregate_closed(prefix, 4)
        h4 = aggregate_closed(prefix, 16)
        p1, _ = _forecast_prob(h1, horizon=max(1, round(horizon / 4)), min_tokens=60)
        p4, _ = _forecast_prob(h4, horizon=1, min_tokens=40)

        start = float(prefix[-1]["c"])
        finish = float(candles[end + horizon - 1]["c"])
        actual_return = finish / start - 1.0
        out.append(ComponentForecast(
            symbol=symbol,
            index=end,
            horizon=horizon,
            p_15m=p15,
            p_1h=p1,
            p_4h=p4,
            median_15m=med15,
            features=feats,
            regime=regime_tag(feats),
            actual_return=actual_return,
            actual_up=actual_return > 0.0,
        ))
    return out


def apply_profile(
    components: list[ComponentForecast],
    analog_points: list[AnalogPoint],
    profile: V3Profile,
    *,
    round_trip_cost: float,
    calibrator: Calibrator | None = None,
) -> list[V3Observation]:
    rows = []
    for c in components:
        pa, analog_move, analog_n = analog_forecast(
            analog_points,
            decision_index=c.index,
            features=c.features,
            regime=c.regime,
            horizon=c.horizon,
            k=profile.analog_k,
            regime_match=profile.regime_match,
        )
        weights = (profile.w_15m, profile.w_1h, profile.w_4h, profile.w_analog)
        probs = (c.p_15m, c.p_1h, c.p_4h, pa)
        denom = sum(weights) or 1.0
        raw_p = sum(w * p for w, p in zip(weights, probs)) / denom
        predicted_up = raw_p >= 0.5
        agreement = sum((p >= 0.5) == predicted_up for p in probs)
        edge = abs(raw_p - 0.5) * 2.0
        expected_move = 0.50 * c.median_15m + 0.50 * analog_move
        signaled = bool(
            analog_n >= max(8, profile.analog_k // 2)
            and edge >= profile.min_edge
            and agreement >= profile.min_agree
            and abs(expected_move) >= profile.cost_mult * round_trip_cost
        )
        signed = c.actual_return if predicted_up else -c.actual_return
        p_cal = calibrator.apply(raw_p) if calibrator is not None else raw_p
        rows.append(V3Observation(
            symbol=c.symbol,
            index=c.index,
            raw_p_up=raw_p,
            p_up=p_cal,
            predicted_up=predicted_up,
            actual_up=c.actual_up,
            actual_return=c.actual_return,
            gross_return=signed if signaled else 0.0,
            net_return=(signed - round_trip_cost) if signaled else 0.0,
            signaled=signaled,
            agreement=agreement,
            expected_move=expected_move,
            regime=c.regime,
        ))
    return rows


def fit_calibrator(rows: list[V3Observation], *, bins: int = 5, prior_strength: float = 12.0) -> Calibrator:
    if not rows:
        return Calibrator(0.5, tuple(0.5 for _ in range(bins)), bins, prior_strength)
    global_rate = _safe_mean(float(r.actual_up) for r in rows)
    rates = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [r for r in rows if lo <= r.raw_p_up < hi or (b == bins - 1 and r.raw_p_up == 1.0)]
        successes = sum(float(r.actual_up) for r in group)
        rates.append((successes + prior_strength * global_rate) / (len(group) + prior_strength))
    return Calibrator(global_rate, tuple(rates), bins, prior_strength)


def _ece(rows: list[V3Observation], bins: int = 10) -> float:
    if not rows:
        return 0.0
    total = len(rows)
    out = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [r for r in rows if lo <= r.p_up < hi or (b == bins - 1 and r.p_up == 1.0)]
        if group:
            out += len(group) / total * abs(
                _safe_mean(r.p_up for r in group) - _safe_mean(float(r.actual_up) for r in group)
            )
    return out


def _corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    mx, my = mean(xs), mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def _bootstrap_ci(values: list[float], samples: int = 1500, seed: int = 19) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    vals = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(samples)
    )
    return vals[int(0.025 * (samples - 1))], vals[int(0.975 * (samples - 1))]


def metrics(rows: list[V3Observation], *, bootstrap: bool = False) -> dict:
    if not rows:
        return {
            "samples": 0, "signals": 0, "coverage": 0.0,
            "directional_accuracy": 0.0, "brier_up": 0.0,
            "brier_naive": 0.0, "brier_skill": 0.0, "ece": 0.0,
            "ic": 0.0, "mean_gross_return": 0.0, "mean_net_return": 0.0,
            "positive_net_fraction": 0.0, "bootstrap_net_low": 0.0,
            "bootstrap_net_high": 0.0,
        }
    signals = [r for r in rows if r.signaled]
    actual_rate = _safe_mean(float(r.actual_up) for r in rows)
    brier = _safe_mean((r.p_up - float(r.actual_up)) ** 2 for r in rows)
    naive = _safe_mean((actual_rate - float(r.actual_up)) ** 2 for r in rows)
    net = [r.net_return for r in signals]
    low, high = _bootstrap_ci(net) if bootstrap and net else (0.0, 0.0)
    return {
        "samples": len(rows),
        "signals": len(signals),
        "coverage": len(signals) / len(rows),
        "directional_accuracy": _safe_mean(float(r.predicted_up == r.actual_up) for r in signals),
        "brier_up": brier,
        "brier_naive": naive,
        "brier_skill": 1.0 - brier / naive if naive > 0 else 0.0,
        "ece": _ece(rows),
        "ic": _corr([2.0 * r.p_up - 1.0 for r in rows], [r.actual_return for r in rows]),
        "mean_gross_return": _safe_mean(r.gross_return for r in signals),
        "mean_net_return": _safe_mean(net),
        "positive_net_fraction": _safe_mean(float(x > 0) for x in net),
        "bootstrap_net_low": low,
        "bootstrap_net_high": high,
    }


def _selection_key(rep: dict) -> tuple:
    sufficient = rep["signals"] >= 25 and rep["coverage"] >= 0.10
    return (
        1 if sufficient else 0,
        rep["mean_net_return"],
        rep["brier_skill"],
        rep["directional_accuracy"],
    )


def _indices(start: int, stop: int, *, step: int, horizon: int) -> list[int]:
    return list(range(start, max(start, stop - horizon + 1), max(step, horizon)))


async def _fetch_universe(client, symbols: tuple[str, ...], limit_15m: int) -> dict[str, list[dict]]:
    out = {}
    for symbol in symbols:
        rows = await fetch_history(client, symbol, "15", limit_15m)
        if len(rows) >= min(1200, limit_15m):
            out[symbol] = rows
    return out


async def run_v3(
    *,
    dev_symbols: tuple[str, ...] = DEV_SYMBOLS,
    holdout_symbols: tuple[str, ...] = HOLDOUT_SYMBOLS,
    limit_15m: int = 1800,
    tune_step: int = 32,
    holdout_step: int = 16,
) -> dict:
    fee = configured_taker_fee()
    async with PublicKuCoinFuturesClient() as client:
        dev = await _fetch_universe(client, dev_symbols, limit_15m)
        holdout = await _fetch_universe(client, holdout_symbols, limit_15m)

    if len(dev) < 4:
        raise RuntimeError(f"insufficient development universe: {sorted(dev)}")
    if len(holdout) < 4:
        raise RuntimeError(f"insufficient unseen holdout universe: {sorted(holdout)}")

    costs = {
        symbol: 2.0 * (fee + slippage_rate_for_symbol(symbol))
        for symbol in set(dev) | set(holdout)
    }

    # Development symbols are allowed for architecture/profile selection.
    combo_reports = {}
    combo_rows = {}
    combo_calibrators = {}
    component_cache = {}
    analog_cache = {}

    for symbol, candles in dev.items():
        n = len(candles)
        train_end = max(320, int(n * 0.55))
        val_end = max(train_end + 160, int(n * 0.80))
        val_end = min(val_end, n - 80)
        for horizon in HORIZONS:
            idx = _indices(train_end, val_end, step=tune_step, horizon=horizon)
            component_cache[(symbol, horizon)] = compute_components(
                symbol, candles, idx, horizon=horizon
            )
            analog_cache[(symbol, horizon)] = build_analog_index(candles, horizon)

    for horizon in HORIZONS:
        for profile in PROFILES:
            key = f"H{horizon}:{profile.name}"
            pooled = []
            for symbol in dev:
                pooled.extend(apply_profile(
                    component_cache[(symbol, horizon)],
                    analog_cache[(symbol, horizon)],
                    profile,
                    round_trip_cost=costs[symbol],
                ))
            combo_rows[key] = pooled
            combo_reports[key] = metrics(pooled)
            combo_calibrators[key] = fit_calibrator(pooled)

    selected_key = max(combo_reports, key=lambda k: _selection_key(combo_reports[k]))
    h_text, profile_name = selected_key.split(":", 1)
    selected_horizon = int(h_text[1:])
    selected_profile = next(p for p in PROFILES if p.name == profile_name)
    calibrator = combo_calibrators[selected_key]

    # Final test is cross-symbol: these assets were never used in profile,
    # horizon, mixture, cost gate, or calibration selection.
    final_rows = []
    by_symbol = {}
    raw_by_symbol = {}
    for symbol, candles in holdout.items():
        n = len(candles)
        test_start = max(320, int(n * 0.75))
        idx = _indices(test_start, n, step=holdout_step, horizon=selected_horizon)
        comps = compute_components(symbol, candles, idx, horizon=selected_horizon)
        analogs = build_analog_index(candles, selected_horizon)
        raw = apply_profile(
            comps, analogs, selected_profile,
            round_trip_cost=costs[symbol],
            calibrator=None,
        )
        calibrated = apply_profile(
            comps, analogs, selected_profile,
            round_trip_cost=costs[symbol],
            calibrator=calibrator,
        )
        final_rows.extend(calibrated)
        by_symbol[symbol] = metrics(calibrated, bootstrap=True)
        raw_by_symbol[symbol] = metrics(raw)

    pooled = metrics(final_rows, bootstrap=True)
    positive_symbols = sum(1 for rep in by_symbol.values() if rep["mean_net_return"] > 0)
    edge_screen_pass = bool(
        len(by_symbol) >= 4
        and pooled["signals"] >= 50
        and pooled["coverage"] >= 0.12
        and pooled["mean_net_return"] > 0.0
        and pooled["bootstrap_net_low"] > 0.0
        and pooled["directional_accuracy"] > 0.50
        and pooled["brier_skill"] > 0.0
        and positive_symbols >= 3
    )

    return {
        "status": "EDGE_SCREEN_PASS" if edge_screen_pass else "EDGE_SCREEN_FAIL",
        "promotion_authority": False,
        "runtime_effect": False,
        "architecture": "MODEL_H_V3_MULTISCALE_ANALOG_MOE",
        "development_universe": sorted(dev),
        "unseen_holdout_universe": sorted(holdout),
        "selection": {
            "selected_key": selected_key,
            "horizon_15m_bars": selected_horizon,
            "horizon_minutes": 15 * selected_horizon,
            "profile": asdict(selected_profile),
            "calibrator": asdict(calibrator),
            "selection_uses_holdout_symbols": False,
        },
        "development_validation": {
            "all_combinations": combo_reports,
            "selected": combo_reports[selected_key],
        },
        "unseen_symbol_holdout": {
            "pooled": pooled,
            "positive_net_symbols": positive_symbols,
            "symbols": by_symbol,
            "raw_uncalibrated_symbols": raw_by_symbol,
        },
        "methodology": {
            "strict_prefix_only": True,
            "causal_analog_outcomes_only": True,
            "closed_15m_to_1h_4h_aggregation": True,
            "unseen_symbols_for_final_test": True,
            "horizons_minutes": [15, 30, 60, 120],
            "profiles": [p.name for p in PROFILES],
            "fees_included": True,
            "slippage_included": True,
            "funding_included": False,
            "bootstrap_samples": 1500,
            "external_model_dependency": False,
            "exchange_mutations": False,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit-15m", type=int, default=1800)
    p.add_argument("--tune-step", type=int, default=32)
    p.add_argument("--holdout-step", type=int, default=16)
    p.add_argument("--output", default="artifacts/model_h_v3_evidence.json")
    args = p.parse_args()
    report = asyncio.run(run_v3(
        limit_15m=args.limit_15m,
        tune_step=args.tune_step,
        holdout_step=args.holdout_step,
    ))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "selection": report["selection"],
        "holdout": report["unseen_symbol_holdout"]["pooled"],
        "positive_net_symbols": report["unseen_symbol_holdout"]["positive_net_symbols"],
        "holdout_symbols": report["unseen_holdout_universe"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
