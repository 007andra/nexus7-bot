# RAW_SIGNAL_EDGE_V1: pré-registro

Escrito e congelado em 2026-10-14, antes de qualquer dado histórico ser lido. Os
parâmetros abaixo são os do dicionário `PREREG` em
`bot/raw_signal_edge_study_v1.py`. O relatório grava o SHA-256 desse dicionário
(`prereg_sha256`), então qualquer mudança posterior fica visível.

## Pergunta

O sinal da estratégia, antes do filtro NEXUS, paga os próprios custos na
Binance USD-M?

As coortes prospectivas em produção mostraram que o NEXUS escolhe pior que o
acaso (`PROSPECTIVE_OOS_COHORT_V1` com `EVIDENCE_FAIL` e lift negativo nos dois
horizontes). Antes de mexer no filtro é preciso saber se existe algo a filtrar.
Se o sinal bruto não paga custo, nenhum filtro resolve e a estratégia precisa
ser repensada.

## Dados

Arquivos mensais públicos de `data.binance.vision` (USD-M): candles de 15m, 1h
e 4h e o histórico de funding. Cada arquivo é conferido contra o `.CHECKSUM`
publicado antes do uso, e qualquer buraco ou sobreposição nos candles aborta o
estudo.

O universo são os 25 pares de `cfg.SYMBOLS`, o universo de trading configurado.

| Janela | Meses | Uso |
|---|---|---|
| Aquecimento | 2025-09 | só histórico para os indicadores |
| Descoberta | 2025-10 a 2026-05 | exploração livre, não decide nada |
| Holdout | 2026-06 a 2026-09 | único período que decide o veredito |

## Sinal

O estudo usa a pilha efetiva de produção: os wrappers do `runtime_bootstrap`
sobre `Analyzer.analyze_mtf`, chamados do mesmo jeito que o scan
HARD_GATE_SHADOW chama. As janelas são as mesmas (200/100/120 candles, com
mínimo de 60/40/20), e a avaliação roda dentro de `hard_gate_shadow_context.scope()`.
A cada fronteira de 15m o relógio do processo fica congelado em t+1s e o
analisador só vê candles fechados. A rede do processo de cálculo fica bloqueada
por um audit hook, e ele só lê o cache verificado.

Os parâmetros de score são os padrões do código: `MIN_ENTRY_SCORE=60`,
`MIN_RR_RATIO=2.0`, `MIN_VOLUME_MULT=0.5` e `FEE_MULTIPLIER=2.0`. O log de
produção confirma `score >= 60`. As outras variáveis existem no Railway, mas
os valores vêm ocultos e não puderam ser conferidos. Isso é uma limitação.

Há três populações, todas exigindo `rr >= 2.0`:

- `ALL`: todo sinal que o analisador produz, inclusive os barrados pelo pullback.
- `PULLBACK_PASS`: os que passam a confirmação de pullback, ou seja, os que o
  loop LIVE receberia.
- `FUNNEL` (primária): `PULLBACK_PASS` mais o funil pré-NEXUS de produção.
  Isso inclui o score ajustado pela sessão (calculada na hora da decisão, não
  na hora atual), a direção permitida pelo regime e `expected_pnl > 0`.

## Resultado de cada sinal

O simulador é o de `nexus_oos_real_replay._simulate_net_r`:

- entrada a mercado na abertura do candle seguinte, com slippage adverso
  estático por par;
- stop e alvos deslocados pela diferença de preenchimento;
- TP parcial de 50% no TP1, com o stop indo para o breakeven depois;
- quando stop e alvo caem no mesmo candle, conta o stop;
- horizonte máximo de 40 candles de 15m (10h), e fecha a mercado se nada for
  atingido;
- taxa de taker na entrada e na saída, mais o funding real cobrado no período.

O resultado é medido em R líquido, ou seja, em múltiplos do risco inicial.

A taxa primária é 0,06% por lado, o mesmo piso que a produção usa quando não
consegue ler a taxa real da conta. Para sensibilidade, o estudo também calcula
com 0,04% e 0,05%.

Os trades não se sobrepõem por par: um novo sinal do mesmo par só conta depois
da saída simulada do anterior. Isso se aproxima de um bot com uma posição por
par e evita contar o mesmo movimento várias vezes.

## Critério de aprovação

O veredito sai só do holdout, na população `FUNNEL`, com taxa de 0,06%.

- `INSUFFICIENT_EVIDENCE` se houver menos de 150 trades ou menos de 10 pares.
- `RAW_SIGNAL_EDGE_PASS` se tudo abaixo valer ao mesmo tempo:
  - R líquido médio maior que 0;
  - limite inferior unilateral de 95% maior que 0, calculado com bootstrap por
    cluster (par × dia UTC), 10.000 réplicas e seed 20261014;
  - R médio maior que 0 ao retirar cada par, um de cada vez (leave-one-symbol-out).
- `RAW_SIGNAL_EDGE_FAIL` em qualquer outro caso.

## O que cada resultado significa

- **PASS**: existe um sinal para filtrar. O próximo passo é congelar a regra e
  testar daqui para frente com o coletor isolado (#666), em pré-registro novo.
  Não autoriza LIVE.
- **FAIL**: a estratégia como está não paga custo. O trabalho no filtro NEXUS
  para, e a decisão passa a ser redesenhar a estratégia ou encerrá-la.
- **INSUFFICIENT_EVIDENCE**: o resultado fica registrado como está. Estender
  período, universo ou critério exige um pré-registro novo, e o holdout já
  aberto não pode ser reaproveitado como holdout.

O período de descoberta pode ser explorado à vontade (por direção, tipo de
entrada, regime, par ou mês), mas nada visto lá muda este critério.

## Limitações conhecidas

- O NEXUS não é chamado. Não existe histórico de OI, book ou notícias, então
  este estudo não mede o filtro, só o sinal.
- O proxy de execução ignora trailing stop, saída por estagnação e saídas por
  CHoCH ou regime, que a produção usa.
- A slippage é estática por par, e não há medida de BBO histórico.
- Os valores reais de `MIN_RR_RATIO`, `MIN_VOLUME_MULT` e `FEE_MULTIPLIER` no
  Railway não puderam ser conferidos.
- É uma simulação histórica. Não prova execução nem rentabilidade real e não
  libera LIVE.
