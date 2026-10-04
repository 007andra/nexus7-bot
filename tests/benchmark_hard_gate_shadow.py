"""Reproducible offline benchmark: composed analyzer and signal-heavy NEXUS path.

Synthetic cached candles, SQLite in memory, warm runtime. No network or claims
about Railway latency. Forced signals in the second workload ONLY; NEXUS real.
"""
import asyncio
from contextlib import nullcontext
import json
import os
import statistics
import time
from unittest.mock import patch

os.environ["EXCHANGE"] = "binance"
os.environ["PAPER_TRADE"] = "true"
os.environ["NEXUS_TELEGRAM"] = "false"

from tests.run_offline import install_network_guard
from tests.test_hard_gate_shadow_scan import DB, ENV, engine, signal
from bot.runtime_bootstrap import install
from bot import hard_gate_shadow_scan as shadow
from bot.config import cfg
from bot.strategy import Analyzer


async def main():
    install_network_guard()
    install()
    obj = engine([f"ASSET{i}USDT" for i in range(25)])
    sql = DB()
    results = []
    sample_id = [0]
    def fresh_signal(symbol, *args, **kwargs):
        sig = signal(symbol)
        sig._bgx_formation_bucket = sample_id[0]
        return sig
    for workload in ("composed_analyzer", "forced_signal_real_nexus"):
        timings, rests, evaluated = [], 0, 0
        cm = patch.object(Analyzer, "analyze_mtf", side_effect=fresh_signal) if workload.startswith("forced") else nullcontext()
        with patch.dict(os.environ, ENV), patch.object(cfg, "MAX_DRAWDOWN", .17), cm:
            for run in range(21):
                sample_id[0] = run + 1
                start = time.perf_counter()
                output = await shadow.scan(obj, db=sql)
                duration = (time.perf_counter() - start) * 1000
                if run:
                    timings.append(duration)
                    rests += output["summary"]["additional_rest_calls_per_scan"]
                    evaluated += output["summary"]["nexus_evaluated"]
        results.append({"workload": workload, "mean_ms": round(statistics.mean(timings), 3),
                        "p95_ms": round(sorted(timings)[18], 3), "symbols": 25,
                        "samples": 20, "additional_rest_calls": rests,
                        "nexus_evaluated": evaluated})
    # Exercise overlap on the real loop, with instrumented entry count.
    with patch.dict(os.environ, ENV), patch.object(cfg, "MAX_DRAWDOWN", .17):
        scans = await asyncio.gather(*(shadow.scan(obj, db=sql) for _ in range(5)))
    results.append({"concurrent_requests": 5, "completed_scans": sum(x is not None for x in scans),
                    "skipped_previous_scan_running": sum(x is None for x in scans),
                    "overlapping_scans": max(0, sum(x is not None for x in scans) - 1)})
    sql.conn.close()
    print("HARD_GATE_BENCHMARK=" + json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
