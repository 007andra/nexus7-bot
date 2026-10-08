"""Reconstruct SAME frozen OOS cohort trade inputs from two Railway log markers.

Private log input only, public-safe source code. Does NOT execute orders or
contain database credentials. Reconstructs both counterfactual approved AND
rejected candidates from [MIN_ORDER_COUNTERFACTUAL_NEXUS_V1_CANDIDATE] and
[SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED] using exact candidate IDs.
Requires the operator to supply the frozen cohort started_epoch documented
by PROSPECTIVE_OOS_COHORT_V1; does not modify that metadata or any outcomes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

DECISION = "[MIN_ORDER_COUNTERFACTUAL_NEXUS_V1_CANDIDATE]"
SOURCE = "[SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED]"
KEYS = ("symbol", "setup")
KV = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def _number(v):
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _parse(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("message"), str):
        return None
    text = obj["message"]
    for marker, kind in ((DECISION, "decision"), (SOURCE, "shadow")):
        if marker in text:
            d = dict(KV.findall(text.split(marker, 1)[1]))
            if d.get("candidate_id"):
                return kind, d
    return None


def reconstruct(logs, *, cutoff_epoch, as_of_epoch):
    cutoff, as_of = _number(cutoff_epoch), _number(as_of_epoch)
    if cutoff is None or as_of is None or cutoff <= 0 or cutoff >= as_of:
        raise ValueError("FROZEN_TIMESTAMPS_REQUIRED")
    decisions, raw_rows = {}, {}
    for item in logs:
        parsed = _parse(item)
        if parsed is None:
            continue
        kind, data = parsed
        cid = data["candidate_id"]
        table = decisions if kind == "decision" else raw_rows
        prior = table.get(cid)
        if prior is not None:
            # Re-emitted records are normal, but material conflicts fail closed.
            for k in ("symbol", "setup", "side", "regime", "entry", "stop", "target",
                      "allowed", "captured_epoch", "risk_epoch_traversal_credit"):
                if k in prior and k in data and prior[k] != data[k]:
                    raise ValueError("CONFLICTING_LOG_RECORD_" + k.upper())
        table[cid] = data
    output, excluded = [], Counter()
    for cid, decision in sorted(decisions.items()):
        raw = raw_rows.get(cid)
        if raw is None:
            excluded["DECISION_WITHOUT_SHADOW_SOURCE"] += 1
            continue
        capture = _number(raw.get("captured_epoch"))
        if capture is None:
            raise ValueError("CAPTURE_TIME_INVALID")
        if capture < cutoff:
            excluded["BEFORE_FROZEN_CUTOFF"] += 1
            continue
        if capture > as_of:
            excluded["AFTER_FROZEN_AS_OF"] += 1
            continue
        if (any(decision.get(k) != raw.get(k) for k in KEYS)
                or raw.get("population") != "HARD_GATE_SHADOW"
                or decision.get("population") != "HARD_GATE_SHADOW"
                or raw.get("shadow_only") != "true"
                or decision.get("shadow_only") != "true"
                or raw.get("live_eligible") != "false"
                or decision.get("live_eligible") != "false"
                or raw.get("decision_effect") != "NONE"
                or raw.get("execution_effect") != "NONE"
                or decision.get("risk_epoch_traversal_credit") != "false"):
            raise ValueError("CANDIDATE_COHORT_AUTHORITY_OR_IDENTITY_INVALID")
        allowed = decision.get("allowed")
        if allowed not in ("true", "false"):
            raise ValueError("INDETERMINATE_DECISION_NOT_ALLOWED_AS_REJECTED")
        side = raw.get("side")
        symbol = raw.get("symbol")
        setup = raw.get("setup")
        if not cid.startswith(f"HARD_GATE_SHADOW:{symbol}:{side}:{setup}:"):
            raise ValueError("CANDIDATE_ID_COMPONENT_MISMATCH")
        ent, sl, tp = (_number(raw.get(k)) for k in ("entry", "stop", "target"))
        if (side not in ("LONG", "SHORT") or any(x is None or x <= 0 for x in (ent, sl, tp))
                or not ((sl < ent < tp) if side == "LONG" else (tp < ent < sl))):
            raise ValueError("INVALID_SIGNAL_LEVELS")
        output.append({
            "candidate_id": cid, "symbol": symbol, "side": side,
            "setup": setup, "regime": raw.get("regime"),
            "captured_epoch": capture, "entry": ent, "stop": sl, "target": tp,
            "cohort_decision": ("COUNTERFACTUAL_APPROVED" if allowed == "true"
                                else "COUNTERFACTUAL_REJECTED"),
            "cost_snapshot": None,
            "evidence_source": "RAILWAY_EXACT_ID_FROZEN_COUNTERFACTUAL_LOG_JOIN",
        })
    output.sort(key=lambda x: (x["captured_epoch"], x["candidate_id"]))
    canonical = "".join(json.dumps(x, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for x in output)
    flags = Counter(x["cohort_decision"] for x in output)
    report = {
        "frozen_cutoff_epoch": cutoff, "as_of_epoch": as_of,
        "matched_candidates": len(output), "approved": flags["COUNTERFACTUAL_APPROVED"],
        "rejected": flags["COUNTERFACTUAL_REJECTED"],
        "excluded": dict(sorted(excluded.items())), "candidate_rows_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "cost_snapshot_available": False, "canonical_postgres_confirmed": False,
        "return_basis": "INPUT_ONLY_NO_MARKET_RETURNS",
        "live_allowed": False, "authority": "OFFLINE_READ_ONLY_RESEARCH",
    }
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("railway_jsonl", type=Path)
    parser.add_argument("--cutoff-epoch", type=float, required=True)
    parser.add_argument("--as-of-epoch", type=float, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    records = []
    for i, line in enumerate(args.railway_jsonl.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSON object required")
                records.append(row)
            except (ValueError, TypeError) as exc:
                parser.error(f"invalid JSONL record at line {i}: {exc}")
    rows, report = reconstruct(records, cutoff_epoch=args.cutoff_epoch, as_of_epoch=args.as_of_epoch)
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    args.prefix.with_suffix(".jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in rows), encoding="utf-8")
    args.prefix.with_suffix(".json").write_text(
        json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
