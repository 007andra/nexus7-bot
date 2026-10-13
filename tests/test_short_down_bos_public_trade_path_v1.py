"""The #609 source path audit can NEVER mint executable/attested evidence."""
import unittest

from bot import short_down_bos_public_trade_path_v1 as path
from bot import short_down_bos_exec_net_oos_v1 as study

CID = "HARD_GATE_SHADOW:BTCUSDT:SHORT:BOS_BREAK:2001111"
SYM = "BTCUSDT"
BASE = 1_800_000_000  # on a 900-second boundary


def candidate(captured=BASE):
    return {
        "candidate_id": CID, "symbol": SYM, "captured_epoch": captured,
        "population": "HARD_GATE_SHADOW",
        "side": "SHORT", "regime": "TRENDING_DOWN",
        "setup": "BOS_BREAK", "nexus_called": True,
        "nexus_allowed": True, "shadow_only": True,
        "live_eligible": False, "decision_effect": "NONE",
        "execution_effect": "NONE",
        "entry": 100, "stop": 103, "target": 94, "nexus_net_rr": 1.8,
    }


def source(captured=BASE, horizon=60):
    start = captured * 1000
    end = start + horizon * 60_000
    first_grid = (start + path.INTERVAL_MS - 1) // path.INTERVAL_MS * path.INTERVAL_MS
    starts = list(range(first_grid, end, path.INTERVAL_MS))
    if start % path.INTERVAL_MS:
        starts.append(start)
    points = sorted(set([start - 100, end + 100] + [v + 100 for v in starts]))
    raw = [{"e": "aggTrade", "s": SYM, "a": 200 + i,
            "T": stamp, "p": "100.0"} for i, stamp in enumerate(points)]
    return {
        "candidate_id": CID, "symbol": SYM,
        "source": path.SOURCE, "cohort_id": path.COHORT_ID,
        "stream_connected": True, "stream_gap_count": 0,
        "generation": 1,
        "observed_from_ms": points[0], "observed_until_ms": points[-1],
        "first_agg_id": raw[0]["a"], "last_agg_id": raw[-1]["a"],
        "events": raw,
    }


class ProspectiveSourceTests(unittest.TestCase):
    def test_partial_path_preserves_exact_capture_and_horizon(self):
        for h in path.HORIZONS:
            for offset in (0, 30):
                with self.subTest(h=h, offset=offset):
                    row = candidate(BASE + offset)
                    capture = source(BASE + offset, h)
                    r = path.build_path(row, capture, horizon=h)
                    self.assertEqual(r["status"], "SHAPE_VALID_SOURCE_UNATTESTED")
                    self.assertTrue(r["shape_ready"])
                    self.assertIsNone(r["evaluator_evidence"])
                    self.assertFalse(r["live_allowed"])
                    self.assertFalse(r["source_independently_authenticated"])
                    self.assertEqual(len(r["bars"]), h // 15 - (1 if offset else 0))
                    if offset:
                        self.assertEqual(r["entry_partial_window"]["start_epoch"],
                                         BASE + offset)
                        self.assertEqual(r["exit_partial_window"]["end_epoch"],
                                         BASE + offset + h * 60)
                    else:
                        self.assertIsNone(r["entry_partial_window"])
                        self.assertIsNone(r["exit_partial_window"])
                    # #610 time-path logic must accept *shape only* after an
                    # auditor has independently supplied genuine provenance.
                    ev = {
                        "bars": r["bars"],
                        "entry_partial_window": r["entry_partial_window"],
                        "exit_partial_window": r["exit_partial_window"],
                    }
                    path_rows = study._bar_path(
                        row, ev, h, now_epoch=row["captured_epoch"] + h * 60 + 1)
                    self.assertIsNotNone(path_rows)

    def test_missing_or_discontinuous_source_is_fail_closed(self):
        good = source(BASE + 30)
        cases = [
            ("missing leading event", {**good, "events": good["events"][1:]}),
            ("missing interior trade", {**good, "events": (
                good["events"][:2] + good["events"][3:]
            )}),
            ("missing last event", {**good, "events": good["events"][:-1]}),
            ("stream disconnected", {**good, "stream_connected": False}),
            ("reported stream gap", {**good, "stream_gap_count": 1}),
            ("missing generation", {**good, "generation": None}),
            ("interval ended early", {**good, "observed_until_ms":
                                        (BASE + 30) * 1000 + 2}),
            ("wrong symbol", {**good, "symbol": "ETHUSDT"}),
            ("wrong cohort", {**good, "cohort_id": "HISTORICAL_R3"}),
            ("wrong first ID", {**good, "first_agg_id": -1}),
            ("unbounded events", {**good, "events": []}),
        ]
        for label, variant in cases:
            with self.subTest(label=label):
                r = path.build_path(candidate(BASE + 30), variant, horizon=60)
                self.assertFalse(r["shape_ready"])
                self.assertIsNone(r["evaluator_evidence"])
                self.assertFalse(r["promotion_allowed"])

    def test_invalid_quote_and_tampered_trade_stream_not_authentic(self):
        original = source()
        changes = [
            {"e": "bookTicker"}, {"s": "ETHUSDT"},
            {"T": None}, {"p": "nan"}, {"p": -0.01},
            {"a": 999999}, {"T": original["events"][0]["T"] - 50},
        ]
        for change in changes:
            with self.subTest(change=change):
                bad = {**original, "events": [dict(v) for v in original["events"]]}
                bad["events"][1].update(change)
                res = path.build_path(candidate(), bad, horizon=60)
                self.assertEqual(res["status"], "NET_SOURCE_INCOMPLETE")

    def test_empty_last_partial_bar_is_never_assumed_flat(self):
        row = candidate(BASE + 30)
        original = source(BASE + 30)
        finish = (BASE + 30 + 3600) * 1000
        # The last interval is after the 3 full bars, before the exact exit.
        chop = {**original, "events": [
            dict(v) for v in original["events"]
            if v["T"] < finish - 150_000 or v["T"] > finish
        ]}
        # Re-sequence to isolate missing price observations from ID gaps.
        for i, ev in enumerate(chop["events"]):
            ev["a"] = 200 + i
        chop["first_agg_id"] = 200
        chop["last_agg_id"] = 200 + len(chop["events"]) - 1
        result = path.build_path(row, chop, horizon=60)
        self.assertEqual(result["reason"], "EMPTY_PARTIAL_BOUNDARY")

    def test_original_cohort_cutoff_not_repurposed(self):
        self.assertEqual(path.COHORT_ID, study.COHORT_ID)
        self.assertLess(study.CUTOFF_EPOCH, BASE)
        self.assertFalse(path.AUTHORITY["real_fills_proven"])


if __name__ == "__main__":
    unittest.main()
