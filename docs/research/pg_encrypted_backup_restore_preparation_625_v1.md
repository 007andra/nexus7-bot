# NEXUS-7 / #625 — Backup PostgreSQL criptografado e teste de restauração

**ESTADO: PREPARAÇÃO OFFLINE, NÃO EXECUTAR NO BANCO REAL.**

Este material atende à autorização para **preparar**, não para extrair,
copiar, restaurar ou publicar dados financeiros. Repositório base:
`007andra/nexus7-bot`, branch de produção `migration/binance-usdm`.
Nenhum dado real do Railway/Binance ou segredo é disponibilizado aqui.

## Limites e perigos

- A tela do operador **Postgres > Backups** no Railway mostrou **No
  Backups**. Uma nova cópia será somente do estado corrente; não recupera
  os valores HWM anteriores a setembro ou eventos perdidos.
- O PostgreSQL de produção usa Railway Postgres SSL **major 18** e um único
  volume em sfo. Os scripts rejeitam ferramentas `pg_dump`/`pg_restore`
  de versão <18 para evitar incompatibilidade com servidor/arquivo 18.
- `pg_dump` produz um snapshot lógico consistente de **um banco**;
  não é PITR, não inclui necessariamente roles/globals, outros bancos,
  buckets, segredos ou volume inteiro. O backup e teste podem consumir
  CPU, egress e armazenamento; só usar depois da aprovação de janela,
  cotação e permissões.
- A criptografia X25519 é via executável `age`. A **identidade privada**
  deve ser criada e guardada fora do GitHub, ChatGPT, Railway logs e
  serviços operacionais. Sem ela o arquivo fica irrecuperável. Testar
  custódia e recuperação da identidade em canal independente.
- Arquivo `backup.pgdump.age` e `manifest.json` ficam numa pasta de
  permissão 0700. O conteúdo do dump é transmitido em pipe diretamente
  ao `age`; nenhum dump não criptografado é escrito no disco. O manifesto
  registra apenas metadados, SHA256 do **ciphertext**, bytes e versão
  do cliente — nunca DSN, chaves/IDs de contas, HWM ou transações.
- SHA256 detecta corrupção acidental, **não comprova autenticidade
  independente**: um adversário com permissão de editar arquivo e
  manifesto pode recalcular o hash. Fixar o hash em registro assinado,
  datado e com controle de acesso independente, e cifrar também qualquer
  metadado sensível de retenção.
- Uma cópia local **não é uma cópia offsite**. A ferramenta informa
  `offsite_verified=false` até a transferência e validação, a serem
  autorizadas em etapa separada. Um bucket Railway não oferece object
  lock/versioning conforme documentação; não tratar como WORM.
- O restore em banco local (loopback, nome obrigatório sufixado
  `_restore_test`) recusa um schema ocupado e não usa
  `--clean`/`--create`, nem tem acesso a ordens. Não usar
  túnel/port-forward de loopback para produção! A checagem de isolamento
  físico/credenciais é parte obrigatória da revisão humana.

## Comandos SEGUROS permitidos agora

Somente planejar; **não conecta a serviços nem cria arquivos**:

```bash
python -m tools.backup.pg_encrypted_backup_v1 plan
```

Saída: `PLAN_ONLY_NO_CONNECT_NO_WRITE`.

A análise dos testes mock pode ser feita em CI:

```bash
python -m unittest -v tests.test_pg_encrypted_backup_preparation_625_v1
```

Os testes não usam credenciais reais e provam os guardrails de consentimento,
diretório privado, hash, recusa de destino remoto/ocupado e streaming
conceitual. **Não** provam ainda uma extração/restauração real com binários
PostgreSQL 18 + age; essa prova precisa ocorrer em um laboratório dedicado,
com banco sintético, antes do primeiro acesso a dados reais.

## Futura preparação para teste isolado (SEM EXECUÇÃO NESTE PR)

1. Obter aprovação explícita de uma janela de leitura do banco de
   produção, tamanho esperado, orçamento e um destino independente.
2. Preparar um host seguro com `pg_dump` e `pg_restore` major 18+,
   `age`, `psql`, PostgreSQL isolado para testes e armazenamento
   cifrado; registrar versões e compatibilidade.
3. Gerar par de chaves age X25519 em host seguro. Só o recipient **público**
   segue para ambiente de backup; identidade privada sob custódia externa.
4. Configurar conexão de leitura PostgreSQL por `PGHOST`, `PGPORT`,
   `PGUSER`, `PGDATABASE`, `PGSSLMODE`, `PGPASSFILE` (permissão 0600),
   sem usar password/DSN em linha de comando. Exigir role mínimo autorizado
   com acesso de SELECT/snapshot do banco correto. Não publicar variáveis.
5. Criar pasta privada 0700 fora do checkout Git, via equipe autorizada;
   não salvar backup na conversa, repositório, artefatos de GitHub Actions
   nem download público.
6. A execução futura de backup exige **três condições explícitas**:
   modo `backup --execute`, `--approval-ref` de mudança e variável
   `NEXUS_APPROVE_BACKUP_EXPORT=YES_THIS_SINGLE_RUN`.
   Sem todas, fail-closed. A ferramenta grava **só ciphertext**.
7. Transferir os dois arquivos para armazenamento offsite independente,
   com permissões mínimas, criptografia mantida, retenção e compromisso
   SHA256 assinado/datatado. Confirmar leitura no destino; manter histórico.
8. Preparar PostgreSQL 18 descartável e **desconectado** da Binance/live,
   instância criada vazia com nome terminado em `_restore_test` e
   acessível apenas localmente (sem qualquer túnel à produção).
   `NEXUS_APPROVE_ISOLATED_RESTORE=YES_THIS_SINGLE_RUN`, mais modo
   `restore --execute --approval-ref`, são necessários para restaurar.
9. A restauração verifica integridade do ciphertext, recusa schema não
   vazio e usa `age --decrypt | pg_restore --single-transaction
   --exit-on-error --no-owner --no-privileges`, sem criar dump plaintext.
   Resultado é **RESTORE_COMPLETED_FINANCIAL_ASSERTIONS_PENDING**, não PASS.
10. Auditor independente confere HWM, provenance (LEGACY vs CANONICAL),
    `key_value`, CAS, ledger/cursor, quantidades por tabela,
    Pg roles/extensions/ownership e não-escrita de produção.
    Só registrar `RESTORE_PASS` mediante provas agregadas da
    restauração sintética, e depois da restauração com dados
    previamente autorizados. Não publicar informações identificadoras.
11. Só após backup + restauração real com PASS, análise do patch Railway,
    retirada do caminho legado direto de HWM e aprovação independente
    avaliar migração real. PRs #621–#624 permanecem DRAFT.

## Segurança operacional e rollback

Apenas scripts independentes em `tools/backup/`; não há imports no bot,
start command, web endpoint, configurações Railway, DDL ou feature flag.
Durante um eventual experimento autorizado, se backup falhar, apagar
apenas a pasta temporária da nova execução. Nunca restaurar ao banco
LIVE como experimento. Se restore falhar, descartar **somente** o banco
descartável do laboratório. Não repetir exportações automaticamente.
Não confundir uma restauração bem-sucedida com auditoria histórica
completa ou autorização de estratégias de futuros.

**Não altera HWM = 22.79869386 USDT, DD = ~76.68% (limite 30%),
nem EVIDENCE_FAIL OOS. LIVE continua NO-GO.**
