"""Before/after benchmark: operational latency with research on shared vs dedicated DB.

Usage: TEST_POSTGRES_DSN=postgresql://... python -m scripts.bench_oos_reader_isolation \
           [--seconds 60] [--one-way-ms 70] [--candidates 2200] [--out results.json]

Scenarios (same proxied PostgreSQL, same data):
  A  operational only (heartbeat + lock probes), no research
  B  research readouts on the shared operational connection (flag OFF, current prod)
  C  research readouts on the dedicated reader (OOS_RESEARCH_READER_V2=true)
Only a disposable schema is touched; no exchange, no production database.
"""
import argparse
import asyncio
import json
import logging
import sys
import time

from tests.oos_isolation_harness import DSN, Rig, run_mixed_load, summarize


async def operational_only(rig, seconds, hb_interval=1.0, probe_interval=0.25):
    hb, lock_wait, op_total = [], [], []
    deadline = time.monotonic() + seconds

    async def heartbeat_loop():
        while time.monotonic() < deadline:
            hb.append(await rig.heartbeat_once())
            await asyncio.sleep(hb_interval)

    async def probe_loop():
        while time.monotonic() < deadline:
            waited, total = await rig.lock_probe_once()
            lock_wait.append(waited)
            op_total.append(total)
            await asyncio.sleep(probe_interval)

    await asyncio.gather(heartbeat_loop(), probe_loop())
    return {"heartbeat_ms": summarize(hb), "lock_wait_ms": summarize(lock_wait),
            "operational_query_ms": summarize(op_total)}


async def main(args):
    if not DSN:
        sys.exit("TEST_POSTGRES_DSN is required")
    logging.disable(logging.CRITICAL)  # research emitters log every round
    results = {"config": vars(args)}
    async with Rig(one_way_ms=args.one_way_ms, candidates=args.candidates) as rig:
        await rig.heartbeat_once()  # warm-up (creates the lease row)
        results["A_operational_only"] = await operational_only(rig, args.seconds)
        results["B_research_shared_connection"] = await run_mixed_load(
            rig, research_on=False, seconds=args.seconds)
        results["C_research_dedicated_reader"] = await run_mixed_load(
            rig, research_on=True, seconds=args.seconds)
    text = json.dumps(results, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--one-way-ms", type=float, default=70.0)
    parser.add_argument("--candidates", type=int, default=2200)
    parser.add_argument("--out", default="")
    asyncio.run(main(parser.parse_args()))
