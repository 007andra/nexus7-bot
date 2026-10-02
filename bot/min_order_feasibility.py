"""Early LIVE minimum-order feasibility gate.

This gate rejects a candidate before NEXUS only when the same stop-risk
economics used by final sizing prove that no exchange-valid minimum order can
fit the current risk/collateral budget.

It is deliberately authorization-neutral:
- never grows or rounds a quantity upward;
- never changes score, stop, target, leverage, risk policy, or position limits;
- never submits/cancels/modifies an exchange order;
- PASS only allows the existing NEXUS + final sizing + pre-dispatch chain to run.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os

from bot.account_capital_reader import read_account_capital
from bot.config import cfg
from bot.drawdown_recovery import recovery_size_multiplier
from bot import execution_cost
from bot.sizing_decomposition import decompose


@dataclass(frozen=True)
class FeasibilityDecision:
    allowed: bool
    reason: str
    detail: dict
    proven: bool = True


def _effective_stop_risk_pct(engine) -> float:
    base = float(engine._effective_risk_pct())
    drawdown = float(getattr(getattr(engine, "risk", None), "drawdown", 0.0) or 0.0)
    multiplier = float(recovery_size_multiplier(drawdown))
    effective = base * multiplier
    if (
        not math.isfinite(base)
        or not math.isfinite(multiplier)
        or not math.isfinite(effective)
        or base <= 0
        or multiplier <= 0
        or not 0 < effective <= 1
    ):
        raise ValueError("invalid effective stop-risk percentage")
    return effective


def _stress_slippage(snapshot) -> float:
    configured = float(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))
    if not math.isfinite(configured) or configured < 0:
        raise ValueError("invalid configured slippage")
    if snapshot is None:
        return configured
    observed = float(snapshot.slippage_allowance)
    if not math.isfinite(observed) or observed < 0:
        raise ValueError("invalid snapshot slippage")
    return max(configured, observed)


async def _capital_for_feasibility(engine):
    """Return positive current capital, otherwise None and defer downstream.

    Prefer a fresh read. Some offline/adversarial harnesses intentionally expose
    partial account shapes that are sufficient for later dedicated gates but
    not for account-capital normalization; in that case a confirmed
    RiskManagerV3 snapshot is acceptable for this advisory early proof.
    """
    try:
        fresh = await read_account_capital(engine.client)
        capital = fresh.capital
        equity = float(capital.equity)
        available = float(capital.available_collateral)
        if (
            math.isfinite(equity)
            and math.isfinite(available)
            and equity > 0
            and available > 0
        ):
            return capital
    except Exception:
        pass

    try:
        risk_snapshot = engine.risk.professional_snapshot
        capital = risk_snapshot.capital
        equity = float(capital.equity)
        available = float(capital.available_collateral)
        if (
            bool(risk_snapshot.confirmed)
            and math.isfinite(equity)
            and math.isfinite(available)
            and equity > 0
            and available > 0
        ):
            return capital
    except Exception:
        pass
    return None


async def evaluate_candidate(engine, sig) -> FeasibilityDecision:
    """Prove whether at least one exchange-valid order fits current risk.

    Uses the same candidate cost snapshot, exchange quantity metadata,
    RiskManagerV3 risk percentage semantics and sizing decomposition as the
    final sizing path. This is an early necessary-condition gate, not sizing
    authority: a PASS never authorizes execution.
    """
    symbol = str(getattr(sig, "symbol", "") or "")
    info = (getattr(engine, "instruments", {}) or {}).get(symbol)
    if not symbol or not isinstance(info, dict) or not info:
        return FeasibilityDecision(False, "INVALID_METADATA", {})

    entry = float(getattr(sig, "entry", 0.0) or 0.0)
    stop = float(getattr(sig, "sl", 0.0) or 0.0)
    if (
        not math.isfinite(entry)
        or not math.isfinite(stop)
        or entry <= 0
        or stop <= 0
        or entry == stop
    ):
        return FeasibilityDecision(False, "INVALID_GEOMETRY", {})

    capital = await _capital_for_feasibility(engine)
    if capital is None:
        return FeasibilityDecision(True, "DEFER_CAPITAL_UNCONFIRMED", {}, proven=False)

    try:
        risk_pct = _effective_stop_risk_pct(engine)
    except (TypeError, ValueError, ArithmeticError):
        return FeasibilityDecision(True, "DEFER_RISK_CONTEXT", {}, proven=False)

    # operator_loss_policy runs outside this wrapper and normally attaches the
    # candidate snapshot first. If it could not, use the exact RiskManagerV3
    # no-snapshot fallback rather than doing another network read here.
    cost_snapshot = execution_cost.reusable_snapshot(sig)
    fee_rate = (
        float(cost_snapshot.taker_fee)
        if cost_snapshot is not None
        else float(execution_cost.fallback_taker_fee())
    )
    slippage = _stress_slippage(cost_snapshot)

    detail = decompose(
        info=info,
        equity=float(capital.equity),
        available=float(capital.available_collateral),
        entry=entry,
        stop=stop,
        risk_pct=risk_pct,
        leverage=float(cfg.LEVERAGE),
        max_margin_pct=float(getattr(cfg, "MAX_MARGIN_PCT", 0.80)),
        fee_rate_per_side=fee_rate,
        slippage_pct=slippage,
    )
    result = str(detail.get("result") or "").upper()
    reason = str(detail.get("reason") or "UNKNOWN")
    binding = str(detail.get("binding") or "")

    if result == "PASS":
        return FeasibilityDecision(True, reason or "SIZED", detail, proven=True)
    if (
        result == "BLOCK"
        and reason == "INSUFFICIENT_RISK_BUDGET"
        and binding in {"MIN_QTY_BINDING", "MIN_NOTIONAL_BINDING"}
    ):
        return FeasibilityDecision(False, reason, detail, proven=True)

    # This gate owns only minimum-order risk feasibility. Margin, metadata,
    # readiness, durable state, ownership, cross-stress and market-data
    # ambiguity remain owned by their existing downstream fail-closed gates.
    return FeasibilityDecision(
        True,
        f"DEFER_{reason or 'UNPROVEN'}",
        detail,
        proven=False,
    )


def install(TradingEngine, log) -> None:
    """Install an outer _open gate so impossible lots never invoke NEXUS."""
    if getattr(TradingEngine, "_min_order_feasibility_installed", False):
        return

    original_open = TradingEngine._open

    async def _open_with_min_order_feasibility(self, sig, *args, **kwargs):
        if (
            getattr(self, "paper_trade", False)
            or not bool(getattr(getattr(self, "pilot", None), "enabled", False))
        ):
            return await original_open(self, sig, *args, **kwargs)

        symbol = str(getattr(sig, "symbol", "") or "UNKNOWN")
        try:
            decision = await evaluate_candidate(self, sig)
        except Exception as exc:
            # Feasibility is not an execution authority. If it cannot prove a
            # minimum-order violation, preserve the existing downstream
            # fail-closed chain rather than stealing another gate's authority.
            log.warning(
                "[MIN_ORDER_FEASIBILITY] symbol=%s result=DEFER "
                "reason=evaluation_%s early_gate_only=true execution_authorized=false "
                "thresholds_unchanged=true leverage_unchanged=true",
                symbol,
                type(exc).__name__,
            )
            return await original_open(self, sig, *args, **kwargs)

        detail = decision.detail or {}
        if not decision.allowed:
            log.warning(
                "[MIN_ORDER_FEASIBILITY] symbol=%s result=BLOCK reason=%s "
                "risk_budget=%s min_valid_qty=%s risk_at_min_valid_qty=%s "
                "required_equity_at_min_valid_qty=%s binding=%s "
                "candidate_only=true nexus_called=false quantity_raised=false "
                "thresholds_unchanged=true leverage_unchanged=true",
                symbol,
                decision.reason,
                detail.get("risk_budget", "NA"),
                detail.get("min_valid_qty", "NA"),
                detail.get("risk_at_min_valid_qty", "NA"),
                detail.get("required_equity_at_min_valid_qty", "NA"),
                detail.get("binding", "NA"),
            )
            return None

        if not decision.proven:
            log.info(
                "[MIN_ORDER_FEASIBILITY] symbol=%s result=DEFER reason=%s "
                "binding=%s early_gate_only=true execution_authorized=false "
                "thresholds_unchanged=true leverage_unchanged=true",
                symbol,
                decision.reason,
                detail.get("binding", "NA"),
            )
            return await original_open(self, sig, *args, **kwargs)

        log.info(
            "[MIN_ORDER_FEASIBILITY] symbol=%s result=PASS reason=%s "
            "risk_budget=%s min_valid_qty=%s risk_at_min_valid_qty=%s "
            "early_gate_only=true execution_authorized=false "
            "thresholds_unchanged=true leverage_unchanged=true",
            symbol,
            decision.reason,
            detail.get("risk_budget", "NA"),
            detail.get("min_valid_qty", "NA"),
            detail.get("risk_at_min_valid_qty", "NA"),
        )
        return await original_open(self, sig, *args, **kwargs)

    TradingEngine._open = _open_with_min_order_feasibility
    TradingEngine._min_order_feasibility_installed = True
    log.warning(
        "[MIN_ORDER_FEASIBILITY] installed=true stage=BEFORE_NEXUS "
        "authority=minimum_order_risk_proof_only final_sizing_authority=RiskManagerV3 "
        "unknown_or_other_gate=DEFER quantity_raised=false thresholds_unchanged=true "
        "leverage_unchanged=true execution_effect=BLOCK_PROVABLY_INEXECUTABLE_CANDIDATE_ONLY"
    )
