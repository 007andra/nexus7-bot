"""#666 frozen statistics and membership manifest; synthetic data only.

Synthetic returns test the arithmetic and fail-closed paths of the gate. They
are not market evidence and never count toward the 60-member sample.
"""
import json
import math
from pathlib import Path
import tempfile
import unittest

from bot import capital_free_market_manifest_666_v1 as manifest
from bot import capital_free_market_oos_666_v1 as study
from bot import capital_free_market_stats_666_v1 as stats
from tests.test_capital_free_market_oos_666_v1 import (
    BASE, FAKE_IMPL, exit_row, signal,
)

SYMBOLS = study.UNIVERSE[:10]


def cohort(n=60, symbols=SYMBOLS, spacing=4):
    """n members spread over symbols and distinct 4h buckets."""
    rows = []
    for i in range(n):
        captured = BASE + i * spacing * study.INTERVAL_MS
        rows.append(signal(i, symbol=symbols[i % len(symbols)],
                           captured_ms=captured))
    return rows


def paired_for(rows, ret60, ret240):
    selection = study.freeze_offline(
        rows, effective_cutoff_ms=study.ISSUE_CREATED_MS, source_complete=True)
    outcomes = {}
    for i, row in enumerate(selection["selected"]):
        for h, fn in ((60, ret60), (240, ret240)):
            r = fn(i, row)
            if r is not None:
                outcomes[(row["candidate_id"], h)] = exit_row(
                    row, h, row["entry_close"] * (1 - r))
    return study.paired_market_observations(selection, outcomes)


class FrozenConstantsTests(unittest.TestCase):
    def test_preregistered_constants(self):
        self.assertEqual(stats.REPLICATES, 10_000)
        self.assertEqual(stats.SEED, 609202613)
        self.assertEqual(stats.MIN_POSITIVE_FRACTION, 0.55)
        self.assertEqual(stats.MIN_TIME_CLUSTERS, 10)
        self.assertEqual(stats.CLUSTER_MS, 14_400_000)
        self.assertEqual(stats.HORIZONS, (60, 240))
        self.assertEqual(study.ISSUE_CREATED_MS, 1791898025000)  # 2026-10-13T13:27:05Z


class BootstrapTests(unittest.TestCase):
    def test_deterministic_and_seed_sensitive(self):
        obs = [((s, b), math.sin(7 * b + ord(s)) / 100.0)
               for s in "ABCDE" for b in range(6)]
        a, ma = stats.cluster_bootstrap_lower_bound(obs)
        b, mb = stats.cluster_bootstrap_lower_bound(obs)
        c, _ = stats.cluster_bootstrap_lower_bound(obs, seed=1)
        self.assertEqual((a, ma), (b, mb))
        self.assertEqual(len(ma), 10_000)
        self.assertEqual(a, ma[500])
        self.assertNotEqual(a, c)

    def test_empty_input_fails_closed(self):
        self.assertEqual(stats.cluster_bootstrap_lower_bound([])[0], float("-inf"))

    def test_empty_replicate_scores_negative_infinity(self):
        # Cannot happen with real clusters; the defined behavior must still
        # only lower the bound, never raise or inflate it.
        self.assertEqual(stats._mean([]), float("-inf"))
        bound, _ = stats.cluster_bootstrap_lower_bound(
            [(("A", 0), 0.1)], replicates=1)
        self.assertEqual(bound, 0.1)


class GateTests(unittest.TestCase):
    def test_strong_uniform_signal_is_review_ready_never_live(self):
        out = stats.evaluate(paired_for(cohort(), lambda i, r: 0.01 + (i % 3) * 0.001,
                                        lambda i, r: 0.02))
        self.assertEqual(out["status"], "MARKET_ONLY_REVIEW_READY")
        for h in (60, 240):
            self.assertTrue(out["horizons"][h]["passed"])
            self.assertGreater(out["horizons"][h]["bootstrap_lower_bound_95_one_sided"], 0)
        self.assertFalse(out["live_allowed"])
        self.assertFalse(out["executable_net_proven"])
        self.assertFalse(out["promotion_allowed"])

    def test_negative_mean_fails(self):
        out = stats.evaluate(paired_for(cohort(), lambda i, r: -0.01, lambda i, r: 0.02))
        self.assertEqual(out["status"], "MARKET_SIGNAL_EVIDENCE_FAIL")
        self.assertFalse(out["horizons"][60]["gates"]["mean_positive"])

    def test_positive_fraction_below_055_fails_despite_positive_mean(self):
        # 30/60 big winners, 30 small losers: mean > 0, fraction 0.50.
        out = stats.evaluate(paired_for(
            cohort(), lambda i, r: 0.05 if i % 2 else -0.001, lambda i, r: 0.02))
        h = out["horizons"][60]
        self.assertGreater(h["mean"], 0)
        self.assertFalse(h["gates"]["positive_fraction"])
        self.assertEqual(out["status"], "MARKET_SIGNAL_EVIDENCE_FAIL")

    def test_single_symbol_carrying_mean_fails_leave_one_out(self):
        top = SYMBOLS[0]
        out = stats.evaluate(paired_for(
            cohort(), lambda i, r: 0.5 if r["symbol"] == top else -0.002,
            lambda i, r: 0.02))
        h = out["horizons"][60]
        self.assertGreater(h["mean"], 0)
        self.assertLessEqual(h["leave_one_symbol_out_means"][top], 0)
        self.assertFalse(h["gates"]["leave_one_symbol_out_all_positive"])

    def test_too_few_time_clusters_fails(self):
        # Every member inside the same few 4h buckets.
        rows = [signal(i, symbol=SYMBOLS[i % 10],
                       captured_ms=BASE + (i // 10) * study.INTERVAL_MS)
                for i in range(60)]
        out = stats.evaluate(paired_for(rows, lambda i, r: 0.01, lambda i, r: 0.01))
        self.assertFalse(out["horizons"][60]["gates"]["min_time_clusters"])
        self.assertEqual(out["status"], "MARKET_SIGNAL_EVIDENCE_FAIL")

    def test_one_censored_pair_is_insufficient_not_fail(self):
        out = stats.evaluate(paired_for(
            cohort(), lambda i, r: 0.01, lambda i, r: None if i == 7 else 0.01))
        self.assertEqual(out["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(out["reason"], "DENOMINATOR_INCOMPLETE")

    def test_fewer_than_60_or_8_symbols_is_insufficient(self):
        out = stats.evaluate(paired_for(cohort(59), lambda i, r: 0.01, lambda i, r: 0.01))
        self.assertEqual(out["status"], "INSUFFICIENT_EVIDENCE")
        out = stats.evaluate(paired_for(cohort(symbols=SYMBOLS[:5]),
                                        lambda i, r: 0.01, lambda i, r: 0.01))
        self.assertEqual(out["status"], "INSUFFICIENT_EVIDENCE")

    def test_forged_or_tampered_input_rejected(self):
        good = paired_for(cohort(), lambda i, r: 0.01, lambda i, r: 0.01)
        for mutate in (
            lambda p: p.update(live_allowed=True),
            lambda p: p.update(reason="NET_PROFIT"),
            lambda p: p.update(pairs=list(p["pairs"])),
            lambda p: p.update(pairs=p["pairs"][:-1] + (dict(
                p["pairs"][-1], executable_net_proven=True),)),
            lambda p: p.update(pairs=p["pairs"][:-1] + (dict(
                p["pairs"][-1], raw_short_return_60m=float("nan")),)),
            lambda p: p.update(pairs=p["pairs"][:-1] + (p["pairs"][0],)),
        ):
            bad = dict(good)
            mutate(bad)
            self.assertEqual(stats.evaluate(bad)["status"],
                             "INSUFFICIENT_EVIDENCE")


class ManifestTests(unittest.TestCase):
    T0 = BASE - study.INTERVAL_MS

    def _new(self, d):
        path = Path(d) / "m.jsonl"
        manifest.create(path, effective_t0_ms=self.T0,
                        implementation_sha=FAKE_IMPL, recorded_ms=self.T0 - 5)
        return path

    def test_enrollment_caps_duplicates_and_target_are_recorded_before_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            kinds = []
            for row in cohort(64):
                kinds.append(manifest.append(path, row,
                                             recorded_ms=row["captured_ms"] + 2000))
            kinds.append(manifest.append(path, cohort(64)[-1],
                                         recorded_ms=cohort(64)[-1]["captured_ms"] + 3000))
            self.assertEqual(kinds.count("ENROLLED"), 60)
            self.assertEqual(kinds.count("EXCLUDED_POST_TARGET"), 4)
            self.assertEqual(kinds[-1], "EXCLUDED_DUPLICATE")
            out = manifest.verify(path)
            self.assertEqual(out["status"], "MANIFEST_SELF_CHAIN_VERIFIED_UNANCHORED")
            self.assertEqual(out["enrolled_count"], 60)
            self.assertTrue(out["frozen_first_60_shape"])
            self.assertFalse(out["cohort_membership_proven"])
            self.assertFalse(out["external_anchor_published"])
            for line in path.read_bytes().splitlines()[1:]:
                self.assertNotIn(b"exit_close", line)
                self.assertNotIn(b"raw_short_return", line)

    def test_symbol_cap_of_12(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            rows = [signal(i, symbol="BTCUSDT") for i in range(13)]
            kinds = [manifest.append(path, r, recorded_ms=r["captured_ms"] + 1)
                     for r in rows]
            self.assertEqual(kinds[-1], "EXCLUDED_SYMBOL_CAP")
            self.assertEqual(kinds.count("ENROLLED"), 12)

    def test_pre_t0_late_out_of_order_and_outcome_fields_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            early = signal(0, captured_ms=self.T0)
            self.assertEqual(manifest.append(path, early, recorded_ms=self.T0 + 1),
                             "EXCLUDED_INVALID")
            row = signal(5)
            late = row["captured_ms"] + study.INTERVAL_MS
            self.assertEqual(manifest.append(path, row, recorded_ms=late),
                             "EXCLUDED_INVALID")
            self.assertEqual(manifest.append(path, signal(6),
                                             recorded_ms=signal(6)["captured_ms"]),
                             "ENROLLED")
            self.assertEqual(manifest.append(path, signal(3),
                                             recorded_ms=signal(6)["captured_ms"] + 1),
                             "EXCLUDED_INVALID")
            leaky = dict(signal(7), exit_close=1.0, api_key="NEVER")
            self.assertEqual(manifest.append(path, leaky,
                                             recorded_ms=leaky["captured_ms"] + 1),
                             "ENROLLED")
            self.assertNotIn(b"NEVER", path.read_bytes())
            self.assertNotIn(b"exit_close", path.read_bytes())

    def test_conflicting_duplicate_fails_closed_without_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            row = signal(1)
            manifest.append(path, row, recorded_ms=row["captured_ms"] + 1)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                manifest.append(path, dict(row, entry_close=101.0),
                                recorded_ms=row["captured_ms"] + 2)
            self.assertEqual(path.read_bytes(), before)

    def test_header_cannot_backdate_t0_or_be_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.jsonl"
            with self.assertRaises(ValueError):
                manifest.create(path, effective_t0_ms=study.ISSUE_CREATED_MS - 1,
                                implementation_sha=FAKE_IMPL,
                                recorded_ms=study.ISSUE_CREATED_MS - 10)
            with self.assertRaises(ValueError):  # t0 frozen after it passed
                manifest.create(path, effective_t0_ms=self.T0,
                                implementation_sha=FAKE_IMPL,
                                recorded_ms=self.T0 + 1)
            self.assertFalse(path.exists())
            path = self._new(d)
            with self.assertRaises(FileExistsError):
                self._new(d)

    def test_edit_reorder_and_rehash_attacks_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            for row in cohort(5):
                manifest.append(path, row, recorded_ms=row["captured_ms"] + 1)
            good = path.read_bytes()
            lines = good.splitlines()
            # Edit one entry price.
            rec = json.loads(lines[2])
            rec["envelope"]["entry_close"] = 99.0
            path.write_bytes(b"\n".join(lines[:2] + [manifest._canonical(rec)]
                                        + lines[3:]) + b"\n")
            self.assertEqual(manifest.verify(path)["status"], "MANIFEST_NOT_PROVEN")
            # Delete a middle entry.
            path.write_bytes(b"\n".join(lines[:2] + lines[3:]) + b"\n")
            self.assertEqual(manifest.verify(path)["status"], "MANIFEST_NOT_PROVEN")
            # Rehash a relabelled kind consistently: rule replay still rejects.
            records = [json.loads(x) for x in lines]
            records[1]["kind"] = "EXCLUDED_SYMBOL_CAP"
            head = records[0]["hash"]
            for r in records[1:]:
                body = {k: v for k, v in r.items() if k not in ("prev", "hash")}
                r["prev"], r["hash"] = head, manifest._chain(head, body)
                head = r["hash"]
            path.write_bytes(b"".join(manifest._canonical(r) + b"\n" for r in records))
            self.assertEqual(manifest.verify(path)["reason"], "CLASSIFICATION_MISMATCH")
            # Truncated final line.
            path.write_bytes(good[:-1])
            self.assertEqual(manifest.verify(path)["status"], "MANIFEST_NOT_PROVEN")

    def test_anchor_receipt_is_prepared_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            r = manifest.anchor_receipt(path)
            self.assertEqual(r["status"], "ANCHOR_PREPARED_NOT_PUBLISHED")
            self.assertEqual(len(r["head_sha256"]), 64)
            self.assertFalse(r["external_anchor_published"])
            self.assertEqual(r["enrolled_count"], 0)

    def test_manifest_members_feed_stats_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._new(d)
            for row in cohort(60):
                manifest.append(path, row, recorded_ms=row["captured_ms"] + 1)
            members = list(manifest.verify(path)["enrolled"])
            out = stats.evaluate(paired_for(members, lambda i, r: 0.01,
                                            lambda i, r: 0.01))
            self.assertEqual(out["status"], "MARKET_ONLY_REVIEW_READY")
            self.assertFalse(out["live_allowed"])


if __name__ == "__main__":
    unittest.main()
