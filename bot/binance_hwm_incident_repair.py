"""One-shot repair for the 2026-09-30 Binance cash-flow/HWM ordering incident.

This module has no execution authority. It only repairs the durable performance
HWM when every durable evidence field matches the single known incident.
"""
from __future__ import annotations

import json
import math

from bot import cash_flow_ledger
from bot import database as db
from bot import drawdown_persistence as ddp
from bot import hwm_namespace, hwm_provenance
from bot.atomic_key_value import save_key_values_atomic_cas
from bot.logger import log

INCIDENT_ID = "2026-09-30-binance-cashflow-ordering"
RECONCILIATION_ID = "auto-176be1f3ca8bbe91"
EXPECTED_TRAN_IDS = ("416195536884", "416435307318")
EXPECTED_IDENTITIES = (
    "1790718360000:416195536884",
    "1790781257000:416435307318",
)
LAST_GOOD_HWM = 8.8015
PRE_FLOW_EQUITY = 7.40782133
POST_FLOW_EQUITY = 19.18862133
BAD_PREVIOUS_HWM = 19.18862133
BAD_ADJUSTED_HWM = cash_flow_ledger.twr_adjusted_peak(
    BAD_PREVIOUS_HWM, PRE_FLOW_EQUITY, POST_FLOW_EQUITY
)
REPAIRED_HWM = cash_flow_ledger.twr_adjusted_peak(
    max(LAST_GOOD_HWM, PRE_FLOW_EQUITY), PRE_FLOW_EQUITY, POST_FLOW_EQUITY
)
MARKER_KEY = (
    "risk:hwm_incident_repair:20260930:"
    + hwm_namespace.hwm_namespace()
)
_TOL = 1e-6
_EQUITY_TOL = 0.02


def _close(left, right, *, tol=_TOL) -> bool:
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= tol


def _load_json(raw, label: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise db.PersistenceError(f"{label} malformed") from exc
    if not isinstance(value, dict):
        raise db.PersistenceError(f"{label} malformed")
    return value


def _incident_record(doc: dict) -> dict:
    applied = doc.get("applied")
    if not isinstance(applied, list):
        raise db.PersistenceError("cash-flow ledger malformed")
    matches = [
        record
        for record in applied
        if isinstance(record, dict)
        and str(record.get("reconciliation_id") or "") == RECONCILIATION_ID
    ]
    if len(matches) != 1:
        raise db.PersistenceError("incident cash-flow record missing or duplicated")
    return matches[0]


def _validate_record(record: dict) -> None:
    checks = (
        str(record.get("method") or "") == "LEDGER_RECONSTRUCTED",
        sorted(str(x) for x in record.get("tran_ids", []))
        == sorted(EXPECTED_TRAN_IDS),
        sorted(str(x) for x in record.get("identities", []))
        == sorted(EXPECTED_IDENTITIES),
        _close(record.get("pre_flow_equity"), PRE_FLOW_EQUITY),
        _close(record.get("post_flow_equity"), POST_FLOW_EQUITY),
        _close(record.get("previous_hwm"), BAD_PREVIOUS_HWM),
        _close(record.get("adjusted_hwm"), BAD_ADJUSTED_HWM),
        _close(record.get("equity_at_reconciliation"), POST_FLOW_EQUITY),
        str(record.get("reason") or "")
        == "flat_account_no_performance_income_after_flow",
    )
    if not all(checks):
        raise db.PersistenceError("incident cash-flow evidence mismatch")


def _validate_provenance(raw: str) -> dict:
    doc = _load_json(raw, "HWM provenance")
    expected_ref = (
        "cash_flow_ledger:LEDGER_RECONSTRUCTED:" + RECONCILIATION_ID
    )
    checks = (
        str(doc.get("reason") or "") == "external_capital_flow_rebase",
        _close(doc.get("old_peak"), BAD_PREVIOUS_HWM),
        _close(doc.get("new_peak"), BAD_ADJUSTED_HWM),
        _close(doc.get("account_equity"), POST_FLOW_EQUITY),
        str(doc.get("evidence_ref") or "") == expected_ref,
    )
    if not all(checks):
        raise db.PersistenceError("incident HWM provenance mismatch")
    return doc


def _marker_value(*, old_peak: float, new_peak: float, equity: float) -> str:
    return json.dumps(
        {
            "version": 1,
            "incident_id": INCIDENT_ID,
            "reconciliation_id": RECONCILIATION_ID,
            "tran_ids": list(EXPECTED_TRAN_IDS),
            "old_peak": old_peak,
            "last_good_peak": LAST_GOOD_HWM,
            "new_peak": new_peak,
            "equity": equity,
            "methodology": "TWR_PERFORMANCE_HWM_REPLAY_WITH_LAST_GOOD_PEAK",
            "execution_effect": "NONE",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def repair_if_needed(risk, equity: float, *, strict: bool = True) -> dict:
    """Repair only the exact known incident; otherwise return without mutation."""
    equity = float(equity)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("repair requires positive finite equity")

    peak_raw = await db.load_key_value(ddp.DURABLE_EQUITY_PEAK_KEY, strict=strict)
    marker_raw = await db.load_key_value(MARKER_KEY, strict=strict)
    if peak_raw is None:
        if marker_raw is not None:
            raise db.PersistenceError("incident repair marker exists without HWM")
        return {"status": "NO_HWM"}
    try:
        peak = float(peak_raw)
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("incident repair HWM malformed") from exc

    if marker_raw is not None:
        marker = _load_json(marker_raw, "incident repair marker")
        if (
            str(marker.get("incident_id") or "") != INCIDENT_ID
            or not _close(marker.get("new_peak"), REPAIRED_HWM)
            or not _close(peak, REPAIRED_HWM)
        ):
            raise db.PersistenceError("incident repair marker/HWM mismatch")
        ddp.install_reconciled_peak(risk, REPAIRED_HWM, equity)
        return {
            "status": "ALREADY_REPAIRED",
            "old_peak": BAD_ADJUSTED_HWM,
            "new_peak": REPAIRED_HWM,
        }

    if not _close(peak, BAD_ADJUSTED_HWM):
        return {"status": "NOT_MATCHED", "observed_peak": peak}

    if not _close(equity, POST_FLOW_EQUITY, tol=_EQUITY_TOL):
        raise db.PersistenceError(
            "known incident HWM matched but current equity changed; manual review required"
        )

    ledger_raw = await db.load_key_value(cash_flow_ledger.LEDGER_KEY, strict=strict)
    provenance_raw = await db.load_key_value(
        hwm_namespace.provenance_key(), strict=strict
    )
    if ledger_raw is None or provenance_raw is None:
        raise db.PersistenceError("incident repair durable evidence missing")

    ledger = _load_json(ledger_raw, "cash-flow ledger")
    record = _incident_record(ledger)
    _validate_record(record)
    _validate_provenance(provenance_raw)

    repaired = REPAIRED_HWM
    provenance = hwm_provenance.build_hwm_provenance(
        reason="incident_repair",
        old_peak=peak,
        new_peak=repaired,
        account_equity=equity,
        evidence_ref=(
            "2026-09-30:cash_flow_ordering:"
            + RECONCILIATION_ID
        ),
    )
    marker = _marker_value(old_peak=peak, new_peak=repaired, equity=equity)
    ok = await save_key_values_atomic_cas(
        (
            (ddp.DURABLE_EQUITY_PEAK_KEY, format(repaired, ".17g")),
            (hwm_namespace.provenance_key(), provenance),
            (MARKER_KEY, marker),
        ),
        expected={
            ddp.DURABLE_EQUITY_PEAK_KEY: peak_raw,
            hwm_namespace.provenance_key(): provenance_raw,
            MARKER_KEY: None,
        },
        strict=strict,
    )
    if strict and not ok:
        raise db.PersistenceError("incident HWM repair write not confirmed")

    ddp.install_reconciled_peak(risk, repaired, equity)
    drawdown = max(0.0, (repaired - equity) / repaired)
    log.critical(
        "[HWM_INCIDENT_REPAIR] incident=%s result=REPAIRED "
        "reconciliation_id=%s old_peak=%.10f last_good_peak=%.4f "
        "pre_flow_equity=%.8f post_flow_equity=%.8f new_peak=%.10f "
        "equity=%.8f drawdown=%.2f%% hard_gate_expected=%s "
        "provenance=durable_atomic_cas execution_effect=NONE",
        INCIDENT_ID,
        RECONCILIATION_ID,
        peak,
        LAST_GOOD_HWM,
        PRE_FLOW_EQUITY,
        POST_FLOW_EQUITY,
        repaired,
        equity,
        drawdown * 100.0,
        str(drawdown >= 0.10).lower(),
    )
    return {
        "status": "REPAIRED",
        "old_peak": peak,
        "new_peak": repaired,
        "drawdown": drawdown,
    }
