# EXIT_MANAGEMENT_V1: pré-registro

Escrito e congelado em 2026-10-14, antes de qualquer dado de 2024-09 a 2025-09
ser lido por este ou por qualquer outro estudo desta série. Os parâmetros são
os do dicionário `PREREG` em `bot/exit_management_study_v1.py`, e o relatório
grava o SHA-256 dele.

## Por que existe

O RAW_SIGNAL_EDGE_V1 deu FAIL (−0,40 R líquido no holdout), mas usava uma saída
simplificada: tudo no stop ou no alvo, com o alvo deslocado pelo preenchimento,
e fechamento forçado em 10h. A produção LIVE gerencia a saída de outro jeito, e
essa era a única limitação daquele estudo capaz de mudar o resultado. Este
estudo usa os mesmos sinais e troca só a saída.

## Gestão de saída simulada

O comportamento efetivo em LIVE depois do `runtime_bootstrap`, levantado no
código:

- **Stop e alvo nativos.** Há um `STOP_MARKET` no stop e um `TAKE_PROFIT_MARKET`
  no `sig.tp` planejado. O TP não é deslocado. O stop vira o mais protetor
  entre o planejado e a distância planejada reancorada no preenchimento
  (`postfill_risk_recheck`).
- **Parcial.** Fecha 50% uma vez, quando o lucro chega a |entrada − stop atual|
  mais 0,03% da entrada. Depois disso o stop vai para o breakeven
  (`durable_partial_exit`).
- **Trailing.** Ativa quando o lucro chega a 0,5 × |alvo − entrada|, e então o
  stop vai para entrada ± 0,75 × o maior avanço a favor
  (`trailing_safety_hardening`). Os stops nunca são cancelados, então vale o
  mais protetor.
- **Sem saída por tempo.** As saídas por estagnação, CHoCH e regime ficam
  inertes em LIVE (`operator_loss_policy`). A saída em 2R também é inerte na
  prática. O teto de segurança é de 21 dias (2016 candles de 15m), e uma saída
  por esse teto é marcada como `MAX_HOLD`.

Cada candle é resolvido de dois jeitos:

- **Primário, `OHLC_PATH`.** Candle de alta percorrido abertura → mínima →
  máxima → fechamento; candle de baixa, abertura → máxima → mínima →
  fechamento. As regras agem quando o caminho passa por elas, como na
  amostragem frequente do preço de marcação que o bot faz.
- **Robustez, conservador.** No mesmo candle o stop vem antes do alvo, e a
  gestão só protege a partir do candle seguinte.

Taxa, slippage e funding seguem o RAW_SIGNAL_EDGE_V1 (taxa primária de 0,06%
por lado, sensibilidade com 0,04% e 0,05%, slippage estática por par).

## Dados, sinal e populações

São idênticos ao RAW_SIGNAL_EDGE_V1: mesmos 25 pares, mesma pilha efetiva do
analisador, mesmas janelas, relógio congelado, rede bloqueada no worker,
populações `ALL`, `PULLBACK_PASS` e `FUNNEL` (primária) e seleção sem
sobreposição por par.

| Janela | Meses | Uso |
|---|---|---|
| Aquecimento | 2024-09 | só histórico |
| Holdout | 2024-10 a 2025-09 | decide o veredito; nunca foi lido por esta série |
| Descritiva | 2025-10 a 2026-09 | já vista no RAW_SIGNAL_EDGE_V1; só descreve |

## Critério

O critério de cada resolução é o mesmo do RAW_SIGNAL_EDGE_V1, aplicado no
holdout à população `FUNNEL` com taxa de 0,06%:

- pelo menos 150 trades e 10 pares;
- média líquida maior que 0;
- limite inferior de 95% do bootstrap por cluster par×dia maior que 0;
- média maior que 0 em todos os leave-one-symbol-out.

| Veredito | Condição |
|---|---|
| `EXIT_MANAGEMENT_PASS` | as duas resoluções passam |
| `EXIT_MANAGEMENT_INCONCLUSIVE_INTRABAR` | só a `OHLC_PATH` passa; precisa de dados de 1 minuto para decidir |
| `EXIT_MANAGEMENT_FAIL` | a `OHLC_PATH` falha |
| `INSUFFICIENT_EVIDENCE` | menos trades ou pares que o mínimo |

## O que cada resultado significa

- **FAIL:** confirma o RAW_SIGNAL_EDGE_V1 com a saída real. A estratégia como
  está não paga custo, e a recomendação passa a ser não operar com ela.
- **PASS:** a gestão de saída faz diferença. Mesmo assim isso não libera LIVE.
  O próximo passo é um teste prospectivo com o coletor, em pré-registro novo.
- **INCONCLUSIVE_INTRABAR:** refazer a resolução com candles de 1 minuto antes
  de qualquer conclusão.

## Limitações

- **Simulação de uma posição por par.** A produção opera uma posição no total,
  o que muda a seleção mas não a expectativa por trade.
- **Último preço no lugar do preço de marcação.** Os stops nativos disparam
  pelo preço de marcação.
- **Ajustes de quantidade ignorados.** O stop pelo orçamento de risco da conta
  e o arredondamento de quantidade, que pode pular o parcial, não entram.
- **Holdout não testado não quer dizer intocado.** Os limiares da estratégia
  podem ter sido calibrados em dados que cobrem esse período. Isso favoreceria
  a estratégia, então um FAIL fica mais convincente e um PASS ainda precisaria
  de confirmação prospectiva.
- **Não autoriza nada em produção.** É uma simulação histórica; não prova
  execução nem libera LIVE.
