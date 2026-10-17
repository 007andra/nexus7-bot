# B2_PROSPECTIVE_V1: pré-registro

Escrito e congelado em 2026-10-16, antes de existir qualquer dado da janela
avaliada. Os parâmetros são os do dicionário `PREREG` em
`bot/b2_prospective_v1.py`, com
`prereg_sha256 = 28838aeb485fa874fa4da5811b8d88a44c39b2a6be077eddf33d59bacc133bbd`.

## Por que existe

O TREND_BREAKOUT_V1 deu B2_FAIL, mas a B2 teve média positiva (+0,20 R no
holdout e +0,11 R na janela descritiva). Ela reprovou porque o ganho dependia de
poucos meses de tendência forte, e o limite inferior do bootstrap ficou
negativo. A única forma honesta de saber se há vantagem é testar a mesma regra
em meses que ainda não aconteceram quando a regra foi congelada.

## Regra testada

É a B2 principal do TREND_BREAKOUT_V1, sem nenhuma mudança:

- **Entrada:** na abertura do dia seguinte a um fechamento além do canal de 20
  dias.
- **Stop:** inicial a 2 × ATR(20) do preenchimento.
- **Saída:** na abertura seguinte a um fechamento além do canal oposto de 10
  dias.
- **Pares, custos e funding:** os mesmos 25 pares, a mesma taxa (0,06% por
  lado, com sensibilidade em 0,04% e 0,05%), a mesma slippage estática e o
  funding real.

## Janela e leituras

| Item | Valor |
|---|---|
| Aquecimento | 2026-08 a 2026-10, só histórico |
| Primeiro mês avaliado | 2026-11 |
| Leitura intermediária | com 2026-11 a 2027-10 publicados (12 meses) |
| Leitura final | com 2026-11 a 2028-10 publicados (24 meses) |

Os trades contam pelo mês de entrada. Uma posição ainda aberta no fim dos dados
publicados não entra na conta.

O estudo roda de novo a cada trimestre sobre todos os meses que a Binance já
publicou. Antes da leitura intermediária, o relatório diz só `NOT_YET` e mostra
números descritivos. Esses números não autorizam nada e não mudam o critério.

## Critério

O cluster do bootstrap é o mês UTC, com 10.000 réplicas e semente 20261116. O
mínimo é de 150 trades em pelo menos 10 pares.

**Leitura intermediária (12 meses):**

- **FAIL definitivo** se a média líquida for menor ou igual a zero (futilidade).
- **PASS** se passar o mínimo de trades, o limite inferior de **1%** do
  bootstrap for maior que zero e todos os leave-one-symbol-out forem
  positivos. O limite é mais rígido porque esta é a primeira de duas leituras.
- **CONTINUE** em qualquer outro caso.

**Leitura final (24 meses), só se a intermediária deu CONTINUE:**

- **PASS** se passar o mínimo de trades, o limite inferior de 5% for maior que
  zero e todos os leave-one-symbol-out forem positivos.
- **INSUFFICIENT_EVIDENCE** se não chegar ao mínimo de trades ou pares.
- **FAIL** em qualquer outro caso.

## O que cada resultado significa

- **PASS:** a vantagem apareceu em dados novos. Só então faz sentido terminar a
  execução real da B2 no bot, com capital compatível com o stop (cerca de 100
  USDT para 1% de risco) e com a autorização explícita do operador.
- **FAIL:** a B2 é encerrada. O modo sombra pode ser desligado.
- **INSUFFICIENT_EVIDENCE:** a amostra não basta. Ampliar o teste exige um
  pré-registro novo.

## Relação com o modo sombra

O modo sombra (`STRATEGY_B2_SHADOW`, ligado em 2026-10-16) registra em produção
o que a B2 faria. Ele serve para conferir que a implementação no bot bate com a
simulação, e não como fonte do veredito. A fonte é a simulação sobre os arquivos
públicos, que é reproduzível e verificada por SHA-256.

## Limitações

- **Último preço no lugar do preço de marcação**, com slippage estática.
- **Simulação por par**, sem limite de exposição total.
- **Mínimo de ordem da Binance ignorado.**
- **Pares escolhidos em 2025/2026.** Na janela nova isso pesa menos, mas um par
  pode ser deslistado no meio do caminho. Nesse caso os dados dele param no
  último mês publicado.
- **Não autoriza nada em produção por conta própria.**
