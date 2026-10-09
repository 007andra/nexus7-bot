"""PostgreSQL 16 proof of BOTH immutable OOS exports vs production algorithms.

Runs ONLY with TEST_POSTGRES_DSN on GitHub Actions' disposable PostgreSQL service.
Creates isolated schema + synthetic records using real bot DDL, then invokes the
exact checked-in psql COPY files under a SELECT-only database role. No Railway
access, no private account info, no real trading or production mutations.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from bot import hard_gate_shadow_scan as shadow
from bot import prospective_oos_cohort_v1 as oos
from bot import prospective_oos_maturation_review_v1 as review
from research.oos_rca_v1.verify_export import validate, APPROVED, REJECTED

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "oos_proof_fixture_readonly"
ROLE = "oos_proof_selectonly"
STARTED = 1791211311.675
BASELINE = {
    **oos.AUTHORITY, "cohort_id": oos.COHORT_ID,
    "started_epoch": STARTED, "discovery_cutoff_epoch": STARTED,
    "hypothesis": oos.FROZEN_HYPOTHESIS,
    "hypothesis_frozen": True, "reset_allowed": False,
}
SQL_FILES = {
    "JSON_PRECISE": "EXPORT_OOS_POSTGRES_PSQL_COPY.sql",
    "REAL_COARSE": "EXPORT_OOS_POSTGRES_LEGACY_REAL_COPY.sql",
}
CSV_FILES = {
    "JSON_PRECISE": "nexus7_oos_coorte.csv",
    "REAL_COARSE": "nexus7_oos_coorte_legacy_real.csv",
}


def _candidate(i, *, captured, allowed, status=None):
    side = "SHORT" if i % 2 else "LONG"
    symbol = "SOLUSDT" if i % 3 else "ETHUSDT"
    setup = "BOS_BREAK"
    cid = f"HARD_GATE_SHADOW:{symbol}:{side}:{setup}:{900000+i}"
    cf = {
        "cohort": oos.COHORT,
        "candidate_id": cid,
        "symbol": symbol, "side": side, "setup": setup,
        "regime": "TRENDING_DOWN" if side == "SHORT" else "TRENDING_UP",
        "status": status or ("APPROVED" if allowed else "REJECTED"),
        "risk_epoch_traversal_credit": False,
    }
    if allowed is not None:
        cf["execution_allowed"] = allowed
    payload = {
        "candidate_id": cid, "captured_epoch": captured,
        "population": oos.POPULATION,
        "symbol": symbol, "side": side, "setup": setup,
        "regime": cf["regime"], "shadow_only": True, "live_eligible": False,
        "entry": 100.0, "stop": 105.0 if side == "SHORT" else 95.0,
        "target": 90.0 if side == "SHORT" else 110.0,
        "counterfactual_nexus_v1": cf,
    }
    return cid, payload


def _outcome(cid, horizon, captured, *, missing_start=False):
    start = math.ceil(captured / 900) * 900
    out = {
        "candidate_id": cid, "horizon": horizon, "outcome": "OBSERVED",
        "future_return": 0.002 if horizon == 60 else -0.003,
        "MFE": 0.01, "MAE": -0.005,
        "return_basis": "hypothetical_entry_gross",
    }
    if not missing_start:
        out["observation_start"] = start
    return out


class Postgres16OOSParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.getenv("TEST_POSTGRES_DSN"):
            raise RuntimeError("TEST_POSTGRES_DSN required; never connect to production")
        if shutil.which("psql") is None:
            raise RuntimeError("psql required for exact client-side COPY proof")
        if not os.getenv("CI"):
            raise RuntimeError("CI flag required: no non-disposable DB allowed")
        cls.dsn = os.environ["TEST_POSTGRES_DSN"]
        if "127.0.0.1" not in cls.dsn or "/nexus_release_proof" not in cls.dsn:
            raise RuntimeError("refusing non-local or non-CI PostgreSQL target")
        cls.rows = asyncio.run(cls._seed())
        cls.exported = {}
        for scope, file in SQL_FILES.items():
            script = ROOT / "research" / "oos_rca_v1" / file
            outpath = ROOT / "private_oos_evidence" / CSV_FILES[scope]
            outpath.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["psql", cls.dsn, "-X", "-v", "ON_ERROR_STOP=1",
                   "-c", f"SET ROLE {ROLE}",
                   "-c", f"SET search_path TO {SCHEMA}",
                   "-f", str(script)]
            outcome = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                     timeout=45, check=False)
            if outcome.returncode != 0:
                raise AssertionError(
                    f"{scope} exact COPY failed: " + outcome.stderr[-1200:])
            with outpath.open(newline="", encoding="utf-8") as f:
                cls.exported[scope] = list(csv.DictReader(f))
            outpath.unlink()

    @classmethod
    async def _seed(cls):
        import asyncpg
        conn = await asyncpg.connect(cls.dsn, command_timeout=20)
        try:
            await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            await conn.execute(f"CREATE SCHEMA {SCHEMA}")
            await conn.execute(f"SET search_path TO {SCHEMA}")
            for ddl in (shadow._TABLE, shadow._OUTCOMES, oos._META):
                await conn.execute(ddl)
            await conn.execute(
                "INSERT INTO prospective_oos_cohort_v1(cohort_id,started_epoch,payload) "
                "VALUES ($1,$2,$3)", oos.COHORT_ID, STARTED, json.dumps(BASELINE))
            rows = []
            # 7 natural approvals, 7 natural rejects; 1 missing execution_allowed
            # must be rejected. 1 ERROR candidate counts as enrolled in both.
            # 1 coarse REAL boundary candidate belongs to legacy only.
            for i in range(16):
                allowed = True if i < 7 else False if i < 14 else None
                captured = STARTED + 900 + i * 900
                cid, obj = _candidate(i, captured=captured, allowed=allowed,
                                      status="ERROR" if i == 15 else None)
                rows.append((cid, obj, i))
            boundary_id, boundary = _candidate(
                17, captured=STARTED - 40, allowed=True)
            rows.append((boundary_id, boundary, 17))
            for cid, payload, i in rows:
                await conn.execute(
                    "INSERT INTO hard_gate_shadow_candidates_v1 "
                    "(candidate_id,captured_epoch,symbol,population,payload) "
                    "VALUES ($1,$2,$3,$4,$5)",
                    cid, payload["captured_epoch"], payload["symbol"], oos.POPULATION,
                    json.dumps(payload))
                for h in (60, 240):
                    # One missing observation_start tests the legacy rules.
                    out = _outcome(cid, h, payload["captured_epoch"],
                                   missing_start=(i == 14 and h == 60))
                    await conn.execute(
                        "INSERT INTO hard_gate_shadow_outcomes_v1 "
                        "(candidate_id,horizon,population,payload) VALUES($1,$2,$3,$4)",
                        cid, h, oos.POPULATION, json.dumps(out))
            # Genuine malformed payload should NOT abort either SELECT/COPY.
            await conn.execute(
                "INSERT INTO hard_gate_shadow_candidates_v1 "
                "(candidate_id,captured_epoch,symbol,population,payload) "
                "VALUES ($1,$2,$3,$4,$5)",
                "HARD_GATE_SHADOW:BROKEN:LONG:BOS_BREAK:999999",
                STARTED+5000, "BROKEN", oos.POPULATION, "{not valid json")
            # Role cannot write or read other schemas: grant only this fixture.
            role_exists = await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname=$1", ROLE)
            if role_exists is None:
                await conn.execute(f"CREATE ROLE {ROLE} NOLOGIN")
            await conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}")
            await conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {ROLE}")
            return rows
        finally:
            await conn.close()

    def test_two_scopes_disagree_only_at_rounding_boundary(self):
        j, r = self.exported["JSON_PRECISE"], self.exported["REAL_COARSE"]
        jc = {x["candidate_id"] for x in j}
        rc = {x["candidate_id"] for x in r}
        self.assertEqual(len(j), 16*2)
        self.assertEqual(len(r), 17*2)
        self.assertEqual(len(rc-jc), 1)
        self.assertEqual(len(jc), 16)
        self.assertEqual(len(rc), 17)
        self.assertTrue(any("900017" in cid for cid in rc-jc))

    def test_both_exports_validate_without_stale_scope_mix(self):
        for scope, records in self.exported.items():
            report = validate(records)
            self.assertTrue(report["passed_schema_and_identity"], (scope, report["integrity_problems"]))
            self.assertEqual(report["export_scope"], scope)
            self.assertEqual(report["malformed_candidate_payloads_skipped_in_population"], 1)
            self.assertEqual(report["decision_counts"][REJECTED], 9)
            self.assertEqual(report["decision_counts"][APPROVED], 7 if scope=="JSON_PRECISE" else 8)

    def test_canonical_legacy_build_report_equivalent(self):
        from bot import counterfactual_approval_failure_analysis_v1 as base
        # Mirror OOS.snapshot PostgreSQL REAL cutoff and JSON parse behavior.
        candidates = []
        map60, map240 = [], []
        for cid, obj, i in self.rows:
            stored_f4 = float(__import__("struct").unpack("!f",
                       __import__("struct").pack("!f", obj["captured_epoch"]))[0])
            cutoff_f4 = float(__import__("struct").unpack("!f",
                       __import__("struct").pack("!f", STARTED))[0])
            if stored_f4 < cutoff_f4:
                continue
            candidates.append(obj)
            map60.append(_outcome(cid,60,obj["captured_epoch"],
                                  missing_start=(i==14)))
            map240.append(_outcome(cid,240,obj["captured_epoch"]))
        official = oos.build_report(candidates, map60, map240, baseline=BASELINE)
        result = validate(self.exported["REAL_COARSE"])
        self.assertEqual(result["unique_candidates"],official["enrolled_candidates"])
        for h in (60,240):
            a,r = official[f"allowed_{h}m"],official[f"rejected_{h}m"]
            self.assertEqual(result["outcomes_matching_production_validator"][str(h)][APPROVED]["n"],a["n"])
            self.assertEqual(result["outcomes_matching_production_validator"][str(h)][REJECTED]["n"],r["n"])
            self.assertAlmostEqual(result["outcomes_matching_production_validator"][str(h)][APPROVED]["mean_gross_fraction"],a["avg_return"])

    def test_canonical_precise_maturation_evaluate_equivalent(self):
        in_scope = [obj for cid,obj,i in self.rows
                    if obj["captured_epoch"]+1e-9>=STARTED]
        for obj in in_scope:
            obj["_review_table_candidate_id"] = obj["candidate_id"]
        m60 = {obj["candidate_id"]:_outcome(obj["candidate_id"],60,obj["captured_epoch"],
                                             missing_start=(i==14))
               for cid,obj,i in self.rows if obj["captured_epoch"]+1e-9>=STARTED}
        m240 = {obj["candidate_id"]:_outcome(obj["candidate_id"],240,obj["captured_epoch"])
                for cid,obj,i in self.rows if obj["captured_epoch"]+1e-9>=STARTED}
        as_of = float(self.exported["JSON_PRECISE"][0]["snapshot_as_of_epoch"])
        official = review.evaluate(in_scope,m60,m240,{},BASELINE,now_epoch=as_of)
        exported = validate(self.exported["JSON_PRECISE"])
        self.assertEqual(exported["unique_candidates"],official["enrolled_candidates"])
        self.assertEqual(exported["decision_counts"][APPROVED],official["approved_candidates"])
        self.assertEqual(exported["decision_counts"][REJECTED],official["rejected_candidates"])
        for h in (60,240):
            for decision,prefix in ((APPROVED,"approved"),(REJECTED,"rejected")):
                expected = official[f"{prefix}_{h}m"]
                observed = exported["outcomes_matching_production_validator"][str(h)][decision]
                self.assertEqual(observed["n"],expected["n"])
                self.assertAlmostEqual(observed["mean_gross_fraction"],expected["avg_return"])

    def test_select_only_role_cannot_delete_and_readonly_prevents_writes(self):
        args = ["psql",self.dsn,"-X","-v","ON_ERROR_STOP=1",
                "-c",f"SET ROLE {ROLE}",
                "-c",f"SET search_path TO {SCHEMA}",
                "-c","BEGIN TRANSACTION READ ONLY",
                "-c","DELETE FROM hard_gate_shadow_candidates_v1"]
        p = subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertNotEqual(p.returncode,0)
        self.assertIn("read-only",p.stderr.lower())

