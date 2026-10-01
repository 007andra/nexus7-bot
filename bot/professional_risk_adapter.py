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
import os
from typing import Any

from bot.config import cfg
from bot import execution_cost
from bot.logger import log
from bot.professional_risk import CapitalState
from bot.risk_manager_v3 import RiskManagerV3


@dataclass(frozen=True)
class PlannedRisk:
    entry: float
    stop: float
    risk_pct: float
    # From the candidate's execution_cost snapshot when available.
    taker_fee: float | None = None
    slippage_allowance: float | None = None
    cost_snapshot_id: str = "none"

    def validate(self) -> "PlannedRisk":
        values = (self.entry, self.stop, self.risk_pct)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("planned risk contains non-finite value")
        if self.entry <= 0 or self.stop <= 0 or self.entry == self.stop:
            raise ValueError("planned entry/stop geometry is invalid")
        if not 0 < self.risk_pct <= 1:
            raise ValueError("planned risk_pct must be in (0,1]")
        for cost in (self.taker_fee, self.slippage_allowance):
            if cost is not None and (not math.isfinite(float(cost)) or not 0 <= cost < 0.05):
                raise ValueError("planned cost outside [0,0.05)")
        return self

    def fee_rate(self) -> float:
        """Snapshot fee, else the conservative exchange-aware fallback."""
        return float(self.taker_fee) if self.taker_fee is not None else execution_cost.fallback_taker_fee()

    def slippage(self) -> float:
        """Never below the configured stress allowance NEXUS_EXPECTED_SLIPPAGE_PCT."""
        configured = float(os.environ.get("NEXUS_EXPECTED_SLIPPAGE_PCT", "0.001"))
        return max(configured, float(self.slippage_allowance or 0.0))


class ProfessionalRiskAdapter:
    """Delegate legacy risk APIs while making new-entry sizing stop-aware.

    Existing engine code continues to read/update ``balance``, ``drawdown`` and
    other legacy attributes through delegation. Only new-entry sizing and its
    operator-facing initialization telemetry are specialized here.
    ``RiskManagerV3`` remains side-effect-free and uses a full ``CapitalState``.
    """

    __slots__ = ("_legacy", "_v3", "_plans", "_last_sizing")

    def __init__(self, legacy: Any) -> None:
        # Keep ordinary self assignments so the repository's static startup
        # self-check can prove these required attributes are initialized.
        self._legacy = legacy
        self._v3 = RiskManagerV3()
        self._plans = {}
        self._last_sizing = {}

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

    def set_plan(self, *, symbol: str, entry: float, stop: float, risk_pct: float,
                 cost_snapshot=None) -> PlannedRisk:
        key = str(symbol)
        if not key:
            raise ValueError("symbol is required")
        if cost_snapshot is not None and getattr(cost_snapshot, "symbol", key) != key:
            raise ValueError("cost snapshot symbol mismatch")
        plan = PlannedRisk(
            float(entry), float(stop), float(risk_pct),
            taker_fee=None if cost_snapshot is None else float(cost_snapshot.taker_fee),
            slippage_allowance=(
                None if cost_snapshot is None else float(cost_snapshot.slippage_allowance)
            ),
            cost_snapshot_id=(
                "none" if cost_snapshot is None else str(cost_snapshot.snapshot_id)
            ),
        ).validate()
        self._plans[key] = plan
        self._last_sizing.pop(key, None)
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

    def _log_sizing_decomposition(self, symbol, entry, stop, instruments, risk_pct,
                                  fee_rate, slippage) -> None:
        """Explain a zero stop-risk quantity. Observability only: any failure
        here is swallowed and can never change the sizing result."""
        try:
            from bot.sizing_decomposition import decompose, format_log

            capital = self._v3.capital
            detail = decompose(
                info=instruments.get(symbol) or {},
                equity=capital.equity,
                available=capital.available_collateral,
                entry=entry,
                stop=stop,
                risk_pct=risk_pct,
                leverage=float(cfg.LEVERAGE),
                max_margin_pct=float(getattr(cfg, "MAX_MARGIN_PCT", 0.80)),
                fee_rate_per_side=fee_rate,
                slippage_pct=slippage,
            )
            log.warning(format_log(symbol, detail))
        except Exception as exc:  # noqa: BLE001 - telemetry must never affect sizing
            log.warning(
                "[SIZING_DECOMPOSITION] symbol=%s result=UNAVAILABLE error=%s decision_effect=NONE",
                symbol, type(exc).__name__,
            )

    def validate_fresh_executable_risk(
        self, symbol: str, executable_entry: float, qty: float
    ) -> tuple[bool, dict]:
        """Recheck the last V3 risk budget at a fresh executable price.

        The monetary budget is the exact budget produced by the most recent
        RiskManagerV3 sizing call, so leverage never scales the allowed loss.
        Price drift may increase stop distance and fee/slippage dollars; if the
        already-quantized final quantity would now exceed that budget, the
        caller must fail closed before dispatch.
        """
        key = str(symbol)
        state = self._last_sizing.get(key)
        plan = self._plans.get(key)
        if not isinstance(state, dict) or plan is None:
            raise RuntimeError("fresh executable risk has no sizing authority")

        entry = float(executable_entry)
        quantity = float(qty)
        budget = float(state.get("risk_budget", float("nan")))
        stop = float(plan.stop)
        fee_rate = float(state.get("fee_rate_per_side", float("nan")))
        slippage = float(state.get("slippage_pct", float("nan")))
        values = (entry, quantity, budget, stop, fee_rate, slippage)
        if any(not math.isfinite(v) for v in values):
            raise ValueError("fresh executable risk contains non-finite value")
        if entry <= 0 or quantity <= 0 or budget <= 0 or stop <= 0:
            raise ValueError("fresh executable risk contains non-positive value")
        if fee_rate < 0 or slippage < 0:
            raise ValueError("fresh executable risk contains negative costs")

        loss_per_unit = (
            abs(entry - stop)
            + entry * fee_rate * 2.0
            + entry * slippage
        )
        projected = quantity * loss_per_unit
        tolerance = max(1e-12, budget * 1e-6)
        allowed = projected <= budget + tolerance
        return allowed, {
            "risk_budget": budget,
            "projected_loss": projected,
            "headroom_usdt": budget - projected,
            "qty": quantity,
            "executable_entry": entry,
            "stop": stop,
            "fee_rate_per_side": fee_rate,
            "slippage_pct": slippage,
            "effective_risk_pct": float(state.get("effective_risk_pct", float("nan"))),
            "cost_snapshot_id": str(state.get("cost_snapshot_id", "none")),
        }

    def size(self, symbol: str, entry: float, instruments: dict,
             size_mult: float = 1.0, open_positions: dict | None = None) -> float:
        """Return stop-risk-sized base quantity or fail closed with zero.

        ``open_positions`` is intentionally not used for margin arithmetic here:
        the authenticated ``CapitalState`` already separates available
        collateral, position margin, and order margin. The argument remains in
        the signature solely for compatibility with the canonical engine.
        """
        del open_positions
        key = str(symbol)
        plan = self._plans.get(key)
        if plan is None:
            log.critical("[RISK_V3_CORE] %s blocked: planned geometry unavailable", key)
            return 0.0
        if not math.isclose(float(entry), plan.entry, rel_tol=1e-9, abs_tol=1e-12):
            log.critical(
                "[RISK_V3_CORE] %s blocked: entry mismatch planned=%.12g current=%.12g",
                key, plan.entry, float(entry),
            )
            return 0.0
        if not self._v3.confirmed:
            log.critical("[RISK_V3_CORE] %s blocked: capital state unconfirmed", key)
            return 0.0

        try:
            multiplier = float(size_mult)
            if not math.isfinite(multiplier) or multiplier <= 0:
                raise ValueError("size_mult must be positive and finite")
            effective_risk_pct = plan.risk_pct * multiplier
            if not 0 < effective_risk_pct <= 1:
                raise ValueError("effective risk_pct outside (0,1]")

            self._reconcile_latest_available()
            if not self._v3.confirmed:
                raise RuntimeError("capital state invalidated during reconciliation")

            expected_slippage = plan.slippage()
            fee_rate = plan.fee_rate()
            sizing = self._v3.size_for_stop(
                symbol=key,
                entry=float(entry),
                stop=plan.stop,
                instruments=instruments,
                risk_pct=effective_risk_pct,
                leverage=float(cfg.LEVERAGE),
                fee_rate_per_side=fee_rate,
                expected_slippage_pct=expected_slippage,
            )
            self._last_sizing[key] = {
                "risk_budget": float(sizing.risk_budget),
                "sized_qty": float(sizing.qty),
                "sized_entry": float(entry),
                "stop": float(plan.stop),
                "fee_rate_per_side": float(fee_rate),
                "slippage_pct": float(expected_slippage),
                "effective_risk_pct": float(effective_risk_pct),
                "cost_snapshot_id": str(plan.cost_snapshot_id),
            }
            log.info(
                "[RISK_V3_CORE] symbol=%s qty=%.12g risk_budget=%.6f "
                "projected_stop_loss=%.6f stop_distance_pct=%.6f "
                "required_margin=%.6f binding=%s taker_bps=%.3f slippage_allowance_bps=%.3f "
                "cost_snapshot_id=%s cost_purpose=RISK_BUDGET_STRESS decision_effect=NONE",
                key, sizing.qty, sizing.risk_budget,
                sizing.projected_stop_loss, sizing.stop_distance_pct,
                sizing.required_margin, sizing.binding_constraint,
                fee_rate * 1e4, expected_slippage * 1e4, plan.cost_snapshot_id,
            )
            if float(sizing.qty) <= 0:
                self._log_sizing_decomposition(
                    key, float(entry), plan.stop, instruments, effective_risk_pct,
                    fee_rate, expected_slippage,
                )
            return float(sizing.qty)
        except Exception as exc:
            log.critical(
                "[RISK_V3_CORE] %s blocked: %s",
                key, type(exc).__name__,
            )
            return 0.0
