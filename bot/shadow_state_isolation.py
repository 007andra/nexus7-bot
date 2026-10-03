"""SHADOW LIVE state-isolation helpers.

This module exists only for non-executing SHADOW analysis. It never changes
exchange state, release authorization, durable state, or IntegrityGuard state.
Real LIVE/PAPER gates remain fail-closed.
"""


def shadow_analysis_only(engine) -> bool:
    """True only when LIVE is held by validation safety and effects are NONE."""
    return bool(
        not getattr(engine, "paper_trade", True)
        and getattr(engine, "_validation_safety_lock_active", False)
    )


def durable_analysis_blocker(engine):
    """Return a blocker only for durability faults relevant to hypothetical analysis.

    A manual/external position may deliberately make execution state divergent.
    That must continue to block real dispatch, but it need not suppress a purely
    hypothetical SHADOW evaluation. Database/order-registry durability faults are
    still treated as analysis blockers.
    """
    errors = set(getattr(engine, "_durable_state_errors", set()) or set())
    hard = sorted(e for e in errors if e in {"database", "orders", "orders_unresolved", "orders_restore", "paper"})
    return hard
