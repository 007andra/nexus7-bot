"""Redacted holder-of-shared-DB-lock sampling for OOS incident #596.

Observer records only bounded, allowlisted operation classes. It does not
replace a lock, execute SQL, fetch rows, change ordering, log identifiers, or
alter cancellation/transaction semantics. Caller chooses original lock by
default and this wrapper only with explicit diagnostic opt-in.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio

TABLE_CLASSES = (
    ("prospective_oos_cohort_v1", "oos_metadata"),
    ("hard_gate_shadow_outcomes_v1", "shadow_outcomes"),
    ("hard_gate_shadow_candidates_v1", "shadow_candidates"),
    ("key_value", "durable_key_value"),
    ("trades", "trades"),
    ("decisions", "decisions"),
    ("signals", "signals"),
    ("performance", "performance"),
    ("risk_events", "risk_events"),
    ("news_events", "news_events"),
    ("market_snapshots", "market_snapshots"),
)
SERIALIZED_CLASSES = {
    "save_trade_open": "trade_open",
    "save_paper_open_atomic": "paper_open",
    "_save_paper_close_atomic_locked": "paper_close",
    "get_recent_decisions": "recent_decisions",
    "save_key_value": "key_value_write",
    "_load_key_value_raw": "key_value_read",
    "get_trades_with_features": "trade_features",
}


def safe_query_label(operation: str, sql: str) -> str:
    """Known static labels only: no SQL text, parameter values or IDs emitted."""
    if operation not in ("exec", "fetchone", "fetchall"):
        raise ValueError("INVALID_DB_LOCK_TRACE_OPERATION")
    source = sql.lower() if isinstance(sql, str) else ""
    label = next((name for token, name in TABLE_CLASSES if token in source), "other")
    return operation + ":" + label


def safe_serialized_label(func_name: str) -> str:
    return "serialized:" + SERIALIZED_CLASSES.get(func_name, "other")


class LockHolderTracker:
    def __init__(self):
        self.holder = None

    def initial_holder(self, lock: asyncio.Lock):
        """Best-effort SINGLE snapshot; NOT definitive causal attribution.

        Several holders can serially acquire the lock during a single wait.
        An unheld-but-contended lock can yield UNKNOWN; never fabricate a label.
        """
        if not lock.locked():
            return "UNHELD_AT_START"
        return self.holder or "UNKNOWN_AT_START"

    @asynccontextmanager
    async def hold(self, lock: asyncio.Lock, label: str):
        if (not isinstance(label, str) or
                not (label.startswith("exec:") or
                     label.startswith("fetchone:") or
                     label.startswith("fetchall:") or
                     label.startswith("serialized:"))):
            raise ValueError("INVALID_DB_LOCK_TRACE_LABEL")
        async with lock:
            self.holder = label
            try:
                yield
            finally:
                # Never retain a stale holder after success, error or cancel.
                self.holder = None
