"""Tests for read-only cohort-path inputs and separately archived candles."""
import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch

from bot import prospective_oos_cohort_v1 as cohort
from research.oos_rca_v1.join_candles import attach
from research.oos_rca_v1.pg_path_inputs_export import prepare, export_inputs


META = {"cohort_id": cohort.COHORT_ID, "started_epoch": 1000.0,
        "discovery_cutoff_epoch": 1000.0, "hypothesis_frozen": True,
        "reset_allowed": False}


def candidate(cid="A", *, allowed=True, levels=True):
    obj = {
        "candidate_id": cid, "captured_epoch": 1100.0,
        "symbol": "BTCUSDT", "side": "LONG", "regime": "TRENDING_UP",
        "setup": "BOS_BREAK", "shadow_only": True, "live_eligible": False,
        "counterfactual_nexus_v1": {
            "candidate_id": cid, "cohort": cohort.COHORT,
            "risk_epoch_traversal_credit": False,
            "status": "APPROVED" if allowed else "REJECTED",
            "execution_allowed": allowed,
        },
        "cost_snapshot": {
            "candidate_id": cid, "symbol": "BTCUSDT", "exchange": "BINANCE",
            "observed_at": 1099, "taker_fee": .0005,
            "entry_slippage": .0002, "exit_slippage": .0003,
            "secret": "SHOULD_NOT_EXPORT",
        },
    }
    if levels:
        obj.update(entry=100, stop=95, target=110)
    return {"candidate_id": cid, "payload": json.dumps(obj)}


def bar(ts, symbol="BTCUSDT"):
    return {"symbol": symbol, "ts": ts, "o": 100, "h": 102, "l": 98, "c": 101}


class TestPathInputs(unittest.TestCase):
    def test_approved_and_rejected_same_cohort(self):
        rows, m = prepare(META, [candidate("A"), candidate("B", allowed=False)], as_of_epoch=9000)
        self.assertEqual(m["exported"], 2)
        self.assertEqual([x["cohort_decision"] for x in rows],
                         ["COUNTERFACTUAL_APPROVED", "COUNTERFACTUAL_REJECTED"])
        self.assertTrue(all("bars" not in r for r in rows))

    def test_cannot_leak_extra_cost_fields(self):
        rows, _ = prepare(META, [candidate()], as_of_epoch=9000)
        self.assertNotIn("secret", rows[0]["cost_snapshot"])

    def test_no_levels_marked_as_excluded(self):
        rows, manifest = prepare(META, [candidate(levels=False)], as_of_epoch=9000)
        self.assertEqual(len(rows), 0)
        self.assertEqual(manifest["excluded"]["INVALID_ENTRY_STOP_TARGET"], 1)

    def test_invalid_identity_not_exported(self):
        c = candidate()
        p = json.loads(c["payload"])
        p["counterfactual_nexus_v1"]["candidate_id"] = "B"
        c["payload"] = json.dumps(p)
        rows, m = prepare(META, [c], as_of_epoch=9000)
        self.assertEqual(rows, [])
        self.assertEqual(m["excluded"]["IDENTITY"], 1)

    def test_future_capture_excluded(self):
        rows, m = prepare(META, [candidate()], as_of_epoch=1050)
        self.assertEqual(len(rows), 0)
        self.assertEqual(m["excluded"]["FUTURE_CAPTURE"], 1)

    def test_metadata_not_frozen_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "INVALID_FROZEN_METADATA"):
            prepare({**META, "hypothesis_frozen": False}, [candidate()], as_of_epoch=9000)

    def test_readonly_transaction_selects_only(self):
        class Context:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
        class Conn:
            def __init__(self):
                self.kw = None
                self.queries = []
                self.closed = False
            def transaction(self, **kwargs):
                self.kw = kwargs
                return Context()
            async def fetchrow(self, sql, *args):
                self.queries.append(sql)
                return {"payload": json.dumps(META)}
            async def fetch(self, sql, *args):
                self.queries.append(sql)
                return [candidate()]
            async def close(self):
                self.closed = True
        conn = Conn()
        async def fake_connect(*args, **kwargs):
            return conn
        with patch.dict(sys.modules, {"asyncpg": types.SimpleNamespace(connect=fake_connect)}):
            rows, m = asyncio.run(export_inputs("postgresql://example/db", 9000))
        self.assertEqual(m["exported"], 1)
        self.assertTrue(conn.closed)
        self.assertEqual(conn.kw, {"readonly": True, "isolation": "repeatable_read"})
        self.assertTrue(all(q.lstrip().upper().startswith("SELECT") for q in conn.queries))

    def test_complete_join_has_sixteen_bars(self):
        candidates, _ = prepare(META, [candidate()], as_of_epoch=9000)
        joined, summary = attach(candidates, [bar(t) for t in range(1800, 16200, 900)])
        self.assertEqual(len(joined[0]["bars"]), 16)
        self.assertEqual(summary["candidate_with_full_240m_path"], 1)
        self.assertEqual(joined[0]["bars"][0]["ts"], 1800)

    def test_join_other_symbol_excluded(self):
        candidates, _ = prepare(META, [candidate()], as_of_epoch=9000)
        joined, summary = attach(candidates, [bar(t, "ETHUSDT") for t in range(1800, 16200, 900)])
        self.assertEqual(joined[0]["bars"], [])
        self.assertEqual(summary["total_missing_expected_240m_bars"], 16)

    def test_join_preserves_missingness(self):
        candidates, _ = prepare(META, [candidate()], as_of_epoch=9000)
        ts = [t for t in range(1800, 16200, 900) if t != 3600]
        joined, summary = attach(candidates, [bar(t) for t in ts])
        self.assertEqual(len(joined[0]["bars"]), 15)
        self.assertEqual(summary["total_missing_expected_240m_bars"], 1)

    def test_duplicate_market_bar_fails(self):
        candidates, _ = prepare(META, [candidate()], as_of_epoch=9000)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_SYMBOL_TIMESTAMP"):
            attach(candidates, [bar(1800), bar(1800)])

    def test_duplicate_candidates_fail(self):
        candidates, _ = prepare(META, [candidate()], as_of_epoch=9000)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_CANDIDATE_ID"):
            attach(candidates + candidates, [bar(1800)])

    def test_source_does_not_import_exchange(self):
        candidates, _ = prepare(META, [candidate()], as_of_epoch=9000)
        _, summary = attach(candidates, [bar(1800)])
        self.assertFalse(summary["live_allowed"])


if __name__ == "__main__":
    unittest.main()
