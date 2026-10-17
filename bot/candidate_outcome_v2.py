"""Canonical analytics view over persisted NEXUS opportunity-audit candidates.

The source population is explicitly NEXUS-evaluated candidates, not every
symbol observed by the scanner. Approved and rejected decisions share the same
baseline population. Future outcomes are consumed only after the selected
horizon has been persisted by opportunity_audit.

Analytics only: no score, threshold, sizing, risk, order or exchange mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Iterable, Mapping

from bot.nexus_oos_edge_gate import CandidateOutcome
from bot.nexus_probability import heuristic_win_probability


_ALLOWED_HORIZONS = {
    "15m": "p15_net_pct",
    "30m": "p30_net_pct",
    "60m": "p60_net_pct",
    "120m": "p120_net_pct",
    "240m": "p240_net_pct",
}


@dataclass(frozen=True)
class CandidateEvidenceV2:
    candidate_id: str
    timestamp: float
    symbol: str
    side: str
    setup: str
    regime: str
    strategy_score: float
    nexus_score: float
    confidence_raw: float
    approved: bool
    decision_reason: str
    blocker_class: str
    entry: float
    stop_loss: float
    take_profit: float
    outcome_net_pct: float | None
    horizon: str

    def validate(self) -> "CandidateEvidenceV2":
        numeric = (
            self.timestamp,
            self.strategy_score,
            self.nexus_score,
            self.confidence_raw,
            self.entry,
            self.stop_loss,
            self.take_profit,
        )
        if not self.candidate_id or not self.symbol or not self.side:
            raise ValueError("candidate identity is required")
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("non-finite candidate evidence")
        if self.outcome_net_pct is not None and not math.isfinite(float(self.outcome_net_pct)):
            raise ValueError("non-finite outcome")
        if self.entry <= 0 or self.entry == self.stop_loss:
            raise ValueError("entry/stop must define positive risk")
        if not 0.0 <= self.confidence_raw <= 100.0:
            raise ValueError("confidence outside 0..100")
        if self.horizon not in _ALLOWED_HORIZONS:
            raise ValueError("unsupported horizon")
        return self

    @property
    def risk_pct(self) -> float:
        return abs(self.entry - self.stop_loss) / self.entry * 100.0

    @property
    def month(self) -> str:
        return datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).strftime("%Y-%m")

    def to_candidate_outcome(self) -> CandidateOutcome:
        self.validate()
        known = self.outcome_net_pct is not None
        r_multiple = (
            None
            if not known
            else float(self.outcome_net_pct) / self.risk_pct
        )
        return CandidateOutcome(
            timestamp=float(self.timestamp),
            approved=bool(self.approved),
            baseline_eligible=True,
            confidence=heuristic_win_probability(self.confidence_raw),
            outcome_known=known,
            r_multiple=r_multiple,
        )


def _metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_opportunity_row(
    row: Mapping[str, Any], *, horizon: str = "240m"
) -> CandidateEvidenceV2:
    column = _ALLOWED_HORIZONS.get(horizon)
    if column is None:
        raise ValueError("unsupported horizon")

    meta = _metadata(row.get("metadata"))
    approved = bool(int(row.get("approved") or 0))
    return CandidateEvidenceV2(
        candidate_id=str(row.get("signal_key") or ""),
        timestamp=float(row.get("created_epoch")),
        symbol=str(row.get("symbol") or "").upper(),
        side=str(row.get("direction") or "").upper(),
        setup=str(row.get("entry_type") or "UNKNOWN").upper(),
        regime=str(
            row.get("nexus_regime")
            or meta.get("signal_regime")
            or "UNKNOWN"
        ).upper(),
        strategy_score=float(row.get("strategy_score") or 0.0),
        nexus_score=float(row.get("nexus_score") or 0.0),
        confidence_raw=float(row.get("nexus_confidence") or 0.0),
        approved=approved,
        decision_reason=str(row.get("decision_reason") or ""),
        blocker_class=str(
            meta.get("blocker_class")
            or ("APPROVED" if approved else "UNKNOWN")
        ),
        entry=float(row.get("entry_price")),
        stop_loss=float(row.get("stop_loss")),
        take_profit=float(row.get("take_profit")),
        outcome_net_pct=(
            None if row.get(column) is None else float(row.get(column))
        ),
        horizon=horizon,
    ).validate()


def normalize_opportunity_rows(
    rows: Iterable[Mapping[str, Any]], *, horizon: str = "240m"
) -> tuple[CandidateEvidenceV2, ...]:
    out = sorted(
        (
            normalize_opportunity_row(row, horizon=horizon)
            for row in rows
        ),
        key=lambda item: (item.timestamp, item.candidate_id),
    )
    seen: set[str] = set()
    for item in out:
        if item.candidate_id in seen:
            raise ValueError("duplicate candidate_id")
        seen.add(item.candidate_id)
    return tuple(out)


async def load_opportunity_candidates(
    db,
    *,
    horizon: str = "240m",
    since_epoch: float | None = None,
    limit: int = 10000,
) -> tuple[CandidateEvidenceV2, ...]:
    column = _ALLOWED_HORIZONS.get(horizon)
    if column is None:
        raise ValueError("unsupported horizon")
    cap = max(1, min(int(limit), 100000))

    where = "WHERE created_epoch>=?" if since_epoch is not None else ""
    params = (float(since_epoch),) if since_epoch is not None else ()
    sql = f"""SELECT signal_key,created_epoch,symbol,direction,entry_type,
                     entry_price,stop_loss,take_profit,strategy_score,nexus_score,
                     nexus_confidence,nexus_regime,approved,decision_reason,
                     metadata,{column}
              FROM opportunity_audit {where}
              ORDER BY created_epoch ASC LIMIT {cap}"""
    raw = await db._fetchall(sql, params)
    keys = (
        "signal_key",
        "created_epoch",
        "symbol",
        "direction",
        "entry_type",
        "entry_price",
        "stop_loss",
        "take_profit",
        "strategy_score",
        "nexus_score",
        "nexus_confidence",
        "nexus_regime",
        "approved",
        "decision_reason",
        "metadata",
        column,
    )

    rows: list[dict[str, Any]] = []
    for item in raw or []:
        if hasattr(item, "keys"):
            rows.append({key: item[key] for key in item.keys()})
        else:
            rows.append(dict(zip(keys, item)))
    return normalize_opportunity_rows(rows, horizon=horizon)
