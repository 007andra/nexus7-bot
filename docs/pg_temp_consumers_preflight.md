# NEXUS-7 — Inventário agregado de consumidores TEMP (Issue #649)

## Escopo

Os PRs #645–#648 estabeleceram que a credencial
`OOS_DIAGNOSTIC_DATABASE_URL` tem permissão PostgreSQL `TEMP` via
`PUBLIC`; `READ_ONLY_ROLE_VERIFIED=false`. Testes de compatibilidade
no PostgreSQL **descartável** aprovaram os caminhos selecionados com
`TEMP` revogado, mas isso **não autoriza** a mesma revogação em produção.

O próximo ponto decisivo é coletar evidência do banco real sobre **roles
que potencialmente utilizam TEMP**, sessões e estatísticas observáveis.
Este PR prepara a leitura; **não a executa na produção**.

### Integração mínima, sem novo serviço nem nova flag

O módulo `scripts/pg_temp_consumer_inventory.py` é invocado pelo
`scripts/railway_pg_readonly_audit_once.py` somente após o relatório
original com 13 checks completo **E** `check=TEMP result=FAIL`.
Assim, é executado apenas sob a flag pré-existente, opt-in e desligada
por padrão `PG_ROLE_AUDIT_STARTUP_ONCE=true`. A flag fica `false`
na configuração operacional e exigirá autorização explícita para
ativação temporária. Nada é executado no start ou deploy normal.

Usa somente `OOS_DIAGNOSTIC_DATABASE_URL`, conexão separada daquela
de persistência do trading e `conn.transaction(readonly=True)`. Sem
`GRANT`, `REVOKE`, `ALTER`, `UPDATE`, ordens à exchange, rede
externa ou tabelas temporárias. Duas amostras de estatísticas
`pg_stat_database` são separadas por aproximadamente 2 segundos,
no máximo 8 segundos para toda a coleta; não são um monitoramento
prolongado.

### Logs sanitizados

```text
[PG_TEMP_CONSUMER_INVENTORY] status=OBSERVED roles_login=NONE|ONE|MULTIPLE roles_connect=NONE|ONE|MULTIPLE roles_temp=NONE|ONE|MULTIPLE roles_super=NONE|ONE|MULTIPLE sessions=NONE|ONE|MULTIPLE sessions_active=NONE|ONE|MULTIPLE sessions_idle_tx=NONE|ONE|MULTIPLE session_role_diversity=NONE|ONE|MULTIPLE session_visibility=LIMITED|FULL_STATS_ROLE temp_files_delta=NONE|ONE|MULTIPLE temp_bytes_delta=NONE|ONE|MULTIPLE sample_window=SHORT_2S complete_dependency_proof=false read_only=true decision_effect=NONE execution_effect=NONE
```

Qualquer falha, inconsistência de contadores ou reset de estatísticas:
`[PG_TEMP_CONSUMER_INVENTORY] status=INCONCLUSIVE ...`.

As consultas **não retornam** `query`, `client_addr`,
`application_name`, nome de role, nome de banco, DSN ou ACL.
Os logs usam apenas faixas `NONE/ONE/MULTIPLE`. Valores originais
não são impressos; erros de driver não são interpolados.

`session_visibility=LIMITED` significa que a role não possui
`pg_read_all_stats` nem superuser e **não pode certificar** o
conteúdo de sessões de terceiros. Mesmo com `FULL_STATS_ROLE`,
duas amostras não capturam jobs fora da janela, apps externos,
comandos futuros, scripts administrativos e a necessidade de TEMP
por recursos não exercitados.

`temp_files_delta` e `temp_bytes_delta` se referem a arquivos
temporários de execução (sort/hash etc.), **não** exclusivamente à
criação de tabelas temporárias. `NONE` não prova ausência de uso de
tabelas TEMP. Também não é seguro inferir ausência de clientes externos
apenas porque o Railway não expõe proxy TCP.

### Contrato de autoridade

O inventário suplementar **nunca** altera o `status` nem
`READ_ONLY_ROLE_VERIFIED` do auditor completo. Uma auditoria geral
`FAIL` permanece `FAIL`. O dado é `OBSERVED`, não `PASS`
de dependências, e nenhuma saída autoriza `REVOKE` automático,
`OOS_RESEARCH_READER_V2` ou LIVE.

## Plano condicionado a nova autorização

1. Concluir Quality Check e Supply Chain Security; revisar PR e
   autorizar separadamente merge/deploy, sem tocar permissões.
2. Após deployment estável e com consentimento, ativar
   `PG_ROLE_AUDIT_STARTUP_ONCE=true` uma vez, ler
   `PG_TEMP_CONSUMER_INVENTORY`, `PG_TEMP_ORIGIN` e grupo 3.
3. Restaurar `PG_ROLE_AUDIT_STARTUP_ONCE=false`; comprovar
   deployment online, fencing, proteção de entradas e ausência de
   mudança em ACL/DSN.
4. Se `session_visibility=LIMITED`, não pedir privilégios extras
   automaticamente. Propor uma verificação administrativa
   separada, opcional, com resultados agregados sem segredos.
5. Exigir inventário completo de consumidores e plano de
   rollback com baseline de ACL antes de **qualquer** revogação.
   Considerar também pendência de backup/restore na issue #625.
6. A revisão sobre eventual revogação fica registrada na
   [issue #649](https://github.com/007andra/nexus7-bot/issues/649).
   Nenhuma autorização para `GRANT/REVOKE/ALTER` é presumida.

Status atual: **NO-GO para revogar TEMP de PUBLIC na produção**.
