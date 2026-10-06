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
import os
import time

from bot import database as db
from bot import drawdown_persistence as ddp
from bot import hwm_namespace
from bot.atomic_key_value import save_key_values_atomic_cas
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

# Pinned 2026-10-05 OP/SEI external-position episode. Unlike the ATOM repair,
# the pre-event equity/HWM snapshot is unavailable, so this incident must never
# rebase performance. It may only be conservatively resolved after Binance
# proves the affected symbols are flat and every fill in the evidence window is
# MANUAL_EXTERNAL. Historical HWM/drawdown stay untouched.
_FLAT_RECONCILE_ENV = "EXTERNAL_PERFORMANCE_FLAT_RECONCILIATION_APPROVED"
_FLAT_RECONCILE_TOKEN = "I_APPROVE_OP_SEI_20261005_CONSERVATIVE_RESOLUTION"
_FLAT_INCIDENT_ID = "OP_SEI_20261005"
_FLAT_INCIDENT_STARTED_MS = 1791232052236
_FLAT_INCIDENT_SYMBOLS = frozenset({"OPUSDT", "SEIUSDT"})
_FLAT_LOOKBACK_MS = 48 * 3600 * 1000


def incident_repair_key() -> str:
    return f"risk:external_performance_repair:ATOMUSDT_20260927:v1:{hwm_namespace.hwm_namespace()}"


def _incident_consumed_marker_for_namespace(namespace: str) -> str:
    return json.dumps({
        "version": 1,
        "incident": "ATOMUSDT_20260927",
        "namespace": str(namespace),
        "start_ms": _INCIDENT_START_MS,
        "end_ms": _INCIDENT_END_MS,
        "status": "CONSUMED",
    }, sort_keys=True, separators=(",", ":"))


def _incident_consumed_marker() -> str:
    return _incident_consumed_marker_for_namespace(
        hwm_namespace.hwm_namespace()
    )


def _accepted_incident_consumed_markers() -> tuple[str, ...]:
    """Accept only exact current/legacy receipts during NOVO-03 migration.

    database.load_key_value may legitimately return the previous release marker
    through its read-only namespace fallback. That payload embeds the old HWM
    namespace, so comparing it only with the current marker incorrectly turns
    a proven CONSUMED incident into PersistenceError. Every other mutation
    remains fail-closed.
    """
    return tuple(dict.fromkeys((
        _incident_consumed_marker_for_namespace(
            hwm_namespace.hwm_namespace()
        ),
        _incident_consumed_marker_for_namespace(
            hwm_namespace.legacy_hwm_namespace()
        ),
    )))


async def _incident_already_consumed() -> bool:
    # Only absence permits a first repair. Unknown versions, partial writes,
    # corrupt values and storage errors must never be interpreted as absence.
    raw = await db.load_key_value(incident_repair_key(), strict=True)
    if raw is None:
        return False
    if raw not in _accepted_incident_consumed_markers():
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
        if doc.get("status") not in {"ACTIVE", "UNRESOLVED", "RESOLVED"}:
            raise ValueError("invalid quarantine status")
        return doc
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("external performance quarantine state malformed") from exc


async def _capture_quarantine(engine, symbols: set[str], log) -> dict:
    existing = await _load_state(strict=True)
    if existing is not None and existing.get("status") == "RESOLVED":
        # A future external position is a new episode, even for the same symbol.
        existing = None
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


def flat_resolution_key() -> str:
    return (
        f"risk:external_performance_resolution:{_FLAT_INCIDENT_ID}:v1:"
        f"{hwm_namespace.hwm_namespace()}"
    )


def _flat_resolution_approved() -> bool:
    return (
        os.environ.get(_FLAT_RECONCILE_ENV, "").strip()
        == _FLAT_RECONCILE_TOKEN
    )


def _is_pinned_flat_incident(state: dict) -> bool:
    try:
        started = int(state.get("started_at_ms"))
    except (TypeError, ValueError):
        return False
    return (
        int(state.get("version", 0)) == 1
        and state.get("status") in {"ACTIVE", "UNRESOLVED"}
        and set(str(x).upper() for x in (state.get("symbols") or []))
        == set(_FLAT_INCIDENT_SYMBOLS)
        and state.get("reason") == "external_symbol_set_changed"
        and started == _FLAT_INCIDENT_STARTED_MS
        and state.get("pre_event_equity") is None
        and state.get("pre_event_peak") is None
    )


def _trade_signed_qty(trade: dict) -> float:
    qty = _finite(trade.get("qty", 0), "trade qty")
    side = str(trade.get("side") or "").upper()
    if qty <= 0:
        raise ValueError("trade qty not positive")
    if side == "BUY":
        return qty
    if side == "SELL":
        return -qty
    raise ValueError("trade side invalid")


async def _prove_pinned_flat_manual_incident(engine, state: dict, rows) -> dict:
    if _active_exchange_symbols(rows):
        raise db.PersistenceError("flat reconciliation requires flat exchange account")
    if not _is_pinned_flat_incident(state):
        raise db.PersistenceError("flat reconciliation incident identity mismatch")

    registry = await _load_registry()
    end_ms = server_now_ms(engine.client)
    start_ms = max(1, _FLAT_INCIDENT_STARTED_MS - _FLAT_LOOKBACK_MS)
    if end_ms <= _FLAT_INCIDENT_STARTED_MS:
        raise db.PersistenceError("flat reconciliation clock invalid")
    if end_ms - start_ms > 7 * 86400000:
        raise db.PersistenceError("flat reconciliation evidence window too large")

    proof = {}
    first_trade_ms = None
    last_trade_ms = None
    for symbol in sorted(_FLAT_INCIDENT_SYMBOLS):
        trades = await collect_user_trades(engine.client, symbol, start_ms, end_ms)
        orders = await collect_orders(engine.client, symbol, start_ms, end_ms)
        if len(trades) < 2:
            raise db.PersistenceError(
                f"flat reconciliation trade evidence incomplete for {symbol}"
            )
        order_map = {
            str(row.get("orderId")): row
            for row in orders
            if isinstance(row, dict) and row.get("orderId") is not None
        }
        signed_qty = 0.0
        total_abs_qty = 0.0
        realized = 0.0
        commission = 0.0
        times = []
        for trade in trades:
            order = order_map.get(str(trade.get("orderId")))
            origin, origin_reason = classify_trade_origin(trade, order, registry)
            if origin != "MANUAL_EXTERNAL":
                raise db.PersistenceError(
                    f"flat reconciliation ownership unproven for {symbol}:"
                    f"{origin}:{origin_reason}"
                )
            if str(trade.get("commissionAsset") or "").upper() != "USDT":
                raise db.PersistenceError(
                    f"flat reconciliation non-USDT commission for {symbol}"
                )
            delta = _trade_signed_qty(trade)
            signed_qty += delta
            total_abs_qty += abs(delta)
            realized += _finite(trade.get("realizedPnl", 0), "trade realizedPnl")
            commission += _finite(trade.get("commission", 0), "trade commission")
            times.append(int(trade.get("time", 0) or 0))

        tolerance = max(1e-9, total_abs_qty * 1e-9)
        if abs(signed_qty) > tolerance:
            raise db.PersistenceError(
                f"flat reconciliation trade path not flat for {symbol}"
            )
        if not times or min(times) <= 0:
            raise db.PersistenceError(
                f"flat reconciliation trade timestamps invalid for {symbol}"
            )
        proof[symbol] = {
            "trades": len(trades),
            "orders": len(orders),
            "first_trade_ms": min(times),
            "last_trade_ms": max(times),
            "realized_pnl": realized,
            "commission_paid": commission,
            "net_signed_qty": signed_qty,
            "origin": "MANUAL_EXTERNAL",
        }
        first_trade_ms = min(
            min(times), first_trade_ms if first_trade_ms is not None else min(times)
        )
        last_trade_ms = max(
            max(times), last_trade_ms if last_trade_ms is not None else max(times)
        )

    if first_trade_ms is None or first_trade_ms > _FLAT_INCIDENT_STARTED_MS:
        raise db.PersistenceError(
            "flat reconciliation does not cover the initially observed OP position"
        )

    income1 = await collect_income(engine.client, start_ms, end_ms)
    income2 = await collect_income(engine.client, start_ms, end_ms)
    if income1 != income2:
        raise db.PersistenceError("flat reconciliation income evidence unstable")

    affected_income = []
    by_type = {}
    net_income = 0.0
    for row in income1:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in _FLAT_INCIDENT_SYMBOLS:
            continue
        kind = str(row.get("incomeType") or "").upper()
        amount = _finite(row.get("income", 0), "income")
        if kind not in _ALLOWED_EXTERNAL_PERFORMANCE_TYPES and abs(amount) > 1e-12:
            raise db.PersistenceError(
                f"flat reconciliation unsupported income type {kind}"
            )
        if str(row.get("asset") or "").upper() != "USDT":
            raise db.PersistenceError("flat reconciliation non-USDT income")
        affected_income.append(row)
        slot = by_type.setdefault(kind, {"count": 0, "income": 0.0})
        slot["count"] += 1
        slot["income"] += amount
        net_income += amount

    return {
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "first_trade_ms": first_trade_ms,
        "last_trade_ms": last_trade_ms,
        "symbols": proof,
        "income_rows": len(affected_income),
        "income_by_type": by_type,
        "net_external_income": net_income,
        "account_flat": True,
        "ownership": "MANUAL_EXTERNAL_ONLY",
    }


async def maybe_resolve_pinned_flat_incident(engine, account_state: dict, rows, log=None) -> bool:
    """Resolve only the pinned OP/SEI episode without changing performance HWM."""
    del account_state
    log = log or default_log
    if not _flat_resolution_approved():
        return False

    state = await _load_state(strict=True)
    if state is None:
        return False
    if state.get("status") == "RESOLVED":
        return True
    if not _is_pinned_flat_incident(state):
        raise db.PersistenceError("flat reconciliation approval does not match quarantine")

    proof = await _prove_pinned_flat_manual_incident(engine, state, rows)
    raw_effective_state = await db.load_key_value(state_key(), strict=True)
    raw_current_state = await db._load_key_value_raw(state_key(), strict=True)
    if raw_effective_state is None:
        raise db.PersistenceError("flat reconciliation state disappeared")
    receipt_key = flat_resolution_key()
    existing_receipt = await db._load_key_value_raw(receipt_key, strict=True)
    if existing_receipt is not None:
        raise db.PersistenceError("flat reconciliation receipt already exists")

    resolved = dict(state)
    resolved.update({
        "status": "RESOLVED",
        "reason": "manual_external_flat_conservative_no_hwm_rebase",
        "resolved_at_ms": int(proof["window_end_ms"]),
        "hwm_rebased": False,
        "hwm_preserved": True,
        "execution_effect": "NONE",
    })
    receipt = {
        "version": 1,
        "incident": _FLAT_INCIDENT_ID,
        "status": "RESOLVED",
        "quarantine_started_at_ms": _FLAT_INCIDENT_STARTED_MS,
        "resolved_at_ms": int(proof["window_end_ms"]),
        "symbols": sorted(_FLAT_INCIDENT_SYMBOLS),
        "ownership": proof["ownership"],
        "account_flat": True,
        "first_trade_ms": proof["first_trade_ms"],
        "last_trade_ms": proof["last_trade_ms"],
        "income_rows": proof["income_rows"],
        "income_by_type": proof["income_by_type"],
        "net_external_income": proof["net_external_income"],
        "hwm_rebased": False,
        "hwm_preserved": True,
        "methodology": "CONSERVATIVE_FLAT_RESOLUTION_NO_HWM_REBASE",
        "reset_allowed": False,
    }
    encoded_state = json.dumps(
        resolved, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    encoded_receipt = json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    ok = await save_key_values_atomic_cas(
        ((state_key(), encoded_state), (receipt_key, encoded_receipt)),
        expected={state_key(): raw_current_state, receipt_key: None},
        strict=True,
    )
    if not ok:
        raise db.PersistenceError("flat reconciliation write unconfirmed")

    log.critical(
        "[EXTERNAL_PERFORMANCE_RECONCILIATION] result=PASS incident=%s "
        "symbols=%s ownership=MANUAL_EXTERNAL_ONLY account_flat=true "
        "income_rows=%s net_external_income=%.8f hwm_rebased=false "
        "hwm_preserved=true methodology=CONSERVATIVE_FLAT_RESOLUTION_NO_HWM_REBASE "
        "execution_effect=NONE",
        _FLAT_INCIDENT_ID,
        ",".join(sorted(_FLAT_INCIDENT_SYMBOLS)),
        proof["income_rows"],
        float(proof["net_external_income"]),
    )
    return True


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
    if state is not None and state.get("status") == "RESOLVED":
        setattr(engine, "_external_performance_quarantine", False)
        return "NORMAL"

    if state is not None and await maybe_resolve_pinned_flat_incident(
        engine, account_state, rows, log=log
    ):
        setattr(engine, "_external_performance_quarantine", False)
        return "NORMAL"

    if state is not None:
        setattr(engine, "_external_performance_quarantine", True)
        log.critical(
            "[EXTERNAL_PERFORMANCE_QUARANTINE] status=%s symbols=%s "
            "reason=external_episode_requires_reconciliation "
            "state_reason=%s started_at_ms=%s pre_event_equity=%s "
            "pre_event_peak=%s execution_effect=BLOCK_NEW_ENTRIES",
            state.get("status"),
            ",".join(state.get("symbols") or []),
            state.get("reason", "NA"),
            state.get("started_at_ms", "NA"),
            state.get("pre_event_equity", "NA"),
            state.get("pre_event_peak", "NA"),
        )
        return "QUARANTINE"

    setattr(engine, "_external_performance_quarantine", False)
    return "NORMAL"
