# OOS_RESEARCH_READER_V2: isolamento das leituras de pesquisa

Estado: implementado atrás da flag `OOS_RESEARCH_READER_V2` (padrão `false`). Sem a flag, o caminho é idêntico ao anterior.

## Problema

Todas as leituras dos diagnósticos de pesquisa (MIN_ORDER_*, BBO calibration, revisões do OOS prospectivo) usavam a conexão asyncpg operacional única, serializada por `database._io_lock`. As mesmas filas atendiam renovação de ownership, fencing, persistência e reconciliação. Em produção (deployment 8d8417d7, 18:43 a 19:14 UTC de 2026-10-11):

- renovação do heartbeat (de `heartbeat_before_renew` a `heartbeat_renewed`, 167 amostras): p50 1066 ms, p95 2648 ms, p99 3121 ms, máx 3398 ms;
- espera pelo lock (`OOS_DB_LOCK_HOLD_V1 pre_acquire_wait_ms`, 248 retenções de 125 ms ou mais): p50 0, p95 734, p99 1146, máx 1303 ms;
- maior detentor do lock: `fetchall:shadow_candidates` (leitura de pesquisa), até 1,6 s por consulta.

Os timeouts dessas leituras também envenenaram a conexão compartilhada nos incidentes de 2026-10-11 (ver PR #641).

## Inventário dos consumidores de `db._conn` / `db._io_lock`

| Classe | Módulos | Destino |
|---|---|---|
| Operacional (autoridade) | `execution_ownership`, `live_execution_fence`, `pilot_submission_counter`, `controlled_live_reentry_v1`, `atomic_key_value`, `critical_state`, `runtime_hardening`, `engine` (save_trade_open/close, paper atomic, risk events, sinais, decisões), `durable_execution`, `durable_daily_pnl`, `durable_daily_stop`, `drawdown_persistence`, `drawdown_recovery`, `risk_epoch`, `capital_flow_reconciliation`, `cash_flow_ledger`, `binance_protection_registry`, `conditional_stop_lifecycle`, `confirmed_rr_exit`, `binance_hwm_incident_repair`, `financial_namespace`, `operational_incident_recovery`, `paper_lifecycle`, `paper_wallet`, `paper_validation_reset`, `daily_pnl_storage` | Conexão operacional (inalterado) |
| Diagnóstico com escrita | `binance_accounting_evidence`, `exchange_accounting_evidence`, `binance_exit_forensics`, `post_trade_forensics`, `external_origin_observation`, `external_position_performance`, `bbo_cost_shadow_runtime`, `nexus_confidence_evidence`, `nexus_shadow_research_runtime`, `confidence_outcome_linking`, `optimizer`, `score` | Conexão operacional (escrevem; fora do escopo) |
| Pesquisa com escrita (`ensure_*`, DDL, persistência de candidatos/outcomes) | `hard_gate_shadow_scan` (persist), `risk_epoch_shadow`, `segregated_pilot_ledger_v1`, `short_down_bos_prospective_v1`, `prospective_oos_post_gate_v2`, `low_capital_shadow_cohort_v1`, `htf_transition_calibration`, `missed_opportunity_audit`, `rr_gate_calibration`, `score_floor_shadow_persistence`, `session_penalty_persistence`, `champion_challenger_forward_v1`, `evidence_stores_v2` | Conexão operacional (escrevem) |
| Pesquisa somente leitura (22 funções, verificadas por AST) | `bbo_calibration_v2/v3`, `calibration_failure_analysis_v1`, `counterfactual_approval_failure_analysis_v1`, `min_order_capital_adequacy_v1`, `min_order_capital_ladder_v1`, `min_order_counterfactual_decision_review_v1`, `min_order_counterfactual_gate_attribution_v1`, `min_order_counterfactual_nexus_v1`, `min_order_counterfactual_threshold_sensitivity_v1`, `min_order_counterfactual_validation_v1`, `min_order_frontier_audit_v1`, `prospective_oos_cohort_v1.snapshot`, `prospective_oos_enrollment_audit_v1`, `prospective_oos_first_approval_review_v1`, `prospective_oos_maturation_review_v1`, `short_down_bos_v2_outcome_proof`, `v4_prospective_ablation_phase_a` | **Leitor dedicado** quando a flag está ligada |
| Pesquisa somente leitura fora do scan | `oos_calibration_readiness` (só no fechamento de trades PAPER) | Conexão operacional (baixa frequência; documentado) |

## Desenho (`bot/oos_research_reader_v2.py`)

- Conexão asyncpg própria, aberta com `OOS_DIAGNOSTIC_DATABASE_URL` (DSN de menor privilégio introduzido no #639). Sessão com `default_transaction_read_only=on`, `statement_timeout=2500`, `lock_timeout=1000`, `idle_in_transaction_session_timeout=5000`, `application_name=nexus_oos_research_v2`.
- Nunca usa a conexão operacional nem `database._io_lock`. Sem DSN, ou se não conseguir conectar, a leitura falha com `ResearchReaderUnavailable` e o readout registra `status=ERROR` (fail-closed).
- Isolamento de cancelamento: a consulta não é cancelada pelo protocolo do asyncpg. Em timeout próprio (3 s) ou cancelamento do chamador, a conexão é encerrada com `terminate()` e descartada, e a próxima leitura abre outra. A contaminação do `cancel_waiter`/`cancel_sent_waiter` (PR #641) não pode ocorrer nesta conexão.
- Concorrência limitada (semáforo de 1, espera máxima de 1 s, depois `ResearchReaderBusy`).
- Reconexão independente, preguiçosa, com intervalo mínimo de 5 s após falha de conexão.
- Somente `SELECT`/`WITH`; `_exec` é rejeitado.
- `prospective_oos_cohort_v1` trata este leitor como isolado (nunca roda DDL por ele), como já fazia com o leitor v1.
- Métricas: `[OOS_RESEARCH_READER_V2]` a cada 5 min com contadores e p50/p95/p99 de espera e de fetch.

## Resultados

Benchmark local reproduzível (`scripts/bench_oos_reader_isolation.py`): PostgreSQL 16 real atrás de proxy com 70 ms por sentido (RTT 140 ms, como ams para sfo), 2.200 candidatos e 4.400 outcomes, heartbeat real (`acquire` + `validate`) a cada 1 s, sonda de lock operacional a cada 250 ms e os 7 emissores MIN_ORDER reais em loop. 60 s por cenário, duas rodadas (valores da rodada 2; a rodada 1 difere em poucos ms).

| ms | A sem pesquisa | B pesquisa na conexão compartilhada (produção atual) | C leitor dedicado |
|---|---|---|---|
| heartbeat p50 / p95 / p99 | 1026 / 1032 / 1105 | 1274 / 1343 / 1391 | 1087 / 1160 / 1169 |
| lock_wait p50 / p95 / p99 | 0 / 459 / 464 | 222 / 812 / 852 | 0 / 505 / 544 |
| consulta operacional p50 / p95 / p99 | 142 / 600 / 605 | 363 / 953 / 997 | 144 / 649 / 687 |
| rodada de pesquisa p50 | n/a | 7080 | 3184 |

O `lock_wait` residual de A e C vem do próprio heartbeat (transação de ~5 round trips) disputando o lock com a sonda.

Teste de travamento (`tests/test_oos_research_reader_v2_postgres.py`): outra sessão segura `ACCESS EXCLUSIVE` na tabela de candidatos por 3 s. Com o leitor dedicado, o heartbeat mantém o custo normal e `db._io_lock` nunca é tocado. No controle (flag desligada), a mesma leitura travada eleva o heartbeat em mais de 800 ms.

## Rollout

1. Confirmar que `OOS_DIAGNOSTIC_DATABASE_URL` aponta para um usuário com permissão só de `SELECT` nas tabelas de pesquisa.
2. Implantar com a flag desligada (sem mudança de comportamento).
3. Ligar `OOS_RESEARCH_READER_V2=true` e observar por 30 min: `[OOS_RESEARCH_READER_V2] ... errors=0 unavailable=0`, `heartbeat_renewed` contínuo, ausência de `fetchall:shadow_candidates` em `OOS_DB_LOCK_HOLD_V1`.

## Rollback

Desligar `OOS_RESEARCH_READER_V2` (volta exatamente ao caminho anterior; não há schema, dado ou estado novo). Se necessário, reverter o merge.
