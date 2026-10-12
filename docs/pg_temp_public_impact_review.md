# NEXUS-7 — impacto de TEMP FROM PUBLIC antes de qualquer revogação

## Estado observado em produção (12/10/2026, UTC)

**Somente observações verificadas:** PRs [#645](https://github.com/007andra/nexus7-bot/pull/645),
[#646](https://github.com/007andra/nexus7-bot/pull/646) e
[#647](https://github.com/007andra/nexus7-bot/pull/647) integrados;
Railway deploy de restauração `fd2a67c2-4395-44b3-9cb8-6c4d4192472d`
SUCCESS, bot e PostgreSQL online (1/1).

A auditoria em **credencial diagnóstica**, não a do motor, registrou:

```text
[PG_TEMP_ORIGIN] status=RESOLVED public=true direct=false inherited=false owner=false default_acl=false potential_revocation_scope=DATABASE_WIDE other_login_roles_public_dependent=NONE decision_effect=NONE execution_effect=NONE
[PG_ROLE_AUDIT_DB_PRIVILEGES] check=CREATE result=PASS
[PG_ROLE_AUDIT_DB_PRIVILEGES] check=TEMP result=FAIL
[PG_ROLE_AUDIT_DB_PRIVILEGES] check=OWNER result=PASS
READ_ONLY_ROLE_VERIFIED=false
```

Isso identifica **uma concessão efetiva de TEMP ao PUBLIC**, armazenada
em ACL não nulo, como motivo de reprovação do grupo 3. Não prova uso de
tabelas temporárias nem autoriza mudanças de permissões. `NONE` abrange
apenas outras roles LOGIN acessíveis pelo diagnóstico, não superusers ou
owners, com CONNECT e sem outra fonte de TEMP; **não captura uso por
sessões, extensões, comandos futuros ou serviços externos não declarados**.

## Escopo de análise das dependências

**Topologia Railway conhecida:** apenas serviços `nexus7-bot` e
`Postgres`, e uma réplica ativa de cada na produção; isso não exclui
clientes externos, backups, ferramentas administrativas ou sessões.

A revisão dirigida do código da branch de produção
`migration/binance-usdm` observou:

| Caminho inspecionado | Uso PostgreSQL visível |
|---|---|
| `bot/database.py` | `asyncpg`, tabelas permanentes, índices, alterações de colunas e operações de negócio |
| `bot/runtime_hardening.py` | Bootstrap idempotente de tabelas/índices e migração de colunas |
| `bot/nexus_persistence.py` | Histórico e índices permanentes |
| `bot/atomic_key_value.py` | Transação durável CAS, advisory locks e `INSERT ... ON CONFLICT` |
| `bot/confidence_outcome_linking.py` | Colunas permanentes de evidência e `UPDATE` |
| `bot/oos_research_reader_v2.py` | Cliente diagnóstico dedicado, SELECT-only e default read-only, **desligado** |

Nos arquivos acima **não foi encontrada SQL de criação de tabelas
temporárias**. Essa é uma inspeção dirigida, não uma varredura verificável
de todas as entradas SQL possíveis nem uma prova dinâmica de inexistência
de dependências no banco real.

## Teste de compatibilidade com revogação (isolado)

`tests/test_pg_temp_public_revoke_compatibility_postgres.py` usa
**exclusivamente** o PostgreSQL de CI com DSN rigidamente fixada para
`postgresql://proof:postgres@127.0.0.1:5432/nexus_release_proof`.
Em cada caso cria banco e login aleatórios/descartáveis, revoga
`TEMPORARY` de `PUBLIC` **somente no banco descartável**, executa o
bootstrap real `install_database_schema_fix` duas vezes, DDL/indexes
do `nexus_persistence` e leituras/gravações representativas
`key_value` com upsert. Verifica que `CREATE TEMPORARY TABLE` é
negado. Um segundo teste simula rollback *no banco de teste*, recuperando
o privilégio com `GRANT TEMPORARY ... TO PUBLIC`.

**Não usa dados, roles, conexões, credenciais ou serviços Railway de
produção.** Não executa engine, ordens de Binance, reconciliação real,
nem garante compatibilidade de todos os jobs e serviços externos.

## Critério de GO/NO-GO antes de eventual correção na produção

1. Este PR de teste precisa ter **Quality Check + Supply Chain PASS**,
   incluindo teste real PostgreSQL (não apenas mocks).
2. Revisar a role de persistência do bot (sem revelar nomes/DSN) quanto
   a `TEMP`, `CREATE`, ownership e schemas; auditar jobs manuais,
   migrações e serviços externos. A lista de serviços Railway não é um
   inventário completo de consumidores da mesma instância PostgreSQL.
3. Confirmar recuperação/rollback de privilégios e impacto no ciclo de
   inicialização, idempotência do schema e CAS. A correção de ACL não
   deve ser combinada com ajustes em risco, HWM, alavancagem ou reader.
4. Se e somente se a revisão dinâmica confirmar que não há uso
   dependente, elaborar uma mudança pontual **e separadamente
   autorizada** para o banco e ambiente corretos:
   `REVOKE TEMPORARY ON DATABASE <DB_VERIFICADO> FROM PUBLIC`.
   O termo `<DB_VERIFICADO>` é um **placeholder**, não um nome a adivinhar.
   Preferir janela com controle operacional e rollback preparado.
5. Após uma alteração *futuramente autorizada*, rodar novamente os
   **13 checks** com o observador temporário e exigir
   `READ_ONLY_ROLE_VERIFIED=true`; conferir saúde dos serviços,
   lease/fencing, consultas e migrações; desabilitar o observador.
   Reverter mudança de ACL se ocorrer regressão.
6. Mesmo um PASS de role **não libera** `OOS_RESEARCH_READER_V2`
   nem LIVE. Permanecem pendentes OOS e capital confirmado da Binance.
   Não modificar `DRAWDOWN_HARD_GATE`, equity ou HWM artificialmente.

## Decisão no presente marco

**NO-GO para revogação em produção por ora.**
A evidência de laboratório e inspeção do repositório pode reduzir
incerteza, mas não substitui a verificação operacional de outras
conexões e da role do motor. Nenhuma ACL foi alterada por este PR.
