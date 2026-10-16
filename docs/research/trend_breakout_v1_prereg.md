# TREND_BREAKOUT_V1: pré-registro

Escrito e congelado em 2026-10-16, antes de qualquer dado de 2021-10 a 2024-08
ser lido por este ou por qualquer outro estudo desta série. Os parâmetros são
os do dicionário `PREREG` em `bot/trend_breakout_study_v1.py`, e o relatório
grava o SHA-256 dele.

## Por que existe

O RAW_SIGNAL_EDGE_V1 e o EXIT_MANAGEMENT_V1 reprovaram a estratégia atual. A
perda já existia antes das taxas, e o custo de ida e volta, de 0,2% a 0,3%,
consumia uma fatia grande de um stop de cerca de 0,7%. Este estudo testa duas
regras de seguir tendência com stops medidos pela amplitude do dia, não pelo
ATR de 15 minutos. Cada hipótese tem veredito próprio.

## Hipótese B1: day trade por rompimento de volatilidade

Para cada par e cada dia UTC, com o dia anterior e os 20 dias anteriores
completos:

- **Direção.** Comprado se o fechamento de ontem estiver acima da média dos
  últimos 20 fechamentos, vendido se estiver abaixo e nada se for igual.
- **Gatilho.** Abertura do dia ± k × a amplitude (máxima − mínima) de ontem,
  no lado da direção. A variante principal usa k = 0,5. As variantes k = 0,3 e
  k = 0,7 só aparecem como sensibilidade e não decidem.
- **Entrada.** Ordem stop no gatilho. Se o candle abrir além do gatilho, o
  preenchimento é na abertura. Não há entradas em candles que começam a partir
  das 20:00 UTC. Fica um trade por par por dia.
- **Stop.** A 0,5 × a amplitude de ontem do gatilho. Com k = 0,5, ele cai
  exatamente na abertura do dia: se o preço volta à abertura, o rompimento
  falhou.
- **Saída.** No stop, ou no fechamento do último candle do dia UTC. Não há alvo
  nem trailing.
- **Mesmo candle.** Se o candle de entrada também tocar o stop, conta como
  stopado depois da entrada (resolução conservadora).
- **Cluster do bootstrap:** o dia UTC, juntando todos os pares, porque os
  trades do mesmo dia andam juntos.

## Hipótese B2: swing por rompimento de canal diário

- **Sinal.** Fechamento diário acima da máxima dos 20 dias anteriores (compra)
  ou abaixo da mínima (venda), só quando não há posição no par.
- **Entrada.** Na abertura do dia seguinte.
- **Stop inicial.** A 2 × o ATR de 20 dias, contado a partir do preenchimento.
  O ATR é a média simples do true range.
- **Saída.**
  - no stop, verificado em candles de 15 minutos;
  - na abertura seguinte a um fechamento além do canal oposto de 10 dias;
  - em `DATA_GAP`, se faltar um dia;
  - em `DATA_END`, no fim dos dados.
- **Variantes.** A principal é 20/10. A 55/20 é só sensibilidade.
- **Cluster do bootstrap:** o mês UTC da entrada, juntando todos os pares,
  porque as tendências de cripto são do mercado inteiro.

## Dados, custos e janelas

Os 25 pares são os mesmos do RAW_SIGNAL_EDGE_V1, com candles de 15 minutos e
funding dos arquivos mensais de data.binance.vision, verificados por SHA-256.

**Dados ausentes:**

- Um par entra no primeiro mês publicado.
- Um mês ausente depois disso tira os dias dele, e isso fica registrado.
- Só um HTTP 404 conta como ausente. Qualquer outro erro de download
  interrompe o estudo.
- Um dia só é usado se tiver os 96 candles contíguos.

**Custos:**

- Taxa taker principal de 0,06% por lado, com sensibilidade em 0,04% e 0,05%.
- Slippage estática por par em todo preenchimento.
- Funding real nos horários de liquidação. Se faltar o arquivo de um mês, cada
  liquidação daquele mês cobra 0,01% contra a posição.

**Medida:** o R é medido pela distância planejada até o stop sobre o
preenchimento, como o dimensionamento da produção faz.

| Janela | Meses | Uso |
|---|---|---|
| Aquecimento | 2021-10 a 2021-12 | só histórico |
| Holdout | 2022-01 a 2024-08 | decide o veredito; nunca foi lido por esta série |
| Descritiva | 2024-09 a 2026-09 | já vista pelos estudos anteriores; só descreve |

## Critério

O critério vale para cada hipótese separadamente, no holdout, com a variante
principal e a taxa de 0,06%:

- pelo menos 150 trades e 10 pares;
- média líquida maior que 0;
- limite inferior de 95% do bootstrap por cluster (10.000 réplicas, semente
  20261016) maior que 0;
- média maior que 0 em todos os leave-one-symbol-out.

| Veredito | Condição |
|---|---|
| `B1_PASS` / `B2_PASS` | todos os critérios passam |
| `B1_FAIL` / `B2_FAIL` | algum critério falha |
| `B1_INSUFFICIENT_EVIDENCE` / `B2_INSUFFICIENT_EVIDENCE` | menos trades ou pares que o mínimo |

## O que cada resultado significa

- **B1 PASS:** o day trade de rompimento tem vantagem histórica depois dos
  custos. O próximo passo é implementá-lo no bot atrás de uma flag, em modo
  sombra, com um pré-registro prospectivo novo. Isso não libera LIVE.
- **Só B2 PASS:** a vantagem está no prazo mais longo. Usar a B2 exige aceitar
  posições abertas por dias ou semanas.
- **As duas FAIL:** nenhuma das duas regras deve ir para o bot.

## Limitações

- **Viés de sobrevivência.** Os 25 pares foram escolhidos em 2025 e 2026 entre
  os que continuam líquidos. Pares que quebraram no período, como LUNA e FTT,
  ficam de fora. O efeito pode ir para qualquer lado.
- **Período conhecido.** O holdout inclui a queda de 2022 e a alta de 2023 e
  2024. A escolha de seguir tendência não usou estes dados, mas o autor sabe
  que houve tendências fortes nesse período. Por isso um PASS ainda precisa de
  confirmação prospectiva.
- **Último preço no lugar do preço de marcação.** O estudo usa o último preço,
  sem book. A slippage é estática e pode subestimar os rompimentos, que
  costumam acontecer com o livro andando.
- **Sem limite de exposição.** A simulação é por par, sem limite de exposição
  total. A produção opera uma posição por vez, o que muda a seleção.
- **Capital mínimo ignorado.** O mínimo de ordem da Binance fica de fora. Com
  stops largos e conta pequena, a posição pode não atingir o mínimo; o
  relatório mostra o stop mediano para medir isso.
- **Não autoriza nada em produção.** É uma simulação histórica: não prova
  execução nem libera LIVE.
