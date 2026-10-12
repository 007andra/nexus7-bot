"""Read-only attribution of PostgreSQL TEMP privilege on the current database.

This query runs ONLY with the existing diagnostic credential in an explicit
READ ONLY transaction, separate from the general role audit transaction.
It reads pg_database, pg_roles and PostgreSQL ACL helpers, without modifying
roles, privileges or application state. Never print a row or an exception:
identities and database names may be present in raw driver messages.
"""
from __future__ import annotations

from dataclasses import dataclass


# PostgreSQL resolves database grants through ACLs, including an implicit
# default ACL when pg_database.datacl IS NULL. PUBLIC is grantee OID 0.
# pg_has_role(..., 'USAGE') covers active inheritance, not SET ROLE alone.
# No identifiers, DSNs, names or arbitrary user inputs are interpolated.
TEMP_ORIGIN_SQL = """
WITH target_db AS (
    SELECT d.datdba, d.datacl AS raw_acl,
           COALESCE(d.datacl, acldefault('d', d.datdba)) AS effective_acl
      FROM pg_catalog.pg_database AS d
     WHERE d.datname = current_database()
), principal AS (
    SELECT r.oid FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user
), temp_acl AS (
    SELECT grant_entry.grantee
      FROM target_db AS d
      CROSS JOIN LATERAL pg_catalog.aclexplode(d.effective_acl) AS grant_entry
     WHERE grant_entry.privilege_type IN ('TEMPORARY', 'TEMP')
)
SELECT pg_catalog.has_database_privilege(
           current_database(), 'TEMP') AS effective_temp,
       EXISTS(SELECT 1 FROM temp_acl AS a
               WHERE a.grantee = 0::oid) AS from_public,
       EXISTS(SELECT 1 FROM temp_acl AS a CROSS JOIN principal AS p
               WHERE a.grantee = p.oid) AS from_direct,
       EXISTS(SELECT 1 FROM temp_acl AS a CROSS JOIN principal AS p
               WHERE a.grantee <> 0::oid AND a.grantee <> p.oid
                 AND pg_catalog.pg_has_role(p.oid, a.grantee, 'USAGE'))
               AS from_inherited,
       EXISTS(SELECT 1 FROM target_db AS d CROSS JOIN principal AS p
               WHERE d.datdba = p.oid) AS from_owner,
       EXISTS(SELECT 1 FROM target_db AS d WHERE d.raw_acl IS NULL)
               AS default_acl
"""


@dataclass(frozen=True)
class TempOrigin:
    from_public: bool
    from_direct: bool
    from_inherited: bool
    from_owner: bool
    default_acl: bool


async def inspect_temp_origin(conn) -> TempOrigin:
    """Derive provenance, fail closed on missing or contradictory evidence."""
    row = await conn.fetchrow(TEMP_ORIGIN_SQL)
    if row is None:
        raise ValueError("missing_catalog_evidence")
    fields = (
        "effective_temp", "from_public", "from_direct",
        "from_inherited", "from_owner", "default_acl",
    )
    try:
        values = {name: row[name] for name in fields}
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("incomplete_catalog_evidence") from exc
    if not all(type(value) is bool for value in values.values()):
        raise ValueError("non_boolean_catalog_evidence")
    # Caller only requests attribution when the main role audit already
    # found TEMP=FAIL, meaning TEMP effective=true. Conflicting evidence is
    # INCONCLUSIVE, never a basis to grant/revoke or bypass the main gate.
    if not values["effective_temp"] or not any(
        values[name] for name in
        ("from_public", "from_direct", "from_inherited", "from_owner")
    ):
        raise ValueError("contradictory_acl_evidence")
    return TempOrigin(
        from_public=values["from_public"],
        from_direct=values["from_direct"],
        from_inherited=values["from_inherited"],
        from_owner=values["from_owner"],
        default_acl=values["default_acl"],
    )


def origin_log_line(origin: TempOrigin | None) -> str:
    """Only fixed labels and booleans; no SQL rows or exception messages."""
    if origin is None or not isinstance(origin, TempOrigin):
        return (
            "[PG_TEMP_ORIGIN] status=INCONCLUSIVE "
            "evidence=UNAVAILABLE decision_effect=NONE execution_effect=NONE"
        )

    def flag(value: bool) -> str:
        return "true" if value is True else "false"

    # PUBLIC grants are database-wide; removal would affect *every* role
    # using this database unless it has an independent TEMP path.
    scope = "DATABASE_WIDE" if origin.from_public else "ROLE_REVIEW_REQUIRED"
    return (
        "[PG_TEMP_ORIGIN] status=RESOLVED "
        f"public={flag(origin.from_public)} "
        f"direct={flag(origin.from_direct)} "
        f"inherited={flag(origin.from_inherited)} "
        f"owner={flag(origin.from_owner)} "
        f"default_acl={flag(origin.default_acl)} "
        f"potential_revocation_scope={scope} "
        "decision_effect=NONE execution_effect=NONE"
    )
