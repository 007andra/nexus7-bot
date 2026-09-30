"""Bounded, explicit drawdown recovery authorization for controlled LIVE.

This module never places/cancels orders and never changes HWM or MAX_DRAWDOWN.
It permits at most one new-risk episode only when explicitly configured and all
fresh LIVE safety authorities are confirmed. Missing/invalid configuration is
fail-closed.

The normal 10% hard gate remains authoritative outside a candidate-scoped
recovery context.
"""
from __future__ import annotations

import contextvars
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from bot import cash_flow_ledger
from bot import database as db
from bot import durable_execution
from bot import execution_ownership
from bot.atomic_key_value import save_key_values_atomic_cas
from bot.config import cfg
from bot.logger import log
from bot.private_stream_health import PrivateStreamHealth


APPROVED_ENV = "LIVE_DRAWDOWN_RECOVERY_APPROVED"
EPISODE_ENV = "LIVE_DRAWDOWN_RECOVERY_EPISODE_ID"
EXPIRES_ENV = "LIVE_DRAWDOWN_RECOVERY_EXPIRES_AT"
MAX_DRAWDOWN_ENV = "LIVE_DRAWDOWN_RECOVERY_MAX_DRAWDOWN"
MAX_RISK_ENV = "LIVE_DRAWDOWN_RECOVERY_MAX_RISK_PCT"
BROAD_OVERRIDE_ENV = "LIVE_RISK_OVERRIDE_APPROVED"

STATE_KEY = "risk:drawdown_recovery:episode:v1"
STATE_VERSION = 1
CONTEXT_MAX_AGE_S = 30.0
_EPISODE_RE = re.compile(r"^[A-Za-z0-9._:-]{6,96}$")
_EPS = 1e-9

_CTX: contextvars.ContextVar["RecoveryContext | None"] = contextvars.ContextVar(
    "nexus_drawdown_recovery_context", default=None
)


@dataclass(frozen=True)
class RecoveryConfig:
    episode_id: str
    expires_at: datetime
    max_drawdown: float
    max_risk_pct: float


@dataclass(frozen=True)
class RecoveryContext:
    episode_id: str
    symbol: str
    max_drawdown: float
    max_risk_pct: float
    arm_drawdown: float
    validated_drawdown: float
    expires_at_ts: float
    validated_mono: float


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def approval_requested() -> bool:
    return _truthy(APPROVED_ENV)


def broad_override_conflict() -> bool:
    return approval_requested() and _truthy(BROAD_OVERRIDE_ENV)


def _pct(raw: str, field: str) -> float:
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"{field} missing")
    if value.endswith("%"):
        number = float(value[:-1].strip()) / 100.0
    else:
        number = float(value)
    if not math.isfinite(number) or not 0 < number < 1:
        raise ValueError(f"{field} outside (0,1)")
    return number


def _expiry(raw: str) -> datetime:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("recovery expiry missing")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("recovery expiry must include timezone")
    return dt.astimezone(timezone.utc)


def load_config(*, now: datetime | None = None) -> tuple[RecoveryConfig | None, str]:
    """Parse explicit operator configuration. Never supplies risk defaults."""
    if not approval_requested():
        return None, "disabled"
    if broad_override_conflict():
        return None, "broad_override_conflict"
    try:
        episode_id = os.environ.get(EPISODE_ENV, "").strip()
        if not _EPISODE_RE.fullmatch(episode_id):
            raise ValueError("invalid recovery episode id")
        expires_at = _expiry(os.environ.get(EXPIRES_ENV, ""))
        now = now or datetime.now(timezone.utc)
        if expires_at <= now:
            return None, "expired"
        max_drawdown = _pct(os.environ.get(MAX_DRAWDOWN_ENV, ""), MAX_DRAWDOWN_ENV)
        max_risk_pct = _pct(os.environ.get(MAX_RISK_ENV, ""), MAX_RISK_ENV)
        normal_limit = float(cfg.MAX_DRAWDOWN)
        normal_risk = float(cfg.MAX_RISK_PCT)
        if not math.isfinite(normal_limit) or not 0 < normal_limit < 1:
            raise ValueError("invalid configured MAX_DRAWDOWN")
        if not math.isfinite(normal_risk) or not 0 < normal_risk <= 1:
            raise ValueError("invalid configured MAX_RISK_PCT")
        if max_drawdown <= normal_limit:
            raise ValueError("recovery max drawdown must exceed normal hard gate")
        if max_risk_pct >= normal_risk:
            raise ValueError("recovery risk must be strictly below normal risk")
        return RecoveryConfig(
            episode_id=episode_id,
            expires_at=expires_at,
            max_drawdown=max_drawdown,
            max_risk_pct=max_risk_pct,
        ), "configured"
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_config"


def scan_decision(drawdown: float, open_positions: int) -> tuple[bool, str]:
    """Permit analysis only; this is never dispatch authority."""
    config, reason = load_config()
    if config is None:
        return False, reason
    try:
        dd = float(drawdown)
        n = int(open_positions)
    except (TypeError, ValueError):
        return False, "invalid_scan_state"
    if not math.isfinite(dd) or dd < 0:
        return False, "invalid_drawdown"
    if n != 0:
        return False, "recovery_requires_flat_local_account"
    if dd < float(cfg.MAX_DRAWDOWN):
        return False, "normal_gate_available"
    if dd >= config.max_drawdown:
        return False, "recovery_ceiling_reached"
    return True, "scan_only"


def current_context() -> RecoveryContext | None:
    return _CTX.get()


def bind_context(context: RecoveryContext):
    return _CTX.set(context)


def reset_context(token) -> None:
    _CTX.reset(token)


def context_allows(drawdown: float, *, open_positions: int = 0) -> bool:
    context = current_context()
    if context is None or _truthy(BROAD_OVERRIDE_ENV):
        return False
    try:
        dd = float(drawdown)
        n = int(open_positions)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(dd) or n != 0:
        return False
    if time.monotonic() - context.validated_mono > CONTEXT_MAX_AGE_S:
        return False
    if time.time() >= context.expires_at_ts:
        return False
    return float(cfg.MAX_DRAWDOWN) <= dd < context.max_drawdown


def effective_risk_pct(normal_risk_pct: float) -> float:
    """Candidate-scoped recovery risk; never increases the normal risk budget."""
    normal = float(normal_risk_pct)
    context = current_context()
    if context is None:
        return normal
    recovery = float(context.max_risk_pct)
    if not all(math.isfinite(v) and v > 0 for v in (normal, recovery)):
        raise ValueError("invalid recovery sizing context")
    if recovery >= normal:
        raise ValueError("recovery risk is not lower than normal risk")
    return recovery


def _parse_state(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("drawdown recovery state malformed") from exc
    if not isinstance(state, dict) or int(state.get("version", 0)) != STATE_VERSION:
        raise db.PersistenceError("drawdown recovery state malformed")
    if not str(state.get("episode_id") or ""):
        raise db.PersistenceError("drawdown recovery state missing episode")
    if str(state.get("status") or "") not in {
        "ARMED", "IN_TRADE", "DISARMED"
    }:
        raise db.PersistenceError("drawdown recovery state status invalid")
    return state


def _dump(state: dict) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


async def _save_state(state: dict, *, expected_raw: str | None) -> str:
    raw = _dump(state)
    ok = await save_key_values_atomic_cas(
        [(STATE_KEY, raw)],
        expected={STATE_KEY: expected_raw},
        strict=True,
    )
    if not ok:
        raise db.PersistenceError("drawdown recovery state write unconfirmed")
    return raw


def _base_state(
    config: RecoveryConfig,
    *,
    equity: float,
    hwm: float,
    drawdown: float,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": STATE_VERSION,
        "episode_id": config.episode_id,
        "status": "ARMED",
        "armed_at": now,
        "expires_at": config.expires_at.isoformat(),
        "arm_equity": float(equity),
        "arm_hwm": float(hwm),
        "arm_drawdown": float(drawdown),
        "worst_drawdown": float(drawdown),
        "recovery_max_drawdown": float(config.max_drawdown),
        "recovery_max_risk_pct": float(config.max_risk_pct),
        "entry_count": 0,
        "entry_symbol": None,
        "entry_client_oids": [],
        "realized_net_pnl": None,
        "pnl_authority": False,
        "disarm_reason": None,
        "updated_at": now,
    }


async def _disarm(state: dict, raw: str, reason: str, *, drawdown: float) -> dict:
    updated = dict(state)
    updated["status"] = "DISARMED"
    updated["disarm_reason"] = str(reason)
    updated["worst_drawdown"] = max(
        float(updated.get("worst_drawdown", 0.0) or 0.0), float(drawdown)
    )
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _save_state(updated, expected_raw=raw)
    log.critical(
        "[DRAWDOWN_RECOVERY] episode=%s event=DISARM reason=%s "
        "drawdown=%.4f%% execution_effect=BLOCK_NEW_ENTRIES",
        updated["episode_id"], reason, float(drawdown) * 100.0,
    )
    return updated


async def _ledger_clear() -> bool:
    raw = await db.load_key_value(cash_flow_ledger.LEDGER_KEY, strict=True)
    if raw is None:
        return False
    doc = cash_flow_ledger._parse_ledger(raw)
    return len(doc.get("pending", [])) == 0


async def _ownership_valid(engine) -> bool:
    if not bool(getattr(engine, "_execution_ownership_valid", False)):
        return False
    client = getattr(engine, "client", None)
    raw_client = getattr(client, "_client", client)
    ownership = getattr(raw_client, "_execution_ownership", None)
    if ownership is None:
        return False
    try:
        await execution_ownership.validate_execution_ownership(ownership)
    except Exception:
        return False
    return True


def _private_stream_valid(engine) -> bool:
    health = getattr(getattr(engine, "client", None), "private_stream_health", None)
    if not isinstance(health, PrivateStreamHealth):
        return False
    ok, _reason = health.check()
    return bool(ok)


def _current_order_ids(engine) -> set[str]:
    try:
        return {
            str(record.get("client_oid"))
            for record in engine.orders.snapshot()
            if record.get("client_oid")
        }
    except Exception:
        return set()


async def authorize_candidate(
    engine,
    *,
    symbol: str,
    equity: float,
    hwm: float,
    drawdown: float,
) -> RecoveryContext | None:
    """Create/validate one candidate-scoped recovery authorization."""
    config, reason = load_config()
    if config is None:
        log.warning(
            "[DRAWDOWN_RECOVERY] event=AUTHORIZE result=BLOCK reason=%s "
            "execution_effect=BLOCK_NEW_ENTRY",
            reason,
        )
        return None

    values = (float(equity), float(hwm), float(drawdown))
    if any(not math.isfinite(v) for v in values) or equity <= 0 or hwm <= 0:
        return None
    if drawdown < float(cfg.MAX_DRAWDOWN):
        return None
    if drawdown >= config.max_drawdown:
        return None
    if len(getattr(engine, "positions", {})) != 0:
        return None
    if getattr(engine, "_external_performance_quarantine", False):
        return None
    if not durable_execution.can_open(engine):
        return None
    if getattr(engine, "orders", None) is None or engine.orders.pending_orders():
        return None
    if not bool(getattr(engine, "_pilot_live_prelive_ready", False)):
        return None

    client = getattr(engine, "client", None)
    if not bool(getattr(client, "_prelive_account_exposure_verified", False)):
        return None
    if not bool(getattr(client, "_prelive_account_exposure_clear", False)):
        return None
    if not await _ownership_valid(engine):
        return None
    if not _private_stream_valid(engine):
        return None
    if not await _ledger_clear():
        return None

    raw = await db.load_key_value(STATE_KEY, strict=True)
    state = _parse_state(raw)
    if state is not None:
        old_episode = str(state["episode_id"])
        status = str(state["status"])
        if old_episode == config.episode_id:
            if status != "ARMED":
                return None
            if (
                not math.isclose(
                    float(state.get("recovery_max_drawdown")),
                    config.max_drawdown,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    float(state.get("recovery_max_risk_pct")),
                    config.max_risk_pct,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or str(state.get("expires_at")) != config.expires_at.isoformat()
            ):
                raise db.PersistenceError("drawdown recovery episode config drift")
            arm_dd = float(state.get("arm_drawdown"))
            if drawdown > arm_dd + _EPS:
                await _disarm(state, raw, "drawdown_worsened_before_entry", drawdown=drawdown)
                return None
        else:
            if status != "DISARMED":
                return None
            state = _base_state(
                config, equity=equity, hwm=hwm, drawdown=drawdown
            )
            raw = await _save_state(state, expected_raw=raw)
    else:
        state = _base_state(config, equity=equity, hwm=hwm, drawdown=drawdown)
        raw = await _save_state(state, expected_raw=None)

    context = RecoveryContext(
        episode_id=config.episode_id,
        symbol=str(symbol),
        max_drawdown=config.max_drawdown,
        max_risk_pct=config.max_risk_pct,
        arm_drawdown=float(state["arm_drawdown"]),
        validated_drawdown=float(drawdown),
        expires_at_ts=config.expires_at.timestamp(),
        validated_mono=time.monotonic(),
    )
    log.critical(
        "[DRAWDOWN_RECOVERY] episode=%s event=AUTHORIZE result=PASS symbol=%s "
        "drawdown=%.4f%% normal_limit=%.4f%% recovery_ceiling=%.4f%% "
        "recovery_risk_pct=%.4f%% one_position_only=true execution_effect=ALLOW_CANDIDATE_ONLY",
        context.episode_id,
        context.symbol,
        drawdown * 100.0,
        float(cfg.MAX_DRAWDOWN) * 100.0,
        config.max_drawdown * 100.0,
        config.max_risk_pct * 100.0,
    )
    return context


async def mark_entry_if_created(
    engine,
    context: RecoveryContext,
    *,
    before_order_ids: set[str],
) -> bool:
    """Consume the one-entry episode only after a new INCREASE intent/position exists."""
    after = getattr(engine, "orders", None)
    records = after.snapshot() if after is not None else []
    new_increase = [
        record for record in records
        if str(record.get("client_oid") or "") not in before_order_ids
        and str(record.get("symbol") or "") == context.symbol
        and str(record.get("exposure_intent") or "INCREASE").upper() == "INCREASE"
    ]
    position_exists = context.symbol in getattr(engine, "positions", {})
    if not new_increase and not position_exists:
        return False

    raw = await db.load_key_value(STATE_KEY, strict=True)
    state = _parse_state(raw)
    if (
        state is None
        or state.get("episode_id") != context.episode_id
        or state.get("status") != "ARMED"
        or int(state.get("entry_count", 0) or 0) != 0
    ):
        raise db.PersistenceError("drawdown recovery episode not consumable")

    updated = dict(state)
    updated["status"] = "IN_TRADE"
    updated["entry_count"] = 1
    updated["entry_symbol"] = context.symbol
    updated["entry_client_oids"] = [
        str(record.get("client_oid")) for record in new_increase
        if record.get("client_oid")
    ]
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    await _save_state(updated, expected_raw=raw)
    log.critical(
        "[DRAWDOWN_RECOVERY] episode=%s event=ENTRY_CONSUMED symbol=%s "
        "entry_count=1 max_entries=1 execution_effect=BLOCK_ADDITIONAL_RECOVERY_ENTRIES",
        context.episode_id, context.symbol,
    )
    return True


async def reconcile_episode(engine, *, equity: float, hwm: float, drawdown: float) -> None:
    """Fail-closed lifecycle reconciliation. Never closes/manages positions."""
    raw = await db.load_key_value(STATE_KEY, strict=True)
    state = _parse_state(raw)
    if state is None or state["status"] == "DISARMED":
        return

    config, config_reason = load_config()
    if config is None or config.episode_id != state["episode_id"]:
        await _disarm(state, raw, f"operator_{config_reason}", drawdown=drawdown)
        return

    if drawdown < float(cfg.MAX_DRAWDOWN):
        await _disarm(state, raw, "normal_gate_recovered", drawdown=drawdown)
        return
    if drawdown >= float(state["recovery_max_drawdown"]):
        await _disarm(state, raw, "recovery_ceiling_reached", drawdown=drawdown)
        return
    if datetime.now(timezone.utc) >= config.expires_at:
        await _disarm(state, raw, "expired", drawdown=drawdown)
        return

    worst = float(state.get("worst_drawdown", state.get("arm_drawdown", drawdown)))
    if drawdown > worst + _EPS:
        updated = dict(state)
        updated["worst_drawdown"] = float(drawdown)
        updated["updated_at"] = datetime.now(timezone.utc).isoformat()
        raw = await _save_state(updated, expected_raw=raw)
        state = updated

    if state["status"] == "ARMED" and drawdown > float(state["arm_drawdown"]) + _EPS:
        await _disarm(state, raw, "drawdown_worsened_before_entry", drawdown=drawdown)
        return

    if state["status"] == "IN_TRADE" and not getattr(engine, "positions", {}):
        entry_ids = {
            str(value) for value in state.get("entry_client_oids", []) if value
        }
        pending_ids = {
            str(getattr(order, "client_oid", "") or "")
            for order in getattr(engine, "orders", object()).pending_orders()
        } if getattr(engine, "orders", None) is not None else set()
        if not (entry_ids & pending_ids):
            await _disarm(
                state, raw, "recovery_trade_completed_reauth_required", drawdown=drawdown
            )


def snapshot_order_ids(engine) -> set[str]:
    return _current_order_ids(engine)
