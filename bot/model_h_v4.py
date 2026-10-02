"""MODEL H V4: invariant cross-sectional residual forecaster.

Research-only. V4 is intentionally disconnected from production runtime.

The architecture removes V3's prevalence/regime probability calibration and
learns one zero-intercept global ridge model on scale-free, cross-sectional
features. Final evaluation is both symbol-unseen and chronologically separated.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median

from bot.backtest import fetch_history
from bot.kucoin_execution_model import configured_taker_fee, slippage_rate_for_symbol
from bot.market_language import forecast_market_language
from bot.model_h_oos_evidence import PublicKuCoinFuturesClient


DEV_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "LINKUSDT", "ADAUSDT", "AVAXUSDT", "LTCUSDT", "BCHUSDT",
)
FRESH_HOLDOUT_SYMBOLS = ("TRXUSDT", "DOTUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT")
HORIZONS = (1, 2, 4, 8)
RIDGES = (0.10, 1.0, 10.0)
THRESHOLDS = (0.20, 0.35, 0.50)
COST_MULTIPLE = 1.25


@dataclass(frozen=True)
class Sample:
    symbol: str
    decision_ts: int
    features: tuple[float, ...]
    target_z: float
    future_return: float
    actual_up: bool
    return_scale: float
    round_trip_cost: float


@dataclass(frozen=True)
class RidgeModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    weights: tuple[float, ...]
    ridge: float

    def predict(self, features: tuple[float, ...]) -> float:
        z = [
            (x - m) / s
            for x, m, s in zip(features, self.means, self.scales)
        ]
        return sum(w * x for w, x in zip(self.weights, z))


@dataclass(frozen=True)
class Observation:
    symbol: str
    decision_ts: int
    score_z: float
    p_up: float
    predicted_up: bool
    actual_up: bool
    actual_return: float
    expected_return: float
    gross_return: float
    net_return: float
    signaled: bool


def _ts_ms(row: dict) -> int:
    ts = int(float(row.get("ts", row.get("time", row.get("timestamp", 0))) or 0))
    return ts * 1000 if 0 < ts < 100_000_000_000 else ts


def align_panel(raw: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Align symbols on the exact same closed 15m timestamps."""
    valid = {s: rows for s, rows in raw.items() if rows}
    if not valid:
        return {}
    common = None
    maps = {}
    for symbol, rows in valid.items():
        mapping = {_ts_ms(r): r for r in rows if _ts_ms(r) > 0}
        maps[symbol] = mapping
        keys = set(mapping)
        common = keys if common is None else common & keys
    stamps = sorted(common or ())
    if not stamps:
        return {}
    return {
        symbol: [mapping[t] for t in stamps]
        for symbol, mapping in maps.items()
    }


def _rank(values: dict[str, float], symbol: str) -> float:
    if symbol not in values or len(values) <= 1:
        return 0.0
    ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    pos = next(i for i, (s, _) in enumerate(ordered) if s == symbol)
    return 2.0 * pos / (len(ordered) - 1) - 1.0


def _log_returns(rows: list[dict], end: int, lookback: int = 64) -> list[float]:
    start = max(1, end - lookback)
    out = []
    for i in range(start, end):
        a = float(rows[i - 1]["c"])
        b = float(rows[i]["c"])
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _realized_vol(rows: list[dict], end: int) -> float:
    rs = _log_returns(rows, end, 32)
    if not rs:
        return 1e-6
    return max(1e-6, math.sqrt(sum(x * x for x in rs) / len(rs)))


def _ret(rows: list[dict], end: int, window: int) -> float:
    if end <= window:
        return 0.0
    a = float(rows[end - 1 - window]["c"])
    b = float(rows[end - 1]["c"])
    return b / a - 1.0 if a > 0 else 0.0


def _volume_surprise(rows: list[dict], end: int) -> float:
    vals = [max(0.0, float(x.get("v", 0.0) or 0.0)) for x in rows[max(0, end - 32):end]]
    if len(vals) < 16:
        return 1.0
    recent = mean(vals[-8:])
    base = mean(vals[:-8]) or recent or 1e-9
    return recent / base if base else 1.0


def _ml_edge(rows: list[dict], end: int, horizon: int) -> float:
    if end < 100:
        return 0.0
    try:
        f = forecast_market_language(
            rows[:end],
            horizon=horizon,
            sample_count=16,
            order=4,
            temperature=0.85,
            top_p=0.90,
            min_tokens=80,
        )
    except Exception:
        return 0.0
    if f.sample_count <= 0:
        return 0.0
    return max(-1.0, min(1.0, 2.0 * float(f.probability_up) - 1.0))


def invariant_features(
    panel: dict[str, list[dict]],
    symbol: str,
    end: int,
    *,
    horizon: int,
) -> tuple[tuple[float, ...], float]:
    """Scale-free features; no symbol identity and no fitted prevalence term."""
    if symbol not in panel or end < 80:
        raise ValueError("insufficient panel history")

    windows = (4, 16, 64)
    returns_by_window = {
        w: {s: _ret(rows, end, w) for s, rows in panel.items()}
        for w in windows
    }
    vols = {s: _realized_vol(rows, end) for s, rows in panel.items()}
    vol_median = max(1e-6, median(vols.values()))
    volume_surprises = {s: _volume_surprise(rows, end) for s, rows in panel.items()}

    market = {w: median(vals.values()) for w, vals in returns_by_window.items()}
    own = {w: returns_by_window[w][symbol] for w in windows}
    own_vol = vols[symbol]

    factor_z = [
        max(-6.0, min(6.0, market[w] / (vol_median * math.sqrt(w))))
        for w in windows
    ]
    relative_ranks = [_rank(returns_by_window[w], symbol) for w in windows]
    residual_z = max(
        -6.0,
        min(6.0, (own[16] - market[16]) / (own_vol * math.sqrt(16))),
    )
    vol_rank = _rank(vols, symbol)
    volume_rank = _rank(volume_surprises, symbol)
    breadth = 2.0 * mean(float(x > 0.0) for x in returns_by_window[4].values()) - 1.0
    trend_alignment = (
        1.0 if own[16] > 0 and market[16] > 0
        else -1.0 if own[16] < 0 and market[16] < 0
        else 0.0
    )
    ml = _ml_edge(panel[symbol], end, horizon)

    features = tuple(
        factor_z
        + relative_ranks
        + [residual_z, vol_rank, volume_rank, breadth, trend_alignment, ml]
    )
    return features, own_vol * math.sqrt(max(1, horizon))


def build_samples(
    panel: dict[str, list[dict]],
    symbols: tuple[str, ...] | list[str],
    *,
    horizon: int,
    step: int,
    min_ts: int | None = None,
    max_ts: int | None = None,
) -> list[Sample]:
    if not panel:
        return []
    n = min(len(x) for x in panel.values())
    out = []
    fee = configured_taker_fee()
    for end in range(100, n - horizon, max(step, horizon)):
        decision_ts = _ts_ms(next(iter(panel.values()))[end - 1])
        if min_ts is not None and decision_ts < min_ts:
            continue
        if max_ts is not None and decision_ts >= max_ts:
            continue
        for symbol in symbols:
            if symbol not in panel:
                continue
            features, scale = invariant_features(panel, symbol, end, horizon=horizon)
            start = float(panel[symbol][end - 1]["c"])
            finish = float(panel[symbol][end + horizon - 1]["c"])
            future = finish / start - 1.0
            target = max(-6.0, min(6.0, future / max(scale, 1e-6)))
            cost = 2.0 * (fee + slippage_rate_for_symbol(symbol))
            out.append(Sample(
                symbol=symbol,
                decision_ts=decision_ts,
                features=features,
                target_z=target,
                future_return=future,
                actual_up=future > 0.0,
                return_scale=scale,
                round_trip_cost=cost,
            ))
    return out


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            continue
        a[col], a[pivot] = a[pivot], a[col]
        div = a[col][col]
        a[col] = [x / div for x in a[col]]
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor:
                a[r] = [x - factor * y for x, y in zip(a[r], a[col])]
    return [a[i][-1] if abs(a[i][i]) > 1e-9 else 0.0 for i in range(n)]


def fit_ridge(samples: list[Sample], ridge: float) -> RidgeModel:
    if not samples:
        raise ValueError("empty training samples")
    d = len(samples[0].features)
    means = tuple(mean(s.features[j] for s in samples) for j in range(d))
    scales = []
    for j in range(d):
        var = mean((s.features[j] - means[j]) ** 2 for s in samples)
        scales.append(max(1e-6, math.sqrt(var)))
    x_rows = [
        [(x - means[j]) / scales[j] for j, x in enumerate(s.features)]
        for s in samples
    ]
    xtx = [[0.0 for _ in range(d)] for _ in range(d)]
    xty = [0.0 for _ in range(d)]
    for x, sample in zip(x_rows, samples):
        for i in range(d):
            xty[i] += x[i] * sample.target_z
            for j in range(d):
                xtx[i][j] += x[i] * x[j]
    for i in range(d):
        xtx[i][i] += float(ridge)
    weights = tuple(_solve(xtx, xty))
    return RidgeModel(means, tuple(scales), weights, float(ridge))


def _p_up(score_z: float) -> float:
    # Fixed symmetric link: no prevalence intercept or post-hoc probability fit.
    return 0.5 + 0.5 * math.tanh(max(-6.0, min(6.0, score_z)) / 2.0)


def evaluate(
    model: RidgeModel,
    samples: list[Sample],
    *,
    threshold_z: float,
    cost_multiple: float = COST_MULTIPLE,
) -> list[Observation]:
    out = []
    for s in samples:
        score = model.predict(s.features)
        predicted_up = score >= 0.0
        expected = score * s.return_scale
        signal = bool(
            abs(score) >= threshold_z
            and abs(expected) >= cost_multiple * s.round_trip_cost
        )
        signed = s.future_return if predicted_up else -s.future_return
        out.append(Observation(
            symbol=s.symbol,
            decision_ts=s.decision_ts,
            score_z=score,
            p_up=_p_up(score),
            predicted_up=predicted_up,
            actual_up=s.actual_up,
            actual_return=s.future_return,
            expected_return=expected,
            gross_return=signed if signal else 0.0,
            net_return=(signed - s.round_trip_cost) if signal else 0.0,
            signaled=signal,
        ))
    return out


def _corr(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
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
    out = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [r for r in rows if lo <= r.p_up < hi or (b == bins - 1 and r.p_up == 1.0)]
        if group:
            out += len(group) / len(rows) * abs(
                mean(r.p_up for r in group) - mean(float(r.actual_up) for r in group)
            )
    return out


def _bootstrap(values: list[float], samples: int = 1500, seed: int = 41) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(samples)
    )
    return means[int(0.025 * (samples - 1))], means[int(0.975 * (samples - 1))]


def metrics(rows: list[Observation], *, bootstrap: bool = False) -> dict:
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
    actual_rate = mean(float(r.actual_up) for r in rows)
    brier = mean((r.p_up - float(r.actual_up)) ** 2 for r in rows)
    naive = mean((actual_rate - float(r.actual_up)) ** 2 for r in rows)
    net = [r.net_return for r in signals]
    low, high = _bootstrap(net) if bootstrap and net else (0.0, 0.0)
    return {
        "samples": len(rows),
        "signals": len(signals),
        "coverage": len(signals) / len(rows),
        "directional_accuracy": mean(float(r.predicted_up == r.actual_up) for r in signals) if signals else 0.0,
        "brier_up": brier,
        "brier_naive": naive,
        "brier_skill": 1.0 - brier / naive if naive > 0 else 0.0,
        "ece": _ece(rows),
        "ic": _corr([r.score_z for r in rows], [r.actual_return for r in rows]),
        "mean_gross_return": mean(r.gross_return for r in signals) if signals else 0.0,
        "mean_net_return": mean(net) if net else 0.0,
        "positive_net_fraction": mean(float(x > 0) for x in net) if net else 0.0,
        "bootstrap_net_low": low,
        "bootstrap_net_high": high,
    }


def _selection_key(rep: dict) -> tuple:
    sufficient = rep["signals"] >= 35 and rep["coverage"] >= 0.10
    return (
        1 if sufficient else 0,
        rep["mean_net_return"],
        rep["brier_skill"],
        rep["ic"],
    )


async def _fetch(client, symbols: tuple[str, ...], limit: int) -> dict[str, list[dict]]:
    out = {}
    for symbol in symbols:
        rows = await fetch_history(client, symbol, "15", limit)
        if len(rows) >= min(1200, limit):
            out[symbol] = rows
    return out


async def run_v4(
    *,
    limit_15m: int = 1700,
    tune_step: int = 24,
    holdout_step: int = 12,
) -> dict:
    async with PublicKuCoinFuturesClient() as client:
        dev_raw = await _fetch(client, DEV_SYMBOLS, limit_15m)
        hold_raw = await _fetch(client, FRESH_HOLDOUT_SYMBOLS, limit_15m)

    dev = align_panel(dev_raw)
    hold = align_panel(hold_raw)
    if len(dev) < 8:
        raise RuntimeError(f"insufficient dev panel: {sorted(dev)}")
    if len(hold) < 4:
        raise RuntimeError(f"insufficient fresh holdout panel: {sorted(hold)}")

    hold_n = min(len(x) for x in hold.values())
    hold_start_index = max(300, int(hold_n * 0.75))
    holdout_start_ts = _ts_ms(next(iter(hold.values()))[hold_start_index - 1])

    dev_ts = [_ts_ms(x) for x in next(iter(dev.values()))]
    eligible_dev_ts = [t for t in dev_ts if t < holdout_start_ts]
    if len(eligible_dev_ts) < 700:
        raise RuntimeError("insufficient chronologically prior dev history")
    train_cut_ts = eligible_dev_ts[int(len(eligible_dev_ts) * 0.70)]

    screens = {}
    sample_cache = {}
    for horizon in HORIZONS:
        samples = build_samples(
            dev, tuple(dev), horizon=horizon, step=tune_step, max_ts=holdout_start_ts
        )
        train = [s for s in samples if s.decision_ts < train_cut_ts]
        validation = [s for s in samples if s.decision_ts >= train_cut_ts]
        sample_cache[horizon] = (train, validation)
        for ridge in RIDGES:
            model = fit_ridge(train, ridge)
            for threshold in THRESHOLDS:
                key = f"H{horizon}:R{ridge}:T{threshold}"
                screens[key] = metrics(evaluate(model, validation, threshold_z=threshold))

    selected_key = max(screens, key=lambda k: _selection_key(screens[k]))
    h_text, r_text, t_text = selected_key.split(":")
    horizon = int(h_text[1:])
    ridge = float(r_text[1:])
    threshold = float(t_text[1:])

    # Refit only on development-symbol outcomes that predate the fresh holdout.
    refit_samples = build_samples(
        dev, tuple(dev), horizon=horizon, step=tune_step, max_ts=holdout_start_ts
    )
    model = fit_ridge(refit_samples, ridge)

    hold_samples = build_samples(
        hold,
        tuple(hold),
        horizon=horizon,
        step=holdout_step,
        min_ts=holdout_start_ts,
    )
    hold_obs = evaluate(model, hold_samples, threshold_z=threshold)
    pooled = metrics(hold_obs, bootstrap=True)
    by_symbol = {
        symbol: metrics([r for r in hold_obs if r.symbol == symbol], bootstrap=True)
        for symbol in sorted(hold)
    }
    positive_symbols = sum(1 for x in by_symbol.values() if x["mean_net_return"] > 0)
    train_max_ts = max(s.decision_ts for s in refit_samples)
    test_min_ts = min(s.decision_ts for s in hold_samples) if hold_samples else 0

    passed = bool(
        len(by_symbol) >= 4
        and pooled["signals"] >= 50
        and pooled["coverage"] >= 0.12
        and pooled["mean_net_return"] > 0.0
        and pooled["bootstrap_net_low"] > 0.0
        and pooled["directional_accuracy"] > 0.50
        and pooled["brier_skill"] > 0.0
        and pooled["ic"] > 0.0
        and positive_symbols >= 3
        and train_max_ts < test_min_ts
    )

    return {
        "status": "EDGE_SCREEN_PASS" if passed else "EDGE_SCREEN_FAIL",
        "promotion_authority": False,
        "runtime_effect": False,
        "architecture": "MODEL_H_V4_INVARIANT_CROSS_SECTIONAL_RIDGE",
        "development_universe": sorted(dev),
        "fresh_unseen_holdout_universe": sorted(hold),
        "selection": {
            "selected_key": selected_key,
            "horizon_15m_bars": horizon,
            "horizon_minutes": 15 * horizon,
            "ridge": ridge,
            "threshold_z": threshold,
            "cost_multiple": COST_MULTIPLE,
            "zero_intercept": True,
            "prevalence_calibration": False,
        },
        "temporal_fencing": {
            "train_max_decision_ts": train_max_ts,
            "holdout_min_decision_ts": test_min_ts,
            "strictly_separated": train_max_ts < test_min_ts,
        },
        "development_validation": {
            "screens": screens,
            "selected": screens[selected_key],
        },
        "fresh_symbol_holdout": {
            "pooled": pooled,
            "positive_net_symbols": positive_symbols,
            "symbols": by_symbol,
        },
        "model": {
            "feature_count": len(model.weights),
            "weights": list(model.weights),
            "means": list(model.means),
            "scales": list(model.scales),
        },
        "methodology": {
            "symbol_identity_feature": False,
            "zero_intercept": True,
            "probability_prevalence_calibration": False,
            "cross_sectional_ranks": True,
            "market_factor_features": True,
            "residual_features": True,
            "market_language_edge_feature": True,
            "fresh_symbols_for_final_test": True,
            "fresh_test_after_training_time": True,
            "fees_included": True,
            "slippage_included": True,
            "funding_included": False,
            "external_model_dependency": False,
            "exchange_mutations": False,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit-15m", type=int, default=1700)
    p.add_argument("--tune-step", type=int, default=24)
    p.add_argument("--holdout-step", type=int, default=12)
    p.add_argument("--output", default="artifacts/model_h_v4_evidence.json")
    a = p.parse_args()
    report = asyncio.run(run_v4(
        limit_15m=a.limit_15m,
        tune_step=a.tune_step,
        holdout_step=a.holdout_step,
    ))
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "selection": report["selection"],
        "temporal_fencing": report["temporal_fencing"],
        "holdout": report["fresh_symbol_holdout"]["pooled"],
        "positive_net_symbols": report["fresh_symbol_holdout"]["positive_net_symbols"],
        "holdout_symbols": report["fresh_unseen_holdout_universe"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
