"""Read-only PostgreSQL privilege audit for the OOS diagnostic credential.

Usage (inside a network that reaches the database, e.g. the Railway service):
    python -m scripts.pg_readonly_role_audit [--dsn-env OOS_DIAGNOSTIC_DATABASE_URL] [--json]

Safety:
* the DSN is read from an environment variable and never printed;
* only catalog SELECTs and has_*_privilege()/pg_has_role() calls are issued,
  all inside a READ ONLY transaction; no DML, DDL, GRANT or SET ROLE;
* the session is opened WITHOUT forcing default_transaction_read_only, so
  item 8 reports the role's real configuration, not one this script imposed;
* role and database names are masked in the output.

Exit code 0 only when every check PASSes (READ_ONLY_ROLE_VERIFIED=true).
Any write privilege, ownership, dangerous attribute or membership, or any
check that cannot be evaluated is a FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

SYSTEM_SCHEMA_FILTER = (
    "n.nspname NOT IN ('pg_catalog','information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'"
)
TABLE_PRIVS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
# Predefined roles that grant writes, server file/program access or control.
DANGEROUS_PREDEFINED = (
    "pg_write_all_data", "pg_write_server_files", "pg_execute_server_program",
    "pg_read_server_files", "pg_signal_backend", "pg_create_subscription",
    "pg_checkpoint", "pg_use_reserved_connections", "pg_maintain",
)


def mask(name):
    if name is None:
        return None
    text = str(name)
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return f"{text[:2]}***#{digest}"


class Audit:
    def __init__(self):
        self.rows = []

    def add(self, item, check, ok, evidence):
        self.rows.append({"item": item, "check": check,
                          "result": "PASS" if ok else "FAIL", "evidence": evidence})

    @property
    def verified(self):
        return bool(self.rows) and all(r["result"] == "PASS" for r in self.rows)


async def run(conn) -> Audit:
    a = Audit()
    q = conn.fetch
    q1 = conn.fetchrow

    # 1. identity and server role
    ident = await q1(
        "SELECT current_user AS cu, session_user AS su, current_database() AS db, "
        "pg_is_in_recovery() AS rec, current_setting('server_version_num')::int AS ver")
    a.add(1, "identidade da sessão", True,
          f"current_user={mask(ident['cu'])} session_user={mask(ident['su'])} "
          f"same_user={ident['cu'] == ident['su']} database={mask(ident['db'])} "
          f"pg_is_in_recovery={ident['rec']} server_version_num={ident['ver']}")
    ver = int(ident["ver"])

    # 2. role attributes
    attrs = await q1(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
        "rolinherit, rolcanlogin FROM pg_roles WHERE rolname = current_user")
    bad = [k for k in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication",
                       "rolbypassrls") if attrs[k]]
    a.add(2, "atributos da role (super/createdb/createrole/replication/bypassrls)",
          not bad, "todos false" if not bad else "true: " + ",".join(bad))

    # 3. database privileges
    dbp = await q1(
        "SELECT has_database_privilege(current_database(),'CREATE') AS c, "
        "has_database_privilege(current_database(),'TEMP') AS t, "
        "pg_catalog.pg_get_userbyid(d.datdba) = current_user AS owner "
        "FROM pg_database d WHERE d.datname = current_database()")
    a.add(3, "CREATE no banco", not dbp["c"], f"CREATE={dbp['c']}")
    a.add(3, "TEMP no banco", not dbp["t"],
          f"TEMP={dbp['t']} (tabelas temporárias são escrita na sessão)")
    a.add(3, "dono do banco", not dbp["owner"], f"owner={dbp['owner']}")

    # 4. schema privileges (all application schemas, including public)
    schemas = await q(
        "SELECT n.nspname AS s, has_schema_privilege(n.oid,'CREATE') AS c, "
        "has_schema_privilege(n.oid,'USAGE') AS u, "
        "pg_get_userbyid(n.nspowner) = current_user AS owner "
        f"FROM pg_namespace n WHERE {SYSTEM_SCHEMA_FILTER} ORDER BY 1")
    creatable = [r["s"] for r in schemas if r["c"]]
    owned = [r["s"] for r in schemas if r["owner"]]
    a.add(4, "CREATE em schemas de aplicação", not creatable,
          f"schemas={len(schemas)} create={creatable or 'nenhum'}")
    a.add(4, "dono de schema", not owned, f"owned={owned or 'nenhum'}")

    # 5. relation privileges (effective, inheritance included) + column grants
    privs = ", ".join(f"has_table_privilege(c.oid,'{p}') AS {p.lower()}" for p in TABLE_PRIVS)
    rels = await q(
        f"SELECT n.nspname||'.'||c.relname AS rel, c.relkind::text AS kind, {privs}, "
        "has_table_privilege(c.oid,'SELECT') AS sel, "
        "has_any_column_privilege(c.oid,'INSERT') AS col_ins, "
        "has_any_column_privilege(c.oid,'UPDATE') AS col_upd, "
        "pg_get_userbyid(c.relowner) = current_user AS owner "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE c.relkind IN ('r','p','v','m','f') AND {SYSTEM_SCHEMA_FILTER}")
    writable = {}
    for r in rels:
        hits = [p for p in TABLE_PRIVS if r[p.lower()]]
        if r["col_ins"]:
            hits.append("INSERT(coluna)")
        if r["col_upd"]:
            hits.append("UPDATE(coluna)")
        if hits:
            writable[r["rel"]] = hits
    a.add(5, "INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER em tabelas",
          not writable,
          f"relações={len(rels)} legíveis={sum(1 for r in rels if r['sel'])} "
          f"com_escrita={len(writable)}"
          + ("" if not writable else " ex: " + "; ".join(
              f"{k}:{'/'.join(v)}" for k, v in list(writable.items())[:5])))
    owned_rels = [r["rel"] for r in rels if r["owner"]]
    a.add(5, "dono de tabela/view", not owned_rels,
          f"owned={len(owned_rels)}" + ("" if not owned_rels else f" ex: {owned_rels[:5]}"))

    # 6. sequences, functions and routines
    seqs = await q(
        "SELECT n.nspname||'.'||c.relname AS seq, "
        "has_sequence_privilege(c.oid,'UPDATE') AS upd, "
        "has_sequence_privilege(c.oid,'USAGE') AS usg, "
        "pg_get_userbyid(c.relowner) = current_user AS owner "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE c.relkind='S' AND {SYSTEM_SCHEMA_FILTER}")
    seq_bad = [s["seq"] for s in seqs if s["upd"] or s["usg"] or s["owner"]]
    a.add(6, "sequences (USAGE/UPDATE permitem nextval/setval)", not seq_bad,
          f"sequences={len(seqs)} com_escrita={len(seq_bad)}"
          + ("" if not seq_bad else f" ex: {seq_bad[:5]}"))
    funcs = await q(
        "SELECT n.nspname||'.'||p.proname AS fn, p.provolatile::text AS vol, "
        "p.prosecdef AS secdef, pg_get_userbyid(p.proowner) = current_user AS owner, "
        "l.lanname AS lang "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "JOIN pg_language l ON l.oid=p.prolang "
        f"WHERE {SYSTEM_SCHEMA_FILTER} AND has_function_privilege(p.oid,'EXECUTE')")
    # SECURITY DEFINER runs as its owner (privilege escalation); an owned
    # function can be redefined. Invoker functions only have caller rights.
    risky = [f["fn"] for f in funcs if f["secdef"] or f["owner"]]
    volatile = sum(1 for f in funcs if f["vol"] == "v")
    a.add(6, "funções/rotinas que escalam privilégio (SECURITY DEFINER ou próprias)",
          not risky,
          f"executáveis={len(funcs)} security_definer_ou_próprias={len(risky)} "
          f"volatile_invoker={volatile} (rodam com os privilégios de quem chama)"
          + ("" if not risky else f" ex: {risky[:5]}"))

    # 7. role membership, SET ROLE and inherited write paths
    member_mode = "SET" if ver >= 160000 else "MEMBER"
    roles = await q(
        "SELECT r.rolname AS role, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
        "r.rolreplication, r.rolbypassrls, "
        "pg_has_role(current_user, r.oid, 'USAGE') AS inherited, "
        f"pg_has_role(current_user, r.oid, '{member_mode}') AS can_set "
        "FROM pg_roles r WHERE r.rolname <> current_user "
        "AND pg_has_role(current_user, r.oid, 'MEMBER')")
    dangerous, write_via = [], []
    for r in roles:
        if (r["rolsuper"] or r["rolcreatedb"] or r["rolcreaterole"] or r["rolreplication"]
                or r["rolbypassrls"] or r["role"] in DANGEROUS_PREDEFINED):
            dangerous.append(mask(r["role"]) if not r["role"].startswith("pg_") else r["role"])
        hits = await q1(
            "SELECT count(*) FILTER (WHERE "
            + " OR ".join(f"has_table_privilege($1::name, c.oid, '{p}')" for p in TABLE_PRIVS)
            + ") AS w FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE c.relkind IN ('r','p','v','m','f') AND {SYSTEM_SCHEMA_FILTER}",
            r["role"])
        if hits["w"]:
            label = r["role"] if r["role"].startswith("pg_") else mask(r["role"])
            write_via.append(f"{label}:{hits['w']}")
    a.add(7, "roles assumíveis/herdadas", not dangerous and not write_via,
          f"memberships={len(roles)} perigosas={dangerous or 'nenhuma'} "
          f"escrita_via_role={write_via or 'nenhuma'} (modo {member_mode})")

    # 8. default_transaction_read_only: session setting vs effective privileges
    show = await q1(
        "SELECT current_setting('default_transaction_read_only') AS dtro, "
        "current_setting('transaction_read_only') AS tro")
    cfg = await q(
        "SELECT unnest(setconfig) AS kv, (setrole <> 0) AS for_role, "
        "(setdatabase <> 0) AS for_db FROM pg_db_role_setting "
        "WHERE setrole IN (0, (SELECT oid FROM pg_roles WHERE rolname=current_user)) "
        "AND setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname=current_database()))")
    pinned = [f"{'role' if r['for_role'] else ''}{'+' if r['for_role'] and r['for_db'] else ''}"
              f"{'db' if r['for_db'] else ''}:{r['kv']}" for r in cfg
              if str(r["kv"]).startswith("default_transaction_read_only")]
    a.add(8, "default_transaction_read_only (configuração, não é barreira de privilégio)",
          show["dtro"] == "on",
          f"sessão default_transaction_read_only={show['dtro']} "
          f"(transação desta auditoria READ ONLY={show['tro']}); "
          f"configurado_em={pinned or 'nenhum ALTER ROLE/DATABASE'}; "
          "qualquer sessão pode fazer SET default_transaction_read_only=off, então só os "
          "itens 2 a 7 provam incapacidade de escrita")
    return a


async def main(args):
    dsn = os.environ.get(args.dsn_env, "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        print(f"FAIL: {args.dsn_env} ausente ou inválido (valor não exibido)")
        print("READ_ONLY_ROLE_VERIFIED=false")
        return 2
    import asyncpg
    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as exc:
        print(f"FAIL: conexão não estabelecida ({type(exc).__name__}); auditoria inconclusiva")
        print("READ_ONLY_ROLE_VERIFIED=false")
        return 2
    try:
        async with conn.transaction(readonly=True):
            audit = await run(conn)
    except Exception as exc:
        print(f"FAIL: auditoria interrompida ({type(exc).__name__}); inconclusiva")
        print("READ_ONLY_ROLE_VERIFIED=false")
        return 2
    finally:
        await conn.close()
    if args.json:
        print(json.dumps({"rows": audit.rows, "verified": audit.verified}, indent=2,
                         ensure_ascii=False))
    else:
        print("| # | Verificação | Resultado | Evidência |")
        print("|---|---|---|---|")
        for r in audit.rows:
            print(f"| {r['item']} | {r['check']} | {r['result']} | {r['evidence']} |")
    print(f"READ_ONLY_ROLE_VERIFIED={'true' if audit.verified else 'false'}")
    return 0 if audit.verified else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="OOS_DIAGNOSTIC_DATABASE_URL")
    parser.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
