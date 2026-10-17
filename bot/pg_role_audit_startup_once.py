"""Optional one-shot PostgreSQL role audit after the web-service bootstrap.

Production default: disabled. Enabling PG_ROLE_AUDIT_STARTUP_ONCE=true
schedules exactly one isolated observer task per process. The observer waits
for the existing bootstrap task without delaying it, then runs the already
tested read-only catalog audit with its own asyncpg connection. No trading
authority, application database connection, risk gate, exchange client, or
HTTP endpoint is imported or modified here. Results go to normal stdout.

This is an *evidence emitter*, not a permission toggle. A PASS never enables
OOS_RESEARCH_READER_V2 or LIVE trading.
"""
from __future__ import annotations

import asyncio
import os

FLAG = "PG_ROLE_AUDIT_STARTUP_ONCE"
BOOTSTRAP_TIMEOUT_SECONDS = 120


def enabled() -> bool:
    """Require the exact literal 'true'; missing or other values do nothing."""
    return os.environ.get(FLAG, "") == "true"


def _inconclusive(reason: str) -> bool:
    # reason is a code literal defined in this module, never an exception.
    print(
        f"[PG_ROLE_AUDIT_STARTUP] status=INCONCLUSIVE reason={reason} "
        "read_only=true decision_effect=NONE execution_effect=NONE",
        flush=True,
    )
    print("READ_ONLY_ROLE_VERIFIED=false", flush=True)
    return False


async def run_after_bootstrap(bootstrap_task, *, runner=None,
                              bootstrap_timeout_s=BOOTSTRAP_TIMEOUT_SECONDS) -> bool:
    """Wait in a separate task; do not block the main lifespan/engine bootstrap.

    Never expose exception messages, tracebacks, DSNs, role names or object
    names. Cancel during shutdown and mark audit inconclusive.
    """
    try:
        # shield ensures this observer cannot cancel the real bootstrap.
        await asyncio.wait_for(asyncio.shield(bootstrap_task),
                               timeout=bootstrap_timeout_s)
    except asyncio.CancelledError:
        _inconclusive("shutdown")
        raise
    except asyncio.TimeoutError:
        return _inconclusive("bootstrap_timeout")
    except Exception:
        return _inconclusive("bootstrap_failed")

    try:
        if runner is None:
            from scripts.railway_pg_readonly_audit_once import execute
            runner = execute
        exit_code = await runner()
    except asyncio.CancelledError:
        _inconclusive("shutdown")
        raise
    except Exception:
        return _inconclusive("audit_exception")

    # The underlying script produces the individual item checks plus its
    # READ_ONLY_ROLE_VERIFIED marker, and independently enforces completeness.
    # Never trust an unrecognized exit code as verified.
    status = {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE"}.get(exit_code, "INCONCLUSIVE")
    print(
        f"[PG_ROLE_AUDIT_STARTUP] status={status} "
        "read_only=true decision_effect=NONE execution_effect=NONE",
        flush=True,
    )
    if exit_code not in (0, 1, 2):
        return _inconclusive("unexpected_exit_code")
    return exit_code == 0
