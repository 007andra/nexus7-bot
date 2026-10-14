# RAW_SIGNAL_EDGE_V1: resultado

Executado em 2026-10-14 no GitHub Actions (PR #677, commit `84d90be`). O
relatório veio do job "Pre-registered verdict" (id 115948980096), com
`prereg_sha256 = af4dec73540442f7273102b5e58673515c07b3d07197e9f76632e7c381bdea47`,
o mesmo `PREREG` de `docs/research/raw_signal_edge_v1_prereg.md`, que não foi
alterado depois da execução.

## Veredito: RAW_SIGNAL_EDGE_FAIL

Base do veredito: holdout (2026-06 a 2026-09), população `FUNNEL`, taxa de 0,06%
por lado, sem sobreposição por par.

| Métrica | Valor |
|---|---|
| Trades | 6.822 em 25 pares, 1.939 clusters par×dia |
| R líquido médio | −0,401 |
| Limite inferior de 95% (bootstrap por cluster) | −0,433 |
| Pior média sem um dos pares (LOSO) | −0,415 |
| Taxa de acerto | 29,2% |
| Profit factor | 0,56 |
| R médio antes de taxa e funding (já com slippage) | −0,232 |

## Sensibilidade

| Janela | População | Taxa 0,04% | Taxa 0,05% | Taxa 0,06% |
|---|---|---|---|---|
| Descoberta | FUNNEL (12.949 trades) | −0,321 | −0,348 | −0,374 |
| Holdout | FUNNEL (6.822) | −0,345 | −0,373 | −0,401 |
| Holdout | PULLBACK_PASS (6.941) | −0,349 | −0,377 | −0,405 |
| Holdout | ALL (7.144) | −0,334 | −0,360 | −0,386 |

O resultado é negativo em todos os recortes das duas janelas:

- em cada um dos 25 pares;
- em cada um dos 12 meses;
- nas duas direções;
- nos três tipos de entrada (PULLBACK, MOMENTUM e BOS_BREAK).

No holdout, o melhor par foi UNIUSDT (−0,10 R) e o pior foi TRXUSDT (−1,36 R).

## Leitura

O resultado não depende da taxa: mesmo com 0,04% por lado, o limite inferior
fica perto de −0,38 R. A taxa sozinha custa cerca de 0,17 R por trade (a
diferença entre o líquido com 0,06% e o resultado antes de taxa e funding).
Isso indica um stop médio perto de 0,7% do preço, então qualquer atrito pesa
muito em R. Mesmo antes de taxa e funding o resultado já é −0,23 R, e a slippage
estática do modelo (0,05% por lado nos majors, 0,10% nos demais) explica só
parte disso. Na melhor hipótese o sinal empata antes dos custos e perde depois
deles.

Pelo pré-registro, isso encerra o trabalho no filtro NEXUS: não existe sinal
lucrativo para filtrar. A decisão passa a ser redesenhar a estratégia ou
encerrá-la. Este resultado não autoriza nem bloqueia nada em produção por conta
própria, e o LIVE continua bloqueado pelos gates existentes.

## Limitações

As limitações listadas no pré-registro continuam valendo:

- proxy de execução sem trailing, sem saída por estagnação e sem CHoCH;
- slippage estática;
- valores de `MIN_RR_RATIO`, `MIN_VOLUME_MULT` e `FEE_MULTIPLIER` no Railway não
  conferidos.

Nenhuma delas parece capaz de inverter um resultado tão negativo e tão uniforme,
mas a primeira afeta a gestão de saída e merece teste próprio caso se queira
salvar a estratégia.
