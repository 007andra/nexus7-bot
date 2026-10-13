"""#666 append-only membership manifest; local, self-chained, OFFLINE.

Fixes cohort membership independently of outcomes: entries hold only the
capture-time envelope (no exit prices or returns), every decision (enrolled,
over-cap, duplicate, invalid, post-target) is written before any outcome
exists, and the header freezes ``effective_t0`` before the first capture.

Limits, stated plainly: a self-hash chain detects edits of a retained file
but anyone holding the file can rewrite it wholesale. Durability and
non-retroactivity need an external witness: ``anchor_receipt`` returns the
current head digest to be posted on issue #666 (server-timestamped) within
the same 15m bar as each append. Nothing here posts, uploads or proves that.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from bot import capital_free_market_oos_666_v1 as contract

VERSION = "CAPITAL_FREE_MANIFEST_666_V1"
MAX_LINES = 5_000
MAX_LINE_BYTES = 4_096
ENVELOPE_KEYS = frozenset((
    "candidate_id", "symbol", "captured_ms", "observed_ms", "population",
    "side", "regime", "setup", "market_only", "nexus_called", "nexus_allowed",
    "live_eligible", "decision_effect", "execution_effect", "config_sha",
    "snapshot_complete", "no_future_inputs", "source_sha256",
    "implementation_sha", "entry_close",
))
KINDS = ("ENROLLED", "EXCLUDED_SYMBOL_CAP", "EXCLUDED_DUPLICATE",
         "EXCLUDED_INVALID", "EXCLUDED_POST_TARGET")
AUTHORITY = {
    "self_hash_chained": True, "external_anchor_published": False,
    "durable_remote_copy_proven": False, "source_attested": False,
    "cohort_membership_proven": False, "live_allowed": False,
    "promotion_allowed": False, "decision_effect": "NONE",
    "execution_effect": "NONE",
}


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _chain(previous: str, body: dict) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + _canonical(body)).hexdigest()


def _hex(value, n):
    return (isinstance(value, str) and len(value) == n
            and all(c in "0123456789abcdef" for c in value))


def _read(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("MANIFEST_MISSING_OR_SYMLINK")
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    if lines[-1] != b"" or len(lines) - 1 > MAX_LINES:
        raise ValueError("MANIFEST_TRUNCATED_OR_OVERSIZED")
    return [json.loads(x) for x in lines[:-1]]


def _replay(records):
    """Validate the full chain and rules; return (head, state)."""
    if not records:
        raise ValueError("EMPTY_MANIFEST")
    header = records[0]
    body = {k: v for k, v in header.items() if k != "hash"}
    if (set(header) != {"seq", "kind", "version", "cohort_id",
                        "effective_t0_ms", "recorded_ms", "implementation_sha",
                        "config_sha", "hash"}
            or header["seq"] != 0 or header["kind"] != "HEADER"
            or header["version"] != VERSION
            or header["cohort_id"] != contract.COHORT_ID
            or type(header["effective_t0_ms"]) is not int
            or header["effective_t0_ms"] < contract.ISSUE_CREATED_MS
            or type(header["recorded_ms"]) is not int
            or header["recorded_ms"] > header["effective_t0_ms"]
            or not _hex(header["implementation_sha"], 40)
            or header["config_sha"] != contract.CONFIG_SOURCE_SHA
            or header["hash"] != _chain("0" * 64, body)):
        raise ValueError("INVALID_HEADER")
    head = header["hash"]
    state = {"t0": header["effective_t0_ms"], "enrolled": [], "seen": {},
             "counts": {}, "last_key": None, "excluded": 0,
             "implementation_sha": header["implementation_sha"]}
    for i, rec in enumerate(records[1:], start=1):
        body = {k: v for k, v in rec.items() if k not in ("prev", "hash")}
        if (set(rec) != {"seq", "kind", "recorded_ms", "envelope", "prev", "hash"}
                or rec["seq"] != i or rec["kind"] not in KINDS
                or rec["prev"] != head or rec["hash"] != _chain(head, body)):
            raise ValueError("CHAIN_BROKEN")
        expected = _classify(state, rec["envelope"], rec["recorded_ms"])
        if expected != rec["kind"]:
            raise ValueError("CLASSIFICATION_MISMATCH")
        _apply(state, rec["envelope"], rec["kind"])
        head = rec["hash"]
    return head, state


def _classify(state, env, recorded_ms):
    if (not isinstance(env, dict) or set(env) != ENVELOPE_KEYS
            or not contract._eligible(env, state["t0"])
            or env["implementation_sha"] != state["implementation_sha"]
            or type(recorded_ms) is not int
            or not env["captured_ms"] <= recorded_ms
            < env["captured_ms"] + contract.INTERVAL_MS):
        return "EXCLUDED_INVALID"
    key = (env["captured_ms"], contract.UNIVERSE.index(env["symbol"]),
           env["candidate_id"])
    if env["candidate_id"] in state["seen"]:
        if state["seen"][env["candidate_id"]] != env:
            raise ValueError("CONFLICTING_DUPLICATE_ID")
        return "EXCLUDED_DUPLICATE"
    if state["last_key"] is not None and key < state["last_key"]:
        # Out-of-order arrival would let selection depend on write timing.
        return "EXCLUDED_INVALID"
    if len(state["enrolled"]) >= contract.TARGET:
        return "EXCLUDED_POST_TARGET"
    if state["counts"].get(env["symbol"], 0) >= contract.MAX_PER_SYMBOL:
        return "EXCLUDED_SYMBOL_CAP"
    return "ENROLLED"


def _apply(state, env, kind):
    if kind in ("EXCLUDED_INVALID", "EXCLUDED_DUPLICATE"):
        state["excluded"] += 1
        return
    state["seen"][env["candidate_id"]] = env
    state["last_key"] = (env["captured_ms"],
                         contract.UNIVERSE.index(env["symbol"]),
                         env["candidate_id"])
    if kind == "ENROLLED":
        state["enrolled"].append(env)
        state["counts"][env["symbol"]] = state["counts"].get(env["symbol"], 0) + 1
    else:
        state["excluded"] += 1


def create(path, *, effective_t0_ms, implementation_sha, recorded_ms):
    """Write the header exclusively; refuses to overwrite or backdate t0."""
    path = Path(path)
    body = {"seq": 0, "kind": "HEADER", "version": VERSION,
            "cohort_id": contract.COHORT_ID,
            "effective_t0_ms": effective_t0_ms, "recorded_ms": recorded_ms,
            "implementation_sha": implementation_sha,
            "config_sha": contract.CONFIG_SOURCE_SHA}
    header = {**body, "hash": _chain("0" * 64, body)}
    _replay([header])  # validates every header rule before touching disk
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as f:
        f.write(_canonical(header) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    return header["hash"]


def append(path, envelope, *, recorded_ms):
    """Classify and append one envelope; returns the recorded kind."""
    path = Path(path)
    records = _read(path)
    head, state = _replay(records)
    # Only allowlisted capture-time keys are ever written, even for rows
    # classified invalid: no outcome, account or free-form payload persists.
    env = ({k: envelope[k] for k in ENVELOPE_KEYS if k in envelope}
           if isinstance(envelope, dict) else None)
    kind = _classify(state, env, recorded_ms)
    body = {"seq": len(records), "kind": kind, "recorded_ms": recorded_ms,
            "envelope": env}
    line = _canonical({**body, "prev": head, "hash": _chain(head, body)})
    if len(line) > MAX_LINE_BYTES or len(records) + 1 > MAX_LINES:
        raise ValueError("MANIFEST_LIMIT")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "ab") as f:
        f.write(line + b"\n")
        f.flush()
        os.fsync(f.fileno())
    return kind


def verify(path):
    """Replay the chain and rules; enrolled rows must match freeze_offline."""
    def reject(reason):
        return {**AUTHORITY, "status": "MANIFEST_NOT_PROVEN", "reason": reason}
    try:
        head, state = _replay(_read(Path(path)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
        return reject(reason if reason.isupper() else "MANIFEST_CORRUPT")
    enrolled = tuple(state["enrolled"])
    selection = contract.freeze_offline(
        list(enrolled), effective_cutoff_ms=state["t0"], source_complete=True)
    if selection.get("selected") != enrolled:
        return reject("SELECTION_DIVERGES_FROM_FROZEN_RULE")
    return {
        **AUTHORITY, "status": "MANIFEST_SELF_CHAIN_VERIFIED_UNANCHORED",
        "head_sha256": head, "effective_t0_ms": state["t0"],
        "enrolled_count": len(enrolled), "excluded_count": state["excluded"],
        "frozen_first_60_shape": selection["frozen_first_60_shape"],
        "enrolled": enrolled,
    }


def anchor_receipt(path):
    """Digest to post on issue #666; posting is a separate reviewed step."""
    checked = verify(path)
    if checked["status"] != "MANIFEST_SELF_CHAIN_VERIFIED_UNANCHORED":
        return {**AUTHORITY, "status": "ANCHOR_REFUSED",
                "reason": checked["reason"]}
    raw = Path(path).read_bytes()
    return {**AUTHORITY, "status": "ANCHOR_PREPARED_NOT_PUBLISHED",
            "cohort_id": contract.COHORT_ID,
            "head_sha256": checked["head_sha256"],
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "effective_t0_ms": checked["effective_t0_ms"],
            "enrolled_count": checked["enrolled_count"],
            "excluded_count": checked["excluded_count"]}
