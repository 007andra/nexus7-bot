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
