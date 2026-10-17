# H1_PROSPECTIVE_V1: pré-registro

Escrito e congelado em 2026-10-17, antes de existir qualquer dado da janela
avaliada. Os parâmetros são os do dicionário `PREREG` em
`bot/h1_prospective_v1.py`, com
`prereg_sha256 = d2f00043f7f288f4f77fb83289714f5f486d806aa17d755f92bfdb265097195d`.

## Por que existe

A H1 passou no TREND_LONG_V1 (H1_PASS), mas o ganho veio quase todo de 2020 e
2021, e na janela de confirmação os anos de 2025 e 2026 ficaram negativos. Não
dá para saber pelo histórico se a regra ainda funciona no mercado de agora. A
forma honesta de responder é testar a mesma regra em meses que ainda não
aconteceram quando ela foi congelada.

## Regra testada

É a H1 do TREND_LONG_V1, sem nenhuma mudança:

- **Filtro:** o fechamento do BTCUSDT no dia do sinal precisa estar acima da
  média simples dos últimos 200 fechamentos.
- **Entrada:** compra na abertura seguinte a um fechamento acima da máxima dos
  20 dias anteriores. Não há venda a descoberto.
- **Stop:** 2 × ATR(20) abaixo do preenchimento.
- **Saída:** na abertura seguinte a um fechamento abaixo da mínima dos 10 dias
  anteriores, ou no stop.
- **Pares e custos:** os mesmos 25 pares, taxa de 0,06% por lado, slippage
  estática por par e funding real. Os dados são os candles diários do USD-M.

## Janela e leituras

| Item | Valor |
|---|---|
| Aquecimento | 2026-01 a 2026-10, só para a média de 200 dias e os canais |
| Primeiro mês avaliado | 2026-11 |
| Leitura intermediária | com 2026-11 a 2027-10 publicados (12 meses) |
| Leitura final | com 2026-11 a 2028-10 publicados (24 meses) |

Os trades contam pelo mês de entrada. Uma posição ainda aberta no fim dos dados
publicados não entra na conta. Isso é conservador para a H1, porque os trades
que mais ganham são os que ficam mais tempo abertos.

O estudo roda junto com o B2_PROSPECTIVE_V1, a cada trimestre, sobre todos os
meses já publicados. Antes da leitura intermediária, o relatório diz só
`NOT_YET` e mostra números descritivos, que não autorizam nada.

## Critério

O cluster do bootstrap é o mês UTC, com 10.000 réplicas e semente 20261117. O
mínimo é de 100 trades em pelo menos 10 pares. O mínimo é menor que o da B2
porque a H1 opera menos: só compra e fica parada quando o BTC está abaixo da
média.

**Leitura intermediária (12 meses):**

- **FAIL definitivo** se houver trades e a média líquida for menor ou igual a
  zero.
- **PASS** se passar o mínimo, o limite inferior de **1%** do bootstrap for
  maior que zero e todos os leave-one-symbol-out forem positivos.
- **CONTINUE** em qualquer outro caso, inclusive com zero trades.

**Leitura final (24 meses), só se a intermediária deu CONTINUE:**

- **PASS** se passar o mínimo, o limite inferior de 5% for maior que zero e
  todos os leave-one-symbol-out forem positivos.
- **INSUFFICIENT_EVIDENCE** abaixo do mínimo de trades ou pares.
- **FAIL** em qualquer outro caso.

## O que cada resultado significa

- **PASS:** é o sinal para construir a execução real da H1, com capital
  compatível com o stop (cerca de 100 USDT para 1% de risco), autorização
  explícita do operador e a liberação da permissão de segurança.
- **FAIL:** a H1 é encerrada e o modo sombra dela pode ser desligado.
- **INSUFFICIENT_EVIDENCE:** a amostra não basta, e ampliar o teste exige um
  pré-registro novo. Isso é provável se o BTC passar muito tempo abaixo da
  média de 200 dias.

## Relação com o modo sombra

O modo sombra (`STRATEGY_H1_SHADOW`) registra em produção o que a H1 faria.
Ele usa a chave `strategy:h1:shadow:state:v1` e substitui o modo sombra da B2
de duas pontas, que é desligado. O estado da B2 fica guardado. O modo sombra
serve para conferir que o bot executa a mesma regra da simulação, e não como
fonte do veredito. A fonte é a simulação sobre os arquivos públicos.

## Limitações

- **Último preço no lugar do preço de marcação**, com slippage estática.
- **Simulação por par**, sem limite de exposição total. Com o filtro do BTC, as
  entradas tendem a vir juntas, e o risco real somado pode passar de 1% por vez.
- **Mínimo de ordem da Binance ignorado.**
- **Um mês sem arquivo de funding** não adiciona funding. O relatório lista
  esses casos.
- **Não autoriza nada em produção por conta própria.**
