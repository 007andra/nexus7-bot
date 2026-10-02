"""Runtime composition for native NEXUS market-language MODEL H.

Keeps the protected production sources bot/nexus_ai.py and bot/nexus_models.py
byte-identical. The overlay extends the already-composed NEXUS ensemble at
runtime and captures the current k15 candle geometry/timestamps so MODEL H can
use native temporal context without changing canonical authority modules.
"""
from __future__ import annotations

import contextvars
import functools
from typing import Any

from bot.market_language import model_market_language


_K15_CONTEXT: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "nexus_market_language_k15", default=None
)


def _arg(args, kwargs, name: str, index: int):
    if name in kwargs:
        return kwargs[name]
    if len(args) > index:
        return args[index]
    return None


def _context_arrays(k15, expected_len: int):
    rows = list(k15 or [])
    if not rows or len(rows) < expected_len:
        return None, None
    rows = rows[-expected_len:]
    opens = []
    timestamps = []
    for row in rows:
        if not isinstance(row, dict):
            return None, None
        close = row.get("c", row.get("close"))
        opens.append(row.get("o", row.get("open", close)))
        timestamps.append(row.get("ts", row.get("time", row.get("timestamp"))))
    return opens, timestamps


def install(nexus_ai, log) -> None:
    """Append MODEL H to the existing ensemble without replacing authorities."""
    if getattr(nexus_ai, "_market_language_overlay_installed", False):
        return

    original_run_ensemble = nexus_ai.run_ensemble
    original_decide = nexus_ai.decide
    original_monitor = nexus_ai.monitor_position

    @functools.wraps(original_run_ensemble)
    def run_ensemble_with_market_language(*args, **kwargs):
        models = list(original_run_ensemble(*args, **kwargs))
        if any(getattr(model, "name", "") == "MARKET_LANGUAGE" for model in models):
            return models

        closes = _arg(args, kwargs, "closes", 0)
        highs = _arg(args, kwargs, "highs", 1)
        lows = _arg(args, kwargs, "lows", 2)
        volumes = _arg(args, kwargs, "volumes", 3)
        if not all(value is not None for value in (closes, highs, lows, volumes)):
            return models

        n = min(len(closes), len(highs), len(lows), len(volumes))
        opens, timestamps = _context_arrays(_K15_CONTEXT.get(), n)
        try:
            market_model = model_market_language(
                closes,
                highs,
                lows,
                volumes,
                opens=opens,
                timestamps=timestamps,
            )
        except Exception as exc:
            log.debug(
                "[MARKET_LANGUAGE] model_error=%s action=ABSTAIN execution_effect=NONE",
                type(exc).__name__,
            )
            return models

        models.append(market_model)
        return models

    @functools.wraps(original_decide)
    def decide_with_market_language_context(*args, **kwargs):
        k15 = _arg(args, kwargs, "k15", 1)
        token = _K15_CONTEXT.set(list(k15 or []))
        try:
            return original_decide(*args, **kwargs)
        finally:
            _K15_CONTEXT.reset(token)

    @functools.wraps(original_monitor)
    def monitor_with_market_language_context(*args, **kwargs):
        k15 = _arg(args, kwargs, "k15", 6)
        token = _K15_CONTEXT.set(list(k15 or []))
        try:
            return original_monitor(*args, **kwargs)
        finally:
            _K15_CONTEXT.reset(token)

    nexus_ai.run_ensemble = run_ensemble_with_market_language
    nexus_ai.decide = decide_with_market_language_context
    nexus_ai.monitor_position = monitor_with_market_language_context
    nexus_ai._market_language_overlay_installed = True

    log.warning(
        "[MARKET_LANGUAGE] installed=true model=H native=true "
        "hierarchical_tokens=true causal_normalization=true temporal_context=true "
        "autoregressive_paths=true top_p=true batch_capable=true "
        "canonical_nexus_ai_unchanged=true canonical_nexus_models_unchanged=true "
        "order_authority=false leverage_authority=false sizing_authority=false"
    )
