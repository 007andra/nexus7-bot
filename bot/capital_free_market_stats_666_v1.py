"""#666 frozen market-direction statistics; OFFLINE, standard library only.

Implements the review gate exactly as preregistered in issue #666, fixed and
tested BEFORE any real observation exists:

- denominator: all 60 frozen members with paired 60m/240m verified returns,
  >=8 symbols, <=12 per symbol; otherwise INSUFFICIENT_EVIDENCE;
- per horizon: mean > 0, positive fraction >= 0.55, one-sided 95% lower
  bound of the mean > 0 from 10,000 cluster-bootstrap replicates (seed
  609202613, clusters (symbol, UTC 4h start)), every leave-one-symbol-out
  mean > 0, and at least 10 distinct time clusters;
- any failed gate -> MARKET_SIGNAL_EVIDENCE_FAIL. A full pass is only
  MARKET_ONLY_REVIEW_READY: never NET, fills, profit, #609 promotion or LIVE.

Interpretations fixed here, before data (flagged for operator confirmation):
1. "10 distinct time clusters" is enforced on BOTH distinct UTC 4h starts and
   distinct (symbol, 4h) clusters (the stricter reading).
2. Leave-one-symbol-out passes only if the mean stays > 0 for EVERY symbol
   removed, not only the most frequent one.
3. Lower bound = sorted replicate means[floor(0.05 * 10000)] (0-based 500th).
4. Each horizon uses its own random.Random(609202613); clusters are drawn
   with replacement in sorted key order, K draws for K clusters. A replicate
   with zero observations cannot occur with non-empty clusters; if it ever
   does, it scores -inf, which can only lower the bound (fail closed).
"""
from __future__ import annotations

from collections import Counter
import math
import random

TARGET = 60
MAX_PER_SYMBOL = 12
MIN_SYMBOLS = 8
HORIZONS = (60, 240)
REPLICATES = 10_000
SEED = 609202613
MIN_POSITIVE_FRACTION = 0.55
MIN_TIME_CLUSTERS = 10
CLUSTER_MS = 4 * 3_600_000
LOWER_QUANTILE = 0.05
AUTHORITY = {
    "market_only": True, "research_only": True,
    "executable_net_proven": False, "fills_proven": False,
    "source_attested": False, "canonical_nexus_approval": False,
    "eligible_for_609_enrollment": False, "promotion_allowed": False,
    "live_allowed": False, "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _quantile(sorted_values, q):
    """Inclusive linear-interpolation quantile for descriptive reporting."""
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _mean(values):
    return sum(values) / len(values) if values else float("-inf")


def cluster_bootstrap_lower_bound(observations, *, replicates=REPLICATES,
                                  seed=SEED, quantile=LOWER_QUANTILE):
    """observations: iterable of (cluster_key, value). Returns (bound, means)."""
    clusters = {}
    for key, value in observations:
        clusters.setdefault(key, []).append(value)
    keys = sorted(clusters)
    if not keys or replicates <= 0:
        return float("-inf"), []
    rng = random.Random(seed)
    k = len(keys)
    means = []
    for _ in range(replicates):
        total = count = 0
        for _ in range(k):
            members = clusters[keys[rng.randrange(k)]]
            total += sum(members)
            count += len(members)
        means.append(total / count if count else float("-inf"))
    means.sort()
    return means[math.floor(quantile * replicates)], means


def _horizon_report(pairs, horizon):
    key = f"raw_short_return_{horizon}m"
    values = [p[key] for p in pairs]
    ordered = sorted(values)
    observations = [((p["symbol"], p["captured_ms"] // CLUSTER_MS * CLUSTER_MS),
                     p[key]) for p in pairs]
    bound, _ = cluster_bootstrap_lower_bound(observations)
    symbols = sorted({p["symbol"] for p in pairs})
    loso = {s: _mean([p[key] for p in pairs if p["symbol"] != s])
            for s in symbols}
    by_symbol = {s: _mean([p[key] for p in pairs if p["symbol"] == s])
                 for s in symbols}
    time_buckets = Counter(p["captured_ms"] // CLUSTER_MS for p in pairs)
    clusters = {c for c, _ in observations}
    mean = _mean(values)
    positive = sum(v > 0 for v in values) / len(values)
    gates = {
        "mean_positive": mean > 0,
        "positive_fraction": positive >= MIN_POSITIVE_FRACTION,
        "bootstrap_lower_bound_positive": bound > 0,
        "leave_one_symbol_out_all_positive": all(m > 0 for m in loso.values()),
        "min_time_clusters": (len(time_buckets) >= MIN_TIME_CLUSTERS
                              and len(clusters) >= MIN_TIME_CLUSTERS),
    }
    return {
        "horizon_minutes": horizon, "n": len(values), "mean": mean,
        "median": _quantile(ordered, 0.5), "p05": _quantile(ordered, 0.05),
        "p95": _quantile(ordered, 0.95), "min": ordered[0],
        "worst_5": ordered[:5], "positive_fraction": positive,
        "bootstrap_lower_bound_95_one_sided": bound,
        "bootstrap_replicates": REPLICATES, "bootstrap_seed": SEED,
        "distinct_time_buckets_4h": len(time_buckets),
        "distinct_symbol_time_clusters": len(clusters),
        "max_observations_in_one_4h_bucket": max(time_buckets.values()),
        "leave_one_symbol_out_means": loso, "per_symbol_means": by_symbol,
        "gates": gates, "passed": all(gates.values()),
    }


def _pearson(xs, ys):
    mx, my = _mean(xs), _mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _valid_pair(p):
    return (isinstance(p, dict) and isinstance(p.get("symbol"), str)
            and type(p.get("captured_ms")) is int
            and isinstance(p.get("candidate_id"), str)
            and all(type(p.get(f"raw_short_return_{h}m")) is float
                    and math.isfinite(p[f"raw_short_return_{h}m"])
                    for h in HORIZONS)
            and p.get("fills_proven") is False
            and p.get("executable_net_proven") is False)


def evaluate(paired):
    """Apply the frozen gate to `paired_market_observations` output."""
    def insufficient(reason, **extra):
        return {**AUTHORITY, "status": "INSUFFICIENT_EVIDENCE",
                "reason": reason, **extra}
    if (not isinstance(paired, dict)
            or paired.get("reason") != "RAW_MARKET_PAIRS_UNATTESTED_NOT_EXECUTABLE"
            or paired.get("live_allowed") is not False
            or not isinstance(paired.get("pairs"), tuple)):
        return insufficient("INVALID_PAIRED_INPUT")
    pairs = paired["pairs"]
    missing = paired.get("missing_or_unverified")
    if (paired.get("target_met") is not True or len(pairs) != TARGET
            or missing != 0):
        return insufficient("DENOMINATOR_INCOMPLETE", paired_count=len(pairs),
                            missing_or_unverified=missing)
    if not all(_valid_pair(p) for p in pairs):
        return insufficient("MALFORMED_PAIR")
    if len({p["candidate_id"] for p in pairs}) != TARGET:
        return insufficient("DUPLICATE_CANDIDATE")
    counts = Counter(p["symbol"] for p in pairs)
    if len(counts) < MIN_SYMBOLS or max(counts.values()) > MAX_PER_SYMBOL:
        return insufficient("SYMBOL_COVERAGE_OR_CAP", symbol_counts=dict(counts))
    reports = {h: _horizon_report(pairs, h) for h in HORIZONS}
    passed = all(r["passed"] for r in reports.values())
    return {
        **AUTHORITY,
        "status": "MARKET_ONLY_REVIEW_READY" if passed
        else "MARKET_SIGNAL_EVIDENCE_FAIL",
        "horizons": reports, "symbol_counts": dict(counts),
        "top_symbol_share": max(counts.values()) / TARGET,
        "corr_60m_240m": _pearson(
            [p["raw_short_return_60m"] for p in pairs],
            [p["raw_short_return_240m"] for p in pairs]),
        "missing_or_unverified": 0,
    }
