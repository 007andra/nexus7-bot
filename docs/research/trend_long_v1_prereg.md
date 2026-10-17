# TREND_LONG_V1: pré-registro

Escrito e congelado em 2026-10-17, antes de qualquer dado do mercado à vista de
2017-08 a 2021-09 ser lido por este ou por qualquer outro estudo desta série.
Os parâmetros são os do dicionário `PREREG` em `bot/trend_long_study_v1.py`,
com
`prereg_sha256 = 47f2fe40e3877d54f6df14c44aa68b24af4414cccc874297d0c2c5d10e4a5db7`.

## Por que existe

Três estudos reprovaram day trade nesses pares. A B2 (rompimento de canal
diário) teve média positiva sem significância, e quase todo o ganho veio do
lado comprado. O operador não quer esperar o teste prospectivo de 2027. Este
estudo usa o único período histórico que nenhum estudo tocou, com duas
hipóteses só compradas, sob regras fixadas antes de ver os dados.

## Hipóteses

**H1: B2 só comprada com filtro do BTC.**

- **Filtro:** no dia do sinal, o fechamento do BTCUSDT precisa estar acima da
  média simples dos últimos 200 fechamentos.
- **Entrada:** compra na abertura seguinte a um fechamento acima da máxima dos
  20 dias anteriores.
- **Stop:** 2 × ATR(20).
- **Saída:** na abertura seguinte a um fechamento abaixo da mínima dos 10 dias
  anteriores, ou no stop.
- **Origem:** é um refinamento sugerido por dados já vistos (o lado comprado da
  B2). Por isso só o período intocado pode aprovar.

**H2: momentum semanal.**

- **Seleção:** toda segunda-feira, na abertura UTC, compra os 3 pares com maior
  retorno positivo nos 28 dias anteriores, medido até o fechamento de domingo.
- **Stop:** 2 × ATR(20).
- **Saída:** na abertura da segunda seguinte, ou no stop.
- **Custo:** cada semana é uma ida e volta completa, mesmo que o par volte a
  ser escolhido. Isso é conservador.

## Dados e custos

| Janela | Fonte | Meses | Uso |
|---|---|---|---|
| Decisiva | Binance à vista, candles diários | 2018-01 a 2021-09 (aquecimento em 2017-08 a 2017-12) | decide; nunca lida |
| Confirmação | Binance USD-M, candles diários | 2022-01 a 2026-09 (aquecimento em 2021-04 a 2021-12) | só pode vetar; já vista por outros estudos |

**Pares:** são os 25 pares de sempre. Cada um entra no primeiro mês publicado.
Em 2018, só uma parte deles existia no mercado à vista.

**Custos:**

- **Taxa:** 0,06% por lado.
- **Slippage:** estática por par.
- **Funding na janela decisiva:** o mercado à vista não tem funding, então o
  estudo cobra 0,02% contra a posição a cada 8 horas. É um valor conservador.
- **Funding na janela de confirmação:** real.

**Medida:** o R usa a distância planejada até o stop sobre o preenchimento.
Uma posição ainda aberta no fim dos dados não conta.

## Critério

Vale para cada hipótese separadamente, com taxa de 0,06%. São duas condições.

**1. Janela decisiva:**

- pelo menos 150 trades em 8 pares;
- média líquida maior que 0;
- limite inferior do bootstrap maior que 0. O bootstrap usa cluster mensal,
  10.000 réplicas e semente 20261017. O quantil é de **2,5%**, porque são duas
  hipóteses (correção de Bonferroni sobre 5%).
- média maior que 0 em todos os leave-one-symbol-out.

**2. Janela de confirmação:** média líquida maior que 0.

PASS exige as duas condições. Abaixo do mínimo de trades ou pares na janela
decisiva, o resultado é INSUFFICIENT_EVIDENCE.

## O que cada resultado significa

- **PASS:** é o melhor indício que conseguimos sem esperar o teste prospectivo.
  Mesmo assim, não é prova. O próximo passo seria rodar a regra em modo sombra
  e, por decisão do operador, com pouco dinheiro. A execução real também
  depende da liberação da permissão que hoje bloqueia as exceções de segurança.
- **FAIL nas duas:** nenhuma regra só comprada simples deste tipo se sustenta
  nos dados disponíveis.

## Limitações

- **Mercado à vista em vez de futuros na janela decisiva.** O mercado à vista
  não tem funding nem alavancagem, e por isso o estudo usa um funding assumido.
  O spread e a liquidez de 2018 também eram piores, e a slippage estática pode
  subestimar esse custo.
- **2020-2021 foi uma alta excepcional.** A queda de 2018 e a lateralização de
  2019 equilibram isso em parte. A tabela por ano no relatório mostra se o
  resultado depende só de 2020-2021.
- **Viés de sobrevivência.** Os pares foram escolhidos em 2025 e 2026. Pares
  que morreram em 2018 ficam de fora, o que favorece estratégias compradas.
- **H1 nasceu de dados vistos.** A janela de confirmação não é independente
  para ela.
- **Não autoriza nada em produção.**
