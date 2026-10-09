#!/usr/bin/env bash
# CI-only synthetic encrypted dump + isolated restore (PostgreSQL 18 + age).
set -euo pipefail
umask 077

if [[ "${GITHUB_ACTIONS:-}" != "true" || "${NEXUS_SYNTHETIC_PG18_LAB:-}" != "I_CONFIRM_NO_PRODUCTION" ]]; then
  echo "::error::CI-only synthetic laboratory approval missing"; exit 30
fi
if [[ -n "${DATABASE_URL:-}" || -n "${BINANCE_API_KEY:-}" || -n "${RAILWAY_TOKEN:-}" ]]; then
  echo "::error::Production credential environment not permitted"; exit 31
fi
[[ "${PGHOST:-}" == "127.0.0.1" && "${PGPORT:-}" == "5432" && "${PGUSER:-}" == "proof" ]] ||
  { echo "::error::Not using the isolated synthetic PostgreSQL endpoint"; exit 32; }

image="public.ecr.aws/docker/library/postgres:18"
temp="$(mktemp -d)"
chmod 700 "$temp"
trap 'rm -rf "$temp"' EXIT
mkdir -m 700 "$temp/bin" "$temp/private" "$temp/keyring"

# Only PostgreSQL 18 binaries run; no host PostgreSQL client is required.
for cmd in pg_dump pg_restore psql; do
  cat > "$temp/bin/$cmd" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec docker run --rm -i --network host -e PGPASSWORD -e PGSSLMODE --entrypoint $cmd $image "\$@"
EOF
  chmod 700 "$temp/bin/$cmd"
done
export PATH="$temp/bin:$PATH"
export PGSSLMODE=disable
export PGDATABASE=nexus_synthetic_source

[[ "$(pg_dump --version)" == *"PostgreSQL) 18."* ]] || exit 33
[[ "$(pg_restore --version)" == *"PostgreSQL) 18."* ]] || exit 34
age --version >/dev/null
age-keygen -o "$temp/keyring/identity" >/dev/null 2>&1
chmod 600 "$temp/keyring/identity"
recipient="$(age-keygen -y "$temp/keyring/identity")"

# Synthetic account data only: none of these values belong to real users.
psql -q -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
CREATE TABLE public.key_value (
    key text PRIMARY KEY, value text NOT NULL, updated_at text
);
INSERT INTO public.key_value(key,value,updated_at) VALUES
('synthetic:hwm:peak', '123.4567', 'synthetic'),
('synthetic:hwm:provenance', '{"version":1,"new_peak":123.4567,"reason":"bootstrap","evidence_ref":"CI_SYNTHETIC","execution_effect":"NONE"}', 'synthetic'),
('synthetic:ledger:cursor', '3', 'synthetic'),
('synthetic:ledger:net', '7.25', 'synthetic');
CREATE TABLE public.synthetic_flow_events (
    event_id integer PRIMARY KEY, amount numeric(20,8) NOT NULL,
    flow_kind text NOT NULL
);
INSERT INTO public.synthetic_flow_events(event_id,amount,flow_kind) VALUES
(1,10.00000000,'CREDIT'),(2,-2.00000000,'DEBIT'),(3,-0.75000000,'DEBIT');
SQL

source_keys="$(psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT key||'='||value FROM public.key_value ORDER BY key")"
[[ "$(psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM public.synthetic_flow_events")" == "3" ]] || exit 35

export NEXUS_BACKUP_PRIVATE_DIR="$temp/private"
export NEXUS_BACKUP_AGE_RECIPIENT="$recipient"
export NEXUS_APPROVE_BACKUP_EXPORT="YES_THIS_SINGLE_RUN"
python -m tools.backup.pg_encrypted_backup_v1 backup --execute \
  --approval-ref SYNTHETIC_PG18_TEST_625

mapfile -t backups < <(find "$temp/private" -mindepth 1 -maxdepth 1 -type d)
[[ "${#backups[@]}" -eq 1 ]] || exit 36
encrypted_dir="${backups[0]}"
[[ -s "$encrypted_dir/backup.pgdump.age" && -s "$encrypted_dir/manifest.json" ]] || exit 37
[[ "$(find "$encrypted_dir" -maxdepth 1 -type f | wc -l | tr -d ' ')" == "2" ]] || exit 38
[[ ! -e "$encrypted_dir/backup.pgdump" ]] || exit 39

PGDATABASE=postgres psql -q -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE nexus_synthetic_restore_test" >/dev/null
export NEXUS_BACKUP_RESTORE_SOURCE_DIR="$encrypted_dir"
export NEXUS_BACKUP_AGE_IDENTITY_FILE="$temp/keyring/identity"
export NEXUS_APPROVE_ISOLATED_RESTORE="YES_THIS_SINGLE_RUN"

# Corrupt ciphertext is refused by SHA256 manifest before decryption.
tampered="$temp/private/tampered"
mkdir -m 700 "$tampered"
cp "$encrypted_dir/backup.pgdump.age" "$encrypted_dir/manifest.json" "$tampered/"
printf 'INVALID' >> "$tampered/backup.pgdump.age"
if NEXUS_BACKUP_RESTORE_SOURCE_DIR="$tampered" PGDATABASE=nexus_synthetic_restore_test \
   python -m tools.backup.pg_encrypted_backup_v1 restore --execute \
     --approval-ref SYNTHETIC_RESTORE_625 >/dev/null; then
  echo "::error::Corrupt ciphertext was accepted"; exit 40
fi

# Unrelated decryption key must fail and the destination remain empty.
age-keygen -o "$temp/keyring/wrong_identity" >/dev/null 2>&1
chmod 600 "$temp/keyring/wrong_identity"
if NEXUS_BACKUP_AGE_IDENTITY_FILE="$temp/keyring/wrong_identity" PGDATABASE=nexus_synthetic_restore_test \
   python -m tools.backup.pg_encrypted_backup_v1 restore --execute \
     --approval-ref SYNTHETIC_RESTORE_625 >/dev/null; then
  echo "::error::Wrong decryption identity accepted"; exit 41
fi
empty="$(PGDATABASE=nexus_synthetic_restore_test psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON
  c.relnamespace=n.oid WHERE c.relkind IN ('r','p','v','m','S','f')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'")"
[[ "$empty" == "0" ]] || { echo "::error::Wrong-key restore wrote SQL"; exit 42; }

PGDATABASE=nexus_synthetic_restore_test \
  python -m tools.backup.pg_encrypted_backup_v1 restore --execute \
    --approval-ref SYNTHETIC_RESTORE_625

restored_keys="$(PGDATABASE=nexus_synthetic_restore_test psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT key||'='||value FROM public.key_value ORDER BY key")"
restored_count="$(PGDATABASE=nexus_synthetic_restore_test psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM public.synthetic_flow_events")"
restored_net="$(PGDATABASE=nexus_synthetic_restore_test psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT sum(amount) FROM public.synthetic_flow_events")"
[[ "$restored_keys" == "$source_keys" && "$restored_count" == "3" &&
   "$restored_net" == "7.25000000" ]] ||
  { echo "::error::Synthetic financial invariants diverged"; exit 43; }

[[ "$(PGDATABASE=nexus_synthetic_source psql -At -v ON_ERROR_STOP=1 \
  -c "SELECT key||'='||value FROM public.key_value ORDER BY key")" == "$source_keys" ]] ||
  { echo "::error::Synthetic source mutated"; exit 44; }

echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] status=PASS"
echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] pg_major=18"
echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] age_encrypt_decrypt=PASS"
echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] tamper_wrong_key_rejected=PASS"
echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] local_restore=PASS"
echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] hwm_provenance_ledger_cursor_synthetic=PASS"
echo "[SYNTHETIC_PG18_AGE_ROUNDTRIP] production_access=NONE live_allowed=false"
