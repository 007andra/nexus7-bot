"""Offline regression checks for frozen-cohort PostgreSQL CSV integrity."""
import unittest
import re
from pathlib import Path
from research.oos_rca_v1.verify_export import validate, APPROVED, REJECTED


def sample(cid="HARD_GATE_SHADOW:BTCUSDT:SHORT:BOS_BREAK:1", h=60,
           decision=APPROVED, state="OBSERVED", gain="0.01"):
    return {
        "cohort_id":"CALIBRATION_GENERALIZATION_V1",
        "snapshot_as_of_epoch":"20000", "frozen_started_epoch":"1000",
        "candidate_id":cid, "captured_epoch":"1001",
        "captured_epoch_real":"1001", "export_scope":"JSON_PRECISE",
        "counterfactual_status":"REJECTED" if decision == REJECTED else "APPROVED",
        "execution_allowed_source":"true" if decision == APPROVED else "false",
        "outcome_payload_parse_ok":"true",
        "symbol":"BTCUSDT",
        "side":"SHORT", "regime":"TRENDING_DOWN", "setup":"BOS_BREAK",
        "decision_state":decision, "outcome_horizon_minutes":str(h),
        "outcome_state":state, "return_basis":"hypothetical_entry_gross",
        "hypothetical_gross_return":gain, "hypothetical_mfe":"0.02",
        "hypothetical_mae":"-0.01", "observation_start_epoch":"1800",
        "outcome_identity_ok":"true", "outcome_horizon_ok":"true",
    }


class TestPostgresCsvIntegrity(unittest.TestCase):
    def test_approved_rejected_same_cohort(self):
        cid="HARD_GATE_SHADOW:ETHUSDT:SHORT:BOS_BREAK:1"
        result=validate([sample(),sample(h=240),
                         sample(cid=cid,decision=REJECTED),
                         sample(cid=cid,h=240,decision=REJECTED)])
        self.assertTrue(result["passed_schema_and_identity"])
        self.assertEqual(result["unique_candidates"],2)
        self.assertEqual(result["outcomes_matching_production_validator"]["60"][APPROVED]["n"],1)
        self.assertEqual(result["outcomes_matching_production_validator"]["60"][REJECTED]["n"],1)

    def test_missing_not_imputed_zero(self):
        result=validate([sample(state="UNKNOWN_CACHE_GAP"),
                         sample(h=240,state="MISSING")])
        self.assertIsNone(result["outcomes_matching_production_validator"]["60"][APPROVED]["mean_gross_fraction"])

    def test_duplicate_horizon_fails(self):
        self.assertFalse(validate([sample(),sample(),sample(h=240)])["passed_schema_and_identity"])

    def test_future_outcome_not_observed(self):
        later=sample(h=240)
        later["observation_start_epoch"]="10000"
        result=validate([sample(),later])
        self.assertEqual(result["outcomes_matching_production_validator"]["240"][APPROVED]["n"],0)

    def test_snapshots_are_atomic(self):
        later=sample(h=240);later["snapshot_as_of_epoch"]="21000"
        self.assertFalse(validate([sample(),later])["passed_schema_and_identity"])

    def test_candidate_meta_conflict_fails(self):
        later=sample(h=240);later["side"]="LONG"
        self.assertFalse(validate([sample(),later])["passed_schema_and_identity"])

    def test_outcome_identity_mismatch_fails(self):
        wrong=sample();wrong["outcome_identity_ok"]="false"
        self.assertFalse(validate([wrong,sample(h=240)])["passed_schema_and_identity"])

    def test_zero_is_not_positive(self):
        result=validate([sample(gain="0"),sample(h=240,state="MISSING")])
        self.assertEqual(result["outcomes_matching_production_validator"]["60"][APPROVED]["positive_fraction"],0)

    def test_wrong_cohort_fails(self):
        wrong=sample();wrong["cohort_id"]="OTHER"
        self.assertFalse(validate([wrong,sample(h=240)])["passed_schema_and_identity"])

    def test_missing_gross_basis_flagged(self):
        wrong=sample();wrong["return_basis"]=""
        result=validate([wrong,sample(h=240)])
        self.assertEqual(result["observed_missing_or_different_return_basis"],1)

    def test_absent_execution_allowed_is_rejected_in_production(self):
        records = [sample(decision=REJECTED),sample(h=240,decision=REJECTED)]
        for row in records:
            row["execution_allowed_source"] = ""
        report = validate(records)
        self.assertTrue(report["passed_schema_and_identity"])
        self.assertEqual(report["outcomes_matching_production_validator"]["60"][REJECTED]["n"], 1)

    def test_missing_allowed_mislabelled_indeterminate_is_rejected(self):
        records = [sample(decision=REJECTED),sample(h=240,decision=REJECTED)]
        for row in records:
            row["execution_allowed_source"] = ""
            row["decision_state"] = "INDETERMINATE"
        self.assertFalse(validate(records)["passed_schema_and_identity"])

    def test_real_cutoff_includes_legacy_coarse_member(self):
        records = [sample(), sample(h=240)]
        for row in records:
            row["snapshot_as_of_epoch"] = "2000000000"
            row["frozen_started_epoch"] = "1791211311.675"
            row["captured_epoch"] = "1791211270"
            row["captured_epoch_real"] = "1791211264"
            row["export_scope"] = "REAL_COARSE"
            row["observation_start_epoch"] = "1791211400"
        report = validate(records)
        self.assertTrue(report["passed_schema_and_identity"])
        self.assertEqual(report["export_scope"], "REAL_COARSE")

    def test_json_cutoff_rejects_legacy_coarse_member(self):
        records = [sample(), sample(h=240)]
        for row in records:
            row["snapshot_as_of_epoch"] = "2000000000"
            row["frozen_started_epoch"] = "1791211311.675"
            row["captured_epoch"] = "1791211270"
            row["captured_epoch_real"] = "1791211264"
            row["export_scope"] = "JSON_PRECISE"
        self.assertFalse(validate(records)["passed_schema_and_identity"])

    def test_mixed_cuts_not_comparable(self):
        records = [sample(), sample(h=240)]
        records[1]["export_scope"] = "REAL_COARSE"
        self.assertFalse(validate(records)["passed_schema_and_identity"])

    def test_legacy_error_excludes_observed_even_when_rejected(self):
        records = [sample(decision=REJECTED), sample(h=240,decision=REJECTED)]
        for row in records:
            row["export_scope"] = "REAL_COARSE"
            row["counterfactual_status"] = "ERROR"
        report = validate(records)
        self.assertTrue(report["passed_schema_and_identity"])
        self.assertEqual(report["outcomes_matching_production_validator"]["60"][REJECTED]["n"],0)
        self.assertEqual(report["decision_counts"][REJECTED],1)

    def test_bad_outcome_json_is_explicit_integrity_problem(self):
        records = [sample(),sample(h=240)]
        records[0]["outcome_payload_parse_ok"] = "false"
        records[0]["outcome_state"] = "MALFORMED_JSON"
        self.assertFalse(validate(records)["passed_schema_and_identity"])



    def test_sql_scopes_distinct_and_readonly(self):
        root=Path(__file__).resolve().parents[1] / "research" / "oos_rca_v1"
        js=(root/"EXPORT_OOS_POSTGRES_READ_ONLY_SELECT.sql").read_text()
        real=(root/"EXPORT_OOS_POSTGRES_LEGACY_REAL_SELECT.sql").read_text()
        self.assertIn("'JSON_PRECISE' AS export_scope",js)
        self.assertIn("'REAL_COARSE' AS export_scope",real)
        self.assertIn("c.captured_epoch >= ((f.metadata ->> 'started_epoch')::real)",real)
        self.assertIn("(p.signal->>'captured_epoch')::double precision >=",js)
        self.assertNotIn("c.captured_epoch >= ((f.metadata ->> 'started_epoch')::real)",js)
        for sql in (js,real):
            self.assertIn("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",sql)
            self.assertIn("ROLLBACK;",sql)
            self.assertNotIn("THEN 'INDETERMINATE'",sql)
            self.assertIn("ELSE 'COUNTERFACTUAL_REJECTED'",sql)
            self.assertIn("pg_input_is_valid(",sql)
            self.assertIn("MALFORMED_JSON",sql)

    def test_client_side_copy_has_same_query_as_sql_select(self):
        root=Path(__file__).resolve().parents[1] / "research" / "oos_rca_v1"
        for select_path,copy_path in (
            ("EXPORT_OOS_POSTGRES_READ_ONLY_SELECT.sql","EXPORT_OOS_POSTGRES_PSQL_COPY.sql"),
            ("EXPORT_OOS_POSTGRES_LEGACY_REAL_SELECT.sql","EXPORT_OOS_POSTGRES_LEGACY_REAL_COPY.sql")
        ):
            src=(root/select_path).read_text()
            copy=(root/copy_path).read_text()
            self.assertIn("\\set ON_ERROR_STOP on",copy)
            self.assertIn("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",copy)
            self.assertIn("ROLLBACK;",copy)
            self.assertIn("\\copy (",copy)
            first=src.index("WITH frozen_source AS (")
            last=src.rindex(";\n\nROLLBACK;")
            query=re.sub(r"\s+"," ",re.sub(r"--[^\n]*","",src[first:last]).strip())
            inner=copy.split("\\copy (",1)[1].split(") TO 'private_oos_evidence/",1)[0]
            self.assertEqual(query,inner)


    def test_legacy_observed_without_start_still_counts(self):
        # Legacy _outcome_map validates finite return/MFE/MAE, not start.
        records=[sample(),sample(h=240)]
        for r in records:
            r["export_scope"]="REAL_COARSE"
            r["observation_start_epoch"]=""
        out=validate(records)
        self.assertTrue(out["passed_schema_and_identity"])
        self.assertEqual(out["outcomes_matching_production_validator"]["60"][APPROVED]["n"],1)

    def test_precise_observed_without_start_is_not_verified(self):
        records=[sample(),sample(h=240)]
        for r in records:
            r["observation_start_epoch"]=""
        out=validate(records)
        self.assertEqual(out["outcomes_matching_production_validator"]["60"][APPROVED]["n"],0)



if __name__=="__main__":
    unittest.main()
