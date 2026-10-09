"""Synthetic and isolated REAL PostgreSQL proofs for read-only HWM preflight."""
import asyncio
import json
import os
import unittest
import uuid
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlsplit

from bot import hwm_legacy_migration_preflight_ro_v1 as pre

CP = "test:hwm:canonical:peak"
CV = "test:hwm:canonical:provenance"
LP = "test:hwm:legacy:peak"
LV = "test:hwm:legacy:provenance"
KP = {
    "canonical": (CP, CV), "legacy": (LP, LV),
    "v1": (pre.V1_PEAK, pre.V1_PROV),
}


def snapshot(peak="22.79869386"):
    return json.dumps({
        "version": 1,
        "new_peak": float(peak),
        "recorded_at": "2026-10-09T18:00:00+00:00",
        "reason": "external_capital_flow_rebase",
        "evidence_ref": "private:TRAN_ID_12345678",
        "execution_effect": "NONE",
    })


def valid_legacy():
    return {LP: "22.79869386", LV: snapshot()}


def local_database_only():
    try:
        u = urlsplit(os.getenv("TEST_POSTGRES_DSN", ""))
        return u.hostname in {"127.0.0.1", "localhost"} and u.port == 5432 and u.path == "/nexus_release_proof"
    except ValueError:
        return False


class PureNamespaceReview(unittest.TestCase):
    def review(self, rows, keys=KP):
        return pre.review_values(rows, keys=keys)

    def test_legacy_pair_detected_not_migrated(self):
        r = self.review(valid_legacy())
        self.assertEqual(r["status"], "CONSISTENT_LEGACY_MANUAL_MIGRATION_REQUIRED")
        self.assertEqual(r["effective_source"], "LEGACY")
        self.assertEqual(r["effective_peak_usdt"], 22.79869386)
        self.assertEqual(r["scope_state"]["canonical"], "ABSENT")
        self.assertEqual(r["scope_state"]["legacy"], "PAIR_VALID")
        self.assertFalse(r["anchor_created"])
        self.assertFalse(r["backup_available_verified"])
        self.assertFalse(r["migration_authorized"])
        self.assertFalse(r["historic_provenance_complete"])
        self.assertFalse(r["live_allowed"])

    def test_canonical_sole_scope_requires_anchor_review(self):
        d = {CP: "22.79869386", CV: snapshot()}
        r = self.review(d)
        self.assertEqual(r["status"], "CANONICAL_PAIR_PRESENT_REVIEW_ANCHOR_REQUIRED")
        self.assertEqual(r["effective_source"], "CANONICAL")

    def test_dual_scope_blocks_even_identical_values(self):
        d = valid_legacy()
        d.update({CP: "22.79869386", CV: snapshot()})
        r = self.review(d)
        self.assertIn("DUAL_CANONICAL_LEGACY_SCOPE_REVIEW_REQUIRED", r["blockers"])
        self.assertEqual(r["status"], "MIGRATION_PREFLIGHT_BLOCKED")

    def test_unpaired_and_no_value_fail_closed(self):
        for d in ({}, {LP: "22.79869386"}, {LV: snapshot()}):
            with self.subTest(d=d):
                self.assertEqual(self.review(d)["status"], "MIGRATION_PREFLIGHT_BLOCKED")

    def test_provenance_mismatch_invalid_timestamp_and_wrong_reason(self):
        candidates = [
            json.dumps(dict(json.loads(snapshot()), new_peak=5)),
            json.dumps(dict(json.loads(snapshot()), recorded_at="2026-10-09T18:00:00")),
            json.dumps(dict(json.loads(snapshot()), reason="fake")),
            json.dumps(dict(json.loads(snapshot()), execution_effect="SUBMIT_ORDER")),
            '{"version": 1, "recorded_at": [], "new_peak": 22}',
            '{"version": 1, "recorded_at": 7, "new_peak": 22}',
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                r = self.review({LP: "22.79869386", LV: candidate})
                self.assertIn("LEGACY_INVALID_OR_DIVERGENT", r["blockers"])

    def test_v1_record_blocks_even_valid_legacy(self):
        d = valid_legacy()
        d.update({pre.V1_PEAK: "22.79869386", pre.V1_PROV: snapshot()})
        self.assertIn("ANCIENT_V1_NAMESPACE_REVIEW_REQUIRED", self.review(d)["blockers"])

    def test_mapping_missing_or_colliding_fails_closed(self):
        for keys in (
            dict(KP, legacy=(None, None)),
            dict(KP, legacy=(CP, CV)),
            dict(KP, legacy=(LP, LP)),
        ):
            with self.subTest(keys=keys):
                self.assertEqual(self.review(valid_legacy(), keys)["status"], "MIGRATION_PREFLIGHT_BLOCKED")

    def test_unconfigured_canonical_account_blocks(self):
        k = dict(KP, canonical=("v3:account=UNCONFIGURED", CV))
        self.assertIn("ACTIVE_ACCOUNT_NAMESPACE_UNCONFIGURED", self.review(valid_legacy(), k)["blockers"])

    def test_redacted_report_contains_no_source_identity(self):
        result = json.dumps(self.review(valid_legacy()))
        for forbidden in (LP, LV, CP, CV, "TRAN_ID_12345678", "private:", "test:hwm:"):
            self.assertNotIn(forbidden, result)

    def test_only_one_select_and_bounded_timeout(self):
        class Connection:
            calls = []
            async def fetch(self, sql, args):
                self.calls.append((sql, args))
                return [{"key": k, "value": v} for k, v in valid_legacy().items()]
        conn = Connection()
        result = asyncio.run(pre.collect_once(conn, keys=KP))
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(conn.calls[0][0], pre.SQL)
        self.assertTrue(pre.SQL.strip().upper().startswith("SELECT"))
        self.assertNotIn("UPDATE", pre.SQL)
        self.assertEqual(pre.SETTINGS["default_transaction_read_only"], "on")
        self.assertEqual(pre.SETTINGS["statement_timeout"], "700")
        self.assertFalse(result["migration_authorized"])

    def test_exception_messages_never_leak_credentials(self):
        async def fail():
            raise RuntimeError("postgres://user:secret-password@db.example/private")
        with patch.object(pre, "_run", fail):
            capture = StringIO()
            with redirect_stdout(capture):
                status = pre.main()
        self.assertEqual(status, 2)
        self.assertNotIn("secret-password", capture.getvalue())
        self.assertFalse(json.loads(capture.getvalue())["live_allowed"])


@unittest.skipUnless(local_database_only(), "LOCAL ONLY disposable PostgreSQL gate")
class RealPgReadOnlyProof(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_connection_with_real_physical_keys(self):
        import asyncpg
        dsn = os.environ["TEST_POSTGRES_DSN"]
        prefix = "test:ro_namespace:" + str(uuid.uuid4())
        keys = {
            "canonical": (prefix + ":canonical:peak", prefix + ":canonical:prov"),
            "legacy": (prefix + ":legacy:peak", prefix + ":legacy:prov"),
            "v1": (prefix + ":v1:peak", prefix + ":v1:prov"),
        }
        setup = await asyncpg.connect(dsn)
        try:
            await setup.execute(
                "CREATE TABLE IF NOT EXISTS key_value "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
            )
            await setup.execute(
                "INSERT INTO key_value (key,value,updated_at) VALUES "
                "($1,$2,'fixture'),($3,$4,'fixture')",
                keys["legacy"][0], "22.79869386",
                keys["legacy"][1], snapshot(),
            )
        finally:
            await setup.close()
        conn = await asyncpg.connect(dsn, server_settings=pre.SETTINGS)
        try:
            self.assertEqual(
                await conn.fetchval("SELECT current_setting('transaction_read_only')"),
                "on",
            )
            result = await pre.collect_once(conn, keys=keys)
            self.assertEqual(result["status"], "CONSISTENT_LEGACY_MANUAL_MIGRATION_REQUIRED")
            with self.assertRaises(asyncpg.exceptions.ReadOnlySQLTransactionError):
                await conn.execute(
                    "UPDATE key_value SET value='99' WHERE key=$1",
                    keys["legacy"][0],
                )
            self.assertEqual(
                await conn.fetchval("SELECT value FROM key_value WHERE key=$1", keys["legacy"][0]),
                "22.79869386",
            )
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
