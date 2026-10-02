from bot.market_language import MarketLanguageForecast
from bot.market_language_model import model_market_language


def _inputs(n=80):
    closes = [100.0 + i * 0.1 for i in range(n)]
    highs = [x + 0.2 for x in closes]
    lows = [x - 0.2 for x in closes]
    volumes = [1000.0 + (i % 7) * 10 for i in range(n)]
    return closes, highs, lows, volumes


def test_market_language_model_fails_closed_on_short_history():
    c, h, l, v = _inputs(40)
    out = model_market_language(c, h, l, v)
    assert out.name == "MARKET_LANGUAGE"
    assert out.available is False
    assert out.direction.value == "WAIT"


def test_market_language_model_emits_long_when_native_forecast_is_strong(monkeypatch):
    forecast = MarketLanguageForecast(
        probability_up=0.82,
        probability_down=0.18,
        probability_flat=0.0,
        median_return=0.012,
        q10_return=-0.002,
        q90_return=0.025,
        dispersion=0.008,
        entropy=0.43,
        evidence=20,
        horizon=4,
        sample_count=128,
    )

    import bot.market_language_model as mlm
    monkeypatch.setattr(mlm, "forecast_distribution", lambda *a, **k: forecast)

    c, h, l, v = _inputs()
    out = model_market_language(c, h, l, v)

    assert out.available is True
    assert out.direction.value == "LONG"
    assert out.confidence > 0
    assert out.details["native"] is True
    assert out.details["external_model"] is False


def test_market_language_model_abstains_instead_of_diluting_weak_edge(monkeypatch):
    forecast = MarketLanguageForecast(
        probability_up=0.52,
        probability_down=0.48,
        probability_flat=0.0,
        median_return=0.0001,
        q10_return=-0.01,
        q90_return=0.01,
        dispersion=0.006,
        entropy=0.63,
        evidence=20,
        horizon=4,
        sample_count=128,
    )

    import bot.market_language_model as mlm
    monkeypatch.setattr(mlm, "forecast_distribution", lambda *a, **k: forecast)

    c, h, l, v = _inputs()
    out = model_market_language(c, h, l, v)

    assert out.available is False
    assert out.direction.value == "WAIT"
    assert "edge sequencial insuficiente" in out.reason
