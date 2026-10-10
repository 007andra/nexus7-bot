"""Operational risk epoch: a new, separately measured drawdown period.

The historical (lifetime, cash-flow-adjusted) drawdown is NOT replaced, reset,
rebased or relabelled by this module. It keeps its own HWM, its own limit
(``MAX_DRAWDOWN``) and remains the authoritative account-wide hard gate.

A risk epoch adds a second, independent measurement:

* a verifiable starting reference captured once, from freshly authenticated
  account equity, while the account is flat, with no pending external cash
  flow, bound to the HWM namespace, the external cash-flow ledger fingerprint
  and the deployed code SHA; the immutable baseline carries a SHA-256 digest
  that detects accidental drift or corruption (it is not authentication
  against a malicious writer with database access);
* an epoch peak that only rises with observed authenticated equity;
* ``epoch_drawdown = (epoch_peak - equity) / epoch_peak`` measured against its
  own immutable limit (``RISK_EPOCH_MAX_DRAWDOWN``, default and hard cap 30%);
* a sticky, durable BREACHED state.

Authority is TIGHTEN_ONLY: an epoch can block new entries, it can never allow
one. A disabled epoch has no effect on the normal runtime. The one-shot
controlled LIVE re-entry additionally requires an ACTIVE epoch with enough
headroom that its absolute loss budget cannot breach the epoch floor.

Nothing here writes the historical HWM, its provenance, the cash-flow ledger,
PnL rows or any exchange state. No order is placed, cancelled or authorized.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone

ENABLED_ENV = "RISK_EPOCH_ENABLED"
EPOCH_ID_ENV = "RISK_EPOCH_ID"
MAX_DRAWDOWN_ENV = "RISK_EPOCH_MAX_DRAWDOWN"
SUPERSEDE_ACK_ENV = "RISK_EPOCH_SUPERSEDE_ACK"

HARD_MAX_EPOCH_DRAWDOWN = 0.30
DEFAULT_EPOCH_DRAWDOWN = 0.30
VERSION = 1
KEY_PREFIX = "risk_epoch:v1:"
_EPOCH_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{5,63}$")
_EMIT_EVERY_S = 300.0
_LAST_EMIT: dict = {}

AUTHORITY = {
    "authority": "TIGHTEN_ONLY",
    "historical_gate_unchanged": True,
    "historical_hwm_written": False,
    "live_authorization": "NONE",
}

# Immutable baseline fields covered by the digest. Mutable observation fields
# (epoch peak, last equity, breach) are deliberately excluded.
_IMMUTABLE_FIELDS = (
    "version",
    "epoch_id",
    "namespace",
    "started_at",
    "started_epoch_s",
    "start_equity",
    "start_available",
    "epoch_drawdown_limit",
    "historical_peak_equity_at_start",
    "historical_drawdown_at_start",
    "historical_drawdown_limit_at_start",
    "cash_flow_fingerprint",
    "cash_flow_applied_records",
    "cash_flow_applied_net",
    "code_sha",
    "baseline_source",
    "previous_epoch_id",
    "previous_epoch_status",
    "previous_epoch_drawdown",
)

# Statuses. Only ACTIVE permits anything, and only "not blocked by the epoch".
DISABLED = "DISABLED"
CONFIG_INVALID = "CONFIG_INVALID"
PENDING_BASELINE = "PENDING_BASELINE"
ACTIVE = "ACTIVE"
BREACHED = "BREACHED"
INVALID = "INVALID"
FLOW_CHANGED = "FLOW_CHANGED"
UNKNOWN = "UNKNOWN"


class EpochConfig:
    __slots__ = ("enabled", "epoch_id", "limit", "reason")

    def __init__(self, enabled: bool, epoch_id: str, limit: float | None, reason: str):
        self.enabled = enabled
        self.epoch_id = epoch_id
        self.limit = limit
        self.reason = reason

    @property
    def valid(self) -> bool:
        return self.enabled and self.reason == "configured"


def config_from_env() -> EpochConfig:
    if os.environ.get(ENABLED_ENV, "").strip().lower() != "true":
        return EpochConfig(False, "", None, "disabled")
    epoch_id = os.environ.get(EPOCH_ID_ENV, "").strip()
    if not _EPOCH_ID_RE.match(epoch_id):
        return EpochConfig(True, epoch_id, None, "invalid_epoch_id")
    raw = os.environ.get(MAX_DRAWDOWN_ENV, "").strip()
    try:
        limit = DEFAULT_EPOCH_DRAWDOWN if not raw else float(raw)
    except ValueError:
        return EpochConfig(True, epoch_id, None, "invalid_epoch_limit")
    # Percent form (e.g. "30") is accepted like cfg._pct, then hard-capped.
    if math.isfinite(limit) and 1.0 < limit <= 100.0:
        limit = limit / 100.0
    if not math.isfinite(limit) or not 0.0 < limit <= HARD_MAX_EPOCH_DRAWDOWN:
        return EpochConfig(True, epoch_id, None, "invalid_epoch_limit")
    return EpochConfig(True, epoch_id, limit, "configured")


# ─── pure helpers ─────────────────────────────────────────────────────────────

def _positive_finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return out


def epoch_drawdown(epoch_peak: float, equity: float) -> float:
    peak = _positive_finite(epoch_peak, "epoch peak")
    equity = float(equity)
    if not math.isfinite(equity) or equity < 0:
        raise ValueError("equity must be finite and non-negative")
    return max(0.0, (peak - equity) / peak)


def floor_equity(epoch_peak: float, limit: float) -> float:
    return _positive_finite(epoch_peak, "epoch peak") * (1.0 - float(limit))


def baseline_digest(record: dict) -> str:
    material = {name: record.get(name) for name in _IMMUTABLE_FIELDS}
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _num(value) -> str | None:
    if value is None:
        return None
    return format(float(value), ".17g")


def cash_flow_fingerprint(doc: dict) -> str:
    """Canonical identity AND amounts of the external-flow ledger.

    Covers every applied reconciliation (id, flow identities, signed net,
    pre/post equity, adjusted HWM) and every pending flow, order-independent.
    Changing an amount while keeping its identity changes the fingerprint.
    This detects drift/corruption; it is not authentication against a
    malicious writer with database access.
    """
    doc = doc or {}
    applied = sorted(
        (
            str(r.get("reconciliation_id")),
            sorted(str(i) for i in r.get("identities", [])),
            _num(r.get("net_amount")),
            _num(r.get("pre_flow_equity")),
            _num(r.get("post_flow_equity")),
            _num(r.get("adjusted_hwm")),
        )
        for r in doc.get("applied", [])
    )
    pending = sorted(
        (str(p.get("identity")), _num(p.get("amount")))
        for p in doc.get("pending", [])
    )
    raw = json.dumps({"applied": applied, "pending": pending}, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _namespace() -> str:
    from bot import hwm_namespace
    return hwm_namespace.hwm_namespace()


def epoch_key(epoch_id: str, namespace: str | None = None) -> str:
    return f"{KEY_PREFIX}{namespace or _namespace()}:epoch:{epoch_id}"


def index_key(namespace: str | None = None) -> str:
    return f"{KEY_PREFIX}{namespace or _namespace()}:index"


def _code_sha() -> str:
    for name in ("RAILWAY_GIT_COMMIT_SHA", "SOURCE_COMMIT", "GIT_COMMIT_SHA"):
        value = os.environ.get(name, "").strip()
        if value:
            return value[:40]
    return "UNKNOWN"


def _historical(engine) -> tuple[float, float]:
    """Read (historical_drawdown, historical_peak). Read-only; NaN if unreadable."""
    risk = getattr(engine, "risk", None)
    try:
        drawdown = float(getattr(risk, "drawdown", float("nan")))
    except (TypeError, ValueError):
        drawdown = float("nan")
    try:
        peak = float(
            getattr(risk, "peak_equity", 0.0)
            or getattr(risk, "peak_balance", 0.0)
            or 0.0
        )
    except (TypeError, ValueError):
        peak = float("nan")
    return drawdown, peak


def confirmed_capital(engine) -> tuple[float, float, bool]:
    """Freshly authenticated (equity, available); never falls back past a bad snapshot."""
    from bot import controlled_live_reentry_v1 as controlled
    return controlled._capital(engine)


def _pending_orders(engine) -> int:
    orders = getattr(engine, "orders", None)
    reader = getattr(orders, "pending_orders", None)
    if not callable(reader):
        return 0
    try:
        return len(list(reader() or []))
    except Exception:
        return 1


def _loads(raw: str) -> dict:
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("epoch record must be an object")
    return doc


def _dump(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _verify_record(record: dict, config: EpochConfig, namespace: str) -> str | None:
    """Return an INVALID reason, or None when the stored baseline is intact."""
    if record.get("version") != VERSION:
        return "version_mismatch"
    if record.get("epoch_id") != config.epoch_id:
        return "epoch_id_mismatch"
    if record.get("namespace") != namespace:
        return "namespace_mismatch"
    if record.get("baseline_digest") != baseline_digest(record):
        return "baseline_digest_mismatch"
    try:
        start = _positive_finite(record.get("start_equity"), "start equity")
        peak = _positive_finite(record.get("epoch_peak_equity"), "epoch peak")
        limit = float(record.get("epoch_drawdown_limit"))
    except (TypeError, ValueError):
        return "baseline_values_invalid"
    if peak + 1e-12 < start:
        return "epoch_peak_below_start"
    # The limit is part of the immutable baseline. Changing the env var can
    # never loosen (or silently re-scope) an existing epoch.
    if not math.isfinite(limit) or abs(limit - float(config.limit)) > 1e-12:
        return "epoch_limit_changed"
    return None


def _state(config: EpochConfig, status: str, reason: str, *, record: dict | None = None,
           equity: float | None = None, engine=None) -> dict:
    hist_dd, hist_peak = _historical(engine) if engine is not None else (float("nan"), float("nan"))
    try:
        from bot.config import cfg
        hist_limit = float(cfg.MAX_DRAWDOWN)
    except Exception:
        hist_limit = float("nan")
    state = {
        "epoch_id": config.epoch_id or None,
        "status": status,
        "reason": reason,
        "historical_drawdown": hist_dd,
        "historical_drawdown_limit": hist_limit,
        "historical_peak_equity": hist_peak,
        "epoch_drawdown_limit": config.limit,
        "epoch_start_equity": None,
        "epoch_started_at": None,
        "epoch_peak_equity": None,
        "epoch_equity": equity,
        "epoch_drawdown": None,
        "epoch_floor_equity": None,
        "epoch_headroom_usdt": None,
        "epoch_breached_at": None,
        "epoch_max_drawdown": None,
        "baseline_digest": None,
        "observed_monotonic": time.monotonic(),
        **AUTHORITY,
    }
    if record:
        peak = float(record.get("epoch_peak_equity") or 0.0)
        state.update({
            "epoch_start_equity": record.get("start_equity"),
            "epoch_started_at": record.get("started_at"),
            "epoch_peak_equity": peak or None,
            "epoch_breached_at": record.get("breached_at"),
            "epoch_max_drawdown": record.get("max_epoch_drawdown"),
            "baseline_digest": record.get("baseline_digest"),
        })
        if equity is not None and peak > 0 and config.limit is not None:
            dd = epoch_drawdown(peak, equity)
            floor = floor_equity(peak, config.limit)
            state.update({
                "epoch_drawdown": dd,
                "epoch_floor_equity": floor,
                "epoch_headroom_usdt": equity - floor,
            })
        elif record.get("breach_drawdown") is not None:
            state["epoch_drawdown"] = record.get("breach_drawdown")
    return state


# ─── durable lifecycle ────────────────────────────────────────────────────────

async def _ledger(strict: bool = True) -> dict:
    from bot import cash_flow_ledger
    return await cash_flow_ledger.ledger_snapshot(strict=strict)


async def _build_baseline(engine, config: EpochConfig, namespace: str, equity: float,
                          available: float, ledger: dict, previous: dict | None) -> dict:
    from bot.config import cfg
    hist_dd, hist_peak = _historical(engine)
    if not (math.isfinite(hist_dd) and hist_dd >= 0 and math.isfinite(hist_peak) and hist_peak > 0):
        raise ValueError("historical drawdown/HWM unreadable")
    totals = ledger["totals"]
    now = datetime.now(timezone.utc)
    record = {
        "version": VERSION,
        "epoch_id": config.epoch_id,
        "namespace": namespace,
        "started_at": now.isoformat(),
        "started_epoch_s": round(now.timestamp(), 3),
        "start_equity": float(equity),
        "start_available": float(available),
        "epoch_drawdown_limit": float(config.limit),
        "historical_peak_equity_at_start": float(hist_peak),
        "historical_drawdown_at_start": float(hist_dd),
        "historical_drawdown_limit_at_start": float(cfg.MAX_DRAWDOWN),
        "cash_flow_fingerprint": cash_flow_fingerprint(ledger["ledger"]),
        "cash_flow_applied_records": int(totals["applied_records"]),
        "cash_flow_applied_net": float(totals["applied_net"]),
        "code_sha": _code_sha(),
        "baseline_source": "AUTHENTICATED_ACCOUNT_EQUITY_FLAT_NO_PENDING_FLOWS",
        "previous_epoch_id": None if previous is None else previous.get("epoch_id"),
        "previous_epoch_status": None if previous is None else previous.get("status"),
        "previous_epoch_drawdown": None if previous is None else previous.get("drawdown"),
    }
    record["baseline_digest"] = baseline_digest(record)
    record.update({
        "epoch_peak_equity": float(equity),
        "last_equity": float(equity),
        "last_observed_at": now.isoformat(),
        "observations": 1,
        "max_epoch_drawdown": 0.0,
        "min_equity": float(equity),
        "breached": False,
        "breached_at": None,
        "breach_equity": None,
        "breach_drawdown": None,
        "closed": False,
        "closed_at": None,
        "closed_by_epoch_id": None,
    })
    return record


async def _previous_epoch(index_raw: str | None, namespace: str, config: EpochConfig):
    """Prepare the formal closure of the latest prior epoch.

    Any succession, ACTIVE or BREACHED, requires an explicit operator ack
    naming the exact previous epoch id. Without it no successor is created,
    so changing RISK_EPOCH_ID alone can never reset the period meter. The
    closure (final/max drawdown, closing successor) is written atomically
    with the successor; the previous baseline is never modified.
    Returns (ids, summary, block_reason, prev_key, prev_raw, closed_prev).
    """
    from bot import database as db
    ids = [] if index_raw is None else list(_loads(index_raw).get("epochs", []))
    if not ids:
        return ids, None, None, None, None, None
    prev_id = ids[-1]
    prev_key = epoch_key(prev_id, namespace)
    raw = await db._load_key_value_raw(prev_key, strict=True)
    if raw is None:
        return ids, None, "previous_epoch_record_missing", None, None, None
    prev = _loads(raw)
    if prev.get("baseline_digest") != baseline_digest(prev):
        return ids, None, "previous_epoch_digest_mismatch", None, None, None
    if prev.get("closed"):
        return ids, None, "previous_epoch_already_closed", None, None, None
    status = BREACHED if prev.get("breached") else "CLOSED_BY_SUCCESSOR"
    max_dd = prev.get("breach_drawdown") if prev.get("breached") else prev.get("max_epoch_drawdown")
    summary = {"epoch_id": prev_id, "status": status, "drawdown": max_dd}
    if os.environ.get(SUPERSEDE_ACK_ENV, "").strip() != prev_id:
        return ids, summary, "previous_epoch_supersede_ack_missing", None, None, None
    closed = dict(prev)
    closed.update({
        "closed": True,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "closed_by_epoch_id": config.epoch_id,
        "closing_status": status,
    })
    return ids, summary, None, prev_key, raw, closed


async def observe(engine, *, strict: bool = True) -> dict:
    """Refresh the epoch from the current authenticated equity.

    Creates the immutable baseline exactly once when every baseline
    precondition holds; otherwise only raises the epoch peak, records a
    sticky breach, or reports a fail-closed status. Never raises on
    domain problems: the returned state carries the blocking status.
    """
    from bot import database as db
    from bot.atomic_key_value import CompareAndSwapConflict, save_key_values_atomic_cas

    config = config_from_env()
    if not config.enabled:
        state = _state(config, DISABLED, "disabled", engine=engine)
        _store(engine, state)
        return state
    if not config.valid:
        state = _state(config, CONFIG_INVALID, config.reason, engine=engine)
        _store(engine, state)
        return state

    equity, available, confirmed = confirmed_capital(engine)
    try:
        namespace = _namespace()
        key = epoch_key(config.epoch_id, namespace)
        raw = await db._load_key_value_raw(key, strict=True)
        ledger = await _ledger(strict=True)
    except Exception as exc:
        state = _state(config, UNKNOWN, f"persistence_{type(exc).__name__}", engine=engine,
                       equity=equity if confirmed else None)
        _store(engine, state)
        return state

    if raw is None:
        state = await _try_create(engine, config, namespace, key, equity, available,
                                  confirmed, ledger)
        _store(engine, state)
        return state

    try:
        record = _loads(raw)
    except (TypeError, ValueError):
        state = _state(config, INVALID, "record_malformed", engine=engine)
        _store(engine, state)
        return state
    invalid = _verify_record(record, config, namespace)
    if invalid:
        state = _state(config, INVALID, invalid, record=record, engine=engine)
        _store(engine, state)
        return state
    if record.get("closed"):
        state = _state(config, INVALID, "epoch_closed_by_successor", record=record, engine=engine)
        _store(engine, state)
        return state
    totals = ledger["totals"]
    if (
        cash_flow_fingerprint(ledger["ledger"]) != record["cash_flow_fingerprint"]
        or int(totals["pending_flows"]) != 0
        or int(totals["applied_records"]) != int(record["cash_flow_applied_records"])
        or abs(float(totals["applied_net"]) - float(record["cash_flow_applied_net"])) > 1e-9
    ):
        # A deposit could mask an epoch loss and a withdrawal could fake one.
        # Either way the epoch reference no longer measures trading alone.
        state = _state(config, FLOW_CHANGED, "external_cash_flow_since_baseline",
                       record=record, engine=engine, equity=equity if confirmed else None)
        _store(engine, state)
        return state
    if record.get("breached"):
        state = _state(config, BREACHED, "sticky_breach", record=record, engine=engine,
                       equity=equity if confirmed else None)
        _store(engine, state)
        return state
    if not confirmed:
        state = _state(config, UNKNOWN, "capital_unconfirmed", record=record, engine=engine)
        _store(engine, state)
        return state

    updated = dict(record)
    peak = max(float(record["epoch_peak_equity"]), float(equity))
    dd = epoch_drawdown(peak, equity)
    now = datetime.now(timezone.utc).isoformat()
    max_dd = max(float(record.get("max_epoch_drawdown") or 0.0), dd)
    min_equity = min(float(record.get("min_equity", equity)), float(equity))
    updated.update({
        "epoch_peak_equity": peak,
        "last_equity": float(equity),
        "last_observed_at": now,
        "observations": int(record.get("observations", 0)) + 1,
        "max_epoch_drawdown": max_dd,
        "min_equity": min_equity,
    })
    if dd >= float(config.limit):
        updated.update({
            "breached": True,
            "breached_at": now,
            "breach_equity": float(equity),
            "breach_drawdown": dd,
        })
    # Durable checkpoint of every equity change, so a sub-limit loss is
    # never only in memory (restart and successor summaries read it).
    changed = (
        updated["epoch_peak_equity"] != record["epoch_peak_equity"]
        or updated["breached"] != record.get("breached")
        or updated["max_epoch_drawdown"] != record.get("max_epoch_drawdown")
        or abs(float(updated["last_equity"]) - float(record.get("last_equity", equity))) > 1e-12
    )
    if changed:
        try:
            await save_key_values_atomic_cas([(key, _dump(updated))], expected={key: raw},
                                             strict=strict)
        except CompareAndSwapConflict:
            state = _state(config, UNKNOWN, "concurrent_epoch_update", record=record,
                           engine=engine, equity=equity)
            _store(engine, state)
            return state
        except Exception as exc:
            state = _state(config, UNKNOWN, f"persistence_{type(exc).__name__}",
                           record=record, engine=engine, equity=equity)
            _store(engine, state)
            return state
    status = BREACHED if updated["breached"] else ACTIVE
    state = _state(config, status, "observed", record=updated, engine=engine, equity=equity)
    _store(engine, state)
    return state


async def _try_create(engine, config, namespace, key, equity, available, confirmed, ledger) -> dict:
    from bot import database as db
    from bot.atomic_key_value import CompareAndSwapConflict, save_key_values_atomic_cas

    blockers = []
    if getattr(engine, "paper_trade", True):
        blockers.append("paper_mode")
    if not confirmed:
        blockers.append("capital_unconfirmed")
    if len(getattr(engine, "positions", {}) or {}) != 0:
        blockers.append("account_not_flat")
    if _pending_orders(engine) != 0:
        blockers.append("pending_orders")
    if int(ledger["totals"]["pending_flows"]) != 0:
        blockers.append("pending_external_flows")
    if ledger.get("durable_hwm") is None:
        blockers.append("historical_hwm_missing")
    hist_dd, hist_peak = _historical(engine)
    if not (math.isfinite(hist_dd) and hist_dd >= 0 and math.isfinite(hist_peak) and hist_peak > 0):
        blockers.append("historical_drawdown_unreadable")
    if blockers:
        return _state(config, PENDING_BASELINE, ",".join(blockers), engine=engine,
                      equity=equity if confirmed else None)

    ikey = index_key(namespace)
    try:
        index_raw = await db._load_key_value_raw(ikey, strict=True)
        ids, previous, prev_block, prev_key, prev_raw, closed_prev = await _previous_epoch(
            index_raw, namespace, config
        )
    except Exception as exc:
        return _state(config, UNKNOWN, f"index_{type(exc).__name__}", engine=engine)
    if config.epoch_id in ids:
        return _state(config, INVALID, "epoch_id_reused", engine=engine)
    if prev_block:
        return _state(config, PENDING_BASELINE, prev_block, engine=engine, equity=equity)

    record = await _build_baseline(engine, config, namespace, equity, available, ledger, previous)
    index_doc = {"version": VERSION, "epochs": ids + [config.epoch_id]}
    items = [(key, _dump(record)), (ikey, _dump(index_doc))]
    expected = {key: None, ikey: index_raw}
    if closed_prev is not None:
        items.append((prev_key, _dump(closed_prev)))
        expected[prev_key] = prev_raw
    try:
        await save_key_values_atomic_cas(items, expected=expected, strict=True)
    except CompareAndSwapConflict:
        return _state(config, UNKNOWN, "concurrent_epoch_create", engine=engine)
    except Exception as exc:
        return _state(config, UNKNOWN, f"persistence_{type(exc).__name__}", engine=engine)
    return _state(config, ACTIVE, "baseline_created", record=record, engine=engine,
                  equity=equity)


def _store(engine, state: dict) -> None:
    try:
        engine._risk_epoch_state = state
    except Exception:
        pass


def cached_state(engine) -> dict | None:
    state = getattr(engine, "_risk_epoch_state", None)
    return state if isinstance(state, dict) else None


# ─── gates (TIGHTEN_ONLY) ─────────────────────────────────────────────────────

def blocks_new_entries(state: dict | None) -> tuple[bool, str]:
    """True when an enabled epoch must block. A disabled epoch never blocks."""
    config = config_from_env()
    if not config.enabled:
        return False, "epoch_disabled"
    if state is None:
        return True, "epoch_state_unavailable"
    if state.get("epoch_id") != config.epoch_id:
        return True, "epoch_state_stale_id"
    if state.get("status") != ACTIVE:
        return True, f"epoch_{str(state.get('status')).lower()}:{state.get('reason')}"
    return False, "epoch_active"


async def predispatch_allows(engine, log) -> bool:
    """Fresh epoch check immediately before dispatch; can only block."""
    if not config_from_env().enabled:
        return True
    state = await observe(engine, strict=True)
    blocked, reason = blocks_new_entries(state)
    emit(engine, log, state=state, force=True)
    if blocked:
        log.error(
            "[RISK_EPOCH_PREDISPATCH] result=BLOCK reason=%s epoch_id=%s "
            "historical_drawdown=%s epoch_drawdown=%s epoch_drawdown_limit=%s "
            "authority=TIGHTEN_ONLY execution_effect=BLOCK_NEW_ENTRY",
            reason, state.get("epoch_id"), _fmt(state.get("historical_drawdown")),
            _fmt(state.get("epoch_drawdown")), _fmt(state.get("epoch_drawdown_limit")),
        )
        return False
    return True


def controlled_reentry_requirement(engine, *, equity: float, loss_budget: float) -> tuple[bool, str, dict]:
    """One-shot LIVE requirement: ACTIVE epoch whose floor survives the full budget."""
    config = config_from_env()
    if not config.enabled:
        return False, "risk_epoch_disabled", {}
    state = cached_state(engine)
    blocked, reason = blocks_new_entries(state)
    if blocked:
        return False, f"risk_epoch_{reason}", {}
    try:
        peak = max(float(state["epoch_peak_equity"]), float(equity))
        floor = floor_equity(peak, float(config.limit))
        worst = float(equity) - float(loss_budget)
    except (KeyError, TypeError, ValueError):
        return False, "risk_epoch_values_unreadable", {}
    evidence = {
        "epoch_id": config.epoch_id,
        "epoch_floor_equity": floor,
        "epoch_worst_case_equity": worst,
        "epoch_drawdown_limit": float(config.limit),
    }
    if not math.isfinite(worst) or worst < floor:
        return False, "risk_epoch_headroom_insufficient", evidence
    return True, "risk_epoch_headroom_ok", evidence


def startup_log() -> str:
    config = config_from_env()
    return (
        "[RISK_EPOCH_V1] stage=STARTUP "
        f"enabled={str(config.enabled).lower()} "
        f"epoch_id={config.epoch_id or 'NA'} "
        f"epoch_drawdown_limit={config.limit if config.limit is not None else 'NA'} "
        f"hard_max_epoch_drawdown={HARD_MAX_EPOCH_DRAWDOWN} "
        f"reason={config.reason} authority=TIGHTEN_ONLY "
        "historical_gate_unchanged=true historical_hwm_written=false "
        "live_authorization=NONE"
    )


# ─── telemetry ────────────────────────────────────────────────────────────────

def _fmt(value):
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "NA"
    if isinstance(value, float):
        return "nan" if math.isnan(value) else f"{value:.12g}"
    return str(value).replace(" ", "_")


_LOG_FIELDS = (
    "epoch_id", "status", "reason",
    "historical_drawdown", "historical_drawdown_limit", "historical_peak_equity",
    "epoch_drawdown", "epoch_drawdown_limit", "epoch_start_equity", "epoch_started_at",
    "epoch_peak_equity", "epoch_equity", "epoch_floor_equity", "epoch_headroom_usdt",
    "epoch_breached_at", "epoch_max_drawdown", "baseline_digest",
    "authority", "historical_gate_unchanged", "historical_hwm_written", "live_authorization",
)


def telemetry(state: dict | None) -> dict:
    """Public, explicitly named fields; never calls the epoch drawdown 'historical'."""
    state = state or {}
    return {name: state.get(name) for name in _LOG_FIELDS}


def emit(engine, log, *, state: dict | None = None, force: bool = False) -> dict | None:
    state = state if state is not None else cached_state(engine)
    if state is None:
        return None
    signature = (
        state.get("epoch_id"), state.get("status"), state.get("reason"),
        None if state.get("epoch_drawdown") is None else round(float(state["epoch_drawdown"]), 6),
    )
    now = time.monotonic()
    previous = _LAST_EMIT.get(id(engine))
    if not force and previous and previous[0] == signature and now - previous[1] < _EMIT_EVERY_S:
        return state
    _LAST_EMIT[id(engine)] = (signature, now)
    fields = " ".join(f"{k}={_fmt(v)}" for k, v in telemetry(state).items())
    log.warning("[RISK_EPOCH_V1] %s", fields)
    return state


async def observe_and_emit(engine, log) -> dict | None:
    """Best-effort periodic hook. Observability failures surface as UNKNOWN."""
    if not config_from_env().enabled:
        return None
    try:
        state = await observe(engine, strict=True)
    except Exception as exc:  # noqa: BLE001 - reported and fail-closed via UNKNOWN
        state = _state(config_from_env(), UNKNOWN, f"observe_{type(exc).__name__}", engine=engine)
        _store(engine, state)
    return emit(engine, log, state=state)


__all__ = [
    "ACTIVE", "BREACHED", "FLOW_CHANGED", "INVALID", "PENDING_BASELINE", "UNKNOWN",
    "HARD_MAX_EPOCH_DRAWDOWN", "config_from_env", "epoch_drawdown", "floor_equity",
    "baseline_digest", "cash_flow_fingerprint", "epoch_key", "index_key", "observe",
    "blocks_new_entries", "predispatch_allows", "controlled_reentry_requirement",
    "telemetry", "emit", "startup_log", "observe_and_emit", "cached_state",
]
