"""Bounded drawdown-recovery policy primitives.

This module is deliberately side-effect free. Recovery is disabled unless an
explicit, complete episode configuration is present. It never resets HWM,
changes leverage, or bypasses non-drawdown safety gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os

from bot.config import cfg
from bot import database as db
from bot.atomic_key_value import save_key_values_atomic_cas, CompareAndSwapConflict

AUTH_ENV = "LIVE_RECOVERY_AUTHORIZED"
EPISODE_ENV = "LIVE_RECOVERY_EPISODE_ID"
EXPIRES_ENV = "LIVE_RECOVERY_EXPIRES_AT"
MAX_DD_ENV = "RECOVERY_MAX_DRAWDOWN"
RISK_ENV = "RECOVERY_MAX_RISK_PCT"
STATE_KEY = "risk:drawdown_recovery:v1"


@dataclass(frozen=True)
class RecoveryPolicy:
    authorized: bool
    episode_id: str
    expires_at: datetime | None
    max_drawdown: float | None
    risk_pct: float | None
    reason: str

    @property
    def configured(self) -> bool:
        return (
            self.authorized
            and bool(self.episode_id)
            and self.expires_at is not None
            and self.max_drawdown is not None
            and self.risk_pct is not None
            and self.reason == "configured"
        )


def _positive_finite(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _expiry(raw: str | None) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def policy_from_env() -> RecoveryPolicy:
    authorized = os.environ.get(AUTH_ENV, "").strip().lower() == "true"
    if not authorized:
        return RecoveryPolicy(False, "", None, None, None, "disabled")

    episode = os.environ.get(EPISODE_ENV, "").strip()
    expires = _expiry(os.environ.get(EXPIRES_ENV))
    max_dd = _positive_finite(os.environ.get(MAX_DD_ENV))
    risk_pct = _positive_finite(os.environ.get(RISK_ENV))

    if not episode:
        return RecoveryPolicy(True, "", expires, max_dd, risk_pct, "missing_episode_id")
    if expires is None:
        return RecoveryPolicy(True, episode, None, max_dd, risk_pct, "invalid_expiry")
    if max_dd is None or not float(cfg.MAX_DRAWDOWN) < max_dd < 1.0:
        return RecoveryPolicy(True, episode, expires, max_dd, risk_pct, "invalid_recovery_drawdown")
    if risk_pct is None or risk_pct > float(cfg.MAX_RISK_PCT):
        return RecoveryPolicy(True, episode, expires, max_dd, risk_pct, "invalid_recovery_risk")
    return RecoveryPolicy(True, episode, expires, max_dd, risk_pct, "configured")


def threshold_decision(drawdown: float, *, now: datetime | None = None) -> tuple[bool, str, RecoveryPolicy]:
    """Decide only the drawdown-threshold exception.

    A PASS here is never sufficient to dispatch an order. Existing ownership,
    fencing, reconciliation, private-stream, protection, market-data, CROSS
    stress and duplicate-order gates remain authoritative.
    """
    try:
        dd = float(drawdown)
    except (TypeError, ValueError):
        return False, "drawdown_unreadable", policy_from_env()
    if not math.isfinite(dd) or dd < 0:
        return False, "drawdown_unreadable", policy_from_env()
    if dd < float(cfg.MAX_DRAWDOWN):
        return True, "normal_drawdown", policy_from_env()

    policy = policy_from_env()
    if not policy.configured:
        return False, policy.reason, policy

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current >= policy.expires_at:
        return False, "expired", policy
    if dd >= float(policy.max_drawdown):
        return False, "recovery_ceiling_reached", policy
    return True, "recovery_threshold_exception", policy


def recovery_size_multiplier(drawdown: float, *, now: datetime | None = None) -> float:
    """Return a <=1 multiplier for the existing normal-risk sizing path."""
    allowed, reason, policy = threshold_decision(drawdown, now=now)
    if not allowed:
        return 0.0
    if reason == "normal_drawdown":
        return 1.0
    normal = float(cfg.MAX_RISK_PCT)
    if normal <= 0 or policy.risk_pct is None:
        return 0.0
    multiplier = float(policy.risk_pct) / normal
    if not math.isfinite(multiplier) or not 0 < multiplier <= 1:
        return 0.0
    return multiplier


def _state_payload(*, policy: RecoveryPolicy, status: str, armed_drawdown: float,
                   worst_drawdown: float, reason: str, realized_net_pnl: float | None = None,
                   armed_at: str | None = None) -> str:
    payload = {
        "version": 1,
        "episode_id": policy.episode_id,
        "status": status,
        "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
        "max_drawdown": policy.max_drawdown,
        "risk_pct": policy.risk_pct,
        "armed_at": armed_at or datetime.now(timezone.utc).isoformat(),
        "armed_drawdown": armed_drawdown,
        "worst_drawdown": worst_drawdown,
        "reason": reason,
        "realized_net_pnl": realized_net_pnl,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _policy_from_state(state: dict) -> RecoveryPolicy:
    expires = _expiry(str(state.get("expires_at") or ""))
    try:
        max_dd = float(state.get("max_drawdown"))
        risk_pct = float(state.get("risk_pct"))
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("recovery durable policy malformed") from exc
    if (
        not str(state.get("episode_id") or "")
        or expires is None
        or not math.isfinite(max_dd)
        or not math.isfinite(risk_pct)
        or max_dd <= 0
        or risk_pct <= 0
    ):
        raise db.PersistenceError("recovery durable policy malformed")
    return RecoveryPolicy(
        True,
        str(state.get("episode_id")),
        expires,
        max_dd,
        risk_pct,
        "configured",
    )


def _decode_state(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("recovery durable state malformed") from exc
    if not isinstance(value, dict) or int(value.get("version", 0) or 0) != 1:
        raise db.PersistenceError("recovery durable state version invalid")
    return value


async def ensure_durable_episode(drawdown: float, *, strict: bool = True) -> tuple[bool, str]:
    """Create/validate the durable recovery receipt.

    Restart semantics are deterministic: the same configured episode must match
    the durable receipt exactly and remain ARMED. A different episode cannot
    overwrite an existing receipt. Persistence ambiguity fails closed.
    """
    allowed, reason, policy = threshold_decision(drawdown)
    if not (allowed and reason == "recovery_threshold_exception"):
        return False, reason

    raw = await db.load_key_value(STATE_KEY, strict=strict)
    state = _decode_state(raw)
    if state is None:
        payload = _state_payload(
            policy=policy, status="ARMED", armed_drawdown=float(drawdown),
            worst_drawdown=float(drawdown), reason="operator_armed",
        )
        try:
            ok = await save_key_values_atomic_cas(
                ((STATE_KEY, payload),), expected={STATE_KEY: None}, strict=strict,
            )
        except CompareAndSwapConflict:
            return False, "durable_arm_conflict"
        if not ok:
            return False, "durable_arm_unconfirmed"
        return True, "durable_armed"

    if str(state.get("episode_id") or "") != policy.episode_id:
        return False, "durable_episode_mismatch"
    if str(state.get("status") or "") != "ARMED":
        return False, "durable_episode_disarmed"
    try:
        armed_at = datetime.fromisoformat(str(state.get("armed_at") or "").replace("Z", "+00:00"))
        if armed_at.tzinfo is None:
            raise ValueError("naive armed_at")
    except (TypeError, ValueError):
        return False, "durable_armed_at_malformed"
    try:
        armed_drawdown = float(state.get("armed_drawdown"))
        worst_drawdown = float(state.get("worst_drawdown"))
    except (TypeError, ValueError):
        return False, "durable_drawdown_malformed"
    if (
        not math.isfinite(armed_drawdown)
        or not math.isfinite(worst_drawdown)
        or armed_drawdown < 0
        or worst_drawdown < armed_drawdown
    ):
        return False, "durable_drawdown_malformed"
    # A previous process may have observed worsening drawdown and crashed before
    # persisting DISARMED. Refuse restart if the current authenticated drawdown
    # is worse than the original arm point.
    if float(drawdown) > armed_drawdown + 1e-12:
        return False, "restart_drawdown_worsened"
    if state.get("expires_at") != policy.expires_at.isoformat():
        return False, "durable_expiry_mismatch"
    if float(state.get("max_drawdown", -1)) != float(policy.max_drawdown):
        return False, "durable_ceiling_mismatch"
    if float(state.get("risk_pct", -1)) != float(policy.risk_pct):
        return False, "durable_risk_mismatch"
    return True, "durable_restart_match"


async def record_drawdown_observation(drawdown: float, *, strict: bool = True) -> tuple[bool, str]:
    """Persist worst drawdown and disarm if the episode worsens from its arm point."""
    raw = await db.load_key_value(STATE_KEY, strict=strict)
    state = _decode_state(raw)
    if state is None or str(state.get("status") or "") != "ARMED":
        return False, "not_armed"
    dd = float(drawdown)
    armed = float(state.get("armed_drawdown"))
    worst = max(float(state.get("worst_drawdown", armed)), dd)
    status = "DISARMED" if dd > armed + 1e-12 else "ARMED"
    reason = "drawdown_worsened" if status == "DISARMED" else "drawdown_observed"
    policy = _policy_from_state(state)
    payload = _state_payload(
        policy=policy, status=status, armed_drawdown=armed,
        worst_drawdown=worst, reason=reason,
        armed_at=str(state.get("armed_at") or ""),
    )
    try:
        await save_key_values_atomic_cas(
            ((STATE_KEY, payload),), expected={STATE_KEY: raw}, strict=strict,
        )
    except CompareAndSwapConflict:
        return False, "durable_observation_conflict"
    return status == "ARMED", reason


async def record_recovery_close(
    net_pnl: float,
    *,
    opening_fill_ms: int | None = None,
    strict: bool = True,
) -> tuple[bool, str]:
    """Apply a confirmed close only when it belongs to the armed episode.

    Old startup/backfill exits must never disarm a newly armed recovery episode.
    We bind by Binance opening fill time versus the durable armed_at timestamp.
    Missing/invalid lineage is conservative: the episode is disarmed because
    attribution cannot be proven safe.
    """
    raw = await db.load_key_value(STATE_KEY, strict=strict)
    state = _decode_state(raw)
    if state is None or str(state.get("status") or "") != "ARMED":
        return False, "not_armed"

    try:
        armed_at = datetime.fromisoformat(str(state.get("armed_at") or "").replace("Z", "+00:00"))
        if armed_at.tzinfo is None:
            raise ValueError("naive armed_at")
        armed_ms = int(armed_at.astimezone(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        armed_ms = -1

    policy = _policy_from_state(state)
    if armed_ms <= 0 or opening_fill_ms is None or int(opening_fill_ms) <= 0:
        payload = _state_payload(
            policy=policy, status="DISARMED",
            armed_drawdown=float(state.get("armed_drawdown")),
            worst_drawdown=float(state.get("worst_drawdown")),
            reason="close_lineage_unbound",
            armed_at=str(state.get("armed_at") or ""),
        )
        try:
            await save_key_values_atomic_cas(
                ((STATE_KEY, payload),), expected={STATE_KEY: raw}, strict=strict,
            )
        except CompareAndSwapConflict:
            return False, "durable_close_conflict"
        return False, "close_lineage_unbound"

    if int(opening_fill_ms) < armed_ms:
        return True, "outside_episode"

    pnl = float(net_pnl)
    if not math.isfinite(pnl):
        payload = _state_payload(
            policy=policy, status="DISARMED",
            armed_drawdown=float(state.get("armed_drawdown")),
            worst_drawdown=float(state.get("worst_drawdown")),
            reason="close_pnl_unconfirmed",
            armed_at=str(state.get("armed_at") or ""),
        )
        try:
            await save_key_values_atomic_cas(
                ((STATE_KEY, payload),), expected={STATE_KEY: raw}, strict=strict,
            )
        except CompareAndSwapConflict:
            return False, "durable_close_conflict"
        return False, "close_pnl_unconfirmed"
    if pnl >= 0:
        return True, "non_losing_close"

    payload = _state_payload(
        policy=policy, status="DISARMED",
        armed_drawdown=float(state.get("armed_drawdown")),
        worst_drawdown=float(state.get("worst_drawdown")),
        reason="recovery_trade_net_loss", realized_net_pnl=pnl,
        armed_at=str(state.get("armed_at") or ""),
    )
    try:
        await save_key_values_atomic_cas(
            ((STATE_KEY, payload),), expected={STATE_KEY: raw}, strict=strict,
        )
    except CompareAndSwapConflict:
        return False, "durable_close_conflict"
    return False, "recovery_trade_net_loss"

