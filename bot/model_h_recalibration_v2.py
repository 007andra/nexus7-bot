"""Nested walk-forward recalibration for native MODEL H.

Research-only: hyperparameters and context filters are selected on a chronological
validation slice, then frozen before the untouched final test slice. No runtime
overlay, ensemble mutation, exchange mutation, sizing, or execution authority.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from bot.backtest import fetch_history
from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
from bot.market_language import forecast_market_language
from bot.model_h_oos_evidence import PublicKuCoinFuturesClient


HORIZONS = (1, 2, 4, 8, 16)


@dataclass(frozen=True)
class Profile:
    name: str
    order: int
    temperature: float
    top_p: float
    dominance: float
    confidence: float
    min_tokens: int


PROFILES = (
    Profile("DEFAULT", 3, 0.85, 0.90, 0.58, 12.0, 80),
    Profile("CONSERVATIVE", 3, 0.70, 0.80, 0.62, 16.0, 120),
    Profile("DEEP_CONTEXT", 4, 0.75, 0.85, 0.60, 12.0, 100),
    Profile("SHORT_CONTEXT", 2, 0.75, 0.85, 0.60, 12.0, 100),
    Profile("LOW_ENTROPY", 4, 0.60, 0.75, 0.62, 14.0, 120),
    Profile("BROAD_SAMPLE", 3, 1.00, 0.95, 0.60, 14.0, 100),
    Profile("STRICT_ABSTAIN", 3, 0.85, 0.90, 0.66, 18.0, 120),
    Profile("MODERATE_DEEP", 4, 0.85, 0.90, 0.60, 10.0, 100),
)


@dataclass(frozen=True)
class Observation:
    symbol: str
    index: int
    p_up: float
    confidence: float
    predicted_up: bool
    actual_up: bool
    actual_return: float
    gross_return: float
    net_return: float
    signaled: bool
    context: str
    hour_bucket: str
    weekday: int
    volume_bucket: str


def _ts_seconds(row: dict) -> float:
    value = float(row.get("ts", row.get("time", row.get("timestamp", 0))) or 0)
    return value / 1000.0 if value > 1e11 else value


def _safe_mean(values) -> float:
    vals = list(values)
    return mean(vals) if vals else 0.0


def context_features(prefix: list[dict]) -> tuple[str, str, int, str]:
    """Causal coarse context used only for validation-selected abstention."""
    closes = [float(x["c"]) for x in prefix[-96:]]
    vols = [float(x.get("v", 0.0) or 0.0) for x in prefix[-48:]]
    if len(closes) < 65:
        trend = "UNKNOWN"
        vol_regime = "UNKNOWN"
    else:
        r16 = closes[-1] / closes[-17] - 1.0
        r64 = closes[-1] / closes[-65] - 1.0
        if r16 > 0.002 and r64 > 0:
            trend = "UP"
        elif r16 < -0.002 and r64 < 0:
            trend = "DOWN"
        else:
            trend = "RANGE"
        abs_r = [abs(b / a - 1.0) for a, b in zip(closes[-65:-1], closes[-64:]) if a > 0]
        recent = _safe_mean(abs_r[-16:])
        prior = _safe_mean(abs_r[:-16]) or recent or 1e-12
        ratio = recent / prior if prior else 1.0
        vol_regime = "HIGH" if ratio > 1.20 else "LOW" if ratio < 0.80 else "NORMAL"

    if len(vols) >= 40:
        recent_v = _safe_mean(vols[-8:])
        prior_v = _safe_mean(vols[-40:-8]) or recent_v or 1e-12
        vr = recent_v / prior_v if prior_v else 1.0
        volume_bucket = "HIGH" if vr > 1.20 else "LOW" if vr < 0.80 else "NORMAL"
    else:
        volume_bucket = "UNKNOWN"

    ts = _ts_seconds(prefix[-1])
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else datetime(1970, 1, 1, tzinfo=timezone.utc)
    hour_bucket = "H00_07" if dt.hour < 8 else "H08_15" if dt.hour < 16 else "H16_23"
    return f"{trend}|{vol_regime}", hour_bucket, dt.weekday(), volume_bucket


def split_bounds(n: int, *, warmup: int = 240) -> tuple[int, int, int]:
    if n < warmup + 200:
        raise ValueError("insufficient candles for nested split")
    train_end = max(warmup, int(n * 0.55))
    val_end = max(train_end + 80, int(n * 0.75))
    val_end = min(val_end, n - 80)
    return warmup, train_end, val_end


def _signal_allowed(forecast, profile: Profile) -> bool:
    if forecast.sample_count <= 0:
        return False
    dominant = max(float(forecast.probability_up), float(forecast.probability_down))
    return dominant >= profile.dominance and float(forecast.confidence) >= profile.confidence


def evaluate_indices(
    symbol: str,
    candles: list[dict],
    indices: list[int],
    *,
    horizon: int,
    profile: Profile,
    round_trip_cost: float,
    sample_count: int = 24,
    allowed_contexts: set[str] | None = None,
) -> list[Observation]:
    rows: list[Observation] = []
    for end in indices:
        if end < profile.min_tokens + 1 or end + horizon > len(candles):
            continue
        prefix = candles[:end]
        forecast = forecast_market_language(
            prefix,
            horizon=horizon,
            sample_count=sample_count,
            order=profile.order,
            temperature=profile.temperature,
            top_p=profile.top_p,
            min_tokens=profile.min_tokens,
        )
        start = float(prefix[-1]["c"])
        finish = float(candles[end + horizon - 1]["c"])
        actual_return = finish / start - 1.0
        actual_up = actual_return > 0
        predicted_up = forecast.probability_up >= forecast.probability_down
        raw_signal = _signal_allowed(forecast, profile)
        context, hour_bucket, weekday, volume_bucket = context_features(prefix)
        context_ok = allowed_contexts is None or context in allowed_contexts
        signaled = bool(raw_signal and context_ok)
        signed = actual_return if predicted_up else -actual_return
        rows.append(Observation(
            symbol=symbol,
            index=end,
            p_up=float(forecast.probability_up),
            confidence=float(forecast.confidence),
            predicted_up=bool(predicted_up),
            actual_up=bool(actual_up),
            actual_return=float(actual_return),
            gross_return=float(signed) if signaled else 0.0,
            net_return=float(signed - round_trip_cost) if signaled else 0.0,
            signaled=signaled,
            context=context,
            hour_bucket=hour_bucket,
            weekday=weekday,
            volume_bucket=volume_bucket,
        ))
    return rows


def _corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    mx, my = mean(xs), mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)


def _ece(rows: list[Observation], bins: int = 10) -> float:
    if not rows:
        return 0.0
    total = len(rows)
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [r for r in rows if lo <= r.p_up < hi or (b == bins - 1 and r.p_up == 1.0)]
        if not group:
            continue
        conf = mean(r.p_up for r in group)
        rate = mean(float(r.actual_up) for r in group)
        ece += len(group) / total * abs(conf - rate)
    return ece


def _bootstrap_ci(values: list[float], *, samples: int = 1500, seed: int = 7) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * (samples - 1))], means[int(0.975 * (samples - 1))]


def metrics(rows: list[Observation], *, bootstrap: bool = False) -> dict:
    if not rows:
        return {
            "samples": 0, "signals": 0, "coverage": 0.0,
            "directional_accuracy": 0.0, "brier_up": 0.0,
            "brier_naive": 0.0, "brier_skill": 0.0, "ece": 0.0,
            "ic": 0.0, "mean_gross_return": 0.0, "mean_net_return": 0.0,
            "bootstrap_net_low": 0.0, "bootstrap_net_high": 0.0,
        }
    signals = [r for r in rows if r.signaled]
    actual_rate = mean(float(r.actual_up) for r in rows)
    brier = mean((r.p_up - float(r.actual_up)) ** 2 for r in rows)
    naive = mean((actual_rate - float(r.actual_up)) ** 2 for r in rows)
    accuracy = mean(float(r.predicted_up == r.actual_up) for r in signals) if signals else 0.0
    net = [r.net_return for r in signals]
    low, high = _bootstrap_ci(net) if bootstrap and net else (0.0, 0.0)
    return {
        "samples": len(rows),
        "signals": len(signals),
        "coverage": len(signals) / len(rows),
        "directional_accuracy": accuracy,
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


def _selection_key(report: dict) -> tuple:
    enough = report["signals"] >= 30 and report["coverage"] >= 0.15
    return (
        1 if enough else 0,
        report["mean_net_return"],
        report["brier_skill"],
        report["directional_accuracy"],
    )


def _indices(start: int, stop: int, step: int, horizon: int) -> list[int]:
    return list(range(start, max(start, stop - horizon + 1), max(step, horizon)))


def learn_context_whitelist(rows: list[Observation], *, min_signals: int = 8) -> set[str]:
    grouped: dict[str, list[float]] = {}
    for r in rows:
        if r.signaled:
            grouped.setdefault(r.context, []).append(r.net_return)
    allowed = {
        key for key, vals in grouped.items()
        if len(vals) >= min_signals and mean(vals) > 0.0
    }
    return allowed


async def recalibrate(
    symbols: list[str],
    *,
    limit_15m: int = 2200,
    tune_step: int = 16,
    test_step: int = 8,
) -> dict:
    fee = configured_taker_fee()
    data: dict[str, list[dict]] = {}
    costs: dict[str, float] = {}
    async with PublicKuCoinFuturesClient() as client:
        for symbol in symbols:
            candles = await fetch_history(client, symbol, "15", limit_15m)
            data[symbol] = candles
            costs[symbol] = 2.0 * (fee + slippage_rate_for_symbol(symbol))

    common_n = min(len(x) for x in data.values())
    _, train_end, val_end = split_bounds(common_n)
    for symbol in list(data):
        data[symbol] = data[symbol][-common_n:]

    # Stage 1: choose horizon only on validation, using frozen default profile.
    default = PROFILES[0]
    horizon_reports = {}
    for horizon in HORIZONS:
        pooled = []
        for symbol in symbols:
            idx = _indices(train_end, val_end, tune_step, horizon)
            pooled.extend(evaluate_indices(
                symbol, data[symbol], idx, horizon=horizon, profile=default,
                round_trip_cost=costs[symbol], sample_count=24,
            ))
        horizon_reports[str(horizon)] = metrics(pooled)
    eligible_horizons = [
        h for h in HORIZONS
        if horizon_reports[str(h)]["signals"] >= 30
        and horizon_reports[str(h)]["coverage"] >= 0.15
    ]
    selected_horizon = max(
        eligible_horizons or HORIZONS,
        key=lambda h: _selection_key(horizon_reports[str(h)]),
    )

    # Stage 2: choose one global profile on validation for the chosen horizon.
    profile_reports = {}
    profile_rows: dict[str, list[Observation]] = {}
    for profile in PROFILES:
        pooled = []
        for symbol in symbols:
            idx = _indices(train_end, val_end, tune_step, selected_horizon)
            pooled.extend(evaluate_indices(
                symbol, data[symbol], idx, horizon=selected_horizon,
                profile=profile, round_trip_cost=costs[symbol], sample_count=24,
            ))
        profile_rows[profile.name] = pooled
        profile_reports[profile.name] = metrics(pooled)
    selected_profile = max(PROFILES, key=lambda p: _selection_key(profile_reports[p.name]))

    # Stage 3: context conditioning learned on validation only, then frozen.
    validation_rows = profile_rows[selected_profile.name]
    allowed_contexts = learn_context_whitelist(validation_rows)
    use_context_filter = bool(allowed_contexts)
    validation_filtered = [
        Observation(**{**asdict(r), "signaled": bool(r.signaled and (not use_context_filter or r.context in allowed_contexts))})
        for r in validation_rows
    ]

    # Final untouched test. Hyperparameters and context whitelist are frozen.
    test_rows = []
    by_symbol = {}
    for symbol in symbols:
        idx = _indices(val_end, common_n, test_step, selected_horizon)
        rows = evaluate_indices(
            symbol, data[symbol], idx,
            horizon=selected_horizon,
            profile=selected_profile,
            round_trip_cost=costs[symbol],
            sample_count=48,
            allowed_contexts=allowed_contexts if use_context_filter else None,
        )
        test_rows.extend(rows)
        by_symbol[symbol] = metrics(rows, bootstrap=True)

    pooled_test = metrics(test_rows, bootstrap=True)
    positive_symbols = sum(1 for rep in by_symbol.values() if rep["mean_net_return"] > 0)
    edge_screen_pass = bool(
        pooled_test["signals"] >= 50
        and pooled_test["coverage"] >= 0.15
        and pooled_test["mean_net_return"] > 0.0
        and pooled_test["bootstrap_net_low"] > 0.0
        and pooled_test["directional_accuracy"] > 0.50
        and pooled_test["brier_skill"] > 0.0
        and positive_symbols >= 3
    )

    context_breakdown = {}
    for key in sorted({r.context for r in test_rows}):
        context_breakdown[key] = metrics([r for r in test_rows if r.context == key])

    return {
        "status": "EDGE_SCREEN_PASS" if edge_screen_pass else "EDGE_SCREEN_FAIL",
        "promotion_authority": False,
        "runtime_effect": False,
        "selected": {
            "horizon_15m_bars": selected_horizon,
            "horizon_minutes": selected_horizon * 15,
            "profile": asdict(selected_profile),
            "context_filter_enabled": use_context_filter,
            "allowed_contexts_from_validation": sorted(allowed_contexts),
        },
        "split": {
            "candles_per_symbol": common_n,
            "train_end": train_end,
            "validation_end": val_end,
            "test_start": val_end,
            "selection_uses_test": False,
        },
        "validation": {
            "horizon_screen": horizon_reports,
            "profile_screen": profile_reports,
            "selected_profile_unfiltered": profile_reports[selected_profile.name],
            "selected_profile_context_filtered": metrics(validation_filtered),
        },
        "untouched_test": {
            "pooled": pooled_test,
            "positive_net_symbols": positive_symbols,
            "symbols": by_symbol,
            "contexts": context_breakdown,
        },
        "methodology": {
            "strict_prefix_only": True,
            "global_config_across_symbols": True,
            "validation_only_hyperparameter_selection": True,
            "validation_only_context_selection": True,
            "final_test_untouched_until_freeze": True,
            "horizons_minutes": [15, 30, 60, 120, 240],
            "fees_included": True,
            "slippage_included": True,
            "funding_included": False,
            "bootstrap_samples": 1500,
            "execution_mutations": False,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"])
    p.add_argument("--limit-15m", type=int, default=2200)
    p.add_argument("--tune-step", type=int, default=16)
    p.add_argument("--test-step", type=int, default=8)
    p.add_argument("--output", default="artifacts/model_h_recalibration_v2.json")
    args = p.parse_args()
    report = asyncio.run(recalibrate(
        args.symbols,
        limit_15m=args.limit_15m,
        tune_step=args.tune_step,
        test_step=args.test_step,
    ))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "selected": report["selected"],
        "test": report["untouched_test"]["pooled"],
        "positive_net_symbols": report["untouched_test"]["positive_net_symbols"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
