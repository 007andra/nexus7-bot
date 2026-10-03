"""Canonical decision evidence bundles for deterministic replay.

Bundles capture closed input candles, feature-schema identity and decision
artifacts. Persistence is optional and uses the existing NEXUS key-value store.
This module has no execution authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Mapping, Sequence

_MAX_CANDLES_PER_TIMEFRAME = 512
_MAX_BUNDLE_BYTES = 2_000_000
_KEY_PREFIX = "nexus:decision_evidence:v1:"


def _timestamp_ms(value: object) -> int:
    """Normalize epoch seconds/ms/us/ns to integer milliseconds."""
    try:
        ts = int(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid timestamp") from exc
    if ts <= 0:
        raise ValueError("timestamp must be positive")
    # Plausible modern epoch seconds.
    if ts < 100_000_000_000:
        ts *= 1000
    # Microseconds.
    elif ts >= 100_000_000_000_000 and ts < 100_000_000_000_000_000:
        ts //= 1000
    # Nanoseconds.
    elif ts >= 100_000_000_000_000_000:
        ts //= 1_000_000
    return ts


@dataclass(frozen=True)
class CandleEvidence:
    timeframe: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not self.timeframe:
            raise ValueError("invalid candle identity")
        object.__setattr__(self, "ts", _timestamp_ms(self.ts))
        vals = (self.open, self.high, self.low, self.close, self.volume)
        if not all(isfinite(float(v)) for v in vals):
            raise ValueError("non-finite candle")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("invalid candle values")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("candle high invariant violated")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("candle low invariant violated")


@dataclass(frozen=True)
class DecisionEvidenceBundle:
    candidate_id: str
    symbol: str
    side: str
    decision_ts: int
    code_sha: str
    feature_schema_version: str
    feature_fingerprint: str
    candles: tuple[CandleEvidence, ...]
    signal: Mapping[str, object]
    decision: Mapping[str, object]
    cost_snapshot: Mapping[str, object]

    def __post_init__(self) -> None:
        if not all((
            self.candidate_id, self.symbol, self.side, self.code_sha,
            self.feature_schema_version, self.feature_fingerprint,
        )):
            raise ValueError("missing evidence identity")
        if self.side.upper() not in {"LONG", "SHORT"}:
            raise ValueError("invalid evidence side")
        object.__setattr__(self, "decision_ts", _timestamp_ms(self.decision_ts))
        counts: dict[str, int] = {}
        previous: dict[str, int] = {}
        for candle in self.candles:
            counts[candle.timeframe] = counts.get(candle.timeframe, 0) + 1
            if counts[candle.timeframe] > _MAX_CANDLES_PER_TIMEFRAME:
                raise ValueError("too many evidence candles")
            last = previous.get(candle.timeframe, -1)
            if candle.ts <= last:
                raise ValueError("evidence candles not chronological")
            if candle.ts > self.decision_ts:
                raise ValueError("future candle in decision evidence")
            previous[candle.timeframe] = candle.ts

    def canonical_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol.upper(),
            "side": self.side.upper(),
            "decision_ts": int(self.decision_ts),
            "code_sha": self.code_sha,
            "feature_schema_version": self.feature_schema_version,
            "feature_fingerprint": self.feature_fingerprint,
            "candles": [asdict(c) for c in self.candles],
            "signal": dict(self.signal),
            "decision": dict(self.decision),
            "cost_snapshot": dict(self.cost_snapshot),
        }

    def canonical_json(self) -> str:
        raw = json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        if len(raw.encode("utf-8")) > _MAX_BUNDLE_BYTES:
            raise ValueError("decision evidence bundle too large")
        return raw

    @property
    def bundle_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def normalize_candles(
    timeframe: str,
    rows: Sequence[Mapping[str, object]],
) -> tuple[CandleEvidence, ...]:
    out = []
    for row in rows:
        out.append(CandleEvidence(
            timeframe=str(timeframe),
            ts=_timestamp_ms(row.get("ts", row.get("timestamp", 0))),
            open=float(row.get("o", row.get("open", 0)) or 0),
            high=float(row.get("h", row.get("high", 0)) or 0),
            low=float(row.get("l", row.get("low", 0)) or 0),
            close=float(row.get("c", row.get("close", 0)) or 0),
            volume=float(row.get("v", row.get("volume", 0)) or 0),
        ))
    return tuple(out)


async def persist_bundle(bundle: DecisionEvidenceBundle, *, strict: bool = False) -> bool:
    from bot import database

    payload = {
        "hash": bundle.bundle_hash,
        "bundle": bundle.canonical_dict(),
    }
    return bool(await database.save_key_value(
        _KEY_PREFIX + bundle.candidate_id,
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
        strict=strict,
    ))


async def load_bundle(candidate_id: str, *, strict: bool = False) -> dict | None:
    from bot import database

    raw = await database.load_key_value(_KEY_PREFIX + str(candidate_id), strict=strict)
    if not raw:
        return None
    parsed = json.loads(raw)
    bundle = parsed.get("bundle")
    expected = str(parsed.get("hash", ""))
    actual = hashlib.sha256(
        json.dumps(
            bundle, sort_keys=True, separators=(",", ":"), allow_nan=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if not expected or actual != expected:
        raise ValueError("decision evidence hash mismatch")
    return parsed
