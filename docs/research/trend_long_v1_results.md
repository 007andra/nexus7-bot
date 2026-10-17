# TREND_LONG_V1: resultado

Rodado no GitHub Actions em 2026-10-17 (run 38993972590, commit 4b038b8f),
com `prereg_sha256 = 47f2fe40e3877d54f6df14c44aa68b24af4414cccc874297d0c2c5d10e4a5db7`,
o mesmo do pré-registro. Taxa de 0,06% por lado, funding assumido de 0,02% a
cada 8 horas contra a posição na janela decisiva e funding real na de
confirmação.

| Hipótese | Veredito |
|---|---|
| H1: B2 só comprada com filtro BTC > SMA200 | **H1_PASS** |
| H2: momentum semanal top 3 | **H2_FAIL** |

## H1

| Janela | Trades | Pares | Média líquida (R) | Limite inferior 2,5% | Pior LOSO | Acerto |
|---|---|---|---|---|---|---|
| Decisiva (à vista 2018-01 a 2021-09) | 181 | 19 | +2,68 | +1,03 | +2,40 | 33,7% |
| Confirmação (USD-M 2022-01 a 2026-09) | 411 | 25 | +0,39 | -0,22 | +0,31 | 31,1% |

Por ano, na janela decisiva: 2018 sem trades (o filtro ficou desligado quase o
ano todo), 2019 com 41 trades a -0,08 R, 2020 com 78 a +3,49 R e 2021 com 62 a
+3,48 R. Na confirmação: 2022 sem trades, 2023 com 136 a +0,83 R, 2024 com 150
a +0,56 R, 2025 com 99 a -0,28 R e 2026 com 26 a -0,33 R.

As saídas por canal deram +6,73 R em média na janela decisiva (87 trades) e
+2,09 R na confirmação (189). Os stops deram cerca de -1,05 R nas duas.

**Leitura.** O critério pré-registrado foi cumprido e o veredito vale. Mesmo
assim, o ganho da janela decisiva vem todo de 2020 e 2021, e 2019 ficou
levemente negativo. Na confirmação, os dois anos mais recentes estão
negativos. A regra ganha muito em alta forte de mercado e perde um pouco fora
dela. O filtro do BTC protege das quedas longas (2018 e 2022 ficaram sem
trades), mas não das lateralizações. O viés de sobrevivência dos pares, já
listado no pré-registro, favorece este resultado.

## H2

| Janela | Trades | Pares | Média líquida (R) | Limite inferior 2,5% | Pior LOSO |
|---|---|---|---|---|---|
| Decisiva | 425 | 19 | +0,14 | -0,009 | +0,11 |
| Confirmação | 603 | 25 | +0,07 | -0,06 | +0,05 |

O limite inferior ficou logo abaixo de zero, por isso o veredito é FAIL. Por
ano, na janela decisiva: 2018 -0,25, 2019 +0,11, 2020 +0,27 e 2021 +0,27.

## Consequência

H1 passa a ser a candidata a substituir a B2 de duas pontas no modo sombra. O
passo seguinte é pré-registrar um teste prospectivo próprio para H1, porque o
da B2 (B2_PROSPECTIVE_V1) mede outra regra. Nada disto libera LIVE.
