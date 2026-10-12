"""Standalone transaction/witness safety tests for V3.1 isolated research DB.

The fake DB proves adapter call order, isolation and rollback contract. It is
NOT a substitute for real PostgreSQL DDL/role/grants integration tests.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock
from dataclasses import asdict

from bot.short_down_bos_v31_readonly_enrollment import (
    EnrollmentAuthority, PREREG_EPOCH, RosterEvidenceError,
)
from bot.short_down_bos_v31_manifest import PROTOCOL, empty_manifest
from bot.short_down_bos_v31_postgres_adapter import (
    PriorHeadWitness, append_isolated_transaction, verify_isolated_transaction,
)


def authority():
    return EnrollmentAuthority(
        "a" * 40, PREREG_EPOCH + 100, PREREG_EPOCH + 200,
        "v31-human-approved-offline-only",
    )


def candidate(i, sym="BTCUSDT"):
    return {
        "candidate_id": f"PRIVATE_V31_ID_{i}",
        "captured_epoch": PREREG_EPOCH + 300 + i,
        "symbol": sym, "entry": 100, "stop": 102, "target": 96,
        "population": "HARD_GATE_SHADOW",
        "side": "SHORT", "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "nexus_called": True, "nexus_allowed": True,
        "shadow_only": True, "live_eligible": False,
        "decision_effect": "NONE", "execution_effect": "NONE",
    }


class FakeTx:
    def __init__(self, conn, opts):
        self.conn, self.opts = conn, opts
    async def __aenter__(self):
        self.conn.tx_opts = self.opts
        self.conn.in_tx = True
        self.start = deepcopy((self.conn.head, self.conn.events))
        return self
    async def __aexit__(self, kind, error, tb):
        if kind is not None:
            self.conn.head, self.conn.events = deepcopy(self.start)
        self.conn.in_tx = False
        return False


class FakeIsolatedDb:
    def __init__(self, *, dbname="nexus7_research_offline", role="nexus_v31_writer",
                 readonly="off", genesis=True):
        self.dbname, self.role, self.readonly = dbname, role, readonly
        self.tx_opts = None
        self.in_tx = False
        self.statements = []
        self.force_cas_failure = False
        self.events = []
        genesis_row = empty_manifest(authority())
        self.head = {
            "protocol": PROTOCOL,
            "effective_cutoff_epoch": genesis_row.effective_cutoff_epoch,
            "implementation_sha": genesis_row.implementation_sha,
            "authorization_reference": genesis_row.authorization_reference,
            "genesis_digest": genesis_row.genesis_digest,
            "sequence": 0,
            "head_digest": genesis_row.genesis_digest,
        } if genesis else None

    def transaction(self, **opts):
        return FakeTx(self, opts)

    async def fetchrow(self, sql, *params):
        self.statements.append(("fetchrow", sql))
        assert self.in_tx
        if "current_database()" in sql:
            return {"dbname": self.dbname, "username": self.role,
                    "readonly": self.readonly}
        if "FROM v31_research.manifest_heads" in sql:
            if self.head and params != (PROTOCOL,):
                raise AssertionError("incorrect protocol")
            return deepcopy(self.head)
        raise AssertionError("UNEXPECTED_QUERY")

    async def fetch(self, sql, *params):
        self.statements.append(("fetch", sql))
        assert self.in_tx and params == (PROTOCOL,)
        if "FROM v31_research.manifest_events" not in sql:
            raise AssertionError("UNEXPECTED_QUERY")
        return deepcopy(self.events)

    async def execute(self, sql, *params):
        self.statements.append(("execute", sql))
        assert self.in_tx
        if sql.startswith("INSERT INTO v31_research.manifest_events"):
            protocol, seq, cid, serialized, digest = params
            assert protocol == PROTOCOL
            assert seq == len(self.events) + 1
            assert not any(row["candidate_id"] == cid for row in self.events)
            self.events.append({
                "sequence": seq, "candidate_id": cid,
                "event_data": serialized, "digest": digest,
            })
            return "INSERT 0 1"
        if sql.startswith("UPDATE v31_research.manifest_heads"):
            protocol, seq, digest, prev_seq, prev_hash = params
            if self.force_cas_failure:
                return "UPDATE 0"
            assert protocol == PROTOCOL
            assert self.head["sequence"] == prev_seq
            assert self.head["head_digest"] == prev_hash
            assert seq == prev_seq + 1
            self.head["sequence"] = seq
            self.head["head_digest"] = digest
            return "UPDATE 1"
        raise AssertionError("MUTATION_OUTSIDE_RESEARCH_TABLE")


def witness(conn):
    return PriorHeadWitness(conn.head["head_digest"],
                            "offline-test-external-independent-witness")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_append_and_new_head_required_for_next_step(self):
        db = FakeIsolatedDb()
        old = witness(db)
        result = await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(1), witness=old,
        )
        self.assertEqual(result["status"], "TRANSACTION_COMMITTED_ANCHOR_PENDING")
        self.assertEqual(result["sequence"], 1)
        self.assertFalse(result["roster_durable"])
        self.assertFalse(result["live_allowed"])
        self.assertEqual(db.tx_opts, {"isolation": "serializable"})
        self.assertEqual(db.head["head_digest"], result["head_digest"])
        with self.assertRaisesRegex(RosterEvidenceError, "EXTERNAL_ANCHOR"):
            await append_isolated_transaction(
                db, authority=authority(), raw_candidate=candidate(2), witness=old,
            )
        self.assertEqual(len(db.events), 1)
        updated = await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(2), witness=witness(db),
        )
        self.assertEqual(updated["sequence"], 2)
        self.assertEqual(len(db.events), 2)

    async def test_identical_replay_does_not_modify_database(self):
        db = FakeIsolatedDb()
        await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(1), witness=witness(db),
        )
        before = len([x for x in db.statements if x[0] == "execute"])
        x = await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(1), witness=witness(db),
        )
        self.assertEqual(x["status"], "IDEMPOTENT_READ_ONLY")
        self.assertEqual(len([x for x in db.statements if x[0] == "execute"]), before)

    async def test_conflicting_duplicate_rolls_back(self):
        db = FakeIsolatedDb()
        await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(1), witness=witness(db),
        )
        bad = candidate(1)
        bad["stop"] = 103
        with self.assertRaisesRegex(RosterEvidenceError, "CONFLICTING_CANDIDATE"):
            await append_isolated_transaction(db, authority=authority(),
                                              raw_candidate=bad, witness=witness(db))
        self.assertEqual(len(db.events), 1)

    async def test_stale_order_does_not_append(self):
        db = FakeIsolatedDb()
        await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(10), witness=witness(db),
        )
        with self.assertRaisesRegex(RosterEvidenceError, "LATE_ARRIVAL"):
            await append_isolated_transaction(
                db, authority=authority(), raw_candidate=candidate(1),
                witness=witness(db),
            )
        self.assertEqual(len(db.events), 1)

    async def test_cas_failure_rolls_back_insert(self):
        db = FakeIsolatedDb()
        db.force_cas_failure = True
        with self.assertRaisesRegex(RosterEvidenceError, "DB_HEAD_CAS"):
            await append_isolated_transaction(
                db, authority=authority(), raw_candidate=candidate(1),
                witness=witness(db),
            )
        self.assertEqual(db.head["sequence"], 0)
        self.assertEqual(db.events, [])

    async def test_shared_database_or_trading_role_denied_before_mutation(self):
        for name, role in (
            ("nexus7_production", "nexus_v31_writer"),
            ("nexus7_research_offline", "nexus_trade_executor"),
            ("nexus7_research_offline", "postgres"),
        ):
            with self.subTest(db=name, role=role):
                db = FakeIsolatedDb(dbname=name, role=role)
                with self.assertRaisesRegex(RosterEvidenceError, "ISOLATED_RESEARCH"):
                    await append_isolated_transaction(
                        db, authority=authority(), raw_candidate=candidate(1),
                        witness=witness(db),
                    )
                self.assertFalse(db.events)
                self.assertFalse([s for s in db.statements if s[0] == "execute"])

    async def test_missing_genesis_fails_without_writes(self):
        db = FakeIsolatedDb(genesis=False)
        with self.assertRaisesRegex(RosterEvidenceError, "GENESIS_NOT_PROVISIONED"):
            await append_isolated_transaction(db, authority=authority(),
                                              raw_candidate=candidate(1),
                                              witness=PriorHeadWitness(
                                                  "f" * 64, "independent-witness"))
        self.assertFalse([s for s in db.statements if s[0] == "execute"])

    async def test_mutated_stored_event_rejected(self):
        db = FakeIsolatedDb()
        await append_isolated_transaction(
            db, authority=authority(), raw_candidate=candidate(1), witness=witness(db),
        )
        forged = json.loads(db.events[0]["event_data"])
        forged["stop"] = "103"
        db.events[0]["event_data"] = json.dumps(forged)
        with self.assertRaisesRegex(RosterEvidenceError, "EVENT_HASH"):
            await append_isolated_transaction(
                db, authority=authority(), raw_candidate=candidate(2),
                witness=witness(db),
            )

    async def test_read_only_audit_requires_independent_witness(self):
        db = FakeIsolatedDb()
        result = await verify_isolated_transaction(
            db, authority=authority(), witness=witness(db),
        )
        self.assertEqual(result["observed_events"], 0)
        self.assertEqual(db.tx_opts,
                         {"isolation": "repeatable_read", "readonly": True})
        self.assertFalse([s for s in db.statements if s[0] == "execute"])

    async def test_missing_witness_is_invalid(self):
        db = FakeIsolatedDb()
        for bad in (PriorHeadWitness("foo", "witness-test"),
                    PriorHeadWitness("0" * 64, "")):
            with self.assertRaises(RosterEvidenceError):
                await append_isolated_transaction(db, authority=authority(),
                                                  raw_candidate=candidate(1),
                                                  witness=bad)
        self.assertFalse(db.statements)


if __name__ == "__main__":
    unittest.main()
