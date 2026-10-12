"""One-shot, sanitized PostgreSQL role audit, also used by startup observer.

Run directly only in a separate, disposable Railway service without trading
credentials. The bot may invoke execute() solely via the opt-in startup
observer, never as its production start/pre-deploy command.

Only OOS_DIAGNOSTIC_DATABASE_URL is read. No DSN, exception text, role,
database, or object identifier is logged. The existing catalog audit uses
a READ ONLY transaction; no DB mutations or exchange actions are performed.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import os

from scripts.pg_readonly_role_audit import run

EXPECTED_CHECKS = {1: 1, 2: 1, 3: 3, 4: 2, 5: 2, 6: 2, 7: 1, 8: 1}
DSN_ENV = "OOS_DIAGNOSTIC_DATABASE_URL"

# Exact check labels are controlled by scripts.pg_readonly_role_audit.run().
# Never interpolate labels or evidence returned by a database/query into logs.
# These identifiers are non-sensitive, fixed vocabulary.
DB_PRIVILEGE_CHECKS = {
    "CREATE": "CREATE no banco",
    "TEMP": "TEMP no banco",
    "OWNER": "dono do banco",
}


def _group3_results(rows):
    """Return precisely 3 named PASS/FAIL decisions, otherwise None.

    Fail closed on missing, duplicated, renamed, or unrecognized rows so
    a truncated/malformed audit cannot be misclassified or misreported.
    """
    try:
        group = [row for row in rows if row["item"] == 3]
        if len(group) != len(DB_PRIVILEGE_CHECKS):
            return None
        result_by_label = {}
        for row in group:
            label, result = row["check"], row["result"]
            if (label not in DB_PRIVILEGE_CHECKS.values()
                    or result not in ("PASS", "FAIL")
                    or label in result_by_label):
                return None
            result_by_label[label] = result
        if set(result_by_label) != set(DB_PRIVILEGE_CHECKS.values()):
            return None
        return {name: result_by_label[label]
                for name, label in DB_PRIVILEGE_CHECKS.items()}
    except (KeyError, TypeError):
        return None


def _report_complete(audit) -> bool:
    """Guard against accidentally approving a truncated audit."""
    rows = getattr(audit, "rows", None)
    if not isinstance(rows, list):
        return False
    try:
        return (Counter(row["item"] for row in rows) == EXPECTED_CHECKS
                and all(row["result"] in ("PASS", "FAIL") for row in rows)
                and _group3_results(rows) is not None)
    except (TypeError, KeyError):
        return False


def _emit(status: str, rows=None) -> int:
    """Print strictly bounded, non-identifying diagnostic fields."""
    if rows is not None:
        for item in sorted(EXPECTED_CHECKS):
            matching = [r["result"] for r in rows if r["item"] == item]
            result = "PASS" if matching and all(v == "PASS" for v in matching) else "FAIL"
            print(f"[PG_ROLE_AUDIT_ONCE] item={item} result={result}", flush=True)
    # Group 3 was previously emitted only as an aggregate FAIL. These
    # fixed-name results identify the failing privilege without exposing any
    # database names, connection data, or untrusted check/evidence strings.
    group3 = _group3_results(rows) if rows is not None else None
    if group3 is not None:
        for name in DB_PRIVILEGE_CHECKS:
            print(
                f"[PG_ROLE_AUDIT_DB_PRIVILEGES] check={name} "
                f"result={group3[name]}",
                flush=True,
            )
    print(f"[PG_ROLE_AUDIT_ONCE] status={status} "
          "read_only_transaction=true execution_effect=NONE", flush=True)
    print(f"READ_ONLY_ROLE_VERIFIED={'true' if status == 'PASS' else 'false'}", flush=True)
    return 0 if status == "PASS" else (1 if status == "FAIL" else 2)


async def execute() -> int:
    dsn = os.environ.get(DSN_ENV, "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        return _emit("INCONCLUSIVE")

    conn = None
    close_ok = True
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
                close_ok = False
                try:
                    conn.terminate()
                except Exception:
                    pass

    if not close_ok or not _report_complete(audit):
        return _emit("INCONCLUSIVE")
    return _emit("PASS" if audit.verified else "FAIL", audit.rows)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(execute()))
