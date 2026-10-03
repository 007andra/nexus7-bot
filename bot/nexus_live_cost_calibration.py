"""Live NEXUS execution-cost calibration for BGX.

This hardening changes only the cost inputs used by NEXUS expected-value math.
It does NOT change score thresholds, R:R thresholds, leverage, risk sizing,
position limits, order routing, or any execution permission.

Cost inputs come from the per-candidate ``bot.execution_cost`` snapshot, the
same one used by the technical-policy R:R diagnostics, RiskManagerV3 sizing and
the projected-loss ceiling, so every layer reports the same economics for the
same candidate. Fees are exchange-aware (Binance ``/fapi/v1/commissionRate``;
KuCoin ``/api/v1/trade-fees``). When live data are unavailable the fallback is
never below the legacy NEXUS assumptions (6 bps taker; static per-symbol
slippage >= 5 bps). If snapshot construction itself fails, the exact legacy
6 bps / 5 bps context is used and labelled ``legacy_fallback``.
"""
from __future__ import annotations

import contextvars
import functools
from dataclasses import dataclass

from bot import execution_cost
from bot.kucoin_execution_model import (
    DEFAULT_SLIPPAGE,
    DEFAULT_TAKER_FEE,
)


@dataclass(frozen=True)
class NexusCostContext:
    symbol: str
    taker_fee: float
    slippage: float
    fee_source: str
    slippage_source: str
    spread_bps: float | None = None
    snapshot: execution_cost.ExecutionCostSnapshot | None = None


_COST_CONTEXT: contextvars.ContextVar[NexusCostContext | None] = contextvars.ContextVar(
    "bgx_nexus_cost_context", default=None
)
# Shared with bot.execution_cost so there is exactly one fee cache.
_FEE_CACHE = execution_cost._FEE_CACHE
_FEE_CACHE_TTL_S = execution_cost.FEE_CACHE_TTL_S


def slippage_from_ticker(ticker: dict | None, symbol: str) -> tuple[float, str, float | None]:
    """Compatibility view over ``execution_cost.ticker_slippage`` (one formula)."""
    estimate, spread_bps = execution_cost.ticker_slippage(ticker, symbol)
    if estimate is None:
        return DEFAULT_SLIPPAGE, "legacy_fallback", None
    return estimate, "ticker_half_spread_plus_impact", spread_bps


async def build_cost_context(engine, sig) -> NexusCostContext:
    """Build read-only NEXUS cost inputs from the shared candidate snapshot."""
    snap, _reused = await execution_cost.snapshot_for(engine, sig)
    return NexusCostContext(
        symbol=snap.symbol,
        taker_fee=snap.taker_fee,
        slippage=snap.one_way_slippage,
        fee_source=snap.fee_source,
        slippage_source=snap.slippage_source,
        spread_bps=snap.spread_bps,
        snapshot=snap,
    )


def _attach_cost_context(decision, ctx: NexusCostContext, log) -> bool:
    """Attach private telemetry when the returned decision supports attributes.

    Production NexusDecision objects support dynamic private attributes. Some
    compatibility/unit-test validators return plain dicts; those must keep their
    exact return value and must never fail because observability cannot attach.
    """
    try:
        setattr(decision, "_bgx_nexus_cost_context", ctx)
        return True
    except (AttributeError, TypeError):
        debug = getattr(log, "debug", None)
        if callable(debug):
            debug(
                "[NEXUS_COST] context_handoff_unsupported symbol=%s decision_type=%s "
                "decision_effect=NONE execution_effect=NONE",
                ctx.symbol,
                type(decision).__name__,
            )
        return False


def install(TradingEngine, nexus_ai, log) -> None:
    """Install fail-safe context propagation around the existing NEXUS gate."""
    if getattr(TradingEngine, "_bgx_nexus_cost_calibration_installed", False):
        return

    original_validate = TradingEngine._nexus_validate
    original_expected_value = nexus_ai.expected_value

    @functools.wraps(original_expected_value)
    def calibrated_expected_value(
        win_prob: float,
        entry: float,
        sl: float,
        tp: float,
        taker_fee: float = DEFAULT_TAKER_FEE,
        slippage: float = DEFAULT_SLIPPAGE,
    ) -> dict:
        ctx = _COST_CONTEXT.get()
        if ctx is None:
            return original_expected_value(
                win_prob, entry, sl, tp, taker_fee=taker_fee, slippage=slippage
            )

        result = original_expected_value(
            win_prob,
            entry,
            sl,
            tp,
            taker_fee=ctx.taker_fee,
            slippage=ctx.slippage,
        )
        if isinstance(result, dict):
            result = dict(result)
            result["taker_fee"] = round(ctx.taker_fee, 8)
            result["slippage"] = round(ctx.slippage, 8)
            result["cost_source"] = f"{ctx.fee_source}+{ctx.slippage_source}"
            result["spread_bps"] = None if ctx.spread_bps is None else round(ctx.spread_bps, 4)
            if ctx.snapshot is not None:
                result["cost_snapshot_id"] = ctx.snapshot.snapshot_id
                result["candidate_id"] = ctx.snapshot.candidate_id
        return result

    @functools.wraps(original_validate)
    async def calibrated_validate(self, sig):
        try:
            ctx = await build_cost_context(self, sig)
        except Exception as exc:
            ctx = NexusCostContext(
                symbol=str(getattr(sig, "symbol", "UNKNOWN")),
                taker_fee=DEFAULT_TAKER_FEE,
                slippage=DEFAULT_SLIPPAGE,
                fee_source="legacy_fallback",
                slippage_source="legacy_fallback",
                spread_bps=None,
            )
            log.warning(
                "[NEXUS_COST] symbol=%s calibration_error=%s fallback=true "
                "thresholds_unchanged=true",
                ctx.symbol,
                type(exc).__name__,
            )

        token = _COST_CONTEXT.set(ctx)
        try:
            log.info(
                "[NEXUS_COST] symbol=%s taker_bps=%.3f slippage_bps=%.3f "
                "spread_bps=%s fee_source=%s slippage_source=%s %s "
                "thresholds_unchanged=true leverage_unchanged=true",
                ctx.symbol,
                ctx.taker_fee * 10_000.0,
                ctx.slippage * 10_000.0,
                "NA" if ctx.spread_bps is None else f"{ctx.spread_bps:.3f}",
                ctx.fee_source,
                ctx.slippage_source,
                ctx.snapshot.log_fields() if ctx.snapshot is not None
                else "cost_snapshot_id=NA",
            )
            decision = await original_validate(self, sig)

            # Runtime post-decision observability executes after this wrapper
            # returns and after the ContextVar is reset. Production decisions
            # therefore carry the exact frozen context as private telemetry.
            # NexusDecision.to_dict()/asdict does not serialize dynamic attrs.
            _attach_cost_context(decision, ctx, log)
            return decision
        finally:
            _COST_CONTEXT.reset(token)

    nexus_ai.expected_value = calibrated_expected_value
    TradingEngine._nexus_validate = calibrated_validate
    TradingEngine._bgx_nexus_cost_calibration_installed = True

    log.warning(
        "[NEXUS_COST_CALIBRATION] installed cost_authority=execution_cost.snapshot "
        "actual_taker_fee=exchange_aware ticker_spread_slippage=true "
        "fee_cache_ttl_s=3600 fallback_taker_bps_min=6 fallback_slippage=static_symbol "
        "rr_threshold_unchanged=true score_threshold_unchanged=true "
        "leverage_unchanged=true"
    )