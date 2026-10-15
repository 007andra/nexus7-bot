"""Strict post-decision 60/240m Binance USDM research candle windows.

No order placement. Never infer a fill in a candle that opened before the
decision timestamp; mark partial-minute evidence as missing instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bot.binance_usdm_oos_evidence_importer import fetch_klines, EvidenceError
from bot.binance_usdm_exec_net_oos_v1 import conservative_path

MINUTE_MS = 60_000


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def replay_horizon(*, symbol: str, side: str, decision_epoch_ms: int,
                   horizon_minutes: int, entry: str, stop: str, target: str,
                   transport) -> dict:
    """Strict exact-horizon clock, fail closed on partial first/last candle.

    A decision at HH:MM:SS cannot be faithfully modeled from HH:MM OHLC.
    This function refuses such windows rather than using pre-decision highs/lows.
    """
    common = {"symbol": symbol, "horizon_minutes": horizon_minutes,
              "execution_effect": "NONE", "live_allowed": False}
    if horizon_minutes not in (60, 240):
        return {**common, "status": "NET_PROOF_MISSING", "reason": "UNSUPPORTED_HORIZON"}
    if not isinstance(decision_epoch_ms, int) or decision_epoch_ms % MINUTE_MS:
        return {**common, "status": "NET_PROOF_MISSING",
                "reason": "PARTIAL_MINUTE_DECISION_REQUIRES_FINER_DATA"}
    end = decision_epoch_ms + horizon_minutes * MINUTE_MS
    try:
        evidence = fetch_klines(symbol=symbol, start_utc=_iso(decision_epoch_ms),
                                end_utc=_iso(end), interval="1m", transport=transport)
    except (EvidenceError, ValueError) as exc:
        return {**common, "status": "NET_PROOF_MISSING",
                "reason": "BINANCE_CANDLE_EVIDENCE_ERROR", "detail": str(exc)}
    candles = evidence["candles"]
    if len(candles) != horizon_minutes or not evidence["complete"]:
        return {**common, "status": "NET_PROOF_MISSING", "reason": "INCOMPLETE_HORIZON"}
    path = conservative_path(side=side, entry=Decimal(entry), stop=Decimal(stop),
                             target=Decimal(target), candles=candles)
    # Even an OHLC-observed barrier is not proof of a fill or of executable net.
    return {**common, "status": "PATH_OBSERVED_NET_UNPROVEN"
            if path["status"] == "PATH_OBSERVED" else "NET_PROOF_MISSING",
            "path": path, "candle_source": evidence["source"],
            "request_provenance": evidence["request_provenance"],
            "funding_verified": False, "fees_verified": False,
            "spread_verified": False, "slippage_verified": False,
            "symbol_filters_verified": False}
