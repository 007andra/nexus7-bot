"""Regression tests for strict same-cohort OOS evidence export; no real DB writes."""
from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch

from bot import prospective_oos_cohort_v1 as cohort
from research.oos_rca_v1.pg_matched_cohort_export import build_export, export_readonly


META = {
    "cohort_id": cohort.COHORT_ID, "started_epoch": 1000,
    "discovery_cutoff_epoch": 1000,
    "hypothesis_frozen": True, "reset_allowed": False,
}


def candidate(cid, allowed, *, cohort_name=None, epoch=1100):
    cf = {"cohort": cohort_name or cohort.COHORT, "candidate_id": cid,
          "risk_epoch_traversal_credit": False, "execution_allowed": allowed,
          "status": "APPROVED" if allowed else "REJECTED"}
    payload = {
        "candidate_id": cid, "captured_epoch": epoch, "population": cohort.POPULATION,
        "symbol": "SOLUSDT", "side": "SHORT", "regime": "TRENDING_DOWN",
        "setup": "BOS_BREAK", "shadow_only": True, "live_eligible": False,
        "counterfactual_nexus_v1": cf,
    }
    return {"candidate_id": cid, "payload": json.dumps(payload)}


def outcome(cid, horizon=60, *, ret=0.01, state="OBSERVED",
            start=1800, basis="hypothetical_entry_gross"):
    payload = {
        "candidate_id": cid, "horizon": horizon, "outcome": state,
        "observation_start": start, "future_return": ret,
        "MFE": 0.02, "MAE": -0.03, "return_basis": basis,
    }
    return {"candidate_id": cid, "horizon": horizon, "payload": json.dumps(payload)}


class TestFrozenOOSExport(unittest.TestCase):
    def test_approved_vs_rejected_same_cohort(self):
        rows, report = build_export(META, [candidate("a", True), candidate("b", False)],
                                    [outcome("a"), outcome("b", ret=-0.01)], as_of=5500)
        self.assertEqual(len(rows), 4)
        self.assertEqual(report["groups"]["60"]["COUNTERFACTUAL_APPROVED"]["verified"], 1)
        self.assertEqual(report["groups"]["60"]["COUNTERFACTUAL_REJECTED"]["avg_return_gross"], -0.01)

    def test_other_cohort_never_mixed(self):
        _, report = build_export(META, [candidate("a", True), candidate("v2", True,
                                             cohort_name="OTHER_COHORT")], [], as_of=5500)
        self.assertEqual(report["included_candidates"], 1)
        self.assertEqual(report["excluded"]["NON_OOS"], 1)

    def test_unknown_cache_gap_not_zero(self):
        rows, _ = build_export(META, [candidate("a", True)],
                               [outcome("a", state="UNKNOWN_CACHE_GAP")], as_of=5500)
        self.assertFalse(rows[0]["verified"])
        self.assertEqual(rows[0]["missing_reason"], "UNKNOWN_CACHE_GAP")
        self.assertIsNone(rows[0]["future_return_gross"])

    def test_outcome_immature_never_verified(self):
        rows, _ = build_export(META, [candidate("a", True)],
                               [outcome("a", start=4500)], as_of=5500)
        self.assertFalse(rows[0]["verified"])
        self.assertIn("IMMATURE", rows[0]["missing_reason"])

    def test_missing_horizon_does_not_impute(self):
        rows, _ = build_export(META, [candidate("a", True)], [outcome("a")], as_of=5500)
        self.assertIsNone(rows[1]["future_return_gross"])
        self.assertEqual(rows[1]["missing_reason"], "NOT_MATURED")

    def test_wrong_return_basis_rejected(self):
        rows, _ = build_export(META, [candidate("a", True)],
                               [outcome("a", basis="executed_net")], as_of=5500)
        self.assertFalse(rows[0]["verified"])
        self.assertEqual(rows[0]["missing_reason"], "RETURN_BASIS_NOT_GROSS")

    def test_duplicate_candidate_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "DUPLICATE"):
            build_export(META, [candidate("a", True), candidate("a", True)], [], as_of=5500)

    def test_metadata_change_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "COHORT_METADATA_INVALID"):
            build_export({**META, "hypothesis_frozen": False}, [candidate("a", True)], [], as_of=5500)

    def test_non_matching_candidate_identity(self):
        other = candidate("a", True)
        changed = json.loads(other["payload"])
        changed["counterfactual_nexus_v1"]["candidate_id"] = "OTHER"
        other["payload"] = json.dumps(changed)
        _, report = build_export(META, [other], [], as_of=5500)
        self.assertEqual(report["included_candidates"], 0)
        self.assertEqual(report["excluded"]["IDENTITY"], 1)

    def test_deterministic_hash(self):
        c = [candidate("a", True), candidate("b", False)]
        o = [outcome("a"), outcome("b", ret=-0.01)]
        _, first = build_export(META, c, o, as_of=5500)
        _, second = build_export(META, list(reversed(c)), list(reversed(o)), as_of=5500)
        self.assertEqual(first["rows_sha256"], second["rows_sha256"])

    def test_postgres_transaction_is_readonly_and_select_only(self):
        class Transaction:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False

        class Connection:
            def __init__(self):
                self.readonly = None
                self.queries = []
                self.closed = False
            def transaction(self, **kwargs):
                self.readonly = kwargs
                return Transaction()
            async def fetchrow(self, sql, *args):
                self.queries.append(sql)
                return {"payload": json.dumps(META)}
            async def fetch(self, sql, *args):
                self.queries.append(sql)
                if "FROM hard_gate_shadow_candidates_v1" in sql:
                    return [candidate("a", True)]
                return [outcome("a")]
            async def close(self):
                self.closed = True

        connection = Connection()
        async def fake_connect(dsn, **kwargs):
            return connection
        fake_module = types.SimpleNamespace(connect=fake_connect)
        with patch.dict(sys.modules, {"asyncpg": fake_module}):
            rows, report = asyncio.run(export_readonly("postgresql://example/unused", 5500))
        self.assertEqual(connection.readonly, {"isolation": "repeatable_read", "readonly": True})
        self.assertTrue(all(query.lstrip().upper().startswith("SELECT") for query in connection.queries))
        self.assertTrue(connection.closed)
        self.assertEqual(report["groups"]["60"]["COUNTERFACTUAL_APPROVED"]["verified"], 1)


if __name__ == "__main__":
    unittest.main()
