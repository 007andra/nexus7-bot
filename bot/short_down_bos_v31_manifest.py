"""Research-only, deterministic append-only V3.1 enrollment ledger (issue #658).

Pure Python, no DB writes, Binance orders, network, runtime hooks or scheduling.
Hash chaining detects accidental/casual rewrites but is NOT tamper-proof without
an externally authenticated digest and durable append-only storage.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

from bot.short_down_bos_v31_readonly_enrollment import (
    EnrollmentAuthority, PREREG_EPOCH, _epoch, _is_canonical,
    RosterEvidenceError, SYMBOL, TARGET, PER_SYMBOL_CAP,
)

PROTOCOL = "BINANCE_USDM_SHORT_DOWN_BOS_V31_20261012"
DOMAIN = "NEXUS7_V31_ROSTER_HASH_CHAIN_V1"
DISPOSITIONS = ("ENROLLED", "SKIPPED_SYMBOL_CAP", "SKIPPED_AFTER_40")


@dataclass(frozen=True)
class RosterEvent:
    sequence: int
    captured_epoch: float
    candidate_id: str
    symbol: str
    entry: str
    stop: str
    target: str
    disposition: str
    previous_digest: str
    digest: str


@dataclass(frozen=True)
class ProposedManifest:
    protocol: str
    effective_cutoff_epoch: float
    implementation_sha: str
    authorization_reference: str
    genesis_digest: str
    events: tuple[RosterEvent, ...]
    # Never promote this flag: no durable DB writer/anchor exists here.
    roster_durable: bool = False


def _json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _digest(obj) -> str:
    return hashlib.sha256(_json(obj)).hexdigest()


def _genesis(authority: EnrollmentAuthority) -> str:
    return _digest({
        "domain": DOMAIN, "protocol": PROTOCOL,
        "prereg_epoch": PREREG_EPOCH,
        "cutoff_epoch": authority.effective_cutoff(),
        "implementation_sha": authority.implementation_sha,
        "authorization_reference": authority.authorization_reference,
    })


def empty_manifest(authority: EnrollmentAuthority) -> ProposedManifest:
    return ProposedManifest(
        protocol=PROTOCOL,
        effective_cutoff_epoch=authority.effective_cutoff(),
        implementation_sha=authority.implementation_sha,
        authorization_reference=authority.authorization_reference,
        genesis_digest=_genesis(authority),
        events=(),
    )


def _price(raw, key: str) -> str:
    if key not in raw or isinstance(raw[key], bool):
        raise RosterEvidenceError("V31_MISSING_PRICE_GEOMETRY")
    try:
        value = Decimal(str(raw[key]))
    except (InvalidOperation, TypeError, ValueError):
        raise RosterEvidenceError("V31_MISSING_PRICE_GEOMETRY") from None
    if not value.is_finite() or value <= 0:
        raise RosterEvidenceError("V31_INVALID_SHORT_PRICE_GEOMETRY")
    return format(value.normalize(), "f")


def _validated(raw, cutoff: float):
    if not _is_canonical(raw):
        raise RosterEvidenceError("V31_NON_CANONICAL_CANDIDATE")
    t = _epoch(raw.get("captured_epoch"), historical_candidate=True)
    if not t > cutoff:
        raise RosterEvidenceError("V31_NOT_AFTER_EFFECTIVE_CUTOFF")
    cid = raw.get("candidate_id")
    symbol = raw.get("symbol")
    if not isinstance(cid, str) or not cid or len(cid) > 256:
        raise RosterEvidenceError("V31_INVALID_CANDIDATE_ID")
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise RosterEvidenceError("V31_INVALID_SYMBOL")
    entry, stop, target = (_price(raw, key) for key in ("entry", "stop", "target"))
    if not Decimal(stop) > Decimal(entry) > Decimal(target):
        raise RosterEvidenceError("V31_INVALID_SHORT_PRICE_GEOMETRY")
    return t, cid, symbol, entry, stop, target


def _event_content(sequence, t, cid, sym, entry, stop, target,
                   disposition, previous):
    return {
        "domain": DOMAIN, "protocol": PROTOCOL,
        "sequence": sequence, "captured_epoch": t,
        "candidate_id": cid, "symbol": sym, "entry": entry,
        "stop": stop, "target": target,
        "disposition": disposition, "previous_digest": previous,
    }


def verify_manifest(manifest: ProposedManifest, authority: EnrollmentAuthority,
                    *, trusted_head_digest: str | None = None) -> dict:
    """Recompute full chain and eligibility deterministically; no I/O.

    trusted_head_digest must come from an independently protected anchor.
    The value stored in this same mutable artifact is not independent.
    """
    if not isinstance(manifest, ProposedManifest) or manifest.roster_durable is not False:
        raise RosterEvidenceError("V31_MANIFEST_UNVERIFIED")
    if (manifest.protocol != PROTOCOL
            or manifest.implementation_sha != authority.implementation_sha
            or manifest.authorization_reference != authority.authorization_reference
            or manifest.effective_cutoff_epoch != authority.effective_cutoff()
            or manifest.genesis_digest != _genesis(authority)):
        raise RosterEvidenceError("V31_MANIFEST_AUTHORITY_CONFLICT")

    previous = manifest.genesis_digest
    prior_key = None
    seen = {}
    counts = Counter()
    enrolled = 0
    for seq, event in enumerate(manifest.events, 1):
        if not isinstance(event, RosterEvent) or event.sequence != seq:
            raise RosterEvidenceError("V31_MANIFEST_SEQUENCE_BROKEN")
        if not (math.isfinite(event.captured_epoch)
                and event.captured_epoch > manifest.effective_cutoff_epoch):
            raise RosterEvidenceError("V31_MANIFEST_INVALID_CUTOFF")
        key = (event.captured_epoch, event.candidate_id)
        if prior_key is not None and key <= prior_key:
            raise RosterEvidenceError("V31_MANIFEST_ORDER_BROKEN")
        prior_key = key
        if event.candidate_id in seen:
            raise RosterEvidenceError("V31_MANIFEST_DUPLICATE_ID")
        seen[event.candidate_id] = True
        if not isinstance(event.symbol, str) or not SYMBOL.fullmatch(event.symbol):
            raise RosterEvidenceError("V31_MANIFEST_INVALID_SYMBOL")
        if not Decimal(event.stop) > Decimal(event.entry) > Decimal(event.target):
            raise RosterEvidenceError("V31_MANIFEST_INVALID_GEOMETRY")
        expected_disposition = (
            "SKIPPED_AFTER_40" if enrolled >= TARGET
            else "SKIPPED_SYMBOL_CAP" if counts[event.symbol] >= PER_SYMBOL_CAP
            else "ENROLLED"
        )
        if event.disposition != expected_disposition:
            raise RosterEvidenceError("V31_MANIFEST_DISPOSITION_BROKEN")
        if expected_disposition == "ENROLLED":
            counts[event.symbol] += 1
            enrolled += 1
        if event.previous_digest != previous:
            raise RosterEvidenceError("V31_MANIFEST_PREVIOUS_HASH_BROKEN")
        content = _event_content(
            seq, event.captured_epoch, event.candidate_id, event.symbol,
            event.entry, event.stop, event.target,
            event.disposition, previous,
        )
        expected = _digest(content)
        if event.digest != expected:
            raise RosterEvidenceError("V31_MANIFEST_EVENT_HASH_BROKEN")
        previous = expected
    if trusted_head_digest is not None and previous != trusted_head_digest:
        raise RosterEvidenceError("V31_MANIFEST_EXTERNAL_ANCHOR_MISMATCH")

    return {
        "enrolled": enrolled,
        "target": TARGET,
        "distinct_symbols": len(counts),
        "largest_symbol_count": max(counts.values(), default=0),
        "observed_events": len(manifest.events),
        "head_digest": previous,
        "verified_against_external_anchor": trusted_head_digest is not None,
        "roster_durable": False,
        "net_evidence_ready": False,
        "promotion_allowed": False,
        "live_allowed": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


def append_candidate(manifest: ProposedManifest, raw: dict,
                     authority: EnrollmentAuthority) -> ProposedManifest:
    """Build a new append-only state; caller must persist/anchor externally.

    Idempotent replays are allowed only when the complete original candidate
    geometry is exactly identical; no outcomes ever affect disposition.
    """
    current = verify_manifest(manifest, authority)
    t, cid, sym, entry, stop, target = _validated(
        raw, manifest.effective_cutoff_epoch,
    )
    for event in manifest.events:
        if event.candidate_id == cid:
            if (event.captured_epoch, event.symbol, event.entry,
                    event.stop, event.target) != (t, sym, entry, stop, target):
                raise RosterEvidenceError("V31_CONFLICTING_CANDIDATE_ID")
            return manifest

    if manifest.events and (t, cid) <= (
            manifest.events[-1].captured_epoch, manifest.events[-1].candidate_id):
        raise RosterEvidenceError("V31_LATE_ARRIVAL_REQUIRES_AUDIT")

    cap = sum(e.disposition == "ENROLLED" and e.symbol == sym
              for e in manifest.events)
    disposition = ("SKIPPED_AFTER_40" if current["enrolled"] >= TARGET
                   else "SKIPPED_SYMBOL_CAP" if cap >= PER_SYMBOL_CAP
                   else "ENROLLED")
    prev = current["head_digest"]
    seq = len(manifest.events) + 1
    event = RosterEvent(seq, t, cid, sym, entry, stop, target,
                        disposition, prev,
                        _digest(_event_content(seq, t, cid, sym, entry,
                                               stop, target, disposition, prev)))
    new = ProposedManifest(
        protocol=manifest.protocol,
        effective_cutoff_epoch=manifest.effective_cutoff_epoch,
        implementation_sha=manifest.implementation_sha,
        authorization_reference=manifest.authorization_reference,
        genesis_digest=manifest.genesis_digest,
        events=manifest.events + (event,),
    )
    verify_manifest(new, authority)
    return new
