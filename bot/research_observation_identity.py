"""Research observation identity (SHADOW / OOS only).

Three identities exist and must never be conflated:

* ``setup_id`` (``candidate_trace.build_candidate_id``) — deterministic hash of
  the setup payload. INV-SETUP-ID-001: equivalent setups MAY share it, also at
  different decision timestamps.
* ``observation_id`` (this module) — one research observation = one setup seen
  at one decision timestamp on one venue. INV-RESEARCH-OBS-ID-001: two distinct
  OOS observations never share it; replaying the same event yields the same id.
  It is causal: built only from facts known at decision time (setup id,
  decision timestamp, symbol, side, venue) and never from outcomes (realized R,
  exit, MFE/MAE, labels).
* financial intent / clientOid (engine idempotency key + generation) —
  INV-FINANCIAL-INTENT-ID-001: independent of both; this module is not imported
  by any LIVE execution path.
"""
from __future__ import annotations

import hashlib
import json

PREFIX = "obs-"
VERSION = "research-observation-v1"


def build_observation_id(
    setup_id: str,
    decision_ts_ms: int,
    symbol: str,
    side: str,
    *,
    venue: str = "BINANCE_USDM",
) -> str:
    setup = str(setup_id or "")
    sym = str(symbol or "").upper()
    direction = str(side or "UNKNOWN").upper()
    if not setup or not sym:
        raise ValueError("observation identity requires setup_id and symbol")
    if isinstance(decision_ts_ms, bool):
        raise ValueError("decision_ts_ms must be an integer timestamp")
    ts = int(decision_ts_ms)
    if ts < 0:
        raise ValueError("decision_ts_ms cannot be negative")
    raw = json.dumps(
        {"v": VERSION, "setup_id": setup, "decision_ts_ms": ts,
         "symbol": sym, "side": direction, "venue": str(venue or "").upper()},
        sort_keys=True, separators=(",", ":"),
    )
    return PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
