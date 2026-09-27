# Full codebase inventory (generated 2026-09-26)

Generated from the real graph, not grep alone: AST import graph (top-level and function-local imports), modules actually loaded by the production bootstrap (`import main_hardened` with sitecustomize) in three configurations (Binance LIVE-shaped with dummy tokens, Binance PAPER, KuCoin PAPER), the transitive lazy-import closure from those loaded sets, direct test imports, and `.github/workflows` references.

RUNTIME_REACHABLE: `IMPORT` = loaded at bootstrap; `LAZY` = reachable through a function-local import from a loaded module; `NONE` = not reachable from production; `ENTRY` = process entrypoint. TEST_REACHABLE = number of test modules importing it directly. ROLE is a name-based classification cross-checked against reachability. ACTION follows the rules in FULL_CODEBASE_AUDIT_2026-09-26.md (DELETE_SAFE only when evidence A–F all hold).

| PATH | LOC | ROLE | RUNTIME_REACHABLE | TEST_REACHABLE | CI_REACHABLE | IMPORTED_BY (prod) | ACTION | EVIDENCE |
|---|---|---|---|---|---|---|---|---|
| .github/NO_RUNTIME_CHANGE_MARKER.txt | 2 | CI | — | n/a | no | — | KEEP |  |
| .github/workflows/ci_attest.yml | 112 | CI | — | n/a | no | — | KEEP |  |
| .github/workflows/oos_real_replay.yml | 105 | CI | — | n/a | yes | — | KEEP |  |
| .github/workflows/oos_replay_diagnostics.yml | 46 | CI | — | n/a | yes | — | KEEP |  |
| .github/workflows/quality.yml | 216 | CI | — | n/a | no | — | KEEP |  |
| .github/workflows/runtime_truth.yml | 47 | CI | — | n/a | yes | — | KEEP |  |
| .github/workflows/security.yml | 69 | CI | — | n/a | no | — | KEEP |  |
| .gitignore | 8 | CONFIG | — | n/a | no | — | KEEP |  |
| .gitleaksignore | 1 | CONFIG | — | n/a | no | — | KEEP |  |
| .railway/promote.txt | 1 | DOC | — | n/a | no | — | KEEP |  |
| CHANGELOG.md | 121 | DOC | — | n/a | no | — | KEEP |  |
| Dockerfile | 30 | CONFIG | — | n/a | no | — | KEEP |  |
| KUCOIN_MIGRATION.md | 73 | DOC | — | n/a | no | — | HISTORICAL | KuCoin migration record; labelled historical |
| META_AUDIT.md | 96 | DOC | — | n/a | no | — | HISTORICAL | labelled historical |
| README.md | 79 | DOC | — | n/a | no | — | STALE | describes Bybit; rewritten in docs commit |
| VERIFICATION_LOG.md | 45 | DOC | — | n/a | no | — | KEEP |  |
| bot/__init__.py | 7 | CORE | — | n/a | no | — | KEEP |  |
| bot/account_balance_observability.py | 100 | OBSERVABILITY | IMPORT | 1 | no | runtime_bootstrap | CONSOLIDATE | overlaps balance_observability |
| bot/account_balance_semantics.py | 82 | CORE | IMPORT | 2 | no | nexus_runtime_engine, pilot_live_runtime, shadow_balance_semantics | KEEP |  |
| bot/account_capital_reader.py | 35 | CORE | IMPORT | 1 | no | nexus_runtime_engine, shadow_connect, shadow_live | KEEP |  |
| bot/accounting_fill_link.py | 205 | CORE | LAZY | 1 | no | exchange_accounting_evidence | KEEP |  |
| bot/adaptive_mtf_calibration.py | 411 | CORE | IMPORT | 3 | no | adaptive_mtf_dedup_shadow, adaptive_mtf_entry | KEEP |  |
| bot/adaptive_mtf_dedup_shadow.py | 188 | SHADOW | IMPORT | 1 | no | adaptive_mtf_entry | KEEP |  |
| bot/adaptive_mtf_entry.py | 275 | CORE | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/atomic_key_value.py | 64 | CORE | IMPORT | 1 | no | drawdown_persistence | KEEP |  |
| bot/auth_log_redaction.py | 62 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/backtest.py | 725 | RESEARCH | IMPORT | 2 | no | engine, nexus_oos_real_replay, nexus_oos_real_replay_corrected, nexus_oos_replay … | KEEP |  |
| bot/balance_observability.py | 110 | OBSERVABILITY | IMPORT | 1 | no | runtime_bootstrap | CONSOLIDATE | overlaps account_balance_observability (both read KuCoin account-overview for logs) |
| bot/binance.py | 2220 | VENUE_BINANCE | IMPORT | 3 | no | binance_cross_portfolio_stress, exchange, execution_cost, pilot … | KEEP |  |
| bot/binance_accounting_evidence.py | 809 | VENUE_BINANCE | IMPORT | 1 | no | capital_flow_reconciliation, engine, runtime_overlays | KEEP |  |
| bot/binance_cross_portfolio_stress.py | 453 | VENUE_BINANCE | IMPORT | 2 | no | engine, runtime_overlays | KEEP |  |
| bot/binance_protection_failclosed.py | 314 | VENUE_BINANCE | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/capital_flow_reconciliation.py | 365 | CORE | IMPORT | 1 | no | pilot_live_runtime | KEEP |  |
| bot/cash_flow_admin.py | 111 | OPERATOR_CLI | CLI (`python -m bot.cash_flow_admin`) | 0 | no | — | KEEP | added by the cash-flow-adjusted drawdown fix |
| bot/cash_flow_ledger.py | 939 | CORE | IMPORT | 2 | no | capital_flow_reconciliation, cash_flow_admin | KEEP | external cash-flow authority (TWR drawdown) |
| bot/ci_deploy_gate.py | 144 | CORE | NONE | 1 | no | — | DEPRECATE | Railway prod config has no pre-deploy command and checkSuites=false (read-only check 2026-09-26): gate not in use; keep until operator decides to re-enable |
| bot/conditional_stop_lifecycle.py | 633 | CORE | LAZY | 1 | no | native_stop_repair, protection_readiness | KEEP |  |
| bot/conditional_stop_protection.py | 253 | CORE | IMPORT | 2 | no | binance_protection_failclosed, conditional_stop_lifecycle, durable_partial_exit, initial_reconciliation … | KEEP |  |
| bot/confidence_outcome_linking.py | 162 | CORE | IMPORT | 1 | no | confidence_outcome_runtime | KEEP |  |
| bot/confidence_outcome_runtime.py | 166 | CORE | IMPORT | 1 | no | nexus_confidence_observability | KEEP |  |
| bot/config.py | 112 | CORE | IMPORT | 22 | no | backtest, binance, binance_cross_portfolio_stress, correlation … | KEEP |  |
| bot/confirmed_rr_exit.py | 91 | CORE | LAZY | 1 | no | durable_partial_exit, engine | KEEP |  |
| bot/core_execution_risk.py | 91 | CORE | LAZY | 1 | no | shadow_live | KEEP |  |
| bot/correlation.py | 142 | CORE | LAZY | 1 | no | main | KEEP |  |
| bot/critical_state.py | 36 | CORE | IMPORT | 1 | no | binance, execution_ownership, kucoin, pilot_submission_counter | KEEP |  |
| bot/cross_geometry_target_policy.py | 98 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/cross_portfolio_stress.py | 389 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP | KuCoin-only CROSS stress (installed only when EXCHANGE=kucoin); Binance twin is binance_cross_portfolio_stress |
| bot/daily_pnl_estimate_lineage_hardening.py | 73 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/daily_pnl_exchange_reconciliation.py | 57 | CORE | IMPORT | 0 | no | runtime_overlays | KEEP |  |
| bot/daily_pnl_storage.py | 69 | CORE | IMPORT | 1 | no | durable_daily_pnl | KEEP |  |
| bot/daily_stop_observability.py | 15 | OBSERVABILITY | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/daily_stop_override_telemetry.py | 92 | OBSERVABILITY | IMPORT | 2 | no | runtime_overlays | KEEP |  |
| bot/daily_stop_runtime_hardening.py | 112 | HARDENING_OVERLAY | IMPORT | 3 | no | runtime_bootstrap | KEEP |  |
| bot/daily_tracker.py | 226 | CORE | IMPORT | 1 | no | engine, scan_summary_hardening | KEEP |  |
| bot/database.py | 771 | CORE | IMPORT | 16 | no | atomic_key_value, backtest, binance_accounting_evidence, capital_flow_reconciliation … | KEEP |  |
| bot/derivatives_news_freshness_hardening.py | 480 | HARDENING_OVERLAY | IMPORT | 2 | no | runtime_bootstrap, runtime_overlays | KEEP |  |
| bot/drawdown_persistence.py | 228 | CORE | IMPORT | 6 | no | capital_flow_reconciliation, exchange_accounting_evidence, nexus_runtime_engine, operational_incident_recovery … | KEEP |  |
| bot/durable_daily_pnl.py | 367 | CORE | IMPORT | 3 | no | daily_pnl_exchange_reconciliation, durable_daily_stop, engine, runtime_overlays | KEEP |  |
| bot/durable_daily_stop.py | 263 | CORE | LAZY | 4 | no | engine, exchange_accounting_evidence, post_trade_forensics | KEEP |  |
| bot/durable_execution.py | 401 | CORE | IMPORT | 5 | no | binance_accounting_evidence, binance_protection_failclosed, durable_live_reconciliation, engine … | KEEP |  |
| bot/durable_live_reconciliation.py | 286 | CORE | IMPORT | 2 | no | durable_execution, pilot_submission_counter, protection_readiness | KEEP |  |
| bot/durable_partial_exit.py | 103 | CORE | LAZY | 1 | no | partial_tp_execution_hardening | KEEP |  |
| bot/durable_reconcile_hardening.py | 198 | HARDENING_OVERLAY | IMPORT | 3 | no | runtime_bootstrap | KEEP |  |
| bot/e2e_validation_contract.py | 97 | CORE | NONE | 1 | no | — | KEEP | CLI + quality.yml test step |
| bot/engine.py | 4130 | CORE | IMPORT | 21 | no | balance_observability, durable_execution, instrument_readiness_guard, nexus_decision_dedupe … | KEEP |  |
| bot/entry_latency_observability.py | 264 | OBSERVABILITY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/entry_type_shadow.py | 118 | SHADOW | IMPORT | 1 | no | entry_type_shadow_overlay | KEEP |  |
| bot/entry_type_shadow_overlay.py | 215 | SHADOW | IMPORT | 0 | no | runtime_overlays | KEEP |  |
| bot/event_intelligence.py | 118 | CORE | LAZY | 2 | no | news_event_observability | KEEP |  |
| bot/exchange.py | 65 | CORE | IMPORT | 2 | no | engine, execution_cost, exit_policy_telemetry, nexus_runtime_engine … | KEEP |  |
| bot/exchange_accounting_evidence.py | 446 | CORE | IMPORT | 2 | no | engine, runtime_overlays | KEEP |  |
| bot/execution_capability.py | 37 | CORE | IMPORT | 2 | no | binance, conditional_stop_lifecycle, execution_ownership, kucoin … | KEEP |  |
| bot/execution_cost.py | 298 | CORE | IMPORT | 1 | no | final_sizing_invariants, nexus_live_cost_calibration, nexus_runtime_engine, operator_loss_policy … | KEEP |  |
| bot/execution_ownership.py | 309 | CORE | LAZY | 8 | no | binance, conditional_stop_lifecycle, durable_live_reconciliation, engine … | KEEP |  |
| bot/exit_policy_telemetry.py | 207 | OBSERVABILITY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/external_origin_observation.py | 146 | CORE | IMPORT | 1 | no | external_origin_runtime | KEEP |  |
| bot/external_origin_runtime.py | 155 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/final_loss_budget.py | 119 | CORE | LAZY | 2 | no | final_sizing_invariants, pilot_risk_cap_hardening | KEEP |  |
| bot/final_sizing_invariants.py | 205 | CORE | IMPORT | 3 | no | runtime_overlays | KEEP |  |
| bot/financial_state.py | 53 | CORE | IMPORT | 3 | no | pilot | KEEP |  |
| bot/funnel_metrics.py | 213 | OBSERVABILITY | IMPORT | 1 | no | runtime_overlays, status_observability | KEEP |  |
| bot/htf_transition_calibration.py | 229 | CORE | IMPORT | 1 | no | htf_transition_shadow | KEEP |  |
| bot/htf_transition_shadow.py | 257 | SHADOW | IMPORT | 1 | no | entry_type_shadow_overlay, mtf_strategy_observability | KEEP |  |
| bot/hwm_namespace.py | 37 | CORE | IMPORT | 4 | no | drawdown_persistence, exchange_accounting_evidence | KEEP |  |
| bot/hwm_provenance.py | 59 | CORE | IMPORT | 4 | no | drawdown_persistence | KEEP |  |
| bot/indicators.py | 765 | CORE | IMPORT | 2 | no | correlation, engine, entry_type_shadow_overlay, exit_policy_telemetry … | KEEP |  |
| bot/initial_reconciliation.py | 249 | CORE | LAZY | 1 | no | engine | KEEP |  |
| bot/instrument_readiness_guard.py | 80 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/integrity.py | 387 | CORE | IMPORT | 8 | no | engine, integrity_external_coexistence_runtime, paper_lifecycle, runtime_bootstrap | KEEP |  |
| bot/integrity_external_coexistence_runtime.py | 96 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/integrity_protected_external_policy.py | 19 | CORE | IMPORT | 0 | no | integrity_external_coexistence_runtime | KEEP |  |
| bot/kucoin.py | 2349 | VENUE_KUCOIN | IMPORT | 27 | no | account_balance_observability, backtest, balance_observability, conditional_stop_lifecycle … | KEEP |  |
| bot/kucoin_contract_risk_hardening.py | 574 | VENUE_KUCOIN | IMPORT | 1 | no | runtime_bootstrap, runtime_overlays | KEEP |  |
| bot/kucoin_cross_margin_order.py | 37 | VENUE_KUCOIN | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/kucoin_execution_model.py | 180 | VENUE_KUCOIN | IMPORT | 1 | no | backtest, execution_cost, missed_opportunity_audit, nexus_live_cost_calibration … | KEEP |  |
| bot/kucoin_fill_normalization.py | 80 | VENUE_KUCOIN | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/kucoin_native_tpsl.py | 283 | VENUE_KUCOIN | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/kucoin_order_forensics.py | 83 | VENUE_KUCOIN | NONE | 1 | no | — | DEPRECATE | KuCoin-only manual CLI; keep while KuCoin compatibility is supported |
| bot/kucoin_position_units.py | 74 | VENUE_KUCOIN | IMPORT | 3 | no | nexus_runtime_engine | KEEP |  |
| bot/kucoin_price_tick_hardening.py | 78 | VENUE_KUCOIN | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/legacy_pretrade_advisory.py | 163 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/liquidation.py | 216 | CORE | IMPORT | 4 | no | engine, runtime_bootstrap, shadow_live, main | KEEP |  |
| bot/liquidation_override_guard.py | 32 | CORE | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/live_execution_fence.py | 167 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/log_redaction_hardening.py | 76 | HARDENING_OVERLAY | IMPORT | 1 | no | bot | KEEP |  |
| bot/logger.py | 214 | CORE | IMPORT | 3 | no | atomic_key_value, backtest, binance, binance_accounting_evidence … | KEEP |  |
| bot/market_data.py | 1002 | CORE | IMPORT | 1 | no | derivatives_news_freshness_hardening, engine, news_context_hardening, score | KEEP |  |
| bot/market_data_integrity.py | 324 | CORE | IMPORT | 4 | no | nexus_decision_consistency, nexus_oos_real_replay_corrected, runtime_bootstrap, runtime_overlays | KEEP |  |
| bot/market_data_health.py | 121 | CORE | IMPORT | 2 | no | binance, pilot | KEEP | canonical public market-data freshness authority (gate 11) |
| bot/market_radar.py | 388 | OBSERVABILITY | IMPORT | 1 | no | operator_runtime_policy, runtime_overlays | KEEP |  |
| bot/market_risk_binance_fallback.py | 212 | VENUE_BINANCE | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/market_risk_coverage_observability.py | 133 | OBSERVABILITY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/market_risk_intelligence.py | 120 | CORE | IMPORT | 1 | no | market_risk_runtime | KEEP |  |
| bot/market_risk_news_bridge.py | 39 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/market_risk_runtime.py | 605 | CORE | IMPORT | 3 | no | runtime_bootstrap, runtime_overlays | KEEP |  |
| bot/market_viability_fail_closed.py | 100 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/missed_opportunity_audit.py | 498 | OBSERVABILITY | IMPORT | 6 | no | nexus_runtime_engine, rr_gate_calibration | KEEP |  |
| bot/mtf_shadow.py | 278 | SHADOW | LAZY | 2 | no | mtf_strategy_observability, status_observability | KEEP |  |
| bot/mtf_strategy_observability.py | 133 | OBSERVABILITY | IMPORT | 1 | no | strategy | KEEP |  |
| bot/native_stop_repair.py | 388 | CORE | LAZY | 3 | no | kucoin, prelive_protection_failclosed | KEEP |  |
| bot/news_context_hardening.py | 272 | HARDENING_OVERLAY | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/news_event_observability.py | 48 | OBSERVABILITY | LAZY | 1 | no | news_context_hardening | KEEP |  |
| bot/news_pipeline.py | 359 | CORE | LAZY | 0 | no | engine | KEEP |  |
| bot/news_semantic_hardening.py | 142 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/nexus_ai.py | 804 | NEXUS | IMPORT | 4 | no | engine, mtf_shadow, nexus_oos_real_replay, nexus_oos_replay … | KEEP |  |
| bot/nexus_calibration.py | 74 | NEXUS | NONE | 1 | no | — | KEEP | offline calibration metrics with tests; basis for missing calibration-drift monitoring |
| bot/nexus_confidence_evidence.py | 110 | NEXUS | LAZY | 1 | no | nexus_confidence_observability | KEEP |  |
| bot/nexus_confidence_observability.py | 84 | OBSERVABILITY | IMPORT | 0 | no | validation_safety_lock | KEEP |  |
| bot/nexus_decision_consistency.py | 271 | NEXUS | IMPORT | 4 | no | missed_opportunity_audit, runtime_bootstrap, score_floor_shadow, session_penalty_shadow | KEEP |  |
| bot/nexus_decision_dedupe.py | 154 | NEXUS | IMPORT | 1 | no | runtime_bootstrap, status_observability | KEEP |  |
| bot/nexus_grade_display.py | 35 | NEXUS | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/nexus_latency_telegram.py | 210 | OBSERVABILITY | IMPORT | 1 | no | validation_safety_lock | KEEP |  |
| bot/nexus_live_cost_calibration.py | 190 | NEXUS | IMPORT | 3 | no | rr_gate_calibration, runtime_bootstrap | KEEP |  |
| bot/nexus_models.py | 497 | NEXUS | IMPORT | 1 | no | nexus_ai | KEEP |  |
| bot/nexus_oos_edge_gate.py | 181 | RESEARCH | NONE | 4 | yes | nexus_oos_evidence_bundle, nexus_oos_evidence_dataset, nexus_oos_evidence_io, nexus_oos_portfolio … | KEEP |  |
| bot/nexus_oos_evidence_bundle.py | 190 | RESEARCH | NONE | 0 | no | — | UNKNOWN_REQUIRES_EVIDENCE | manual research CLI, no refs/tests/CI; may be run by hand |
| bot/nexus_oos_evidence_dataset.py | 67 | RESEARCH | NONE | 1 | no | — | KEEP |  |
| bot/nexus_oos_evidence_io.py | 98 | RESEARCH | NONE | 1 | no | — | KEEP |  |
| bot/nexus_oos_portfolio.py | 206 | RESEARCH | NONE | 1 | no | — | KEEP |  |
| bot/nexus_oos_real_replay.py | 381 | RESEARCH | NONE | 1 | yes | nexus_oos_evidence_bundle, nexus_oos_real_replay_corrected, nexus_oos_replay_diagnostics, nexus_oos_robustness_replay | KEEP |  |
| bot/nexus_oos_real_replay_corrected.py | 96 | RESEARCH | NONE | 1 | yes | nexus_oos_evidence_bundle, nexus_oos_replay_diagnostics_corrected, nexus_oos_robustness_replay | CONSOLIDATE | wrapper entrypoint used by oos_real_replay.yml; merge into nexus_oos_real_replay when CI is updated |
| bot/nexus_oos_replay.py | 262 | RESEARCH | NONE | 2 | yes | nexus_oos_portfolio | KEEP |  |
| bot/nexus_oos_replay_diagnostics.py | 124 | RESEARCH | NONE | 0 | yes | nexus_oos_replay_diagnostics_corrected | KEEP |  |
| bot/nexus_oos_replay_diagnostics_corrected.py | 7 | RESEARCH | NONE | 0 | yes | — | CONSOLIDATE | 7-line wrapper used by oos_replay_diagnostics.yml |
| bot/nexus_oos_robustness.py | 117 | RESEARCH | NONE | 1 | yes | nexus_oos_evidence_bundle, nexus_oos_robustness_replay | KEEP |  |
| bot/nexus_oos_robustness_replay.py | 103 | RESEARCH | NONE | 0 | yes | — | KEEP |  |
| bot/nexus_optional_evidence.py | 134 | NEXUS | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/nexus_persistence.py | 348 | NEXUS | IMPORT | 3 | no | nexus_decision_dedupe, nexus_validation_observability, status_observability | KEEP |  |
| bot/nexus_prefinal_veto_observability.py | 89 | OBSERVABILITY | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/nexus_probability.py | 17 | NEXUS | IMPORT | 1 | no | nexus_ai, nexus_decision_consistency, oos_calibration_readiness | KEEP |  |
| bot/nexus_regime_transition_consistency.py | 126 | NEXUS | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/nexus_runtime_engine.py | 187 | NEXUS | IMPORT | 5 | no | main | KEEP |  |
| bot/nexus_structure_semantics.py | 164 | NEXUS | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/nexus_terminal_notifications.py | 351 | NEXUS | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/nexus_types.py | 224 | NEXUS | IMPORT | 9 | no | engine, kucoin_contract_risk_hardening, nexus_ai, nexus_models … | KEEP |  |
| bot/nexus_validation_observability.py | 71 | OBSERVABILITY | IMPORT | 2 | no | nexus_runtime_engine | KEEP |  |
| bot/nexus_zero_observability.py | 49 | OBSERVABILITY | IMPORT | 2 | no | nexus_validation_observability | KEEP |  |
| bot/notifier.py | 667 | CORE | IMPORT | 1 | no | engine, market_radar, nexus_decision_dedupe, nexus_runtime_engine … | KEEP |  |
| bot/oos_calibration_readiness.py | 141 | RESEARCH | IMPORT | 2 | no | confidence_outcome_runtime, nexus_confidence_observability | KEEP |  |
| bot/oos_model_validation.py | 170 | RESEARCH | IMPORT | 2 | no | oos_calibration_readiness | KEEP |  |
| bot/operational_incident_recovery.py | 188 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/operator_loss_policy.py | 163 | CORE | IMPORT | 3 | no | runtime_overlays | KEEP |  |
| bot/operator_runtime_policy.py | 338 | CORE | IMPORT | 4 | no | pilot_live_runtime, runtime_overlays | KEEP |  |
| bot/optimizer.py | 354 | RESEARCH | IMPORT | 1 | no | engine, research_process | KEEP |  |
| bot/order_state.py | 362 | CORE | IMPORT | 15 | no | binance, durable_execution, durable_live_reconciliation, engine … | KEEP |  |
| bot/order_visibility_race_hardening.py | 88 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/paper_e2e.py | 70 | CORE | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/paper_lifecycle.py | 408 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/paper_loss_budget.py | 38 | CORE | IMPORT | 1 | no | engine | KEEP |  |
| bot/paper_validation_reset.py | 158 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/paper_wallet.py | 361 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/partial_tp_execution_hardening.py | 199 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/pilot.py | 252 | CORE | IMPORT | 8 | no | engine, pilot_exposure_capacity, pilot_readiness_observability, pilot_submission_counter … | KEEP |  |
| bot/pilot_exposure_capacity.py | 147 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/pilot_external_position_guard.py | 314 | CORE | IMPORT | 3 | no | runtime_bootstrap | KEEP |  |
| bot/pilot_live_runtime.py | 392 | CORE | IMPORT | 4 | no | operator_runtime_policy, runtime_bootstrap | KEEP |  |
| bot/pilot_readiness_observability.py | 35 | OBSERVABILITY | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/pilot_release_control.py | 51 | CORE | IMPORT | 1 | no | runtime_bootstrap, runtime_release_contract, shadow_startup_logging | KEEP |  |
| bot/pilot_risk_cap_hardening.py | 294 | HARDENING_OVERLAY | IMPORT | 6 | no | binance_cross_portfolio_stress, cross_portfolio_stress, operator_runtime_policy, runtime_bootstrap … | KEEP |  |
| bot/pilot_submission_counter.py | 363 | CORE | IMPORT | 6 | no | durable_reconcile_hardening, runtime_bootstrap | KEEP |  |
| bot/policy_log_throttle.py | 45 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/post_trade_forensics.py | 286 | OBSERVABILITY | IMPORT | 2 | no | binance_accounting_evidence, exchange_accounting_evidence, runtime_overlays | KEEP |  |
| bot/pre_dispatch_guard.py | 280 | CORE | IMPORT | 2 | no | core_execution_risk, pilot_risk_cap_hardening, shadow_live | KEEP |  |
| bot/prelive_protection_failclosed.py | 205 | CORE | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/prelive_readonly_probe.py | 153 | CORE | LAZY | 1 | no | private_ws_readonly_observability | KEEP |  |
| bot/pretrade_hardening.py | 72 | HARDENING_OVERLAY | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/private_ws_readonly_observability.py | 169 | OBSERVABILITY | LAZY | 2 | no | pilot_live_runtime, validation_safety_lock | KEEP |  |
| bot/professional_risk.py | 213 | CORE | IMPORT | 11 | no | account_capital_reader, core_execution_risk, nexus_runtime_engine, pilot_exposure_capacity … | KEEP |  |
| bot/professional_risk_adapter.py | 231 | CORE | IMPORT | 6 | no | nexus_runtime_engine | KEEP |  |
| bot/protection_readiness.py | 505 | CORE | LAZY | 2 | no | engine | KEEP |  |
| bot/pullback_confirmation_hardening.py | 302 | HARDENING_OVERLAY | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/quantity.py | 70 | CORE | IMPORT | 2 | no | conditional_stop_protection, durable_partial_exit, engine, final_sizing_invariants … | KEEP |  |
| bot/release_proof.py | 38 | CORE | NONE | 0 | yes | — | KEEP |  |
| bot/research_process.py | 58 | RESEARCH | LAZY | 1 | no | optimizer | KEEP |  |
| bot/restart_opening_order_lineage.py | 87 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/restart_ownership_recovery.py | 261 | CORE | IMPORT | 1 | no | external_origin_runtime, pilot_external_position_guard | KEEP |  |
| bot/risk.py | 491 | CORE | IMPORT | 10 | no | engine, operator_runtime_policy, paper_e2e, runtime_contract_guard | KEEP |  |
| bot/risk_manager_v3.py | 160 | CORE | IMPORT | 5 | no | operator_runtime_policy, professional_risk_adapter, runtime_contract_guard, shadow_connect … | KEEP |  |
| bot/rr_gate_calibration.py | 380 | CORE | IMPORT | 1 | no | nexus_validation_observability | KEEP |  |
| bot/rr_precision_hardening.py | 80 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/runtime_bootstrap.py | 231 | CORE | IMPORT | 0 | yes | nexus_oos_evidence_bundle, nexus_oos_real_replay, nexus_oos_replay_diagnostics, nexus_oos_robustness_replay … | KEEP |  |
| bot/runtime_contract_guard.py | 215 | CORE | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/runtime_hardening.py | 373 | HARDENING_OVERLAY | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/runtime_mode_observability.py | 102 | OBSERVABILITY | IMPORT | 1 | no | main, main_hardened | KEEP |  |
| bot/runtime_overlays.py | 135 | HARDENING_OVERLAY | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/runtime_readiness.py | 62 | CORE | IMPORT | 7 | no | binance, kucoin, main_hardened | KEEP |  |
| bot/runtime_release_contract.py | 97 | CORE | IMPORT | 1 | no | runtime_bootstrap, runtime_overlays | KEEP |  |
| bot/runtime_truth.py | 406 | OBSERVABILITY | IMPORT | 7 | yes | runtime_truth_exporter, runtime_truth_hooks, runtime_truth_ws_filter | KEEP |  |
| bot/runtime_truth_exporter.py | 186 | OBSERVABILITY | LAZY | 2 | yes | runtime_truth_hooks | KEEP |  |
| bot/runtime_truth_hooks.py | 511 | OBSERVABILITY | IMPORT | 2 | yes | runtime_bootstrap | KEEP |  |
| bot/runtime_truth_rest.py | 40 | OBSERVABILITY | IMPORT | 1 | no | runtime_truth_hooks | KEEP |  |
| bot/runtime_truth_ws_compat.py | 52 | OBSERVABILITY | IMPORT | 1 | no | sitecustomize | KEEP |  |
| bot/runtime_truth_ws_filter.py | 85 | OBSERVABILITY | IMPORT | 1 | no | sitecustomize | KEEP |  |
| bot/scan_summary_hardening.py | 246 | HARDENING_OVERLAY | IMPORT | 3 | no | runtime_bootstrap | KEEP |  |
| bot/score.py | 577 | CORE | IMPORT | 1 | no | engine, news_context_hardening, pretrade_hardening, runtime_bootstrap … | KEEP |  |
| bot/score_floor_shadow.py | 492 | SHADOW | LAZY | 1 | no | mtf_strategy_observability | KEEP |  |
| bot/score_floor_shadow_persistence.py | 146 | SHADOW | LAZY | 1 | no | score_floor_shadow | KEEP |  |
| bot/score_tf_rebalanced_shadow.py | 204 | SHADOW | IMPORT | 1 | no | adaptive_mtf_dedup_shadow | KEEP |  |
| bot/score_weights.py | 222 | RESEARCH | NONE | 0 | no | — | DELETE_SAFE → REMOVED (commit `b4e0aa6`) | A-F satisfied: no import/call, no string/dynamic ref, no CI, no test; unused offline calibrator (comments only) |
| bot/scoring_safety_hardening.py | 277 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/selfcheck.py | 481 | CORE | IMPORT | 4 | yes | runtime_bootstrap, scan_summary_hardening, main | KEEP |  |
| bot/selfcheck_entrypoint_hardening.py | 57 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/service_readiness.py | 52 | CORE | IMPORT | 2 | no | main_hardened | KEEP |  |
| bot/session_penalty_persistence.py | 176 | CORE | LAZY | 1 | no | session_penalty_shadow | KEEP |  |
| bot/session_penalty_shadow.py | 577 | SHADOW | LAZY | 3 | no | mtf_strategy_observability | KEEP |  |
| bot/shadow_balance_semantics.py | 48 | SHADOW | IMPORT | 1 | no | nexus_runtime_engine, shadow_connect, shadow_live, validation_safety_lock | KEEP |  |
| bot/shadow_connect.py | 148 | SHADOW | LAZY | 1 | no | validation_safety_lock | KEEP |  |
| bot/shadow_integrity_isolation.py | 71 | SHADOW | LAZY | 1 | no | validation_safety_lock | KEEP |  |
| bot/shadow_live.py | 416 | SHADOW | LAZY | 5 | no | validation_safety_lock | KEEP |  |
| bot/shadow_mode_observability.py | 47 | SHADOW | IMPORT | 0 | no | runtime_bootstrap | KEEP |  |
| bot/shadow_position_forensics.py | 95 | SHADOW | IMPORT | 1 | no | pilot_external_position_guard | KEEP |  |
| bot/shadow_startup_logging.py | 55 | SHADOW | IMPORT | 2 | no | runtime_bootstrap | KEEP |  |
| bot/shadow_state_isolation.py | 27 | SHADOW | LAZY | 1 | no | shadow_live | KEEP |  |
| bot/silent_except_audit.py | 163 | OBSERVABILITY | LAZY | 2 | no | startup_block | KEEP |  |
| bot/stagnation_time_hardening.py | 132 | HARDENING_OVERLAY | IMPORT | 2 | no | exit_policy_telemetry, runtime_bootstrap | KEEP |  |
| bot/startup_block.py | 103 | CORE | IMPORT | 4 | no | main | KEEP |  |
| bot/startup_position_unit_hardening.py | 175 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/startup_ready_notification.py | 93 | CORE | IMPORT | 1 | no | operator_runtime_policy | KEEP |  |
| bot/state_authority_observability.py | 54 | OBSERVABILITY | IMPORT | 1 | no | database, hwm_namespace, runtime_overlays | KEEP |  |
| bot/status_observability.py | 163 | OBSERVABILITY | IMPORT | 2 | no | startup_ready_notification, main | KEEP |  |
| bot/strategy.py | 766 | CORE | IMPORT | 8 | no | backtest, engine, exit_policy_telemetry, htf_transition_shadow … | KEEP |  |
| bot/telegram_serialization_hardening.py | 66 | HARDENING_OVERLAY | IMPORT | 1 | no | validation_safety_lock | KEEP |  |
| bot/trailing_safety_hardening.py | 47 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| bot/validation_safety_lock.py | 163 | CORE | IMPORT | 3 | no | runtime_bootstrap, scan_summary_hardening | KEEP |  |
| bot/viability_fail_closed_hardening.py | 120 | HARDENING_OVERLAY | IMPORT | 1 | no | runtime_bootstrap | KEEP |  |
| bot/volume_gate_shadow.py | 336 | SHADOW | LAZY | 1 | no | mtf_strategy_observability | KEEP |  |
| bot/volume_ratio_diagnostics.py | 73 | OBSERVABILITY | IMPORT | 1 | no | runtime_overlays | KEEP |  |
| dashboard/index.html | 49 | UI_STATIC | — | n/a | no | — | KEEP |  |
| dashboard/nexus-alpha-v2.jsx | 674 | UI_STATIC | — | n/a | no | — | KEEP |  |
| docs/.hwm_atomicity_gate | 3 | CONFIG | — | n/a | no | — | KEEP |  |
| docs/DAILY_PNL_PERSISTENCE.md | 29 | DOC | — | n/a | no | — | KEEP |  |
| docs/DRAWDOWN_POLICY_AUDIT_20260921.md | 139 | DOC | — | n/a | no | — | KEEP |  |
| docs/ENTRY_PAUSE.md | 24 | DOC | — | n/a | no | — | KEEP |  |
| docs/LIVE_RELEASE_REVIEW.md | 58 | DOC | — | n/a | no | — | KEEP |  |
| docs/LIVE_RISK_OVERRIDE.md | 17 | DOC | — | n/a | no | — | KEEP |  |
| docs/NEWS_EVENT_RUNTIME_OBSERVABILITY.md | 9 | DOC | — | n/a | no | — | KEEP |  |
| docs/PREPILOT_RELEASE_2026-09-08.md | 98 | DOC | — | n/a | no | — | KEEP |  |
| docs/PROFESSIONAL_HARDENING_ROADMAP_2026-09-08.md | 111 | DOC | — | n/a | no | — | KEEP |  |
| docs/RAILWAY_PRODUCTION_PROMOTION.md | 64 | DOC | — | n/a | no | — | KEEP |  |
| docs/RISK_MANAGER_V3_MIGRATION.md | 23 | DOC | — | n/a | no | — | KEEP |  |
| docs/SEVEN_SAFETY_FIXES.md | 56 | DOC | — | n/a | no | — | KEEP |  |
| docs/SHADOW_POSITION_FORENSICS_2026-09-08.md | 17 | DOC | — | n/a | no | — | KEEP |  |
| docs/SHADOW_POSITION_FORENSICS_CHECKLIST.md | 1 | DOC | — | n/a | no | — | KEEP |  |
| docs/SHADOW_POSITION_FORENSICS_FINAL.md | 1 | DOC | — | n/a | no | — | KEEP |  |
| docs/SHADOW_POSITION_FORENSICS_RELEASE_NOTE.md | 1 | DOC | — | n/a | no | — | KEEP |  |
| docs/SHADOW_POSITION_FORENSICS_SECURITY.md | 1 | DOC | — | n/a | no | — | KEEP |  |
| docs/SYSTEM_AUDIT_2026-09-13.md | 63 | DOC | — | n/a | no | — | KEEP |  |
| docs/audit/CANONICAL_AUTHORITIES.md | 25 | DOC | — | n/a | no | — | KEEP |  |
| docs/audit/NEXUS_PRODUCTION_CONSISTENCY_AUDIT_2026-09-26.md | 276 | DOC | — | n/a | no | — | KEEP |  |
| docs/hwm_atomicity_20260915.md | 11 | DOC | — | n/a | no | — | KEEP |  |
| docs/hwm_atomicity_review.md | 3 | DOC | — | n/a | no | — | KEEP |  |
| docs/hwm_atomicity_scope.md | 3 | DOC | — | n/a | no | — | KEEP |  |
| docs/hwm_atomicity_status.txt | 4 | DOC | — | n/a | no | — | KEEP |  |
| docs/hwm_atomicity_test_matrix.md | 9 | DOC | — | n/a | no | — | KEEP |  |
| docs/live_pilot_activation.md | 28 | DOC | — | n/a | no | — | KEEP |  |
| docs/oos_real_replay_methodology.md | 19 | DOC | — | n/a | no | — | KEEP |  |
| main.py | 755 | ENTRYPOINT | ENTRY | n/a | yes | — | KEEP |  |
| main_hardened.py | 194 | ENTRYPOINT | ENTRY | n/a | yes | — | KEEP |  |
| railway.toml | 9 | CONFIG | — | n/a | no | — | KEEP |  |
| requirements.txt | 14 | DOC | — | n/a | yes | — | KEEP |  |
| sitecustomize.py | 46 | ENTRYPOINT | ENTRY | n/a | yes | — | KEEP |  |
| tests/__init__.py | 0 | TEST | — | self | no | — | KEEP |  |
| tests/execution_test_context.py | 62 | TEST | — | self | no | — | KEEP |  |
| tests/mock_kucoin.py | 322 | TEST | — | self | no | — | KEEP |  |
| tests/run_offline.py | 155 | TEST | — | self | yes | — | KEEP |  |
| tests/test_account_balance_observability.py | 69 | TEST | — | self | no | — | KEEP |  |
| tests/test_account_balance_semantics_shared.py | 97 | TEST | — | self | no | — | KEEP |  |
| tests/test_account_capital_reader.py | 50 | TEST | — | self | no | — | KEEP |  |
| tests/test_accounting_fill_link.py | 105 | TEST | — | self | no | — | KEEP |  |
| tests/test_adaptive_duplicate_evidence.py | 90 | TEST | — | self | no | — | KEEP |  |
| tests/test_adaptive_mtf_calibration.py | 99 | TEST | — | self | no | — | KEEP |  |
| tests/test_adaptive_mtf_dedup_shadow.py | 37 | TEST | — | self | no | — | KEEP |  |
| tests/test_adaptive_mtf_entry.py | 175 | TEST | — | self | no | — | KEEP |  |
| tests/test_adaptive_mtf_funnel_summary.py | 101 | TEST | — | self | no | — | KEEP |  |
| tests/test_ai_gate.py | 110 | TEST | — | self | no | — | KEEP |  |
| tests/test_api_lifecycle_audit.py | 64 | TEST | — | self | no | — | KEEP |  |
| tests/test_atomic_key_value.py | 57 | TEST | — | self | no | — | KEEP |  |
| tests/test_audit_remaining_regressions.py | 93 | TEST | — | self | no | — | KEEP |  |
| tests/test_auth_log_redaction.py | 66 | TEST | — | self | no | — | KEEP |  |
| tests/test_balance_gate.py | 42 | TEST | — | self | no | — | KEEP |  |
| tests/test_balance_observability.py | 37 | TEST | — | self | yes | — | KEEP |  |
| tests/test_bgx_predispatch_001.py | 149 | TEST | — | self | no | — | KEEP |  |
| tests/test_bgx_predispatch_002.py | 237 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_accounting_evidence.py | 388 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_cross_portfolio_stress.py | 155 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_ed25519_signing.py | 93 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_external_position_guard.py | 99 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_liquidation_safety_50x.py | 98 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_protection_failclosed.py | 134 | TEST | — | self | no | — | KEEP |  |
| tests/test_binance_usdm_migration.py | 683 | TEST | — | self | no | — | KEEP |  |
| tests/test_bluegreen_ownership_handoff.py | 81 | TEST | — | self | no | — | KEEP |  |
| tests/test_canonical_http_readiness.py | 318 | TEST | — | self | no | — | KEEP |  |
| tests/test_capital_flow_reconciliation.py | 304 | TEST | — | self | no | — | KEEP |  |
| tests/test_cash_flow_adjusted_drawdown.py | 585 | TEST | — | self | no | — | KEEP |  |
| tests/test_cash_flow_cas_postgres.py | 49 | TEST | — | self | no | — | KEEP | real PostgreSQL (CI `TEST_POSTGRES_DSN`) |
| tests/test_chaos.py | 175 | TEST | — | self | no | — | KEEP |  |
| tests/test_ci_deploy_gate.py | 132 | TEST | — | self | no | — | KEEP |  |
| tests/test_conditional_stop_protection.py | 194 | TEST | — | self | no | — | KEEP |  |
| tests/test_confidence_outcome_linking.py | 61 | TEST | — | self | no | — | KEEP |  |
| tests/test_confidence_outcome_runtime.py | 175 | TEST | — | self | no | — | KEEP |  |
| tests/test_confirmed_rr_exit.py | 82 | TEST | — | self | no | — | KEEP |  |
| tests/test_core_execution_risk.py | 101 | TEST | — | self | no | — | KEEP |  |
| tests/test_correlation_fail_closed.py | 54 | TEST | — | self | no | — | KEEP |  |
| tests/test_critical_invariants_remediation.py | 30 | TEST | — | self | no | — | KEEP |  |
| tests/test_critical_state.py | 31 | TEST | — | self | no | — | KEEP |  |
| tests/test_cross_geometry_target_policy.py | 130 | TEST | — | self | no | — | KEEP |  |
| tests/test_cross_margin_balance_semantics.py | 78 | TEST | — | self | no | — | KEEP |  |
| tests/test_cross_portfolio_stress.py | 170 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_pnl_estimate_lineage_hardening.py | 130 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_pnl_exchange_reconciliation.py | 190 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_stop_operator_override.py | 110 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_stop_override_retirement.py | 37 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_stop_override_telemetry.py | 56 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_stop_prelive.py | 38 | TEST | — | self | no | — | KEEP |  |
| tests/test_daily_stop_reset_ordering.py | 67 | TEST | — | self | no | — | KEEP |  |
| tests/test_database_atomic_paper.py | 140 | TEST | — | self | yes | — | KEEP |  |
| tests/test_database_observability.py | 46 | TEST | — | self | no | — | KEEP |  |
| tests/test_database_rollback_observability.py | 30 | TEST | — | self | no | — | KEEP |  |
| tests/test_derivatives_news_freshness_hardening.py | 119 | TEST | — | self | no | — | KEEP |  |
| tests/test_drawdown_anomaly_diagnostics.py | 30 | TEST | — | self | no | — | KEEP |  |
| tests/test_drawdown_incident_repair.py | 78 | TEST | — | self | no | — | KEEP |  |
| tests/test_drawdown_peak_sanity_repair.py | 106 | TEST | — | self | no | — | KEEP |  |
| tests/test_drawdown_persistence.py | 143 | TEST | — | self | no | — | KEEP |  |
| tests/test_drawdown_second_incident.py | 26 | TEST | — | self | no | — | KEEP |  |
| tests/test_durable_daily_pnl.py | 148 | TEST | — | self | no | — | KEEP |  |
| tests/test_durable_daily_stop.py | 159 | TEST | — | self | no | — | KEEP |  |
| tests/test_durable_dispatch_gate.py | 35 | TEST | — | self | yes | — | KEEP |  |
| tests/test_durable_execution_restart.py | 239 | TEST | — | self | yes | — | KEEP |  |
| tests/test_durable_partial_exit.py | 86 | TEST | — | self | no | — | KEEP |  |
| tests/test_durable_reconcile_hardening.py | 188 | TEST | — | self | yes | — | KEEP |  |
| tests/test_e2e_validation_contract.py | 54 | TEST | — | self | yes | — | KEEP |  |
| tests/test_entry_latency_observability.py | 158 | TEST | — | self | no | — | KEEP |  |
| tests/test_entry_pause_lifecycle.py | 82 | TEST | — | self | no | — | KEEP |  |
| tests/test_entry_type_shadow.py | 54 | TEST | — | self | no | — | KEEP |  |
| tests/test_event_intelligence.py | 35 | TEST | — | self | no | — | KEEP |  |
| tests/test_exchange_accounting_evidence.py | 215 | TEST | — | self | no | — | KEEP |  |
| tests/test_exec01_unit_normalization.py | 339 | TEST | — | self | no | — | KEEP |  |
| tests/test_exec02_ambiguous_order.py | 185 | TEST | — | self | no | — | KEEP |  |
| tests/test_exec03_partial_fill_reconcile.py | 407 | TEST | — | self | no | — | KEEP |  |
| tests/test_execution_cost_snapshot.py | 208 | TEST | — | self | no | — | KEEP |  |
| tests/test_execution_ownership.py | 237 | TEST | — | self | no | — | KEEP |  |
| tests/test_external_origin_runtime.py | 137 | TEST | — | self | no | — | KEEP |  |
| tests/test_external_position_ownership_p0.py | 86 | TEST | — | self | no | — | KEEP |  |
| tests/test_external_position_protection_policy.py | 85 | TEST | — | self | no | — | KEEP |  |
| tests/test_final_evidence_closure.py | 218 | TEST | — | self | no | — | KEEP |  |
| tests/test_final_loss_budget.py | 27 | TEST | — | self | no | — | KEEP |  |
| tests/test_final_release_proof.py | 136 | TEST | — | self | no | — | KEEP |  |
| tests/test_final_sizing_invariants.py | 119 | TEST | — | self | no | — | KEEP |  |
| tests/test_financial_state.py | 85 | TEST | — | self | no | — | KEEP |  |
| tests/test_hardening.py | 213 | TEST | — | self | no | — | KEEP |  |
| tests/test_htf_transition_calibration.py | 38 | TEST | — | self | no | — | KEEP |  |
| tests/test_htf_transition_shadow.py | 68 | TEST | — | self | no | — | KEEP |  |
| tests/test_hwm_atomic_contract.py | 23 | TEST | — | self | no | — | KEEP |  |
| tests/test_hwm_namespace_v2.py | 55 | TEST | — | self | no | — | KEEP |  |
| tests/test_hwm_provenance.py | 48 | TEST | — | self | no | — | KEEP |  |
| tests/test_initial_reconciliation_authority.py | 152 | TEST | — | self | no | — | KEEP |  |
| tests/test_instrument_readiness_guard.py | 114 | TEST | — | self | no | — | KEEP |  |
| tests/test_integrity_base_units.py | 31 | TEST | — | self | no | — | KEEP |  |
| tests/test_integrity_block_log_throttle.py | 44 | TEST | — | self | no | — | KEEP |  |
| tests/test_integrity_external_positions.py | 111 | TEST | — | self | no | — | KEEP |  |
| tests/test_integrity_protected_external_coexistence.py | 116 | TEST | — | self | no | — | KEEP |  |
| tests/test_kucoin_contract_risk_hardening.py | 397 | TEST | — | self | no | — | KEEP |  |
| tests/test_kucoin_cross_margin_order.py | 40 | TEST | — | self | no | — | KEEP |  |
| tests/test_kucoin_execution_parity.py | 117 | TEST | — | self | no | — | KEEP |  |
| tests/test_kucoin_fill_normalization.py | 72 | TEST | — | self | yes | — | KEEP |  |
| tests/test_kucoin_native_tpsl.py | 192 | TEST | — | self | yes | — | KEEP |  |
| tests/test_kucoin_order_forensics.py | 80 | TEST | — | self | no | — | KEEP |  |
| tests/test_kucoin_price_tick_hardening.py | 69 | TEST | — | self | no | — | KEEP |  |
| tests/test_kucoin_volume_basis_parity.py | 88 | TEST | — | self | no | — | KEEP |  |
| tests/test_legacy_pretrade_advisory.py | 163 | TEST | — | self | no | — | KEEP |  |
| tests/test_leverage_no_fallback.py | 45 | TEST | — | self | yes | — | KEEP |  |
| tests/test_liquidation_override_guard.py | 54 | TEST | — | self | yes | — | KEEP |  |
| tests/test_liquidation_safe_leverage.py | 29 | TEST | — | self | no | — | KEEP |  |
| tests/test_live_adapter_chaos.py | 112 | TEST | — | self | yes | — | KEEP |  |
| tests/test_live_execution_fence.py | 152 | TEST | — | self | no | — | KEEP |  |
| tests/test_live_pilot_release_gate.py | 91 | TEST | — | self | no | — | KEEP |  |
| tests/test_log_redaction_hardening.py | 58 | TEST | — | self | no | — | KEEP |  |
| tests/test_log_throttle_scan_suspenso.py | 208 | TEST | — | self | no | — | KEEP |  |
| tests/test_logger_audit_pacing_core.py | 46 | TEST | — | self | no | — | KEEP |  |
| tests/test_margin_reconciliation.py | 348 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_data_integrity.py | 155 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_radar.py | 329 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_data_freshness_authority.py | 391 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_binance_fallback.py | 94 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_coverage_observability.py | 121 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_intelligence.py | 24 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_news.py | 36 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_numeric_normalization.py | 38 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_runtime.py | 214 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_risk_source_refresh.py | 85 | TEST | — | self | no | — | KEEP |  |
| tests/test_market_viability_fail_closed.py | 88 | TEST | — | self | no | — | KEEP |  |
| tests/test_missed_opportunity_audit.py | 31 | TEST | — | self | no | — | KEEP |  |
| tests/test_mtf_strategy_observability_core.py | 104 | TEST | — | self | no | — | KEEP |  |
| tests/test_native_stop_repair.py | 170 | TEST | — | self | no | — | KEEP |  |
| tests/test_news_context_hardening.py | 106 | TEST | — | self | yes | — | KEEP |  |
| tests/test_news_event_observability.py | 43 | TEST | — | self | no | — | KEEP |  |
| tests/test_news_event_runtime_wiring.py | 52 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_calibration.py | 41 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_confidence_evidence.py | 70 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_decision_consistency.py | 154 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_latency_telegram.py | 249 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_live_cost_calibration.py | 173 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_oos_edge_gate.py | 106 | TEST | — | self | yes | — | KEEP |  |
| tests/test_nexus_oos_evidence_dataset.py | 47 | TEST | — | self | yes | — | KEEP |  |
| tests/test_nexus_oos_evidence_io.py | 78 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_oos_portfolio.py | 89 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_oos_real_replay.py | 81 | TEST | — | self | yes | — | KEEP |  |
| tests/test_nexus_oos_replay.py | 124 | TEST | — | self | yes | — | KEEP |  |
| tests/test_nexus_oos_replay_full_clock.py | 40 | TEST | — | self | yes | — | KEEP |  |
| tests/test_nexus_oos_robustness.py | 65 | TEST | — | self | yes | — | KEEP |  |
| tests/test_nexus_optional_evidence.py | 202 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_persistence_core_serialization.py | 73 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_prefinal_veto_observability.py | 113 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_regime_transition_consistency.py | 118 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_runtime_drawdown_advisory.py | 72 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_score_snapshot_attach.py | 64 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_structure_conflict_shadow.py | 120 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_structure_semantics.py | 119 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_terminal_fallbacks.py | 62 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_terminal_notifications.py | 213 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_validation_observability_core.py | 113 | TEST | — | self | no | — | KEEP |  |
| tests/test_nexus_zero_observability.py | 55 | TEST | — | self | yes | — | KEEP |  |
| tests/test_observability_only_hardening.py | 342 | TEST | — | self | no | — | KEEP |  |
| tests/test_oos_calibration_readiness.py | 54 | TEST | — | self | no | — | KEEP |  |
| tests/test_oos_model_validation.py | 73 | TEST | — | self | no | — | KEEP |  |
| tests/test_operational_incident_recovery.py | 108 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_drawdown_advisory_engine.py | 173 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_loss_policy.py | 107 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_loss_policy_technical_stop.py | 81 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_runtime_contract.py | 46 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_runtime_policy.py | 127 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_runtime_policy_contract.py | 31 | TEST | — | self | no | — | KEEP |  |
| tests/test_operator_runtime_risk_override.py | 86 | TEST | — | self | no | — | KEEP |  |
| tests/test_opportunity_metadata_fallback.py | 52 | TEST | — | self | no | — | KEEP |  |
| tests/test_order_state_rest_ack_race.py | 150 | TEST | — | self | no | — | KEEP |  |
| tests/test_order_visibility_race_hardening.py | 104 | TEST | — | self | no | — | KEEP |  |
| tests/test_orphan_position_reconciliation.py | 269 | TEST | — | self | no | — | KEEP |  |
| tests/test_paper_loss_budget.py | 57 | TEST | — | self | yes | — | KEEP |  |
| tests/test_paper_mutations.py | 67 | TEST | — | self | yes | — | KEEP |  |
| tests/test_paper_shadow_daily_stop_advisory.py | 81 | TEST | — | self | no | — | KEEP |  |
| tests/test_paper_signal_exit_idempotency.py | 66 | TEST | — | self | yes | — | KEEP |  |
| tests/test_paper_validation_reset.py | 44 | TEST | — | self | no | — | KEEP |  |
| tests/test_paper_wallet_restart.py | 172 | TEST | — | self | yes | — | KEEP |  |
| tests/test_partial_tp_execution_hardening.py | 127 | TEST | — | self | no | — | KEEP |  |
| tests/test_persistent_daily_stop_operator_override.py | 102 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_counter.py | 167 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_durable_submission_counter.py | 145 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_exposure_capacity.py | 183 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_external_position_guard.py | 164 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_live_runtime.py | 430 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_minimum.py | 83 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_mode.py | 248 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_market_data_runtime_bridge.py | 86 | TEST | — | self | no | — | KEEP | real bootstrap in child process |
| tests/binance_ws_runtime_harness.py | 222 | TEST_SUPPORT | module run by tests | test_pilot_market_data_runtime_bridge | no | — | KEEP | LIVE-shaped harness, network-guarded |
| tests/test_pilot_notional_sizing.py | 61 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_paper_isolation.py | 37 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_readiness_observability.py | 55 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_release_control.py | 57 | TEST | — | self | no | — | KEEP |  |
| tests/test_pilot_risk_cap_hardening.py | 294 | TEST | — | self | no | — | KEEP |  |
| tests/test_policy_log_throttle.py | 43 | TEST | — | self | no | — | KEEP |  |
| tests/test_position_unit_normalization.py | 122 | TEST | — | self | no | — | KEEP |  |
| tests/test_post_close_lifecycle_hardening.py | 554 | TEST | — | self | no | — | KEEP |  |
| tests/test_post_trade_forensics.py | 116 | TEST | — | self | no | — | KEEP |  |
| tests/test_post_trade_lineage_v2.py | 108 | TEST | — | self | no | — | KEEP |  |
| tests/test_pre_dispatch_guard.py | 216 | TEST | — | self | no | — | KEEP |  |
| tests/test_pre_live_runtime_cleanup.py | 124 | TEST | — | self | no | — | KEEP |  |
| tests/test_predispatch_drawdown_race.py | 153 | TEST | — | self | no | — | KEEP |  |
| tests/test_prelive_protection_failclosed.py | 137 | TEST | — | self | no | — | KEEP |  |
| tests/test_prelive_readonly_probe.py | 79 | TEST | — | self | no | — | KEEP |  |
| tests/test_pretrade_fail_closed.py | 35 | TEST | — | self | yes | — | KEEP |  |
| tests/test_private_ws.py | 224 | TEST | — | self | no | — | KEEP |  |
| tests/test_private_ws_race_hardening.py | 186 | TEST | — | self | no | — | KEEP |  |
| tests/test_private_ws_readonly_observability.py | 162 | TEST | — | self | no | — | KEEP |  |
| tests/test_private_ws_readonly_observability_protected_external.py | 102 | TEST | — | self | no | — | KEEP |  |
| tests/test_production_paper_startup_probe.py | 36 | TEST | — | self | no | — | KEEP |  |
| tests/test_professional_risk.py | 134 | TEST | — | self | no | — | KEEP |  |
| tests/test_professional_risk_adapter.py | 151 | TEST | — | self | no | — | KEEP |  |
| tests/test_protection_readiness_authority.py | 623 | TEST | — | self | yes | — | KEEP |  |
| tests/test_pullback_confirmation_hardening.py | 213 | TEST | — | self | no | — | KEEP |  |
| tests/test_quant_audit_h01_h05.py | 215 | TEST | — | self | no | — | KEEP |  |
| tests/test_quantity_boundary.py | 60 | TEST | — | self | no | — | KEEP |  |
| tests/test_rate_limit_post.py | 452 | TEST | — | self | no | — | KEEP |  |
| tests/test_regression.py | 495 | TEST | — | self | no | — | KEEP |  |
| tests/test_release_execution_boundary.py | 116 | TEST | — | self | no | — | KEEP |  |
| tests/test_release_pilot_postgres.py | 71 | TEST | — | self | no | — | KEEP |  |
| tests/test_release_source_regression.py | 31 | TEST | — | self | no | — | KEEP |  |
| tests/test_research_process.py | 60 | TEST | — | self | no | — | KEEP |  |
| tests/test_restart_opening_order_lineage.py | 90 | TEST | — | self | no | — | KEEP |  |
| tests/test_restart_ownership_recovery.py | 286 | TEST | — | self | no | — | KEEP |  |
| tests/test_risk_invariants_grid.py | 92 | TEST | — | self | no | — | KEEP |  |
| tests/test_risk_manager_v3.py | 89 | TEST | — | self | no | — | KEEP |  |
| tests/test_risk_v3_init_telemetry.py | 45 | TEST | — | self | no | — | KEEP |  |
| tests/test_rr_boundary_precision.py | 24 | TEST | — | self | no | — | KEEP |  |
| tests/test_rr_gate_calibration.py | 230 | TEST | — | self | no | — | KEEP |  |
| tests/test_rr_precision_isolation.py | 67 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_bootstrap_structure.py | 41 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_contract_guard.py | 155 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_mode_observability.py | 103 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_overlays_phase2.py | 63 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_position_unit_boundary.py | 36 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_readiness.py | 54 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_release_contract.py | 134 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_truth_core.py | 110 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_exporter.py | 110 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_full_stack.py | 58 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_performance.py | 88 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_provenance.py | 119 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_security.py | 65 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_semantics.py | 156 | TEST | — | self | yes | — | KEEP |  |
| tests/test_runtime_truth_ws_compat.py | 41 | TEST | — | self | no | — | KEEP |  |
| tests/test_runtime_truth_ws_filter.py | 110 | TEST | — | self | no | — | KEEP |  |
| tests/test_scan_summary_hardening.py | 33 | TEST | — | self | yes | — | KEEP |  |
| tests/test_score_floor_shadow.py | 264 | TEST | — | self | no | — | KEEP |  |
| tests/test_score_tf_rebalanced_shadow.py | 57 | TEST | — | self | no | — | KEEP |  |
| tests/test_scoring_trailing_safety.py | 137 | TEST | — | self | no | — | KEEP |  |
| tests/test_selfcheck_entrypoint_visibility.py | 22 | TEST | — | self | no | — | KEEP |  |
| tests/test_selfcheck_python_semantics.py | 34 | TEST | — | self | no | — | KEEP |  |
| tests/test_service_readiness_semantics.py | 70 | TEST | — | self | no | — | KEEP |  |
| tests/test_session_penalty_persistence.py | 207 | TEST | — | self | no | — | KEEP |  |
| tests/test_session_penalty_shadow.py | 252 | TEST | — | self | no | — | KEEP |  |
| tests/test_session_shadow_scheduling_observability.py | 140 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_ai_reason_observability.py | 33 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_balance_semantics.py | 83 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_connect_failclosed.py | 126 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_execution_capability.py | 41 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_final_gate_observability.py | 62 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_live_readonly.py | 115 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_mode_observability.py | 31 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_periodic_equity_refresh.py | 97 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_position_forensics.py | 159 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_post_ai_chain.py | 164 | TEST | — | self | yes | — | KEEP |  |
| tests/test_shadow_risk_v3_integration.py | 68 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_runtime_isolation.py | 74 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_runtime_static_safety.py | 15 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_score_summary_observability.py | 47 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_score_threshold.py | 30 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_startup_logging.py | 47 | TEST | — | self | no | — | KEEP |  |
| tests/test_shadow_state_isolation.py | 45 | TEST | — | self | no | — | KEEP |  |
| tests/test_silent_except_triage.py | 45 | TEST | — | self | no | — | KEEP |  |
| tests/test_sitecustomize_fail_closed.py | 45 | TEST | — | self | yes | — | KEEP |  |
| tests/test_sizing_truth_invariants.py | 228 | TEST | — | self | no | — | KEEP |  |
| tests/test_stagnation_time_hardening.py | 80 | TEST | — | self | no | — | KEEP |  |
| tests/test_startup_block.py | 47 | TEST | — | self | no | — | KEEP |  |
| tests/test_startup_block_integration.py | 30 | TEST | — | self | no | — | KEEP |  |
| tests/test_startup_diagnostics_migration.py | 53 | TEST | — | self | no | — | KEEP |  |
| tests/test_startup_position_unit_hardening.py | 154 | TEST | — | self | no | — | KEEP |  |
| tests/test_startup_ready_notification.py | 75 | TEST | — | self | no | — | KEEP |  |
| tests/test_startup_selfcheck_fail_closed.py | 56 | TEST | — | self | yes | — | KEEP |  |
| tests/test_state_authority_observability.py | 58 | TEST | — | self | no | — | KEEP |  |
| tests/test_status_execution_observability.py | 98 | TEST | — | self | no | — | KEEP |  |
| tests/test_status_observability_core.py | 71 | TEST | — | self | no | — | KEEP |  |
| tests/test_viability_fail_closed_hardening.py | 51 | TEST | — | self | no | — | KEEP |  |
| tests/test_viable_symbols_recovery.py | 336 | TEST | — | self | no | — | KEEP |  |
| tests/test_volume_gate_shadow.py | 133 | TEST | — | self | no | — | KEEP |  |
| tests/test_volume_ratio_diagnostics.py | 30 | TEST | — | self | no | — | KEEP |  |
