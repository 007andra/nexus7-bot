"""Explicit runtime bootstrap for the NEXUS-7 hardening stack.

Phase 2 of sitecustomize consolidation: startup remains fail-closed and the
hardening installation order stays explicit, while transitional observability
wrappers live in ``bot.runtime_overlays`` instead of inline here.

Strategy thresholds are unchanged. Leaving validation-held SHADOW mode requires
all explicit controlled-pilot acknowledgements and installs a separate LIVE
preflight runtime before any candidate can reach the normal execution path.
"""
from __future__ import annotations

import builtins


def install() -> None:
    if getattr(builtins, "_nexus_runtime_bootstrap_installed", False):
        return

    from bot import pilot_release_control as _pilot_release_control
    from bot import exchange as _exchange
    from bot import shadow_startup_logging as _shadow_startup_logging
    _shadow_startup_logging.install_preimport()

    from bot.engine import TradingEngine
    from bot.integrity import IntegrityGuard
    from bot import runtime_hardening as _rh
    from bot import runtime_overlays as _runtime_overlays
    from bot import runtime_truth_hooks as _runtime_truth_hooks
    from bot import paper_e2e as _paper_e2e
    from bot import paper_lifecycle as _paper_lifecycle
    from bot import paper_wallet as _paper_wallet
    from bot import paper_validation_reset as _paper_validation_reset
    from bot import pretrade_hardening as _pretrade_hardening
    from bot import rr_precision_hardening as _rr_precision_hardening
    from bot import scan_summary_hardening as _scan_summary_hardening
    from bot import validation_safety_lock as _validation_safety_lock
    from bot import pilot_live_runtime as _pilot_live_runtime
    from bot import pilot_risk_cap_hardening as _pilot_risk_cap_hardening
    from bot import legacy_pretrade_advisory as _legacy_pretrade_advisory
    from bot import operational_incident_recovery as _operational_incident_recovery
    from bot import liquidation_override_guard as _liquidation_override_guard
    from bot import kucoin_contract_risk_hardening as _kucoin_contract_risk_hardening
    from bot import instrument_readiness_guard as _instrument_readiness_guard
    from bot import viability_fail_closed_hardening as _viability_fail_closed_hardening
    from bot import nexus_decision_dedupe as _nexus_decision_dedupe
    from bot import nexus_grade_display as _nexus_grade_display
    from bot import nexus_terminal_notifications as _nexus_terminal_notifications
    from bot import nexus_optional_evidence as _nexus_optional_evidence
    from bot import nexus_structure_semantics as _nexus_structure_semantics
    from bot import nexus_decision_consistency as _nexus_decision_consistency
    from bot import nexus_live_cost_calibration as _nexus_live_cost_calibration
    from bot import min_order_feasibility as _min_order_feasibility
    from bot import nexus_prefinal_veto_observability as _nexus_prefinal_veto_observability
    from bot import nexus_shadow_research_runtime as _nexus_shadow_research_runtime
    from bot import daily_stop_observability as _daily_stop_observability
    from bot import daily_stop_runtime_hardening as _daily_stop_runtime_hardening
    from bot import selfcheck_entrypoint_hardening as _selfcheck_entrypoint_hardening
    from bot import selfcheck as _selfcheck
    from bot import news_context_hardening as _news_context_hardening
    from bot import derivatives_news_freshness_hardening as _derivatives_news_freshness_hardening
    from bot import market_risk_runtime as _market_risk_runtime
    from bot import market_risk_binance_fallback as _market_risk_binance_fallback
    from bot import market_risk_news_bridge as _market_risk_news_bridge
    from bot import balance_observability as _balance_observability
    from bot import account_balance_observability as _account_balance_observability
    from bot import stagnation_time_hardening as _stagnation_time_hardening
    from bot import pilot_readiness_observability as _pilot_readiness_observability
    from bot import pilot_exposure_capacity as _pilot_exposure_capacity
    from bot import pilot_submission_counter as _pilot_submission_counter
    from bot import live_execution_fence as _live_execution_fence
    from bot import shadow_mode_observability as _shadow_mode_observability
    from bot import startup_position_unit_hardening as _startup_position_unit_hardening
    from bot import prelive_protection_failclosed as _prelive_protection_failclosed
    from bot import binance_protection_failclosed as _binance_protection_failclosed
    from bot import binance_leverage_reconcile as _binance_leverage_reconcile
    from bot import binance_legacy_algo_inventory as _binance_legacy_algo_inventory
    from bot import pilot_external_position_guard as _pilot_external_position_guard
    from bot import kucoin_price_tick_hardening as _kucoin_price_tick_hardening
    from bot import kucoin_cross_margin_order as _kucoin_cross_margin_order
    from bot import kucoin_fill_normalization as _kucoin_fill_normalization
    from bot import kucoin_native_tpsl as _kucoin_native_tpsl
    from bot import durable_reconcile_hardening as _durable_reconcile_hardening
    from bot import durable_execution as _durable_execution
    from bot import order_state as _order_state
    from bot import integrity_external_coexistence_runtime as _integrity_external_coexistence_runtime
    from bot import adaptive_mtf_entry as _adaptive_mtf_entry
    from bot import pullback_confirmation_hardening as _pullback_confirmation_hardening
    from bot import market_data_integrity as _market_data_integrity
    from bot import strategy as _strategy
    from bot import pilot as _pilot
    from bot import score as _score
    from bot import kucoin as _kucoin
    from bot import liquidation as _liquidation
    from bot import notifier as _notifier
    from bot import nexus_types as _nexus_types
    from bot import nexus_ai as _nexus_ai
    from bot.logger import log as _log

    _rh.install_database_schema_fix(_log)
    _rh.install_telegram_fix(_log)
    _rh.install_paper_execution_fix(_log)
    _pretrade_hardening.install(_log)
    _rr_precision_hardening.install(_log)
    _scan_summary_hardening.install(_log)
    _daily_stop_observability.install(_log)
    _daily_stop_runtime_hardening.install(TradingEngine, _log)
    _selfcheck_entrypoint_hardening.install(_selfcheck, _log)
    _paper_e2e.install(_log)
    _paper_wallet.install(_log)
    _paper_lifecycle.install(_log)
    _paper_validation_reset.install(_log)
    # Must precede restart ownership guard installation: that guard captures the
    # current startup loader and validates its reconstructed local quantity.
    _startup_position_unit_hardening.install(TradingEngine, _log)
    if _exchange.is_kucoin():
        _prelive_protection_failclosed.install(TradingEngine, _kucoin, _log)
        _pilot_external_position_guard.install(
            TradingEngine, _log, exchange_name="kucoin"
        )
        _kucoin_price_tick_hardening.install(_kucoin.KuCoinClient, _log)
        _kucoin_cross_margin_order.install(_kucoin.KuCoinClient, _log)
        _kucoin_fill_normalization.install(_kucoin.KuCoinClient, _kucoin, _log)
        _kucoin_native_tpsl.install(_kucoin.KuCoinClient, _kucoin, _log)
    else:
        # Install protection first. The ownership wrapper installed immediately
        # after it remains the outer/final boundary, so external/manual
        # positions can never reach repair or emergency-close mutations.
        _binance_protection_failclosed.install(TradingEngine, _log)
        _pilot_external_position_guard.install(
            TradingEngine, _log, exchange_name="binance"
        )
        _binance_leverage_reconcile.install(TradingEngine, _log)
        # P1-DEPLOY-1: read-only account-wide inventory, once, after connect.
        _binance_legacy_algo_inventory.install(TradingEngine, _log)
        _log.info(
            "[BINANCE_MIGRATION] kucoin_price_tick=NOT_NEEDED_NATIVE_DECIMAL "
            "kucoin_fill_normalization=NOT_NEEDED_BASE_ASSET_NATIVE "
            "external_position_guard=INSTALLED "
            "execution_effect=NONE"
        )
    _pilot_submission_counter.install(_exchange.ExchangeClient, _log)
    _live_execution_fence.install(
        _exchange.ExchangeClient, _exchange, _log
    )
    _durable_reconcile_hardening.install(_durable_execution, _order_state, _log)

    # One contract renders both the release line and the Binance accounting
    # line, so they can never contradict each other (audit P0-5).
    from bot import runtime_release_contract as _release_contract
    _contract = _release_contract.current()
    if _contract.release_authorized:
        _pilot_live_runtime.install(TradingEngine, _log)
        _pilot_risk_cap_hardening.install(TradingEngine, _log)
        _operational_incident_recovery.install(TradingEngine, _log)
        _log.critical(_contract.release_log())
    else:
        _validation_safety_lock.install(_log)
        _log.warning(_contract.release_log())

    _liquidation_override_guard.install(_log)
    if _exchange.is_kucoin():
        _kucoin_contract_risk_hardening.install(
            _kucoin.KuCoinClient, TradingEngine, _score, _liquidation, _log
        )
    else:
        _log.info(
            "[BINANCE_MIGRATION] kucoin_cross_mmr_model=SKIPPED "
            "binance_cross_stress_authority=runtime_overlays "
            "execution_effect=NONE"
        )
    _instrument_readiness_guard.install(_log)
    _viability_fail_closed_hardening.install(TradingEngine, _log)
    _news_context_hardening.install(_log)
    _derivatives_news_freshness_hardening.install(TradingEngine, _score, _log)
    _market_risk_runtime.install(_pilot.PilotGuard, _score, _log)
    _market_risk_binance_fallback.install(_score, _market_risk_runtime, _log)
    _market_risk_news_bridge.install(_news_context_hardening, _market_risk_runtime, _log)
    _balance_observability.install(_log)
    _account_balance_observability.install(_log)
    _pilot_exposure_capacity.install(_pilot.PilotGuard, _log)
    _integrity_external_coexistence_runtime.install(IntegrityGuard, _log)
    _stagnation_time_hardening.install(TradingEngine, _log)
    _pilot_readiness_observability.install(_log)
    _shadow_mode_observability.install(_log)
    _nexus_decision_dedupe.install(_log)
    _nexus_grade_display.install(_notifier, _log)
    _nexus_optional_evidence.install(_nexus_ai, _log)
    _nexus_structure_semantics.install(_nexus_ai, _log)
    _nexus_decision_consistency.install(_nexus_ai, _log)
    _nexus_live_cost_calibration.install(TradingEngine, _nexus_ai, _log)
    _min_order_feasibility.install(TradingEngine, _log)
    _nexus_prefinal_veto_observability.install(_nexus_ai, _log)
    _nexus_terminal_notifications.install(TradingEngine, _notifier, _nexus_types, _log)
    _nexus_shadow_research_runtime.install(TradingEngine, _log)
    if not _exchange.is_kucoin():
        # SHADOW ONLY: separate /public @bookTicker feed + passive STATIC vs
        # LIVE_BBO cost comparison. Returns the champion decision unchanged;
        # never read by any trading path (decision_effect=NONE).
        from bot import binance as _binance_client
        from bot import bbo_cost_shadow_runtime as _bbo_cost_shadow_runtime
        _bbo_cost_shadow_runtime.install(TradingEngine, _binance_client.BinanceClient, _log)

    # Runtime-truth stage wrappers are deliberately interleaved with the
    # existing strategy wrapper installation order. Each wrapper records the
    # result it receives and returns the exact same object unchanged.
    _runtime_truth_hooks.install_canonical_stage(_strategy.Analyzer)
    _adaptive_mtf_entry.install(_strategy.Analyzer, _strategy, _log)
    _runtime_truth_hooks.install_adaptive_stage(_strategy.Analyzer)
    _pullback_confirmation_hardening.install(_strategy.Analyzer, _log)
    _runtime_truth_hooks.install_pullback_stage(_strategy.Analyzer)
    # This wrapper must be immediately inside market_data_integrity so it sees
    # the exact timestamp-prepared series delegated by that outer authority.
    _runtime_truth_hooks.install_analysis_authority_inner(_strategy.Analyzer)
    if _exchange.is_kucoin():
        _market_data_integrity.install(
            _kucoin.KuCoinClient, _strategy.Analyzer, _log
        )
    else:
        _log.info(
            "[BINANCE_MARKET_DATA] native_kline_parser=true "
            "kucoin_volume_rewrite=false execution_effect=NONE"
        )

    if _pilot_release_control.live_pilot_release_authorized():
        _legacy_pretrade_advisory.install(TradingEngine, _score, _log)

    _runtime_overlays.install(TradingEngine, _log)

    # Transport/cache hooks are installed after market_data_integrity so the WS
    # cache observer sees the original parsed KuCoin payload before the
    # integrity wrapper rewrites its activity field, while cache hashes are
    # sampled only after the existing mutation path has completed.
    if _exchange.is_kucoin():
        _runtime_truth_hooks.install_transport_and_cache(_kucoin.KuCoinClient)
    else:
        _log.info(
            "[BINANCE_RUNTIME_TRUTH] transport_hook=DEFERRED "
            "core_stage_truth_preserved=true"
        )
    _runtime_truth_hooks.install_marketdata_outer(_strategy.Analyzer, TradingEngine)
    _runtime_truth_hooks.install_engine_and_downstream(
        TradingEngine, _exchange.ExchangeClient, _score, _nexus_ai
    )

    builtins._nexus_runtime_bootstrap_installed = True
    _log.info("[RUNTIME_BOOTSTRAP] installed centralized hardening bootstrap")
