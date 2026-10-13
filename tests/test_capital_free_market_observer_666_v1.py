"""#666 isolated observer regressions. Synthetic candles only, NO real capture.

A synthetic downtrend producing a SHORT BOS_BREAK proves the plumbing and the
isolation contract; it is not a candidate, sample progress or market evidence.
"""
import hashlib
import logging
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

from bot import capital_free_market_observer_666_v1 as ob
from bot import capital_free_market_oos_666_v1 as contract

P15, P1H, P4H = 900_000, 3_600_000, 14_400_000
T = 1_800_000_000_000 // P15 * P15
ROOT = Path(__file__).resolve().parents[1]


def _series(seed, n=2320, drift=-0.0008, vol=0.004):
    r, p, out = random.Random(seed), 100.0, []
    for _ in range(n):
        o = p
        c = max(0.01, o * (1 + drift + r.gauss(0, vol)))
        h = max(o, c) * (1 + abs(r.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(r.gauss(0, vol / 2)))
        out.append((o, h, lo, c, r.uniform(50, 150)))
        p = c
    return out


def synthetic_snapshot(seed=1, drift=-0.0008, t=T):
    base = _series(seed, drift=drift)
    first = t - len(base) * P15
    rows = [dict(ts=first + i * P15, o=o, h=h, l=lo, c=c, v=v)
            for i, (o, h, lo, c, v) in enumerate(base)]

    def agg(period):
        groups = {}
        for r in rows:
            groups.setdefault(r["ts"] // period * period, []).append(r)
        out = []
        for ts in sorted(groups):
            g = groups[ts]
            if len(g) == period // P15 and ts + period <= t:
                out.append(dict(ts=ts, o=g[0]["o"], h=max(x["h"] for x in g),
                                l=min(x["l"] for x in g), c=g[-1]["c"],
                                v=sum(x["v"] for x in g)))
        return out

    def fmt(data, period, limit):
        return [dict(ts=r["ts"], close_ms=r["ts"] + period - 1,
                     **{k: f"{r[k]:.8f}" for k in "ohlcv"})
                for r in data[-limit:]]
    return {
        "symbol": "SOLUSDT", "boundary_ms": t, "observed_ms": t + 1500,
        "source": "BINANCE_USDM_PUBLIC_KLINES",
        "raw_payload_sha256": hashlib.sha256(b"synthetic").hexdigest(),
        "k15": fmt(rows, P15, 200), "k1h": fmt(agg(P1H), P1H, 100),
        "k4h": fmt(agg(P4H), P4H, 120),
    }


class SnapshotValidationTests(unittest.TestCase):
    def test_valid_snapshot_appends_zero_elapsed_placeholder(self):
        reason, prep = ob.validate_snapshot(synthetic_snapshot())
        self.assertEqual(reason, "OK")
        for key, period in (("k15", P15), ("k1h", P1H), ("k4h", P4H)):
            self.assertTrue(prep[key][-1]["_placeholder"])
            self.assertEqual(prep[key][-1]["ts"], T // period * period)
            self.assertEqual(prep[key][-1]["v"], 0.0)
            self.assertLess(prep[key][-2]["ts"] + period - 1, T)
        self.assertEqual(prep["entry_close"], prep["k15"][-2]["c"])

    def test_candle_closing_at_or_after_boundary_is_lookahead(self):
        snap = synthetic_snapshot()
        snap["k15"].append(dict(snap["k15"][-1], ts=T, close_ms=T + P15 - 1))
        snap["k15"] = snap["k15"][-200:]
        self.assertEqual(ob.validate_snapshot(snap)[0], "LOOKAHEAD_CANDLE")

    def test_gap_disorder_and_short_lookback_rejected(self):
        snap = synthetic_snapshot()
        del snap["k1h"][10]
        self.assertEqual(ob.validate_snapshot(snap)[0], "GAP_OR_DISORDER")
        snap = synthetic_snapshot()
        snap["k15"][3], snap["k15"][4] = snap["k15"][4], snap["k15"][3]
        self.assertEqual(ob.validate_snapshot(snap)[0], "GAP_OR_DISORDER")
        snap = synthetic_snapshot()
        snap["k4h"] = snap["k4h"][-19:]
        self.assertEqual(ob.validate_snapshot(snap)[0], "LOOKBACK_INCOMPLETE")
        snap = synthetic_snapshot()
        snap["k15"] = snap["k15"][:-1]  # latest closed bar missing
        self.assertEqual(ob.validate_snapshot(snap)[0], "GAP_OR_DISORDER")

    def test_malformed_or_inconsistent_candles_rejected(self):
        for mutate in (
            lambda r: r.update(c=1.5),            # numeric, not exchange string
            lambda r: r.update(h="0.00000001"),   # high below body
            lambda r: r.update(v="-1"),
            lambda r: r.update(o="nan"),
            lambda r: r.update(extra=1),
            lambda r: r.update(close_ms=r["close_ms"] + 1),
        ):
            snap = synthetic_snapshot()
            mutate(snap["k15"][50])
            self.assertEqual(ob.validate_snapshot(snap)[0], "MALFORMED_CANDLE")

    def test_boundary_observation_source_and_hash_rules(self):
        cases = (
            ("boundary_ms", T + 1, "BOUNDARY_OR_OBSERVATION_TIME_INVALID"),
            ("observed_ms", T - 1, "BOUNDARY_OR_OBSERVATION_TIME_INVALID"),
            ("observed_ms", T + P15, "BOUNDARY_OR_OBSERVATION_TIME_INVALID"),
            ("source", "SYNTHETIC_FIXTURE", "SOURCE_NOT_ALLOWED"),
            ("raw_payload_sha256", None, "RAW_PAYLOAD_HASH_MISSING"),
            ("symbol", "SOLUSD", "INVALID_SYMBOL"),
        )
        for key, value, reason in cases:
            snap = synthetic_snapshot()
            snap[key] = value
            self.assertEqual(ob.validate_snapshot(snap)[0], reason, key)


class IsolationTests(unittest.TestCase):
    def test_pinned_production_digest_matches_working_tree(self):
        # Any production code drift from 45ebd59 must stop the observer.
        self.assertEqual(ob.production_code_digest(), ob.PRODUCTION_CODE_DIGEST)

    def test_child_environment_drops_credentials_and_pins_parameters(self):
        env = ob._child_env({
            "PATH": "/usr/bin", "BINANCE_API_KEY": "x", "BINANCE_API_SECRET": "y",
            "DATABASE_URL": "postgres://u:p@h/db", "TELEGRAM_BOT_TOKEN": "z",
            "MIN_ENTRY_SCORE": "55", "PAPER_TRADE": "false",
            "NEXUS_LIVE_RELEASE": "true", "PGPASSWORD": "q",
        })
        self.assertEqual(env["PATH"], "/usr/bin")
        for leaked in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "DATABASE_URL",
                       "TELEGRAM_BOT_TOKEN", "NEXUS_LIVE_RELEASE", "PGPASSWORD"):
            self.assertNotIn(leaked, env)
        self.assertEqual(env["MIN_ENTRY_SCORE"], "60")
        self.assertEqual(env["PAPER_TRADE"], "true")
        self.assertEqual(env["NEXUS_TELEGRAM"], "false")

    def test_child_network_block_refuses_external_socket(self):
        code = ("import socket,sys; sys.path.insert(0,'.');"
                "from bot.capital_free_market_observer_666_v1 import "
                "_install_network_block as g; g();\n"
                "try:\n socket.create_connection(('1.1.1.1',443),timeout=1)\n"
                "except PermissionError: print('BLOCKED')\n")
        out = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=ROOT,
                             env=ob._child_env(), capture_output=True,
                             text=True, timeout=60, check=False)
        self.assertEqual(out.stdout.strip(), "BLOCKED")

    def test_live_runtime_never_imports_research_modules(self):
        for path in (ROOT / "bot").rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(ob.RESEARCH_PREFIX):
                continue
            self.assertNotIn("capital_free_market_", path.read_text(), rel)
        for entry in ("main.py", "main_hardened.py", "sitecustomize.py"):
            self.assertNotIn("capital_free_market_",
                             (ROOT / entry).read_text(), entry)

    def test_invalid_implementation_sha_never_spawns(self):
        self.assertEqual(
            ob.run_isolated(synthetic_snapshot(), implementation_sha="HEAD")["status"],
            "IMPLEMENTATION_SHA_INVALID")


class EndToEndTests(unittest.TestCase):
    def test_isolated_run_emits_contract_eligible_unattested_envelope(self):
        out = ob.run_isolated(synthetic_snapshot(), implementation_sha="a" * 40)
        self.assertEqual(out["status"], "CANDIDATE_ENVELOPE_UNATTESTED")
        self.assertTrue(contract._eligible(out, contract.ISSUE_CREATED_MS))
        for key, value in ob.AUTHORITY.items():
            self.assertEqual(out[key], value, key)
        self.assertEqual(out["frozen_env"], ob.FROZEN_ENV)
        # Envelope alone can never become enrolled membership.
        selection = contract.freeze_offline(
            [out], effective_cutoff_ms=contract.ISSUE_CREATED_MS,
            source_complete=True)
        self.assertIsNone(selection["enrollment_count"])
        self.assertFalse(selection["source_attested"])

    def test_isolated_run_rejects_lookahead_without_analysis(self):
        snap = synthetic_snapshot()
        snap["k1h"][-1]["close_ms"] = T + P1H - 1
        snap["k1h"][-1]["ts"] = T
        out = ob.run_isolated(snap, implementation_sha="a" * 40)
        self.assertEqual(out["status"], "SOURCE_NOT_PROVEN")
        self.assertNotIn("candidate_id", out)

    def test_flat_market_yields_no_candidate(self):
        out = ob.run_isolated(synthetic_snapshot(seed=7, drift=0.0),
                              implementation_sha="a" * 40)
        self.assertIn(out["status"], ("NO_SIGNAL", "OUT_OF_SCOPE_SIGNAL"))
        self.assertNotIn("candidate_id", out)


class PlaceholderInvarianceTests(unittest.TestCase):
    """The zero-elapsed bar must not influence any production-stack result."""

    @classmethod
    def setUpClass(cls):
        os.environ.update(ob.FORCED_ENV)
        from bot.runtime_bootstrap import install
        install()
        logging.disable(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def _run(self, klines):
        from bot.hard_gate_shadow_context import scope
        from bot.strategy import Analyzer
        with scope() as ctx:
            sig = Analyzer().analyze_mtf("SOLUSDT", *klines, min_score=60,
                                         fee_mult=2.0, vol_mult=0.5)
            sig = sig or ctx.signal
        if sig is None:
            return None
        return (sig.direction, sig.regime, sig.entry_type, sig.score,
                round(sig.entry, 8), round(sig.sl, 8), round(sig.tp, 8))

    def test_placeholder_contents_do_not_change_results(self):
        signals = 0
        for seed in range(12):
            _, prep = ob.validate_snapshot(synthetic_snapshot(seed=seed))
            klines = [[{k: v for k, v in r.items() if k != "_placeholder"}
                       for r in prep[key]] for key in ("k15", "k1h", "k4h")]
            baseline = self._run(klines)
            signals += baseline is not None
            for mult, vol in ((0.5, 1e9), (2.0, 0.0)):
                changed = [[dict(r) for r in rows] for rows in klines]
                for rows in changed:
                    c = rows[-2]["c"] * mult
                    rows[-1].update(h=max(c, rows[-2]["c"]),
                                    l=min(c, rows[-2]["c"]), c=c, v=vol)
                self.assertEqual(self._run(changed), baseline, (seed, mult))
        self.assertGreater(signals, 0)


if __name__ == "__main__":
    unittest.main()
