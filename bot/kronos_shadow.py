"""Kronos shadow-mode integration for NEXUS-7.

This module has no execution authority. It runs probabilistic forecast observation
out of band, summarizes forecast paths, and emits telemetry. It must never alter
NEXUS execution_allowed, sizing, risk gates, order state, or exchange dispatch.
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import aiohttp
import numpy as np

from bot.logger import log


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(high, value))


@dataclass(frozen=True)
class KronosShadowConfig:
    enabled: bool
    url: str
    timeout_s: float
    max_context: int
    horizon: int
    sample_count: int
    temperature: float
    top_p: float
    timeframe_minutes: int

    @classmethod
    def from_env(cls) -> "KronosShadowConfig":
        return cls(
            enabled=_env_bool("KRONOS_SHADOW_ENABLED", False),
            url=os.environ.get("KRONOS_SHADOW_URL", "").strip(),
            timeout_s=_bounded_float("KRONOS_SHADOW_TIMEOUT_S", 3.0, 0.2, 30.0),
            max_context=_bounded_int("KRONOS_MAX_CONTEXT", 400, 32, 512),
            horizon=_bounded_int("KRONOS_HORIZON", 16, 1, 120),
            sample_count=_bounded_int("KRONOS_SAMPLE_COUNT", 16, 1, 64),
            temperature=_bounded_float("KRONOS_TEMPERATURE", 1.0, 0.05, 3.0),
            top_p=_bounded_float("KRONOS_TOP_P", 0.9, 0.05, 1.0),
            timeframe_minutes=_bounded_int("KRONOS_TIMEFRAME_MINUTES", 15, 1, 1440),
        )


@dataclass(frozen=True)
class KronosForecastFeatures:
    symbol: str
    side: str
    sample_count: int
    direction_probability_pct: float
    median_return_pct: float
    mean_return_pct: float
    p10_return_pct: float
    p90_return_pct: float
    dispersion_pct: float
    mean_path_volatility_pct: float
    median_mfe_pct: float
    median_mae_pct: float
    tp_before_sl_pct: float
    sl_before_tp_pct: float
    ambiguous_barrier_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _bar_value(bar: dict, long_name: str, short_name: str) -> Optional[float]:
    value = bar.get(long_name, bar.get(short_name))
    return _number(value)


def _validate_path(path: Sequence[dict]) -> List[dict]:
    valid: List[dict] = []
    for bar in path:
        if not isinstance(bar, dict):
            continue
        o = _bar_value(bar, "open", "o")
        h = _bar_value(bar, "high", "h")
        l = _bar_value(bar, "low", "l")
        c = _bar_value(bar, "close", "c")
        if None in (o, h, l, c):
            continue
        if min(o, h, l, c) <= 0 or h < l or not (l <= o <= h) or not (l <= c <= h):
            continue
        valid.append({"open": o, "high": h, "low": l, "close": c})
    return valid


def _path_barrier_result(path: Sequence[dict], side: str, sl: float, tp: float) -> str:
    side = side.upper()
    for bar in path:
        high = bar["high"]
        low = bar["low"]
        if side == "LONG":
            hit_tp = high >= tp
            hit_sl = low <= sl
        else:
            hit_tp = low <= tp
            hit_sl = high >= sl
        if hit_tp and hit_sl:
            return "AMBIGUOUS"
        if hit_tp:
            return "TP"
        if hit_sl:
            return "SL"
    return "NONE"


def summarize_forecast_paths(
    *,
    symbol: str,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    paths: Sequence[Sequence[dict]],
) -> KronosForecastFeatures:
    """Convert stochastic OHLC forecast paths into execution-neutral diagnostics."""
    side = str(side).upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    entry = float(entry)
    sl = float(sl)
    tp = float(tp)
    if not all(math.isfinite(v) and v > 0 for v in (entry, sl, tp)):
        raise ValueError("entry/sl/tp must be finite positive numbers")
    if side == "LONG" and not (sl < entry < tp):
        raise ValueError("LONG levels must satisfy sl < entry < tp")
    if side == "SHORT" and not (tp < entry < sl):
        raise ValueError("SHORT levels must satisfy tp < entry < sl")

    signed_returns: List[float] = []
    path_vols: List[float] = []
    mfes: List[float] = []
    maes: List[float] = []
    barriers: List[str] = []

    for raw_path in paths:
        path = _validate_path(raw_path)
        if not path:
            continue

        final_close = path[-1]["close"]
        raw_ret = (final_close / entry) - 1.0
        signed_ret = raw_ret if side == "LONG" else -raw_ret
        signed_returns.append(signed_ret)

        closes = np.asarray([entry] + [bar["close"] for bar in path], dtype=float)
        log_rets = np.diff(np.log(closes))
        path_vols.append(float(np.std(log_rets)) if len(log_rets) else 0.0)

        if side == "LONG":
            mfe = max((bar["high"] - entry) / entry for bar in path)
            mae = max((entry - bar["low"]) / entry for bar in path)
        else:
            mfe = max((entry - bar["low"]) / entry for bar in path)
            mae = max((bar["high"] - entry) / entry for bar in path)
        mfes.append(max(0.0, float(mfe)))
        maes.append(max(0.0, float(mae)))
        barriers.append(_path_barrier_result(path, side, sl, tp))

    if not signed_returns:
        raise ValueError("no valid forecast paths")

    arr = np.asarray(signed_returns, dtype=float)
    n = len(arr)

    def pct(value: float) -> float:
        return round(float(value) * 100.0, 6)

    return KronosForecastFeatures(
        symbol=symbol,
        side=side,
        sample_count=n,
        direction_probability_pct=round(float(np.mean(arr > 0)) * 100.0, 4),
        median_return_pct=pct(np.median(arr)),
        mean_return_pct=pct(np.mean(arr)),
        p10_return_pct=pct(np.quantile(arr, 0.10)),
        p90_return_pct=pct(np.quantile(arr, 0.90)),
        dispersion_pct=pct(np.std(arr)),
        mean_path_volatility_pct=pct(np.mean(path_vols)),
        median_mfe_pct=pct(np.median(mfes)),
        median_mae_pct=pct(np.median(maes)),
        tp_before_sl_pct=round(barriers.count("TP") / n * 100.0, 4),
        sl_before_tp_pct=round(barriers.count("SL") / n * 100.0, 4),
        ambiguous_barrier_pct=round(barriers.count("AMBIGUOUS") / n * 100.0, 4),
    )


def build_candle_payload(k15: Sequence[dict], max_context: int) -> List[dict]:
    """Normalize exchange candle aliases without fabricating missing fields."""
    rows: List[dict] = []
    for candle in list(k15)[-max_context:]:
        if not isinstance(candle, dict):
            continue
        ts = _number(candle.get("ts", candle.get("timestamp")))
        o = _bar_value(candle, "open", "o")
        h = _bar_value(candle, "high", "h")
        l = _bar_value(candle, "low", "l")
        c = _bar_value(candle, "close", "c")
        v = _number(candle.get("volume", candle.get("v", 0.0)))
        if None in (ts, o, h, l, c):
            continue
        if min(o, h, l, c) <= 0 or h < l or not (l <= o <= h) or not (l <= c <= h):
            continue
        rows.append({
            "ts": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": max(0.0, v or 0.0),
        })
    return rows


_LATEST: Dict[str, Dict[str, Any]] = {}
_LAST_CANDLE: Dict[str, float] = {}
_BACKGROUND_TASKS: set = set()
_SEMAPHORE: Optional[asyncio.Semaphore] = None


def latest_snapshot(symbol: Optional[str] = None) -> Any:
    if symbol is None:
        return dict(_LATEST)
    return _LATEST.get(symbol)


def _get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(_bounded_int("KRONOS_SHADOW_CONCURRENCY", 2, 1, 8))
    return _SEMAPHORE


def _endpoint(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/forecast") else f"{url}/forecast"


async def observe(
    *,
    symbol: str,
    side: str,
    k15: Sequence[dict],
    entry: float,
    sl: float,
    tp: float,
    nexus_snapshot: Optional[dict] = None,
    config: Optional[KronosShadowConfig] = None,
) -> Optional[Dict[str, Any]]:
    """Run one shadow observation. All failures are isolated from trading execution."""
    cfg = config or KronosShadowConfig.from_env()
    if not cfg.enabled or not cfg.url:
        return None

    candles = build_candle_payload(k15, cfg.max_context)
    if len(candles) < 32:
        log.info("[KRONOS_SHADOW] symbol=%s status=skip reason=insufficient_context candles=%s", symbol, len(candles))
        return None

    payload = {
        "symbol": symbol,
        "timeframe_minutes": cfg.timeframe_minutes,
        "horizon": cfg.horizon,
        "sample_count": cfg.sample_count,
        "temperature": cfg.temperature,
        "top_p": cfg.top_p,
        "candles": candles,
    }

    try:
        async with _get_semaphore():
            timeout = aiohttp.ClientTimeout(total=cfg.timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(_endpoint(cfg.url), json=payload) as response:
                    if response.status != 200:
                        body = (await response.text())[:200]
                        raise RuntimeError(f"provider_http_{response.status}:{body}")
                    data = await response.json()

        paths = data.get("paths") if isinstance(data, dict) else None
        if not isinstance(paths, list):
            raise ValueError("provider response missing paths")

        features = summarize_forecast_paths(
            symbol=symbol,
            side=side,
            entry=entry,
            sl=sl,
            tp=tp,
            paths=paths,
        )
        snapshot = features.to_dict()
        snapshot.update({
            "status": "ok",
            "model": data.get("model"),
            "tokenizer": data.get("tokenizer"),
            "nexus_decision": (nexus_snapshot or {}).get("decision"),
            "nexus_score": (nexus_snapshot or {}).get("setup_quality"),
            "nexus_confidence": (nexus_snapshot or {}).get("confidence"),
        })
        _LATEST[symbol] = snapshot
        log.info(
            "[KRONOS_SHADOW] symbol=%s side=%s status=ok samples=%s dir_prob=%.2f "
            "median_ret=%.4f dispersion=%.4f tp_first=%.2f sl_first=%.2f model=%s",
            symbol,
            side,
            features.sample_count,
            features.direction_probability_pct,
            features.median_return_pct,
            features.dispersion_pct,
            features.tp_before_sl_pct,
            features.sl_before_tp_pct,
            data.get("model", "unknown"),
        )
        return snapshot
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "[KRONOS_SHADOW] symbol=%s status=error error=%s",
            symbol,
            type(exc).__name__,
        )
        return None


def schedule_observation(
    *,
    symbol: str,
    side: str,
    k15: Sequence[dict],
    entry: float,
    sl: float,
    tp: float,
    nexus_decision: Any,
) -> bool:
    """Schedule shadow work and immediately return; never block the execution path."""
    cfg = KronosShadowConfig.from_env()
    if not cfg.enabled or not cfg.url or not k15:
        return False

    candle_ts = _number(k15[-1].get("ts", k15[-1].get("timestamp"))) if isinstance(k15[-1], dict) else None
    if candle_ts is not None and _LAST_CANDLE.get(symbol) == candle_ts:
        return False
    if candle_ts is not None:
        _LAST_CANDLE[symbol] = candle_ts

    nexus_snapshot = None
    try:
        nexus_snapshot = nexus_decision.to_dict()
    except Exception:
        nexus_snapshot = {
            "decision": getattr(nexus_decision, "decision", None),
            "setup_quality": getattr(nexus_decision, "setup_quality", None),
            "confidence": getattr(nexus_decision, "confidence", None),
        }

    task = asyncio.create_task(observe(
        symbol=symbol,
        side=side,
        k15=list(k15),
        entry=entry,
        sl=sl,
        tp=tp,
        nexus_snapshot=nexus_snapshot,
        config=cfg,
    ))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return True
