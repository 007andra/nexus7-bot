"""RAW_SIGNAL_EDGE_V1 mechanics: data integrity, no lookahead, isolation, stats.

Synthetic inputs here test the machinery only; they are never evidence.
"""
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import binance_public_history as hist
from bot import raw_signal_edge_study_v1 as study

ROOT = Path(__file__).resolve().parents[1]
H15 = 900_000


def _zip(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def _candles(n, start=1_759_276_800_000, step=H15, price=100.0):
    return [{"ts": start + i * step, "o": price, "h": price * 1.01,
             "l": price * 0.99, "c": price, "v": 10.0} for i in range(n)]


class PublicHistoryTests(unittest.TestCase):
    def test_months_and_archive_paths(self):
        self.assertEqual(hist.months_between("2025-11", "2026-02"),
                         ["2025-11", "2025-12", "2026-01", "2026-02"])
        self.assertEqual(hist.kline_archive("BTCUSDT", "15m", "2026-01"),
                         "klines/BTCUSDT/15m/BTCUSDT-15m-2026-01.zip")
        self.assertEqual(hist.funding_archive("BTCUSDT", "2026-01"),
                         "fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-01.zip")
        with self.assertRaises(ValueError):
            hist.kline_archive("BTCUSDT", "5m", "2026-01")

    def _fetcher(self, payload, checksum=None):
        digest = checksum or hashlib.sha256(payload).hexdigest()
        calls = []

        def fetch(url):
            calls.append(url)
            if url.endswith(".CHECKSUM"):
                return f"{digest}  file.zip\n".encode()
            return payload
        return fetch, calls

    def test_archive_is_checksum_verified_and_cached(self):
        payload = _zip("a.csv", "1,1,1,1,1,1\n")
        fetch, calls = self._fetcher(payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = hist.ensure_archive(Path(tmp), "klines/X/15m/a.zip", fetch=fetch)
            self.assertEqual(path.read_bytes(), payload)
            hist.ensure_archive(Path(tmp), "klines/X/15m/a.zip", fetch=fetch)
        self.assertEqual(len(calls), 2)  # checksum + archive once, then cache

    def test_checksum_mismatch_and_corrupted_cache_fail_closed(self):
        payload = _zip("a.csv", "1,1,1,1,1,1\n")
        fetch, _ = self._fetcher(payload, checksum="0" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(hist.HistoryIntegrityError):
                hist.ensure_archive(Path(tmp), "k/a.zip", fetch=fetch)
            self.assertFalse((Path(tmp) / "k/a.zip").exists())
        fetch, _ = self._fetcher(payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = hist.ensure_archive(Path(tmp), "k/a.zip", fetch=fetch)
            path.write_bytes(payload + b"x")
            with self.assertRaises(hist.HistoryIntegrityError):
                hist.ensure_archive(Path(tmp), "k/a.zip", fetch=fetch)

    def test_kline_parsing_header_microseconds_and_invalid_rows(self):
        rows = [["open_time", "open", "high", "low", "close", "volume"],
                ["1759276800000", "10", "11", "9", "10.5", "3"],
                ["1759277700000000", "10.5", "11", "10", "10.8", "2"]]
        candles = hist.parse_klines(rows)
        self.assertEqual([c["ts"] for c in candles], [1759276800000, 1759277700000])
        self.assertEqual(candles[0], {"ts": 1759276800000, "o": 10.0, "h": 11.0,
                                      "l": 9.0, "c": 10.5, "v": 3.0})
        with self.assertRaises(hist.HistoryIntegrityError):
            hist.parse_klines([["1759276800000", "10", "9", "11", "10", "1"]])
        with self.assertRaises(hist.HistoryIntegrityError):
            hist.check_contiguous(_candles(3)[:1] + _candles(3)[2:], "15m")

    def test_funding_parsing(self):
        events = hist.parse_funding([["calc_time", "funding_interval_hours", "last_funding_rate"],
                                     ["1759276800000", "8", "0.0001"]])
        self.assertEqual(events, [{"timepoint": 1759276800000, "fundingRate": 0.0001}])


class StudyMechanicsTests(unittest.TestCase):
    def test_prereg_is_frozen_and_hashable(self):
        self.assertEqual(len(study.prereg_digest()), 64)
        self.assertEqual(study.PREREG["windows"], {"15m": 200, "1h": 100, "4h": 120})
        self.assertFalse(set(study.PREREG["holdout_months"]) & set(study.PREREG["discovery_months"]))
        self.assertLess(study.PREREG["discovery_months"][-1], study.PREREG["holdout_months"][0])

    def test_session_and_funnel_use_decision_time_not_wall_clock(self):
        sig = SimpleNamespace(score=65, direction="LONG", regime="TRENDING_UP", expected_pnl=1.0)
        asia = 1_759_287_600_000      # 2025-10-01 03:00 UTC
        london = asia + 9 * 3_600_000  # 12:00 UTC
        self.assertEqual(study.session_for(asia), "ASIA")
        self.assertEqual(study.session_for(london), "LONDON")
        self.assertEqual(study.funnel_flags(sig, "SOLUSDT", asia, "PASS", 60)["adjusted_score"], 57)
        self.assertFalse(study.funnel_flags(sig, "SOLUSDT", asia, "PASS", 60)["funnel_pass"])
        self.assertTrue(study.funnel_flags(sig, "SOLUSDT", london, "PASS", 60)["funnel_pass"])
        self.assertFalse(study.funnel_flags(sig, "SOLUSDT", london, "BLOCKED", 60)["funnel_pass"])
        short = SimpleNamespace(score=80, direction="SHORT", regime="TRENDING_UP", expected_pnl=1.0)
        self.assertFalse(study.funnel_flags(short, "BTCUSDT", london, "PASS", 60)["regime_allows"])

    def test_analyzer_sees_only_closed_candles_at_frozen_clock(self):
        k15 = _candles(400)
        k1h = _candles(120, start=k15[0]["ts"] - 30 * 3_600_000, step=3_600_000)
        k4h = _candles(60, start=k15[0]["ts"] - 50 * 14_400_000, step=14_400_000)
        seen = []

        def fake_analyze(self, symbol, w15, w1h, w4h, **kwargs):
            decision = int(study._CLOCK[0] * 1000) - 1000
            seen.append(decision)
            assert w15[-1]["ts"] + H15 <= decision
            assert w1h[-1]["ts"] + 3_600_000 <= decision
            assert w4h[-1]["ts"] + 14_400_000 <= decision
            return SimpleNamespace(direction="LONG", entry=100.0, sl=99.0, tp=102.0, tp1=102.0,
                                   tp2=102.0, score=70, rr=2.0, entry_type="PULLBACK",
                                   regime="TRENDING_UP", expected_pnl=1.0)

        start = k15[250]["ts"]
        with patch("bot.strategy.Analyzer.analyze_mtf", fake_analyze):
            rows = study.evaluate_symbol("BTCUSDT", k15, k1h, k4h, [], start_ms=start,
                                         end_ms=start + 20 * H15, fees=[0.0006])
        self.assertEqual(seen, [start + i * H15 for i in range(20)])
        self.assertEqual(len(rows), 20)
        for row in rows:
            self.assertGreater(row["exit_ts"], row["ts"])
            self.assertLess(row["net_r"]["0.0006"], row["gross_r"])  # costs always subtract

    def test_non_overlapping_selection(self):
        rows = [{"symbol": "A", "ts": 0, "exit_ts": 10}, {"symbol": "A", "ts": 5, "exit_ts": 20},
                {"symbol": "B", "ts": 5, "exit_ts": 8}, {"symbol": "A", "ts": 10, "exit_ts": 30}]
        chosen = study.select_non_overlapping(rows)
        self.assertEqual([(r["symbol"], r["ts"]) for r in chosen], [("A", 0), ("B", 5), ("A", 10)])

    def test_bootstrap_is_deterministic_and_conservative(self):
        clusters = {("A", d): [1.0 if d % 3 else -1.0] for d in range(60)}
        a = study.cluster_bootstrap_lower(clusters, replicates=2000, seed=1, quantile=0.05)
        b = study.cluster_bootstrap_lower(clusters, replicates=2000, seed=1, quantile=0.05)
        mean = sum(v[0] for v in clusters.values()) / 60
        self.assertEqual(a, b)
        self.assertLess(a, mean)

    def test_verdict_gates(self):
        good = {"trades": 300, "symbols": 12, "mean_r": 0.1, "bootstrap_lower_r": 0.01,
                "loso_min_mean_r": 0.05}
        self.assertEqual(study.verdict(good), "RAW_SIGNAL_EDGE_PASS")
        self.assertEqual(study.verdict({**good, "trades": 100}), "INSUFFICIENT_EVIDENCE")
        self.assertEqual(study.verdict({**good, "symbols": 5}), "INSUFFICIENT_EVIDENCE")
        self.assertEqual(study.verdict({**good, "bootstrap_lower_r": -0.01}), "RAW_SIGNAL_EDGE_FAIL")
        self.assertEqual(study.verdict({**good, "loso_min_mean_r": -0.01}), "RAW_SIGNAL_EDGE_FAIL")

    def test_report_decides_on_holdout_only(self):
        lo, _ = study._month_bounds(study.PREREG["discovery_months"])
        hlo, _ = study._month_bounds(study.PREREG["holdout_months"])
        row = {"rr": 2.0, "pullback_pass": True, "funnel_pass": True, "direction": "LONG",
               "entry_type": "PULLBACK", "regime": "TRENDING_UP", "gross_r": 1.0}
        rows = [dict(row, symbol=f"S{i % 12}", ts=lo + i * 86_400_000, exit_ts=lo + i * 86_400_000 + 1,
                     net_r={"0.0006": 2.0, "0.0004": 2.0, "0.0005": 2.0}) for i in range(200)]
        rows += [dict(row, symbol=f"S{i % 12}", ts=hlo + i * 3_600_000, exit_ts=hlo + i * 3_600_000 + 1,
                      net_r={"0.0006": -0.5, "0.0004": -0.5, "0.0005": -0.5}) for i in range(200)]
        report = study.build_report(rows)
        self.assertEqual(report["verdict"], "RAW_SIGNAL_EDGE_FAIL")
        self.assertGreater(report["windows"]["discovery"]["FUNNEL"]["by_fee"]["0.0006"]["mean_r"], 0)
        self.assertFalse(report["live_allowed"])


class WorkerIsolationTests(unittest.TestCase):
    def test_worker_blocks_network_freezes_clock_and_scrubs_secrets(self):
        code = (
            "import os, socket, time\n"
            "from bot import raw_signal_edge_study_v1 as s\n"
            "s._prepare_worker_env(); s._isolate_worker()\n"
            "s._CLOCK[0] = 123.0\n"
            "assert time.time() == 123.0\n"
            "assert 'BINANCE_API_KEY' not in os.environ and os.environ['PAPER_TRADE'] == 'true'\n"
            "try:\n"
            "    socket.create_connection(('8.8.8.8', 443), timeout=1)\n"
            "except PermissionError:\n"
            "    print('BLOCKED')\n"
        )
        env = {**os.environ, "BINANCE_API_KEY": "x"}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("BLOCKED", out.stdout)

    def test_worker_never_downloads(self):
        with self.assertRaises(PermissionError):
            study._no_fetch("https://data.binance.vision/x")


if __name__ == "__main__":
    unittest.main()
