# TREND_BREAKOUT_V1: resultado

O estudo rodou no GitHub Actions, no PR #681 (job 116997734651), com os
parâmetros do pré-registro `docs/research/trend_breakout_v1_prereg.md` e
`prereg_sha256 = 2ee7034279a2c4bd842a7b4af0c82f8e84abdd9463b00bd4db4ad24d3e39d68b`.

Os dados vieram completos. Os 25 pares têm klines e funding em todos os meses
publicados. Nenhum funding precisou ser assumido e nenhum mês faltou depois da
listagem. Sete pares têm um único dia incompleto cada.

## Vereditos: B1_FAIL e B2_FAIL

| Hipótese | Trades | R líquido médio | Limite inferior 95% | Pior LOSO | Profit factor | Acerto |
|---|---|---|---|---|---|---|
| B1 (day trade, k = 0,5) | 8.588 em 25 pares | −0,149 | −0,210 | −0,154 | 0,75 | 35,0% |
| B2 (swing, 20/10) | 644 em 25 pares | +0,203 | −0,104 | +0,166 | 1,36 | 32,3% |

A base do veredito é o holdout de 2022-01 a 2024-08, com a variante principal
e taxa de 0,06% por lado.

## B1: day trade

A B1 reprova sem ambiguidade:

- **Antes dos custos:** a perda já existe, com R bruto médio de −0,089.
- **Pares e meses:** todos os 25 pares são negativos, e 26 dos 32 meses também.
- **Direções:** comprado dá −0,12 R e vendido dá −0,17 R.
- **Variantes:** nenhuma salva o resultado. Com k = 0,3 a média é −0,128 R, e
  com k = 0,7 é −0,210 R.
- **Janela descritiva** (2024-09 a 2026-09): o padrão se repete, com −0,137 R e
  limite inferior de −0,215.

O stop mediano foi de 2,5%, então o custo deixou de ser o problema principal.
O problema é que o rompimento intradiário não continua com frequência
suficiente: 43% dos trades voltam até a abertura do dia e batem o stop.

## B2: swing

A B2 reprova pelo critério, mas não pelo mesmo motivo da B1. Ela é o primeiro
resultado positivo desta série:

- **Médias:** a média líquida é positiva (+0,20 R), e o leave-one-symbol-out é
  positivo em todos os casos. O ganho existe antes dos custos (+0,25 R bruto).
  Taxa e slippage pesam pouco, porque o stop mediano é de 12,2%. O funding
  custa 0,04 R por trade.
- **Por que reprova:** o limite inferior do bootstrap é negativo. O cluster é o
  mês, e com 32 meses o resultado depende de poucos deles: 2022-04 (+3,05 R),
  2023-01 (+4,02 R), 2023-11 (+3,48 R) e 2024-02 (+1,36 R). Esses quatro meses
  somam cerca de +197 R, e os outros 28 meses somam cerca de −66 R. É o perfil típico de seguir
  tendência, mas o critério exige que a vantagem seja distinguível do acaso, e
  não foi.
- **Direção:** quase todo o ganho vem do lado comprado (+0,40 R). O lado
  vendido fica em −0,01 R.
- **Variante 55/20:** média de +0,40 R, mas com limite inferior de −0,256.
- **Janela descritiva:** a média continua positiva (+0,11 R na 20/10 e
  +0,22 R na 55/20), também com limite inferior negativo.
- **Prazo:** a permanência mediana é de 14 dias. Não é day trade.

## Leitura

A hipótese de day trade está descartada nesta forma. A B2 sugere que existe
alguma vantagem em seguir tendência no diário, mas a amostra não basta para
separá-la do acaso. O resultado também não autoriza afrouxar o critério depois
de visto. O próximo passo honesto seria um pré-registro novo e prospectivo
para a B2, que levaria muitos meses para juntar clusters suficientes.

Com stop de 12% e risco de 1% por trade, a posição é cerca de 8% do capital.
Para atingir o mínimo de 5 USDT por ordem, a conta precisaria de uns 60 USDT, e
bem mais para pares com mínimo maior.

Isso não autoriza nem bloqueia nada em produção por conta própria, e o LIVE
continua bloqueado pelos gates existentes.

## Limitações

As limitações do pré-registro continuam valendo: viés de sobrevivência,
período conhecido, último preço no lugar do preço de marcação, slippage
estática, simulação por par sem limite de exposição e mínimo de ordem
ignorado.
