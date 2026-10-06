"""One-shot controlled LIVE re-entry authority for NEXUS-7.

This module is deliberately narrower than the account-wide drawdown policy.
It never resets or rebases the historical HWM, never changes MAX_DRAWDOWN,
never relaxes NEXUS/RR/EV thresholds, and never authorizes more than one new
submission for one explicitly named operator episode.

Purpose:
- permit one execution-proof candidate while the historical account drawdown
  remains above the normal hard gate;
- size that candidate from an absolute USDT loss ceiling, capped by a maximum
  percentage of freshly confirmed equity;
- consume the episode durably at the final transport boundary before HTTP POST.

The module is inert unless every explicit environment prerequisite is present.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os

ENABLED_ENV = "CONTROLLED_LIVE_REENTRY_ENABLED"
EPISODE_ENV = "CONTROLLED_LIVE_REENTRY_EPISODE_ID"
ARM_ENV = "CONTROLLED_LIVE_REENTRY_ARM"
LOSS_BUDGET_ENV = "CONTROLLED_LIVE_REENTRY_LOSS_BUDGET_USDT"
MAX_RISK_PCT_ENV = "CONTROLLED_LIVE_REENTRY_MAX_RISK_PCT"

ARM_TOKEN = "I_APPROVE_ONE_CONTROLLED_LIVE_REENTRY"
STATE_KEY_PREFIX = "controlled_live_reentry_v1:"
HARD_MAX_RISK_PCT = 0.02
_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class ReentryPolicy:
    enabled: bool
    episode_id: str
    armed: bool
    loss_budget_usdt: float | None
    max_risk_pct: float | None
    reason: str

    @property
    def envelope_configured(self) -> bool:
        """Risk envelope exists even while the final LIVE arm is absent.

        This may be used for read-only feasibility/NEXUS traversal. It grants
        no drawdown bridge and no dispatch authority.
        """
        return (
            self.enabled
            and bool(self.episode_id)
            and self.loss_budget_usdt is not None
            and self.max_risk_pct is not None
            and 0 < float(self.max_risk_pct) <= HARD_MAX_RISK_PCT
        )

    @property
    def configured(self) -> bool:
        return (
            self.envelope_configured
            and self.armed
            and self.reason == "configured"
        )


def _positive(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def policy_from_env() -> ReentryPolicy:
    enabled = os.environ.get(ENABLED_ENV, "").strip().lower() == "true"
    if not enabled:
        return ReentryPolicy(False, "", False, None, None, "disabled")

    episode = os.environ.get(EPISODE_ENV, "").strip()
    armed = os.environ.get(ARM_ENV, "").strip() == ARM_TOKEN
    budget = _positive(os.environ.get(LOSS_BUDGET_ENV))
    max_risk = _positive(os.environ.get(MAX_RISK_PCT_ENV))

    if not episode:
        return ReentryPolicy(True, "", armed, budget, max_risk, "missing_episode_id")
    if not armed:
        return ReentryPolicy(True, episode, False, budget, max_risk, "manual_arm_missing")
    if budget is None:
        return ReentryPolicy(True, episode, True, None, max_risk, "invalid_loss_budget")
    if max_risk is None or max_risk > HARD_MAX_RISK_PCT:
        return ReentryPolicy(True, episode, True, budget, max_risk, "invalid_max_risk_pct")
    return ReentryPolicy(True, episode, True, budget, max_risk, "configured")


def _pending_orders(engine) -> int:
    orders = getattr(engine, "orders", None)
    reader = getattr(orders, "pending_orders", None)
    if not callable(reader):
        return 0
    try:
        return len(list(reader() or []))
    except Exception:
        return 1


def _capital(engine) -> tuple[float, float, bool]:
    risk = getattr(engine, "risk", None)
    snapshot = getattr(risk, "professional_snapshot", None)
    if snapshot is not None:
        try:
            equity = float(snapshot.capital.equity)
            available = float(snapshot.capital.available_collateral)
            confirmed = bool(snapshot.confirmed)
            if (
                confirmed
                and math.isfinite(equity)
                and math.isfinite(available)
                and equity > 0
                and available > 0
            ):
                return equity, available, True
        except (AttributeError, TypeError, ValueError):
            # A present professional snapshot is the authoritative LIVE capital
            # source. If it is malformed/unreadable, fail closed instead of
            # falling back to less authoritative cached/legacy balances.
            return 0.0, 0.0, False

    try:
        equity = float(
            getattr(engine, "_pilot_account_equity", 0.0)
            or getattr(risk, "balance", 0.0)
            or 0.0
        )
        available = float(
            getattr(engine, "_pilot_available_balance", 0.0)
            or getattr(risk, "available_balance", 0.0)
            or 0.0
        )
        confirmed = bool(getattr(risk, "balance_confirmed", False))
    except (TypeError, ValueError):
        return 0.0, 0.0, False
    valid = (
        confirmed
        and math.isfinite(equity)
        and math.isfinite(available)
        and equity > 0
        and available > 0
    )
    return equity, available, valid


def candidate_risk_pct(engine, equity: float) -> float:
    """Return the configured one-shot risk pct for early feasibility only.

    This helper does not authorize execution and intentionally does not require
    the later preflight/capital snapshot. Final execution calls readiness()
    again after fresh authenticated account reads.
    """
    policy = policy_from_env()
    if not policy.envelope_configured:
        raise RuntimeError(f"controlled re-entry envelope unavailable: {policy.reason}")
    try:
        equity_f = float(equity)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid feasibility equity") from exc
    if not math.isfinite(equity_f) or equity_f <= 0:
        raise ValueError("invalid feasibility equity")

    from bot import pilot
    from bot.pilot_release_control import live_pilot_release_authorized
    from bot.operator_runtime_policy import _risk_override_enabled
    from bot.drawdown_recovery import policy_from_env as recovery_policy

    if getattr(engine, "paper_trade", True):
        raise RuntimeError("paper_mode")
    if not live_pilot_release_authorized():
        raise RuntimeError("controlled_pilot_release_missing")
    if not bool(getattr(getattr(engine, "pilot", None), "enabled", False)):
        raise RuntimeError("pilot_guard_disabled")
    if int(pilot.PILOT_MAX_CONCURRENT_POSITIONS) != 1:
        raise RuntimeError("pilot_position_cap_not_one")
    if int(pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION) != 1:
        raise RuntimeError("pilot_submission_cap_not_one")
    if _risk_override_enabled():
        raise RuntimeError("generic_risk_override_forbidden")
    if recovery_policy().authorized:
        raise RuntimeError("overlapping_recovery_authority_forbidden")

    effective = min(float(policy.max_risk_pct), float(policy.loss_budget_usdt) / equity_f)
    if not math.isfinite(effective) or not 0 < effective <= HARD_MAX_RISK_PCT:
        raise RuntimeError("effective_risk_invalid")
    return effective


def readiness(engine) -> tuple[bool, str, dict]:
    """Validate only the segregated one-shot re-entry contract.

    Existing ownership, market data, private stream, protection, durable state,
    CROSS stress and NEXUS gates remain independent and authoritative.
    """
    policy = policy_from_env()
    evidence = {
        "episode_id": policy.episode_id,
        "loss_budget_usdt": policy.loss_budget_usdt,
        "max_risk_pct": policy.max_risk_pct,
    }
    if not policy.configured:
        return False, policy.reason, evidence

    if getattr(engine, "paper_trade", True):
        return False, "paper_mode", evidence

    try:
        from bot import pilot
        from bot.pilot_release_control import live_pilot_release_authorized
        from bot.operator_runtime_policy import _risk_override_enabled
        from bot.drawdown_recovery import policy_from_env as recovery_policy
    except Exception as exc:
        return False, f"authority_import_{type(exc).__name__}", evidence

    if not live_pilot_release_authorized():
        return False, "controlled_pilot_release_missing", evidence
    if not bool(getattr(getattr(engine, "pilot", None), "enabled", False)):
        return False, "pilot_guard_disabled", evidence
    if int(pilot.PILOT_MAX_CONCURRENT_POSITIONS) != 1:
        return False, "pilot_position_cap_not_one", evidence
    if int(pilot.MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION) != 1:
        return False, "pilot_submission_cap_not_one", evidence
    if _risk_override_enabled():
        return False, "generic_risk_override_forbidden", evidence
    if recovery_policy().authorized:
        return False, "overlapping_recovery_authority_forbidden", evidence
    if len(getattr(engine, "positions", {}) or {}) != 0:
        return False, "account_not_flat", evidence
    if _pending_orders(engine) != 0:
        return False, "pending_orders", evidence
    if not bool(getattr(engine, "_pilot_live_prelive_ready", False)):
        return False, "preflight_not_ready", evidence

    equity, available, confirmed = _capital(engine)
    evidence.update({"equity": equity, "available": available, "capital_confirmed": confirmed})
    if not confirmed:
        return False, "capital_unconfirmed", evidence

    effective = min(
        float(policy.max_risk_pct),
        float(policy.loss_budget_usdt) / equity,
    )
    if not math.isfinite(effective) or not 0 < effective <= HARD_MAX_RISK_PCT:
        return False, "effective_risk_invalid", evidence
    evidence["effective_risk_pct"] = effective
    evidence["effective_risk_budget_usdt"] = equity * effective
    return True, "ready", evidence


def effective_risk_pct(engine) -> float:
    ok, reason, evidence = readiness(engine)
    if not ok:
        raise RuntimeError(f"controlled re-entry unavailable: {reason}")
    return float(evidence["effective_risk_pct"])


def drawdown_bridge_allowed(engine) -> tuple[bool, str]:
    ok, reason, evidence = readiness(engine)
    if not ok:
        return False, reason
    return True, (
        f"episode={evidence['episode_id']}:"
        f"budget={float(evidence['loss_budget_usdt']):.12g}:"
        f"risk_pct={float(evidence['effective_risk_pct']):.12g}"
    )


def projected_loss_allowed(engine, projected_loss: float) -> tuple[bool, str, dict]:
    ok, reason, evidence = readiness(engine)
    if not ok:
        return False, reason, evidence
    try:
        loss = float(projected_loss)
    except (TypeError, ValueError):
        return False, "projected_loss_unreadable", evidence
    budget = float(evidence["loss_budget_usdt"])
    if not math.isfinite(loss) or loss < 0:
        return False, "projected_loss_unreadable", evidence
    tolerance = max(1e-12, budget * 1e-6)
    evidence = dict(evidence)
    evidence["projected_loss_usdt"] = loss
    evidence["headroom_usdt"] = budget - loss
    if loss > budget + tolerance:
        return False, "absolute_loss_budget_exceeded", evidence
    return True, "within_absolute_loss_budget", evidence


def _state_key(episode_id: str) -> str:
    return f"{STATE_KEY_PREFIX}{episode_id}"


def _encode_consumed(policy: ReentryPolicy, *, symbol: str, client_oid: str) -> str:
    return json.dumps({
        "version": 1,
        "episode_id": policy.episode_id,
        "status": "DISPATCH_CONSUMED",
        "symbol": str(symbol),
        "client_oid": str(client_oid),
        "loss_budget_usdt": policy.loss_budget_usdt,
        "max_risk_pct": policy.max_risk_pct,
        "consumed_at": datetime.now(timezone.utc).isoformat(),
        "reset_allowed": False,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)


async def consume_dispatch_once(engine, *, symbol: str, client_oid: str) -> tuple[bool, str]:
    """Consume the one-shot episode durably before the exchange HTTP POST.

    Consumption is intentionally not rolled back after this point. Ambiguous
    transport or exchange rejection therefore cannot accidentally grant a
    second real-money attempt. A new attempt requires a new episode id and
    explicit arm token.
    """
    ok, reason, _ = readiness(engine)
    if not ok:
        return False, reason
    policy = policy_from_env()
    if not policy.configured:
        return False, policy.reason

    from bot import database as db
    from bot.critical_state import critical_state

    critical_state.assert_available_for_new_risk()
    conn = getattr(db, "_conn", None)
    if conn is None or db.configured_postgres_unavailable():
        raise db.PersistenceError("controlled re-entry reservation blocked: database unavailable")

    key = _state_key(policy.episode_id)
    payload = _encode_consumed(policy, symbol=symbol, client_oid=client_oid)

    async with _LOCK:
        async with db._io_lock:
            if getattr(db, "_is_pg", False):
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
                    row = await conn.fetchrow(
                        "SELECT value FROM key_value WHERE key=$1 FOR UPDATE", key
                    )
                    if row is not None:
                        return False, "episode_already_consumed"
                    await conn.execute(
                        "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3)",
                        key, payload, datetime.now(timezone.utc).isoformat(),
                    )
                    return True, "dispatch_authorization_consumed"

            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute(
                    "SELECT value FROM key_value WHERE key=?", (key,)
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    await conn.commit()
                    return False, "episode_already_consumed"
                await conn.execute(
                    "INSERT INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                    (key, payload, datetime.now(timezone.utc).isoformat()),
                )
                await conn.commit()
                return True, "dispatch_authorization_consumed"
            except Exception:
                await conn.rollback()
                raise


def startup_log() -> str:
    policy = policy_from_env()
    return (
        "[CONTROLLED_LIVE_REENTRY_V1] "
        f"enabled={str(policy.enabled).lower()} "
        f"envelope_configured={str(policy.envelope_configured).lower()} "
        f"configured={str(policy.configured).lower()} "
        f"armed={str(policy.armed).lower()} "
        f"episode_id={policy.episode_id or 'NA'} "
        f"loss_budget_usdt={policy.loss_budget_usdt if policy.loss_budget_usdt is not None else 'NA'} "
        f"max_risk_pct={policy.max_risk_pct if policy.max_risk_pct is not None else 'NA'} "
        f"reason={policy.reason} one_shot=true max_positions=1 max_submissions=1 "
        "historical_hwm_preserved=true max_drawdown_unchanged=true "
        "nexus_thresholds_unchanged=true rr_ev_unchanged=true "
        "averaging_down=false martingale=false"
    )


__all__ = [
    "ARM_TOKEN",
    "HARD_MAX_RISK_PCT",
    "ReentryPolicy",
    "policy_from_env",
    "readiness",
    "candidate_risk_pct",
    "effective_risk_pct",
    "drawdown_bridge_allowed",
    "projected_loss_allowed",
    "consume_dispatch_once",
    "startup_log",
]
