> **HISTORICAL** — kept as a record. It does not describe the current Binance USD-M runtime; see `README.md` and `docs/audit/`.

# Auditoria de Meta-Processo — Confiabilidade das Alegações

Este documento audita **como as afirmações sobre o bot foram feitas**,
não o código em si. Existe porque um erro real ocorreu e precisa ficar
registrado, junto com o mecanismo que previne a repetição.

---

## Caso registrado: alegação prematura de resolução definitiva

**Commit:** `453b101` (2026-08-28T20:24:06Z)
**Mensagem:** *"fix(kucoin): URL completa no _get — resolve 400004 definitivo"*

**O que realmente aconteceu**, reconstruído pelos timestamps dos
commits subsequentes (não pela memória de quem escreveu):

| Timestamp | Commit | Evento |
|---|---|---|
| 20:24:06 | `453b101` | Alega resolução **definitiva** |
| 20:32:47 | `f726860` | Erro `400004` volta a ocorrer |
| 20:35:15 | `cd74b04` | `400005` alternando com `400004`, ainda ativo |
| 20:43:35 | `85e4698` | Autenticação de fato resolvida |

**Gap entre a alegação e a resolução real: 19 minutos, 3 causas raízes
distintas** (assinatura de URL → suporte a API v1/v2 → sincronização de
relógio).

### Classificação

**Excesso de confiança, não fabricação deliberada.** Cada commit
subsequente mostra investigação genuína de uma causa nova — não é o
mesmo erro sendo re-explicado sem investigar. Mas a palavra
"definitivo" foi usada **antes de qualquer teste contra a KuCoin
real**, num ambiente que nunca teve acesso de rede à exchange. Isso
torna a palavra estruturalmente injustificável, independente de o
diagnóstico técnico estar certo ou errado.

### Correção estrutural

Daqui em diante, mensagens de commit e respostas não usam "definitivo",
"resolvido" ou equivalentes para bugs de integração externa **sem**
uma das duas condições:
1. Execução real confirmada contra o sistema externo, ou
2. Qualificação explícita do nível de confiança (ex: "corrige a causa
   identificada; não testado contra produção").

---

## Caso registrado: evidência externa mal classificada

**Contexto:** o usuário enviou um print de tela da KuCoin mostrando uma
posição ETHUSDT aberta, com o preço de liquidação real visível.

**O erro:** a análise técnica do preço de liquidação estava correta
(validada contra a fórmula oficial, diferença de 0,097%), mas a
imagem foi apresentada como *"primeira evidência do bot operando em
produção"*.

**O que a evidência realmente mostrava:** a tela de trading **manual**
da KuCoin (botão "Comprar/Long" visível na própria imagem). Nada ali
provava que a ordem foi aberta pelo bot em vez de manualmente pelo
usuário.

### Correção estrutural

Antes de aceitar qualquer evidência externa como prova de que o bot
executou uma ação, checar explicitamente:

- [ ] A evidência mostra o CAMINHO DE EXECUÇÃO do bot (logs, `orderId`
      correlacionado, `clientOid` identificável) — ou só o ESTADO
      resultante (uma posição existe, sem indicar quem a abriu)?
- [ ] Existe uma marca de autoria technicamente verificável, ou a
      conclusão depende de inferência/coincidência de horário?
- [ ] O que essa evidência **não prova**, mesmo sendo genuína?

Correção de código associada (`9f4941e`): o `clientOid` gerado pelo
bot agora tem o prefixo `bgx7-`, tornando qualquer ordem sua
identificável diretamente na interface da KuCoin — fechando a lacuna
que tornou o erro de classificação possível em primeiro lugar.

---

## O que NÃO foi encontrado nesta auditoria

Buscados em todos os 366 commits do repositório:

- Nenhuma alegação de "pronto para produção" ou "certificado"
- Nenhum caso de reciclagem da mesma explicação sem nova investigação
  genuína
- 7 commits reconhecem explicitamente limitação de ambiente ou
  ausência de validação — o padrão oposto ao problema, presente e
  mais frequente que os casos de erro

Isso não significa que não haja outros casos não capturados pelos
padrões de busca usados. Este documento é auditável e deve ser
expandido se novos casos forem identificados.
