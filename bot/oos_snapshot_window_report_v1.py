"""Read-only OOS latency window audit for GitHub issue #596.

Input is exported, already-redacted Railway runtime log TEXT. The module
does not connect to a database/exchange, log raw payloads or change state.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
from statistics import median

TAG = "[PROSPECTIVE_OOS_SNAPSHOT_LATENCY_V1]"
MODES = ("LEGACY_DDL_GUARDED", "IMMUTABLE_READ_FIRST")
ALLOWED_STATUSES = {"OK", "TIMEOUT", "ERROR", "CANCELLED"}
MEASURES = (
    "elapsed_ms",
    "metadata_ms",
    "metadata_fetch_ms",
    "db_exec_ms",
    "lock_wait_ms",
    "outcomes_lock_wait_ms",
    "db_fetch_ms",
    "rows_fetched",
)
REQUIRED = (
    "status", "elapsed_ms", "timeout_limit_ms", "metadata_ms",
    "metadata_fetch_ms", "db_exec_ms", "exec_calls", "lock_wait_ms",
    "db_fetch_ms", "rows_fetched", "metadata_read_mode",
)
TOKEN = re.compile(r"([a-z][a-z0-9_]*)=([^\s]+)")
TIMESTAMP = re.compile(r"20\d{2}-\d\d-\d\d[T ]\d\d:\d\d:\d\d")


def _timestamp(line: str) -> datetime:
    found = TIMESTAMP.search(line.split(TAG, 1)[0])
    if found is None:
        raise ValueError("OOS_LOG_MISSING_TIMESTAMP")
    # Second precision is sufficient for the existing >=5m observation cadence.
    return datetime.strptime(found.group(0).replace("T", " "), "%Y-%m-%d %H:%M:%S")


def parse_export(text: str, *, expected_mode: str) -> list[dict]:
    """Parse only allowlisted summary fields; never return raw log or payload."""
    if expected_mode not in MODES:
        raise ValueError("OOS_INVALID_EXPECTED_MODE")
    samples = []
    seen = set()
    for line in text.splitlines():
        if TAG not in line:
            continue
        ts = _timestamp(line)
        fields = dict(TOKEN.findall(line.split(TAG, 1)[1]))
        if any(k not in fields for k in REQUIRED):
            raise ValueError("OOS_LOG_INCOMPLETE_SUMMARY")
        if fields["metadata_read_mode"] != expected_mode:
            raise ValueError("OOS_MIXED_METADATA_MODES")
        if fields["status"] not in ALLOWED_STATUSES:
            raise ValueError("OOS_INVALID_STATUS")
        if ts in seen:
            raise ValueError("OOS_DUPLICATE_SNAPSHOT_TIMESTAMP")
        seen.add(ts)
        item = {"timestamp": ts, "status": fields["status"]}
        for k in set(MEASURES) | {"timeout_limit_ms", "exec_calls"}:
            if k not in fields:
                if k == "outcomes_lock_wait_ms":
                    item[k] = 0.0
                    continue
                raise ValueError("OOS_LOG_MISSING_METRIC")
            try:
                value = float(fields[k])
            except (TypeError, ValueError) as exc:
                raise ValueError("OOS_LOG_INVALID_NUMERIC_METRIC") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError("OOS_LOG_INVALID_NUMERIC_METRIC")
            item[k] = value
        if item["timeout_limit_ms"] != 3000.0:
            raise ValueError("OOS_TIMEOUT_BUDGET_CHANGED")
        item["owner"] = fields.get("max_wait_owner_at_start", "NOT_RECORDED")
        # Never emit owner values; only a coarse identity-known count.
        item["owner_known"] = item["owner"].startswith(
            ("serialized:", "fetchall:", "fetchone:", "exec:")
        )
        samples.append(item)
    return sorted(samples, key=lambda s: s["timestamp"])


def _nearest_rank(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize(samples: list[dict], *, mode: str, min_n: int = 6,
              max_n: int = 12, max_gap_seconds: int = 480) -> dict:
    """Report descriptive ranges even if too small, but withhold p95.

    p95 uses nearest-rank: with 6-12 snapshots p95 equals the maximum,
    making conclusions about rare timeouts inherently unstable.
    """
    if mode not in MODES:
        raise ValueError("OOS_INVALID_EXPECTED_MODE")
    if not 2 <= min_n <= max_n <= 144:
        raise ValueError("OOS_INVALID_SAMPLE_WINDOW")
    window = sorted(samples, key=lambda x: x["timestamp"])[-max_n:]
    gaps = [
        (b["timestamp"] - a["timestamp"]).total_seconds()
        for a, b in zip(window, window[1:])
    ]
    continuity = all(0 < seconds <= max_gap_seconds for seconds in gaps)
    n = len(window)
    completed = sum(s["status"] == "OK" for s in window)
    timed_out = sum(s["status"] == "TIMEOUT" for s in window)
    other_failed = n - completed - timed_out
    ready = n >= min_n and continuity
    stats = {}
    for key in MEASURES:
        data = [s[key] for s in window]
        stats[key] = {
            "median": round(median(data), 3) if data else None,
            "max": round(max(data), 3) if data else None,
            "p95_nearest_rank": round(_nearest_rank(data, .95), 3)
            if ready else None,
        }
    return {
        "research_only": True,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
        "live_allowed": False,
        "promotion_allowed": False,
        "mode": mode,
        "status": "WINDOW_READY_DESCRIPTIVE_ONLY" if ready
        else "INSUFFICIENT_OR_NONCONSECUTIVE",
        "n": n,
        "min_required": min_n,
        "max_window": max_n,
        "consecutive_by_480s_gap": continuity,
        "first_utc": window[0]["timestamp"].isoformat() + "Z" if window else None,
        "last_utc": window[-1]["timestamp"].isoformat() + "Z" if window else None,
        "ok": completed,
        "timeouts": timed_out,
        "errors_or_cancellations": other_failed,
        "timeout_rate_observed": round(timed_out / n, 5) if n else None,
        "owner_known_at_wait_start_events": sum(s["owner_known"] for s in window),
        "successful_readfirst_with_ddl": sum(
            s["status"] == "OK" and s["exec_calls"] > 0
            for s in window if mode == "IMMUTABLE_READ_FIRST"
        ),
        "p95_caution": "NEAREST_RANK_EQUALS_MAX_WITH_N_6_TO_12",
        "statistics": stats,
    }


def compare_exports(baseline_text: str | None, optimized_text: str,
                    *, min_n: int = 6, max_n: int = 12) -> dict:
    """Never declare a speedup or release; contexts may not be randomized."""
    optimized = summarize(
        parse_export(optimized_text, expected_mode="IMMUTABLE_READ_FIRST"),
        mode="IMMUTABLE_READ_FIRST", min_n=min_n, max_n=max_n,
    )
    baseline = None
    if baseline_text is not None:
        baseline = summarize(
            parse_export(baseline_text, expected_mode="LEGACY_DDL_GUARDED"),
            mode="LEGACY_DDL_GUARDED", min_n=min_n, max_n=max_n,
        )
    return {
        "research_only": True, "causal_speedup_proven": False,
        "timeout_budget_ms": 3000,
        "readiness": "ENOUGH_FOR_DESCRIPTIVE_REVIEW_ONLY"
        if optimized["status"] == "WINDOW_READY_DESCRIPTIVE_ONLY"
        and (baseline is None or baseline["status"] == "WINDOW_READY_DESCRIPTIVE_ONLY")
        else "INSUFFICIENT_COMPARABLE_WINDOW",
        "baseline": baseline, "optimized": optimized,
        "required_next_evidence": (
            "natural_samples_and_lock_wait_distribution",
            "postgres_server_vs_transport_vs_client_latency_if_fetch_outliers",
            "frozen_oos_economic_evidence_separate_from_latency",
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--min-n", type=int, default=6)
    parser.add_argument("--max-n", type=int, default=12)
    args = parser.parse_args(argv)
    result = compare_exports(
        args.baseline.read_text(encoding="utf-8")
        if args.baseline else None,
        args.optimized.read_text(encoding="utf-8"),
        min_n=args.min_n, max_n=args.max_n,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
