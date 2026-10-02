"""Shadow-only distribution drift telemetry for NEXUS.

Consumes the existing NEXUS decision ledger. It measures changes in score,
confidence, regime, approval rate, execution-cost inputs and realized shadow
outcomes. It never changes thresholds, probabilities, risk, sizing or execution.
"""
from __future__ import annotations

import math
import time
from typing import Iterable, Mapping

from bot import nexus_persistence
from bot.drift_shadow import evaluate_drift
from bot.logger import log


_MIN_BUCKET = 10
_REFRESH_SECONDS = 900.0
_last_refresh_monotonic = 0.0
_cache = {
    "status": "UNINITIALIZED",
    "execution_effect": "NONE",
    "updated_at": None,
}


def _finite(value):
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _numeric(rows: Iterable[Mapping[str, object]], getter) -> list[float]:
    out = []
    for row in rows:
        value = _finite(getter(row))
        if value is not None:
            out.append(value)
    return out


def _categorical(rows: Iterable[Mapping[str, object]], getter) -> list[str]:
    out = []
    for row in rows:
        value = getter(row)
        if value is None:
            continue
        out.append(str(value))
    return out


def _cost(row):
    raw = row.get("raw") if isinstance(row, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    cost = raw.get("_cost")
    return cost if isinstance(cost, Mapping) else {}


def _signal_score(row):
    raw = row.get("raw") if isinstance(row, Mapping) else {}
    return (raw or {}).get("_signal_score") if isinstance(raw, Mapping) else None


def _outcome_summary(rows: list[Mapping[str, object]]) -> dict:
    values = []
    for row in rows:
        status = str(row.get("shadow_status", ""))
        value = _finite(row.get("shadow_r"))
        if value is None or status in {"PENDING", "AMBIGUOUS", "INVALID"}:
            continue
        values.append(value)
    if not values:
        return {"n": 0, "expectancy_r": None, "hit_rate": None}
    return {
        "n": len(values),
        "expectancy_r": sum(values) / len(values),
        "hit_rate": sum(1 for value in values if value > 0) / len(values),
    }


def evaluate_history(
    newest_first: list[Mapping[str, object]],
    *,
    baseline_n: int = 100,
    current_n: int = 40,
) -> dict:
    """Compare an earlier baseline window with the most recent current window."""
    if baseline_n < _MIN_BUCKET or current_n < _MIN_BUCKET:
        raise ValueError("drift windows too small")
    ordered = list(reversed(newest_first))
    required = baseline_n + current_n
    if len(ordered) < required:
        return {
            "status": "INSUFFICIENT_HISTORY",
            "available_rows": len(ordered),
            "required_rows": required,
            "execution_effect": "NONE",
        }

    baseline = ordered[-required:-current_n]
    current = ordered[-current_n:]

    numeric_getters = {
        "nexus_score": lambda row: row.get("nexus_score"),
        "confidence": lambda row: row.get("confidence"),
        "signal_score": _signal_score,
        "spread_bps": lambda row: _cost(row).get("spread_bps"),
        "taker_fee": lambda row: _cost(row).get("taker_fee"),
        "entry_slippage": lambda row: _cost(row).get("entry_slippage"),
        "exit_slippage": lambda row: _cost(row).get("exit_slippage"),
        "funding_rate": lambda row: _cost(row).get("funding_rate"),
    }
    ref_numeric = {}
    cur_numeric = {}
    skipped_numeric = []
    for name, getter in numeric_getters.items():
        ref = _numeric(baseline, getter)
        cur = _numeric(current, getter)
        if len(ref) >= _MIN_BUCKET and len(cur) >= _MIN_BUCKET:
            ref_numeric[name] = ref
            cur_numeric[name] = cur
        else:
            skipped_numeric.append(name)

    categorical_getters = {
        "regime": lambda row: row.get("regime"),
        "side": lambda row: row.get("side"),
        "approved": lambda row: "APPROVE" if row.get("approved") else "VETO",
        "shadow_status": lambda row: row.get("shadow_status"),
        "cost_fallback": lambda row: (
            "FALLBACK" if bool(_cost(row).get("fallback")) else "MEASURED"
        ),
    }
    ref_cat = {}
    cur_cat = {}
    for name, getter in categorical_getters.items():
        ref = _categorical(baseline, getter)
        cur = _categorical(current, getter)
        if len(ref) >= _MIN_BUCKET and len(cur) >= _MIN_BUCKET:
            ref_cat[name] = ref
            cur_cat[name] = cur

    drift = evaluate_drift(
        ref_numeric,
        cur_numeric,
        reference_categorical=ref_cat,
        current_categorical=cur_cat,
    )
    drift.update({
        "baseline_rows": len(baseline),
        "current_rows": len(current),
        "skipped_numeric": skipped_numeric,
        "baseline_outcomes": _outcome_summary(baseline),
        "current_outcomes": _outcome_summary(current),
        "execution_effect": "NONE",
    })
    return drift


async def refresh(
    *,
    limit: int = 300,
    baseline_n: int = 100,
    current_n: int = 40,
    force: bool = False,
) -> dict:
    """Refresh cached drift at most once per 15 minutes unless explicitly forced."""
    global _last_refresh_monotonic, _cache
    now = time.monotonic()
    if not force and now - _last_refresh_monotonic < _REFRESH_SECONDS:
        return dict(_cache)

    rows = await nexus_persistence.recent_observations(limit=limit)
    result = evaluate_history(
        rows,
        baseline_n=baseline_n,
        current_n=current_n,
    )
    result["updated_at"] = time.time()
    _cache = result
    _last_refresh_monotonic = now

    status = str(result.get("status", "UNKNOWN"))
    if status not in {"STABLE", "INSUFFICIENT_HISTORY"}:
        log.warning(
            "[NEXUS_SHADOW_DRIFT] status=%s baseline_rows=%s current_rows=%s "
            "execution_effect=NONE",
            status,
            result.get("baseline_rows", "NA"),
            result.get("current_rows", "NA"),
        )
    else:
        log.info(
            "[NEXUS_SHADOW_DRIFT] status=%s execution_effect=NONE",
            status,
        )
    return dict(result)


def get_cached() -> dict:
    return dict(_cache)
