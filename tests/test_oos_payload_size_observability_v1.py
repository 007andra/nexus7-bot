"""Read-only research timing: aggregate payload bytes with no row-content logging.

Offline-only regression suite. No Railway DB, Binance or trade execution.
"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from bot.prospective_oos_snapshot_timing_v1 import SnapshotTimingProbe
from bot import prospective_oos_cohort_v1 as cohort


class PayloadProbeUnitTests(unittest.TestCase):
    def test_stage_boundaries_unicode_and_privacy(self):
        probe = SnapshotTimingProbe(clock=lambda: 12)
        private = '{"candidate_id":"SECRET_ID_987","label":"ação"}'
        other = b'{"x":2}'
        probe.record_payload_size("candidates", private)
        probe.record_payload_size("candidates", other)
        probe.record_payload_size("outcomes", '{"return":0.3}')
        probe.record_payload_size("metadata", "no-metadata-payload")
        probe.record_payload_size("candidates", None)
        fields = probe.fields(status="OK")
        self.assertEqual(fields["candidate_json_utf8_bytes"],
                         len(private.encode("utf-8")) + len(other))
        self.assertEqual(fields["candidate_json_measured_rows"], 2)
        self.assertEqual(fields["candidate_json_max_utf8_bytes"],
                         len(private.encode("utf-8")))
        self.assertEqual(fields["outcome_json_measured_rows"], 1)
        self.assertEqual(fields["outcome_json_utf8_bytes"],
                         len('{"return":0.3}'.encode("utf-8")))
        self.assertEqual(fields["json_size_scope"],
                         "CLIENT_JSON_UTF8_ONLY_NOT_WIRE_OR_PG_EXECUTION")
        recorded = probe.log_line(status="OK")
        self.assertNotIn("SECRET_ID_987", recorded)
        self.assertNotIn("ação", recorded)
        self.assertNotIn("no-metadata-payload", recorded)

    def test_zero_sizes_do_not_affect_existing_read_metrics(self):
        probe = SnapshotTimingProbe(clock=lambda: 100)
        before = probe.fields(status="OK")
        probe.record_payload_size("candidates", "SENSITIVE_VALUE")
        after = probe.fields(status="OK")
        for key in ("db_fetch_ms", "lock_wait_ms", "rows_fetched",
                    "metadata_ms", "candidates_ms", "outcomes_ms",
                    "compute_ms", "promotion_allowed", "live_allowed",
                    "execution_effect", "decision_effect"):
            self.assertEqual(after[key], before[key])


class PayloadProbeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_still_builds_identical_report_with_probe(self):
        private_candidate = json.dumps({"candidate_id": "PRIVATE-C", "side": "SHORT"})
        utf8_candidate = json.dumps({"candidate_id": "PRIVATE-D", "tag": "ação"},
                                    ensure_ascii=False)
        invalid_candidate = '{"malformed":'
        private_outcome = json.dumps({"future_return": -0.2, "info": "PRIVATE-O"})
        invalid_outcome = 'not-json'

        class FakeDB:
            async def _fetchall(self, sql, params=()):
                if sql.startswith("SELECT payload FROM hard_gate_shadow_candidates_v1"):
                    return [{"payload": private_candidate},
                            {"payload": utf8_candidate},
                            {"payload": invalid_candidate}]
                if sql.startswith("SELECT o.candidate_id,o.horizon,o.payload"):
                    return [
                        {"candidate_id": "PRIVATE-C", "horizon": 60,
                         "payload": private_outcome},
                        {"candidate_id": "PRIVATE-C", "horizon": 240,
                         "payload": invalid_outcome},
                    ]
                raise AssertionError("UNEXPECTED_SQL")

        def fake_report(payloads, o60, o240, *, baseline):
            return {"population": payloads, "observed60": o60,
                    "observed240": o240, "baseline": baseline}

        fake_meta = AsyncMock(return_value={"started_epoch": 1000})
        db = FakeDB()
        with (patch.object(cohort, "load_frozen_metadata_for_snapshot", fake_meta),
              patch.object(cohort, "build_report", side_effect=fake_report)):
            no_probe_report = await cohort.snapshot(db)
            timing = SnapshotTimingProbe()
            same_report = await cohort.snapshot(db, timing_probe=timing)
        self.assertEqual(no_probe_report, same_report)
        fields = timing.fields(status="OK")
        self.assertEqual(fields["candidate_json_measured_rows"], 3)
        self.assertEqual(fields["outcome_json_measured_rows"], 2)
        self.assertEqual(fields["candidate_json_utf8_bytes"],
                         sum(len(raw.encode("utf-8")) for raw in
                             (private_candidate, utf8_candidate, invalid_candidate)))
        self.assertEqual(fields["outcome_json_utf8_bytes"],
                         sum(len(raw.encode("utf-8")) for raw in
                             (private_outcome, invalid_outcome)))
        log = timing.log_line(status="OK")
        for secret in ("PRIVATE-C", "PRIVATE-D", "PRIVATE-O", "ação", "malformed"):
            self.assertNotIn(secret, log)
        self.assertFalse(fields["live_allowed"])
        self.assertFalse(fields["promotion_allowed"])
        self.assertEqual(fields["execution_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
