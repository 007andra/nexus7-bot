# EXIT_MANAGEMENT_V1: resultado

Executado no GitHub Actions no PR #679. Os parâmetros são os do pré-registro
`docs/research/exit_management_v1_prereg.md`, com
`prereg_sha256 = efb377ffa8f347d62fc02e9b81fc7d474219ba6d52bf20d99e0b88aacd746ec4`,
que é o mesmo nas duas execuções.

## Veredito: EXIT_MANAGEMENT_FAIL

| Execução | Commit | Job do veredito | R líquido médio no holdout (FUNNEL, 0,06%) |
|---|---|---|---|
| 1ª (R pela distância realizada, com erro) | `2999ec2b` | 116018428254 | −0,696 |
| 2ª (R pela distância planejada, corrigida) | `28efc907` | 116067716425 | −0,279 |

A primeira execução media o R pela distância realizada entre o preenchimento e
o stop. Isso inflava as perdas nos trades em que o candle de entrada abria
colado no stop (veja a correção registrada no pré-registro). A segunda execução
mede como o RAW_SIGNAL_EDGE_V1 e como o dimensionamento da produção. As duas
reprovam. O veredito não depende da correção.

## Resultado corrigido

Base do veredito: holdout (2024-10 a 2025-09, nunca lido antes por esta série),
população `FUNNEL`, taxa de 0,06% por lado, gestão de saída LIVE.

| Métrica | Caminho OHLC (primário) | Conservador |
|---|---|---|
| Trades | 33.981 em 25 pares | 32.184 em 25 pares |
| R líquido médio | −0,279 | −0,329 |
| Limite inferior de 95% (bootstrap por cluster) | −0,289 | −0,338 |
| Pior média sem um dos pares (LOSO) | −0,284 | −0,333 |
| Taxa de acerto | 43,6% | 41,1% |
| Profit factor | 0,52 | 0,45 |
| R médio antes de taxa e funding (com slippage) | −0,149 | (não reportado) |

Sensibilidade à taxa (caminho OHLC):

| Janela | Trades | 0,04% | 0,05% | 0,06% |
|---|---|---|---|---|
| Holdout | 33.981 | −0,236 | −0,258 | −0,279 |
| Descritiva | 31.424 | −0,291 | −0,317 | −0,343 |

### Como as posições terminaram no holdout

| Saída | Trades |
|---|---|
| Stop original | 18.475 |
| Trailing | 10.509 |
| Take profit | 2.541 |
| Breakeven | 2.456 |

O parcial em 1R aconteceu em 36% dos trades, e a mediana de permanência foi de
3 candles de 15m.

### Recortes

O resultado é negativo em todos os recortes das duas janelas:

- **Pares:** todos os 25. No holdout, o melhor foi PEOPLEUSDT (−0,15 R) e o pior,
  TRXUSDT (−0,73 R).
- **Meses:** todos os 12 de cada janela.
- **Direções:** as duas. No holdout, LONG −0,31 R e SHORT −0,25 R.
- **Tipos de entrada:** PULLBACK −0,18 R, MOMENTUM −0,25 R e BOS_BREAK −0,38 R.
  A exceção é `NONE`, com 45 trades, pequena demais para contar.

## Leitura

A gestão de saída da produção (parcial, breakeven e trailing) melhora o
resultado em relação à saída simplificada do RAW_SIGNAL_EDGE_V1, mas não chega
perto de torná-lo positivo. A perda já existe antes de taxa e funding
(−0,15 R), e os custos a quase dobram. Não é uma questão de janela, par, direção
ou tipo de entrada.

Junto com o RAW_SIGNAL_EDGE_V1, isso responde à pergunta: **a estratégia atual,
com a gestão de saída que a produção usa, não paga os próprios custos na
Binance USD-M.** A reexecução do RAW_SIGNAL_EDGE_V1 neste PR repetiu aquele
veredito linha a linha (job 116055681332), o que confirma que o refactor do
gerador de sinais não mudou nada.

Isso não autoriza nem bloqueia nada em produção por conta própria, e o LIVE
continua bloqueado pelos gates existentes.

## Limitações

As limitações listadas no pré-registro continuam valendo:

- uma posição por par;
- preço de último negócio em vez de preço de marcação;
- stop por orçamento e arredondamento de quantidade ignorados;
- calibração possivelmente feita em dados que cobrem o período.

A correção do R foi feita depois de ver o primeiro resultado. Como a primeira e
a segunda execução reprovam, isso não muda a conclusão.
