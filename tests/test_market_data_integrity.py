import time
from types import SimpleNamespace

from bot.market_data_integrity import (
    closed_candles,
    prepare_strategy_series,
    validate_nexus_candles,
)
from bot.nexus_types import DataQuality
from bot import nexus_decision_consistency as consistency


def _bar(ts_ms, price=100.0, volume=10.0):
    return {
        "ts": int(ts_ms),
        "o": price,
        "h": price + 1,
        "l": price - 1,
        "c": price + 0.25,
        "v": volume,
    }


def _series(n, interval_ms, last_close_ms):
    last_start = int(last_close_ms - interval_ms)
    first_start = last_start - (n - 1) * interval_ms
    return [_bar(first_start + i * interval_ms, 100 + i * 0.01) for i in range(n)]


def test_closed_selection_uses_time_boundary_not_list_length():
    now = 1_789_145_700_000  # fixed deterministic timestamp
    width = 15 * 60 * 1000
    closed_start = now - width - 60_000
    open_start = now - 60_000
    bars = [_bar(closed_start), _bar(open_start)]

    selected = closed_candles(bars, "15", now_ms=now, require_timestamps=True)
    assert [x["ts"] for x in selected] == [closed_start]

    # If the feed contains only already-closed bars, the final bar is retained;
    # no unconditional ``[:-1]`` is applied merely because the list is long.
    only_closed = [_bar(closed_start - i * width) for i in reversed(range(61))]
    selected2 = closed_candles(only_closed, "15", now_ms=now, require_timestamps=True)
    assert len(selected2) == 61
    assert selected2[-1]["ts"] == closed_start


def test_strategy_adapter_preserves_last_confirmed_bar_after_legacy_slice():
    now = 1_789_145_700_000
    width = 15 * 60 * 1000
    last_close = now - 60_000
    closed = _series(60, width, last_close)

    prepared = prepare_strategy_series(closed, "15", 20, now_ms=now)
    assert len(prepared) == 61
    # Canonical/adaptive analyzers still slice [-1]; the sentinel ensures that
    # operation removes only the sentinel, not the last confirmed market bar.
    consumed = prepared[:-1]
    assert len(consumed) == 60
    assert consumed[-1]["ts"] == closed[-1]["ts"]


def test_nexus_hard_gate_blocks_candles_more_than_24h_old():
    now = 1_789_200_000_000
    old_close = now - 24 * 60 * 60 * 1000
    k15 = _series(60, 15 * 60_000, old_close)
    k1h = _series(40, 60 * 60_000, old_close)
    k4h = _series(20, 240 * 60_000, old_close)

    ok, reason, _ = validate_nexus_candles(k15, k1h, k4h, now_ms=now)
    assert ok is False
    assert "hard_stale" in reason


def test_nexus_hard_gate_accepts_fresh_contiguous_closed_series():
    now = 1_789_200_000_000
    last_close = now - 5 * 60_000
    k15 = _series(60, 15 * 60_000, last_close)
    k1h = _series(40, 60 * 60_000, last_close)
    k4h = _series(20, 240 * 60_000, last_close)

    ok, reason, views = validate_nexus_candles(k15, k1h, k4h, now_ms=now)
    assert ok is True
    assert reason == "ok"
    assert tuple(map(len, views)) == (60, 40, 20)


def test_nexus_hard_gate_blocks_abnormal_recent_gap():
    now = 1_789_200_000_000
    width = 15 * 60_000
    last_close = now - 5 * 60_000
    # Start with 62 contiguous bars, then remove two adjacent recent bars.
    # Sixty bars remain (so minimum-history validation still passes), while the
    # resulting three-interval jump deterministically exercises the gap gate.
    k15 = _series(62, width, last_close)
    del k15[-3]
    del k15[-3]
    assert len(k15) == 60
    k1h = _series(40, 60 * 60_000, last_close)
    k4h = _series(20, 240 * 60_000, last_close)

    ok, reason, _ = validate_nexus_candles(k15, k1h, k4h, now_ms=now)
    assert ok is False
    assert "abnormal_gap" in reason


def test_nexus_validate_data_wrapper_turns_hard_integrity_failure_into_zero_quality():
    now = int(time.time() * 1000)
    old_close = now - 24 * 60 * 60 * 1000
    k15 = _series(60, 15 * 60_000, old_close)
    k1h = _series(40, 60 * 60_000, old_close)
    k4h = _series(20, 240 * 60_000, old_close)

    fake = SimpleNamespace()
    fake.decide = lambda *args, **kwargs: SimpleNamespace(decision="WAIT", setup_quality=0.0)
    fake.detect_regime = lambda *args, **kwargs: ("RANGE", {})
    fake.validate_data = lambda *args, **kwargs: DataQuality()
    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    consistency.install(fake, logger)
    dq = fake.validate_data("BTCUSDT", k15, k1h, k4h)
    assert dq.is_acceptable is False
    assert dq.score == 0.0
    assert any("CRITICAL_CANDLE_INTEGRITY" in e for e in dq.errors)
