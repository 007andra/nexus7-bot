"""PostgreSQL-backed LIVE execution ownership with monotonic fencing.

This authority gates only mutations that can OPEN_NEW_RISK. Risk-reducing
reduceOnly/protection/reconciliation paths deliberately remain available.
"""
from __future__ import annotations
import asyncio, os, uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from bot import database as db
from bot.critical_state import CriticalStateUnavailable
from bot.db_cancel_safety import is_spurious_cancel
from bot.execution_capability import ExecutionCapability, current_execution_capability

_KEY = "live_execution_ownership_v1"
_OWNER_ID = os.environ.get("EXECUTION_OWNER_ID") or f"{os.environ.get('RAILWAY_SERVICE_ID','local')}:{uuid.uuid4().hex}"
_LEASE_SECONDS = int(os.environ.get("EXECUTION_OWNERSHIP_LEASE_SECONDS", str(15 * 2)))

class ExecutionOwnershipUnavailable(CriticalStateUnavailable): pass
class StaleExecutionFence(ExecutionOwnershipUnavailable): pass

@dataclass(frozen=True)
class ExecutionOwnership:
    owner_id: str
    fencing_token: int
    session_id: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str

def _session_id():
    return os.environ.get("RAILWAY_DEPLOYMENT_ID") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "local"

def _now(): return datetime.now(timezone.utc)
def _parse(v): return v if isinstance(v, datetime) else datetime.fromisoformat(v)

_OWNERSHIP_LOG_SIGNATURES = {}

def _ownership_state_log(
    engine,
    *,
    event: str,
    db_lease_valid,
    local_lease_valid,
    execution_ownership_valid,
    fencing_valid,
    lease_remaining_seconds,
    reason: str,
) -> None:
    """Sanitized transition-based ownership telemetry; never changes authority."""
    key = id(engine) if engine is not None else 0
    signature = (
        event, db_lease_valid, local_lease_valid,
        execution_ownership_valid, fencing_valid, reason,
    )
    if _OWNERSHIP_LOG_SIGNATURES.get(key) == signature:
        return
    _OWNERSHIP_LOG_SIGNATURES[key] = signature
    from bot.logger import log
    log_fn = log.info if execution_ownership_valid is True else log.warning
    log_fn(
        "[OWNERSHIP_STATE] event=%s db_lease_valid=%s local_lease_valid=%s "
        "execution_ownership_valid=%s fencing_valid=%s lease_remaining_seconds=%s reason=%s",
        event,
        str(db_lease_valid).lower() if isinstance(db_lease_valid, bool) else "unknown",
        str(local_lease_valid).lower() if isinstance(local_lease_valid, bool) else "unknown",
        str(execution_ownership_valid).lower() if isinstance(execution_ownership_valid, bool) else "unknown",
        str(fencing_valid).lower() if isinstance(fencing_valid, bool) else "unknown",
        f"{max(0.0, float(lease_remaining_seconds)):.3f}" if lease_remaining_seconds is not None else "unknown",
        reason,
    )

def observe_readiness_ownership(engine, result: bool, *, reason: str) -> None:
    """Observe the readiness view without acquiring, renewing or mutating ownership."""
    expires_at = getattr(engine, "_execution_ownership_expires_at", None)
    remaining = None
    local_lease_valid = False
    if isinstance(expires_at, datetime):
        remaining = (expires_at - _now()).total_seconds()
        local_lease_valid = remaining > 0
    _ownership_state_log(
        engine,
        event="readiness_read",
        db_lease_valid=None,
        local_lease_valid=local_lease_valid,
        execution_ownership_valid=result,
        fencing_valid=None,
        lease_remaining_seconds=remaining,
        reason=reason,
    )

def publish_valid_execution_ownership(engine, ownership: ExecutionOwnership, *, event: str) -> None:
    """Publish validated DB lease state into the canonical local readiness view."""
    raw_expires_at = getattr(ownership, "expires_at", None)
    if raw_expires_at is None:
        invalidate_local_execution_ownership(engine, event=event, reason="lease_expiry_missing")
        return
    expires_at = _parse(raw_expires_at)
    remaining = max(0.0, (expires_at - _now()).total_seconds())
    if remaining <= 0:
        invalidate_local_execution_ownership(engine, event="lease_expired", reason="lease_expired")
        return
    engine._execution_ownership_expires_at = expires_at
    engine._execution_ownership_valid = True
    _ownership_state_log(
        engine,
        event=event,
        db_lease_valid=True,
        local_lease_valid=True,
        execution_ownership_valid=True,
        fencing_valid=True,
        lease_remaining_seconds=remaining,
        reason="validated",
    )

def invalidate_local_execution_ownership(engine, *, event: str, reason: str) -> None:
    engine._execution_ownership_valid = False
    engine._execution_ownership_expires_at = None
    _ownership_state_log(
        engine,
        event=event,
        db_lease_valid=False,
        local_lease_valid=False,
        execution_ownership_valid=False,
        fencing_valid=False,
        lease_remaining_seconds=0,
        reason=reason,
    )

async def acquire_execution_ownership(owner_id: str | None=None) -> ExecutionOwnership:
    if current_execution_capability() is not ExecutionCapability.LIVE:
        raise ExecutionOwnershipUnavailable("SHADOW_CANNOT_ACQUIRE_LIVE_EXECUTION_OWNERSHIP")
    conn=getattr(db,"_conn",None)
    if conn is None or not getattr(db,"_is_pg",False) or db.configured_postgres_unavailable():
        raise ExecutionOwnershipUnavailable("PostgreSQL ownership authority unavailable")
    owner=owner_id or _OWNER_ID; now=_now()
    try:
        return await _acquire_locked(owner, now)
    except asyncio.CancelledError:
        # A cancel left on the shared connection by another caller must not
        # silently end the heartbeat; only a real task cancel propagates.
        if is_spurious_cancel():
            db.schedule_connection_recovery("ownership_acquire")
            raise db.DatabaseOperationAborted("ownership acquire") from None
        raise

async def _acquire_locked(owner, now) -> ExecutionOwnership:
    async with db._io_lock:
      # Read under the lock: connection recovery may have replaced it.
      conn=db._conn
      if conn is None:
          raise ExecutionOwnershipUnavailable("PostgreSQL ownership authority unavailable")
      async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",_KEY)
        row=await conn.fetchrow("SELECT value FROM key_value WHERE key=$1 FOR UPDATE",_KEY)
        import json
        old=json.loads(row[0]) if row and row[0] else {}
        old_owner=str(old.get("owner_id","")); old_token=int(old.get("fencing_token",0) or 0)
        expires=old.get("expires_at"); live=False
        if expires:
            try: live=_parse(expires)>now
            except Exception: live=False
        if live and old_owner and old_owner!=owner:
            raise ExecutionOwnershipUnavailable("LIVE_EXECUTION_OWNERSHIP_HELD")
        token=old_token if live and old_owner==owner else old_token+1
        acquired=old.get("acquired_at") if live and old_owner==owner else now.isoformat()
        state=ExecutionOwnership(owner,token,_session_id(),acquired,now.isoformat(),(now+timedelta(seconds=_LEASE_SECONDS)).isoformat())
        payload=json.dumps(state.__dict__,sort_keys=True,separators=(",",":"))
        await conn.execute("INSERT INTO key_value(key,value,updated_at) VALUES($1,$2,$3) ON CONFLICT(key) DO UPDATE SET value=$2,updated_at=$3",_KEY,payload,now.isoformat())
        return state

async def validate_execution_ownership(ownership: ExecutionOwnership) -> None:
    if current_execution_capability() is not ExecutionCapability.LIVE:
        raise StaleExecutionFence("execution capability is not LIVE")
    conn=getattr(db,"_conn",None)
    if conn is None or not getattr(db,"_is_pg",False) or db.configured_postgres_unavailable():
        raise ExecutionOwnershipUnavailable("ownership validation database unavailable")
    import json
    try:
        async with db._io_lock:
            conn=db._conn
            if conn is None:
                raise ExecutionOwnershipUnavailable("ownership validation database unavailable")
            row=await conn.fetchrow("SELECT value FROM key_value WHERE key=$1",_KEY)
    except asyncio.CancelledError:
        if is_spurious_cancel():
            db.schedule_connection_recovery("ownership_validate")
            raise db.DatabaseOperationAborted("ownership validate") from None
        raise
    if not row or not row[0]: raise StaleExecutionFence("REJECTED_STALE_FENCE missing authority")
    cur=json.loads(row[0]); now=_now()
    if str(cur.get("owner_id"))!=ownership.owner_id or int(cur.get("fencing_token",-1))!=ownership.fencing_token:
        _ownership_state_log(
            None, event="takeover_detected", db_lease_valid=True,
            local_lease_valid=None, execution_ownership_valid=None,
            fencing_valid=False, lease_remaining_seconds=None,
            reason="superseded_fence",
        )
        raise StaleExecutionFence("REJECTED_STALE_FENCE superseded token")
    try: valid_until=_parse(str(cur.get("expires_at")))
    except Exception as exc: raise StaleExecutionFence("REJECTED_STALE_FENCE invalid lease") from exc
    if valid_until<=now:
        _ownership_state_log(
            None, event="lease_expired", db_lease_valid=False,
            local_lease_valid=None, execution_ownership_valid=None,
            fencing_valid=False, lease_remaining_seconds=0,
            reason="db_lease_expired",
        )
        raise StaleExecutionFence("REJECTED_STALE_FENCE expired lease")


async def initialize_live_execution_ownership(engine):
    """Establish the LIVE lease during engine startup, before readiness can pass.

    READ_ONLY runtimes never acquire LIVE authority. The ownership object is
    stored on the raw exchange client because KuCoin's transport fence executes
    there, while the readiness bit lives on the engine.
    """
    if current_execution_capability() is not ExecutionCapability.LIVE:
        invalidate_local_execution_ownership(engine, event="local_invalidated", reason="capability_not_live")
        return None
    ownership = await acquire_execution_ownership()
    try:
        remaining = max(0.0, (_parse(ownership.expires_at) - _now()).total_seconds())
    except Exception:
        remaining = None
    _ownership_state_log(
        engine,
        event="startup_acquired",
        db_lease_valid=True,
        local_lease_valid=False,
        execution_ownership_valid=bool(getattr(engine, "_execution_ownership_valid", False)),
        fencing_valid=False,
        lease_remaining_seconds=remaining,
        reason="acquired_pending_validation",
    )
    await validate_execution_ownership(ownership)
    client = getattr(engine, "client", None)
    raw_client = getattr(client, "_client", client)
    if raw_client is None:
        raise ExecutionOwnershipUnavailable("execution client unavailable")
    raw_client._execution_ownership = ownership
    # Keep proxy-visible state coherent for simple clients/tests.
    if client is not raw_client:
        client._execution_ownership = ownership
    publish_valid_execution_ownership(engine, ownership, event="startup_validated")
    from bot.logger import log
    log.info("[EXECUTION_OWNERSHIP] acquired=true fencing_valid=true lease_seconds=%s", _LEASE_SECONDS)
    return ownership


async def wait_for_live_execution_ownership(
    engine,
    *,
    retry_seconds: float = 2.0,
):
    """Wait fail-closed for exclusive LIVE ownership during blue-green rollout.

    A Railway candidate can overlap the currently serving replica during its
    healthcheck. The serving replica must retain the lease until cutover, so a
    candidate seeing LIVE_EXECUTION_OWNERSHIP_HELD is healthy-but-not-executable,
    not a crashed engine. No execution-valid bit is published until the normal
    acquire+validate path succeeds.
    """
    import asyncio
    from bot.logger import log

    delay = max(0.1, float(retry_seconds))
    while getattr(engine, "_running", False):
        try:
            return await initialize_live_execution_ownership(engine)
        except ExecutionOwnershipUnavailable as exc:
            if str(exc) != "LIVE_EXECUTION_OWNERSHIP_HELD":
                raise
            invalidate_local_execution_ownership(
                engine,
                event="startup_wait",
                reason="lease_held_by_serving_replica",
            )
            log.warning(
                "[EXECUTION_OWNERSHIP] startup_wait=true reason=%s "
                "retry_seconds=%.1f execution_ownership_valid=false "
                "execution_effect=BLOCK_NEW_ENTRIES",
                str(exc),
                delay,
            )
            await asyncio.sleep(delay)

    invalidate_local_execution_ownership(
        engine,
        event="startup_wait_cancelled",
        reason="engine_stopped",
    )
    return None


async def execution_ownership_heartbeat(engine):
    """Renew the LIVE lease for the current owner; fail closed on any loss."""
    from bot.logger import log
    if current_execution_capability() is not ExecutionCapability.LIVE:
        invalidate_local_execution_ownership(engine, event="local_invalidated", reason="capability_not_live")
        return
    interval = max(1.0, _LEASE_SECONDS / 3.0)
    try:
        while getattr(engine, "_running", False):
            try:
                expires_at = getattr(engine, "_execution_ownership_expires_at", None)
                remaining = (
                    (expires_at - _now()).total_seconds()
                    if isinstance(expires_at, datetime) else None
                )
                _ownership_state_log(
                    engine,
                    event="heartbeat_before_renew",
                    db_lease_valid=None,
                    local_lease_valid=bool(remaining is not None and remaining > 0),
                    execution_ownership_valid=bool(getattr(engine, "_execution_ownership_valid", False)),
                    fencing_valid=None,
                    lease_remaining_seconds=remaining,
                    reason="renewal_start",
                )
                ownership = await acquire_execution_ownership()
                await validate_execution_ownership(ownership)
                client = getattr(engine, "client", None)
                raw_client = getattr(client, "_client", client)
                raw_client._execution_ownership = ownership
                if client is not raw_client:
                    client._execution_ownership = ownership
                publish_valid_execution_ownership(engine, ownership, event="heartbeat_renewed")
                log.info("[EXECUTION_OWNERSHIP] heartbeat_renewed=true fencing_valid=true lease_seconds=%s", _LEASE_SECONDS)
            except asyncio.CancelledError:
                if not is_spurious_cancel():
                    raise
                # Driver-level cancel leaked from the shared DB connection:
                # fail closed for this renewal and keep the heartbeat alive.
                invalidate_local_execution_ownership(engine, event="heartbeat_failed", reason="SpuriousCancelledError")
                log.error("[EXECUTION_OWNERSHIP] heartbeat_invalid type=SpuriousCancelledError")
                db.schedule_connection_recovery("heartbeat")
            except Exception as exc:
                db.note_connection_failure(exc, reason="heartbeat")
                invalidate_local_execution_ownership(engine, event="heartbeat_failed", reason=type(exc).__name__)
                log.error("[EXECUTION_OWNERSHIP] heartbeat_invalid type=%s", type(exc).__name__)
            await asyncio.sleep(interval)
    finally:
        # A heartbeat that stops while the engine still runs must never leave a
        # locally valid lease behind it (it would only expire 30s later).
        if getattr(engine, "_running", False):
            invalidate_local_execution_ownership(engine, event="heartbeat_stopped", reason="heartbeat_exit_while_engine_running")
            log.error("[EXECUTION_OWNERSHIP] heartbeat_stopped engine_running=true local_ownership=INVALIDATED")
