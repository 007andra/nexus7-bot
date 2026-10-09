"""Prove both live HWM persistence primitives share one fail-closed journal.

Disposable, local PostgreSQL only. No Railway or exchange APIs are touched.
"""
import asyncio
import json
import os
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit

from bot import database as db
from bot import hwm_namespace
from bot import financial_namespace
from bot import hwm_provenance as provenance
from bot import hwm_transition_journal_v1 as journal
from bot.atomic_key_value import (
    CompareAndSwapConflict, save_key_values_atomic, save_key_values_atomic_cas,
)


def isolated_pg():
    try:
        u = urlsplit(os.environ.get("TEST_POSTGRES_DSN", ""))
        return (u.hostname in ("127.0.0.1", "localhost")
                and u.port == 5432 and u.path == "/nexus_release_proof")
    except ValueError:
        return False


@unittest.skipUnless(isolated_pg(), "local disposable PostgreSQL mandatory")
class AllWriterJournalAtomicityPG(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.admin = await asyncpg.connect(os.environ["TEST_POSTGRES_DSN"])
        await self.admin.execute(
            "CREATE TABLE IF NOT EXISTS key_value "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        await self.admin.execute(journal.SCHEMA_SQL)
        ident = str(uuid.uuid4())
        self.peak_key = "test:allwriter:peak:" + ident
        self.prov_key = "test:allwriter:prov:" + ident
        self.ledger = "test:allwriter:ledger:" + ident
        self.cursor = "test:allwriter:cursor:" + ident
        self.marker = "test:allwriter:marker:" + ident
        self.legacy_key = "test:allwriter:legacy:" + ident
        self.prior = "22.79869386"
        self.prev_prov = provenance.build_hwm_provenance(
            old_peak=21, new_peak=22.79869386,
            account_equity=21, reason="new_equity_high",
            evidence_ref="synthetic:test_initial_state",
        )
        await self.admin.execute(
            "INSERT INTO key_value (key,value,updated_at) VALUES "
            "($1,$2,'test'),($3,$4,'test')",
            self.peak_key, self.prior, self.prov_key, self.prev_prov,
        )
        self.mocks = [
            patch.object(hwm_namespace, "equity_peak_key", return_value=self.peak_key),
            patch.object(hwm_namespace, "provenance_key", return_value=self.prov_key),
            patch.object(financial_namespace, "legacy_key_for", return_value=None),
            patch.dict(os.environ, {"HWM_JOURNAL_INTEGRATION_V1": "true"}),
            patch.object(db, "_conn", self.admin),
            patch.object(db, "_is_pg", True),
            patch.object(db, "DATABASE_URL", os.environ["TEST_POSTGRES_DSN"]),
            patch.object(db, "_io_lock", asyncio.Lock()),
        ]
        for p in self.mocks:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.mocks):
            p.stop()
        await self.admin.close()

    async def val(self, key):
        return await self.admin.fetchval("SELECT value FROM key_value WHERE key=$1", key)

    async def anchor(self):
        async with self.admin.transaction():
            await journal.anchor_current_peak_in_tx(
                self.admin, peak_key=self.peak_key, provenance_key=self.prov_key,
                event_id=str(uuid.uuid4()), evidence_ref="synthetic:future_only_anchor",
            )

    def new_event(self, value=24.0, reason="new_equity_high"):
        evidence = "synthetic:authenticated_equity_proof"
        new_prov = provenance.build_hwm_provenance(
            old_peak=22.79869386, new_peak=value,
            account_equity=24.0 if value >= 24 else 15.0,
            reason=reason, evidence_ref=evidence,
        )
        event = {
            "old_peak": 22.79869386,
            "account_equity": 24.0 if value >= 24 else 15.0,
            "reason": reason,
            "evidence_ref": evidence,
        }
        return [(self.peak_key, format(value, ".17g")),
                (self.prov_key, new_prov)], event

    async def test_atomic_hwm_writer_records_exactly_one_transition(self):
        await self.anchor()
        pairs, event = self.new_event()
        ok = await save_key_values_atomic(
            pairs, strict=True, hwm_transition=event,
        )
        self.assertTrue(ok)
        self.assertEqual(await self.val(self.peak_key), "24")
        report = await journal.verify_scope_from_pg(self.admin, peak_key=self.peak_key)
        self.assertEqual(report["count"], 2)
        self.assertFalse(report["history_before_anchor_verified"])

    async def test_cas_cashflow_ledger_and_cursor_use_one_commit(self):
        await self.anchor()
        pairs, event = self.new_event(value=18.0, reason="external_capital_flow_rebase")
        pairs = [(self.ledger, "two applied source events"),
                 (self.cursor, "exchange cursor checkpoint")] + pairs
        ok = await save_key_values_atomic_cas(
            pairs, expected={
                self.peak_key: self.prior,
                self.ledger: None, self.cursor: None,
            }, strict=True, hwm_transition=event,
        )
        self.assertTrue(ok)
        self.assertEqual(await self.val(self.ledger), "two applied source events")
        self.assertEqual(await self.val(self.cursor), "exchange cursor checkpoint")
        self.assertEqual(await self.val(self.peak_key), "18")
        report = await journal.verify_scope_from_pg(self.admin, peak_key=self.peak_key)
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["last_peak"], "18")

    async def test_cashflow_rolls_back_if_not_anchored(self):
        pairs, event = self.new_event()
        pairs += [(self.ledger, "MUST_NOT_COMMIT")]
        with self.assertRaises(db.PersistenceError):
            await save_key_values_atomic_cas(
                pairs, expected={self.peak_key: self.prior, self.ledger: None},
                strict=False, hwm_transition=event,
            )
        self.assertIsNone(await self.val(self.ledger))
        self.assertEqual(await self.val(self.peak_key), self.prior)

    async def test_wrong_current_provenance_causes_zero_writes(self):
        await self.anchor()
        pairs, event = self.new_event()
        pairs = [(self.marker, "CONSUMED")] + pairs
        broken = list(pairs)
        i = next(n for n,(k,_) in enumerate(broken) if k == self.prov_key)
        broken[i] = (self.prov_key, json.dumps({"version":1,"reason":"FAKE"}))
        with self.assertRaises(db.PersistenceError):
            await save_key_values_atomic_cas(
                broken, expected={
                    self.peak_key: self.prior, self.marker: None,
                }, strict=True, hwm_transition=event,
            )
        self.assertIsNone(await self.val(self.marker))
        self.assertEqual(
            (await journal.verify_scope_from_pg(self.admin,peak_key=self.peak_key))["count"], 1
        )

    async def test_stale_cas_conflict_preserves_history_and_ledger(self):
        await self.anchor()
        pairs, event = self.new_event()
        with self.assertRaises(CompareAndSwapConflict):
            await save_key_values_atomic_cas(
                [(self.ledger, "MUST_NOT_COMMIT")] + pairs,
                expected={self.peak_key: "historically_stale", self.ledger: None},
                strict=True, hwm_transition=event,
            )
        self.assertEqual(await self.val(self.peak_key), self.prior)
        self.assertIsNone(await self.val(self.ledger))
        self.assertEqual(
            (await journal.verify_scope_from_pg(self.admin,peak_key=self.peak_key))["count"], 1
        )

    async def test_legacy_existing_blocks_activation_without_migration(self):
        await self.anchor()
        await self.admin.execute(
            "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3)",
            self.legacy_key, self.prior, "test",
        )
        pairs, event = self.new_event()
        with patch.object(financial_namespace, "legacy_key_for",
                          return_value=self.legacy_key):
            with self.assertRaises(db.PersistenceError):
                await save_key_values_atomic(
                    pairs, strict=True, hwm_transition=event,
                )
        self.assertEqual(await self.val(self.peak_key), self.prior)
        self.assertEqual(
            (await journal.verify_scope_from_pg(self.admin,peak_key=self.peak_key))["count"], 1
        )

    async def test_missing_hwm_provenance_pair_and_evidence_fail(self):
        await self.anchor()
        pairs, event = self.new_event()
        for sample, metadata in (
            (pairs[:1], event),
            (pairs, None),
        ):
            with self.subTest(sample=len(sample),meta=bool(metadata)):
                with self.assertRaises(db.PersistenceError):
                    await save_key_values_atomic(
                        sample, strict=False, hwm_transition=metadata,
                    )
        self.assertEqual(await self.val(self.peak_key), self.prior)

    async def test_parallel_cas_writers_only_one_event(self):
        await self.anchor()
        pairs, event = self.new_event()
        async def candidate(marker):
            try:
                await save_key_values_atomic_cas(
                    [(self.marker, marker)] + pairs,
                    expected={self.peak_key: self.prior, self.marker: None},
                    strict=True, hwm_transition=event,
                )
                return "COMMITTED"
            except CompareAndSwapConflict:
                return "STALE"
        result = await asyncio.gather(candidate("first"), candidate("second"))
        self.assertEqual(sorted(result), ["COMMITTED", "STALE"])
        self.assertEqual(
            (await journal.verify_scope_from_pg(self.admin,peak_key=self.peak_key))["count"], 2
        )

    async def test_legacy_incident_direct_peak_write_cannot_bypass_journal(self):
        await self.anchor()
        for key in (self.peak_key, self.prov_key):
            with self.subTest(key_kind="hwm" if key == self.peak_key else "provenance"):
                with self.assertRaisesRegex(db.PersistenceError, "un-journaled"):
                    await db.save_key_value(key, "99", strict=False)
        with patch.object(financial_namespace, "legacy_key_for",
                          return_value=self.legacy_key):
            with self.assertRaisesRegex(db.PersistenceError, "un-journaled"):
                await db.save_key_value(self.legacy_key, "99", strict=True)
        self.assertEqual(await self.val(self.peak_key), self.prior)
        self.assertIsNone(await self.val(self.legacy_key))
        self.assertEqual(
            (await journal.verify_scope_from_pg(
                self.admin, peak_key=self.peak_key,
            ))["count"], 1
        )

    async def test_opt_out_behavior_does_not_write_journal(self):
        pairs, event = self.new_event()
        with patch.dict(os.environ, {"HWM_JOURNAL_INTEGRATION_V1": "false"}):
            ok = await save_key_values_atomic(pairs, strict=True,
                                               hwm_transition=event)
        self.assertTrue(ok)
        self.assertEqual(await self.val(self.peak_key), "24")
        self.assertIsNone(await self.admin.fetchval(
            "SELECT seq FROM hwm_transition_journal_v1 WHERE scope_sha256=$1",
            journal._scope(self.peak_key),
        ))


if __name__ == "__main__":
    unittest.main()
