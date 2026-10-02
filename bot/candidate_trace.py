"""Deterministic candidate identity spanning signal -> NEXUS -> sizing -> order.

Observability/lineage only. Candidate ids never authorize execution.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Mapping


_PREFIX = "nx7-"
_VERSION = "candidate-v1"


def _finite(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("candidate trace contains non-numeric geometry") from exc
    if not math.isfinite(out):
        raise ValueError("candidate trace contains non-finite geometry")
    return out


def _number(value: object) -> str:
    return format(_finite(value), ".12g")


def candidate_payload(sig) -> dict:
    symbol = str(getattr(sig, "symbol", "") or "").upper()
    direction = str(getattr(sig, "direction", "") or "").upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        raise ValueError("candidate trace requires symbol and direction")

    formation_bucket = getattr(sig, "_bgx_formation_bucket", None)
    formation_ts = getattr(sig, "_bgx_formation_timestamp", None)
    if formation_bucket is None and formation_ts is not None:
        formation_bucket = int(_finite(formation_ts) // 900)

    payload = {
        "version": _VERSION,
        "symbol": symbol,
        "direction": direction,
        "entry": _number(getattr(sig, "entry", 0.0)),
        "sl": _number(getattr(sig, "sl", 0.0)),
        "tp": _number(getattr(sig, "tp", 0.0)),
        "formation_bucket": (
            int(formation_bucket) if formation_bucket is not None else None
        ),
        "entry_type": str(getattr(sig, "entry_type", "") or ""),
        "regime": str(getattr(sig, "regime", "") or ""),
        "score": int(getattr(sig, "score", 0) or 0),
    }
    if min(
        _finite(getattr(sig, "entry", 0.0)),
        _finite(getattr(sig, "sl", 0.0)),
        _finite(getattr(sig, "tp", 0.0)),
    ) <= 0:
        raise ValueError("candidate trace requires positive trade geometry")
    return payload


def build_candidate_id(sig) -> str:
    raw = json.dumps(
        candidate_payload(sig),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return _PREFIX + digest[:24]


def ensure_candidate_id(sig) -> str:
    existing = str(getattr(sig, "_bgx_setup_id", "") or "")
    if existing:
        return existing
    cid = build_candidate_id(sig)
    try:
        setattr(sig, "_bgx_setup_id", cid)
        setattr(sig, "_bgx_candidate_payload", candidate_payload(sig))
    except (AttributeError, TypeError):
        pass
    return cid


def attach_decision(decision, sig) -> str:
    cid = ensure_candidate_id(sig)
    try:
        setattr(decision, "_bgx_candidate_id", cid)
    except (AttributeError, TypeError):
        pass
    return cid


def bind_managed_order(order, sig) -> str:
    """Bind a candidate to durable order state and reject identity drift."""
    cid = ensure_candidate_id(sig)
    existing = str(getattr(order, "candidate_id", "") or "")
    if existing and existing != cid:
        raise ValueError(
            f"managed order candidate identity conflict: {existing} != {cid}"
        )
    try:
        setattr(order, "candidate_id", cid)
    except (AttributeError, TypeError) as exc:
        raise ValueError("managed order cannot retain candidate identity") from exc
    return cid


def candidate_id_from_decision(decision) -> str | None:
    cid = str(getattr(decision, "_bgx_candidate_id", "") or "")
    if cid:
        return cid
    ctx = getattr(decision, "_bgx_nexus_cost_context", None)
    snapshot = getattr(ctx, "snapshot", None)
    cid = str(getattr(snapshot, "candidate_id", "") or "")
    return cid or None
