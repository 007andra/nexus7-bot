"""Segmented edge diagnostics over canonical candidate outcomes."""
from __future__ import annotations

from statistics import mean
from typing import Callable, Iterable

from bot.candidate_outcome_v2 import CandidateEvidenceV2


def _stats(rows: list[CandidateEvidenceV2]) -> dict[str, object]:
    known = [row for row in rows if row.outcome_net_pct is not None]
    r_values = [
        float(outcome.r_multiple)
        for row in known
        if (outcome := row.to_candidate_outcome()).r_multiple is not None
    ]
    positive = [value for value in r_values if value > 0]
    negative = [value for value in r_values if value < 0]
    if negative:
        profit_factor = sum(positive) / abs(sum(negative))
    elif positive:
        profit_factor = float("inf")
    else:
        profit_factor = None
    return {
        "candidate_n": len(rows),
        "known_n": len(r_values),
        "expectancy_r": mean(r_values) if r_values else None,
        "hit_rate": (
            sum(value > 0 for value in r_values) / len(r_values)
            if r_values
            else None
        ),
        "profit_factor": profit_factor,
        "positive_r_sum": sum(positive),
        "negative_r_sum": sum(negative),
    }


def _group(
    rows: tuple[CandidateEvidenceV2, ...],
    key: Callable[[CandidateEvidenceV2], str],
    *,
    min_known_n: int,
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[CandidateEvidenceV2]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    output = {}
    for label, values in sorted(grouped.items()):
        stats = _stats(values)
        stats["evidence_sufficient"] = (
            int(stats["known_n"]) >= int(min_known_n)
        )
        output[label] = stats
    return output


def build_edge_diagnostics(
    rows: Iterable[CandidateEvidenceV2],
    *,
    min_known_n: int = 30,
) -> dict[str, object]:
    vals = tuple(
        sorted(
            (row.validate() for row in rows),
            key=lambda row: (row.timestamp, row.candidate_id),
        )
    )
    return {
        "overall": _stats(list(vals)),
        "setup": _group(vals, lambda row: row.setup, min_known_n=min_known_n),
        "regime": _group(vals, lambda row: row.regime, min_known_n=min_known_n),
        "symbol": _group(vals, lambda row: row.symbol, min_known_n=min_known_n),
        "side": _group(vals, lambda row: row.side, min_known_n=min_known_n),
        "setup_x_regime": _group(
            vals,
            lambda row: f"{row.setup}|{row.regime}",
            min_known_n=min_known_n,
        ),
        "symbol_x_side": _group(
            vals,
            lambda row: f"{row.symbol}|{row.side}",
            min_known_n=min_known_n,
        ),
        "promotion_authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }
