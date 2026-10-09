"""Future-only HWM journal contract: pure chain and REAL isolated PostgreSQL.

No production DSN is acceptable. The integration tests require the disposable
CI PostgreSQL service; they do not alter production key_value rows or schemas.
"""
import asyncio
import json
import os
import unittest
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

from bot import hwm_provenance as hp
from bot import hwm_transition_journal_v1 as j


def _safe_test_dsn():
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    try:
        u = urlsplit(dsn)
        return (u.hostname in {"127.0.0.1", "localhost"}
                and u.port == 5432
                and u.path == "/nexus_release_proof")
    except ValueError:
        return False


class JournalPureProof(unittest.TestCase):
    def test_scope_hash_and_evidence_redaction(self):
        scope = j._scope("private:scope:DO_NOT_PRINT")
        e = j._payload(
            kind="ANCHOR", scope=scope, event_id=str(uuid.uuid4()),
            old_peak="22.79869386", new_peak="22.79869386",
            equity=None, reason="first_observed_peak",
            evidence_ref="private:EXCHANGE_TRAN_ID_NOT_FOR_REPORTS"
        )
        payload = j._canonical(e)
        self.assertNotIn("private", payload)
        self.assertNotIn("TRAN_ID", payload)
        self.assertIsNone(e["account_equity"])
        self.assertFalse(e["history_before_anchor_verified"])

    def test_reject_invalid_events_and_nonfinite_hwm(self):
        for invalid in (0, -1, float("nan"), float("inf"), True, None, "not-a-number"):
            with self.subTest(invalid=str(invalid)), self.assertRaises(j.JournalIntegrityError):
                j._number(invalid)
        with self.assertRaises(j.JournalIntegrityError):
            j._payload(
                kind="TRANSITION", scope=j._scope("key"),
                event_id=str(uuid.uuid4()), old_peak=None, new_peak=10,
                equity=9, reason="new_equity_high", evidence_ref="some evidence"
            )

    def test_corrupt_chain_or_fabricated_prior_completeness_fails_closed(self):
        scope = j._scope("test:scope:pure")
        payload = j._payload(
            kind="ANCHOR", scope=scope, event_id=str(uuid.uuid4()),
            old_peak="22.79869386", new_peak="22.79869386", equity=None,
            reason="first_observed_peak", evidence_ref="first observation"
        )
        row = {
            "scope_sha256": scope, "seq": 1,
            "event_id": payload["event_id"], "kind": "ANCHOR",
            "previous_digest": j.ZERO_DIGEST,
            "digest": j._chain_digest(j.ZERO_DIGEST, payload),
            "payload_json": j._canonical(payload),
        }
        report = j.verify_chain_rows([row], peak_key="test:scope:pure")
        self.assertEqual(report["status"], "PROSPECTIVE_CHAIN_VALID_PRIOR_HISTORY_UNVERIFIED")
        self.assertFalse(report["live_allowed"])
        for changed in (
            dict(row, digest="f" * 64),
            dict(row, seq=2),
            dict(row, previous_digest="f" * 64),
            dict(row, scope_sha256="b" * 64),
            dict(row, payload_json=json.dumps(dict(payload, history_before_anchor_verified=True)))
        ):
            with self.subTest(changed=changed), self.assertRaises(j.JournalIntegrityError):
                j.verify_chain_rows([changed], peak_key="test:scope:pure")

    def test_open_transaction_is_mandatory(self):
        class NotInTransaction:
            def is_in_transaction(self):
                return False
        with self.assertRaisesRegex(j.JournalIntegrityError, "requires outer transaction"):
            j._require_tx(NotInTransaction())


@unittest.skipUnless(_safe_test_dsn(), "disposable local TEST_POSTGRES_DSN required")
class JournalRealPostgresProof(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.conn = await asyncpg.connect(os.environ["TEST_POSTGRES_DSN"])
        # CI-only fixture. No production startup migration.
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS key_value "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        await self.conn.execute(j.SCHEMA_SQL)
        self.peak_key = "test:future_hwm_journal:" + str(uuid.uuid4())
        self.prov_key = self.peak_key + ":provenance"
        self.orig = "22.79869386"
        self.orig_prov = hp.build_hwm_provenance(
            reason="new_equity_high", old_peak=20, new_peak=float(self.orig),
            account_equity=20, evidence_ref="test:legacy_snapshot",
        )
        await self.conn.execute(
            "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3),($4,$5,$6)",
            self.peak_key, self.orig, "test",
            self.prov_key, self.orig_prov, "test",
        )

    async def asyncTearDown(self):
        # Append-only rows are deliberately NOT deleted; every test uses a
        # unique scope in the disposable CI database.
        await self.conn.close()

    async def anchor(self):
        async with self.conn.transaction():
            return await j.anchor_current_peak_in_tx(
                self.conn, peak_key=self.peak_key, provenance_key=self.prov_key,
                event_id=str(uuid.uuid4()), evidence_ref="test:anchor_evidence",
            )

    async def transition(self, *, event_id=None, old=None, old_prov=None, new="24.0"):
        return await j.commit_transition_in_tx(
            self.conn, peak_key=self.peak_key,
            provenance_key=self.prov_key, expected_peak_raw=self.orig if old is None else old,
            expected_provenance_raw=self.orig_prov if old_prov is None else old_prov,
            new_peak=new, account_equity="24.0", reason="new_equity_high",
            evidence_ref="test:authenticated_equity", event_id=event_id or str(uuid.uuid4()),
        )

    async def current(self):
        return await self.conn.fetchval("SELECT value FROM key_value WHERE key=$1", self.peak_key)

    async def test_anchor_observes_peak_without_modifying_hwm(self):
        info = await self.anchor()
        self.assertEqual(info["status"], "ANCHOR_RECORDED_FROM_CURRENT_STATE")
        self.assertFalse(info["history_before_anchor_verified"])
        self.assertEqual(await self.current(), self.orig)
        report = await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key)
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["last_peak"], self.orig)
        self.assertFalse(report["history_before_anchor_verified"])

    async def test_transition_writes_peak_provenance_and_journal_atomically(self):
        await self.anchor()
        async with self.conn.transaction():
            answer = await self.transition()
        self.assertEqual(answer["seq"], 2)
        self.assertEqual(await self.current(), "24.0")
        provenance_raw = await self.conn.fetchval(
            "SELECT value FROM key_value WHERE key=$1", self.prov_key
        )
        self.assertEqual(json.loads(provenance_raw)["new_peak"], 24.0)
        report = await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key)
        self.assertEqual(report["count"], 2)
        self.assertEqual(Decimal(report["last_peak"]), Decimal("24"))
        self.assertFalse(report["history_before_anchor_verified"])

    async def test_outer_rollback_never_advances_peak_or_chain(self):
        await self.anchor()
        with self.assertRaisesRegex(RuntimeError, "simulate upstream rollback"):
            async with self.conn.transaction():
                await self.transition()
                raise RuntimeError("simulate upstream rollback")
        self.assertEqual(await self.current(), self.orig)
        self.assertEqual(
            (await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key))["count"], 1
        )
        self.assertEqual(
            await self.conn.fetchval("SELECT value FROM key_value WHERE key=$1", self.prov_key),
            self.orig_prov,
        )

    async def test_missing_anchor_or_stale_provenance_fails_closed(self):
        with self.assertRaisesRegex(j.JournalIntegrityError, "anchor missing"):
            async with self.conn.transaction():
                await self.transition()
        self.assertEqual(await self.current(), self.orig)
        await self.anchor()
        with self.assertRaisesRegex(j.JournalIntegrityError, "CAS"):
            async with self.conn.transaction():
                await self.transition(old_prov="wrong")
        self.assertEqual(await self.current(), self.orig)
        self.assertEqual(
            (await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key))["count"], 1
        )

    async def test_duplicate_event_id_never_writes_second_transition(self):
        await self.anchor()
        uid = str(uuid.uuid4())
        async with self.conn.transaction():
            await self.transition(event_id=uid)
        new_prov = await self.conn.fetchval(
            "SELECT value FROM key_value WHERE key=$1", self.prov_key
        )
        with self.assertRaises(j.JournalIntegrityError):
            async with self.conn.transaction():
                # Expected old value is stale after first commit.
                await self.transition(event_id=uid)
        self.assertEqual(await self.current(), "24.0")
        self.assertEqual(
            (await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key))["count"], 2
        )
        self.assertEqual(
            await self.conn.fetchval("SELECT value FROM key_value WHERE key=$1", self.prov_key),
            new_prov,
        )

    async def test_immutability_triggers_refuse_update_delete_truncate(self):
        await self.anchor()
        scope = j._scope(self.peak_key)
        sqls = (
            ("UPDATE hwm_transition_journal_v1 SET digest=$2 WHERE scope_sha256=$1", (scope, "1" * 64)),
            ("DELETE FROM hwm_transition_journal_v1 WHERE scope_sha256=$1", (scope,)),
            ("TRUNCATE hwm_transition_journal_v1", ()),
        )
        for sql, args in sqls:
            with self.subTest(sql=sql):
                with self.assertRaises(Exception) as raised:
                    async with self.conn.transaction():
                        await self.conn.execute(sql, *args)
                self.assertIn("hwm transition journal append-only", str(raised.exception))
        self.assertEqual(
            (await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key))["count"], 1
        )

    async def test_corrupt_anchor_provenance_fails_before_insert(self):
        await self.conn.execute(
            "UPDATE key_value SET value=$2 WHERE key=$1",
            self.prov_key, json.dumps({"version": 1, "new_peak": 3}),
        )
        with self.assertRaisesRegex(j.JournalIntegrityError, "does not match"):
            async with self.conn.transaction():
                await j.anchor_current_peak_in_tx(
                    self.conn, peak_key=self.peak_key,
                    provenance_key=self.prov_key, event_id=str(uuid.uuid4()),
                    evidence_ref="test:bad_anchor",
                )
        self.assertIsNone(await self.conn.fetchval(
            "SELECT seq FROM hwm_transition_journal_v1 WHERE scope_sha256=$1",
            j._scope(self.peak_key),
        ))

    async def test_concurrent_writers_cannot_double_commit(self):
        await self.anchor()
        import asyncpg
        other = await asyncpg.connect(os.environ["TEST_POSTGRES_DSN"])
        try:
            async def contender(conn):
                try:
                    async with conn.transaction():
                        return await j.commit_transition_in_tx(
                            conn, peak_key=self.peak_key, provenance_key=self.prov_key,
                            expected_peak_raw=self.orig,
                            expected_provenance_raw=self.orig_prov,
                            new_peak="24", account_equity="24",
                            reason="new_equity_high",
                            evidence_ref="test:authenticated_equity",
                            event_id=str(uuid.uuid4()),
                        )
                except j.JournalIntegrityError:
                    return "CAS_REJECTED"
            result = await asyncio.gather(contender(self.conn), contender(other))
            self.assertEqual(sum(isinstance(x, dict) for x in result), 1)
            self.assertIn("CAS_REJECTED", result)
        finally:
            await other.close()
        self.assertEqual(
            (await j.verify_scope_from_pg(self.conn, peak_key=self.peak_key))["count"], 2
        )
        self.assertEqual(await self.current(), "24")


if __name__ == "__main__":
    unittest.main()
