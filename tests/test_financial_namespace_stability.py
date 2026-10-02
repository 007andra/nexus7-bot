"""NOVO-03 — durable financial state survives Railway project/env/service changes.

Same database + same exchange account + different RAILWAY_* identifiers =>
the same financial namespace, and state written by the previous release under
the Railway-scoped key is still read (migration). Different accounts never
collide.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import confirmed_rr_exit, daily_pnl_storage, database as db, durable_daily_stop
from bot import financial_namespace as fn
from bot import hwm_namespace

BASE = {"DATABASE_URL": "postgresql://u:p@db.internal:5432/nexus", "EXCHANGE": "binance",
        "BINANCE_API_KEY": "acct-1"}
PROJ_A = {"RAILWAY_PROJECT_ID": "proj-a", "RAILWAY_SERVICE_ID": "svc-a", "RAILWAY_ENVIRONMENT_ID": "env-a",
          "RAILWAY_ENVIRONMENT_NAME": "production"}
PROJ_B = {"RAILWAY_PROJECT_ID": "proj-b", "RAILWAY_SERVICE_ID": "svc-b", "RAILWAY_ENVIRONMENT_ID": "env-b",
          "RAILWAY_ENVIRONMENT_NAME": "production-moved"}


class FakeStore:
    def __init__(self):
        self.data = {}

    async def raw(self, key, strict=False):
        return self.data.get(key)


class FinancialNamespaceTests(unittest.TestCase):
    def keys(self, extra):
        with patch.dict(os.environ, {**BASE, **extra}, clear=True):
            return (durable_daily_stop.state_key("2026-10-02"), daily_pnl_storage.ledger_key("2026-10-02"),
                    hwm_namespace.equity_peak_key(),
                    confirmed_rr_exit.identity("ETHUSDT", SimpleNamespace(
                        _forensic_lineage={"opening_order_id": "123"}))[0])

    def test_railway_move_keeps_every_financial_key(self):
        self.assertEqual(self.keys(PROJ_A), self.keys(PROJ_B))

    def test_distinct_accounts_never_collide(self):
        a = self.keys({**PROJ_A, "BINANCE_API_KEY": "acct-1"})
        b = self.keys({**PROJ_A, "BINANCE_API_KEY": "acct-2"})
        for left, right in zip(a, b):
            self.assertNotEqual(left, right)

    def test_distinct_databases_never_collide(self):
        a = self.keys(PROJ_A)
        b = self.keys({**PROJ_A, "DATABASE_URL": "postgresql://u:p@other.internal:5432/nexus"})
        self.assertNotEqual(a, b)

    def test_secret_never_in_key(self):
        for key in self.keys(PROJ_A):
            self.assertNotIn("acct-1", key)
            self.assertNotIn("u:p@", key)


class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def _load(self, store, key):
        with patch.object(db, "_load_key_value_raw", side_effect=store.raw):
            return await db.load_key_value(key, strict=True)

    async def test_daily_stop_written_by_previous_release_is_still_read(self):
        store = FakeStore()
        with patch.dict(os.environ, {**BASE, **PROJ_A}, clear=True):
            legacy = "daily_stop_v3:" + fn.legacy_railway_scope() + ":2026-10-02"
            store.data[legacy] = '{"triggered":true}'
            new = durable_daily_stop.state_key("2026-10-02")
            self.assertNotEqual(new, legacy)
            self.assertEqual(await self._load(store, new), '{"triggered":true}')

    async def test_hwm_peak_written_by_previous_release_is_still_read(self):
        store = FakeStore()
        with patch.dict(os.environ, {**BASE, **PROJ_A}, clear=True):
            legacy = "risk:account_equity_peak:" + hwm_namespace.legacy_hwm_namespace()
            store.data[legacy] = "1234.5"
            self.assertEqual(await self._load(store, hwm_namespace.equity_peak_key()), "1234.5")

    async def test_new_key_wins_once_written(self):
        store = FakeStore()
        with patch.dict(os.environ, {**BASE, **PROJ_A}, clear=True):
            new = durable_daily_stop.state_key("2026-10-02")
            store.data["daily_stop_v3:" + fn.legacy_railway_scope() + ":2026-10-02"] = "old"
            store.data[new] = "new"
            self.assertEqual(await self._load(store, new), "new")

    async def test_in_flight_partial_exit_keeps_its_legacy_identity(self):
        pos = SimpleNamespace(_forensic_lineage={"opening_order_id": "777"})
        with patch.dict(os.environ, {**BASE, **PROJ_A}, clear=True):
            legacy_key, legacy_idem = confirmed_rr_exit._legacy_identity("ETHUSDT", pos)
            legacy_key = legacy_key.replace("rr_exit_v1:", "partial_exit_v1:")
            legacy_idem = legacy_idem.replace("rr-", "partial-", 1)
            stored = json.dumps({"idem": legacy_idem, "status": "SUBMITTED"})
            store = {legacy_key: stored}

            async def load(key, strict=False):
                return store.get(key)
            with patch.object(db, "load_key_value", side_effect=load):
                key, idem, raw = await confirmed_rr_exit.durable_identity("ETHUSDT", pos, "partial")
        self.assertEqual((key, idem, raw), (legacy_key, legacy_idem, stored))

    async def test_unrelated_missing_key_stays_missing(self):
        store = FakeStore()
        with patch.dict(os.environ, {**BASE, **PROJ_A}, clear=True):
            self.assertIsNone(await self._load(store, "some_other_key"))


if __name__ == "__main__":
    unittest.main()
