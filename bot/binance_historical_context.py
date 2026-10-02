"""Leakage-safe Binance USD-M historical derivatives/microstructure context.

Research-only. Parses checksum-verified daily Binance Vision archives and
normalizes known upstream quirks without granting any trading authority.
"""
from __future__ import annotations

import csv
import io
import math
import zipfile
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Sequence


_METRICS_SHIFT_DATE = date(2026, 6, 25)
_BOOK_DEPTH_KNOWN_ISSUE_DATE = date(2026, 9, 3)


def _zip_csv_text(payload: bytes) -> str:
    if not payload:
        raise ValueError("empty Binance archive")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError("archive must contain exactly one CSV")
            return zf.read(names[0]).decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
        raise ValueError("invalid Binance context archive") from exc


def _utc_ms(value: object) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty timestamp")
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None and math.isfinite(numeric):
        ts = int(numeric)
        if ts < 100_000_000_000:
            ts *= 1000
        elif ts >= 100_000_000_000_000 and ts < 100_000_000_000_000_000:
            ts //= 1000
        elif ts >= 100_000_000_000_000_000:
            ts //= 1_000_000
        return ts
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"invalid timestamp {raw!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _finite(value: object, *, allow_zero: bool = True) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("non-numeric context value") from exc
    if not math.isfinite(out):
        raise ValueError("non-finite context value")
    if not allow_zero and out <= 0:
        raise ValueError("non-positive context value")
    return out


@dataclass(frozen=True)
class MetricsObservation:
    label_ts_ms: int
    effective_ts_ms: int
    symbol: str
    sum_open_interest: float
    sum_open_interest_value: float
    top_account_ls_ratio: float | None
    top_position_ls_ratio: float | None
    global_account_ls_ratio: float | None
    taker_ls_volume_ratio: float | None
    source_date: str
    convention: str

    def as_nexus_inputs(
        self,
        previous: "MetricsObservation | None" = None,
    ) -> dict:
        oi_delta = None
        if previous is not None and previous.sum_open_interest > 0:
            oi_delta = (
                self.sum_open_interest / previous.sum_open_interest - 1.0
            )
        return {
            "oi": {
                "openInterest": self.sum_open_interest,
                "openInterestValue": self.sum_open_interest_value,
                "timestamp": self.effective_ts_ms,
            },
            "oi_delta": oi_delta,
            "ls_ratio": self.top_position_ls_ratio,
            "taker_ls_ratio": self.taker_ls_volume_ratio,
        }


@dataclass(frozen=True)
class BookDepthBand:
    timestamp_ms: int
    percentage: float
    depth: float
    notional: float
    source_date: str


@dataclass(frozen=True)
class BookDepthSnapshot:
    timestamp_ms: int
    source_date: str
    bands: tuple[BookDepthBand, ...]
    quality: str

    def _side_value(self, pct: float, field: str) -> float | None:
        for band in self.bands:
            if math.isclose(band.percentage, pct, abs_tol=1e-9):
                return float(getattr(band, field))
        return None

    def summary(self) -> dict:
        bid1 = self._side_value(-1.0, "notional")
        ask1 = self._side_value(1.0, "notional")
        bid2 = self._side_value(-2.0, "notional")
        ask2 = self._side_value(2.0, "notional")
        bid5 = self._side_value(-5.0, "notional")
        ask5 = self._side_value(5.0, "notional")

        def imbalance(bid, ask):
            if bid is None or ask is None or bid + ask <= 0:
                return None
            return (bid - ask) / (bid + ask)

        return {
            "timestamp": self.timestamp_ms,
            "bid_notional_1pct": bid1,
            "ask_notional_1pct": ask1,
            "bid_notional_2pct": bid2,
            "ask_notional_2pct": ask2,
            "bid_notional_5pct": bid5,
            "ask_notional_5pct": ask5,
            "imbalance_1pct": imbalance(bid1, ask1),
            "imbalance_2pct": imbalance(bid2, ask2),
            "imbalance_5pct": imbalance(bid5, ask5),
            "quality": self.quality,
        }


def parse_metrics_archive(
    payload: bytes,
    *,
    source_date: str,
) -> tuple[MetricsObservation, ...]:
    """Parse 5m metrics and shift post-2026-06-25 labels to availability time.

    Before 2026-06-25 the archive label T describes a snapshot/flow ending at T.
    From 2026-06-25 onward observed archive semantics are start-labeled, so row T
    contains information from the next 5-minute period and is only safe at T+5m.
    """
    src_day = date.fromisoformat(str(source_date))
    text = _zip_csv_text(payload)
    reader = csv.DictReader(io.StringIO(text))
    required = {
        "create_time",
        "symbol",
        "sum_open_interest",
        "sum_open_interest_value",
        "count_toptrader_long_short_ratio",
        "sum_toptrader_long_short_ratio",
        "count_long_short_ratio",
        "sum_taker_long_short_vol_ratio",
    }
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError("unexpected Binance metrics schema")

    out = []
    previous_effective = -1
    convention = (
        "START_LABEL_SHIFTED_TO_AVAILABILITY"
        if src_day >= _METRICS_SHIFT_DATE
        else "END_LABEL"
    )
    for row in reader:
        label_ts = _utc_ms(row["create_time"])
        effective_ts = (
            label_ts + 5 * 60 * 1000
            if src_day >= _METRICS_SHIFT_DATE
            else label_ts
        )
        if effective_ts <= previous_effective:
            raise ValueError("non-monotonic metrics effective timestamp")
        previous_effective = effective_ts

        def optional(name: str) -> float | None:
            value = str(row.get(name, "") or "").strip()
            if value.lower() in {"", "nan", "none", "null"}:
                return None
            return _finite(value)

        out.append(MetricsObservation(
            label_ts_ms=label_ts,
            effective_ts_ms=effective_ts,
            symbol=str(row["symbol"]).upper(),
            sum_open_interest=_finite(row["sum_open_interest"]),
            sum_open_interest_value=_finite(row["sum_open_interest_value"]),
            top_account_ls_ratio=optional("count_toptrader_long_short_ratio"),
            top_position_ls_ratio=optional("sum_toptrader_long_short_ratio"),
            global_account_ls_ratio=optional("count_long_short_ratio"),
            taker_ls_volume_ratio=optional("sum_taker_long_short_vol_ratio"),
            source_date=src_day.isoformat(),
            convention=convention,
        ))
    return tuple(out)


def parse_book_depth_archive(
    payload: bytes,
    *,
    source_date: str,
) -> tuple[BookDepthSnapshot, ...]:
    """Parse cumulative Binance USD-M depth bands grouped by snapshot timestamp."""
    src_day = date.fromisoformat(str(source_date))
    text = _zip_csv_text(payload)
    reader = csv.DictReader(io.StringIO(text))
    required = {"timestamp", "percentage", "depth", "notional"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError("unexpected Binance bookDepth schema")

    grouped: dict[int, list[BookDepthBand]] = {}
    for row in reader:
        ts = _utc_ms(row["timestamp"])
        band = BookDepthBand(
            timestamp_ms=ts,
            percentage=_finite(row["percentage"]),
            depth=_finite(row["depth"]),
            notional=_finite(row["notional"]),
            source_date=src_day.isoformat(),
        )
        grouped.setdefault(ts, []).append(band)

    quality = (
        "KNOWN_UPSTREAM_ISSUE_QUARANTINE"
        if src_day >= _BOOK_DEPTH_KNOWN_ISSUE_DATE
        else "OK"
    )
    result = []
    for ts in sorted(grouped):
        bands = tuple(sorted(grouped[ts], key=lambda item: item.percentage))
        seen = [round(item.percentage, 8) for item in bands]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate bookDepth percentage band")
        result.append(BookDepthSnapshot(
            timestamp_ms=ts,
            source_date=src_day.isoformat(),
            bands=bands,
            quality=quality,
        ))
    return tuple(result)


class AggTradeTimeline:
    """Timestamp-indexed historical aggressor flow without future leakage."""

    def __init__(self, rows) -> None:
        values = sorted(
            tuple(rows),
            key=lambda row: (int(row.timestamp), int(row.aggregate_trade_id)),
        )
        self.rows = values
        self.timestamps = [int(row.timestamp) for row in values]

    def pressure(
        self,
        decision_ts_ms: int,
        *,
        window_ms: int = 5 * 60 * 1000,
    ) -> dict:
        """Return BUY-vs-SELL aggressor pressure using trades known by decision time."""
        decision_ts = int(decision_ts_ms)
        if decision_ts <= 0 or window_ms <= 0:
            raise ValueError("invalid aggTrades pressure window")
        right = bisect_right(self.timestamps, decision_ts)
        left = bisect_right(self.timestamps, decision_ts - int(window_ms))
        rows = self.rows[left:right]
        buy_notional = 0.0
        sell_notional = 0.0
        for row in rows:
            if row.aggressor_side == "BUY":
                buy_notional += float(row.notional)
            else:
                sell_notional += float(row.notional)
        total = buy_notional + sell_notional
        pressure = (
            (buy_notional - sell_notional) / total
            if total > 0 else None
        )
        latest_ts = int(rows[-1].timestamp) if rows else None
        return {
            "available": bool(rows) and total > 0,
            "window_ms": int(window_ms),
            "rows": len(rows),
            "buy_notional": buy_notional,
            "sell_notional": sell_notional,
            "taker_pressure": pressure,
            "latest_trade_ts_ms": latest_ts,
            "age_ms": (
                decision_ts - latest_ts if latest_ts is not None else None
            ),
            "future_rows_used": False,
            "execution_effect": "NONE",
            "score_effect": "NONE",
            "promotion_authority": False,
        }


class MetricsTimeline:
    def __init__(self, rows: Iterable[MetricsObservation]) -> None:
        values = sorted(rows, key=lambda row: row.effective_ts_ms)
        self.rows: tuple[MetricsObservation, ...] = tuple(values)
        self.timestamps = [row.effective_ts_ms for row in values]
        if len(self.timestamps) != len(set(self.timestamps)):
            raise ValueError("duplicate effective metrics timestamp")

    def asof(self, decision_ts_ms: int) -> tuple[MetricsObservation | None, MetricsObservation | None]:
        idx = bisect_right(self.timestamps, int(decision_ts_ms)) - 1
        if idx < 0:
            return None, None
        current = self.rows[idx]
        previous = self.rows[idx - 1] if idx > 0 else None
        return current, previous


class BookDepthTimeline:
    def __init__(self, rows: Iterable[BookDepthSnapshot]) -> None:
        values = sorted(rows, key=lambda row: row.timestamp_ms)
        self.rows: tuple[BookDepthSnapshot, ...] = tuple(values)
        self.timestamps = [row.timestamp_ms for row in values]
        if len(self.timestamps) != len(set(self.timestamps)):
            raise ValueError("duplicate bookDepth timestamp")

    def asof(self, decision_ts_ms: int) -> BookDepthSnapshot | None:
        idx = bisect_right(self.timestamps, int(decision_ts_ms)) - 1
        return self.rows[idx] if idx >= 0 else None


def shadow_microstructure_signal(snapshot: BookDepthSnapshot | None) -> dict:
    """Observational imbalance score only; no production score or order authority."""
    if snapshot is None:
        return {
            "available": False,
            "reason": "DATA_UNAVAILABLE",
            "execution_effect": "NONE",
        }
    summary = snapshot.summary()
    if snapshot.quality != "OK":
        return {
            "available": False,
            "reason": snapshot.quality,
            "summary": summary,
            "execution_effect": "NONE",
        }
    vals = [
        summary.get("imbalance_1pct"),
        summary.get("imbalance_2pct"),
        summary.get("imbalance_5pct"),
    ]
    known = [float(value) for value in vals if value is not None]
    if not known:
        return {
            "available": False,
            "reason": "MISSING_REQUIRED_BANDS",
            "summary": summary,
            "execution_effect": "NONE",
        }
    score = sum(known) / len(known)
    direction = "LONG" if score > 0.10 else ("SHORT" if score < -0.10 else "WAIT")
    return {
        "available": True,
        "direction": direction,
        "imbalance_score": score,
        "summary": summary,
        "execution_effect": "NONE",
    }



def _bounded_log_ratio(value: float | None, *, scale: float = 2.0) -> float | None:
    """Center a positive ratio at 1.0 and bound it to [-1, 1]."""
    if value is None:
        return None
    raw = float(value)
    if not math.isfinite(raw) or raw <= 0:
        return None
    return math.tanh(math.log(raw) * float(scale))


def shadow_microstructure_context(
    snapshot: BookDepthSnapshot | None,
    current_metrics: MetricsObservation | None,
    previous_metrics: MetricsObservation | None,
    *,
    decision_ts_ms: int,
    side: str,
    max_depth_age_ms: int = 15 * 60 * 1000,
    max_metrics_age_ms: int = 15 * 60 * 1000,
    oi_delta_override: float | None = None,
    agg_trade_pressure_override: float | None = None,
) -> dict:
    """Build pre-trade SHADOW microstructure/flow features.

    The result is observational only. It never enters the production NEXUS
    score, threshold, sizing or dispatch path.
    """
    direction = str(side or "").upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    decision_ts = int(decision_ts_ms)
    if decision_ts <= 0:
        raise ValueError("decision timestamp required")

    depth = shadow_microstructure_signal(snapshot)
    depth_age = None
    depth_ok = False
    depth_score = None
    if snapshot is not None:
        depth_age = decision_ts - int(snapshot.timestamp_ms)
        depth_ok = (
            depth.get("available") is True
            and 0 <= depth_age <= int(max_depth_age_ms)
        )
        if depth_ok:
            depth_score = float(depth["imbalance_score"])

    metrics_age = None
    oi_delta = None
    taker_ratio = None
    taker_pressure = None
    metrics_ok = False
    if current_metrics is not None:
        metrics_age = decision_ts - int(current_metrics.effective_ts_ms)
        metrics_ok = 0 <= metrics_age <= int(max_metrics_age_ms)
        if metrics_ok:
            taker_ratio = current_metrics.taker_ls_volume_ratio
            taker_pressure = _bounded_log_ratio(taker_ratio)
            if oi_delta_override is not None:
                candidate_delta = float(oi_delta_override)
                if math.isfinite(candidate_delta):
                    oi_delta = candidate_delta
            elif previous_metrics is not None and previous_metrics.sum_open_interest > 0:
                oi_delta = (
                    current_metrics.sum_open_interest
                    / previous_metrics.sum_open_interest
                    - 1.0
                )

    if agg_trade_pressure_override is not None:
        raw_pressure = float(agg_trade_pressure_override)
        if math.isfinite(raw_pressure):
            taker_pressure = max(-1.0, min(1.0, raw_pressure))

    components = []
    weights = []
    if depth_score is not None:
        components.append(max(-1.0, min(1.0, depth_score)))
        weights.append(0.55)
    if taker_pressure is not None:
        components.append(max(-1.0, min(1.0, taker_pressure)))
        weights.append(0.30)
    if oi_delta is not None and math.isfinite(float(oi_delta)):
        # 2% five-minute OI change already represents a large impulse.
        oi_pressure = math.tanh(float(oi_delta) / 0.02)
        components.append(max(-1.0, min(1.0, oi_pressure)))
        weights.append(0.15)
    else:
        oi_pressure = None

    composite = None
    if components:
        total_weight = sum(weights)
        composite = sum(
            value * weight for value, weight in zip(components, weights)
        ) / total_weight

    side_sign = 1.0 if direction == "LONG" else -1.0
    directional_alignment = (
        side_sign * composite if composite is not None else None
    )

    available = bool(depth_ok or metrics_ok)
    return {
        "available": available,
        "side": direction,
        "depth_available": depth_ok,
        "metrics_available": metrics_ok,
        "depth_age_ms": depth_age,
        "metrics_age_ms": metrics_age,
        "depth_imbalance": depth_score,
        "taker_long_short_ratio": taker_ratio,
        "taker_pressure": taker_pressure,
        "taker_pressure_source": (
            "AGG_TRADES"
            if agg_trade_pressure_override is not None
            else "METRICS_RATIO"
        ),
        "oi_delta": oi_delta,
        "oi_pressure": oi_pressure,
        "composite_pressure": composite,
        "directional_alignment": directional_alignment,
        "quality": (
            "OK"
            if available
            else "INSUFFICIENT_PRETRADE_MICROSTRUCTURE"
        ),
        "execution_effect": "NONE",
        "score_effect": "NONE",
        "promotion_authority": False,
    }
