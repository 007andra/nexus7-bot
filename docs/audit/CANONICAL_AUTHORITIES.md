# NEXUS-7 canonical authorities (Binance USD-M production)

One owner per decision. "Final hook" = the last installed wrapper, which is the
one that actually executes. Anything not listed as the owner is diagnostics.

| concern | authority (module / symbol) | notes |
|---|---|---|
| Venue selection | `bot.exchange` (`EXCHANGE`) | `bot.kucoin` is imported for compatibility wrappers only and never claims KuCoin as venue unless `EXCHANGE=kucoin`. |
| Market data | `BinanceClient` WS kline/ticker caches + REST (`get_klines`) | closed candles only for pretrade (`pretrade_hardening` drops the forming candle). Public WS uses the routed `/market` stream (kline/24hrTicker are /market; unrouted URLs receive nothing). |
| Market-data freshness (PilotGuard 11_MARKET_DATA) | `BinanceClient.market_data_health` (`bot.market_data_health.MarketDataHealth`), written only by the native public WS handler after a validated kline/24hrTicker; read by `pilot.market_data_blockers` | monotonic; never advanced by REST, private WS, reconnect, malformed/unknown frames or handler errors; 120 s limit unchanged; `_last_ws_update` is a read-only view; runtime contract pins the handler and the `/market` route. See `docs/audit/PILOT_MARKET_DATA_RUNTIME_BRIDGE.md`. |
| Strategy signal | `bot.strategy.Analyzer` → `adaptive_mtf_entry` → `pullback_confirmation_hardening` | strategy `TOTAL_COST` is a signal pre-filter assumption, not the execution cost authority. |
| Execution cost (fee/slippage/spread/funding) | `bot.execution_cost.ExecutionCostSnapshot` (one per candidate, attached to the signal) | Binance fee from `/fapi/v1/commissionRate`; fallback ≥ 6 bps; funding not in pre-trade cost (explicit). |
| NEXUS decision (EV / net R:R) | `nexus_ai` via `nexus_live_cost_calibration` (costs = snapshot) | technical-policy R:R uses the same snapshot and is diagnostic only. |
| Risk budget | `RiskManagerV3.size_for_stop` via `ProfessionalRiskAdapter.size` | `equity * effective_risk_pct`; leverage only affects collateral. |
| Sizing (final quantity) | `final_sizing_invariants` (final `minimum_base_quantity` hook) | `min(stop_risk_qty, operator_margin_cap_qty)`; cap = 50% available as initial margin. Earlier pilot hooks are shadowed and say so. |
| Projected-loss ceiling | `final_loss_budget.validate` (final sizing + fresh pre-dispatch) | cost = `max(snapshot, static)`; never loosened. |
| Liquidation safety | `binance_cross_portfolio_stress` (final pre-dispatch) | brackets + account state; stop-stress risk-rate < 90%; missing data blocks. |
| Drawdown | durable performance HWM (`drawdown_persistence`) adjusted for external cash flows by `cash_flow_ledger` (TWR: `P' = P·E+/E-`; Binance flows only from `/fapi/v1/income` evidence, ambiguous → PENDING = BLOCK_NEW_ENTRIES, operator attestation via `python -m bot.cash_flow_admin`); hard gate re-checked on the final pre-dispatch equity read (`pilot_live_runtime._entry_drawdown_allows`) | methodology: `docs/audit/CASH_FLOW_ADJUSTED_DRAWDOWN.md`; thresholds and `LIVE_RISK_OVERRIDE_APPROVED` semantics unchanged. |
| External cash flows | `cash_flow_ledger` (ledger key `risk:external_cash_flows:binance:ledger:v1`, CAS-atomic with HWM/provenance/cursor) | REALIZED_PNL/COMMISSION/FUNDING_FEE are performance, never flows; a balance difference is never interpreted as a transfer. |
| Daily stop | `daily_stop_runtime_hardening` / `durable_daily_pnl` | unchanged by this audit. |
| Release | `pilot_release_control.live_pilot_release_authorized` rendered through `runtime_release_contract.ReleaseContract` | Binance accounting evidence is OBSERVABILITY_ONLY. |
| Execution / idempotency | `BinanceClient.place_order` (`newClientOrderId`, non-blind retry, ambiguous + `-4116` reconcile by id) + `live_execution_fence` (Postgres ownership lease) | reduce-only/closePosition keep an escape path when entries are paused. |
| Order state | `OrderRegistry` + durable registry (`durable_execution`) | ALGO_UPDATE lifecycle kept separate from normal order transitions. |
| Protection | `binance_protection_failclosed` + `conditional_stop_*` | unconfirmed protection → repair+read-back → emergency close after fill/flat confirmation; external positions never touched. |
| Accounting | `binance_accounting_evidence` | evidence only, no execution effect. |
| Notifications | `notifier` / `nexus_terminal_notifications` (fire-and-forget, dedupe/cooldown) | never alters trading state. |
| Market Radar (Telegram panel) | `market_radar` (passive log observer; started/cancelled by the `TradingEngine.run` owner) | observability only: never changes score, NEXUS, risk, sizing or orders; stale entries expire to SCANNING; configurable via `MARKET_RADAR_ENABLED`, `MARKET_RADAR_INTERVAL_S` (default 1800, min 300), `MARKET_RADAR_STALE_S` (default 900), `MARKET_RADAR_FIRST_DELAY_S` (default 300). |
| Account capital for RiskManagerV3 | `account_capital_reader.read_account_capital` (venue-aware since the full audit: `get_account_state()` on Binance) | same venue rule as `account_balance_semantics.read_account_state`. |
| Quantity units | `quantity.quantity_rules` (contract venues: contracts×multiplier; BASE_ASSET venues: stepSize units) | contract conversions refuse BASE_ASSET instruments (fail-closed). |
| Telegram credentials | `telegram_credentials` (TELEGRAM_TOKEN/TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT/TELEGRAM_CHAT_ID) | used by config, logger and funnel_metrics. |
