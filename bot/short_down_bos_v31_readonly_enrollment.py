"""V3.1 Binance USD-M future-only roster proposal (issue #658).

Research ONLY; never opens a Binance session, starts background tasks,
writes a database, authorizes orders, or changes the production risk path.

IMPORTANT: a snapshot is a PROVISIONAL roster. Only a separately approved
append-only durable manifest with a pinned implementation/capture cutoff
could establish final prospective enrollment.
"""
from __future__ import annotations

from collections import Counter
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

PREREG_UTC = "2026-10-12T19:39:52Z"
PREREG_EPOCH = 1791833992.0
TARGET = 40
PER_SYMBOL_CAP = 10
MIN_SYMBOLS = 6
MAX_SCAN = 1000
SYMBOL = re.compile(r"^[A-Z0-9]{2,18}USDT$")
SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
POPULATION = "HARD_GATE_SHADOW"
AUTHORITIES = {
    "research_only": True,
    "live_allowed": False,
    "promotion_allowed": False,
    "decision_effect": "NONE",
    "execution_effect": "NONE",
    "sample_replacement_allowed": False,
    "roster_durable": False,
}


class RosterEvidenceError(ValueError):
    """Only constant, non-sensitive errors; no candidate IDs in messages."""


@dataclass(frozen=True)
class EnrollmentAuthority:
    implementation_sha: str
    implementation_commit_epoch: float
    authorized_capture_epoch: float
    authorization_reference: str

    def effective_cutoff(self) -> float:
        if not SHA40.fullmatch(str(self.implementation_sha)):
            raise RosterEvidenceError("V31_IMPLEMENTATION_SHA_NOT_VERIFIED")
        if not isinstance(self.authorization_reference, str) or not (
            4 <= len(self.authorization_reference.strip()) <= 120
        ):
            raise RosterEvidenceError("V31_CAPTURE_AUTHORIZATION_MISSING")
        if not self.authorization_reference.isascii():
            raise RosterEvidenceError("V31_CAPTURE_AUTHORIZATION_INVALID")
        commit = _epoch(self.implementation_commit_epoch)
        authorized = _epoch(self.authorized_capture_epoch)
        if commit < PREREG_EPOCH or authorized < commit:
            raise RosterEvidenceError("V31_INVALID_CHRONOLOGICAL_AUTHORITY")
        return max(PREREG_EPOCH, commit, authorized)


def _epoch(value, *, historical_candidate=False) -> float:
    if isinstance(value, bool):
        raise RosterEvidenceError("V31_INVALID_TIMESTAMP")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RosterEvidenceError("V31_INVALID_TIMESTAMP") from None
    # The chronological admission cutoff applies to enrollment, not to
    # parsing historical candidates that must be safely excluded.
    min_epoch = 0.0 if historical_candidate else PREREG_EPOCH
    if not math.isfinite(result) or not (min_epoch < result <= 4_102_444_800):
        raise RosterEvidenceError("V31_INVALID_TIMESTAMP")
    return result


def _is_canonical(raw) -> bool:
    return (
        isinstance(raw, dict)
        and raw.get("population") == POPULATION
        and raw.get("side") == "SHORT"
        and raw.get("regime") == "TRENDING_DOWN"
        and raw.get("setup") == "BOS_BREAK"
        and raw.get("nexus_called") is True
        and raw.get("nexus_allowed") is True
        and raw.get("shadow_only") is True
        and raw.get("live_eligible") is False
        and raw.get("decision_effect") == "NONE"
        and raw.get("execution_effect") == "NONE"
    )


def propose_roster(candidates, *, authority: EnrollmentAuthority,
                   scan_truncated: bool = False, private: bool = False):
    """Select natural, chronology-fixed first-40 proposal; no outcome input.

    Raw inputs are trusted only as *unverified research evidence*; approval
    status cannot establish that an exchange-observable execution was feasible.
    The selector NEVER considers whether an outcome was good or available.
    """
    cutoff = authority.effective_cutoff()
    valid = []
    for raw in candidates:
        if not isinstance(raw, dict):
            raise RosterEvidenceError("V31_INVALID_ROW_SHAPE")
        if not _is_canonical(raw):
            continue
        t = _epoch(raw.get("captured_epoch"), historical_candidate=True)
        if t <= cutoff:
            continue
        cid = raw.get("candidate_id")
        symbol = raw.get("symbol")
        if not isinstance(cid, str) or not cid or len(cid) > 256:
            raise RosterEvidenceError("V31_INVALID_CANDIDATE_ID")
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise RosterEvidenceError("V31_INVALID_SYMBOL")
        # The original protected geometry must be available, even if its
        # executability will only be adjudicated by the separate NET engine.
        try:
            entry, stop, target = (float(raw[k]) for k in ("entry", "stop", "target"))
        except (KeyError, ValueError, TypeError, OverflowError):
            raise RosterEvidenceError("V31_MISSING_PRICE_GEOMETRY") from None
        if not (all(math.isfinite(x) and x > 0 for x in (entry, stop, target))
                and stop > entry > target):
            raise RosterEvidenceError("V31_INVALID_SHORT_PRICE_GEOMETRY")
        # Store immutable decision geometry for duplicate fingerprint checks.
        valid.append((t, cid, symbol, entry, stop, target))

    valid.sort(key=lambda row: (row[0], row[1]))
    # An inconsistent duplicate must stop collection; never silently choose
    # whichever row makes the cohort more favorable.
    seen = {}
    chosen = []
    counts = Counter()
    cap_excluded = 0
    for t, cid, symbol, entry, stop, target in valid:
        key = (t, symbol, entry, stop, target)
        if cid in seen:
            if seen[cid] != key:
                raise RosterEvidenceError("V31_CONFLICTING_CANDIDATE_ID")
            continue
        seen[cid] = key
        if counts[symbol] >= PER_SYMBOL_CAP:
            cap_excluded += 1
            continue
        if len(chosen) >= TARGET:
            continue
        counts[symbol] += 1
        chosen.append((t, cid, symbol))

    n = len(chosen)
    distinct = len(counts)
    status = (
        "SCAN_TRUNCATED_NOT_PROVEN" if scan_truncated else
        "AWAITING_FUTURE_CANDIDATES" if n < TARGET else
        "ROSTER_PROVISIONAL_NOT_PROVEN"
    )
    reasons = []
    if scan_truncated:
        reasons.append("INCOMPLETE_SOURCE_WINDOW")
    if n < TARGET:
        reasons.append("TARGET_40_NOT_REACHED")
    if n >= TARGET and distinct < MIN_SYMBOLS:
        reasons.append("SIX_SYMBOLS_NOT_REACHED")
    # Future, independently complete NET evidence and durable enrollment are
    # mandatory irrespective of numeric sample completeness.
    reasons.extend(("IMMUTABLE_MANIFEST_NOT_ATTESTED",
                    "BINANCE_EXECUTION_NET_EVIDENCE_NOT_PROVEN"))
    report = {
        **AUTHORITIES,
        "status": status,
        "blockers": tuple(reasons),
        "prereg_cutoff_epoch": PREREG_EPOCH,
        "effective_cutoff_epoch": cutoff,
        "implementation_sha": authority.implementation_sha,
        "enrolled": n,
        "target": TARGET,
        "distinct_symbols": distinct,
        "six_symbol_requirement_met": distinct >= MIN_SYMBOLS,
        "largest_symbol_count": max(counts.values(), default=0),
        "symbol_cap_rejections": cap_excluded,
        "scan_truncated": bool(scan_truncated),
        "net_evidence_ready": False,
    }
    if private:
        # Private operator/session only. Never send this object to logging
        # or GitHub issue comments: it contains sensitive candidate IDs.
        report["private_candidate_ids"] = tuple(cid for _, cid, _ in chosen)
    return report


def public_summary(report) -> dict:
    """Explicit allowlist for logs; never emit IDs or raw candidate fields."""
    fields = (
        "status", "blockers", "enrolled", "target", "distinct_symbols",
        "six_symbol_requirement_met", "largest_symbol_count",
        "symbol_cap_rejections", "scan_truncated", "roster_durable",
        "net_evidence_ready", "live_allowed", "promotion_allowed",
        "decision_effect", "execution_effect",
    )
    return {key: report[key] for key in fields}


async def snapshot_readonly(reader, *, authority: EnrollmentAuthority):
    """Operator-invoked single bounded SELECT, not scheduled or wired to LIVE.

    Only the two approved read-only research reader classes are accepted.
    No fallback to the shared trading database on failure.
    """
    from bot.oos_readonly_connection_v1 import OOSReadOnlyConnection
    from bot.oos_research_reader_v2 import ResearchReader

    if not isinstance(reader, (OOSReadOnlyConnection, ResearchReader)):
        raise RosterEvidenceError("V31_DEDICATED_READONLY_READER_REQUIRED")
    cutoff = authority.effective_cutoff()
    sql = (
        "SELECT candidate_id,captured_epoch,payload "
        "FROM hard_gate_shadow_candidates_v1 "
        "WHERE population=? AND captured_epoch>? "
        "ORDER BY captured_epoch,candidate_id LIMIT ?"
    )
    rows = await reader._fetchall(sql, (POPULATION, cutoff, MAX_SCAN))
    records = []
    for item in rows:
        table_id = item["candidate_id"]
        table_epoch = float(item["captured_epoch"])
        try:
            payload = json.loads(item["payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise RosterEvidenceError("V31_INVALID_PERSISTED_JSON") from None
        if (not isinstance(payload, dict)
                or payload.get("candidate_id") != table_id
                or not isinstance(payload.get("captured_epoch"), (int, float))
                or not math.isclose(float(payload["captured_epoch"]),
                                    table_epoch, abs_tol=1e-6, rel_tol=0)):
            raise RosterEvidenceError("V31_TABLE_PAYLOAD_MISMATCH")
        records.append(payload)
    return propose_roster(records, authority=authority,
                          scan_truncated=len(rows) >= MAX_SCAN)
