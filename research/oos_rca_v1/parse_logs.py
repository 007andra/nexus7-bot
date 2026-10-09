"""Normalize Railway research logs to minimal candidate records."""
import json
import re
from datetime import datetime, timezone

FIELDS = frozenset(("candidate_id", "symbol", "side", "setup", "regime",
    "captured_epoch", "horizon", "outcome", "future_return", "verified",
    "nexus_called", "nexus_allowed", "approval_state", "score", "return60",
    "return240", "terminal_reason", "bbo_valid", "bbo_age_ms",
    "live_total_cost_bps", "spread_bps", "observation_start"))
MARKERS = {
    "PROSPECTIVE_OOS_APPROVED_CANDIDATE_V1": "approval",
    "CANDIDATE_TERMINAL": "terminal",
    "SHORT_DOWN_BOS_V2_MEMBER_PROOF": "outcome",
    "COST_SHADOW_BBO_V3_COST_ONLY": "cost",
}
KV = re.compile(r"(?<!\w)([A-Za-z_][\w]*)=([^\s]+)")

def utc_epoch(value):
    if not value:
        return None
    try:
        if isinstance(value, (float, int)):
            return float(value)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None

def parse_record(obj):
    """Ignore unrecognized markers and strip non-whitelisted data, including secrets."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError:
            obj = {"message": obj}
    if not isinstance(obj, dict):
        return None
    if obj.get("record_type") in MARKERS.values():
        return {k: v for k, v in obj.items() if k in FIELDS or k in ("record_type", "event_epoch")}
    message = obj.get("message")
    if not isinstance(message, str):
        return None
    marker = next((m for m in MARKERS if "[" + m + "]" in message), None)
    if not marker:
        return None
    values = dict(KV.findall(message.split("[" + marker + "]", 1)[1]))
    if not values.get("candidate_id"):
        return None
    output = {k: v for k, v in values.items() if k in FIELDS}
    output["record_type"] = MARKERS[marker]
    timestamp = utc_epoch(obj.get("timestamp"))
    if timestamp is not None:
        output["event_epoch"] = timestamp
    return output
