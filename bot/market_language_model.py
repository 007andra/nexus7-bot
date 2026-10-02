"""MODEL H — native market-language sequence model for the NEXUS ensemble."""
from __future__ import annotations

from typing import List

from bot.market_language import forecast_distribution, signal_from_forecast
from bot.nexus_types import Decision, ModelOutput


def model_market_language(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    volumes: List[float],
) -> ModelOutput:
    """Probabilistic sequence model built from NEXUS-native hierarchical tokens."""
    model = ModelOutput(name="MARKET_LANGUAGE")

    if len(closes) < 64:
        model.available = False
        model.reason = "dados insuficientes (<64 candles)"
        return model

    try:
        forecast = forecast_distribution(closes, highs, lows, volumes)
        signal = signal_from_forecast(forecast)

        if signal["direction"] == "WAIT":
            # The data exists, but the sequence does not contain enough usable
            # probabilistic edge to deserve a vote in the ensemble.
            model.available = False
            model.reason = (
                "edge sequencial insuficiente "
                f"p_up={forecast.probability_up:.2f} "
                f"p_down={forecast.probability_down:.2f} "
                f"entropy={forecast.entropy:.2f}"
            )
        else:
            model.direction = (
                Decision.LONG if signal["direction"] == "LONG" else Decision.SHORT
            )
            model.confidence = float(signal["confidence"])
            model.risk_score = float(signal["risk_score"])
            model.reason = (
                f"{signal['direction']} p={signal['major_probability']:.2f} "
                f"median={forecast.median_return * 100:+.3f}% "
                f"q10/q90={forecast.q10_return * 100:+.3f}%/"
                f"{forecast.q90_return * 100:+.3f}%"
            )

        model.details = {
            **forecast.to_dict(),
            "native": True,
            "external_model": False,
            "hierarchical_tokens": True,
            "causal_normalization": True,
            "nucleus_sampling": True,
        }
    except Exception as exc:
        model.available = False
        model.reason = f"erro market-language: {exc}"

    return model
