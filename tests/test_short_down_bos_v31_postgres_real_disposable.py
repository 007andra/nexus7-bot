"""True PG transaction and privilege proof; disposable GitHub CI DB ONLY.

This file MUST never open production Railway/Binance endpoints or credentials.
Requires the exact TEST_POSTGRES_DSN fixture of .github/workflows/quality.yml,
GITHUB_ACTIONS=true, and an ephemeral locally-created research database.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import unittest
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import asyncpg

from bot.short_down_bos_v31_manifest import empty_manifest
from bot.short_down_bos_v31_readonly_enrollment import (
    EnrollmentAuthority, PREREG_EPOCH, RosterEvidenceError,
)
from bot.short_down_bos_v31_postgres_adapter import (
    PriorHeadWitness, append_isolated_transaction, verify_isolated_transaction,
)

FIXTURE_DSN = "postgresql://proof:postgres@127.0.0.1:5432/nexus_release_proof"
ENABLED = (
    os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("TEST_POSTGRES_DSN") == FIXTURE_DSN
)


def authority():
    return EnrollmentAuthority(
        implementation_sha="a" * 40,
        implementation_commit_epoch=PREREG_EPOCH + 100,
        authorized_capture_epoch=PREREG_EPOCH + 200,
        authorization_reference="ci-disposable-research-protocol-v31",
    )


def candidate(i):
    return {
        "candidate_id": f"CI_V31_CANDIDATE_{i}",
        "captured_epoch": PREREG_EPOCH + 300 + i,
        "symbol": "BTCUSDT" if i % 2 == 0 else "ETHUSDT",
        "entry": 100, "stop": 102, "target": 96,
        "population": "HARD_GATE_SHADOW",
        "side": "SHORT", "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "nexus_called": True, "nexus_allowed": True,
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
    }


@unittest.skipUnless(ENABLED, "CI-only disposable PostgreSQL fixture required")
class V31IsolatedPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.authority = authority()
        self.admin = await asyncpg.connect(FIXTURE_DSN)
        self.database_name = "nexus7_research_ci_" + uuid4().hex[:12]
        self.connections = []
        # Operate only against literal local disposable CI fixture.
        self.role_created = not await self.admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='nexus_v31_writer')"
        )
        if self.role_created:
            await self.admin.execute("CREATE ROLE nexus_v31_writer NOLOGIN")
        await self.admin.execute(f"CREATE DATABASE {self.database_name}")
        parts = urlparse(FIXTURE_DSN)
        self.db_dsn = urlunparse(parts._replace(path="/" + self.database_name))
        dbadmin = await asyncpg.connect(self.db_dsn)
        self.connections.append(dbadmin)
        sqlpath = (Path(__file__).resolve().parents[1]
                   / "docs/research/sql/v31_isolated_manifest.sql")
        await dbadmin.execute(sqlpath.read_text(encoding="utf-8"))
        initial = empty_manifest(self.authority)
        await dbadmin.execute(
            "INSERT INTO v31_research.manifest_heads "
            "(protocol,effective_cutoff_epoch,implementation_sha,"
            " authorization_reference,genesis_digest,sequence,head_digest) "
            "VALUES ($1,$2,$3,$4,$5,0,$6)",
            initial.protocol, initial.effective_cutoff_epoch,
            initial.implementation_sha, initial.authorization_reference,
            initial.genesis_digest, initial.genesis_digest,
        )
        for sql in (
            "GRANT USAGE ON SCHEMA v31_research TO nexus_v31_writer",
            "GRANT SELECT ON v31_research.manifest_heads TO nexus_v31_writer",
            "GRANT SELECT,INSERT ON v31_research.manifest_events TO nexus_v31_writer",
            "GRANT UPDATE (sequence,head_digest) ON v31_research.manifest_heads TO nexus_v31_writer",
        ):
            await dbadmin.execute(sql)

    async def asyncTearDown(self):
        for conn in self.connections:
            if not conn.is_closed():
                await conn.close()
        if hasattr(self, "admin") and not self.admin.is_closed():
            if hasattr(self, "database_name"):
                await self.admin.execute(
                    f"DROP DATABASE IF EXISTS {self.database_name} WITH (FORCE)"
                )
            if getattr(self, "role_created", False):
                await self.admin.execute("DROP ROLE IF EXISTS nexus_v31_writer")
            await self.admin.close()

    async def writer(self):
        connection = await asyncpg.connect(self.db_dsn)
        self.connections.append(connection)
        await connection.execute("SET ROLE nexus_v31_writer")
        return connection

    async def test_isolated_real_pg_commit_privileges_and_tamper_rejection(self):
        db = await self.writer()
        initial = empty_manifest(self.authority)
        seed = PriorHeadWitness(initial.genesis_digest, "ci-independent-witness")
        first = await append_isolated_transaction(
            db, authority=self.authority,
            raw_candidate=candidate(1), witness=seed,
        )
        self.assertEqual(first["sequence"], 1)
        self.assertFalse(first["roster_durable"])
        self.assertFalse(first["live_allowed"])
        with self.assertRaisesRegex(RosterEvidenceError, "EXTERNAL_ANCHOR"):
            await append_isolated_transaction(
                db, authority=self.authority, raw_candidate=candidate(2),
                witness=seed,
            )
        # No UPDATE privilege, and trigger additionally guards mutation.
        with self.assertRaises(asyncpg.PostgresError):
            await db.execute("UPDATE v31_research.manifest_events SET digest='0' WHERE sequence=1")
        second = await append_isolated_transaction(
            db, authority=self.authority, raw_candidate=candidate(2),
            witness=PriorHeadWitness(first["head_digest"], "ci-fresh-anchor"),
        )
        self.assertEqual(second["sequence"], 2)
        audit = await verify_isolated_transaction(
            db, authority=self.authority,
            witness=PriorHeadWitness(second["head_digest"], "ci-fresh-anchor"),
        )
        self.assertEqual(audit["observed_events"], 2)
        self.assertFalse(audit["roster_durable"])
        # Header sequence cannot be bypassed even by a poorly formed UPDATE.
        with self.assertRaises(asyncpg.PostgresError):
            await db.execute(
                "UPDATE v31_research.manifest_heads SET sequence=50 WHERE protocol=$1",
                initial.protocol,
            )

    async def test_concurrent_writers_have_one_winner_no_duplicate_history(self):
        c1, c2 = await self.writer(), await self.writer()
        root = empty_manifest(self.authority)
        witness = PriorHeadWitness(root.genesis_digest, "ci-race-witness")
        outcome = await asyncio.gather(
            append_isolated_transaction(
                c1, authority=self.authority,
                raw_candidate=candidate(1), witness=witness,
            ),
            append_isolated_transaction(
                c2, authority=self.authority,
                raw_candidate=candidate(2), witness=witness,
            ),
            return_exceptions=True,
        )
        success = [x for x in outcome if isinstance(x, dict)]
        failed = [x for x in outcome if isinstance(x, Exception)]
        self.assertEqual(len(success), 1)
        self.assertEqual(len(failed), 1)
        checker = await self.writer()
        audit = await verify_isolated_transaction(
            checker, authority=self.authority,
            witness=PriorHeadWitness(success[0]["head_digest"],
                                     "ci-verified-race-winner"),
        )
        self.assertEqual(audit["observed_events"], 1)
        self.assertEqual(audit["enrolled"], 1)


if __name__ == "__main__":
    unittest.main()
