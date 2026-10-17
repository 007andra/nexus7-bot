# NEXUS-7 — auditar credencial PostgreSQL via logs normais de startup

Este PR **somente prepara** um observador opcional. Não autoriza merge,
deploy, alteração de variável, lançamento de ordem ou ativação do leitor OOS.

## Contrato de isolamento

- `PG_ROLE_AUDIT_STARTUP_ONCE` ausente (padrão) ou qualquer valor diferente
  do literal `true`: **nenhuma task e nenhuma conexão diagnóstica**.
- Com a flag `true`, o lifespan **agenda**, sem aguardar, uma única task
  por processo. Ela espera o bootstrap normal terminar (até 120 segundos)
  e só então executa a auditoria de privilégios. `/health` não espera
  a task, e ela não participa do ciclo de engine, readiness ou dispatch.
- O código existente de `scripts.railway_pg_readonly_audit_once.execute`
  obtém exclusivamente `OOS_DIAGNOSTIC_DATABASE_URL`, abre uma conexão
  asyncpg independente, usa uma transação PostgreSQL `READ ONLY` e
  verifica os mesmos oito grupos de controles do PR #643.
- A auditoria consulta catálogos e permissões; não utiliza a conexão do
  bot, não grava no PostgreSQL, não chama a Binance, não emite ordens,
  não executa GRANT/REVOKE/SET ROLE nem modifica HWM/drawdown.
- Os registros são limitados a número do controle, PASS/FAIL/INCONCLUSIVE,
  um motivo codificado e `READ_ONLY_ROLE_VERIFIED=true|false`. Nenhuma
  credencial, DSN, role, objeto de banco ou mensagem de exceção é exibida.
- Resultados FAIL/INCONCLUSIVE jamais se convertem em PASS. Cancelamento
  durante desligamento é tratado como INCONCLUSIVE e não cancela o bootstrap.
- **A saída é evidência, não uma configuração.** O observador não ativa nem
  altera a variável `READ_ONLY_ROLE_VERIFIED` do Railway.
- A flag ativada repetirá a auditoria **uma vez por inicialização do processo**;
  deve ser desativada logo após coleta, sob autorização operacional.

## Após CI e nova autorização explícita

1. Confirmar que o merge SHA foi implantado com sucesso e que os gates
   `DRAWDOWN_HARD_GATE` e `OOS_RESEARCH_READER_V2` seguem inalterados.
2. Definir temporariamente apenas `PG_ROLE_AUDIT_STARTUP_ONCE=true`
   no serviço `nexus7-bot`. Não editar DSNs, privilégios, flags OOS,
   credenciais de exchange, políticas de drawdown nem outras variáveis.
3. Aguardar novo deploy saudável e consultar os **logs normais da aba Deploy**
   por `[PG_ROLE_AUDIT_STARTUP]`, `[PG_ROLE_AUDIT_ONCE]` e
   `READ_ONLY_ROLE_VERIFIED=`. Confirmar os oito grupos e a decisão final.
   Se a tarefa não executar ou a evidência estiver incompleta: INCONCLUSIVE.
4. Remover a flag e confirmar um novo deploy saudável, task desabilitada,
   ownership/fencing íntegros e bloqueios LIVE preservados.
5. Em PASS, registrar a evidência sanitizada para **revisão manual**
   independente; não habilitar o leitor OOS automaticamente. Em FAIL,
   corrigir privilégios por processo separado. Jamais contornar o gate.

Condição adicional para LIVE: reconciliação real do capital Binance,
tratamento do drawdown e evidência OOS aprovada. A auditoria do PostgreSQL
não libera negociação real nem comprova rentabilidade.

## Identificação estrita do grupo 3 (novo PR de evidência)

A execução real anterior comprovou `[PG_ROLE_AUDIT_ONCE] item=3 result=FAIL`,
enquanto os grupos 1, 2 e 4–8 passaram. Isso **não informa qual
permissão** dentro do grupo 3 violou a política.

O auditor agora emite **apenas quando o relatório tem os 13 resultados
esperados e as três identificações válidas**:

```text
[PG_ROLE_AUDIT_DB_PRIVILEGES] check=CREATE result=PASS|FAIL
[PG_ROLE_AUDIT_DB_PRIVILEGES] check=TEMP result=PASS|FAIL
[PG_ROLE_AUDIT_DB_PRIVILEGES] check=OWNER result=PASS|FAIL
```

Significado das verificações (todas medidas sobre a **credencial
`OOS_DIAGNOSTIC_DATABASE_URL` e o banco ao qual ela se conecta**):

- `CREATE`: pode criar schemas ou outras estruturas no nível do banco.
  A política exige ausência dessa capacidade.
- `TEMP`: pode criar tabelas temporárias. A política exige ausência,
  mesmo quando o PostgreSQL a concede amplamente via `PUBLIC`.
- `OWNER`: a sessão possui o próprio banco; donos têm controle
  administrativo. A política exige que **não** seja dona dele.

`result=PASS` significa que a **capacidade perigosa não foi encontrada**.
`result=FAIL` significa que essa capacidade estava presente. São
identificadores controlados em código, sem interpolar nome de role,
database, DSN, nomes de objeto ou mensagens de erro. Em caso de
duplicação, omissão ou rótulo desconhecido, o relatório é
`INCONCLUSIVE` e `READ_ONLY_ROLE_VERIFIED=false`.

### Plano condicionado a aprovação futura

1. Concluir Quality/Supply Chain CI do PR e solicitar autorização
   independente para merge e deployment. Nenhuma variável é alterada
   neste PR.
2. Após deployment saudável, ativar **temporariamente** somente a flag
   `PG_ROLE_AUDIT_STARTUP_ONCE=true`, coletar os três resultados dos
   logs normais e restaurar a flag para `false`.
3. Inspecionar o `FAIL` concreto e avaliar quem depende da
   permissão, **antes de propor qualquer GRANT/REVOKE ou mudança de
   titularidade**. Nunca usar a conexão/role de produção do motor de
   trading como substituta da credencial de diagnóstico.
4. Só após corrigir as permissões por um procedimento explicitamente
   autorizado e reexecutar a auditoria com todos os grupos `PASS`,
   prosseguir com revisão independente do leitor OOS.
5. Mesmo com auditoria `PASS`, não alterar autorização LIVE, hard
   drawdown, capital, HWM ou permissões de ordem.

**Nota:** este diagnóstico já lê as decisões calculadas pela auditoria
existente. Não executa novas consultas SQL, não consulta dados de
mercado e não modifica o banco.
