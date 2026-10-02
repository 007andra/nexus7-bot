import pytest

from bot.feature_contract import (
    FeatureSchema,
    FeatureSpec,
    categorical_total_variation,
    drift_level,
    population_stability_index,
)


def test_feature_fingerprint_is_stable_and_order_independent():
    schema = FeatureSchema("v1", (FeatureSpec("atr"), FeatureSpec("score")))
    a = schema.fingerprint({"atr": 1.2, "score": 70}, symbol="btcusdt", decision_ts=123)
    b = schema.fingerprint({"score": 70, "atr": 1.2}, symbol="BTCUSDT", decision_ts=123)
    assert a == b


def test_feature_schema_rejects_unknown_or_nonfinite():
    schema = FeatureSchema("v1", (FeatureSpec("x"),))
    with pytest.raises(ValueError):
        schema.validate({"x": float("nan")})
    with pytest.raises(ValueError):
        schema.validate({"x": 1.0, "y": 2.0})


def test_numeric_and_categorical_drift():
    stable = population_stability_index(list(range(100)), list(range(100)))
    shifted = population_stability_index(list(range(100)), list(range(100, 200)))
    assert stable < 0.10
    assert shifted > stable
    assert drift_level(shifted) in {"WATCH", "DRIFT", "SEVERE"}
    assert categorical_total_variation(["A", "A", "B"], ["B", "B", "B"]) > 0.0
