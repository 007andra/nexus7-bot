"""Recover same-ID OOS approved trade inputs from Railway deploy log exports.

Reads JSON Lines containing {"timestamp":"...","message":"..."} log entries.
Never parses or emits account balance, position size, keys or private fee data.
This is a FALLBACK for an approved-signal subset, not a complete PostgreSQL OOS
ledger and not a substitute for matched approved-vs-rejected outcome evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

APPROVED_MARKER = "[PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1]"
SHADOW_MARKER = "[SHADOW_CANDIDATE_WHILE_LIVE_BLOCKED]"
PAIR_FIELDS = ("symbol", "side", "regime", "setup")
VALUE_FIELDS = ("entry", "stop", "target", "captured_epoch")
RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def _parse(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("message"), str):
        return None
    s = raw["message"]
    if APPROVED_MARKER in s:
        tag = "approval"
        suffix = s.split(APPROVED_MARKER, 1)[1]
    elif SHADOW_MARKER in s:
        tag = "shadow"
        suffix = s.split(SHADOW_MARKER, 1)[1]
    else:
        return None
    fields = dict(RE.findall(suffix))
    cid = fields.get("candidate_id")
    if not cid:
        return None
    return tag, cid, fields


def _number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def extract(logs):
    approvals, shadow = {}, {}
    for row in logs:
        result = _parse(row)
        if result is None:
            continue
        kind, cid, fields = result
        dest = approvals if kind == "approval" else shadow
        if cid in dest:
            previous = dest[cid]
            # Reports re-emit the same candidate repeatedly; any conflict is unsafe.
            for key in PAIR_FIELDS + ("captured_epoch",):
                if key in previous and key in fields:
                    if key == "captured_epoch":
                        if abs(_number(previous[key]) - _number(fields[key])) > 0.01:
                            raise ValueError("CONFLICTING_CAPTURE_TIMESTAMP")
                    elif previous[key] != fields[key]:
                        raise ValueError("CONFLICTING_CANDIDATE_IDENTITY")
        dest[cid] = fields
    dataset, missing = [], []
    for cid, approval in sorted(approvals.items()):
        raw = shadow.get(cid)
        if raw is None:
            missing.append(cid)
            continue
        if any(raw.get(k) != approval.get(k) for k in PAIR_FIELDS):
            raise ValueError("APPROVAL_SHADOW_IDENTITY_MISMATCH")
        cap = _number(raw.get("captured_epoch"))
        proof_cap = _number(approval.get("captured_epoch"))
        if cap is None or proof_cap is None or abs(cap - proof_cap) > .01:
            raise ValueError("APPROVAL_SHADOW_CAPTURE_MISMATCH")
        entry, stop, target = (_number(raw.get(k)) for k in ("entry", "stop", "target"))
        side = raw["side"]
        if (side not in ("LONG", "SHORT") or any(x is None or x <= 0 for x in (entry, stop, target))
                or not ((stop < entry < target) if side == "LONG" else (target < entry < stop))):
            raise ValueError("STOP_TARGET_INVALID")
        dataset.append({
            "candidate_id": cid, "symbol": raw["symbol"],
            "side": side, "regime": raw["regime"], "setup": raw["setup"],
            "captured_epoch": cap, "entry": entry, "stop": stop, "target": target,
            "cohort_decision": "COUNTERFACTUAL_APPROVED",
            "evidence_source": "TWO_RAILWAY_LOG_MARKERS_EXACT_CANDIDATE_ID",
            "cost_snapshot": None,
        })
    canonical = "".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in dataset)
    manifest = {
        "approval_unique_in_export": len(approvals),
        "approved_input_pairs": len(dataset),
        "approved_without_shadow_log": len(missing),
        "missing_ids": missing,
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "cohort_label": "CALIBRATION_GENERALIZATION_V1_APPROVED_LOG_SLICE",
        "rejected_comparator_included": False,
        "net_pnl_evidence": False,
        "authority": "SHADOW_RESEARCH_ONLY_NO_LIVE",
    }
    return dataset, manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("railway_jsonl", type=Path)
    p.add_argument("--prefix", required=True, type=Path)
    args = p.parse_args()
    logs = []
    for i, line in enumerate(args.railway_jsonl.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSON object required")
            logs.append(row)
        except (ValueError, TypeError) as exc:
            p.error(f"line {i}: {exc}")
    dataset, manifest = extract(logs)
    args.prefix.parent.mkdir(parents=True, exist_ok=True)
    args.prefix.with_suffix(".jsonl").write_text(
        "".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in dataset),
        encoding="utf-8",
    )
    args.prefix.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


if __name__ == "__main__":
    main()
