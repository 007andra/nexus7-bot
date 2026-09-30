"""Bounded drawdown-recovery policy primitives.

This module is deliberately side-effect free. Recovery is disabled unless an
explicit, complete episode configuration is present. It never resets HWM,
changes leverage, or bypasses non-drawdown safety gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os

from bot.config import cfg

AUTH_ENV = "LIVE_RECOVERY_AUTHORIZED"
EPISODE_ENV = "LIVE_RECOVERY_EPISODE_ID"
EXPIRES_ENV = "LIVE_RECOVERY_EXPIRES_AT"
MAX_DD_ENV = "RECOVERY_MAX_DRAWDOWN"
RISK_ENV = "RECOVERY_MAX_RISK_PCT"


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
