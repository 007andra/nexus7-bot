"""Export the frozen general prospective OOS candidate trade *inputs*.

No candle sourcing, DB writes, exchange connections, or modifications to the
canonical OOS outcomes. Run in operator-controlled private environment with a
SELECT-only PostgreSQL account; this uses an additional READ ONLY transaction.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path

from bot import prospective_oos_cohort_v1 as cohort
from bot import prospective_oos_maturation_review_v1 as review

COST_KEYS = ("candidate_id", "symbol", "exchange", "observed_at",
             "taker_fee", "entry_slippage", "exit_slippage")


def _finite(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _json(value):
    try:
        data = json.loads(value) if not isinstance(value, dict) else value
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def prepare(meta, candidates, *, as_of_epoch):
    meta = _json(meta)
    if (meta is None or meta.get("cohort_id") != cohort.COHORT_ID
            or meta.get("hypothesis_frozen") is not True
            or meta.get("reset_allowed") is not False):
        raise ValueError("INVALID_FROZEN_METADATA")
    start = _finite(meta.get("started_epoch"))
    if start is None or start != _finite(meta.get("discovery_cutoff_epoch")):
        raise ValueError("FROZEN_CUTOFF_MISMATCH")
    items, exclusions, seen = [], Counter(), set()
    for entry in candidates:
        raw = _json(entry["payload"])
        if raw is None:
            exclusions["MALFORMED_JSON"] += 1
            continue
        raw["_review_table_candidate_id"] = entry["candidate_id"]
        normalized, why = review._candidate(raw, started_epoch=start)
        if normalized is None:
            exclusions[why] += 1
            continue
        cid = normalized["candidate_id"]
        if cid in seen:
            raise ValueError("DUPLICATE_CANDIDATE")
        seen.add(cid)
        if normalized["captured_epoch"] > as_of_epoch:
            exclusions["FUTURE_CAPTURE"] += 1
            continue
        levels = tuple(_finite(raw.get(k)) for k in ("entry", "stop", "target"))
        side = normalized["side"]
        if (side not in ("LONG", "SHORT") or any(x is None or x <= 0 for x in levels)
            or not (
                levels[1] < levels[0] < levels[2] if side == "LONG"
                else levels[2] < levels[0] < levels[1]
            )):
            exclusions["INVALID_ENTRY_STOP_TARGET"] += 1
            continue
        cost = raw.get("cost_snapshot")
        filtered_cost = ({k: cost[k] for k in COST_KEYS if k in cost}
                         if isinstance(cost, dict) else None)
        cf = raw["counterfactual_nexus_v1"]
        decision = (
            "INDETERMINATE_ERROR" if cf.get("status") == "ERROR" else
            "COUNTERFACTUAL_APPROVED" if cf.get("execution_allowed") is True else
            "COUNTERFACTUAL_REJECTED" if cf.get("execution_allowed") is False else
            "INDETERMINATE"
        )
        # A candle path MUST be supplied independently from a point-in-time
        # market-history archive. Never reuse OOS marked returns as OHLC bars.
        items.append({
            "candidate_id": cid, "symbol": normalized["symbol"],
            "side": side, "regime": normalized["regime"],
            "setup": normalized["setup"], "captured_epoch": normalized["captured_epoch"],
            "entry": levels[0], "stop": levels[1], "target": levels[2],
            "cohort_decision": decision, "cost_snapshot": filtered_cost,
        })
    items.sort(key=lambda r: (r["captured_epoch"], r["candidate_id"]))
    canonical = "\n".join(json.dumps(i, sort_keys=True, separators=(",", ":"), allow_nan=False) for i in items)
    manifest = {
        "cohort_id": cohort.COHORT_ID, "cutoff_epoch": start,
        "as_of_epoch": as_of_epoch, "exported": len(items),
        "excluded": dict(sorted(exclusions.items())),
        "jsonl_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "market_candles_present": False,
        "authority": "READ_ONLY_OFFLINE_HYPOTHETICAL_REPLAY",
        "live_allowed": False,
    }
    return items, manifest


async def export_inputs(dsn, as_of_epoch):
    import asyncpg
    if not isinstance(dsn, str) or not dsn.startswith(("postgres://", "postgresql://")):
        raise ValueError("SELECT_ONLY_POSTGRES_DSN_REQUIRED")
    conn = await asyncpg.connect(dsn, command_timeout=20, statement_cache_size=0)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            meta = await conn.fetchrow(
                "SELECT payload FROM prospective_oos_cohort_v1 WHERE cohort_id=$1",
                cohort.COHORT_ID)
            if meta is None:
                raise ValueError("COHORT_MISSING")
            rows = await conn.fetch(
                "SELECT candidate_id,payload FROM hard_gate_shadow_candidates_v1 "
                "WHERE population=$1 ORDER BY captured_epoch,candidate_id LIMIT 20001",
                cohort.POPULATION)
            if len(rows) > 20000:
                raise ValueError("CANDIDATES_TRUNCATED")
            return prepare(meta["payload"], rows, as_of_epoch=as_of_epoch)
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-epoch", type=float, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    items, manifest = asyncio.run(export_inputs(os.environ.get("DATABASE_URL", ""), args.as_of_epoch))
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    args.prefix.with_suffix(".jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in items),
        encoding="utf-8")
    args.prefix.with_suffix(".json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
