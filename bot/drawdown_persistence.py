"""Durable, cash-flow-aware account-equity high-water mark for LIVE drawdown.

Safety invariants: ordinary trading never lowers the peak; only verified external
capital flow may rebase it lower; malformed persistence fails closed; the known
2026-09-14 corrupt HWM has a narrowly evidence-bound repair; every HWM write is
atomically paired with durable provenance. No execution authorization exists here.
"""
from __future__ import annotations

import math

from bot import database as db
from bot import hwm_provenance
from bot import hwm_namespace
from bot.atomic_key_value import save_key_values_atomic, save_key_values_atomic_cas
from bot.logger import log

LEGACY_DURABLE_EQUITY_PEAK_KEY = "risk:account_equity_peak:v1"
DURABLE_EQUITY_PEAK_KEY = hwm_namespace.equity_peak_key()
_CACHE_ATTR = "_durable_account_equity_peak"
_INCIDENT_BAD_PEAK = 82_894_351_780.2826
_INCIDENT_LAST_GOOD_PEAK = 28.7914
_INCIDENT_BAD_PEAK_TOLERANCE = 1.0
_TRANSFER_RATIO_INCIDENT_BAD_PEAK = 42_709_241_923.064377
_TRANSFER_RATIO_INCIDENT_REPAIRED_PEAK = 63.7942573
_TRANSFER_RATIO_INCIDENT_TOLERANCE = 1.0
_MAX_UNEXPLAINED_PEAK_TO_EQUITY_RATIO = 1_000.0


def _positive_finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} boolean")
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return out


def _matches_known_20260914_corruption(persisted: float) -> bool:
    return abs(persisted - _INCIDENT_BAD_PEAK) <= _INCIDENT_BAD_PEAK_TOLERANCE


def _matches_known_transfer_ratio_corruption(persisted: float) -> bool:
    return abs(persisted - _TRANSFER_RATIO_INCIDENT_BAD_PEAK) <= _TRANSFER_RATIO_INCIDENT_TOLERANCE


def _validate_peak_vs_equity(peak: float, equity: float) -> None:
    ratio = peak / equity
    if not math.isfinite(ratio) or ratio > _MAX_UNEXPLAINED_PEAK_TO_EQUITY_RATIO:
        log.critical(
            "[DURABLE_DRAWDOWN_ANOMALY] kind=implausible_hwm persisted_peak=%.17g "
            "current_equity=%.17g peak_to_equity_ratio=%.9g threshold=%.9g "
            "known_incident_match=%s execution_effect=BLOCK_FAIL_CLOSED",
            peak,
            equity,
            ratio,
            _MAX_UNEXPLAINED_PEAK_TO_EQUITY_RATIO,
            _matches_known_20260914_corruption(peak),
        )
        raise db.PersistenceError("durable equity peak is implausible relative to current account equity")


def _apply_peak(risk, peak: float, current_equity: float, *, allow_lower: bool = False) -> None:
    peak = _positive_finite(peak, "equity peak")
    current_equity = _positive_finite(current_equity, "account equity")
    v3 = getattr(risk, "_v3", None)
    if v3 is not None:
        if allow_lower and hasattr(v3, "rebase_peak_equity"):
            v3.rebase_peak_equity(peak)
        elif hasattr(v3, "restore_peak_equity"):
            if allow_lower and hasattr(v3, "_peak_equity"):
                v3._peak_equity = peak
            else:
                v3.restore_peak_equity(peak)
    legacy = getattr(risk, "_legacy", risk)
    if allow_lower:
        legacy.peak_balance = peak
    else:
        existing = float(getattr(legacy, "peak_balance", 0.0) or 0.0)
        legacy.peak_balance = max(existing, peak)
        peak = legacy.peak_balance
    legacy.drawdown = max(0.0, (peak - current_equity) / peak)


async def _load_peak(risk, *, strict: bool) -> tuple[float | None, str]:
    cached = getattr(risk, _CACHE_ATTR, None)
    if cached is not None:
        try:
            return _positive_finite(cached, "cached equity peak"), "cache"
        except (TypeError, ValueError) as exc:
            raise db.PersistenceError("cached durable equity peak is malformed") from exc
    raw = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
    if raw is None:
        return None, "database"
    try:
        return _positive_finite(raw, "persisted equity peak"), "database"
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("durable equity peak is malformed") from exc


async def _write_peak_with_provenance(*, old_peak, new_peak, equity, reason, evidence_ref, strict):
    provenance = hwm_provenance.build_hwm_provenance(
        reason=reason,
        old_peak=old_peak,
        new_peak=new_peak,
        account_equity=equity,
        evidence_ref=evidence_ref,
    )
    ok = await save_key_values_atomic(
        (
            (DURABLE_EQUITY_PEAK_KEY, format(new_peak, ".17g")),
            (hwm_namespace.provenance_key(), provenance),
        ),
        strict=strict,
    )
    if strict and not ok:
        raise db.PersistenceError("atomic HWM/provenance write not confirmed")


async def restore_update_real_account_peak(risk, equity: float, *, strict: bool = True) -> float:
    equity = _positive_finite(equity, "account equity")
    persisted, _ = await _load_peak(risk, strict=strict)
    repaired = False
    if persisted is not None:
        if _matches_known_20260914_corruption(persisted):
            old_peak = persisted
            persisted = max(_INCIDENT_LAST_GOOD_PEAK, equity)
            await _write_peak_with_provenance(
                old_peak=old_peak, new_peak=persisted, equity=equity,
                reason="incident_repair",
                evidence_ref="2026-09-14:exact_corrupt_hwm_signature+authenticated_equity",
                strict=strict,
            )
            setattr(risk, _CACHE_ATTR, persisted)
            repaired = True
            log.critical(
                "[DURABLE_DRAWDOWN_REPAIR] incident=2026-09-14-corrupt-hwm kind=exact_signature "
                "old_peak=%.4f last_good_peak=%.4f current_equity=%.4f repaired_peak=%.4f "
                "provenance=durable_atomic execution_effect=NONE",
                old_peak, _INCIDENT_LAST_GOOD_PEAK, equity, persisted,
            )
        elif _matches_known_transfer_ratio_corruption(persisted):
            old_peak = persisted
            persisted = max(_TRANSFER_RATIO_INCIDENT_REPAIRED_PEAK, equity)
            await _write_peak_with_provenance(
                old_peak=old_peak, new_peak=persisted, equity=equity,
                reason="incident_repair",
                evidence_ref="2026-09-20:transfer_ratio_hwm_corruption+authenticated_equity",
                strict=strict,
            )
            setattr(risk, _CACHE_ATTR, persisted)
            repaired = True
            log.critical(
                "[DURABLE_DRAWDOWN_REPAIR] incident=2026-09-20-transfer-ratio-hwm "
                "old_peak=%.4f repaired_peak=%.4f current_equity=%.4f "
                "provenance=durable_atomic execution_effect=NONE",
                old_peak, persisted, equity,
            )
        else:
            _validate_peak_vs_equity(persisted, equity)

    peak = max(equity, persisted or equity)
    needs_write = persisted is None or peak > persisted
    if needs_write:
        await _write_peak_with_provenance(
            old_peak=persisted, new_peak=peak, equity=equity,
            reason="bootstrap" if persisted is None else "new_equity_high",
            evidence_ref="authenticated_account_equity",
            strict=strict,
        )

    setattr(risk, _CACHE_ATTR, peak)
    _apply_peak(risk, peak, equity, allow_lower=repaired)
    log.info(
        "[DURABLE_DRAWDOWN] equity=%.4f peak_equity=%.4f drawdown=%.2f%% source=%s "
        "persistence=%s provenance=%s execution_effect=NONE",
        equity, peak, max(0.0, (peak - equity) / peak) * 100.0,
        "bootstrap" if persisted is None else ("incident_repair" if repaired else "restored"),
        "repaired" if repaired else ("updated" if needs_write else "unchanged"),
        "updated_atomic" if (repaired or needs_write) else "unchanged",
    )
    return peak


async def restore_real_account_peak_without_new_high(
    risk, equity: float, *, strict: bool = True
) -> float:
    """Restore the durable HWM but refuse to create a new high.

    Used while performance attribution is quarantined by an external/manual
    exchange position. Account equity remains authoritative for collateral and
    solvency, but cannot manufacture a trading-performance HWM until ownership
    attribution is resolved.
    """
    equity = _positive_finite(equity, "account equity")
    persisted, _ = await _load_peak(risk, strict=strict)
    if persisted is None:
        raise db.PersistenceError("cannot freeze missing durable equity peak")
    _validate_peak_vs_equity(persisted, equity)
    setattr(risk, _CACHE_ATTR, persisted)
    _apply_peak(risk, persisted, equity, allow_lower=True)
    log.warning(
        "[DURABLE_DRAWDOWN] equity=%.4f peak_equity=%.4f drawdown=%.2f%% "
        "source=restored_no_new_high persistence=unchanged provenance=unchanged "
        "reason=external_performance_quarantine execution_effect=BLOCK_NEW_ENTRIES",
        equity, persisted, max(0.0, (persisted - equity) / persisted) * 100.0,
    )
    return persisted


async def rebase_real_account_peak_for_external_performance(
    risk,
    current_equity: float,
    *,
    pre_event_equity: float,
    post_event_equity: float,
    pre_event_peak: float,
    evidence_ref: str,
    strict: bool = True,
) -> float:
    """Neutralize a proven external/manual performance episode by TWR rebasing.

    This is deliberately analogous to external cash-flow rebasing: when a
    manual position's net realized result is proven not to belong to BGX,
    preserve the pre-episode performance drawdown ratio rather than treating
    that external result as bot profit/loss.
    """
    current_equity = _positive_finite(current_equity, "account equity")
    pre_event_equity = _positive_finite(pre_event_equity, "pre-event equity")
    post_event_equity = _positive_finite(post_event_equity, "post-event equity")
    pre_event_peak = _positive_finite(pre_event_peak, "pre-event peak")
    if not math.isclose(current_equity, post_event_equity, rel_tol=0.0, abs_tol=1e-6):
        raise ValueError("post-event equity does not match current equity")

    ratio = post_event_equity / pre_event_equity
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("external performance rebase ratio invalid")
    rebased_peak = pre_event_peak * ratio
    if not math.isfinite(rebased_peak) or rebased_peak <= 0:
        raise ValueError("external performance rebased HWM invalid")

    raw_peak = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
    if raw_peak is None:
        raise db.PersistenceError("cannot rebase missing durable equity peak")
    try:
        old_peak = _positive_finite(raw_peak, "persisted equity peak")
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("durable equity peak is malformed") from exc

    provenance = hwm_provenance.build_hwm_provenance(
        reason="external_position_performance_rebase",
        old_peak=old_peak,
        new_peak=rebased_peak,
        account_equity=current_equity,
        evidence_ref=evidence_ref,
    )
    ok = await save_key_values_atomic_cas(
        (
            (DURABLE_EQUITY_PEAK_KEY, format(rebased_peak, ".17g")),
            (hwm_namespace.provenance_key(), provenance),
        ),
        expected={DURABLE_EQUITY_PEAK_KEY: raw_peak},
        strict=strict,
    )
    if strict and not ok:
        raise db.PersistenceError("external performance HWM rebase write not confirmed")

    setattr(risk, _CACHE_ATTR, rebased_peak)
    _apply_peak(risk, rebased_peak, current_equity, allow_lower=True)
    log.critical(
        "[EXTERNAL_PERFORMANCE_REBASE] result=RECONCILED old_peak=%.4f "
        "pre_event_equity=%.4f post_event_equity=%.4f new_peak=%.4f "
        "drawdown=%.2f%% methodology=TWR_EXTERNAL_POSITION_PERFORMANCE "
        "provenance=durable_atomic_cas execution_effect=NONE",
        old_peak, pre_event_equity, post_event_equity, rebased_peak,
        max(0.0, (rebased_peak - current_equity) / rebased_peak) * 100.0,
    )
    return rebased_peak


async def rebase_real_account_peak_for_external_flow(
    risk, current_equity: float, *, pre_flow_equity: float, post_flow_equity: float,
    flow_type: str, flow_amount: float, flow_offset: str, strict: bool = True,
) -> float:
    current_equity = _positive_finite(current_equity, "account equity")
    pre_flow_equity = _positive_finite(pre_flow_equity, "pre-flow equity")
    post_flow_equity = _positive_finite(post_flow_equity, "post-flow equity")
    flow_amount = _positive_finite(flow_amount, "external flow amount")
    persisted, _ = await _load_peak(risk, strict=strict)
    if persisted is None:
        raise db.PersistenceError("cannot rebase missing durable equity peak")
    # External transfers are additive cash flows, not multiplicative returns.
    # Ratio rebasing explodes when pre-flow equity is near zero (for example
    # after a losing leveraged position): persisted * post/pre can manufacture
    # a multi-billion HWM from a tens-of-USDT account.
    kind = str(flow_type)
    if kind == "TransferIn":
        # Additive rebasing is mandatory here: a deposit following near-zero
        # equity must not multiply the historical HWM by post/pre.
        rebased_peak = max(current_equity, persisted + flow_amount)
    elif kind == "TransferOut":
        # Preserve the pre-withdrawal drawdown ratio. This is the established
        # durable-risk contract and remains numerically well-defined because a
        # verified withdrawal has positive pre-flow equity.
        ratio = post_flow_equity / pre_flow_equity
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("external flow ratio must be positive and finite")
        rebased_peak = max(current_equity, persisted * ratio)
    else:
        raise ValueError("unsupported external flow type")
    await _write_peak_with_provenance(
        old_peak=persisted, new_peak=rebased_peak, equity=current_equity,
        reason="external_capital_flow_rebase",
        evidence_ref=f"exchange_ledger:{flow_type}:{flow_offset}", strict=strict,
    )
    setattr(risk, _CACHE_ATTR, rebased_peak)
    _apply_peak(risk, rebased_peak, current_equity, allow_lower=True)
    log.warning(
        "[CAPITAL_FLOW_REBASE] type=%s amount=%.4f offset=%s pre_equity=%.4f post_equity=%.4f "
        "old_peak=%.4f new_peak=%.4f drawdown=%.2f%% provenance=durable_atomic execution_effect=NONE",
        flow_type, flow_amount, flow_offset, pre_flow_equity, post_flow_equity,
        persisted, rebased_peak, max(0.0, (rebased_peak-current_equity)/rebased_peak)*100.0,
    )
    return rebased_peak


def install_reconciled_peak(risk, peak: float, current_equity: float) -> None:
    """Install a peak already durably committed by ``cash_flow_ledger``.

    The ledger writes HWM + provenance + ledger atomically; this only mirrors
    the committed value into the in-process cache and risk managers.
    """
    peak = _positive_finite(peak, "reconciled equity peak")
    current_equity = _positive_finite(current_equity, "account equity")
    setattr(risk, _CACHE_ATTR, peak)
    _apply_peak(risk, peak, current_equity, allow_lower=True)


async def reload_durable_peak(risk, equity: float, *, strict: bool = True) -> float | None:
    """Drop the in-process HWM cache and reload the durable value.

    Used when the external cash-flow ledger changed outside this process (an
    operator attestation), so the running process never keeps enforcing a
    stale, unadjusted HWM nor re-persists it.
    """
    equity = _positive_finite(equity, "account equity")
    old = getattr(risk, _CACHE_ATTR, None)
    raw = await db.load_key_value(DURABLE_EQUITY_PEAK_KEY, strict=strict)
    if raw is None:
        if hasattr(risk, _CACHE_ATTR):
            setattr(risk, _CACHE_ATTR, None)
        return None
    try:
        peak = _positive_finite(raw, "persisted equity peak")
    except (TypeError, ValueError) as exc:
        raise db.PersistenceError("durable equity peak is malformed") from exc
    _validate_peak_vs_equity(peak, equity)
    setattr(risk, _CACHE_ATTR, peak)
    _apply_peak(risk, peak, equity, allow_lower=True)
    log.warning(
        "[DURABLE_DRAWDOWN] reload=ledger_changed old_cached_peak=%s peak_equity=%.4f equity=%.4f "
        "drawdown=%.2f%% execution_effect=NONE",
        "N/A" if old is None else f"{float(old):.4f}", peak, equity,
        max(0.0, (peak - equity) / peak) * 100.0,
    )
    return peak
