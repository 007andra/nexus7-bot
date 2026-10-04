# NEXUS Feasibility Frontier v1 e auditoria de custo real

Escopo: pesquisa e shadow. Nada aqui altera produção, risk_pct, recovery, leverage, score, EV, R:R, sizing ou os gates (`decision_effect=NONE`, `execution_effect=NONE`). Os cenários de equity são apenas analíticos e não autorizam nenhuma mudança de saldo.

Arquivos:
- `dataset.json`: filtros dos 25 símbolos com proveniência, geometria observada e 665 setups únicos de produção (logs do Railway de 27/09 a 03/10, leitura apenas).
- `REPORT.md`: tabelas geradas por `python -m bot.feasibility_frontier_report dataset.json REPORT.md`.
- `bot/feasibility_frontier.py`: motor da fronteira. Reutiliza `sizing_decomposition.decompose` e `nexus_ai.expected_value`.
- `bot/bbo_cost_shadow.py`: comparação STATIC_COST vs LIVE_BBO_COST.

Nenhum desses módulos é registrado pelo runtime, e um teste garante isso.

## Validação do modelo contra produção

- **MIN_ORDER:** reproduz o log do SOL (`min_valid_qty=0.05`, PASS) e os números de BNB do PR #492.
- **NEXUS:** o R:R líquido estático reproduz o `rr_net` registrado em **86 de 86** avaliações reais (tolerância de 0,002).
- **Custos:** idênticos aos do `[TECHNICAL_STOP_POLICY]`:
  - majors (BTC, ETH, SOL): taker 5 bps por lado e slippage 5 bps por lado, ida e volta de 0,20%;
  - alts: taker 5 bps e slippage 10 bps por lado, ida e volta de 0,30%.
- **Margin cap:** 100% do disponível (`MAX_MARGIN_PCT=1.0`, operador em 100%). Ele nunca é o limite que bloqueia a ordem mínima.

## Proveniência dos filtros (sem valores inventados)

- **OBSERVED, 4 símbolos:** AAVE, LINK, LTC e NEAR. Filtros completos vieram do `[SIZING_DECOMPOSITION]`. LINK e LTC têm **minNotional de 20 USDT**.
- **INFERRED, 11 símbolos:** BNB, BTC, AVAX, SOL, TRX, PEOPLE, FIL, ARB, OP, ATOM e ETC. Reconstruídos a partir de `min_valid_qty`, binding e preço, com o ceil-to-step conferido. ETC tem minNotional de 20.
- **ASSUMED, 10 símbolos:** ETH, XRP, ADA, DOGE, DOT, SUI, APT, UNI, INJ e SEI. Não há filtros nos logs, então rodo dois cenários: N5 (minNotional 5, otimista) e N20. A matriz corrigida no PR #492 passa a registrar os valores reais no próximo boot.

## Fase 3: custo real

1. **Fonte correta de BBO.**
   - O stream `<symbol>@ticker` (24hrTicker) da USD-M **não traz bid/ask**.
   - O handler em `bot/binance.py` (por volta da linha 2125) grava `bid=0/ask=0` e, além disso, sobrescreve o bid/ask válido que `get_ticker()` lê do REST `bookTicker`.
   - A fonte correta é o stream WS `<symbol>@bookTicker`, com os campos `u`, `E` (event time), `T` (transaction time), `b/B` (melhor bid e quantidade) e `a/A` (melhor ask e quantidade). O REST `GET /fapi/v1/ticker/bookTicker` serve de bootstrap e fallback.
2. **Frequência e latência.**
   - O `bookTicker` é publicado a cada mudança do topo de livro, em tempo real, sem agregação de 1 s como o `@ticker`.
   - O REST custa peso 2 por símbolo ou 5 para todos. Não deve ser lido por candidato em loop.
   - O snapshot deve ser mantido via WS para os 25 símbolos, ou um combined stream `!bookTicker`.
3. **Snapshot BBO.** `BBOSnapshot` guarda bid, ask, as duas quantidades, `event_ms` (T ou E) e `received_ms`, e é construído direto do payload WS ou REST (`from_book_ticker`).
4. **Detecção de dado velho (fail-closed).** O snapshot é recusado quando:
   - está velho localmente (`now − received > 2 s`);
   - o atraso da exchange é alto (`received − event > 2 s`);
   - o livro está cruzado ou um dos lados está vazio;
   - algum valor não é numérico ou não é positivo.

   Nesses casos o registro sai com status de erro e `would_change_decision=NA`, sem comparação.
5. **Meio spread.** `(ask − bid) / (2 · mid)`.
6. **Spread vs. impacto.**
   - Spread é propriedade da cotação.
   - Impacto depende do tamanho da ordem em relação à profundidade do topo. Para ordens que cabem no topo (5 a 20 USDT), uso o piso do runtime: 1 bp nos majors e 2 bp nas alts.
   - Acima do topo, cada múltiplo adicional do topo soma um spread inteiro, como proxy conservador.
7. **Custo estimado vs. custo real.**
   - Comparar o registro shadow `[COST_SHADOW_BBO]` com o slippage efetivo do fill (`fill_vwap` vs. `mid` no envio).
   - O `[BINANCE_EXIT_FORENSICS]` já registra `open_vwap` e `close_vwap`. Basta registrar também o `mid` do BBO no envio.

**Resultado shadow com dados reais** (86 avaliações do NEXUS em 03/10, confiança real, BBO hipotético de 1 tick):
- `would_change_decision=true` em **1 de 86** (AVAX: R:R líquido 1,347 com custo estático contra 1,636 com BBO).
- Esse candidato morreria depois no MIN_ORDER (risco na quantidade mínima de 0,153 contra orçamento de 0,0438).
- Com 3 ticks: 0 de 86 mudariam.

**Resultado agregado nos 665 sinais** (REPORT, seção 6):

| cenário | NEXUS aprova (independe de equity) | cinco gates, equity 8,76 | equity 15 | equity 20 | equity 25 |
|---|---|---|---|---|---|
| STATIC (hoje) | 264 | 5 | 33 | 39 | 64 |
| BBO 3 ticks | 467 | 7 | 60 | 133 | 200 |
| BBO 1 tick | 511 | 8 | 80 | 164 | 231 |

## Fase 4: respostas objetivas

**1. Pares estruturalmente impossíveis hoje** (nenhuma das 54 combinações de R e stop passa com equity de 8,76):
- AAVE, AVAX, BNB, BTC, ETC, LINK e LTC;
- mais todos os ASSUMED se o minNotional deles for 20.
- BTC não passa nem com 50 USDT, porque a ordem mínima tem cerca de 85 USDT de notional.

**2. Executáveis no MIN_ORDER, com a geometria observada e a equity atual:**
- TRX: 34 de 34 sinais;
- ETH: 7 de 9 (filtros ASSUMED);
- SOL: 8 de 31;
- BNB: 5 de 23;
- DOGE: 1 de 11 (ASSUMED).

O MIN_ORDER aprova 55 dos 665 sinais.

**3. MIN_ORDER + NEXUS ao mesmo tempo:**
- Na prática, só **SOL e ETH com R bruto 3** (5 de 665 sinais, 0,8%): SOL com 3 sinais e filtros INFERRED; ETH com 2 sinais e filtros **ASSUMED**.
  O resultado de ETH é hipótese (cenário N5, otimista) e **não deve ser promovido** até os filtros reais serem registrados.
- Em teoria, quase todas as alts com minNotional de 5 passariam com R ≥ 3,5 e stop de cerca de 0,5%. Essa geometria quase não aparece na estratégia atual.
- Os dois gates selecionam populações quase disjuntas:
  - o MIN_ORDER exige stop curto (orçamento de 0,0438 USDT para uma ordem mínima de cerca de 5 USDT ou mais);
  - o NEXUS exige stop ≥ 6,5 × o custo quando R = 2 (1,30% nos majors, 1,95% nas alts).

**4. R bruto mínimo por símbolo, no stop mediano observado.** Está no REPORT, seção 4. Exemplos:
- TRX 5,11; BNB 3,80; BTC 3,34; ETH 2,76 (filtros ASSUMED; o R mínimo independe dos filtros); SOL 2,37;
- alts de stop largo ficam entre 1,9 e 2,3 (ARB, FIL, INJ, LINK e NEAR passam com R = 2).

**5. Equity mínima para o MIN_ORDER, no stop mediano observado, com as políticas atuais:**
- TRX 5,2; ETH 7,1 (ASSUMED, N5); BNB 10,3; SOL 10,5;
- DOGE 11,8 (ASSUMED); XRP 13,2 (ASSUMED); ATOM 14,6; ADA 14,9 (ASSUMED); APT 14,5 (ASSUMED);
- OP 18,3; UNI 18,7 (ASSUMED); DOT 18,9 (ASSUMED); PEOPLE 19,5; SEI 20,6 (ASSUMED); SUI 20,9 (ASSUMED);
- FIL 23,0; ARB 24,0; INJ 28,1 (ASSUMED); NEAR 29,5;
- LTC 44,8; AVAX 47,0; ETC 54,4; AAVE 79,9; BTC 84,6; LINK 92,4 USDT.

Valores marcados ASSUMED usam o cenário N5 (otimista) e são hipóteses, não fatos; com minNotional 20 a equity mínima é cerca de 4× maior (ver REPORT). Isso não basta sozinho: o NEXUS também exige o R mínimo do item 4.

**6. O custo está sendo superestimado?** Provavelmente sim, e de forma material:
- alts: 30 bps estáticos contra pelo menos 14 a 17 bps com BBO;
- majors: 20 bps contra cerca de 12 a 13 bps.

Isso **não está provado**: 1 tick é o limite inferior. É preciso medir com BBO real em shadow antes de qualquer conclusão.

**7. Dá para aumentar a frequência sem aumentar risco?**
- **Com a equity atual, quase nada.** O orçamento de risco de 0,0438 USDT domina, e o melhor caso de custo leva de 5 para 8 sinais em 665.
- **Com equity ≥ 15 USDT** (cenário analítico), custo fiel e preferência por setups com R ≥ 3 multiplicam a interseção (de 33 para 60–80 sinais em 665).
- Excluir símbolos impossíveis não aumenta a frequência. Só elimina avaliações inúteis e ruído.

**8. Só observabilidade** (sem efeito em decisão):
- PR #492: telemetria `[CANDIDATE_TERMINAL]`, matriz de viabilidade e regex de latência;
- registrar também o `mid` do BBO no envio, para medir o slippage real.

**9. Pesquisa ou shadow:**
- este relatório e os módulos `feasibility_frontier` e `bbo_cost_shadow`;
- ligar um feed `@bookTicker` dedicado que emita `[COST_SHADOW_BBO]` por candidato sem alimentar `execution_cost`. Isso toca o transporte WS e exige um PR próprio, com prova de que não há efeito em decisão;
- um ranking de executabilidade por símbolo só em log, como base para um dynamic universe.

**10. Alteraria política LIVE (NÃO aplicar automaticamente):**
- usar o custo de BBO em `execution_cost`, NEXUS ou sizing;
- mudar o piso `NEXUS_EXPECTED_SLIPPAGE_PCT`;
- excluir ou priorizar símbolos no universo LIVE;
- preferir ou filtrar entry types ou R bruto;
- qualquer mudança em `NEXUS_MIN_RR_NET`, MIN_RR, risk_pct, recovery, leverage ou equity.

## Limitações

- 10 símbolos com filtros ASSUMED.
- O tick é inferido pelas casas decimais dos preços.
- A modelagem do NEXUS cobre só os gates de EV e R:R líquido. A decisão real tem outros vetos (modelos, regime, confiança), então as taxas reais de aprovação são menores ou iguais às da fronteira.
- `win_prob` é a heurística do runtime a partir da confiança: 0,45 equivale a confiança 33; o caso observado de confiança 12,4 dá cerca de 0,356.
