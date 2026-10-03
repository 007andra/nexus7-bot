"""Canonical external cash-flow ledger and cash-flow-adjusted (TWR) drawdown.

Trading performance is not the gross change of the account balance. Deposits,
withdrawals and transfers are external cash flows: they change equity without
being profit or loss of the bot. This module is the single authority that
separates the two for Binance USD-M.

Methodology (time-weighted return / performance-index HWM)
----------------------------------------------------------
Let ``P`` be the durable performance high-water mark and ``E`` the account
equity. For an external flow of signed amount ``a`` with equity ``E-``
immediately before it and ``E+ = E- + a`` immediately after it::

    P' = P * E+ / E-                       (performance HWM rebased)
    trading_drawdown = 1 - E / P'

* A flow alone leaves the drawdown unchanged: ``1 - E+/P' = 1 - E-/P``.
  A withdrawal therefore never increases drawdown and a deposit never reduces it.
* A flow never creates a new high: ``P'`` scales with the flow, so ``E+/P'``
  equals ``E-/P``. Only later market performance can move equity above ``P'``.
* Losses and fees after the flow are measured on the capital that remained
  (``E/P'`` falls exactly with trading losses). REALIZED_PNL, COMMISSION,
  FUNDING_FEE and every other non-transfer income type are performance, never
  external flows.

Evidence rules (fail-closed)
----------------------------
A flow is only ever applied from an exchange ledger row (``/fapi/v1/income``
TRANSFER-like rows with identity ``tranId`` and signed ``income``). A balance
difference alone is never interpreted as a transfer.

``E-`` is reconstructed automatically only when it is provable from the ledger:
single-asset margin mode, account flat now (no position margin, zero unrealized
PnL, equity == wallet), the income evidence is stable across a double read, and
*no* non-flow income row (of any asset) exists at or after the first pending
flow. Under those conditions the wallet moved only by the pending flows since
before the first one, so ``E- = wallet_now - sum(pending)`` exactly and no
position was open at the flow time (any position open then would have produced
a closing REALIZED_PNL/COMMISSION row afterwards).

Anything else keeps the flow PENDING, which blocks new entries until an
operator records an explicit, idempotent, auditable attestation
(``python -m bot.cash_flow_admin``). Pending flows are durable, so a flow that
ages out of the exchange query window keeps blocking instead of being silently
forgotten.

A separate wallet invariant (``wallet_now - wallet_baseline == sum(income rows
after the baseline anchor)``) blocks entries when the wallet changed by a
material amount the ledger cannot explain. A drop is never assumed to be a
withdrawal.

No function here places, cancels or authorizes orders.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from bot import database as db
from bot.atomic_key_value import save_key_values_atomic_cas
from bot.logger import log

LEDGER_KEY = "risk:external_cash_flows:binance:ledger:v1"
WALLET_BASELINE_KEY = "risk:external_cash_flows:binance:wallet_baseline:v1"
# Kept for compatibility with the original checkpoint-only detector.
SEEN_CURSOR_KEY = "risk:external_capital_flow:binance:seen_transfers:v1"

DETECTION_WINDOW_MS = 48 * 3600 * 1000
_APPLIED_IDS_ATTR = "_cash_flow_applied_ids"
_BLOCK_HOLD_ATTR = "_cash_flow_block_hold"
# While blocked, the runtime refreshes every few seconds; re-querying
# /fapi/v1/income (weight 30, read twice) on each refresh would approach the
# Binance request-weight limit. A block is therefore held (re-raised without
# I/O) for this long before the evidence is re-read.
BLOCK_HOLD_S = 30.0
_LAST_SUMMARY_ATTR = "_cash_flow_last_summary"

# Binance USD-M /fapi/v1/income types that move capital into or out of the
# futures wallet without being trading performance. DEPOSIT/WITHDRAW are listed
# defensively; Binance reports spot<->futures movements as TRANSFER.
EXTERNAL_FLOW_INCOME_TYPES = frozenset({
    "TRANSFER",
    "INTERNAL_TRANSFER",
    "STRATEGY_UMFUTURES_TRANSFER",
    "CROSS_COLLATERAL_TRANSFER",
    "DEPOSIT",
    "WITHDRAW",
    "COIN_SWAP_DEPOSIT",
    "COIN_SWAP_WITHDRAW",
})
# Explicitly performance (documented for auditors; anything not external is
# treated as performance, which is the conservative default).
PERFORMANCE_INCOME_TYPES = frozenset({
    "REALIZED_PNL",
    "COMMISSION",
    "FUNDING_FEE",
    "INSURANCE_CLEAR",
    "COMMISSION_REBATE",
    "API_REBATE",
    "REFERRAL_KICKBACK",
    "FEE_RETURN",
    "POSITION_LIMIT_INCREASE_FEE",
    "DELIVERED_SETTELMENT",
    "AUTO_EXCHANGE",
})

_MAX_FLOW_RATIO = 1_000.0
_MAX_PEAK_TO_EQUITY_RATIO = 1_000.0
_WALLET_TOLERANCE_ABS = 0.02
_WALLET_TOLERANCE_REL = 0.005
_BASELINE_QUIET_MS = 60_000


class CashFlowBlocked(RuntimeError):
    """New entries must stay blocked: cash flows are not reconciled."""


# ─── pure math ────────────────────────────────────────────────────────────────

def _finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{label} nonfinite")
    return out


def _positive(value, label: str) -> float:
    out = _finite(value, label)
    if out <= 0:
        raise ValueError(f"{label} must be positive")
    return out


def classify_income_type(income_type) -> str:
    kind = str(income_type or "").strip().upper()
    if kind in EXTERNAL_FLOW_INCOME_TYPES:
        return "EXTERNAL_FLOW"
    return "PERFORMANCE"


def twr_adjusted_peak(peak: float, pre_flow_equity: float, post_flow_equity: float) -> float:
    """Rebase a performance HWM across one external flow: ``P * E+ / E-``."""
    peak = _positive(peak, "performance HWM")
    pre = _positive(pre_flow_equity, "pre-flow equity")
    post = _positive(post_flow_equity, "post-flow equity")
    ratio = post / pre
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("external flow ratio must be positive and finite")
    if ratio > _MAX_FLOW_RATIO or ratio < 1.0 / _MAX_FLOW_RATIO:
        raise ValueError("external flow ratio implausible")
    out = peak * ratio
    if not math.isfinite(out) or out <= 0:
        raise ValueError("adjusted HWM nonfinite")
    return out


def trading_drawdown(equity: float, performance_peak: float) -> float:
    equity = _finite(equity, "equity")
    peak = _positive(performance_peak, "performance HWM")
    return max(0.0, (peak - equity) / peak)


# ─── ledger document ──────────────────────────────────────────────────────────

def flow_identity(row: dict) -> str:
    """Stable identity; TRANSFER keeps the original ``time:tranId`` format."""
    kind = str(row.get("incomeType") or "").strip().upper()
    event_time = int(_finite(row.get("time", 0), "income time"))
    if event_time <= 0:
        raise ValueError("income time must be positive")
    tran_id = str(row.get("tranId") or "").strip()
    if not tran_id:
        raise ValueError("income tranId missing")
    if kind == "TRANSFER":
        return f"{event_time}:{tran_id}"
    return f"{kind}:{event_time}:{tran_id}"


def _normalize_flow(row: dict, detected_at_ms: int) -> dict:
    kind = str(row.get("incomeType") or "").strip().upper()
    asset = str(row.get("asset") or "USDT").strip().upper()
    amount = _finite(row.get("income"), "flow amount")
    if amount == 0:
        raise ValueError("flow amount must be nonzero")
    return {
        "identity": flow_identity(row),
        "income_type": kind,
        "tran_id": str(row.get("tranId")),
        "time_ms": int(row.get("time")),
        "amount": amount,
        "asset": asset,
        "detected_at_ms": int(detected_at_ms),
    }


def _empty_ledger() -> dict:
    return {"version": 1, "pending": [], "applied": []}


def _dump(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _parse_ledger(raw) -> dict:
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or int(doc.get("version", 0)) != 1:
            raise ValueError("version")
        for field in ("pending", "applied"):
            if not isinstance(doc.get(field), list):
                raise ValueError(field)
        for item in doc["pending"]:
            if not isinstance(item, dict) or not item.get("identity"):
                raise ValueError("pending item")
            _finite(item.get("amount"), "pending amount")
        for record in doc["applied"]:
            if not isinstance(record, dict) or not record.get("reconciliation_id"):
                raise ValueError("applied record")
            if not isinstance(record.get("identities"), list):
                raise ValueError("applied identities")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("external cash-flow ledger is malformed") from exc
    return doc


def _parse_cursor(raw) -> list[str]:
    try:
        cursor = json.loads(raw)
        if not isinstance(cursor, dict) or int(cursor.get("version", 0)) != 1:
            raise ValueError("invalid version")
        seen = cursor.get("seen")
        if not isinstance(seen, list) or any(not isinstance(i, str) or not i for i in seen):
            raise ValueError("invalid seen identities")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("Binance capital-flow cursor is malformed") from exc
    return list(seen)


def _cursor_value(seen: list[str], now_ms: int) -> str:
    return _dump({"version": 1, "seen": sorted(set(seen)), "observed_at_ms": int(now_ms)})


def applied_identities(doc: dict) -> set[str]:
    out: set[str] = set()
    for record in doc.get("applied", []):
        out.update(str(i) for i in record.get("identities", []))
    return out


def _applied_ids(doc: dict) -> tuple[str, ...]:
    return tuple(sorted(str(r["reconciliation_id"]) for r in doc.get("applied", [])))


def _direction(net: float) -> str:
    if net > 0:
        return "DEPOSIT"
    if net < 0:
        return "WITHDRAWAL"
    return "NET_ZERO"


def ledger_totals(doc: dict) -> dict:
    applied_net = math.fsum(float(r.get("net_amount", 0.0)) for r in doc.get("applied", []))
    pending_net = math.fsum(float(p.get("amount", 0.0)) for p in doc.get("pending", []))
    return {
        "applied_records": len(doc.get("applied", [])),
        "applied_net": applied_net,
        "pending_flows": len(doc.get("pending", [])),
        "pending_net": pending_net,
    }


def _server_now_ms(client) -> int:
    now = getattr(client, "_now_ms", None)
    if callable(now):
        try:
            return int(now())
        except Exception:
            pass
    return int(time.time() * 1000)


def _cfg_drawdown_limit() -> float | None:
    try:
        from bot.config import cfg

        return float(cfg.MAX_DRAWDOWN)
    except Exception:
        return None


# ─── evidence reconstruction ──────────────────────────────────────────────────

def _account_flat(account: dict) -> tuple[bool, str]:
    try:
        wallet = _finite(account.get("walletBalance"), "walletBalance")
        equity = _finite(account.get("equity"), "equity")
        unrealized = _finite(account.get("unrealisedPNL", 0.0), "unrealisedPNL")
        position_margin = _finite(account.get("positionMargin", 0.0), "positionMargin")
    except (TypeError, ValueError):
        return False, "account_state_unreadable"
    if bool(account.get("multiAssetsMargin", False)):
        return False, "multi_assets_margin"
    if position_margin != 0.0:
        return False, "open_position_margin"
    if abs(unrealized) > 1e-8:
        return False, "open_unrealized_pnl"
    if abs(equity - wallet) > 1e-6 * max(1.0, abs(wallet)):
        return False, "equity_wallet_mismatch"
    return True, "flat"


def reconstruct_pending_flows(pending: list[dict], rows: list[dict], account: dict, window_start_ms: int) -> dict:
    """Prove E- for the pending batch from the ledger, or explain why not.

    Returns ``{"ok": True, "pre": E-, "post": E+, "net": a}`` or
    ``{"ok": False, "reason": ...}``. Pure function; no I/O.
    """
    if not pending:
        return {"ok": False, "reason": "no_pending_flows"}
    flat, why = _account_flat(account)
    if not flat:
        return {"ok": False, "reason": why}
    if any(str(p.get("asset", "USDT")).upper() != "USDT" for p in pending):
        return {"ok": False, "reason": "non_usdt_flow"}
    first_t = min(int(p["time_ms"]) for p in pending)
    if first_t < int(window_start_ms):
        return {"ok": False, "reason": "flow_outside_evidence_window"}
    pending_ids = {str(p["identity"]) for p in pending}
    visible_ids = set()
    for row in rows:
        t = int(row.get("time", 0) or 0)
        if t < first_t:
            continue
        kind = classify_income_type(row.get("incomeType"))
        if kind != "EXTERNAL_FLOW":
            return {"ok": False, "reason": "performance_income_after_flow"}
        identity = flow_identity(row)
        if identity not in pending_ids:
            return {"ok": False, "reason": "unreconciled_flow_interleaved"}
        visible_ids.add(identity)
    if visible_ids != pending_ids:
        return {"ok": False, "reason": "pending_flow_not_visible_in_ledger"}
    wallet = _positive(account.get("walletBalance"), "walletBalance")
    net = math.fsum(float(p["amount"]) for p in pending)
    pre = wallet - net
    if not math.isfinite(pre) or pre <= 0:
        return {"ok": False, "reason": "nonpositive_pre_flow_equity"}
    ratio = wallet / pre
    if ratio > _MAX_FLOW_RATIO or ratio < 1.0 / _MAX_FLOW_RATIO:
        return {"ok": False, "reason": "flow_ratio_implausible"}
    return {"ok": True, "pre": pre, "post": wallet, "net": net, "first_time_ms": first_t}


def check_wallet_invariant(baseline: dict | None, rows: list[dict], wallet: float, window_start_ms: int) -> dict:
    """``wallet - baseline.wallet`` must equal the income rows after the anchor."""
    if baseline is None:
        return {"status": "NO_BASELINE"}
    anchor = int(baseline["anchor_ms"])
    if anchor < int(window_start_ms):
        return {"status": "BASELINE_STALE"}
    explained = math.fsum(
        float(row.get("income", 0.0) or 0.0)
        for row in rows
        if int(row.get("time", 0) or 0) > anchor
        and str(row.get("asset") or "USDT").upper() == "USDT"
    )
    delta = float(wallet) - float(baseline["wallet"])
    residual = delta - explained
    tolerance = max(_WALLET_TOLERANCE_ABS, abs(float(wallet)) * _WALLET_TOLERANCE_REL)
    return {
        "status": "OK" if abs(residual) <= tolerance else "UNEXPLAINED",
        "delta": delta,
        "explained": explained,
        "residual": residual,
        "tolerance": tolerance,
    }


def _parse_baseline(raw) -> dict | None:
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or int(doc.get("version", 0)) != 1:
            raise ValueError("version")
        return {
            "wallet": _finite(doc["wallet"], "baseline wallet"),
            "anchor_ms": int(doc["anchor_ms"]),
        }
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise db.PersistenceError("wallet baseline is malformed") from exc


def _baseline_value(wallet: float, anchor_ms: int, reason: str) -> str:
    return _dump({
        "version": 1,
        "wallet": format(float(wallet), ".17g"),
        "anchor_ms": int(anchor_ms),
        "reason": reason,
    })


def _quiet(rows: list[dict], anchor_ms: int) -> bool:
    return not any(
        anchor_ms - _BASELINE_QUIET_MS <= int(r.get("time", 0) or 0) <= anchor_ms
        for r in rows
    )


# ─── HWM application ──────────────────────────────────────────────────────────

async def _load_raw_peak(strict: bool):
    from bot.drawdown_persistence import DURABLE_EQUITY_PEAK_KEY

    raw = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
    if raw is None:
        return None, None
    try:
        return _positive(raw, "persisted equity peak"), raw
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("durable equity peak is malformed") from exc


def build_record(
    *,
    flows: list[dict],
    pre_flow_equity: float,
    post_flow_equity: float,
    previous_hwm: float | None,
    current_equity: float,
    method: str,
    reason: str,
    evidence_ref: str,
    reconciliation_id: str | None = None,
    now_ms: int,
) -> dict:
    identities = sorted(str(f["identity"]) for f in flows)
    if not identities:
        raise ValueError("reconciliation requires at least one flow")
    net = math.fsum(float(f["amount"]) for f in flows)
    pre = _positive(pre_flow_equity, "pre-flow equity")
    post = _positive(post_flow_equity, "post-flow equity")
    if abs((pre + net) - post) > 1e-6 * max(1.0, abs(post)):
        raise ValueError("post-flow equity must equal pre-flow equity + net flow")
    current_equity = _positive(current_equity, "current equity")
    adjusted = None
    if previous_hwm is not None:
        # The HWM in force at the flow is max(P, E-): an unrecorded high just
        # before the flow is performance, then the flow rescales it.
        base = max(_positive(previous_hwm, "previous HWM"), pre)
        adjusted = max(twr_adjusted_peak(base, pre, post), current_equity)
        if adjusted / current_equity > _MAX_PEAK_TO_EQUITY_RATIO:
            raise ValueError("adjusted HWM implausible relative to equity")
    if reconciliation_id is None:
        digest = hashlib.sha256("|".join(identities).encode()).hexdigest()[:16]
        reconciliation_id = f"auto-{digest}"
    reconciliation_id = str(reconciliation_id).strip()
    if not reconciliation_id or len(reconciliation_id) > 64:
        raise ValueError("reconciliation_id must be 1..64 chars")
    return {
        "reconciliation_id": reconciliation_id,
        "method": method,
        "identities": identities,
        "tran_ids": sorted(str(f["tran_id"]) for f in flows),
        "income_types": sorted({str(f["income_type"]) for f in flows}),
        "net_amount": net,
        "gross_in": math.fsum(float(f["amount"]) for f in flows if float(f["amount"]) > 0),
        "gross_out": math.fsum(-float(f["amount"]) for f in flows if float(f["amount"]) < 0),
        "direction": _direction(net),
        "flow_time_first_ms": min(int(f["time_ms"]) for f in flows),
        "flow_time_last_ms": max(int(f["time_ms"]) for f in flows),
        "pre_flow_equity": pre,
        "post_flow_equity": post,
        "previous_hwm": previous_hwm,
        "adjusted_hwm": adjusted,
        "equity_at_reconciliation": current_equity,
        "trading_drawdown_after": (
            None if adjusted is None else trading_drawdown(current_equity, adjusted)
        ),
        "reason": str(reason)[:200],
        "evidence_ref": str(evidence_ref)[:200],
        "recorded_at_ms": int(now_ms),
    }


async def commit_record(
    risk,
    record: dict,
    *,
    ledger: dict,
    ledger_raw,
    seen: list[str],
    cursor_raw,
    peak_raw,
    strict: bool,
    now_ms: int,
) -> dict:
    """Atomically persist HWM + provenance + ledger + cursor (compare-and-swap)."""
    from bot import drawdown_persistence as ddp
    from bot import hwm_namespace, hwm_provenance

    ids = set(record["identities"])
    if ids & applied_identities(ledger):
        raise ValueError("flow already applied")
    if any(str(r["reconciliation_id"]) == record["reconciliation_id"] for r in ledger["applied"]):
        raise ValueError("reconciliation_id already used")
    new_doc = {
        "version": 1,
        "pending": [p for p in ledger["pending"] if str(p["identity"]) not in ids],
        "applied": list(ledger["applied"]) + [record],
    }
    items = [
        (LEDGER_KEY, _dump(new_doc)),
        (SEEN_CURSOR_KEY, _cursor_value(list(seen) + sorted(ids), now_ms)),
    ]
    expected = {LEDGER_KEY: ledger_raw, SEEN_CURSOR_KEY: cursor_raw, ddp.DURABLE_EQUITY_PEAK_KEY: peak_raw}
    adjusted = record.get("adjusted_hwm")
    if adjusted is not None:
        items.append((ddp.DURABLE_EQUITY_PEAK_KEY, format(float(adjusted), ".17g")))
        items.append((
            hwm_namespace.provenance_key(),
            hwm_provenance.build_hwm_provenance(
                reason="external_capital_flow_rebase",
                old_peak=record["previous_hwm"],
                new_peak=adjusted,
                account_equity=record["equity_at_reconciliation"],
                evidence_ref=f"cash_flow_ledger:{record['method']}:{record['reconciliation_id']}"[:160],
            ),
        ))
    ok = await save_key_values_atomic_cas(items, expected=expected, strict=strict)
    if strict and not ok:
        raise db.PersistenceError("cash-flow reconciliation write not confirmed")
    if risk is not None and adjusted is not None:
        ddp.install_reconciled_peak(risk, float(adjusted), float(record["equity_at_reconciliation"]))
    if risk is not None:
        setattr(risk, _APPLIED_IDS_ATTR, _applied_ids(new_doc))
    log.warning(
        "[CASH_FLOW] reconciliation_id=%s method=%s direction=%s net_amount=%.8f "
        "flows=%d tran_ids=%s first_time_ms=%d pre_flow_equity=%.8f post_flow_equity=%.8f "
        "status=APPLIED execution_effect=NONE",
        record["reconciliation_id"], record["method"], record["direction"], record["net_amount"],
        len(record["identities"]), ",".join(record["tran_ids"]), record["flow_time_first_ms"],
        record["pre_flow_equity"], record["post_flow_equity"],
    )
    log.warning(
        "[DRAWDOWN_RECONCILIATION] result=RECONCILED reconciliation_id=%s previous_hwm=%s "
        "adjusted_hwm=%s equity=%.4f trading_drawdown=%s methodology=TWR_PERFORMANCE_HWM "
        "provenance=durable_atomic_cas execution_effect=NONE",
        record["reconciliation_id"],
        "N/A" if record["previous_hwm"] is None else f"{record['previous_hwm']:.4f}",
        "N/A" if adjusted is None else f"{adjusted:.4f}",
        record["equity_at_reconciliation"],
        "N/A" if record["trading_drawdown_after"] is None else f"{record['trading_drawdown_after'] * 100.0:.2f}%",
    )
    return new_doc


# ─── runtime reconciliation (Binance) ─────────────────────────────────────────

def _log_adjusted_equity(risk, equity: float, doc: dict, *, status: str, effect: str) -> None:
    totals = ledger_totals(doc)
    peak = getattr(risk, "_durable_account_equity_peak", None)
    try:
        peak = float(peak) if peak is not None else None
        if peak is not None and status == "RECONCILED":
            # restore_update_real_account_peak raises the HWM to a real new high
            peak = max(peak, float(equity))
    except (TypeError, ValueError):
        peak = None
    dd = None if peak is None else trading_drawdown(equity, peak)
    limit = _cfg_drawdown_limit()
    summary = {
        "status": status,
        "equity": float(equity),
        "performance_hwm": peak,
        "trading_drawdown": dd if status == "RECONCILED" else None,
        "unadjusted_drawdown": dd if status != "RECONCILED" else None,
        "drawdown_limit": limit,
        **totals,
    }
    if risk is not None:
        try:
            setattr(risk, _LAST_SUMMARY_ATTR, summary)
        except Exception:
            pass
    log.info(
        "[ADJUSTED_EQUITY] status=%s equity=%.4f performance_hwm=%s trading_drawdown=%s "
        "unadjusted_drawdown=%s drawdown_limit=%s external_flows_applied=%d "
        "external_flows_net=%.4f pending_flows=%d pending_net=%.4f execution_effect=%s",
        status,
        float(equity),
        "N/A" if peak is None else f"{peak:.4f}",
        "N/A" if summary["trading_drawdown"] is None else f"{summary['trading_drawdown'] * 100.0:.2f}%",
        "N/A" if summary["unadjusted_drawdown"] is None else f"{summary['unadjusted_drawdown'] * 100.0:.2f}%",
        "N/A" if limit is None else f"{limit * 100.0:.2f}%",
        totals["applied_records"],
        totals["applied_net"],
        totals["pending_flows"],
        totals["pending_net"],
        effect,
    )


def _block(risk, equity, doc, *, reason: str, strict: bool, code: str, **info) -> dict:
    details = " ".join(f"{k}={v}" for k, v in sorted(info.items()))
    log.critical(
        "[DRAWDOWN_RECONCILIATION] result=BLOCK reason=%s %s action=BLOCK_NEW_ENTRIES "
        "operator_action=%s execution_effect=BLOCK_NEW_ENTRY",
        reason,
        details,
        "python -m bot.cash_flow_admin show" if code.endswith("UNCONFIRMED") else "none_retry_next_cycle",
    )
    if doc is not None:
        _log_adjusted_equity(risk, equity, doc, status="UNRECONCILED", effect="BLOCK_NEW_ENTRY")
    if risk is not None:
        try:
            setattr(risk, _BLOCK_HOLD_ATTR, (time.monotonic() + BLOCK_HOLD_S, code))
        except Exception:
            pass
    if strict:
        raise CashFlowBlocked(code)
    return {"applied": 0, "blocked": True, "reason": reason}


async def _reload_peak_if_ledger_changed(risk, doc: dict, equity: float, strict: bool) -> None:
    ids = _applied_ids(doc)
    known = getattr(risk, _APPLIED_IDS_ATTR, None)
    if known == ids:
        return
    if known is not None or getattr(risk, "_durable_account_equity_peak", None) is not None:
        # Another process (operator CLI) applied a reconciliation, or this
        # process restarted with a warm cache: reload the durable HWM.
        from bot.drawdown_persistence import reload_durable_peak

        await reload_durable_peak(risk, equity, strict=strict)
    setattr(risk, _APPLIED_IDS_ATTR, ids)


async def reconcile_binance(client, risk, current_equity: float, *, strict: bool = True) -> dict:
    from bot.binance_accounting_evidence import collect_income

    current_equity = _positive(current_equity, "current equity")
    hold = getattr(risk, _BLOCK_HOLD_ATTR, None) if risk is not None else None
    if strict and hold and time.monotonic() < float(hold[0]):
        raise CashFlowBlocked(str(hold[1]))
    if risk is not None and hold:
        setattr(risk, _BLOCK_HOLD_ATTR, None)
    now_ms = _server_now_ms(client)
    window_start = now_ms - DETECTION_WINDOW_MS
    try:
        rows = await collect_income(client, window_start, now_ms)
        account = await client.get_account_state()
        end2 = max(now_ms + 1, _server_now_ms(client))
        rows_again = await collect_income(client, window_start, end2)
    except Exception as exc:
        return _block(
            risk, current_equity, None, reason="exchange_evidence_unavailable", strict=strict,
            code="BINANCE_CASH_FLOW_EVIDENCE_UNAVAILABLE", error=type(exc).__name__,
        )
    if rows != rows_again:
        return _block(
            risk, current_equity, None, reason="income_changed_during_read", strict=strict,
            code="BINANCE_CASH_FLOW_EVIDENCE_UNSTABLE",
        )
    if not isinstance(account, dict):
        return _block(
            risk, current_equity, None, reason="account_state_unreadable", strict=strict,
            code="BINANCE_CASH_FLOW_EVIDENCE_UNAVAILABLE",
        )
    equity = _positive(account.get("equity", current_equity), "account equity")
    wallet = _finite(account.get("walletBalance", equity), "walletBalance")

    external = []
    for row in rows:
        if classify_income_type(row.get("incomeType")) == "EXTERNAL_FLOW":
            external.append(_normalize_flow(row, now_ms))

    cursor_raw = await db.load_key_value(SEEN_CURSOR_KEY, strict=strict)
    ledger_raw = await db.load_key_value(LEDGER_KEY, strict=strict)
    baseline_raw = await db.load_key_value(WALLET_BASELINE_KEY, strict=strict)

    if cursor_raw is None:
        # First observation ever: identities only, no inference from balances.
        doc = _empty_ledger() if ledger_raw is None else _parse_ledger(ledger_raw)
        items = [(SEEN_CURSOR_KEY, _cursor_value([f["identity"] for f in external], now_ms))]
        expected = {SEEN_CURSOR_KEY: None}
        if ledger_raw is None:
            items.append((LEDGER_KEY, _dump(doc)))
            expected[LEDGER_KEY] = None
        if not bool(account.get("multiAssetsMargin", False)):
            items.append((WALLET_BASELINE_KEY, _baseline_value(wallet, now_ms, "bootstrap")))
            expected[WALLET_BASELINE_KEY] = baseline_raw
        ok = await save_key_values_atomic_cas(items, expected=expected, strict=strict)
        if strict and not ok:
            raise db.PersistenceError("Binance capital-flow checkpoint write not confirmed")
        if risk is not None:
            setattr(risk, _APPLIED_IDS_ATTR, _applied_ids(doc))
        log.warning(
            "[CASH_FLOW] bootstrap=checkpoint_only flows=%d rebase=false "
            "reason=pre_existing_flows_predate_ledger execution_effect=NONE",
            len(external),
        )
        return {"applied": 0, "bootstrap": True, "observed": len(external), "authority": "checkpoint_only"}

    seen = _parse_cursor(cursor_raw)
    upgrade = ledger_raw is None
    doc = _empty_ledger() if upgrade else _parse_ledger(ledger_raw)
    await _reload_peak_if_ledger_changed(risk, doc, equity, strict)

    known = set(seen) | {str(p["identity"]) for p in doc["pending"]} | applied_identities(doc)
    new_flows = [f for f in external if f["identity"] not in known]
    checkpointed = []
    if upgrade:
        # The original detector only tracked TRANSFER rows; other transfer-like
        # types it never saw are checkpointed exactly like its bootstrap did.
        checkpointed = [f for f in new_flows if f["income_type"] != "TRANSFER"]
        new_flows = [f for f in new_flows if f["income_type"] == "TRANSFER"]
    if new_flows or upgrade or checkpointed:
        doc = {**doc, "pending": list(doc["pending"]) + new_flows}
        items = [(LEDGER_KEY, _dump(doc))]
        expected = {LEDGER_KEY: ledger_raw}
        if checkpointed:
            seen = seen + [f["identity"] for f in checkpointed]
            items.append((SEEN_CURSOR_KEY, _cursor_value(seen, now_ms)))
            expected[SEEN_CURSOR_KEY] = cursor_raw
        ok = await save_key_values_atomic_cas(items, expected=expected, strict=strict)
        if strict and not ok:
            raise db.PersistenceError("cash-flow ledger write not confirmed")
        ledger_raw = items[0][1]
        if checkpointed:
            cursor_raw = items[1][1]
        for flow in new_flows:
            log.critical(
                "[CASH_FLOW] detected income_type=%s tran_id=%s time_ms=%d amount=%.8f asset=%s "
                "classification=EXTERNAL_FLOW status=PENDING_RECONCILIATION execution_effect=BLOCK_NEW_ENTRY",
                flow["income_type"], flow["tran_id"], flow["time_ms"], flow["amount"], flow["asset"],
            )

    # Ledger completeness: the wallet may only move by income rows.
    multi_asset = bool(account.get("multiAssetsMargin", False))
    baseline = _parse_baseline(baseline_raw)
    invariant = {"status": "UNVERIFIABLE_MULTI_ASSET"} if multi_asset else check_wallet_invariant(
        baseline, rows, wallet, window_start,
    )
    if invariant["status"] == "UNEXPLAINED":
        return _block(
            risk, equity, doc, reason="unexplained_wallet_change", strict=strict,
            code="BINANCE_UNRECONCILED_EQUITY_CHANGE",
            wallet_delta=f"{invariant['delta']:.8f}", ledger_explained=f"{invariant['explained']:.8f}",
            residual=f"{invariant['residual']:.8f}", tolerance=f"{invariant['tolerance']:.8f}",
        )
    if invariant["status"] == "UNVERIFIABLE_MULTI_ASSET":
        log.warning(
            "[DRAWDOWN_RECONCILIATION] wallet_invariant=UNVERIFIABLE reason=multi_assets_margin "
            "flows_from_ledger_only=true execution_effect=NONE"
        )

    applied_now = 0
    if doc["pending"]:
        proof = reconstruct_pending_flows(doc["pending"], rows, account, window_start)
        if not proof["ok"]:
            return _block(
                risk, equity, doc, reason=f"pending_external_flow_unprovable:{proof['reason']}",
                strict=strict, code="BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED",
                pending=len(doc["pending"]),
                pending_net=f"{math.fsum(float(p['amount']) for p in doc['pending']):.8f}",
            )
        previous_hwm, peak_raw = await _load_raw_peak(strict)
        try:
            record = build_record(
                flows=doc["pending"],
                pre_flow_equity=proof["pre"],
                post_flow_equity=proof["post"],
                previous_hwm=previous_hwm,
                current_equity=equity,
                method="LEDGER_RECONSTRUCTED",
                reason="flat_account_no_performance_income_after_flow",
                evidence_ref=f"binance_income:{window_start}-{now_ms}:double_read_stable",
                now_ms=now_ms,
            )
        except ValueError as exc:
            return _block(
                risk, equity, doc, reason="reconciliation_math_refused", strict=strict,
                code="BINANCE_CAPITAL_FLOW_REBASE_UNCONFIRMED", error=str(exc).replace(" ", "_"),
            )
        doc = await commit_record(
            risk, record, ledger=doc, ledger_raw=ledger_raw, seen=seen, cursor_raw=cursor_raw,
            peak_raw=peak_raw, strict=strict, now_ms=now_ms,
        )
        applied_now = 1

    if not multi_asset and _quiet(rows, now_ms):
        if invariant["status"] in {"NO_BASELINE", "BASELINE_STALE"}:
            log.warning(
                "[DRAWDOWN_RECONCILIATION] wallet_baseline=%s action=reanchor_unverified "
                "drawdown_treats_unexplained_change_as_performance=true execution_effect=NONE",
                invariant["status"],
            )
        await save_key_values_atomic_cas(
            [(WALLET_BASELINE_KEY, _baseline_value(wallet, now_ms, invariant["status"].lower()))],
            expected={WALLET_BASELINE_KEY: baseline_raw},
            strict=strict,
        )

    _log_adjusted_equity(risk, equity, doc, status="RECONCILED", effect="NONE")
    return {
        "applied": applied_now,
        "bootstrap": False,
        "blocked": False,
        "observed": len(external),
        "wallet_invariant": invariant["status"],
    }


# ─── operator attestation (one-shot, idempotent, auditable) ───────────────────

async def attest_flows(
    *,
    rows: list[dict],
    reconciliation_id: str,
    tran_ids: list[str],
    pre_flow_equity: float,
    current_equity: float,
    reason: str,
    evidence_ref: str,
    apply: bool,
    now_ms: int,
    strict: bool = True,
) -> dict:
    """Record an operator-attested external flow reconciliation.

    Amount, direction and timestamps always come from the exchange ledger rows
    identified by ``tran_ids``; the operator supplies only the pre-flow equity
    (with an evidence reference) that the ledger alone cannot prove. The same
    ``reconciliation_id`` is idempotent; a flow can never be applied twice.
    """
    reconciliation_id = str(reconciliation_id or "").strip()
    if not reconciliation_id or len(reconciliation_id) > 64:
        raise ValueError("reconciliation_id must be 1..64 chars")
    reason = str(reason or "").strip()
    evidence_ref = str(evidence_ref or "").strip()
    if not reason or not evidence_ref:
        raise ValueError("reason and evidence_ref are mandatory")
    wanted = [str(t).strip() for t in tran_ids if str(t).strip()]
    if not wanted or len(set(wanted)) != len(wanted):
        raise ValueError("tran_ids must be non-empty and unique")

    cursor_raw = await db.load_key_value(SEEN_CURSOR_KEY, strict=strict)
    if cursor_raw is None:
        raise ValueError("runtime cash-flow cursor not bootstrapped; nothing to attest against")
    seen = _parse_cursor(cursor_raw)
    ledger_raw = await db.load_key_value(LEDGER_KEY, strict=strict)
    doc = _empty_ledger() if ledger_raw is None else _parse_ledger(ledger_raw)

    for record in doc["applied"]:
        if str(record["reconciliation_id"]) == reconciliation_id:
            if sorted(record.get("tran_ids", [])) == sorted(wanted):
                return {"status": "ALREADY_APPLIED", "record": record}
            raise ValueError("reconciliation_id already used for different flows")

    flows = []
    for tran_id in wanted:
        matches = [
            r for r in rows
            if str(r.get("tranId")) == tran_id
            and classify_income_type(r.get("incomeType")) == "EXTERNAL_FLOW"
        ]
        if len(matches) != 1:
            raise ValueError(f"tranId {tran_id} not found exactly once as an external flow row")
        flow = _normalize_flow(matches[0], now_ms)
        if flow["asset"] != "USDT":
            raise ValueError("only USDT flows are supported")
        flows.append(flow)
    ids = {f["identity"] for f in flows}
    if ids & applied_identities(doc):
        raise ValueError("flow already applied")
    pending_ids = {str(p["identity"]) for p in doc["pending"]}
    checkpointed = (ids & set(seen)) - pending_ids
    if checkpointed:
        raise ValueError("flow was checkpointed before the ledger existed; refusing to rebase")

    first_t = min(f["time_ms"] for f in flows)
    last_t = max(f["time_ms"] for f in flows)
    for row in rows:
        t = int(row.get("time", 0) or 0)
        if first_t <= t <= last_t and classify_income_type(row.get("incomeType")) != "EXTERNAL_FLOW":
            raise ValueError("performance income between attested flows; attest each flow separately")
        if first_t <= t <= last_t and classify_income_type(row.get("incomeType")) == "EXTERNAL_FLOW":
            if flow_identity(row) not in ids:
                raise ValueError("another external flow lies between attested flows; include it")

    net = math.fsum(f["amount"] for f in flows)
    pre = _positive(pre_flow_equity, "pre-flow equity")
    post = pre + net
    if post <= 0:
        raise ValueError("post-flow equity must be positive")
    previous_hwm, peak_raw = await _load_raw_peak(strict)
    record = build_record(
        flows=flows,
        pre_flow_equity=pre,
        post_flow_equity=post,
        previous_hwm=previous_hwm,
        current_equity=current_equity,
        method="OPERATOR_ATTESTED",
        reason=reason,
        evidence_ref=evidence_ref,
        reconciliation_id=reconciliation_id,
        now_ms=now_ms,
    )
    if not apply:
        return {"status": "DRY_RUN", "record": record}
    await commit_record(
        None, record, ledger=doc, ledger_raw=ledger_raw, seen=seen, cursor_raw=cursor_raw,
        peak_raw=peak_raw, strict=strict, now_ms=now_ms,
    )
    return {"status": "APPLIED", "record": record}


async def rebaseline_wallet(*, wallet: float, anchor_ms: int, reason: str, apply: bool, strict: bool = True) -> dict:
    """Operator re-anchor of the wallet-completeness invariant (never the HWM)."""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason is mandatory")
    wallet = _finite(wallet, "wallet")
    raw = await db.load_key_value(WALLET_BASELINE_KEY, strict=strict)
    value = _baseline_value(wallet, anchor_ms, f"operator:{reason[:120]}")
    if not apply:
        return {"status": "DRY_RUN", "previous": raw, "new": value}
    await save_key_values_atomic_cas(
        [(WALLET_BASELINE_KEY, value)], expected={WALLET_BASELINE_KEY: raw}, strict=strict,
    )
    log.warning(
        "[DRAWDOWN_RECONCILIATION] wallet_baseline=OPERATOR_REANCHOR wallet=%.8f anchor_ms=%d "
        "reason=%s hwm_changed=false execution_effect=NONE",
        wallet, int(anchor_ms), reason[:120].replace(" ", "_"),
    )
    return {"status": "APPLIED", "previous": raw, "new": value}


async def ledger_snapshot(*, strict: bool = True) -> dict:
    cursor_raw = await db.load_key_value(SEEN_CURSOR_KEY, strict=strict)
    ledger_raw = await db.load_key_value(LEDGER_KEY, strict=strict)
    baseline_raw = await db.load_key_value(WALLET_BASELINE_KEY, strict=strict)
    peak, _ = await _load_raw_peak(strict)
    doc = _empty_ledger() if ledger_raw is None else _parse_ledger(ledger_raw)
    return {
        "cursor_bootstrapped": cursor_raw is not None,
        "seen_identities": 0 if cursor_raw is None else len(_parse_cursor(cursor_raw)),
        "ledger": doc,
        "totals": ledger_totals(doc),
        "wallet_baseline": _parse_baseline(baseline_raw),
        "durable_hwm": peak,
    }
