# NEXUS-7 — origem da permissão PostgreSQL TEMP (diagnóstico sem mutações)

## Por que existe este diagnóstico

A auditoria efetivamente executada no Railway em 12/10/2026 comprovou:

- `[PG_ROLE_AUDIT_DB_PRIVILEGES] check=CREATE result=PASS`
- `[PG_ROLE_AUDIT_DB_PRIVILEGES] check=TEMP result=FAIL`
- `[PG_ROLE_AUDIT_DB_PRIVILEGES] check=OWNER result=PASS`
- `READ_ONLY_ROLE_VERIFIED=false`

Não foi determinado ainda **quem concedeu** `TEMP` nem a possível
abrangência de uma revogação. Um PostgreSQL padrão frequentemente concede
`TEMPORARY` aos usuários via `PUBLIC` no ACL do banco; **isso é uma
hipótese, não uma observação confirmada para este banco**.

O novo módulo `scripts/pg_temp_grant_origin.py` executa uma única
consulta estática, sem parâmetros externos, utilizando exclusivamente
`OOS_DIAGNOSTIC_DATABASE_URL` via o executor de auditoria já existente.
A consulta emprega `pg_database`, `pg_roles`,
`aclexplode(COALESCE(datacl, acldefault('d', datdba)))`,
`has_database_privilege` e `pg_has_role(..., 'USAGE')`.
Ela roda em **transação READ ONLY própria**, depois da auditoria original,
**somente se o relatório completo reprovar TEMP**.

## Evidência permitida (sem identidades)

```text
[PG_TEMP_ORIGIN] status=RESOLVED public=true|false direct=true|false inherited=true|false owner=true|false default_acl=true|false potential_revocation_scope=DATABASE_WIDE|ROLE_REVIEW_REQUIRED other_login_roles_public_dependent=NONE|ONE|MULTIPLE decision_effect=NONE execution_effect=NONE
```

Se a investigação falhar ou seus resultados divergirem da auditoria
original, a saída será `[PG_TEMP_ORIGIN] status=INCONCLUSIVE`.
O resultado principal permanece **FAIL**; não ocorre qualquer
`READ_ONLY_ROLE_VERIFIED=true` em consequência desse diagnóstico.

- `public`: `TEMP` aparece no ACL do banco para o pseudo-grupo
  PostgreSQL `PUBLIC` (grantee OID 0).
- `direct`: há concessão direta de `TEMP` à role diagnóstica.
- `inherited`: existe concessão a outra role com caminho efetivo
  `pg_has_role(..., 'USAGE')` até a credencial diagnóstica.
- `owner`: a credencial é proprietária do banco.
- `default_acl`: o ACL armazenado é nulo, então PostgreSQL utiliza o
  ACL padrão calculado internamente.
- `other_login_roles_public_dependent`: banda `NONE/ONE/MULTIPLE`
  para **outras roles LOGIN** com `CONNECT`, não superusuárias, nem donas,
  que *potencialmente* perderiam TEMP se uma concessão a PUBLIC fosse
  removida porque não foi identificado caminho alternativo via concessões
  diretas/herdadas. **Não comprova que essas roles efetivamente usem
  tabelas temporárias**.
- Se `public=true`, o alcance da alteração `REVOKE TEMP ... FROM PUBLIC`
  é no nível do banco e exige revisão de dependências de outros serviços.
  Se `public=false`, `ROLE_REVIEW_REQUIRED` **não autoriza** revogar
  uma concessão individual sem avaliar as dependências.

O log nunca imprime nome de usuário, banco, schema, tabelas, DSN, senha,
SQL retornado, número de roles, mensagens de erro ou evidência bruta.
Apenas booleanos derivados e a banda de impacto são exibidos.

## Plano de correção condicionada ao resultado

**Somente proposta — este PR NÃO altera permissões nem deploy de produção.**

| Resultado apurado | Correção candidata, sempre com autorização futura |
|---|---|
| Somente `direct=true` | Avaliar revogação de TEMP **apenas** da role diagnóstica, após validar outros usos dessa role |
| `public=true` | **Não revogar automaticamente TEMP de PUBLIC**. Abrange outras roles do banco; auditar consumidores, dependências e alternativas (por exemplo credencial e banco de leitura segregados ou migração planejada da política global) |
| `inherited=true` | Investigar a role concedente e a árvore de membros; avaliar alteração mínima no caminho diagnóstico, sem alterar roles do motor |
| `owner=true` | Examinar titularidade e migração controlada; não transferir propriedade sem plano de rollback |
| Múltiplas origens | Corrigir todos os caminhos relevantes antes de repetir o check; nunca assumir que remover um só resolve |
| `INCONCLUSIVE` | Não alterar privilégios; corrigir evidência e repetir investigação |

**Aviso:** não existe um `DENY TEMP` por usuário que vença um `GRANT
TEMP TO PUBLIC` no mesmo banco. Por isso revogar TEMP somente de uma
role, quando PUBLIC ainda a concede, é ineficaz; a solução exige isolamento
ou análise do impacto de mudar o ACL global do banco.

## Próximo marco operacional (precisa de autorização separada)

1. Exigir Quality Check e Supply Chain Security aprovados; revisar o PR.
2. Autorizar separadamente merge e deployment do código (sem tocar ACLs).
3. Sob janela controlada, habilitar `PG_ROLE_AUDIT_STARTUP_ONCE=true` no
   serviço `nexus7-bot`, acompanhar uma execução e registrar
   `PG_TEMP_ORIGIN` e o estado geral da auditoria.
4. Desabilitar novamente com `PG_ROLE_AUDIT_STARTUP_ONCE=false`,
   confirmar novo deploy saudável, lease/fencing válidos e nenhum
   `READ_ONLY_ROLE_VERIFIED=true` indevido.
5. Preparar proposta baseada no resultado, sem `GRANT/REVOKE/ALTER`,
   sem mudar credenciais, sem expor banco à internet e sem ligar
   `OOS_RESEARCH_READER_V2` ou permitir ordens reais.

O gate `DRAWDOWN_HARD_GATE`, o HWM, a alavancagem, o sizing e os
mecanismos de proteção da Binance ficam totalmente fora do escopo.
