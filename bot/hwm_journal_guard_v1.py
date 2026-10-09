"""Fail-closed PostgreSQL HWM journal integration coordinator (OPT-IN, DRAFT).

This module is inert unless HWM_JOURNAL_INTEGRATION_V1=true; enabling it before
reviewed schema installation, anchor and legacy migration intentionally BLOCKS
HWM writes. It does not authorize trading, alter drawdown or migrate history.
"""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal

from bot import database as db
from bot import hwm_namespace, financial_namespace
from bot import hwm_transition_journal_v1 as journal
from bot.atomic_key_value import CompareAndSwapConflict


def enabled():
    return os.environ.get("HWM_JOURNAL_INTEGRATION_V1", "").lower() == "true"


def has_peak_change(pairs):
    names = {key for key, _ in pairs}
    return bool(names & {hwm_namespace.equity_peak_key(), hwm_namespace.provenance_key()})


def _required_metadata(meta, *, new_value, provenance_raw, current_peak):
    if not isinstance(meta, dict):
        raise journal.JournalIntegrityError("HWM journal event metadata required")
    try:
        reason = meta["reason"]
        equity = meta["account_equity"]
        evidence_ref = meta["evidence_ref"]
        former = meta["old_peak"]
        prov = json.loads(provenance_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise journal.JournalIntegrityError("invalid HWM journal evidence") from exc
    if former is None or journal._number(former) != journal._number(current_peak):
        raise journal.JournalIntegrityError("prior HWM mismatch")
    if not isinstance(prov, dict) or prov.get("version") != 1:
        raise journal.JournalIntegrityError("invalid new provenance")
    if (prov.get("reason") != reason
            or prov.get("evidence_ref") != evidence_ref
            or prov.get("execution_effect") != "NONE"):
        raise journal.JournalIntegrityError("new provenance metadata mismatch")
    # Existing provenance stores IEEE floats; normalize via repr of a float.
    for field, want in (
        ("old_peak", former), ("new_peak", new_value),
        ("account_equity", equity),
    ):
        try:
            numeric = Decimal(str(float(want)))
            actual = Decimal(str(prov[field]))
            if not numeric.is_finite() or not actual.is_finite():
                raise ValueError("nonfinite")
            if abs(actual - numeric) > Decimal("1e-10"):
                raise ValueError("numeric mismatch")
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise journal.JournalIntegrityError("new provenance value mismatch") from exc
    return reason, equity, evidence_ref


async def write_with_journal(pairs, *, expected=None, event=None):
    """Atomically persist whole writer batch; no partial non-HWM ledger writes.

    Called by both existing atomic writers ONLY when journal is explicitly
    enabled and the batch changes HWM/provenance. Uses same CAS advisory lock
    ordering, a fresh isolated PostgreSQL connection and a SINGLE transaction.
    """
    if not enabled():
        raise journal.JournalIntegrityError("journal integration disabled")
    if not db.DATABASE_URL.startswith("postgresql") or not db._is_pg:
        raise db.PersistenceError("journal requires PostgreSQL")
    if not has_peak_change(pairs):
        raise journal.JournalIntegrityError("HWM transition keys required")
    if not isinstance(event, dict):
        raise journal.JournalIntegrityError("missing transition evidence")
    if len({k for k, _ in pairs}) != len(pairs):
        raise journal.JournalIntegrityError("duplicate journal keys")

    peak_key = hwm_namespace.equity_peak_key()
    prov_key = hwm_namespace.provenance_key()
    mapping = dict(pairs)
    if peak_key not in mapping or prov_key not in mapping:
        raise journal.JournalIntegrityError("unpaired HWM and provenance")
    other = [(k, v) for k, v in pairs if k not in (peak_key, prov_key)]
    old_peak_key = financial_namespace.legacy_key_for(peak_key)
    old_prov_key = financial_namespace.legacy_key_for(prov_key)
    guard = dict(expected or {})
    legacy_keys = sorted({
        key for key in (old_peak_key, old_prov_key)
        if isinstance(key, str) and key and key not in (peak_key, prov_key)
    })
    guard_keys = sorted(set(guard) | set(mapping) | set(legacy_keys))
    import asyncpg
    conn = await asyncpg.connect(db.DATABASE_URL, timeout=10)
    try:
        async with conn.transaction():
            for key in guard_keys:
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", key
                )
            for key, expected_raw in guard.items():
                row = await conn.fetchrow(
                    "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", key,
                )
                if (row["value"] if row else None) != expected_raw:
                    raise CompareAndSwapConflict("journal CAS conflict")
            # A future release needs an explicitly reviewed physical namespace
            # migration first. Never anchor the current key from legacy in code.
            legacy_values = (
                await conn.fetch(
                    "SELECT key FROM key_value WHERE key=ANY($1::text[])",
                    legacy_keys,
                ) if legacy_keys else []
            )
            if legacy_values:
                raise journal.JournalIntegrityError(
                    "legacy HWM scope migration required before journal activation"
                )
            row = await conn.fetchrow(
                "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", peak_key
            )
            prov_row = await conn.fetchrow(
                "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", prov_key
            )
            if not row or not prov_row:
                raise journal.JournalIntegrityError("current HWM/provenance missing")
            reason, equity, evidence_ref = _required_metadata(
                event, new_value=mapping[peak_key],
                provenance_raw=mapping[prov_key], current_peak=row["value"],
            )
            # commit_transition revalidates the values and anchored chain.
            await journal.commit_transition_in_tx(
                conn,
                peak_key=peak_key, provenance_key=prov_key,
                expected_peak_raw=row["value"],
                expected_provenance_raw=prov_row["value"],
                new_peak=mapping[peak_key], account_equity=equity,
                reason=reason, evidence_ref=evidence_ref,
                event_id=str(uuid.uuid4()),
                new_provenance_raw=mapping[prov_key],
            )
            stamp = await conn.fetchval("SELECT clock_timestamp()")
            for key, value in other:
                await conn.execute(
                    "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                    "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                    key, value, stamp.isoformat(),
                )
        return True
    finally:
        await conn.close(timeout=3)
