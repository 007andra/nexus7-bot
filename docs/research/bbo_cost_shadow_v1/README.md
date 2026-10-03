# NEXUS BBO Cost Shadow v1

Contrato:

```
RESEARCH_ONLY=true
SHADOW_ONLY=true
decision_effect=NONE
execution_effect=NONE
live_authority_unchanged=true
```

O objetivo é medir em produção, de forma passiva, o custo de mercado observado via Binance USD-M `@bookTicker`, comparando STATIC_COST com LIVE_BBO_COST.

## Arquitetura

Pipeline:

```
BinanceClient.start_websocket (original, inalterado)
   └─ depois do retorno: start_feed()  ── conexão SEPARADA wss://fstream.binance.com/public/stream?streams=<s>@bookTicker/...
                                            └─ run_feed()  (loop próprio, backoff 1→30 s, geração por conexão)
                                                 └─ BBOCache.ingest()  (parse estrito → update monotônico por geração)

TradingEngine._nexus_validate (original, inalterado; retorna o MESMO objeto champion)
   └─ depois do retorno: snapshot do cache (cópia) → task assíncrona observe()
        ├─ build_record()   (puro: sig + decision._bgx_score_snapshot + snapshot de custo estático)
        ├─ [COST_SHADOW_BBO] (deduplicado)
        └─ persist()        (INSERT … ON CONFLICT DO NOTHING em nexus_bbo_cost_shadow_v1)
```

**Arquivos**

| Arquivo | Papel |
|---|---|
| `bot/binance_bbo_feed.py` | parser, cache com gerações e loop de WS |
| `bot/bbo_cost_shadow_v1.py` | modelo de custo puro e gate EV/R:R contrafactual |
| `bot/bbo_cost_shadow_runtime.py` | instalação, os dois hooks, dedup e persistência |
| `bot/bbo_cost_shadow_replay.py` | replay de pesquisa; não é importado pelo runtime |
| `bot/runtime_bootstrap.py` | +7 linhas: registro, só para Binance |

**Fonte do bookTicker:** `@bookTicker` é um stream da rota `/public`. A conexão de market data de trading usa `/market` (kline e 24hrTicker) e continua intocada, inclusive o handler `_handle_ws_message`, protegido pelo RUNTIME_CONTRACT.

**Campos capturados:**
- `s`, `b`, `B`, `a`, `A`;
- `u` (update id), `T`/`E` (tempo da exchange);
- relógio wall UTC e monotônico no momento do recebimento;
- a geração da conexão.

## Política de validade (stale)

O `bookTicker` só é publicado quando o topo do livro muda. Medir a idade só por símbolo marcaria como velho, por engano, um livro que está apenas quieto. Por isso a validade combina quatro condições:

| condição | limite | por quê |
|---|---|---|
| geração atual | — | após reconnect, toda cotação anterior é inválida (`STALE/RECONNECT`) até chegar um frame novo daquele símbolo |
| conexão viva | `BBO_CONN_SILENCE_MS=5000` | o stream combinado dos 25 símbolos (BTC, ETH e SOL incluídos) carrega muitos frames por segundo; 5 s sem nenhum frame indica socket morto, não mercado parado |
| idade do símbolo | `BBO_SYMBOL_MAX_AGE_MS=30000` | teto de segurança contra uma assinatura perdida silenciosamente |
| atraso da exchange no recebimento | `BBO_MAX_EXCHANGE_LAG_MS=2000` | um frame que já chega velho é recusado (`STALE_ON_ARRIVAL`) |
| relógio no futuro | `BBO_MAX_FUTURE_SKEW_MS=1000` | `INVALID_TIMESTAMP` |

Valores rejeitados (`INVALID_BOOK`):
- bid ≤ 0, ask ≤ 0 ou quantidades ≤ 0;
- ask < bid;
- NaN ou infinito;
- bool, lista ou string não numérica;
- preço acima de 1e9 ou quantidade acima de 1e15.

Também são rejeitados: símbolo desconhecido, `u` ≤ 0 e update id não monotônico na mesma geração.

**Estados expostos:** `MISSING_BOOK`, `STALE` (com a causa `RECONNECT`, `DISCONNECTED`, `CONN_SILENT` ou `SYMBOL_AGE`), `INVALID_BOOK` e `OK`.

**Calibração:** os limites são parâmetros de pesquisa configuráveis por variável de ambiente. O feed registra `[BBO_SHADOW_FEED] event=stats` a cada 5 minutos com as contagens, para que os limites sejam validados prospectivamente.

**Efeito em trading:** nenhum desses estados bloqueia o trading LIVE.

## Reconexão

- Cada conexão abre uma **geração** nova (`begin_generation`).
- Um frame de geração antiga é descartado (`OLD_GENERATION`), inclusive na corrida em que a mensagem antiga é processada depois que a conexão nova já subiu.
- Na geração nova, update ids menores voltam a ser aceitos, porque a numeração pode reiniciar com a conexão.
- Assinaturas duplicadas não têm efeito: a URL deduplica os streams, `start_feed` é idempotente por cliente e um frame repetido (`u` igual ou menor) é ignorado.

## Modelo de custo

Cada componente do custo de ida e volta fica separado, em bps:

| Componente | Origem |
|---|---|
| `static_fee_bps` | 2 × taker, do snapshot de produção |
| `static_slippage_bps` | slippage de entrada + saída, estático |
| `static_total_cost_bps` | fee + slippage |
| `live_spread_bps` | spread observado |
| `live_half_spread_bps` | metade do spread observado |
| `live_fee_bps` | mesma taxa taker |
| `estimated_impact_bps` | **NA** (`impact_model=UNPROVEN`): o topo do livro não mede impacto |

Como o impacto não é medido, há dois valores de custo ao vivo, nunca misturados:
- `LIVE_BBO_SPREAD_ONLY` = fee + spread observado;
- `LIVE_BBO_PLUS_STATIC_IMPACT` = o anterior + o piso de impacto que o próprio runtime já assume (1 bp por lado nos majors, 2 bp nas alts). É hipótese rotulada. Este é o valor registrado em `live_total_cost_bps`.

O piso de slippage do sizing (`NEXUS_EXPECTED_SLIPPAGE_PCT`) só é **reportado**. Nunca é alterado.

A comparação com o NEXUS usa a mesma álgebra de `nexus_ai.expected_value` e o piso de R:R líquido do runtime. A paridade com o `rr_net` do champion é verificada a cada registro (`static_parity`).
- `shadow_allowed` e `would_change_decision` descrevem **só o gate EV/R:R** (`decision_scope=EV_RR_GATE`). As etapas seguintes do NEXUS não são reexecutadas.
- A decisão champion não é tocada.

## Por que isto não pode afetar o trading LIVE

1. **Champion intacto.** `nexus_validate_with_bbo_shadow` retorna `decision`, o mesmo objeto (`bbo_cost_shadow_runtime.py`). Os testes `test_19` e `test_27_28` provam identidade e ausência de mutação.
2. **BBO fora dos caminhos de trading.**
   - O cache só existe no registro do módulo de runtime.
   - O `test_30` prova que só os dois módulos do shadow referenciam `cache_for`/`BBOCache`.
   - O `test_research_and_replay_modules_not_imported_by_trading_files` faz um scan AST de todo o `bot/`.
3. **Conexão de market data de trading intocada.**
   - O feed abre outra conexão, na rota `/public`.
   - `_handle_ws_message` não muda (`test_30`).
   - `start_websocket` devolve o resultado original mesmo se o feed falhar (`test_30`).
4. **Sem exchange privada, ordem, posição, sizing, risco, recovery, leverage ou config.** Coberto por `test_20_21_22` e `test_23_24_25`. Os dois testes falham se a matemática de trading for chamada.
5. **Fail-open só no shadow.** Erros de feed, parse, cálculo, log ou persistência viram `SHADOW_DATA_UNAVAILABLE` (`test_26`). Os gates de trading continuam fail-closed, sem nenhuma alteração.
6. **Persistência append-only.** `INSERT … ON CONFLICT(observation_id) DO NOTHING`, sem UPDATE nem DELETE (`test_26b`).
7. **Contrato de runtime.** `test_binance_cross_stress_dispatch_proof` (44), `test_runtime_contract_guard` (8) e `test_operator_runtime_contract` (2) passam com o bootstrap instalado.
8. **Kill switch.** Os flags `NEXUS_BBO_COST_SHADOW=false` e `NEXUS_BBO_COST_SHADOW_PERSIST=false` desligam o shadow.

## Replay sobre a população auditada

Ver `REPLAY.md`.
- Não existe BBO gravado para os 86 candidatos de 03/10 (86 de 86 `MISSING`).
- Nos cenários contrafactuais de 1 e 3 ticks, o custo cai de 20 para cerca de 12,8–14,5 bps.
- O gate EV/R:R mudaria em 1 avaliação (AVAX, 1 tick). Esse candidato **não** é executável no MIN_ORDER, então 0 chegariam ao final sizing.

## Critérios de evidência

Classificação atual: **UNPROVEN**.

Antes de qualquer recomendação LIVE, é preciso reunir:
- amostra prospectiva em múltiplos regimes;
- spreads reais por símbolo e horário;
- fills reais e slippage realizado (VWAP do fill contra o mid no envio);
- seleção adversa;
- comparação entre entrada e saída.

Escala:
- **PRELIMINARY** exige amostra prospectiva sem viés de seleção;
- **SUPPORTED** exige fills reais.

Nunca há promoção automática.
