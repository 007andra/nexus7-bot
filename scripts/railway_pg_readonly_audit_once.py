"""One-shot, sanitized PostgreSQL role audit for a temporary Railway worker.

Run ONLY in a separate, disposable Railway service with no trading credentials:
    python -m scripts.railway_pg_readonly_audit_once

Supply OOS_DIAGNOSTIC_DATABASE_URL as a private service variable reference.
Never configure this as the nexus7-bot production start/pre-deploy command.
The process exits after one attempt; configure restart policy NEVER.
No DSN, server exception message, role name or object name is logged.
No transaction writes or trading/exchange actions are performed.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import os

from scripts.pg_readonly_role_audit import run

EXPECTED_CHECKS = {1: 1, 2: 1, 3: 3, 4: 2, 5: 2, 6: 2, 7: 1, 8: 1}
DSN_ENV = "OOS_DIAGNOSTIC_DATABASE_URL"


def _report_complete(audit) -> bool:
    """Guard against accidentally approving a truncated audit."""
    rows = getattr(audit, "rows", None)
    if not isinstance(rows, list):
        return False
    try:
        return Counter(row["item"] for row in rows) == EXPECTED_CHECKS and all(
            row["result"] in ("PASS", "FAIL") for row in rows
        )
    except (TypeError, KeyError):
        return False


def _emit(status: str, rows=None) -> int:
    """Print strictly bounded, non-identifying diagnostic fields."""
    if rows is not None:
        for item in sorted(EXPECTED_CHECKS):
            matching = [r["result"] for r in rows if r["item"] == item]
            result = "PASS" if matching and all(v == "PASS" for v in matching) else "FAIL"
            print(f"[PG_ROLE_AUDIT_ONCE] item={item} result={result}", flush=True)
    print(f"[PG_ROLE_AUDIT_ONCE] status={status} "
          "read_only_transaction=true execution_effect=NONE", flush=True)
    print(f"READ_ONLY_ROLE_VERIFIED={'true' if status == 'PASS' else 'false'}", flush=True)
    return 0 if status == "PASS" else (1 if status == "FAIL" else 2)


async def execute() -> int:
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        return _emit("INCONCLUSIVE")

    conn = None
    try:
        # The diagnostic credential is never transferred to the bot engine.
        import asyncpg
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn, timeout=10, command_timeout=10),
            timeout=12,
        )
        async with conn.transaction(readonly=True):
            audit = await asyncio.wait_for(run(conn), timeout=45)
    except Exception:
        # Do NOT print exception text/traceback: some drivers include DSNs.
        return _emit("INCONCLUSIVE")
    finally:
        if conn is not None:
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except Exception:
                # An unconfirmed close also fails closed; see note below.
                conn.terminate()

    if not _report_complete(audit):
        return _emit("INCONCLUSIVE")
    return _emit("PASS" if audit.verified else "FAIL", audit.rows)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(execute()))
