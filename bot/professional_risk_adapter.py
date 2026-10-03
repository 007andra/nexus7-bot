"""Explicit stop-risk sizing adapter for the executable NEXUS runtime.

The canonical engine still expects the legacy ``RiskManager`` interface. This
adapter preserves that interface by delegation, but makes ``size()`` use
``RiskManagerV3`` once a validated signal geometry and capital snapshot have
been prepared by ``NexusRuntimeEngine``.

No exchange mutation, release change, or execution authorization lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from bot.config import cfg
from bot.logger import log
from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3


@dataclass(frozen=True)
class PlannedRisk:
    entry: float
    stop: float
    risk_pct: float

    def validate(self) -> "PlannedRisk":
        values = (self.entry, self.stop, self.risk_pct)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("planned risk contains non-finite value")
        if self.entry <= 0 or self.stop <= 0 or self.entry == self.stop:
            raise ValueError("planned entry/stop geometry is invalid")
        if not 0 < self.risk_pct <= 1:
            raise ValueError("planned risk_pct must be in (0,1]")
        return self


class ProfessionalRiskAdapter:
    """Delegate legacy risk APIs while making new-entry sizing stop-aware.

    Existing engine code continues to read/update ``balance``, ``drawdown`` and
    other legacy attributes through delegation. Only new-entry sizing and its
    operator-facing initialization telemetry are specialized here.
    ``RiskManagerV3`` remains side-effect-free and uses a full ``CapitalState``.
    """

    __slots__ = ("_legacy", "_v3", "_plans")

    def __init__(self, legacy: Any) -> None:
        # Keep ordinary self assignments so the repository's static startup
        # self-check can prove these required attributes are initialized.
        self._legacy = legacy
        self._v3 = RiskManagerV3()
        self._plans = {}

    def __getattr__(self, name: str):
        return getattr(self._legacy, name)

    def __setattr__(self, name: str, value) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._legacy, name, value)

    def init(self, bal: float):
        """Initialize delegated balance state without legacy risk mislabeling.

        The old ``RiskManager.init`` log called ``LEVERAGE * MAX_RISK_PCT``
        "risk per trade". That number is a notional/buying-power allocation,
        not the maximum loss at the protective stop. Executable NEXUS sizing is
        stop-aware in ``RiskManagerV3``; reporting the legacy quantity as loss
        risk can therefore overstate or understate the real planned exposure.

        State transitions remain equivalent to the legacy initializer: the
        first valid balance initializes peak/drawdown/confirmed state and sets
        ``_ready``. Only the telemetry wording/source is corrected.
        """
        if not bool(getattr(self._legacy, "_ready", False)):
            self._legacy.update(bal)
            self._legacy._ready = True
            log.info(
                "[RISK_V3_CORE] initialized balance=$%.2f leverage=%sx "
                "configured_stop_risk_pct=%.3f%%; actual projected stop loss "
                "is calculated from entry/stop geometry before dispatch",
                float(bal), cfg.LEVERAGE, float(cfg.MAX_RISK_PCT) * 100.0,
            )

    @property
    def professional_snapshot(self):
        return self._v3.snapshot()

    def set_plan(self, *, symbol: str, entry: float, stop: float, risk_pct: float) -> PlannedRisk:
        key = str(symbol)
        if not key:
            raise ValueError("symbol is required")
        plan = PlannedRisk(float(entry), float(stop), float(risk_pct)).validate()
        self._plans[key] = plan
        return plan

    def update_capital(self, capital: CapitalState):
        return self._v3.update_capital(capital)

    def invalidate_capital(self) -> None:
        self._v3.invalidate()

    def _reconcile_latest_available(self) -> None:
        """Use the later legacy balance read as a conservative collateral cap.

        The runtime obtains full account equity/committed-margin semantics from
        ``account-overview`` before sizing. The canonical engine then performs
        its existing fresh ``get_balance`` read immediately before ``size``.
        If that later available balance is lower, keep the lower value without
        changing equity or the risk budget.
        """
        if not self._v3.confirmed:
            return
        if not bool(getattr(self._legacy, "balance_confirmed", False)):
            return
        latest = float(getattr(self._legacy, "balance", 0.0) or 0.0)
        if not math.isfinite(latest) or latest < 0:
            self._v3.invalidate()
            return
        current = self._v3.capital
        available = min(float(current.available_collateral), latest)
        if available == current.available_collateral:
            return
        self._v3.update_capital(CapitalState(
            equity=current.equity,
            available_collateral=available,
            position_margin=current.position_margin,
            order_margin=current.order_margin,
            unrealized_pnl=current.unrealized_pnl,
        ))

    def size_detail(self, symbol: str, entry: float, instruments: dict,
                    size_mult: float = 1.0):
        """F-003 single sizing authority: risk budget -> maximum base quantity.

        Returns ``(StopRiskSizingResult, risk_pct, cost_fraction)``; raises on
        missing plan/capital, entry mismatch or a risk_pct above the structural
        ceiling (never normalized silently).
        """
        from bot.risk_budget import cost_fraction, validate_risk_pct
        from bot.kucoin_execution_model import configured_taker_fee

        key = str(symbol)
        plan = self._plans.get(key)
        if plan is None:
            raise RuntimeError("planned geometry unavailable")
        if not math.isclose(float(entry), plan.entry, rel_tol=1e-9, abs_tol=1e-12):
            raise RuntimeError(
                f"entry mismatch planned={plan.entry:.12g} current={float(entry):.12g}")
        if not self._v3.confirmed:
            raise RuntimeError("capital state unconfirmed")
        multiplier = float(size_mult)
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError("size_mult must be positive and finite")
        effective_risk_pct = validate_risk_pct(plan.risk_pct * multiplier)

        self._reconcile_latest_available()
        if not self._v3.confirmed:
            raise RuntimeError("capital state invalidated during reconciliation")

        # Canonical per-symbol cost: 2 x taker + 2 x adverse slippage (the same
        # model as research/geometry), expressed through the primitive's
        # fee-per-side + total-slippage parameters.
        fee = float(configured_taker_fee())
        total_cost = cost_fraction(key)
        sizing = self._v3.size_for_stop(
            symbol=key,
            entry=float(entry),
            stop=plan.stop,
            instruments=instruments,
            risk_pct=effective_risk_pct,
            leverage=float(cfg.LEVERAGE),
            fee_rate_per_side=fee,
            expected_slippage_pct=max(0.0, total_cost - 2.0 * fee),
        )
        log.info(
            "[RISK_SIZING] symbol=%s qty=%.12g equity=%.6f risk_pct=%.4f risk_budget=%.6f "
            "cost_fraction=%.5f projected_stop_loss=%.6f stop_distance_pct=%.6f "
            "required_margin=%.6f binding=%s rejection=%s",
            key, sizing.qty, self._v3.equity, effective_risk_pct, sizing.risk_budget,
            total_cost, sizing.projected_stop_loss, sizing.stop_distance_pct,
            sizing.required_margin, sizing.binding_constraint,
            sizing.rejection_reason or "-",
        )
        return sizing, effective_risk_pct, total_cost

    def size(self, symbol: str, entry: float, instruments: dict,
             size_mult: float = 1.0, open_positions: dict | None = None) -> float:
        """Return stop-risk-sized base quantity or fail closed with zero.

        ``open_positions`` is intentionally not used for margin arithmetic here:
        the authenticated ``CapitalState`` already separates available
        collateral, position margin, and order margin. The argument remains in
        the signature solely for compatibility with the canonical engine.
        """
        del open_positions
        try:
            sizing, _, _ = self.size_detail(symbol, entry, instruments, size_mult)
            return float(sizing.qty)
        except Exception as exc:
            log.critical(
                "[RISK_V3_CORE] %s blocked: %s",
                str(symbol), type(exc).__name__,
            )
            return 0.0
