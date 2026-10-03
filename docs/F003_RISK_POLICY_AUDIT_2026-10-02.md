# F-003 — Auditoria quantitativa da política de sizing LIVE

Data: 2026-10-02 · Base: `claude/audit-algorithmic-trading-system-uf3zxo` @ `fb5816b` · Tipo: **auditoria + decisão de política, sem patch de produção**.

Reprodução: `python research/risk_policy/f003_sizing_audit.py` (determinístico, offline; Monte Carlo com seed fixa).
Prova contra o runtime real: `python -m tests.run_offline tests.test_f003_sizing_characterization`. Esse teste chama o
`bot.engine.minimum_base_quantity` **final da composição LIVE-pilot** (incluindo o gate RiskManagerV3 real e
`final_loss_budget`) e confirma que o espelho do script reproduz o resultado em >150 vetores.

> Preços de referência (BTC 60 000, ETH 3 000, SOL 150, AVAX 30, XRP 0,60, DOGE 0,15) são **ilustrativos**.
> Multiplicadores vêm das fixtures/comentários do próprio repositório; não houve consulta à web.
> Valores de ambiente são os **defaults do código**. Os valores reais no Railway não estão no repositório (ver decisões).

> **Status (implementação F-003):** política aplicada — risco por trade = `MAX_RISK_PCT` (1% da equity),
> risco aberto agregado = `MAX_OPEN_RISK_PCT` (2%), `MAX_MARGIN_PCT` apenas como teto, sem compressão de stop,
> barreira pré-dispatch no transporte (`bot/risk_budget.py`). As seções abaixo descrevem o comportamento
> **anterior** (pré-F003), mantido como referência em `current_policy` do script. **Deploy LIVE bloqueado até F-013.**

---

## 1. ACTIVE SIZING CALL GRAPH (composição real, resolvida dinamicamente)

```
Analyzer.analyze_mtf (strategy.py:522)  → SL/TP técnicos: sl_atr·{1.2|1.5|2.0}, R:R ≥ MIN_RR_RATIO=2
  → NEXUS validate (+ kucoin_contract_risk_hardening._geometry_from_exact_mmr:112)
       pode COMPRIMIR sig.sl/sig.tp (ADJUSTED) para caber antes da liquidação, retained ≥ 40%
  → nexus_runtime_engine:139  risk.set_plan(entry, stop=sig.sl, risk_pct=_effective_risk_pct()=MAX_RISK_PCT 1%)
       + update_capital(CapitalState(equity, availableMargin)), V3.can_open (drawdown<10%, posições<2)
  → TradingEngine._open  (post_trade_forensics:168 → … → operator_loss_policy (só valida geometria)
       → kucoin_contract_risk_hardening → pilot_live_runtime._open_live_pilot (refresh → _pilot_available_balance,
         _PILOT_TARGET_NOTIONAL = 50% available — PRELIMINAR) → pilot_risk_cap_hardening (contextvars ENGINE/SYMBOL/SIGNAL)
       → prelive_protection_failclosed → core engine._open)
  → engine.py:2785  qty = minimum_base_quantity(info, sig.entry)     ← hook de sizing do piloto
       cadeia do hook (de fora para dentro):
         1. final_sizing_invariants._final_operator_authoritative_quantity:58   ← VENCE (não chama os de baixo no LIVE-pilot)
         2. operator_runtime_policy._margin_target_quantity:234                 (sombreado)
         3. pilot_risk_cap_hardening._risk_authoritative_pilot_quantity:73      (sombreado: min(risk,target))
         4. pilot_live_runtime._pilot_aware_minimum                             (sombreado: 50% notional)
         5. quantity.minimum_base_quantity                                       (sombreado)
  → engine.py:2788  affordability: qty·entry·(1/L + taker) ≤ fresh available
  → validate_base_quantity (lot/minQty/minNotional)
  → _refresh_entry_balance (pilot_risk_cap): microestrutura + final_loss_budget.validate no preço executável
  → place_order(qty base, sl=sig.sl, tp=sig.tp) → _round_qty: base → contratos → POST /api/v1/orders
```

Funções que podem modificar cada grandeza:

| grandeza | quem pode modificar (ativo) | quem tenta mas está sombreado/inativo |
|---|---|---|
| risk_pct | `engine._effective_risk_pct` (MAX_RISK_PCT 1%, ou POST_TARGET_RISK 0,5% após a meta diária) → só alimenta o **gate** V3 | legacy `RiskManager.size` (interpreta como fração de notional) |
| margin fraction | `final_sizing_invariants.MARGIN_FRACTION=0.50` | `operator_runtime_policy.MARGIN_FRACTION`; `PILOT_NOTIONAL_PCT` (notional) |
| notional | `final_sizing_invariants` = 0,5·A·L | `pilot_live_runtime` (0,5·A) |
| leverage | `cfg.LEVERAGE` (env, default 10); não muda em runtime | — |
| qty | `final_sizing_invariants` (floor por lote) | `pilot_risk_cap` min(); legacy |
| max contracts | lot floor; sem cap explícito de contratos | — |
| available balance | `pilot_live_runtime._refresh_account` → `_pilot_available_balance` (availableMargin) | `ProfessionalRiskAdapter._reconcile_latest_available` (só para o gate) |
| stop distance | `strategy` (ATR×mult), `kucoin_contract_risk_hardening` (compressão por liquidação), engine.py:3476 (shift pós-fill, F-013) | `operator_loss_policy.stop_price` (legado, não usado) |

## 2. FINAL AUTHORITY

**`bot/final_sizing_invariants.py:_operator_target_quantity`** escolhe a quantidade enviada à KuCoin:

```
contracts = floor( available × 0.50 × LEVERAGE / (price × multiplier) / lot ) × lot      (≥ minQty)
qty_base  = contracts × multiplier
```

O RiskManagerV3 é chamado só como **gate binário**: `_select_final_quantity` devolve `target_qty` sempre que
`risk_qty > 0`. A seguir `final_loss_budget.validate` exige `qty·(|entry−stop| + entry·cost) ≤ 0,5·margin`.
Prova numérica (BTCUSDT, equity = available = 100, L = 10, o mesmo valor vem do callable real):

| stop | target contracts | V3 risk_qty (1%) | final | notional | margin | perda projetada | decisão |
|---|---|---|---|---|---|---|---|
| 0,5% | 8 | 1 contrato | **8** | 480 | 48 | 3,46 (3,46%) | ACCEPT |
| 1,0% | 8 | 1 contrato | **8** | 480 | 48 | 5,86 (5,86%) | ACCEPT |
| 2,0% | 8 | 0 (1 contrato perde 1,33 > 1,00) | 0 | — | — | — | REJECT (gate V3) |

O "risco 1%" não define o tamanho. Ele só decide **se** o trade sai; quando sai, o tamanho é 8× o que o orçamento de 1% compraria.

## 3. UNIT MAP

| etapa | entrada | saída | unidade |
|---|---|---|---|
| account overview | KuCoin accountEquity / availableMargin | `equity`, `available` | USDT collateral |
| target margin | available | 0,5·available | margin USDT |
| target notional | margin × L | notional | notional USDT |
| contracts | notional / (price·mult) | floor por lot | contracts |
| qty | contracts × mult | qty | base asset |
| leverage | cfg.LEVERAGE | L | adimensional |
| stop | sig.sl | \|entry−sl\| | preço |
| stop % | \|entry−sl\|/entry | s | fração do preço |
| custo | 2·taker + 2·slip | c | fração do notional |
| perda projetada | qty·(\|entry−sl\| + entry·c) | loss | USDT |
| perda % equity | loss / equity | — | % equity |
| limite atual | 0,5·margin = 0,25·available·(margin/target) | — | USDT |

Mistura de unidades encontrada: o **limite** de perda está em unidades de margem (`0,5·margin`), não de equity. O
gate V3 usa `equity`, enquanto o tamanho final usa `available`.

## 4. CURRENT FORMULA

```
margin   = 0.50 × A                      (A = availableMargin no momento da entrada)
notional = margin × L = 0.5·A·L
loss     = notional × (s + c)
admitido ⇔ s + c ≤ 0.5 / L               (final_loss_budget)
        ∧ 1 contrato ≤ min(1%·E/(P·(s+0.22%)), 10%·A·L/P)/mult   (gate V3)
        ∧ s ≤ liq_move(L) − 0.30 pp      (liquidação; senão comprime ou bloqueia)
⇒ loss ≤ 0.5·A·L·(0.5/L) = 0.25·A        — independente de L e de s
```

## 5. CURRENT NUMERIC EXAMPLE (equity = available = 100, política de 50%)

### Continuous (unquantized) current policy, equity = available = 100 USDT
Cost model: 2×taker 0.06% + 2×slippage 0.05% (majors) = 0.22% of notional.

| lev | margin | notional | stop | gross@SL | fees | slippage | total | %equity | admitted? |
|---|---|---|---|---|---|---|---|---|---|
| 1x | 50.00 | 50.00 | 0.5% | 0.25 | 0.06 | 0.05 | 0.36 | 0.36% | yes |
| 1x | 50.00 | 50.00 | 1.0% | 0.50 | 0.06 | 0.05 | 0.61 | 0.61% | yes |
| 1x | 50.00 | 50.00 | 2.0% | 1.00 | 0.06 | 0.05 | 1.11 | 1.11% | yes |
| 1x | 50.00 | 50.00 | 3.0% | 1.50 | 0.06 | 0.05 | 1.61 | 1.61% | yes |
| 1x | 50.00 | 50.00 | 4.0% | 2.00 | 0.06 | 0.05 | 2.11 | 2.11% | yes |
| 1x | 50.00 | 50.00 | 5.0% | 2.50 | 0.06 | 0.05 | 2.61 | 2.61% | yes |
| 5x | 50.00 | 250.00 | 0.5% | 1.25 | 0.30 | 0.25 | 1.80 | 1.80% | yes |
| 5x | 50.00 | 250.00 | 1.0% | 2.50 | 0.30 | 0.25 | 3.05 | 3.05% | yes |
| 5x | 50.00 | 250.00 | 2.0% | 5.00 | 0.30 | 0.25 | 5.55 | 5.55% | yes |
| 5x | 50.00 | 250.00 | 3.0% | 7.50 | 0.30 | 0.25 | 8.05 | 8.05% | yes |
| 5x | 50.00 | 250.00 | 4.0% | 10.00 | 0.30 | 0.25 | 10.55 | 10.55% | yes |
| 5x | 50.00 | 250.00 | 5.0% | 12.50 | 0.30 | 0.25 | 13.05 | 13.05% | yes |
| 10x | 50.00 | 500.00 | 0.5% | 2.50 | 0.60 | 0.50 | 3.60 | 3.60% | yes |
| 10x | 50.00 | 500.00 | 1.0% | 5.00 | 0.60 | 0.50 | 6.10 | 6.10% | yes |
| 10x | 50.00 | 500.00 | 2.0% | 10.00 | 0.60 | 0.50 | 11.10 | 11.10% | yes |
| 10x | 50.00 | 500.00 | 3.0% | 15.00 | 0.60 | 0.50 | 16.10 | 16.10% | yes |
| 10x | 50.00 | 500.00 | 4.0% | 20.00 | 0.60 | 0.50 | 21.10 | 21.10% | yes |
| 10x | 50.00 | 500.00 | 5.0% | 25.00 | 0.60 | 0.50 | 26.10 | 26.10% | no: loss budget |
| 20x | 50.00 | 1,000.00 | 0.5% | 5.00 | 1.20 | 1.00 | 7.20 | 7.20% | yes |
| 20x | 50.00 | 1,000.00 | 1.0% | 10.00 | 1.20 | 1.00 | 12.20 | 12.20% | yes |
| 20x | 50.00 | 1,000.00 | 2.0% | 20.00 | 1.20 | 1.00 | 22.20 | 22.20% | yes |
| 20x | 50.00 | 1,000.00 | 3.0% | 30.00 | 1.20 | 1.00 | 32.20 | 32.20% | no: loss budget |
| 20x | 50.00 | 1,000.00 | 4.0% | 40.00 | 1.20 | 1.00 | 42.20 | 42.20% | no: loss budget |
| 20x | 50.00 | 1,000.00 | 5.0% | 50.00 | 1.20 | 1.00 | 52.20 | 52.20% | no: loss budget |
| 50x | 50.00 | 2,500.00 | 0.5% | 12.50 | 3.00 | 2.50 | 18.00 | 18.00% | yes |
| 50x | 50.00 | 2,500.00 | 1.0% | 25.00 | 3.00 | 2.50 | 30.50 | 30.50% | no: loss budget |
| 50x | 50.00 | 2,500.00 | 2.0% | 50.00 | 3.00 | 2.50 | 55.50 | 55.50% | no: loss budget |
| 50x | 50.00 | 2,500.00 | 3.0% | 75.00 | 3.00 | 2.50 | 80.50 | 80.50% | no: loss budget |
| 50x | 50.00 | 2,500.00 | 4.0% | 100.00 | 3.00 | 2.50 | 105.50 | 105.50% | no: loss budget |
| 50x | 50.00 | 2,500.00 | 5.0% | 125.00 | 3.00 | 2.50 | 130.50 | 130.50% | no: loss budget |

### Worst admissible projected loss per trade (continuous)
| lev | 0.5/L | liq move | max stop by loss budget | max admissible stop | worst loss USDT | worst %equity |
|---|---|---|---|---|---|---|
| 1x | 50.00% | 100.00% | 49.78% | 49.78% | 25.00 | 25.00% |
| 5x | 10.00% | 19.63% | 9.78% | 9.78% | 25.00 | 25.00% |
| 10x | 5.00% | 9.58% | 4.78% | 4.78% | 25.00 | 25.00% |
| 20x | 2.50% | 4.56% | 2.28% | 2.28% | 25.00 | 25.00% |
| 50x | 1.00% | 1.55% | 0.78% | 0.78% | 25.00 | 25.00% |

Leitura: com tamanho fixo pela margem, a perda é **proporcional a L × s**. O teto de 25% é constante: alavancagem
maior apenas estreita o stop admissível (0,78% a 50x).

## 6. MAX LOSS PER TRADE

**Pior perda projetada no stop permitida hoje: 0,25 × available ≈ 25 USDT em 100 USDT (25% da equity com conta flat), em qualquer leverage.**
O cálculo já inclui custos modelados (0,22% em majors / 0,32% em alts).

- Atingível em contratos finos (AVAX, XRP, SOL com equity ≥ 25). Teste `test_worst_admissible_loss_is_quarter_of_available`: AVAX 166 contratos, perda 24,99.
- Em contratos grossos o gate V3 corta o extremo. Exemplo: BTC com equity 100 só passa com stop ≤ ~1,45% (1 contrato × (s+0,22%) ≤ 1% da equity); a perda máxima aceita fica em ≈8%. **O limite efetivo depende da granularidade do contrato.**
- Não modelado (pode exceder): gap através do stop, slippage do stop acima de 0,05–0,10%, funding, ADL.

## 7. MAX AGGREGATE LOSS (2 e 3 posições)

A segunda entrada é dimensionada sobre o available **restante** (cross: availableMargin cai pela margem da primeira).

| position # | available at entry | margin | notional | worst loss | cumulative | % equity | × daily stop (3%) | × max DD (10%) |
|---|---|---|---|---|---|---|---|---|
| 1 | 100.00 | 50.00 | 500.00 | 25.00 | 25.00 | 25.00% | 8.3× | 2.5× |
| 2 | 50.00 | 25.00 | 250.00 | 12.50 | 37.50 | 37.50% | 12.5× | 3.8× |
| 3 | 25.00 | 12.50 | 125.00 | 6.25 | 43.75 | 43.75% | 14.6× | 4.4× |

- MAX_POSITIONS = 2 ⇒ pior agregado ≈ **37,5% da equity**, com duas posições abertas simultaneamente batendo os dois stops. Isso é 12,5× o stop diário de 3% e 3,8× o drawdown máximo de 10%.
- A linha de 3 posições é hipotética (o runtime limita a 2).
- Mesma direção e altcoins correlacionadas: o pior caso é a soma acima, sem nenhum desconto de correlação. Não há cap de correlação.
- O limite ingênuo "2 × 25% = 50%" do audit v2 superestima: a segunda posição usa 50% do restante, não do total.

## 8. DAILY DRAWDOWN / CONSERVATIVE / STOP STREAK

| guarda | regra | efeito sobre um trade de pior caso (−25%) |
|---|---|---|
| stop diário | `DAILY_STOP_LOSS_PCT` 3% da equity, avaliado **depois** de perdas realizadas | um único trade pode perder **8,3×** o limite diário antes de a guarda agir |
| drawdown | `MAX_DRAWDOWN` 10% em `V3.can_open` (bloqueia o gate) | um único trade leva o DD a 25%, e o bot para de abrir posições até rebase do HWM |
| modo conservador | `POST_TARGET_RISK` 0,5% após a meta diária → só muda o risk_pct do **gate** | não reduz o tamanho final (que é fixado pela margem) |
| stop streak | `consecutive_losses` só gera notificação Telegram | não há gate |

Resposta: **sim, um único trade pode ultrapassar o limite diário inteiro (8,3×) e o drawdown máximo (2,5×).**

## 9. LEVERAGE SENSITIVITY

### SOLUSDT @150, stop 0.5%, equity=available=100
| leverage | CURRENT contracts / loss | CURRENT margin | RISK 1% contracts / loss | RISK 1% margin |
|---|---|---|---|---|
| 5x | 16c / 1.73 | 48.00 | 9c / 0.97 | 27.00 |
| 10x | 33c / 3.56 | 49.50 | 9c / 0.97 | 13.50 |
| 20x | 66c / 7.13 | 49.50 | 9c / 0.97 | 6.75 |
| 50x | 166c / 17.93 | 49.80 | 9c / 0.97 | 2.70 |

Na política atual, a perda no stop escala linearmente com L. Esta é a violação de INV-RISK-LEVERAGE-001. Na
política risk-based a perda é constante e só a margem muda.

## 10. STOP-DISTANCE SENSITIVITY (A atual vs B 0,25% / C 0,5% / D 1% / E 2%)

### SOLUSDT @150, equity=available=100, L=10 (contracts / projected loss incl. costs)
| stop | A CURRENT | B 0.25% | C 0.5% | D 1.0% | E 2.0% |
|---|---|---|---|---|---|
| 0.25% | 33c / 2.33 (2.33%) | 3c / 0.21 (0.21%) | 7c / 0.49 (0.49%) | 14c / 0.99 (0.99%) | 28c / 1.97 (1.97%) |
| 0.50% | 33c / 3.56 (3.56%) | 2c / 0.22 (0.22%) | 4c / 0.43 (0.43%) | 9c / 0.97 (0.97%) | 18c / 1.94 (1.94%) |
| 0.75% | 33c / 4.80 (4.80%) | 1c / 0.15 (0.15%) | 3c / 0.44 (0.44%) | 6c / 0.87 (0.87%) | 13c / 1.89 (1.89%) |
| 1.00% | 33c / 6.04 (6.04%) | 1c / 0.18 (0.18%) | 2c / 0.37 (0.37%) | 5c / 0.92 (0.92%) | 10c / 1.83 (1.83%) |
| 1.50% | 33c / 8.51 (8.51%) | REJECT one_contract_exceeds_budget | 1c / 0.26 (0.26%) | 3c / 0.77 (0.77%) | 7c / 1.81 (1.81%) |
| 2.00% | 33c / 10.99 (10.99%) | REJECT one_contract_exceeds_budget | 1c / 0.33 (0.33%) | 3c / 1.00 (1.00%) | 6c / 2.00 (2.00%) |
| 3.00% | 33c / 15.94 (15.94%) | REJECT one_contract_exceeds_budget | 1c / 0.48 (0.48%) | 2c / 0.97 (0.97%) | 4c / 1.93 (1.93%) |
| 5.00% | REJECT final_loss_budget(stop+cost>0.5/L) | REJECT one_contract_exceeds_budget | REJECT one_contract_exceeds_budget | 1c / 0.78 (0.78%) | 2c / 1.57 (1.57%) |

## 11. COST MODEL

| item | valor | origem | classe |
|---|---|---|---|
| taker | 0,06%/lado | `kucoin.TAKER_FEE` (env), lido da conta só em research (`fetch_actual_taker_fee`) | KNOWN (config); taxa real da conta não usada no LIVE |
| maker | 0,02% | `kucoin.MAKER_FEE` | não aplicável (entradas e saídas a mercado) |
| slippage (gate final) | 0,05%/lado majors, 0,10%/lado alts | `kucoin_execution_model.slippage_rate_for_symbol` | ASSUMED |
| slippage (gate V3) | 0,10% total (uma vez) | `NEXUS_EXPECTED_SLIPPAGE_PCT` | ASSUMED |
| round trip (gate final) | **0,22% majors / 0,32% alts** | 2·taker + 2·slip | KNOWN fee + ASSUMED slip |
| round trip (gate V3) | 0,22% para todos | 2·taker + 0,10% | inconsistente com o anterior em alts |
| funding | não modelado no sizing | `funding_return_fraction` só em research | não inventado aqui |
| liquidation fee | 0,06% (no cálculo de liq) | `liquidation.LIQUIDATION_FEE` | ASSUMED |

## 12. SYMBOL / CONTRACT CONSTRAINTS

| symbol | multiplier | ref price | 1-contract notional | RT cost | 1c loss @0.5% | @1% | @2% | min equity 0.5% risk @1% stop | min equity 1% risk @1% stop |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 0.001 | 60,000.00 | 60.00 | 0.22% | 0.4320 | 0.7320 | 1.3320 | 146.40 | 73.20 |
| ETHUSDT | 0.01 | 3,000.00 | 30.00 | 0.22% | 0.2160 | 0.3660 | 0.6660 | 73.20 | 36.60 |
| SOLUSDT | 0.1 | 150.00 | 15.00 | 0.22% | 0.1080 | 0.1830 | 0.3330 | 36.60 | 18.30 |
| AVAXUSDT | 0.1 | 30.00 | 3.00 | 0.32% | 0.0246 | 0.0396 | 0.0696 | 7.92 | 3.96 |
| XRPUSDT | 10.0 | 0.60 | 6.00 | 0.32% | 0.0492 | 0.0792 | 0.1392 | 15.84 | 7.92 |
| DOGEUSDT | 100.0 | 0.15 | 15.00 | 0.32% | 0.1230 | 0.1980 | 0.3480 | 39.60 | 19.80 |

## 13. SMALL-EQUITY FEASIBILITY (contratos a stop 0,5% / 1% / 2%; × = rejeitado; L = 10)

### CURRENT — contracts at stop 0.5% / 1% / 2% (× = rejected), L=10
| symbol | 5 | 10 | 25 | 50 | 100 | 500 | 1000 |
|---|---|---|---|---|---|---|---|
| BTCUSDT | ×/×/× | ×/×/× | ×/×/× | ×/×/× | 8/8/× | 41/41/41 | 83/83/83 |
| ETHUSDT | ×/×/× | ×/×/× | ×/×/× | 8/8/× | 16/16/16 | 83/83/83 | 166/166/166 |
| SOLUSDT | ×/×/× | ×/×/× | 8/8/× | 16/16/16 | 33/33/33 | 166/166/166 | 333/333/333 |
| AVAXUSDT | 8/8/× | 16/16/16 | 41/41/41 | 83/83/83 | 166/166/166 | 833/833/833 | 1666/1666/1666 |
| XRPUSDT | ×/×/× | 8/8/× | 20/20/20 | 41/41/41 | 83/83/83 | 416/416/416 | 833/833/833 |
| DOGEUSDT | ×/×/× | ×/×/× | 8/8/× | 16/16/16 | 33/33/33 | 166/166/166 | 333/333/333 |

### RISK 0.5% — contracts at stop 0.5% / 1% / 2% (× = rejected), L=10
| symbol | 5 | 10 | 25 | 50 | 100 | 500 | 1000 |
|---|---|---|---|---|---|---|---|
| BTCUSDT | ×/×/× | ×/×/× | ×/×/× | ×/×/× | 1/×/× | 5/3/1 | 11/6/3 |
| ETHUSDT | ×/×/× | ×/×/× | ×/×/× | 1/×/× | 2/1/× | 11/6/3 | 23/13/7 |
| SOLUSDT | ×/×/× | ×/×/× | 1/×/× | 2/1/× | 4/2/1 | 23/13/7 | 46/27/15 |
| AVAXUSDT | 1/×/× | 2/1/× | 5/3/1 | 10/6/3 | 20/12/7 | 101/63/35 | 203/126/71 |
| XRPUSDT | ×/×/× | 1/×/× | 2/1/× | 5/3/1 | 10/6/3 | 50/31/17 | 101/63/35 |
| DOGEUSDT | ×/×/× | ×/×/× | 1/×/× | 2/1/× | 4/2/1 | 20/12/7 | 40/25/14 |

### RISK 1% — contracts at stop 0.5% / 1% / 2% (× = rejected), L=10
| symbol | 5 | 10 | 25 | 50 | 100 | 500 | 1000 |
|---|---|---|---|---|---|---|---|
| BTCUSDT | ×/×/× | ×/×/× | ×/×/× | 1/×/× | 2/1/× | 11/6/3 | 23/13/7 |
| ETHUSDT | ×/×/× | ×/×/× | 1/×/× | 2/1/× | 4/2/1 | 23/13/7 | 46/27/15 |
| SOLUSDT | ×/×/× | ×/×/× | 2/1/× | 4/2/1 | 9/5/3 | 46/27/15 | 92/54/30 |
| AVAXUSDT | 2/1/× | 4/2/1 | 10/6/3 | 20/12/7 | 40/25/14 | 203/126/71 | 406/252/143 |
| XRPUSDT | 1/×/× | 2/1/× | 5/3/1 | 10/6/3 | 20/12/7 | 101/63/35 | 203/126/71 |
| DOGEUSDT | ×/×/× | ×/×/× | 2/1/× | 4/2/1 | 8/5/2 | 40/25/14 | 81/50/28 |

### RISK 2% — contracts at stop 0.5% / 1% / 2% (× = rejected), L=10
| symbol | 5 | 10 | 25 | 50 | 100 | 500 | 1000 |
|---|---|---|---|---|---|---|---|
| BTCUSDT | ×/×/× | ×/×/× | 1/×/× | 2/1/× | 4/2/1 | 23/13/7 | 46/27/15 |
| ETHUSDT | ×/×/× | ×/×/× | 2/1/× | 4/2/1 | 9/5/3 | 46/27/15 | 92/54/30 |
| SOLUSDT | ×/×/× | 1/1/× | 4/2/1 | 9/5/3 | 18/10/6 | 92/54/30 | 185/109/60 |
| AVAXUSDT | 4/2/1 | 8/5/2 | 20/12/7 | 40/25/14 | 81/50/28 | 406/252/143 | 813/505/287 |
| XRPUSDT | 2/1/× | 4/2/1 | 10/6/3 | 20/12/7 | 40/25/14 | 203/126/71 | 406/252/143 |
| DOGEUSDT | ×/×/× | 1/1/× | 4/2/1 | 8/5/2 | 16/10/5 | 81/50/28 | 162/101/57 |

Frequência de rejeição na grade sintética (6 símbolos × 8 stops 0,25–5%):

| equity | política | rejeitado | por contrato mínimo | maior perda aceita (stops até 5%) |
|---|---|---|---|---|
| ≤ 50 | CURRENT | 64,1% | 62,5% | 16,5% |
| ≤ 50 | 0,5% | 74,5% | 74,5% | 0,49% |
| ≤ 50 | 1% | 58,3% | 58,3% | 1,00% |
| ≤ 50 | 2% | 39,6% | 39,6% | 2,00% |
| 100 | CURRENT | 18,8% | 10,4% | 16,5% |
| 100 | 0,5% | 25,0% | 25,0% | 0,50% |
| 100 | 1% | 10,4% | 10,4% | 1,00% |
| 100 | 2% | 2,1% | 2,1% | 2,00% |
| ≥ 500 | CURRENT | 12,5% | 0% | 16,6% |
| ≥ 500 | 0,5% | 1,0% | 1,0% | 0,50% |
| ≥ 500 | 1% | 0% | 0% | 1,00% |

(Grade com stop máximo de 3% aceito; no limite de 4,78% a política atual chega a 25%.)

## 14. RISK-BASED FORMULA (candidata, não implementada)

```
risk_budget       = equity × risk_pct
loss_per_contract = multiplier × (|entry − stop_exchange| + entry × cost_fraction(symbol))
contracts_risk    = floor(risk_budget / loss_per_contract / lot) × lot
contracts_margin  = floor(available × margin_cap × L / (entry × multiplier) / lot) × lot
contracts         = min(contracts_risk, contracts_margin)          # margin só reduz
contracts < minQty                       → NO TRADE (nunca "usar 1 contrato mesmo assim")
liq_move(L) − gap < stop_distance        → NO TRADE (não comprimir o stop para caber)
```

- **INV-RISK-SIZING-001:** `qty·(|entry−stop| + entry·c) ≤ equity·risk_pct`.
- **INV-RISK-ROUNDING-001:** a invariante vale **depois** da quantização (sempre floor; se 1 lote excede, rejeita). Verificado em `test_rounding_and_min_contract_invariants`.
- **INV-RISK-LEVERAGE-001:** com E, s e risk_pct fixos, L não altera a perda enquanto houver margem. Verificado em `test_leverage_does_not_change_risk_while_margin_suffices`.

## 15. ROUNDING / MARGIN / LEVERAGE INVARIANTS — evidência

- **Rounding:** o floor garante perda ≤ budget em todos os vetores. Exemplo: SOL 1%, stop 1%, raw 5,464 → 5 contratos, perda 0,915 ≤ 1,00.
- **Margin:** `test_margin_clamp_only_reduces`. BTC, budget 2%, stop 0,25%, L 5: available 100 → binding MARGIN, menos contratos e menos perda do que com available 1000.
- **Leverage:** SOL stop 0,5%: 9 contratos e 0,97 USDT em 5x/10x/20x/50x; a margem cai de 27,00 para 2,70.

## 16. LIQUIDATION INTERACTION

Fórmula do projeto: `liquidation.liquidation_price`, `liq = entry·(1 − 1/L)/(1 − mmr − liq_fee)`, com MMR 0,4% de
fallback e gap mínimo de 0,30 pp.

| L | distância até liquidação | stop máx. pela liquidação | stop máx. pelo loss budget (majors) | binding |
|---|---|---|---|---|
| 5x | 19,63% | 19,33% | 9,78% | loss budget |
| 10x | 9,58% | 9,28% | 4,78% | loss budget |
| 20x | 4,56% | 4,26% | 2,28% | loss budget |
| 50x | 1,55% | 1,25% | 0,78% | loss budget |

- Hoje o `final_loss_budget` é sempre mais restritivo que a liquidação. Logo, a regra "liq buffer > stop + safety" está satisfeita.
- **Atenção 1:** a fórmula é do tipo *isolated* (IM = 1/L). A conta opera em **CROSS** (`kucoin_cross_margin_order`). Em cross a liquidação depende da equity total e das outras posições, e o `analyze` é chamado com `n_open_positions=1`. A medida é aproximada.
- **Atenção 2:** `_geometry_from_exact_mmr` **comprime o stop técnico** quando ele não cabe. Ou seja, a alavancagem altera a geometria do stop. Numa política risk-based isso deveria ser NO TRADE ou redução de L, nunca mover o stop técnico.

## 17. F-013 DEPENDENCY

- No dispatch, o tamanho e o `final_loss_budget` usam `sig.sl`. É exatamente o stop enviado como SL nativo (`place_order(sl=sig.sl)`) e o mesmo verificado pelo readback do `native_stop_repair`. **A invariante pré-dispatch pode ser implementada sem F-013.**
- Depois do fill, `engine.py:3476` desloca `sig.sl` pelo slippage (F-013). O SL nativo fica no nível original, mas `initial_sl`, o 1R e o `_guard_naked_positions` passam a usar o stop deslocado. Num LONG com fill abaixo do sinal, o stop local fica **mais largo** que o nativo; se reaplicado, a perda real excede o orçamento calculado no dispatch.
- **Depende de F-013:** a verificação pós-fill (perda real = qty·|fill − stop_nativo|), a reaplicação de stop, o R-múltiplo/accounting por trade em unidades de risco, e o fallback de ticker sem timestamp como preço de fill.

## 18. CURRENT vs 0,5% vs 1% vs 2% (equity = available = 100, L = 10)

| propriedade | A CURRENT | B 0,5% | C 1,0% | D 2,0% | E risk-based + regime/correlation cap (futuro) |
|---|---|---|---|---|---|
| perda projetada no stop | 3,6% (s 0,5%) … 25% (s 4,78%) | ≤ 0,5% | ≤ 1,0% | ≤ 2,0% | ≤ r × fator de regime |
| perda simultânea máx. (2 posições) | 37,5% | ≤ 1,0% | ≤ 2,0% | ≤ 4,0% | ≤ cap agregado definido |
| margem exigida (SOL, s 0,5%) | 49,5 | 6,0 | 13,5 | 27 | idem C/D |
| sensibilidade ao stop | perda ∝ s (tamanho fixo) | tamanho ∝ 1/s; perda constante | idem | idem | idem |
| sensibilidade à alavancagem | perda ∝ L | nenhuma (margem ∝ 1/L) | nenhuma | nenhuma | nenhuma |
| executável com equity pequena (≤ 50) | 36% dos casos (com perdas de até 16,5%) | 25,5% | 41,7% | 60,4% | ≤ D |
| rejeição por contrato mínimo @100 | 10,4% (via gate V3) | 25,0% | 10,4% | 2,1% | ≥ D |
| compatibilidade com metadata atual | sim | sim (rejeita BTC/ETH < ~150/75 USDT) | sim (BTC exige ~73 USDT para stop 1%) | sim | sim |
| perda diária de pior caso vs stop diário 3% | 12,5× | 0,33× | 0,67× | 1,33× | configurável |

## 19. CONSECUTIVE LOSS TABLE (equity remanescente, composta)

| risk/trade | 1 | 3 | 5 | 10 | 15 |
|---|---|---|---|---|---|
| 0.5% | 99.5% | 98.5% | 97.5% | 95.1% | 92.8% |
| 1.0% | 99.0% | 97.0% | 95.1% | 90.4% | 86.0% |
| 2.0% | 98.0% | 94.1% | 90.4% | 81.7% | 73.9% |
| 5.0% | 95.0% | 85.7% | 77.4% | 59.9% | 46.3% |
| 10.0% | 90.0% | 72.9% | 59.0% | 34.9% | 20.6% |
| 25.0% | 75.0% | 42.2% | 23.7% | 5.6% | 1.3% |

Com 25% por trade (o teto atual), 3 stops seguidos deixam 42% da conta; 5 stops deixam 24%.

## 20. HYPOTHETICAL MONTE CARLO

> **HIPOTÉTICO. Não é evidência de performance do NEXUS.** Win rates e payoffs são cenários arbitrários. Não existe
> win rate empírico confiável do NEXUS LIVE. 200 trades por path, 5 000 paths por célula, fração fixa da equity,
> seed 20261002. Custos já embutidos em R.

| win% | payoff | risk/trade | median maxDD | p95 maxDD | P(DD≥20%) | P(DD≥50%) | P(DD≥90% ruin-like) | expectancy/trade |
|---|---|---|---|---|---|---|---|---|
| 35% | 1R | 0.25% | 14.4% | 18.8% | 1.8% | 0.0% | 0.0% | -0.30R |
| 35% | 1R | 0.50% | 26.8% | 34.1% | 91.5% | 0.0% | 0.0% | -0.30R |
| 35% | 1R | 1.00% | 47.2% | 56.8% | 100.0% | 31.1% | 0.0% | -0.30R |
| 35% | 1R | 2.00% | 72.7% | 82.1% | 100.0% | 99.4% | 0.0% | -0.30R |
| 35% | 1R | 5.00% | 96.5% | 98.8% | 100.0% | 100.0% | 94.8% | -0.30R |
| 35% | 1R | 10.00% | 99.9% | 100.0% | 100.0% | 100.0% | 100.0% | -0.30R |
| 35% | 1R | 25.00% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | -0.30R |
| 35% | 1.5R | 0.25% | 7.9% | 13.5% | 0.0% | 0.0% | 0.0% | -0.13R |
| 35% | 1.5R | 0.50% | 15.7% | 25.2% | 23.1% | 0.0% | 0.0% | -0.13R |
| 35% | 1.5R | 1.00% | 29.3% | 43.9% | 83.9% | 1.0% | 0.0% | -0.13R |
| 35% | 1.5R | 2.00% | 51.1% | 69.8% | 99.4% | 53.5% | 0.0% | -0.13R |
| 35% | 1.5R | 5.00% | 85.6% | 95.7% | 100.0% | 98.7% | 30.5% | -0.13R |
| 35% | 1.5R | 10.00% | 98.8% | 99.9% | 100.0% | 100.0% | 94.7% | -0.13R |
| 35% | 1.5R | 25.00% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | -0.13R |
| 35% | 2R | 0.25% | 4.4% | 8.9% | 0.0% | 0.0% | 0.0% | +0.05R |
| 35% | 2R | 0.50% | 8.8% | 17.1% | 1.7% | 0.0% | 0.0% | +0.05R |
| 35% | 2R | 1.00% | 16.9% | 31.0% | 34.3% | 0.0% | 0.0% | +0.05R |
| 35% | 2R | 2.00% | 32.1% | 53.5% | 92.9% | 8.2% | 0.0% | +0.05R |
| 35% | 2R | 5.00% | 65.9% | 88.2% | 100.0% | 84.1% | 3.2% | +0.05R |
| 35% | 2R | 10.00% | 92.1% | 99.3% | 100.0% | 100.0% | 58.2% | +0.05R |
| 35% | 2R | 25.00% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | +0.05R |
| 45% | 1R | 0.25% | 6.6% | 10.9% | 0.0% | 0.0% | 0.0% | -0.10R |
| 45% | 1R | 0.50% | 12.8% | 21.5% | 7.8% | 0.0% | 0.0% | -0.10R |
| 45% | 1R | 1.00% | 24.2% | 38.1% | 68.8% | 0.0% | 0.0% | -0.10R |
| 45% | 1R | 2.00% | 42.9% | 62.5% | 97.7% | 30.3% | 0.0% | -0.10R |
| 45% | 1R | 5.00% | 78.9% | 92.6% | 100.0% | 96.0% | 11.4% | -0.10R |
| 45% | 1R | 10.00% | 97.0% | 99.7% | 100.0% | 100.0% | 83.5% | -0.10R |
| 45% | 1R | 25.00% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | -0.10R |
| 45% | 1.5R | 0.25% | 3.0% | 5.6% | 0.0% | 0.0% | 0.0% | +0.12R |
| 45% | 1.5R | 0.50% | 5.9% | 11.2% | 0.0% | 0.0% | 0.0% | +0.12R |
| 45% | 1.5R | 1.00% | 11.5% | 20.9% | 6.5% | 0.0% | 0.0% | +0.12R |
| 45% | 1.5R | 2.00% | 22.6% | 39.1% | 65.2% | 0.5% | 0.0% | +0.12R |
| 45% | 1.5R | 5.00% | 49.0% | 73.6% | 100.0% | 46.9% | 0.0% | +0.12R |
| 45% | 1.5R | 10.00% | 78.2% | 95.1% | 100.0% | 99.0% | 16.9% | +0.12R |
| 45% | 1.5R | 25.00% | 99.8% | 100.0% | 100.0% | 100.0% | 98.9% | +0.12R |
| 45% | 2R | 0.25% | 2.2% | 4.2% | 0.0% | 0.0% | 0.0% | +0.35R |
| 45% | 2R | 0.50% | 4.9% | 8.2% | 0.0% | 0.0% | 0.0% | +0.35R |
| 45% | 2R | 1.00% | 8.9% | 15.1% | 0.9% | 0.0% | 0.0% | +0.35R |
| 45% | 2R | 2.00% | 17.5% | 29.1% | 36.1% | 0.0% | 0.0% | +0.35R |
| 45% | 2R | 5.00% | 40.1% | 59.4% | 99.8% | 18.1% | 0.0% | +0.35R |
| 45% | 2R | 10.00% | 67.1% | 87.3% | 100.0% | 93.9% | 2.6% | +0.35R |
| 45% | 2R | 25.00% | 97.8% | 99.9% | 100.0% | 100.0% | 92.8% | +0.35R |
| 50% | 1R | 0.25% | 3.7% | 7.5% | 0.0% | 0.0% | 0.0% | +0.00R |
| 50% | 1R | 0.50% | 7.3% | 14.5% | 0.3% | 0.0% | 0.0% | +0.00R |
| 50% | 1R | 1.00% | 14.4% | 27.1% | 21.8% | 0.0% | 0.0% | +0.00R |
| 50% | 1R | 2.00% | 27.9% | 47.3% | 79.9% | 3.1% | 0.0% | +0.00R |
| 50% | 1R | 5.00% | 58.4% | 81.8% | 100.0% | 69.3% | 0.4% | +0.00R |
| 50% | 1R | 10.00% | 87.2% | 97.8% | 100.0% | 99.4% | 39.9% | +0.00R |
| 50% | 1R | 25.00% | 100.0% | 100.0% | 100.0% | 100.0% | 99.6% | +0.00R |
| 50% | 1.5R | 0.25% | 2.2% | 3.8% | 0.0% | 0.0% | 0.0% | +0.25R |
| 50% | 1.5R | 0.50% | 4.4% | 7.7% | 0.0% | 0.0% | 0.0% | +0.25R |
| 50% | 1.5R | 1.00% | 8.7% | 14.9% | 0.6% | 0.0% | 0.0% | +0.25R |
| 50% | 1.5R | 2.00% | 16.9% | 28.5% | 29.6% | 0.0% | 0.0% | +0.25R |
| 50% | 1.5R | 5.00% | 38.4% | 58.5% | 99.7% | 15.6% | 0.0% | +0.25R |
| 50% | 1.5R | 10.00% | 65.2% | 86.1% | 100.0% | 92.0% | 1.7% | +0.25R |
| 50% | 1.5R | 25.00% | 97.3% | 99.9% | 100.0% | 100.0% | 87.6% | +0.25R |
| 50% | 2R | 0.25% | 2.0% | 3.2% | 0.0% | 0.0% | 0.0% | +0.50R |
| 50% | 2R | 0.50% | 3.9% | 6.3% | 0.0% | 0.0% | 0.0% | +0.50R |
| 50% | 2R | 1.00% | 7.7% | 12.3% | 0.1% | 0.0% | 0.0% | +0.50R |
| 50% | 2R | 2.00% | 14.9% | 23.2% | 13.4% | 0.0% | 0.0% | +0.50R |
| 50% | 2R | 5.00% | 33.7% | 49.7% | 98.8% | 4.7% | 0.0% | +0.50R |
| 50% | 2R | 10.00% | 58.2% | 77.3% | 100.0% | 76.7% | 0.2% | +0.50R |
| 50% | 2R | 25.00% | 93.7% | 99.3% | 100.0% | 100.0% | 69.5% | +0.50R |
| 55% | 1R | 0.25% | 2.5% | 4.7% | 0.0% | 0.0% | 0.0% | +0.10R |
| 55% | 1R | 0.50% | 4.9% | 9.2% | 0.0% | 0.0% | 0.0% | +0.10R |
| 55% | 1R | 1.00% | 9.6% | 17.6% | 2.7% | 0.0% | 0.0% | +0.10R |
| 55% | 1R | 2.00% | 18.6% | 33.2% | 42.0% | 0.1% | 0.0% | +0.10R |
| 55% | 1R | 5.00% | 41.9% | 65.4% | 99.5% | 27.0% | 0.0% | +0.10R |
| 55% | 1R | 10.00% | 70.2% | 91.4% | 100.0% | 92.5% | 7.0% | +0.10R |
| 55% | 1R | 25.00% | 98.8% | 100.0% | 100.0% | 100.0% | 93.3% | +0.10R |
| 55% | 1.5R | 0.25% | 1.7% | 3.0% | 0.0% | 0.0% | 0.0% | +0.38R |
| 55% | 1.5R | 0.50% | 3.5% | 5.9% | 0.0% | 0.0% | 0.0% | +0.38R |
| 55% | 1.5R | 1.00% | 6.8% | 11.5% | 0.0% | 0.0% | 0.0% | +0.38R |
| 55% | 1.5R | 2.00% | 13.4% | 21.8% | 8.8% | 0.0% | 0.0% | +0.38R |
| 55% | 1.5R | 5.00% | 32.1% | 47.6% | 97.7% | 3.0% | 0.0% | +0.38R |
| 55% | 1.5R | 10.00% | 55.4% | 75.5% | 100.0% | 70.3% | 0.1% | +0.38R |
| 55% | 1.5R | 25.00% | 92.1% | 99.1% | 100.0% | 100.0% | 60.0% | +0.38R |
| 55% | 2R | 0.25% | 1.5% | 2.5% | 0.0% | 0.0% | 0.0% | +0.65R |
| 55% | 2R | 0.50% | 3.0% | 4.9% | 0.0% | 0.0% | 0.0% | +0.65R |
| 55% | 2R | 1.00% | 5.9% | 9.7% | 0.0% | 0.0% | 0.0% | +0.65R |
| 55% | 2R | 2.00% | 11.6% | 18.6% | 3.6% | 0.0% | 0.0% | +0.65R |
| 55% | 2R | 5.00% | 27.6% | 42.7% | 92.3% | 0.5% | 0.0% | +0.65R |
| 55% | 2R | 10.00% | 51.2% | 68.6% | 100.0% | 51.3% | 0.0% | +0.65R |
| 55% | 2R | 25.00% | 88.0% | 97.1% | 100.0% | 100.0% | 37.3% | +0.65R |

Leitura estrutural, independente de edge: com 25% por trade, P(DD ≥ 90%) fica entre 37% e 100% **mesmo com
expectativa positiva** (55%/2R → 37%). Com 0,5–1% por trade, P(DD ≥ 50%) é 0% em todos os cenários de expectativa ≥ 0.

Kelly (só referência teórica, sem input confiável): f* = p − (1−p)/b. Para 45%/2R → 17,5%; para 50%/1R → 0. Não usar
como política: depende de p e b, que são desconhecidos.

## 21. 20+ TEST VECTORS (dry-run, LONG; CURRENT = política atual)

| symbol | equity | entry | stop (LONG) | lev | risk_pct | raw contracts | contracts | notional | margin | projected loss | loss %eq | decision |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 100 | 60000.0 | 59700 | 10x | CURRENT | 8 | 8 | 480.00 | 48.00 | 3.4560 | 3.46% | ACCEPT |
| BTCUSDT | 100 | 60000.0 | 59400 | 10x | CURRENT | 8 | 8 | 480.00 | 48.00 | 5.8560 | 5.86% | ACCEPT |
| BTCUSDT | 100 | 60000.0 | 58800 | 10x | CURRENT | 8 | 0 | 0.00 | 0.00 | 0.0000 | 0.00% | REJECT (v3_gate(min_lot>risk_pct_or_margin_cap)) |
| BTCUSDT | 100 | 60000.0 | 59400 | 10x | 1.00% | 1.366 | 1 | 60.00 | 6.00 | 0.7320 | 0.73% | ACCEPT |
| BTCUSDT | 1000 | 60000.0 | 59400 | 10x | 0.50% | 6.831 | 6 | 360.00 | 36.00 | 4.3920 | 0.44% | ACCEPT |
| ETHUSDT | 100 | 3000.0 | 2970 | 10x | CURRENT | 16 | 16 | 480.00 | 48.00 | 5.8560 | 5.86% | ACCEPT |
| ETHUSDT | 100 | 3000.0 | 2970 | 10x | 0.50% | 1.366 | 1 | 30.00 | 3.00 | 0.3660 | 0.37% | ACCEPT |
| ETHUSDT | 25 | 3000.0 | 2970 | 10x | 1.00% | 0.683 | 0 | 0.00 | 0.00 | 0.0000 | 0.00% | REJECT (one_contract_exceeds_budget) |
| SOLUSDT | 100 | 150.0 | 149.25 | 10x | CURRENT | 33 | 33 | 495.00 | 49.50 | 3.5640 | 3.56% | ACCEPT |
| SOLUSDT | 100 | 150.0 | 144 | 10x | CURRENT | 33 | 33 | 495.00 | 49.50 | 20.8890 | 20.89% | ACCEPT |
| SOLUSDT | 100 | 150.0 | 148.5 | 10x | 1.00% | 5.464 | 5 | 75.00 | 7.50 | 0.9150 | 0.92% | ACCEPT |
| SOLUSDT | 10 | 150.0 | 148.5 | 10x | 0.50% | 0.273 | 0 | 0.00 | 0.00 | 0.0000 | 0.00% | REJECT (one_contract_exceeds_budget) |
| SOLUSDT | 100 | 150.0 | 149.25 | 50x | CURRENT | 166 | 166 | 2,490.00 | 49.80 | 17.9280 | 17.93% | ACCEPT |
| SOLUSDT | 100 | 150.0 | 149.25 | 50x | 1.00% | 9.259 | 9 | 135.00 | 2.70 | 0.9720 | 0.97% | ACCEPT |
| AVAXUSDT | 100 | 30.0 | 29.4 | 10x | CURRENT | 166 | 166 | 498.00 | 49.80 | 11.5536 | 11.55% | ACCEPT |
| AVAXUSDT | 100 | 30.0 | 29.4 | 10x | 0.50% | 7.184 | 7 | 21.00 | 2.10 | 0.4872 | 0.49% | ACCEPT |
| XRPUSDT | 100 | 0.6 | 0.594 | 10x | CURRENT | 83 | 83 | 498.00 | 49.80 | 6.5736 | 6.57% | ACCEPT |
| XRPUSDT | 5 | 0.6 | 0.594 | 10x | 1.00% | 0.631 | 0 | 0.00 | 0.00 | 0.0000 | 0.00% | REJECT (one_contract_exceeds_budget) |
| DOGEUSDT | 100 | 0.15 | 0.1455 | 10x | CURRENT | 33 | 33 | 495.00 | 49.50 | 16.4340 | 16.43% | ACCEPT |
| DOGEUSDT | 100 | 0.15 | 0.1455 | 10x | 2.00% | 4.016 | 4 | 60.00 | 6.00 | 1.9920 | 1.99% | ACCEPT |
| DOGEUSDT | 50 | 0.15 | 0.1485 | 20x | 0.50% | 1.263 | 1 | 15.00 | 0.75 | 0.1980 | 0.40% | ACCEPT |
| BTCUSDT | 500 | 60000.0 | 59850 | 10x | 0.25% | 4.433 | 4 | 240.00 | 24.00 | 1.1280 | 0.23% | ACCEPT |

## 22. RISK DOCTRINES

| módulo | fórmula | significado pretendido | ativo? | modifica qty final? | margin | notional | stop |
|---|---|---|---|---|---|---|---|
| `final_sizing_invariants` | 0,5·A·L / P | "50% do available como margem" | **SIM (autoridade)** | **SIM** | ✔ | | |
| `final_loss_budget` | loss ≤ 0,5·margin | teto de perda = metade da margem | SIM (gate) | só bloqueia | ✔ | | ✔ |
| `RiskManagerV3` / `professional_risk.stop_risk_size` | min(E·r/(s+c), 10%·A·L)/P | sizing por stop | SIM, mas **só como gate binário** | só bloqueia (qty>0) | ✔ (cap 10%) | | ✔ |
| `operator_runtime_policy._margin_target_quantity` | 0,5·A·L | idem final | sombreado | não | ✔ | | |
| `pilot_risk_cap_hardening` | min(risk_qty, target) | risco como teto | sombreado (docstring diz "50% notional") | não | | ✔ | ✔ |
| `pilot_live_runtime` | 0,5·A | "50% notional" (docstring + log `[PILOT_LEGACY_TARGET]`) | só log/contexto | não | | ✔ | |
| `risk.RiskManager.size` (legacy) | E·L·MAX_RISK_PCT | MAX_RISK_PCT como fração de notional | inativo (o adapter sobrescreve `size`) | não | ✔ | ✔ | |
| `kucoin_contract_risk_hardening` | comprime stop até liq−gap | segurança de liquidação | SIM | não (muda o **stop**) | | | ✔ |
| `operator_loss_policy.stop_price` | 0,5/L − cost | stop derivado da alavancagem | legado, não usado | não | | | ✔ |

Conflitos: (1) três significados para "50%" (margem, notional, metade da margem como perda); (2) MAX_RISK_PCT tem três
semânticas (gate de stop no V3, fração de notional no legacy, "risco por trade" no log); (3) dois modelos de custo
(0,22/0,32% vs 0,22%); (4) a docstring do `pilot_risk_cap` ("final_qty = min(...)") descreve comportamento que não
está ativo.

## 23. RECOMMENDED POLICY OPTIONS (sem patch)

Escolha por objetivo explícito:

- **Objetivo "limitar a perda por trade a X% da equity":** só B/C/D satisfazem a propriedade, por construção e após arredondamento. A política atual não satisfaz para nenhum X < 25%.
- **Objetivo "uma perda de stop nunca dispara sozinha o stop diário de 3%":** satisfeito por r ≤ 3%; com 2 posições simultâneas, por r ≤ 1,5%. B e C satisfazem; D satisfaz por trade mas não no agregado (2×2% = 4% > 3%).
- **Objetivo "operar com equity ≈ 100 USDT na maior parte dos símbolos":** D rejeita 2,1%, C 10,4%, B 25% da grade. BTC só é operável com C/D e stops ≤ 1%.
- **Objetivo "o drawdown máximo de 10% não é atingido por menos de N stops consecutivos":** r = 1% → N = 11; 2% → N = 6; 0,5% → N = 22; atual → N = 1.

Opção técnica coerente com os limites já configurados (stop diário 3%, DD 10%, 2 posições): **C (1%) com cap
agregado de risco aberto ≤ 2% da equity**, ou B (0,5%) se o objetivo for tolerar ≥ 20 stops até o DD de 10%. A escolha
é do operador.

## 24. PROPOSED IMPLEMENTATION ARCHITECTURE

```
SETUP → entry → technical stop (estratégia; nunca comprimido)
  → risk_budget = equity × risk_pct (fonte única: RiskManagerV3)
  → qty_risk = budget / (|entry − stop| + entry × cost(symbol))
  → contracts = floor(qty_risk / mult / lot) × lot;  < minQty → NO TRADE
  → margin feasibility: min(contracts, floor(available × cap × L / (P × mult)))   (só reduz)
  → liquidation safety: liq_move(L, mmr_api) − gap ≥ stop  senão NO TRADE
  → open-risk cap: Σ risco aberto + este ≤ cap agregado
  → FINAL INVARIANT (abaixo) → dispatch
```

Remover ou neutralizar: `final_sizing_invariants` como autoridade, o hook `pilot_live_runtime` de 50% notional, o
`min()` sombreado do `pilot_risk_cap` e a compressão de stop por liquidação. Unificar o modelo de custo.

## 25. PROPOSED FINAL PRE-DISPATCH INVARIANT

`assert_projected_loss_within_budget(contracts, multiplier, entry, stop, cost_fraction, equity, risk_pct)` (protótipo
em `research/risk_policy/f003_sizing_audit.py`):

- usa **contratos finais**, multiplicador da exchange, **o stop que será enviado como SL nativo**, preço executável fresco e custo do símbolo;
- falha fechado se qualquer valor for não finito, se risk_pct estiver fora de (0, 5%] ou se `projected > budget·(1+1e-9)`;
- posição: imediatamente antes de `place_order` no `_open`, no mesmo ponto do `FRESH_PREDISPATCH_RECHECK` atual (substituindo `limit = 0,5·margin` por `equity·risk_pct`).

Mesmo que o upstream erre, nenhuma ordem sai acima do orçamento.

## 26. NEW FINDINGS

1. **F-003a — MAX_RISK_PCT funciona como gate invertido.** Ele bloqueia trades cujo **contrato mínimo** excede 1% da equity, mas permite que o trade aceito tenha `N_target` contratos (8× em BTC@100, 33× em SOL@100). O efeito acoplado é perverso: em contratos grossos (BTC/ETH) stops largos são bloqueados, enquanto em contratos finos a perda pode chegar a 25%.
2. **F-003b — o gate V3 limita a margem a 10% do available (`MAX_MARGIN_PCT`) e a autoridade final usa 50%.** Mais uma semântica concorrente para a mesma grandeza.
3. **F-003c — dois modelos de custo.** O gate V3 usa 0,22% para todos os símbolos; o gate final e a geometria usam 0,32% em alts.
4. **F-003d — o limite de perda está em unidades de margem (0,5·margin), não de equity.** Ele escala com o available e com a alavancagem escolhida, não com uma tolerância de risco.
5. **F-003e — a alavancagem altera a geometria do stop** (`kucoin_contract_risk_hardening` comprime até 60% do stop técnico). Isso contradiz "leverage é ferramenta de margem".
6. **F-003f — liquidação calculada com fórmula isolated numa conta cross,** com `n_open_positions=1` fixo.
7. **F-003g — `consecutive_losses` e o modo conservador não reduzem o tamanho final** (o primeiro só notifica; o segundo só afeta o gate).
8. **F-003h — docstrings divergentes** (`pilot_risk_cap_hardening`, `pilot_live_runtime`) descrevem "50% notional" e `min(risk,target)`, que não estão ativos. Isso induz erro de auditoria.

## 27. DECISIONS REQUIRED FROM OPERATOR

1. Objetivo de risco: perda máxima por trade (sugestões quantificadas: 0,5% / 1% / 2%) e cap de risco aberto agregado.
2. Valores reais no Railway de `LEVERAGE`, `MAX_RISK_PCT`, `MAX_MARGIN_PCT`, `TAKER_FEE`, `BACKTEST_SLIPPAGE`, `NEXUS_EXPECTED_SLIPPAGE_PCT` e `DAILY_STOP_LOSS_PCT`, e a equity atual. Os números deste relatório usam os defaults do código.
3. Se aceita **NO TRADE** em BTC/ETH quando 1 contrato excede o budget (com equity ~100 isso exclui BTC com stop > ~1,45% em C).
4. Se a compressão de stop por liquidação deve virar NO TRADE ou redução de alavancagem.
5. Ordem: F-013 antes ou depois do patch F-003. A invariante pré-dispatch é independente; a verificação pós-fill depende de F-013.

## 28. F-003 DECISION QUESTIONS — respostas

1. **O que significa hoje o "50%"?** 50% do availableMargin usado como **margem inicial**, logo notional = 0,5·A·L (5× o available a 10x). Outros módulos ainda descrevem "50% notional", mas estão inativos.
2. **Qual módulo vence?** `final_sizing_invariants._operator_target_quantity`, o wrapper mais externo do hook `minimum_base_quantity`.
3. **O leverage altera o risk-at-stop?** Sim, linearmente (perda = 0,5·A·L·(s+c)). Ele também limita o stop admissível (s ≤ 0,5/L − c) e pode comprimir o stop técnico.
4. **Perda máxima por trade hoje?** 0,25 × available, cerca de **25 USDT / 25% da equity** em conta flat de 100 USDT (custos modelados incluídos), em qualquer alavancagem; em contratos grossos o gate V3 reduz o máximo efetivo.
5. **Perda agregada máxima?** 2 posições: **37,5%** (25% + 12,5%); 3 posições (hipotético): 43,75%.
6. **MAX_RISK_PCT limita alguma coisa?** Só a **admissão**: o contrato mínimo precisa caber em 1% da equity e 10% da margem. Não limita o tamanho.
7. **Fórmula única se migrarmos?** `contracts = min(floor(E·r / (mult·(|entry−stop_nativo| + entry·c_symbol))), floor(A·cap·L/(P·mult)))`, rejeitando abaixo de minQty, com o RiskManagerV3 (`stop_risk_size`) como autoridade única e a invariante final antes do dispatch.
8. **Range de risk_pct executável?** Com equity ~100 e L = 10: r = 1% cobre 89,6% da grade (BTC só com stop ≤ ~1,45%); r = 2% cobre 97,9%; r = 0,5% cobre 75%; r = 0,25% cobre 52%. Com equity 50: r = 1% opera ETH/SOL/DOGE só com stops ≤ 1% e BTC só com stop 0,5%; r = 2% libera BTC até ~1,45%. Abaixo de 25 USDT quase nada é executável sem violar o orçamento.
9. **Dependem de F-013:** a verificação de perda real pós-fill, a reaplicação de stop, o 1R/accounting em R e o fallback de ticker.
10. **Independentes:** trocar a autoridade de sizing para o V3; a invariante pré-dispatch; unificar o custo; remover hooks sombreados e docstrings divergentes; cap agregado de risco aberto; tornar a compressão de stop uma rejeição.

## 29. FULL TEST STATUS

`python -m tests.run_offline`: `TOTAL=1861 PASSED=1857 FAILED=1 SKIPPED=0 MANDATORY_SKIPPED=0 FAILED_SUITES=1`

| métrica | valor |
|---|---|
| Passed | 1857 / 1861 testes |
| Failed | 1 suíte (`test_research_process`) |
| Known baseline failures | `test_research_process` (mesma falha de ambiente da baseline, `test_real_child_runs_research_without_blocking_loop`) |
| New regressions | 0 |
| Novos testes | `tests.test_f003_sizing_characterization` 8/8 (diagnóstico, sem efeito em produção) |

Confirmação: **NO production code change · NO merge · NO deploy · NO real order.** Foram criados apenas este documento,
`research/risk_policy/f003_sizing_audit.py` e o teste diagnóstico `tests/test_f003_sizing_characterization.py`
(somente leitura do comportamento).
