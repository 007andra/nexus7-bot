"""Install the NEXUS-native market-language model without editing canonical authorities."""
from __future__ import annotations

import functools

from bot.market_language_model import model_market_language


def install(nexus_ai, nexus_models, log) -> None:
    """Append MODEL H to the existing ensemble through the sanctioned overlay layer."""
    if getattr(nexus_ai, "_market_language_runtime_installed", False):
        return

    original_run_ensemble = nexus_models.run_ensemble

    @functools.wraps(original_run_ensemble)
    def run_ensemble_with_market_language(
        closes, highs, lows, volumes,
        funding=None, oi_delta=None, ls_ratio=None,
    ):
        models = list(
            original_run_ensemble(
                closes, highs, lows, volumes,
                funding=funding,
                oi_delta=oi_delta,
                ls_ratio=ls_ratio,
            )
        )
        models.append(model_market_language(closes, highs, lows, volumes))
        return models

    nexus_models.run_ensemble = run_ensemble_with_market_language
    # nexus_ai imported run_ensemble by value, so update that module-level alias too.
    nexus_ai.run_ensemble = run_ensemble_with_market_language
    nexus_ai._market_language_runtime_installed = True

    log.warning(
        "[NEXUS_MARKET_LANGUAGE] installed model_h=true native=true "
        "hierarchical_tokens=true causal_normalization=true nucleus_sampling=true "
        "canonical_nexus_ai_unchanged=true canonical_nexus_models_unchanged=true "
        "execution_gates_unchanged=true"
    )
