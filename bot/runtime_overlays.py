"""Final runtime compatibility installer.

Most execution/decision hardenings are installed explicitly by
``bot.runtime_bootstrap``. This final layer keeps passive funnel observability,
installs the definitive headline-semantic classifier, activates the narrow
NEXUS HTF-regime transition consistency guard, applies the stop-only CROSS
geometry target policy, and delegates operator-requested LIVE policies to their
isolated hardening modules.

The overlay itself contains no direct class assignments; no class monkey patches remain
in this compatibility module. Class hardening implementation details stay isolated
behind explicit ``install`` calls, consistent with the existing bootstrap pattern.
"""
from __future__ import annotations


def install(TradingEngine, log) -> None:
    """Install passive observability plus final delegated policy semantics."""
    from bot import auth_log_redaction
    from bot import state_authority_observability
    from bot import funnel_metrics as fm
    from bot import score as scoring
    from bot import strategy
    from bot import engine as core_engine
    from bot import derivatives_news_freshness_hardening as derivatives_hardening
    from bot import news_semantic_hardening as news_semantics
    from bot import nexus_ai
    from bot import nexus_regime_transition_consistency as regime_transition
    from bot import kucoin_contract_risk_hardening as cross_risk_hardening
    from bot import cross_geometry_target_policy as cross_target_policy
    from bot import cross_portfolio_stress
    from bot import operator_runtime_policy
    from bot import final_sizing_invariants
    from bot import pilot_risk_cap_hardening as pilot_cap
    from bot import exit_policy_telemetry
    from bot import operator_loss_policy
    from bot import entry_type_shadow_overlay
    from bot import scoring_safety_hardening
    from bot import trailing_safety_hardening
    from bot import market_data_integrity
    from bot import volume_ratio_diagnostics
    from bot import market_viability_fail_closed
    from bot import restart_opening_order_lineage
    from bot import exit_geometry_durability
    from bot import trade_lifecycle
    from bot import post_trade_forensics
    from bot import daily_pnl_estimate_lineage_hardening
    from bot import durable_daily_pnl
    from bot import order_visibility_race_hardening
    from bot import policy_log_throttle
    from bot import partial_tp_execution_hardening
    from bot import exchange_accounting_evidence
    from bot import daily_pnl_exchange_reconciliation
    from bot import daily_stop_override_telemetry
    from bot import external_origin_runtime
    from bot import market_risk_runtime
    from bot import market_risk_coverage_observability
    from bot import entry_latency_observability
    from bot import runtime_contract_guard
    from bot.pilot import PilotGuard
    from bot.kucoin import KuCoinClient, TAKER_FEE

    # Install before any KuCoinClient instance is created by FastAPI lifespan.
    # This is logging-only and cannot affect exchange request semantics.
    auth_log_redaction.install(log)
    state_authority_observability.log_database_authority(log)

    fm.install(log)
    news_semantics.install(scoring, derivatives_hardening, log)

    scoring_safety_hardening.install(strategy, log)
    trailing_safety_hardening.install(core_engine.Position, strategy.cfg, log)
    volume_ratio_diagnostics.install(strategy.Analyzer, market_data_integrity, log)
    entry_type_shadow_overlay.install(strategy.Analyzer, strategy, log)
    market_viability_fail_closed.install(TradingEngine, strategy.cfg, log)

    order_visibility_race_hardening.install(KuCoinClient, log)
    partial_tp_execution_hardening.install(TradingEngine, KuCoinClient, TAKER_FEE, log)
    cross_target_policy.install(cross_risk_hardening, log)

    operator_runtime_policy.install(TradingEngine, policy_log_throttle.wrap(log))
    final_sizing_invariants.install(core_engine, pilot_cap, log)
    exit_policy_telemetry.install(TradingEngine, log)
    operator_loss_policy.install(TradingEngine, log)

    daily_stop_override_telemetry.install(core_engine, log)
    market_risk_coverage_observability.install(market_risk_runtime, log)
    entry_latency_observability.install(log)
    restart_opening_order_lineage.install(TradingEngine, log)
    exit_geometry_durability.install(TradingEngine, log)
    trade_lifecycle.install(TradingEngine, log)
    post_trade_forensics.install(
        TradingEngine, core_engine.Position, strategy.cfg, TAKER_FEE, log
    )
    daily_pnl_estimate_lineage_hardening.install(durable_daily_pnl, log)
    daily_pnl_exchange_reconciliation.install(exchange_accounting_evidence, log)
    external_origin_runtime.install(TradingEngine, exchange_accounting_evidence, log)
    regime_transition.install(nexus_ai, log)
    cross_portfolio_stress.install(TradingEngine, log)

    runtime_contract_guard.install(
        TradingEngine, PilotGuard, nexus_ai, core_engine, log
    )

    log.info(
        "[RUNTIME_OVERLAYS] authentication-log redaction, database-authority observability, passive funnel and entry-latency observability, "
        "scoring/trailing safety, entry-type shadow diagnostics, volume-ratio diagnostics, "
        "fail-closed market viability, KuCoin order-visibility race hardening, fail-closed "
        "partial-TP execution, drawdown hard-gate with explicit override, final risk-authoritative "
        "sizing invariants, truthful daily-stop override telemetry, market-risk coverage telemetry, "
        "restart opening-order lineage recovery, post-trade forensics, direct-close daily-PnL "
        "estimate lineage, confirmed daily-PnL reconciliation, durable external-origin telemetry, "
        "final headline semantics, stop-only CROSS target policy, fail-closed CROSS portfolio "
        "stop-stress gate, startup readiness final notification via operator run owner, delegated operator margin/drawdown/exit policy, strict NEXUS "
        "regime-transition consistency and final runtime ownership contract are active; "
        "thresholds_unchanged=true leverage_unchanged=true railway_variables_unchanged=true"
    )
