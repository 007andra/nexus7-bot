# NEXUS-7 Risk Epoch V1: período operacional com limite de 30%

Data da auditoria: 2026-10-08. Branch base: `migration/binance-usdm` @ `0258a2b`.

## 1. Produção verificada (antes desta mudança)

| Item | Evidência |
|---|---|
| Serviço | `nexus7-bot`, projeto BGX Capital, ambiente `production`, região `ams`, 1 réplica |
| Deployment ativo | `6da7d94b-c8be-4eb8-97d2-8d420642d021`, `SUCCESS`, criado 2026-10-08T15:26:40Z |
| SHA | `0258a2b154a7b32bc01a853e848c1e3fa7e81b1c` (merge do PR #588) |
| Modo | `[PILOT_READINESS] paper_trade=false release_approved=true`, `[CONTROLLED_PILOT_RELEASE] authorized=true` |
| MAX_DRAWDOWN | Carregado como 30%: `[DRAWDOWN_HARD_GATE_INSTANCE] drawdown=76.68% configured_limit=30.00%` e `[REENTRY_READINESS_V1] configured_limit=0.3` |
| Alavancagem | 50x, `margin_type=CROSSED` em todos os 25 símbolos (leitura `/fapi/v1/symbolConfig`, sem mutação) |
| One-shot | `[CONTROLLED_LIVE_REENTRY_V1] enabled=true envelope_configured=true configured=false armed=false episode_id=PR575_SHADOW_VALIDATION_20261007_V1 loss_budget_usdt=0.1 max_risk_pct=0.02 reason=manual_arm_missing` |
| Conta | `[PRELIVE_ACCOUNT_EXPOSURE] positions=0 active_orders=0`, `[BINANCE_LEGACY_ALGO_INVENTORY] result=ZERO` |

Os valores das variáveis do Railway vêm redigidos para o conector OAuth; o valor efetivo de `MAX_DRAWDOWN` foi confirmado pelos logs do próprio processo, que é a fonte mais forte (é o valor que o código leu).

## 2. Origem dos 76,68%

Cadeia, em ordem de execução no `_update_balance` LIVE (`bot/nexus_runtime_engine.py`):

1. Equity autenticada da Binance: `equity=5.3177` (`[PILOT_LIVE_BALANCE]`, `collateral_basis=availableBalance`).
2. Ledger de fluxos externos (`bot/cash_flow_ledger.py`, chave `risk:external_cash_flows:binance:ledger:v1`): `external_flows_applied=2 external_flows_net=7.7808 pending_flows=0`. Cada fluxo rebaseia o HWM por TWR (`P' = P * E+/E-`), então depósito não reduz drawdown e saque não aumenta.
3. HWM durável no PostgreSQL (`bot/drawdown_persistence.py`, chave `risk:account_equity_peak:<namespace v3>` + proveniência atômica): `[DURABLE_DRAWDOWN] equity=5.3177 peak_equity=22.7987 drawdown=76.68% source=restored persistence=unchanged`.
4. RiskManagerV3 (`bot/risk_manager_v3.py`): `drawdown = (peak - equity) / peak = (22.7986938551 - 5.31768526) / 22.7986938551 = 0.766754828`. `can_open()` bloqueia com `drawdown >= cfg.MAX_DRAWDOWN`.
5. Equity necessária para voltar abaixo de 30% só por performance: `22.7987 * 0.70 = 15.9591` USDT, lacuna de 10.6414 USDT (`[REENTRY_READINESS_V1] required_equity_for_limit=15.9590856986`). Aporte externo não fecha essa lacuna, por desenho.

Nada disso foi alterado.

## 3. O que este PR implementa

`bot/risk_epoch.py`, autoridade `TIGHTEN_ONLY`:

- Época identificada por `RISK_EPOCH_ID`, limite próprio `RISK_EPOCH_MAX_DRAWDOWN` (padrão e teto rígido de 30%).
- Baseline criada uma única vez, por CAS no PostgreSQL, só com: capital autenticado confirmado, conta flat, zero ordens pendentes, zero fluxo externo pendente, HWM histórico presente. Registra equity inicial, HWM e drawdown históricos no início, limite histórico vigente, impressão digital do ledger de fluxos, SHA do código e a época anterior. Os campos imutáveis carregam um SHA-256 (`baseline_digest`) que detecta corrupção ou deriva acidental; não é autenticação contra quem tem escrita no banco.
- `epoch_drawdown = (epoch_peak - equity) / epoch_peak`, com `epoch_peak` subindo só com equity autenticada observada. Toda mudança de equity é gravada (checkpoint durável com `last_equity`, `min_equity` e `max_epoch_drawdown`), então uma perda abaixo do limite sobrevive a reinício e entra no resumo da época sucessora.
- Estados que bloqueiam: `PENDING_BASELINE`, `BREACHED` (persistente, sobrevive a restart e a recuperação de equity), `INVALID` (adulteração, troca de limite, reuso de id), `FLOW_CHANGED` (qualquer aporte/saque após a baseline, ou alteração de valor de um fluxo já aplicado: a impressão digital cobre id, identidades, valor líquido, equity pré/pós e HWM ajustado, e os totais são conferidos), `UNKNOWN` (falha de persistência), `CONFIG_INVALID`.
- Qualquer época sucessora, com a anterior ACTIVE ou BREACHED, exige `RISK_EPOCH_SUPERSEDE_ACK=<id exato da anterior>`. Sem isso nenhuma sucessora é criada, então trocar só o `RISK_EPOCH_ID` não reinicia o medidor. A anterior é encerrada formalmente na mesma transação (`closed`, `closed_by_epoch_id`, `closing_status`), mantendo baseline e evidência de quebra; uma época encerrada não pode ser reativada. O índice é só de acréscimo.
- Nunca escreve HWM, proveniência, ledger, PnL ou estado de exchange.

Integração:

| Ponto | Efeito |
|---|---|
| `pilot_live_runtime._entry_drawdown_allows_durable` | Gate histórico primeiro, sem mudança. Se passar (normal, override, bridge ou recovery), a época faz nova observação com equity fresca e pode vetar. Época desabilitada: zero I/O, zero efeito. |
| `controlled_live_reentry_v1.readiness` | O one-shot agora exige época `ACTIVE` e `equity - loss_budget >= epoch_floor`. |
| `operator_runtime_policy` (a cada atualização de saldo) | Observa e emite `[RISK_EPOCH_V1]`. |
| `reentry_readiness` | Campos `historical_drawdown`, `historical_drawdown_limit`, `epoch_status`, `epoch_drawdown`, `epoch_drawdown_limit`, `epoch_floor_equity`. |
| `status_observability` | `risk_drawdown` na API de status e bloqueador `RISK_EPOCH_GATE` quando a época veta. |

O drawdown da época nunca é publicado com o nome `historical_drawdown`.

### Antes e depois

| Cenário | Antes | Depois (época desabilitada) | Depois (época ativa) |
|---|---|---|---|
| Entrada normal com drawdown histórico 76,68% | Bloqueada | Bloqueada | Bloqueada |
| One-shot armado | Permitido pela bridge | Bloqueado: `risk_epoch_disabled` | Permitido só se época ACTIVE e orçamento cabe acima do piso |
| Histórico abaixo do limite, época rompida | Permitido | Permitido | Bloqueado |
| HWM, proveniência, ledger | Inalterados | Inalterados | Inalterados (testado byte a byte) |

## 4. Auditoria dos controles independentes (evidência de produção)

| Controle | Estado observado |
|---|---|
| NEXUS | Chamado e decide; `[CANDIDATE_TERMINAL] nexus_called=true nexus_allowed=true` em vários candidatos shadow. Calibração com problema: `CALIBRATION_FAILURE_ANALYSIS_V1 status=CALIBRATION_INVERSION_CONFIRMED_BOTH_HORIZONS`. |
| EV / R:R | `rr_current_min=1.600`. Atribuição: `RR_BELOW_MIN:122, EV_NEGATIVE:112` rejeições. `COUNTERFACTUAL_APPROVAL_FAILURE_ANALYSIS_V1 status=NEGATIVE_SELECTION_CONFIRMED_BOTH_HORIZONS lift60=-0.004091`. |
| Custos | BBO V3 `cost_only_valid=1802`, custo estático médio 30.5 bps vs BBO 16.8 bps; `promotion_allowed=false`. Custos estáticos são conservadores. |
| Dimensionamento | `[FINAL_SIZING_INVARIANT] final_quantity_policy=min(stop_risk_qty,operator_margin_cap_qty) risk_authority=RiskManagerV3 fail_closed=true`; `max_risk_pct=0.0025`, `margin_fraction=0.25`. |
| Ordem mínima | `MIN_ORDER_CAPITAL_ADEQUACY_V1 min_order_only_blocked=302 required_equity_median=9.6374` com equity 5.3177. Uma fração dos sinais é inviável por `MIN_NOTIONAL_BINDING`. |
| CROSS stress | Gate fail-closed instalado (`fail-closed CROSS portfolio stop-stress gate` nos overlays), `MAX_STOP_STRESS_RISK_RATE=0.90`, provado offline em `tests/test_binance_cross_stress_dispatch_proof.py`. |
| STOP_MARKET / proteção | `[PROTECTION_FLAT_SWEEP] result=VERIFIED symbols=25`; proteção nativa `STOP_MARKET` em `bot/binance.py`. Sem posição aberta, não há proteção ativa a verificar. |
| Saldo real | Leitura autenticada `/fapi/v1/accountConfig canTrade=True multiAssetsMargin=False`; cash-flow ledger `RECONCILED`. |
| Persistência | `[OWNERSHIP_STATE] startup_validated fencing_valid=true`, `[INITIAL_RECONCILIATION] complete=true durable_state_ready=true`, CAS PostgreSQL isolado (PR #585). |
| Reconciliação | `[PRIVATE_STREAM] reconciled exposure_verified=true orders_converged=true`; `[BINANCE_ACCOUNTING_EVIDENCE] status=PASS unknown=1 release_state=AWAITING_CONTROLLED_LIVE_EVIDENCE`. |
| Evidência quantitativa | `PROSPECTIVE_OOS_COHORT_V1 status=EVIDENCE_FAIL blockers=PERFORMANCE_CRITERIA,CONCENTRATION_CRITERIA allowed60_avg=-0.003379 rejected60_avg=0.000546 allowed240_avg=-0.013089`. |

## 5. Piloto LIVE excepcional de operação única

Mecanicamente é possível e limitado: orçamento absoluto de 0.10 USDT (1,88% da equity de 5.3177), uma posição, uma submissão, episódio consumido de forma durável antes do POST, e agora sujeito a uma época de 30% cujo piso (3.7224 USDT para baseline 5.3177) fica 1.495 USDT acima da equity pós-perda máxima. Nenhuma execução foi autorizada nesta auditoria.

A evidência quantitativa é contra: as aprovações do NEXUS rendem menos que as rejeições nos dois horizontes da coorte prospectiva. O piloto só serve como prova de execução (ordem, proteção, reconciliação), não como prova de edge, que é exatamente o que o freeze do `CONTROLLED_LIVE_REENTRY_V1` já diz.

## 6. Requisitos que ainda impedem ordens reais

1. Drawdown histórico 76,68% acima de 30%: o gate global continua bloqueando todas as entradas normais. Liberar sem a bridge exige equity de 15.9591 USDT por performance.
2. `CONTROLLED_LIVE_REENTRY_ARM` ausente (`reason=manual_arm_missing`). É a única porta para uma ordem real hoje, e é manual por contrato.
3. Com este PR: `RISK_EPOCH_ENABLED=true` e `RISK_EPOCH_ID` definidos, baseline criada e `[RISK_EPOCH_V1] status=ACTIVE` confirmado em log no SHA implantado.
4. Evidência de edge: `PROSPECTIVE_OOS_COHORT_V1 EVIDENCE_FAIL` e seleção negativa confirmada. Não é um gate de código, é a razão para não armar.
5. Merge deste PR com CI verde e verificação do runtime no novo SHA.

## 7. Plano de reversão

- Reversão operacional sem deploy: remover `RISK_EPOCH_ENABLED` (ou `false`). A época deixa de bloquear e de fazer I/O; o one-shot volta a ficar bloqueado com `risk_epoch_disabled` (mais restritivo que antes, por desenho).
- Reversão de código: `git revert <merge commit>` e redeploy. Registros `risk_epoch:v1:*` no `key_value` ficam inertes e podem ser mantidos como histórico; nenhuma outra chave foi escrita.
- Rollback imediato no Railway: redeploy do deployment `6da7d94b-c8be-4eb8-97d2-8d420642d021` (SHA `0258a2b`).

## 8. Testes

- `tests/test_risk_epoch_postgres.py` (15, PostgreSQL 16 real, obrigatório no CI): baseline verificável, preservação byte a byte de HWM/proveniência/ledger, pré-condições de baseline, reinício com novo SHA, adulteração, limite imutável, quebra persistente pós-restart, fluxo externo, criação concorrente (4 sessões), cadeia de épocas com ACK, gate histórico ainda soberano, veto da época com gate histórico passando, época desabilitada sem I/O, perda sub-limite durável até a sucessora, alteração de valor com mesma identidade de fluxo, sucessão ACTIVE sem ACK ou com ACK errado.
- `tests/test_risk_epoch.py` (15): configuração, matemática, digest, matriz de bloqueio, telemetria com nomes separados, API de status, log de startup.
- `tests/test_controlled_live_reentry_v1.py` (+6): one-shot exige época ACTIVE com folga.
- Suíte offline completa: 2796/2796 (antes 2763/2763), `MANDATORY_SKIPPED=0`; `bot.release_proof` PASS.
