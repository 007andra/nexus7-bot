"""Prospective HWM append-only journal (ISOLATED CONTRACT; NOT WIRED TO LIVE).

A same-transaction primitive for FUTURE integrations only. Caller must hold
an asyncpg PostgreSQL transaction. The initial anchor attests only the HWM
currently present; it explicitly does NOT invent or certify prior history.

Nothing in this module runs at import or production startup. install_schema()
is an explicit DDL step for an isolated test database, NEVER called by bot.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from bot import hwm_provenance

TABLE = "hwm_transition_journal_v1"
ZERO_DIGEST = "0" * 64
REASONS = frozenset({
    "bootstrap", "new_equity_high", "incident_repair",
    "external_capital_flow_rebase", "external_position_performance_rebase",
})

# Explicitly opt-in DDL, NOT part of the boot path. Trigger guards UPDATE,
# DELETE and TRUNCATE. Privileged DB operators may still bypass it; without
# an independently anchored digest this is not WORM/cryptographic immutability.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hwm_transition_journal_v1 (
    scope_sha256 TEXT NOT NULL CHECK (length(scope_sha256) = 64),
    seq BIGINT NOT NULL CHECK (seq > 0),
    event_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ANCHOR','TRANSITION')),
    previous_digest TEXT NOT NULL CHECK (length(previous_digest) = 64),
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    payload_json TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope_sha256, seq),
    UNIQUE (scope_sha256, event_id)
);
CREATE OR REPLACE FUNCTION hwm_journal_block_mutation_v1()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'hwm transition journal append-only';
END;
$$;
DROP TRIGGER IF EXISTS hwm_journal_no_mutation_v1
    ON hwm_transition_journal_v1;
CREATE TRIGGER hwm_journal_no_mutation_v1
    BEFORE UPDATE OR DELETE ON hwm_transition_journal_v1
    FOR EACH ROW EXECUTE FUNCTION hwm_journal_block_mutation_v1();
DROP TRIGGER IF EXISTS hwm_journal_no_truncate_v1
    ON hwm_transition_journal_v1;
CREATE TRIGGER hwm_journal_no_truncate_v1
    BEFORE TRUNCATE ON hwm_transition_journal_v1
    FOR EACH STATEMENT EXECUTE FUNCTION hwm_journal_block_mutation_v1();
"""


class JournalIntegrityError(ValueError):
    """Any missing/malformed/inconsistent evidence fails closed."""


def _decimal(v):
    if isinstance(v, bool):
        raise JournalIntegrityError("invalid numeric evidence")
    try:
        d = Decimal(str(v))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise JournalIntegrityError("invalid numeric evidence") from exc
    if not d.is_finite() or d <= 0:
        raise JournalIntegrityError("invalid numeric evidence")
    return d


def _number(v):
    # The decimal serialization eliminates binary floating point ambiguity.
    d = _decimal(v)
    return format(d, "f")


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _event_id(value):
    try:
        u = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise JournalIntegrityError("invalid event identity") from exc
    if u.version != 4 or str(u) != str(value):
        raise JournalIntegrityError("event identity must be uuid v4")
    return str(u)


def _scope(peak_key):
    if not isinstance(peak_key, str) or not peak_key:
        raise JournalIntegrityError("missing HWM key")
    return _digest(peak_key)


def _chain_digest(previous, payload):
    if not isinstance(previous, str) or len(previous) != 64:
        raise JournalIntegrityError("invalid previous digest")
    return _digest(previous + "\n" + _canonical(payload))


def _payload(*, kind, scope, event_id, old_peak, new_peak, equity,
             reason, evidence_ref):
    if kind not in {"ANCHOR", "TRANSITION"}:
        raise JournalIntegrityError("unsupported journal event")
    if kind == "ANCHOR" and reason != "first_observed_peak":
        raise JournalIntegrityError("anchor reason invalid")
    if kind == "TRANSITION" and reason not in REASONS:
        raise JournalIntegrityError("transition reason invalid")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip() or len(evidence_ref) > 160:
        raise JournalIntegrityError("invalid evidence reference")
    old_value = None if old_peak is None else _number(old_peak)
    new_value = _number(new_peak)
    eq_value = None if kind == "ANCHOR" else _number(equity)
    if kind == "ANCHOR" and old_value != new_value:
        # An anchor observes only the persisted HWM, not equity or prior PnL.
        raise JournalIntegrityError("anchor cannot rebase historical peak")
    if kind == "TRANSITION" and old_value is None and reason != "bootstrap":
        raise JournalIntegrityError("old peak required")
    return {
        "v": 1,
        "scope_sha256": scope,
        "event_id": _event_id(event_id),
        "kind": kind,
        "old_peak": old_value,
        "new_peak": new_value,
        "account_equity": eq_value,
        "reason": reason,
        "evidence_sha256": _digest(evidence_ref),
        "history_before_anchor_verified": False,
    }


def verify_chain_rows(rows, *, peak_key):
    """Verify ordered journal rows; never upgrades pre-anchor history to PASS.

    This only detects corruption relative to the stored genesis chain. A DB
    owner who can replace the whole chain cannot be detected without an
    external timestamped digest (separately controlled).
    """
    scope = _scope(peak_key)
    previous_digest = ZERO_DIGEST
    previous_peak = None
    seen = set()
    next_seq = 1
    if not rows:
        raise JournalIntegrityError("journal missing")
    for row in rows:
        seq = int(row["seq"])
        if seq != next_seq or row["scope_sha256"] != scope:
            raise JournalIntegrityError("sequence or scope mismatch")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise JournalIntegrityError("invalid event payload") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise JournalIntegrityError("invalid event version")
        if payload.get("scope_sha256") != scope:
            raise JournalIntegrityError("payload scope mismatch")
        if payload.get("history_before_anchor_verified") is not False:
            raise JournalIntegrityError("unauthorized historical completeness")
        event = _event_id(payload.get("event_id"))
        if event in seen or str(row["event_id"]) != event:
            raise JournalIntegrityError("duplicate or inconsistent event identity")
        seen.add(event)
        if row["previous_digest"] != previous_digest:
            raise JournalIntegrityError("broken digest continuity")
        if row["digest"] != _chain_digest(previous_digest, payload):
            raise JournalIntegrityError("event hash mismatch")
        kind = payload.get("kind")
        if kind != row["kind"]:
            raise JournalIntegrityError("kind mismatch")
        if next_seq == 1:
            if (kind != "ANCHOR" or payload.get("reason") != "first_observed_peak"
                    or payload.get("old_peak") != payload.get("new_peak")):
                raise JournalIntegrityError("missing trusted first anchor")
        else:
            if kind != "TRANSITION" or payload.get("reason") not in REASONS:
                raise JournalIntegrityError("invalid transition")
            if payload.get("old_peak") != previous_peak:
                raise JournalIntegrityError("peak continuity mismatch")
        _decimal(payload.get("new_peak"))
        if kind == "ANCHOR":
            if payload.get("account_equity") is not None:
                raise JournalIntegrityError("anchor cannot attest unknown equity")
        else:
            _decimal(payload.get("account_equity"))
        if not isinstance(payload.get("evidence_sha256"), str) or len(payload["evidence_sha256"]) != 64:
            raise JournalIntegrityError("missing evidence commitment")
        previous_digest = str(row["digest"])
        previous_peak = payload["new_peak"]
        next_seq += 1
    return {
        "status": "PROSPECTIVE_CHAIN_VALID_PRIOR_HISTORY_UNVERIFIED",
        "count": next_seq - 1,
        "last_peak": previous_peak,
        "last_digest": previous_digest,
        "history_before_anchor_verified": False,
        "decision_effect": "NONE",
        "live_allowed": False,
    }


def _require_tx(conn):
    if not conn.is_in_transaction():
        raise JournalIntegrityError("journal operation requires outer transaction")


async def _lock_scope(conn, peak_key):
    # Coordinates with existing CAS advisory-lock scheme.
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", peak_key
    )


async def _last(conn, scope):
    return await conn.fetchrow(
        "SELECT scope_sha256,seq,event_id,kind,previous_digest,digest,payload_json "
        "FROM hwm_transition_journal_v1 WHERE scope_sha256=$1 ORDER BY seq DESC LIMIT 1 FOR UPDATE",
        scope,
    )


async def _insert(conn, scope, seq, payload, previous):
    digest = _chain_digest(previous, payload)
    await conn.execute(
        "INSERT INTO hwm_transition_journal_v1 "
        "(scope_sha256,seq,event_id,kind,previous_digest,digest,payload_json) "
        "VALUES ($1,$2,$3::uuid,$4,$5,$6,$7)",
        scope, seq, payload["event_id"], payload["kind"], previous,
        digest, _canonical(payload),
    )
    return digest


async def anchor_current_peak_in_tx(
    conn, *, peak_key, provenance_key, event_id, evidence_ref
):
    """Record today's observed state, NOT a historical origin or mutation.

    Needs preinstalled schema and an open caller transaction. Inserts only the
    journal row; never touches risk/key_value. A later transaction may commit
    transitions through commit_transition_in_tx.
    """
    _require_tx(conn)
    scope = _scope(peak_key)
    await _lock_scope(conn, peak_key)
    if await _last(conn, scope):
        raise JournalIntegrityError("journal already anchored")
    row = await conn.fetchrow(
        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", peak_key
    )
    prov_row = await conn.fetchrow(
        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", provenance_key
    )
    if not row or not prov_row:
        raise JournalIntegrityError("HWM or provenance missing")
    peak = _number(row["value"])
    try:
        provenance = json.loads(prov_row["value"])
        if (_number(provenance["new_peak"]) != peak or
                provenance.get("version") != 1):
            raise JournalIntegrityError("provenance does not match peak")
    except (ValueError, TypeError, KeyError) as exc:
        raise JournalIntegrityError("invalid current provenance") from exc
    payload = _payload(
        kind="ANCHOR", scope=scope, event_id=event_id, old_peak=peak,
        new_peak=peak, equity=None, reason="first_observed_peak",
        evidence_ref=evidence_ref,
    )
    await _insert(conn, scope, 1, payload, ZERO_DIGEST)
    return {"status": "ANCHOR_RECORDED_FROM_CURRENT_STATE",
            "history_before_anchor_verified": False, "live_allowed": False}


async def commit_transition_in_tx(
    conn, *, peak_key, provenance_key, expected_peak_raw,
    expected_provenance_raw, new_peak, account_equity,
    reason, evidence_ref, event_id
):
    """Atomic future HWM+provenance+journal write INSIDE caller transaction.

    Caller can also persist other guarded ledger keys in that same transaction.
    Before rollout, ALL HWM writers must be migrated/reviewed together; do not
    wire this into one path while other paths still bypass the journal.
    """
    _require_tx(conn)
    scope = _scope(peak_key)
    await _lock_scope(conn, peak_key)
    row = await conn.fetchrow(
        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", peak_key
    )
    provenance_row = await conn.fetchrow(
        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", provenance_key
    )
    current = row["value"] if row else None
    current_provenance = provenance_row["value"] if provenance_row else None
    if current != expected_peak_raw or current_provenance != expected_provenance_raw:
        raise JournalIntegrityError("stale HWM or provenance CAS")
    if current is None:
        raise JournalIntegrityError("existing anchor peak required")
    previous = await _last(conn, scope)
    if not previous:
        raise JournalIntegrityError("HWM journal anchor missing")
    try:
        previous_payload = json.loads(previous["payload_json"])
    except (TypeError, ValueError) as exc:
        raise JournalIntegrityError("corrupted journal head") from exc
    if previous["digest"] != _chain_digest(previous["previous_digest"], previous_payload):
        raise JournalIntegrityError("corrupted journal head digest")
    peak = _number(current)
    if _number(previous_payload.get("new_peak")) != peak:
        raise JournalIntegrityError("journal head and HWM diverged")
    if expected_provenance_raw is None:
        raise JournalIntegrityError("missing existing provenance")
    try:
        if _number(json.loads(expected_provenance_raw)["new_peak"]) != peak:
            raise JournalIntegrityError("existing provenance diverged")
    except (TypeError, ValueError, KeyError) as exc:
        raise JournalIntegrityError("invalid existing provenance") from exc
    payload = _payload(
        kind="TRANSITION", scope=scope, event_id=event_id,
        old_peak=peak, new_peak=new_peak, equity=account_equity,
        reason=reason, evidence_ref=evidence_ref,
    )
    # Authenticated account/evidence validity is STILL the responsibility of
    # existing caller risk controls; this helper creates no new authority.
    provenance = hwm_provenance.build_hwm_provenance(
        reason=reason, old_peak=float(peak), new_peak=float(_decimal(new_peak)),
        account_equity=float(_decimal(account_equity)), evidence_ref=evidence_ref,
    )
    await _insert(conn, scope, int(previous["seq"]) + 1, payload, previous["digest"])
    ts = await conn.fetchval("SELECT clock_timestamp()")
    for key, value in (
        (peak_key, _number(new_peak)),
        (provenance_key, provenance),
    ):
        affected = await conn.execute(
            "UPDATE key_value SET value=$2, updated_at=$3 WHERE key=$1",
            key, value, ts.isoformat(),
        )
        if affected != "UPDATE 1":
            raise JournalIntegrityError("HWM/provenance update not exactly one row")
    return {"status": "TRANSITION_COMMITTED_IN_CALLER_TX",
            "seq": int(previous["seq"]) + 1, "live_allowed": False}


async def verify_scope_from_pg(conn, *, peak_key):
    scope = _scope(peak_key)
    rows = await conn.fetch(
        "SELECT scope_sha256,seq,event_id,kind,previous_digest,digest,payload_json "
        "FROM hwm_transition_journal_v1 WHERE scope_sha256=$1 ORDER BY seq",
        scope,
    )
    return verify_chain_rows(rows, peak_key=peak_key)
