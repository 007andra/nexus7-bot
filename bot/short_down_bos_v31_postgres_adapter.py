"""Opt-in-only V3.1 *research database* transactional adapter. NOT runtime wired.

No automatic migration, connection creation, credentials, Binance calls, bot
engine access or LIVE authority. The caller must provide a dedicated isolated
PostgreSQL connection and an independently supplied prior-head witness.

SQL migration: docs/research/sql/v31_isolated_manifest.sql (manual review only).
This cannot establish external tamper-proof anchoring; see release notes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from bot.short_down_bos_v31_readonly_enrollment import EnrollmentAuthority, RosterEvidenceError
from bot.short_down_bos_v31_manifest import (
    PROTOCOL, ProposedManifest, RosterEvent, append_candidate,
    empty_manifest, verify_manifest,
)

# Dedicated research DATABASE + role, never the trading DB or its write role.
RESEARCH_DB_PREFIX = "nexus7_research_"
RESEARCH_WRITER = "nexus_v31_writer"
DIGEST64 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class PriorHeadWitness:
    """Value furnished independently outside the database being modified.

    Caller must independently authenticate this witness before invoking the
    adapter. This class does not verify a signature or make a durable claim.
    """
    head_digest: str
    reference: str

    def validate(self):
        if not isinstance(self.head_digest, str) or not DIGEST64.fullmatch(self.head_digest):
            raise RosterEvidenceError("V31_WITNESS_DIGEST_INVALID")
        if not isinstance(self.reference, str) or not (
                4 <= len(self.reference) <= 120) or not self.reference.isascii():
            raise RosterEvidenceError("V31_WITNESS_REFERENCE_INVALID")


def _safe_events(rows):
    events = []
    for row in rows:
        try:
            data = row["event_data"]
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict):
                raise ValueError()
            event = RosterEvent(**data)
            if (int(row["sequence"]) != event.sequence
                    or row["candidate_id"] != event.candidate_id
                    or row["digest"] != event.digest):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RosterEvidenceError("V31_STORED_EVENT_INVALID") from None
        events.append(event)
    return tuple(events)


def _stored_manifest(row, events, authority):
    try:
        manifest = ProposedManifest(
            protocol=row["protocol"],
            effective_cutoff_epoch=float(row["effective_cutoff_epoch"]),
            implementation_sha=row["implementation_sha"],
            authorization_reference=row["authorization_reference"],
            genesis_digest=row["genesis_digest"],
            events=events,
        )
    except (KeyError, TypeError, ValueError):
        raise RosterEvidenceError("V31_STORED_HEAD_INVALID") from None
    verify_manifest(manifest, authority)
    return manifest


async def _assert_dedicated_session(conn):
    identity = await conn.fetchrow(
        "SELECT current_database() AS dbname, current_user AS username, "
        "current_setting('transaction_read_only') AS readonly"
    )
    if (not identity
            or identity["username"] != RESEARCH_WRITER
            or not str(identity["dbname"]).startswith(RESEARCH_DB_PREFIX)
            or identity["readonly"] not in ("off", False)):
        raise RosterEvidenceError("V31_ISOLATED_RESEARCH_DB_REQUIRED")


async def append_isolated_transaction(conn, *, authority: EnrollmentAuthority,
                                     raw_candidate: dict, witness: PriorHeadWitness):
    """Compare/witness + append event and CAS head in one SERIALIZABLE transaction.

    The DB and signing/anchoring service are NOT created or controlled here.
    If caller doesn't have a genuine independently authenticated witness,
    do not call. The new head must be anchored *outside* this DB before any
    subsequent append. No runtime integration or research-gate promotion.
    """
    witness.validate()
    authority.effective_cutoff()
    if not hasattr(conn, "transaction") or not hasattr(conn, "fetchrow"):
        raise RosterEvidenceError("V31_TRANSACTIONAL_CONNECTION_REQUIRED")

    async with conn.transaction(isolation="serializable"):
        await _assert_dedicated_session(conn)
        head = await conn.fetchrow(
            "SELECT protocol,effective_cutoff_epoch,implementation_sha,"
            "authorization_reference,genesis_digest,sequence,head_digest "
            "FROM v31_research.manifest_heads WHERE protocol=$1 FOR UPDATE",
            PROTOCOL,
        )
        if not head:
            # Administrator must initialize an authorized genesis separately.
            raise RosterEvidenceError("V31_GENESIS_NOT_PROVISIONED")
        rows = await conn.fetch(
            "SELECT sequence,candidate_id,event_data,digest "
            "FROM v31_research.manifest_events WHERE protocol=$1 "
            "ORDER BY sequence",
            PROTOCOL,
        )
        stored = _stored_manifest(head, _safe_events(rows), authority)
        proof = verify_manifest(stored, authority,
                                trusted_head_digest=witness.head_digest)
        if (head["head_digest"] != proof["head_digest"]
                or head["sequence"] != proof["observed_events"]):
            raise RosterEvidenceError("V31_DB_HEAD_CAS_MISMATCH")

        updated = append_candidate(stored, raw_candidate, authority)
        if updated is stored:
            # Idempotent replay, no SQL mutations.
            return {
                "status": "IDEMPOTENT_READ_ONLY",
                "head_digest": proof["head_digest"],
                "sequence": proof["observed_events"],
                "anchoring_required": True,
                "roster_durable": False, "net_evidence_ready": False,
                "promotion_allowed": False, "live_allowed": False,
                "decision_effect": "NONE", "execution_effect": "NONE",
            }

        event = updated.events[-1]
        content = json.dumps(event.__dict__, sort_keys=True,
                             ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        # This SQL is intentionally hard-coded and uses only parameter bindings.
        await conn.execute(
            "INSERT INTO v31_research.manifest_events "
            "(protocol,sequence,candidate_id,event_data,digest) "
            "VALUES ($1,$2,$3,$4::jsonb,$5)",
            PROTOCOL, event.sequence, event.candidate_id, content, event.digest,
        )
        changed = await conn.execute(
            "UPDATE v31_research.manifest_heads "
            "SET sequence=$2,head_digest=$3 "
            "WHERE protocol=$1 AND sequence=$4 AND head_digest=$5",
            PROTOCOL, event.sequence, event.digest,
            proof["observed_events"], proof["head_digest"],
        )
        if changed != "UPDATE 1":
            raise RosterEvidenceError("V31_DB_HEAD_CAS_MISMATCH")
        # Commit has not happened yet; the caller must treat returned data as
        # pending until context manager exits successfully.
    return {
        "status": "TRANSACTION_COMMITTED_ANCHOR_PENDING",
        "head_digest": event.digest,
        "sequence": event.sequence,
        "anchoring_required": True,
        "roster_durable": False,
        "net_evidence_ready": False,
        "promotion_allowed": False,
        "live_allowed": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


async def verify_isolated_transaction(conn, *, authority: EnrollmentAuthority,
                                      witness: PriorHeadWitness):
    """Audit-only bounded consistent scan; requires role+independent witness."""
    witness.validate()
    authority.effective_cutoff()
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        # Cannot ask readonly off here: authentication still proves role and DB.
        identity = await conn.fetchrow(
            "SELECT current_database() AS dbname, current_user AS username"
        )
        if (not identity or identity["username"] != RESEARCH_WRITER
                or not str(identity["dbname"]).startswith(RESEARCH_DB_PREFIX)):
            raise RosterEvidenceError("V31_ISOLATED_RESEARCH_DB_REQUIRED")
        head = await conn.fetchrow(
            "SELECT protocol,effective_cutoff_epoch,implementation_sha,"
            "authorization_reference,genesis_digest,sequence,head_digest "
            "FROM v31_research.manifest_heads WHERE protocol=$1", PROTOCOL,
        )
        if head is None:
            raise RosterEvidenceError("V31_GENESIS_NOT_PROVISIONED")
        events = await conn.fetch(
            "SELECT sequence,candidate_id,event_data,digest "
            "FROM v31_research.manifest_events WHERE protocol=$1 ORDER BY sequence",
            PROTOCOL,
        )
        manifest = _stored_manifest(head, _safe_events(events), authority)
        result = verify_manifest(
            manifest, authority, trusted_head_digest=witness.head_digest,
        )
        if (head["sequence"] != result["observed_events"]
                or head["head_digest"] != result["head_digest"]):
            raise RosterEvidenceError("V31_DB_HEAD_CAS_MISMATCH")
        return result
