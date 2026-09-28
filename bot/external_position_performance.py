"""Performance attribution quarantine for unexpected Binance positions.

Account-level equity/collateral remains authoritative for solvency and sizing.
This module only protects the durable trading-performance HWM from PnL that
belongs to an exchange position not owned by BGX.

General rule:
- when a non-BGX exchange position is observed, new performance highs are frozen;
- a durable quarantine marker is written so a restart cannot silently forget it;
- after the external position disappears, entries remain blocked until the
  episode is explicitly reconciled.

One already-proven production incident (ATOMUSDT, 2026-09-27/28) can be
reconciled automatically because the exchange income ledger, pre-episode
equity/HWM, post-episode equity, and ownership evidence are all pinned.
"""
from __future__ import annotations

import json
import math
import time

from bot import database as db
from bot import drawdown_persistence as ddp
from bot import hwm_namespace
from bot.binance_accounting_evidence import (
    _load_registry,
    classify_trade_origin,
    collect_income,
    collect_orders,
    collect_user_trades,
)
from bot.conditional_stop_protection import _instrument_info, _to_base_size
from bot.logger import log as default_log


_ALLOWED_EXTERNAL_PERFORMANCE_TYPES = frozenset(
    {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}
)
_STATE_KEY_PREFIX = "risk:external_position_performance_quarantine:v1"

# Forensically pinned production incident. Values come from Railway runtime
# evidence and Binance income-ledger aggregates; every predicate below must
# match before the repair can execute.
_INCIDENT_SYMBOL = "ATOMUSDT"
_INCIDENT_START_MS = 1790526000000  # 2026-09-27 16:20:00 UTC
_INCIDENT_END_MS = 1790554200000    # 2026-09-28 00:10:00 UTC
_INCIDENT_BAD_HWM = 7.3562
_INCIDENT_PRE_HWM = 6.4680
_INCIDENT_POST_EQUITY = 6.0944
_INCIDENT_NET_EXTERNAL = 0.21172362
_INCIDENT_EXPECTED_INCOME = {
    "COMMISSION": (6, -0.21386754),
    "REALIZED_PNL": (1, 0.45130999),
    "FUNDING_FEE": (1, -0.02571883),
}
_INCIDENT_TOLERANCE = 0.002


def incident_repair_key() -> str:
    return f"risk:external_performance_repair:ATOMUSDT_20260927:v1:{hwm_namespace.hwm_namespace()}"


def _incident_consumed_marker() -> str:
    return json.dumps({
        "version": 1,
        "incident": "ATOMUSDT_20260927",
        "namespace": hwm_namespace.hwm_namespace(),
        "start_ms": _INCIDENT_START_MS,
        "end_ms": _INCIDENT_END_MS,
        "status": "CONSUMED",
    }, sort_keys=True, separators=(",", ":"))


async def _incident_already_consumed() -> bool:
    # Only absence permits a first repair. Unknown versions, partial writes,
    # corrupt values and storage errors must never be interpreted as absence.
    raw = await db.load_key_value(incident_repair_key(), strict=True)
    if raw is None:
        return False
    if raw != _incident_consumed_marker():
        raise db.PersistenceError("external incident repair marker ambiguous")
    return True


def state_key() -> str:
    return f"{_STATE_KEY_PREFIX}:{hwm_namespace.hwm_namespace()}"


def _finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{label} nonfinite")
    return out


def server_now_ms(client) -> int:
    now = getattr(client, "_now_ms", None)
    if callable(now):
        try:
            value = int(now())
            if value > 0:
                return value
        except Exception:
            pass
    return int(time.time() * 1000)


def _active_exchange_symbols(rows) -> set[str]:
    out: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            size = abs(float(row.get("size", 0) or 0))
        except (TypeError, ValueError):
            continue
        symbol = str(row.get("symbol", "") or "").upper()
        if size > 0 and symbol:
            out.add(symbol)
    return out


def _unowned_symbols(engine, rows) -> set[str]:
    """Conservative ownership view for performance-HWM eligibility.

    Explicit EXTERNAL classification always wins. A local symbol is accepted as
    BGX-owned only when exchange side and base quantity remain compatible with
    the owned local position. Legitimate partial exits may reduce quantity;
    increases or reversals are quarantined exactly like the execution guard.
    """
    local_positions = getattr(engine, "positions", {}) or {}
    explicit_external = {
        str(s).upper()
        for s in (getattr(engine, "_external_position_symbols", set()) or set())
    }
    unowned: set[str] = set()

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "") or "").upper()
        try:
            raw_size = abs(float(row.get("size", 0) or 0))
        except (TypeError, ValueError):
            raw_size = 0.0
        if not symbol or raw_size <= 0:
            continue
        if symbol in explicit_external:
            unowned.add(symbol)
            continue

        owned = local_positions.get(symbol)
        if owned is None:
            unowned.add(symbol)
            continue
        try:
            exchange_base = _to_base_size(
                raw_size,
                row.get("sizeUnit", "CONTRACTS"),
                _instrument_info(engine.client, symbol),
            )
            local_qty = abs(float(getattr(owned, "qty", 0) or 0))
            local_direction = str(getattr(owned, "direction", "") or "").upper()
            exchange_side = str(row.get("side", "") or "").upper()
            exchange_direction = {
                "BUY": "LONG",
                "SELL": "SHORT",
            }.get(exchange_side, exchange_side)
            compatible = (
                exchange_base > 0
                and local_qty > 0
                and exchange_direction == local_direction
                and exchange_base
                <= local_qty + max(1e-12, local_qty * 1e-9)
            )
        except (AttributeError, TypeError, ValueError):
            compatible = False
        if not compatible:
            unowned.add(symbol)

    return unowned


async def _read_positions(engine):
    reader = getattr(engine.client, "get_positions", None)
    if not callable(reader):
        raise RuntimeError("external position reader unavailable")
    rows = await reader()
    if not isinstance(rows, list):
        raise RuntimeError("external position read unavailable")
    return rows


async def _load_state(*, strict: bool = True) -> dict | None:
    raw = await db.load_key_value(state_key(), strict=strict)
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or int(doc.get("version", 0)) != 1:
            raise ValueError("invalid quarantine state")
        if doc.get("status") not in {"ACTIVE", "UNRESOLVED"}:
            raise ValueError("invalid quarantine status")
        return doc
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("external performance quarantine state malformed") from exc


async def _capture_quarantine(engine, symbols: set[str], log) -> dict:
    existing = await _load_state(strict=True)
    if existing is not None:
        merged = sorted(set(existing.get("symbols") or []) | set(symbols))
        if merged != sorted(existing.get("symbols") or []):
            existing["symbols"] = merged
            existing["status"] = "UNRESOLVED"
            existing["reason"] = "external_symbol_set_changed"
            if await db.save_key_value(
                state_key(),
                json.dumps(existing, sort_keys=True, separators=(",", ":")),
                strict=True,
            ) is not True:
                raise db.PersistenceError("quarantine state update not confirmed")
        return existing

    previous_equity = getattr(engine, "_pilot_prev_account_equity", None)
    previous_ms = getattr(engine, "_pilot_prev_account_observed_ms", None)
    raw_peak = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, strict=True)
    try:
        pre_equity = _finite(previous_equity, "pre-external equity")
        pre_peak = _finite(raw_peak, "pre-external HWM")
        start_ms = int(previous_ms)
        if pre_equity <= 0 or pre_peak <= 0 or start_ms <= 0:
            raise ValueError("invalid pre-external snapshot")
        status = "ACTIVE"
        reason = "external_position_detected"
    except (TypeError, ValueError):
        pre_equity = None
        pre_peak = None
        start_ms = server_now_ms(engine.client)
        status = "UNRESOLVED"
        reason = "pre_external_snapshot_unavailable"

    doc = {
        "version": 1,
        "status": status,
        "reason": reason,
        "symbols": sorted(symbols),
        "started_at_ms": start_ms,
        "pre_event_equity": pre_equity,
        "pre_event_peak": pre_peak,
        "execution_effect": "BLOCK_NEW_ENTRIES",
    }
    if await db.save_key_value(
        state_key(), json.dumps(doc, sort_keys=True, separators=(",", ":")), strict=True
    ) is not True:
        raise db.PersistenceError("quarantine state write not confirmed")
    log.critical(
        "[EXTERNAL_PERFORMANCE_QUARANTINE] status=%s symbols=%s "
        "pre_event_equity=%s pre_event_peak=%s execution_effect=BLOCK_NEW_ENTRIES",
        status,
        ",".join(sorted(symbols)),
        "N/A" if pre_equity is None else f"{pre_equity:.4f}",
        "N/A" if pre_peak is None else f"{pre_peak:.4f}",
    )
    return doc


def _incident_rows(rows: list[dict]) -> tuple[list[dict], float]:
    selected: list[dict] = []
    by_type = {
        kind: {"count": 0, "income": 0.0}
        for kind in _INCIDENT_EXPECTED_INCOME
    }
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("income row invalid")
        symbol = str(row.get("symbol") or "").upper()
        kind = str(row.get("incomeType") or "").upper()
        if not (_INCIDENT_START_MS <= int(row.get("time", 0) or 0) <= _INCIDENT_END_MS):
            raise ValueError("income row outside incident window")
        if symbol != _INCIDENT_SYMBOL:
            raise ValueError("non-ATOM income inside incident window")
        if kind not in _ALLOWED_EXTERNAL_PERFORMANCE_TYPES:
            raise ValueError("unsupported incident income type")
        amount = _finite(row.get("income", 0), "income")
        selected.append(row)
        by_type[kind]["count"] += 1
        by_type[kind]["income"] += amount

    if not selected:
        raise ValueError("incident income evidence missing")
    for kind, (expected_count, expected_income) in _INCIDENT_EXPECTED_INCOME.items():
        actual = by_type[kind]
        if int(actual["count"]) != int(expected_count):
            raise ValueError(f"incident {kind} count mismatch")
        if not math.isclose(
            float(actual["income"]),
            float(expected_income),
            rel_tol=0.0,
            abs_tol=1e-8,
        ):
            raise ValueError(f"incident {kind} income mismatch")

    return selected, sum(float(row.get("income", 0) or 0) for row in selected)


async def _prove_incident_manual_ownership(engine) -> tuple[int, float, float]:
    """Require independent exchange + durable proof that ATOM fills were manual."""
    registry = await _load_registry()
    for item in registry:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper().removesuffix("M")
        client_oid = str(item.get("client_oid") or "")
        if symbol == _INCIDENT_SYMBOL and client_oid.startswith("bgx7-"):
            raise db.PersistenceError(
                "BGX durable ATOM order conflicts with external incident repair"
            )

    trades = await collect_user_trades(
        engine.client, _INCIDENT_SYMBOL, _INCIDENT_START_MS, _INCIDENT_END_MS
    )
    orders = await collect_orders(
        engine.client, _INCIDENT_SYMBOL, _INCIDENT_START_MS, _INCIDENT_END_MS
    )
    if len(trades) < 2:
        raise db.PersistenceError("external incident user-trade evidence incomplete")

    order_map = {
        str(row.get("orderId")): row
        for row in orders
        if isinstance(row, dict) and row.get("orderId") is not None
    }
    commission = 0.0
    realized = 0.0
    for trade in trades:
        order = order_map.get(str(trade.get("orderId")))
        origin, _reason = classify_trade_origin(trade, order, registry)
        if origin != "MANUAL_EXTERNAL":
            raise db.PersistenceError(
                "external incident ownership is not provably manual"
            )
        asset = str(trade.get("commissionAsset") or "").upper()
        if asset != "USDT":
            raise db.PersistenceError(
                "external incident commission asset is not USDT"
            )
        commission += _finite(trade.get("commission", 0), "trade commission")
        realized += _finite(trade.get("realizedPnl", 0), "trade realizedPnl")

    if not math.isclose(
        commission,
        abs(_INCIDENT_EXPECTED_INCOME["COMMISSION"][1]),
        rel_tol=0.0,
        abs_tol=1e-8,
    ):
        raise db.PersistenceError("external incident trade commission mismatch")
    if not math.isclose(
        realized,
        _INCIDENT_EXPECTED_INCOME["REALIZED_PNL"][1],
        rel_tol=0.0,
        abs_tol=1e-8,
    ):
        raise db.PersistenceError("external incident trade realized PnL mismatch")
    return len(trades), commission, realized


async def maybe_repair_known_atom_incident(engine, account_state: dict, rows, log=None) -> bool:
    """Repair only the exact, fully pinned 2026-09-27 ATOM manual-position episode."""
    log = log or default_log
    if await _incident_already_consumed():
        return False
    if _active_exchange_symbols(rows):
        return False
    if await _load_state(strict=True) is not None:
        return False

    raw_peak = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, strict=True)
    if raw_peak is None:
        return False
    try:
        persisted_peak = float(raw_peak)
        current_equity = _finite(account_state.get("equity"), "account equity")
    except (TypeError, ValueError):
        return False

    if abs(persisted_peak - _INCIDENT_BAD_HWM) > _INCIDENT_TOLERANCE:
        return False
    if abs(current_equity - _INCIDENT_POST_EQUITY) > _INCIDENT_TOLERANCE:
        return False

    trade_count, trade_commission, trade_realized = (
        await _prove_incident_manual_ownership(engine)
    )

    evidence1 = await collect_income(
        engine.client, _INCIDENT_START_MS, _INCIDENT_END_MS
    )
    evidence2 = await collect_income(
        engine.client, _INCIDENT_START_MS, _INCIDENT_END_MS
    )
    if evidence1 != evidence2:
        raise db.PersistenceError("external incident income evidence unstable")

    try:
        selected, net_external = _incident_rows(evidence1)
    except ValueError as exc:
        raise db.PersistenceError(
            "external incident income signature mismatch"
        ) from exc
    if abs(net_external - _INCIDENT_NET_EXTERNAL) > 1e-6:
        raise db.PersistenceError("external incident income total mismatch")

    pre_equity = current_equity - net_external
    if abs(pre_equity - 5.88267638) > _INCIDENT_TOLERANCE:
        raise db.PersistenceError("external incident pre-equity mismatch")

    target = await ddp.rebase_real_account_peak_for_external_performance(
        engine.risk,
        current_equity,
        pre_event_equity=pre_equity,
        post_event_equity=current_equity,
        pre_event_peak=_INCIDENT_PRE_HWM,
        evidence_ref=(
            "2026-09-27:ATOMUSDT:external_position+income_rows:"
            f"rows={len(selected)}:net={net_external:.8f}"
        )[:160],
        repair_marker_key=incident_repair_key(),
        repair_marker_value=_incident_consumed_marker(),
        expected_peak_raw=raw_peak,
        strict=True,
    )
    log.critical(
        "[EXTERNAL_PERFORMANCE_INCIDENT_REPAIR] result=PASS incident=ATOMUSDT_20260927 "
        "bad_hwm=%.4f repaired_hwm=%.4f external_net=%.8f "
        "pre_equity=%.4f post_equity=%.4f manual_trades=%d "
        "trade_commission=%.8f trade_realized=%.8f execution_effect=NONE",
        persisted_peak, target, net_external, pre_equity, current_equity,
        trade_count, trade_commission, trade_realized,
    )
    return True


async def evaluate(engine, account_state: dict, *, log=None) -> str:
    """Return NORMAL, FREEZE, or QUARANTINE for the performance-HWM update."""
    log = log or default_log

    # Binance USD-M only. Legacy/KuCoin clients and narrow unit-test doubles
    # without the Binance user-data capability keep their existing semantics.
    if not callable(getattr(engine.client, "_listen_key_request", None)):
        setattr(engine, "_external_performance_quarantine", False)
        return "NORMAL"
    if not callable(getattr(engine.client, "get_positions", None)):
        setattr(engine, "_external_performance_quarantine", False)
        return "NORMAL"

    try:
        rows = await _read_positions(engine)
    except Exception as exc:
        setattr(engine, "_external_performance_quarantine", True)
        log.critical(
            "[EXTERNAL_PERFORMANCE_QUARANTINE] status=UNRESOLVED "
            "reason=position_read_failed error=%s execution_effect=BLOCK_NEW_ENTRIES",
            type(exc).__name__,
        )
        return "QUARANTINE"

    if await maybe_repair_known_atom_incident(engine, account_state, rows, log=log):
        setattr(engine, "_external_performance_quarantine", False)
        return "NORMAL"

    unowned = _unowned_symbols(engine, rows)
    if unowned:
        await _capture_quarantine(engine, unowned, log)
        setattr(engine, "_external_performance_quarantine", True)
        return "FREEZE"

    state = await _load_state(strict=True)
    if state is not None:
        setattr(engine, "_external_performance_quarantine", True)
        log.critical(
            "[EXTERNAL_PERFORMANCE_QUARANTINE] status=%s symbols=%s "
            "reason=external_episode_requires_reconciliation "
            "execution_effect=BLOCK_NEW_ENTRIES",
            state.get("status"),
            ",".join(state.get("symbols") or []),
        )
        return "QUARANTINE"

    setattr(engine, "_external_performance_quarantine", False)
    return "NORMAL"
