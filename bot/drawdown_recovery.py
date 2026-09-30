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
import re

from bot.config import cfg
from bot import database as db
from bot.atomic_key_value import save_key_values_atomic_cas

AUTH_ENV = "LIVE_RECOVERY_AUTHORIZED"
EPISODE_ENV = "LIVE_RECOVERY_EPISODE_ID"
EXPIRES_ENV = "LIVE_RECOVERY_EXPIRES_AT"
MAX_DD_ENV = "RECOVERY_MAX_DRAWDOWN"
RISK_ENV = "RECOVERY_MAX_RISK_PCT"
RECEIPT_VERSION = 1
_TERMINAL_STATES = {"CONSUMED", "DISARMED", "EXPIRED"}


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


def _clean_namespace(value: str | None, fallback: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        text = fallback
    return re.sub(r"[^a-z0-9_.-]+", "_", text)[:96]


def receipt_key() -> str:
    env = _clean_namespace(
        os.environ.get("RAILWAY_ENVIRONMENT_ID")
        or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
        or os.environ.get("RAILWAY_ENVIRONMENT"),
        "unknown",
    )
    exchange = _clean_namespace(os.environ.get("EXCHANGE"), "unknown")
    return f"risk:drawdown_recovery:v{RECEIPT_VERSION}:environment={env}:exchange={exchange}"


def _dump(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_receipt(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("drawdown recovery receipt malformed") from exc
    if not isinstance(value, dict) or int(value.get("version", 0) or 0) != RECEIPT_VERSION:
        raise db.PersistenceError("drawdown recovery receipt schema mismatch")
    return value


def policy_from_env() -> RecoveryPolicy:
    authorized = os.environ.get(AUTH_ENV, "").strip().lower() == "true"
    if not authorized:
        return RecoveryPolicy(False, "", None, None, None, "disabled")

    episode = os.environ.get(EPISODE_ENV, "").strip()
    expires = _expiry(os.environ.get(EXPIRES_ENV))
    max_dd = _positive_finite(os.environ.get(MAX_DD_ENV))
    risk_pct = _positive_finite(os.environ.get(RISK_ENV))

    if _clean_namespace(os.environ.get("EXCHANGE"), "unknown") != "binance":
        return RecoveryPolicy(True, episode, expires, max_dd, risk_pct, "unsupported_exchange")
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



def _utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("recovery time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _positive(value, label: str) -> float:
    if isinstance(value, bool):
        raise db.PersistenceError(f"{label} invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError(f"{label} invalid") from exc
    if not math.isfinite(number) or number <= 0:
        raise db.PersistenceError(f"{label} invalid")
    return number


def _engine_equity_hwm(engine) -> tuple[float, float]:
    risk = getattr(engine, "risk", None)
    legacy = getattr(risk, "_legacy", risk)
    equity = _positive(getattr(legacy, "balance", None), "recovery equity")
    hwm = _positive(getattr(legacy, "peak_balance", None), "recovery HWM")
    if hwm + 1e-12 < equity:
        raise db.PersistenceError("recovery HWM below equity")
    return equity, hwm


def _receipt_matches_policy(receipt: dict, policy: RecoveryPolicy) -> bool:
    try:
        return (
            str(receipt.get("episode_id") or "") == policy.episode_id
            and str(receipt.get("expires_at") or "") == _iso(policy.expires_at)
            and math.isclose(
                float(receipt.get("max_drawdown")),
                float(policy.max_drawdown),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            and math.isclose(
                float(receipt.get("risk_pct")),
                float(policy.risk_pct),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
    except (TypeError, ValueError):
        return False


def _armed_receipt(
    policy: RecoveryPolicy,
    *,
    drawdown: float,
    equity: float,
    hwm: float,
    now: datetime,
) -> dict:
    return {
        "version": RECEIPT_VERSION,
        "episode_id": policy.episode_id,
        "status": "ARMED",
        "armed_at": _iso(now),
        "expires_at": _iso(policy.expires_at),
        "max_drawdown": float(policy.max_drawdown),
        "risk_pct": float(policy.risk_pct),
        "drawdown_at_arm": float(drawdown),
        "equity_at_arm": equity,
        "hwm_at_arm": hwm,
        "entry_attempts": 0,
    }


async def ensure_durable_episode(
    engine,
    drawdown: float,
    policy: RecoveryPolicy,
    *,
    now: datetime | None = None,
    strict: bool = True,
) -> tuple[bool, str, dict | None]:
    """Create/restore the exact Recovery episode before threshold bypass.

    A prior ARMED episode with another id blocks replacement. A terminal receipt
    may be replaced only by a different explicitly configured episode id.
    """
    if not policy.configured:
        return False, policy.reason, None
    current = _utc(now)
    if current >= policy.expires_at:
        return False, "expired", None

    equity, hwm = _engine_equity_hwm(engine)
    key = receipt_key()
    raw = await db.load_key_value(key, strict=strict)
    if raw is None:
        receipt = _armed_receipt(
            policy,
            drawdown=float(drawdown),
            equity=equity,
            hwm=hwm,
            now=current,
        )
        ok = await save_key_values_atomic_cas(
            [(key, _dump(receipt))],
            expected={key: None},
            strict=strict,
        )
        if strict and not ok:
            raise db.PersistenceError("recovery episode arm write unconfirmed")
        return True, "armed", receipt

    receipt = _load_receipt(raw)
    status = str(receipt.get("status") or "").upper()
    same_episode = str(receipt.get("episode_id") or "") == policy.episode_id

    if same_episode:
        if not _receipt_matches_policy(receipt, policy):
            raise db.PersistenceError("recovery receipt/config mismatch")
        if status == "ARMED":
            return True, "restored_armed", receipt
        if status == "CONSUMED":
            return False, "episode_consumed", receipt
        if status in {"DISARMED", "EXPIRED"}:
            return False, f"episode_{status.lower()}", receipt
        raise db.PersistenceError("recovery receipt state invalid")

    if status not in _TERMINAL_STATES:
        return False, "different_episode_already_armed", receipt

    replacement = _armed_receipt(
        policy,
        drawdown=float(drawdown),
        equity=equity,
        hwm=hwm,
        now=current,
    )
    ok = await save_key_values_atomic_cas(
        [(key, _dump(replacement))],
        expected={key: raw},
        strict=strict,
    )
    if strict and not ok:
        raise db.PersistenceError("recovery episode rotation write unconfirmed")
    return True, "rotated_armed", replacement


async def consume_for_dispatch(
    engine,
    *,
    symbol: str,
    qty: float,
    drawdown: float,
    now: datetime | None = None,
    strict: bool = True,
) -> tuple[bool, str, dict | None]:
    """Consume one Recovery episode before the exchange dispatch boundary.

    Consumption is intentionally one-shot and happens before network I/O. A
    rejected/ambiguous order does not restore the token; retry requires a new
    explicit episode id. This is stricter than post-trade disarm and makes
    restart behavior deterministic.
    """
    allowed, reason, policy = threshold_decision(drawdown, now=now)
    if allowed and reason == "normal_drawdown":
        return True, "normal_drawdown", None
    if not (allowed and reason == "recovery_threshold_exception"):
        return False, reason, None

    if bool(getattr(engine, "positions", {})):
        return False, "account_not_flat", None
    quantity = _positive(qty, "recovery dispatch quantity")
    symbol = str(symbol or "").upper()
    if not symbol:
        return False, "symbol_missing", None

    current = _utc(now)
    key = receipt_key()
    raw = await db.load_key_value(key, strict=strict)
    if raw is None:
        return False, "durable_receipt_missing", None
    receipt = _load_receipt(raw)
    if not _receipt_matches_policy(receipt, policy):
        raise db.PersistenceError("recovery dispatch receipt/config mismatch")
    if str(receipt.get("status") or "").upper() != "ARMED":
        return False, "episode_not_armed", receipt
    if current >= policy.expires_at:
        return False, "expired", receipt

    consumed = dict(receipt)
    consumed.update({
        "status": "CONSUMED",
        "consumed_at": _iso(current),
        "entry_attempts": 1,
        "dispatch_symbol": symbol,
        "dispatch_qty": quantity,
        "drawdown_at_consume": float(drawdown),
        "execution_effect": "ONE_SHOT_TOKEN_CONSUMED",
    })
    ok = await save_key_values_atomic_cas(
        [(key, _dump(consumed))],
        expected={key: raw},
        strict=strict,
    )
    if strict and not ok:
        raise db.PersistenceError("recovery episode consume write unconfirmed")
    return True, "consumed", consumed
