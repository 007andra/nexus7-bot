"""Post-decision observability for TradingEngine._nexus_validate.

The canonical NEXUS validation method always runs first. This decorator then
records decision history / zero-veto diagnostics and schedules rejected-decision
notification as best-effort telemetry. It always returns the exact decision
object produced by the canonical method.

No strategy threshold, execution permission, exchange mutation, release state,
or risk rule is changed here.
"""
from __future__ import annotations

import asyncio
from functools import wraps

from bot import nexus_persistence
from bot import nexus_zero_observability
from bot import notifier
from bot import rr_gate_calibration
from bot.logger import log


def observe_nexus_validation(method):
    """Decorate a canonical async NEXUS validator without changing its result."""
    @wraps(method)
    async def wrapped(self, sig, *args, **kwargs):
        decision = await method(self, sig, *args, **kwargs)

        # Attach lineage before any post-decision persistence. The outer live
        # cost wrapper may attach the same id again later; this operation is
        # deterministic and observational.
        try:
            from bot.candidate_trace import attach_decision
            attach_decision(decision, sig)
        except Exception as exc:
            log.debug(
                "[CANDIDATE_TRACE] decision_attach_failed symbol=%s error=%s "
                "decision_effect=NONE execution_effect=NONE",
                getattr(sig, "symbol", "UNKNOWN"), type(exc).__name__,
            )

        nexus_zero_observability.observe(decision, log)

        try:
            await nexus_persistence.record_decision(sig, decision)
            asyncio.create_task(nexus_persistence.evaluate_pending(self.client))
            try:
                from bot import nexus_shadow_drift
                asyncio.create_task(nexus_shadow_drift.refresh())
            except Exception as exc:
                log.debug(
                    "[NEXUS_SHADOW_DRIFT] schedule_failed error=%s "
                    "decision_effect=NONE execution_effect=NONE",
                    type(exc).__name__,
                )
        except Exception as exc:
            log.debug(
                "[NEXUS_PERSISTENCE] best_effort_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        # Schedule R:R-gate calibration instead of awaiting it. The canonical
        # live-cost wrapper carries its exact frozen candidate cost snapshot on
        # the decision object before resetting its internal ContextVar. This
        # task consumes that private telemetry handoff and performs no new
        # exchange/API request or trading-state mutation.
        try:
            asyncio.create_task(
                rr_gate_calibration.observe(self, sig, decision, log)
            )
        except Exception as exc:
            log.debug(
                "[RR_GATE_CALIBRATION] schedule_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        try:
            if getattr(decision, "execution_allowed", False) is not True:
                asyncio.create_task(
                    notifier.notify_nexus(decision.to_dict(), approved=False)
                )
        except Exception as exc:
            log.debug(
                "[NEXUS_NOTIFY] best_effort_schedule_failed error=%s "
                "decision_effect=NONE execution_effect=NONE",
                type(exc).__name__,
            )

        return decision

    return wrapped