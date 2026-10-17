from types import SimpleNamespace
from bot import nexus_decision_consistency as consistency


def _candles(n):
    return [
        {"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.0, "v": 10.0, "ts": i}
        for i in range(n)
    ]


def _fake_ai(decide, detect_regime=None):
    if detect_regime is None:
        detect_regime = lambda closes, highs, lows, volumes: ("RANGE", {})
    return SimpleNamespace(decide=decide, detect_regime=detect_regime)


def test_closed_mtf_removes_forming_candle_when_minimum_is_preserved():
    k15, k1h, k4h = consistency.closed_mtf(
        _candles(100), _candles(100), _candles(100)
    )
    assert len(k15) == 99
    assert len(k1h) == 99
    assert len(k4h) == 99
    assert k15[-1]["ts"] == 98
    assert k1h[-1]["ts"] == 98
    assert k4h[-1]["ts"] == 98


def test_closed_mtf_does_not_drop_below_nexus_minimum_history():
    k15, k1h, k4h = consistency.closed_mtf(
        _candles(60), _candles(40), _candles(20)
    )
    assert len(k15) == 60
    assert len(k1h) == 40
    assert len(k4h) == 20


def test_install_routes_decision_through_confirmed_candles_only():
    observed = {}

    def original(symbol, k15, k1h, k4h, *args, **kwargs):
        observed["symbol"] = symbol
        observed["lengths"] = (len(k15), len(k1h), len(k4h))
        observed["last_ts"] = (k15[-1]["ts"], k1h[-1]["ts"], k4h[-1]["ts"])
        return SimpleNamespace(decision="WAIT", setup_quality=0.0)

    fake_ai = _fake_ai(original)
    fake_log = SimpleNamespace(info=lambda *args, **kwargs: None,
                               warning=lambda *args, **kwargs: None)

    consistency.install(fake_ai, fake_log)
    result = fake_ai.decide("ETHUSDT", _candles(100), _candles(100), _candles(100))

    assert result.decision == "WAIT"
    assert observed["symbol"] == "ETHUSDT"
    assert observed["lengths"] == (99, 99, 99)
    assert observed["last_ts"] == (98, 98, 98)


def test_entry_regime_detection_uses_confirmed_4h_context_not_15m_series():
    observed = []

    def detect_regime(closes, highs, lows, volumes):
        observed.append(len(closes))
        return ("TRENDING_BULL", {})

    def original(symbol, k15, k1h, k4h, *args, **kwargs):
        closes = [float(k["c"]) for k in k15]
        highs = [float(k["h"]) for k in k15]
        lows = [float(k["l"]) for k in k15]
        volumes = [float(k["v"]) for k in k15]
        # Mirrors nexus_ai.decide: it asks detect_regime using 15m arrays.
        fake_ai.detect_regime(closes, highs, lows, volumes)
        return SimpleNamespace(decision="WAIT", setup_quality=0.0)

    fake_ai = _fake_ai(original, detect_regime=detect_regime)
    fake_log = SimpleNamespace(info=lambda *args, **kwargs: None,
                               warning=lambda *args, **kwargs: None)

    consistency.install(fake_ai, fake_log)
    fake_ai.decide("ETHUSDT", _candles(200), _candles(100), _candles(120))

    # 4H cache has 120 bars; confirmed view removes the forming bar => 119.
    # If the old 15M behavior were used this would be 199 instead.
    assert observed == [119]


def test_detect_regime_outside_entry_decision_retains_original_inputs():
    observed = []

    def detect_regime(closes, highs, lows, volumes):
        observed.append(len(closes))
        return ("RANGE", {})

    fake_ai = _fake_ai(
        lambda *args, **kwargs: SimpleNamespace(decision="WAIT", setup_quality=0.0),
        detect_regime=detect_regime,
    )
    fake_log = SimpleNamespace(info=lambda *args, **kwargs: None,
                               warning=lambda *args, **kwargs: None)
    consistency.install(fake_ai, fake_log)

    fake_ai.detect_regime([1] * 15, [1] * 15, [1] * 15, [1] * 15)
    assert observed == [15]


def test_wait_score_snapshot_is_handed_back_without_execution_change():
    def original(symbol, k15, k1h, k4h, *args, **kwargs):
        return SimpleNamespace(
            decision="WAIT",
            setup_quality=0.0,
            reasoning=["aguardando confirmação"],
        )

    fake_ai = _fake_ai(original)
    fake_log = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )

    consistency.install(fake_ai, fake_log)
    result = fake_ai.decide(
        "SOLUSDT", _candles(100), _candles(100), _candles(100)
    )

    assert result.decision == "WAIT"
    snapshot = getattr(result, "_bgx_score_snapshot")
    assert isinstance(snapshot, dict)
    assert "ok" in snapshot


def test_install_is_idempotent():
    calls = {"n": 0}

    def original(symbol, k15, k1h, k4h, *args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(decision="WAIT", setup_quality=0.0)

    fake_ai = _fake_ai(original)
    fake_log = SimpleNamespace(info=lambda *args, **kwargs: None,
                               warning=lambda *args, **kwargs: None)

    consistency.install(fake_ai, fake_log)
    first = fake_ai.decide
    first_regime = fake_ai.detect_regime
    consistency.install(fake_ai, fake_log)
    second = fake_ai.decide
    second_regime = fake_ai.detect_regime

    assert first is second
    assert first_regime is second_regime
    second("BTCUSDT", _candles(100), _candles(100), _candles(100))
    assert calls["n"] == 1
