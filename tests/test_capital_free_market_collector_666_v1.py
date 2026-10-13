"""#666 collector loop with a fake public REST layer; NO network, NO capture.

Fixtures are synthetic. They prove the bookkeeping rules (t0 gate, enrollment
before outcomes, exact outcome windows, censoring, chain integrity), not any
market property.
"""
import json
from pathlib import Path
import tempfile
import unittest

from bot import capital_free_market_collector_666_v1 as col
from bot import capital_free_market_manifest_666_v1 as manifest
from bot import capital_free_market_oos_666_v1 as study
from tests.test_capital_free_market_observer_666_v1 import synthetic_snapshot

P15 = col.P15
IMPL = "c" * 40
T0 = (study.ISSUE_CREATED_MS // P15 + 4) * P15


def rest_rows(snapshot_rows, period):
    out = []
    for r in snapshot_rows:
        out.append([r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"],
                    r["ts"] + period - 1, "0", 1, "0", "0", "0"])
    # The still-forming bar Binance appends after the last closed one.
    last = out[-1]
    out.append([last[0] + period, last[4], last[4], last[4], last[4], "0",
                last[0] + 2 * period - 1, "0", 0, "0", "0", "0"])
    return out


class FakeBinance:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail = set()
        self.exit_close = "95.0"

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path != "/fapi/v1/klines":
            raise ValueError("unexpected path")
        if params["symbol"] in self.fail:
            raise OSError("HTTP 451")
        if "startTime" in params:
            start = params["startTime"]
            row = [start, "100", "101", "94", self.exit_close, "10",
                   start + P15 - 1, "0", 1, "0", "0", "0"]
            return json.dumps([row]).encode(), [row]
        t = self.clock.t
        snap = synthetic_snapshot(t=t)
        key, period = {"15m": ("k15", P15), "1h": ("k1h", 3_600_000),
                       "4h": ("k4h", 14_400_000)}[params["interval"]]
        rows = rest_rows(snap[key], period)
        return json.dumps(rows).encode(), rows


class Clock:
    def __init__(self, t):
        self.t = t
        self.offset = 30_000

    def __call__(self):
        return self.t + self.offset


def fake_observer(candidates):
    """Return a contract-valid envelope only for the named symbols."""
    def run(snapshot, *, implementation_sha):
        sym = snapshot["symbol"]
        reason, prep = col.observer.validate_snapshot(snapshot)
        if prep is None:
            return {"status": "SOURCE_NOT_PROVEN", "reason": reason}
        if sym not in candidates:
            return {"status": "NO_SIGNAL"}
        return col.observer.envelope(
            {"direction": "SHORT", "regime": "TRENDING_DOWN",
             "entry_type": "BOS_BREAK", "score": 70}, prep,
            snapshot_sha256="d" * 64, implementation_sha=implementation_sha)
    return run


class CollectorTests(unittest.TestCase):
    def _make(self, d, clock, candidates=("BTCUSDT", "ETHUSDT"), stamps=None):
        fake = FakeBinance(clock)
        logs = []
        c = col.Collector(d, effective_t0_ms=T0, implementation_sha=IMPL,
                          get=fake, run_observer=fake_observer(set(candidates)),
                          now_ms=clock, stamp=stamps or (lambda dg: [b"ots"]),
                          log=logs.append)
        return c, fake, logs

    def test_header_must_be_written_before_t0(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self._make(d, Clock(T0 + P15))
            self.assertFalse((Path(d) / "manifest.jsonl").exists())

    def test_boundaries_at_or_before_t0_capture_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, fake, logs = self._make(d, clock)
            clock.t = T0
            self.assertFalse(c.step(T0))
            self.assertEqual(fake.calls, [])
            self.assertIn("WAITING_FOR_T0", logs[-1])

    def test_capture_enrolls_in_universe_order_and_logs_every_symbol(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, fake, logs = self._make(d, clock)
            t = T0 + P15
            clock.t = t
            self.assertTrue(c.step(t))
            checked = manifest.verify(Path(d) / "manifest.jsonl")
            self.assertEqual([e["symbol"] for e in checked["enrolled"]],
                             ["BTCUSDT", "ETHUSDT"])
            obs = (Path(d) / "observations.jsonl").read_bytes().splitlines()
            self.assertEqual(len(obs), len(study.UNIVERSE))
            # Only closed candles were sent to the observer: 3 REST reads/symbol.
            self.assertEqual(len(fake.calls), 3 * len(study.UNIVERSE))
            self.assertTrue(any('"event": "ANCHOR"' in x for x in logs))
            self.assertTrue((Path(d) / "raw" / f"{t}-BTCUSDT.json.gz").exists())
            # No credentials or signed endpoints are ever used.
            for _, params in fake.calls:
                self.assertFalse({"signature", "timestamp", "apiKey"} & set(params))

    def test_outcomes_recorded_only_inside_exact_window(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, fake, _ = self._make(d, clock, candidates=("BTCUSDT",))
            t = T0 + P15
            clock.t = t
            c.step(t)
            clock.t = t + 45 * 60_000            # 45m: nothing due
            c.record_outcomes()
            self.assertEqual(c.outcomes.count, 0)
            clock.t = t + 60 * 60_000            # 60m window
            self.assertTrue(c.record_outcomes())
            self.assertFalse(c.record_outcomes())  # never twice
            clock.t = t + 240 * 60_000 + P15     # 240m window already closed
            c.record_outcomes()
            outcomes = [json.loads(x)["outcome"] for x in
                        (Path(d) / "outcomes.jsonl").read_bytes().splitlines()]
            self.assertEqual([(o["horizon_minutes"], o["close_ms"]) for o in outcomes],
                             [(60, t + 3_600_000)])
            self.assertEqual(outcomes[0]["exit_close"], 95.0)

    def test_censored_pair_is_insufficient_never_backfilled(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, _, _ = self._make(d, clock, candidates=("BTCUSDT",))
            t = T0 + P15
            clock.t = t
            c.step(t)
            clock.t = t + 60 * 60_000
            c.record_outcomes()
            clock.t = t + 300 * 60_000
            c.record_outcomes()
            result = c.evaluate()
            self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
            self.assertFalse(result["live_allowed"])

    def test_source_failure_is_recorded_not_invented(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, fake, _ = self._make(d, clock)
            fake.fail.add("BTCUSDT")
            t = T0 + P15
            clock.t = t
            c.step(t)
            obs = [json.loads(x) for x in
                   (Path(d) / "observations.jsonl").read_bytes().splitlines()]
            btc = [o for o in obs if o["symbol"] == "BTCUSDT"][0]
            self.assertTrue(btc["status"].startswith("SOURCE_FETCH_FAILED"))
            enrolled = manifest.verify(Path(d) / "manifest.jsonl")["enrolled"]
            self.assertEqual([e["symbol"] for e in enrolled], ["ETHUSDT"])

    def test_restart_resumes_chains_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, _, _ = self._make(d, clock)
            t = T0 + P15
            clock.t = t
            c.step(t)
            c2, _, _ = self._make(d, clock)
            self.assertEqual(c2.observations.head, c.observations.head)
            path = Path(d) / "observations.jsonl"
            lines = path.read_bytes().splitlines()
            rec = json.loads(lines[3])
            rec["status"] = "CANDIDATE_ENVELOPE_UNATTESTED"
            path.write_bytes(b"\n".join(lines[:3] + [col._canonical(rec)] + lines[4:]) + b"\n")
            with self.assertRaises(ValueError):
                self._make(d, clock)

    def test_t0_mismatch_on_restart_refused(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            self._make(d, clock)
            with self.assertRaises(ValueError):
                col.Collector(d, effective_t0_ms=T0 + P15, implementation_sha=IMPL,
                              get=FakeBinance(clock), now_ms=clock,
                              stamp=lambda dg: [], log=lambda x: None)

    def test_anchor_failure_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            c, _, logs = self._make(d, clock, stamps=lambda dg: [])
            clock.t = T0 + P15
            c.step(T0 + P15)
            anchor = [json.loads(x) for x in logs if '"ANCHOR"' in x][0]
            self.assertEqual(anchor["calendars_ok"], 0)

    def test_real_isolated_observer_accepts_binance_rest_rows(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock(T0 - P15)
            fake = FakeBinance(clock)
            c = col.Collector(d, effective_t0_ms=T0, implementation_sha=IMPL,
                              get=fake, now_ms=clock, stamp=lambda dg: [],
                              log=lambda x: None)
            t = T0 + P15
            clock.t = t
            snap, _ = c._fetch_symbol("SOLUSDT", t)
            out = col.observer.run_isolated(snap, implementation_sha=IMPL)
            self.assertEqual(out["status"], "CANDIDATE_ENVELOPE_UNATTESTED")
            self.assertTrue(study._eligible(out, T0))

    def test_still_forming_rows_are_dropped(self):
        t = T0 + P15
        rows = rest_rows(synthetic_snapshot(t=t)["k15"], P15)
        snap = col.build_snapshot("BTCUSDT", t, t + 1000,
                                  {"k15": rows, "k1h": rows, "k4h": rows})
        self.assertTrue(all(r["close_ms"] < t for r in snap["k15"]))
        self.assertEqual(len(snap["k15"]), 200)


if __name__ == "__main__":
    unittest.main()
