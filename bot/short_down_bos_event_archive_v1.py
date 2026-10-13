"""#609 research-only, local append-only *self-chained* aggTrade capture.

This is NOT an independent market attestation. An attacker who controls the
directory can replace the entire chain; independent external anchoring, durable
storage and exchange execution proofs remain unimplemented. Never use this
module to grant NEXUS/LIVE trading authority.

No network, SQL, exchange client, live engine or credential imports.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re

SOURCE = "BINANCE_USDM_PUBLIC_AGGTRADE_WS"
VERSION = "RESEARCH_AGGTRADE_ARCHIVE_V1"
PER_SEGMENT = 5_000
MAX_SEGMENTS = 500
MAX_BYTES_PER_LINE = 4096
# Hard local disk ceiling per opt-in session. Exceeding it INVALIDATES the run.
MAX_SESSION_BYTES = 16 * 1024 * 1024
_NAME = re.compile(r"[a-z0-9_-]{12,64}\Z")
_SYMBOL = re.compile(r"[A-Z0-9]{2,20}USDT\Z")
AUTHORITY = {
    "research_only": True,
    "self_hash_chained": True,
    "independent_source_attestation": False,
    "durable_remote_copy_proven": False,
    "real_exchange_fills_proven": False,
    "candidate_approval_proven": False,
    "live_allowed": False,
    "promotion_allowed": False,
    "execution_effect": "NONE",
    "decision_effect": "NONE",
}


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _hash(previous, item):
    return hashlib.sha256(bytes.fromhex(previous) + _canonical(item)).hexdigest()


def _plain_positive(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        v = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return v.is_finite() and v > 0


def _normalize(raw, symbol):
    if not isinstance(raw, dict) or raw.get("e") != "aggTrade" or raw.get("s") != symbol:
        raise ValueError("UNTRUSTED_OR_WRONG_EVENT")
    allowed = ("e", "E", "s", "a", "p", "q", "f", "l", "T", "m")
    if any(type(raw.get(k)) is not int for k in ("E", "a", "f", "l", "T")):
        raise ValueError("INVALID_EVENT_ID_OR_TIME")
    if any(raw[k] <= 0 for k in ("E", "a", "f", "l", "T")) or raw["f"] > raw["l"]:
        raise ValueError("INVALID_EVENT_RANGE")
    if not _plain_positive(raw.get("p")) or not _plain_positive(raw.get("q")):
        raise ValueError("INVALID_TRADE_PRICE_OR_VOLUME")
    if type(raw.get("m")) is not bool:
        raise ValueError("INVALID_MAKER_FLAG")
    return {k: raw[k] for k in allowed}


def _open_exclusive(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), "wb")


class Archive:
    """Single WS generation; any gap/reconnect invalidates the current session.

    Existing directory required: do not silently create a service, bucket,
    cloud storage, or count local filesystem as durable external attestation.
    """
    def __init__(self, directory, *, symbol, session_id, generation, started_ms):
        if (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or not isinstance(session_id, str) or not _NAME.fullmatch(session_id)
                or type(generation) is not int or generation <= 0
                or type(started_ms) is not int or started_ms <= 0):
            raise ValueError("INVALID_SESSION")
        directory = Path(directory)
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("ARCHIVE_DIRECTORY_MUST_ALREADY_EXIST")
        self.directory = directory
        self.header = {"version": VERSION, "source": SOURCE,
                       "symbol": symbol, "session_id": session_id,
                       "generation": generation, "started_ms": started_ms}
        self._anchor = hashlib.sha256(_canonical(self.header)).hexdigest()
        self._index = 0
        self._segments = []
        self._segment = None
        self._segment_events = 0
        self._last_id = None
        self._last_stamp = None
        self._bytes_written = 0
        self._dead = False
        self._sealed = False
        # Fail at start on duplicate session, including orphaned segments.
        if list(directory.glob(session_id + ".*")):
            raise FileExistsError("SESSION_ALREADY_EXISTS")

    def _open_segment(self):
        if len(self._segments) >= MAX_SEGMENTS:
            raise ValueError("ARCHIVE_SEGMENT_LIMIT")
        filename = f"{self.header['session_id']}.{len(self._segments):04d}.jsonl"
        self._segment = _open_exclusive(self.directory / filename)
        self._segments.append({"file": filename, "count": 0})
        self._segment_events = 0

    def append(self, raw, *, received_ms):
        if self._dead or self._sealed:
            raise ValueError("ARCHIVE_NOT_ACCEPTING_EVENTS")
        try:
            ev = _normalize(raw, self.header["symbol"])
            if (type(received_ms) is not int or received_ms < self.header["started_ms"]
                    or ev["T"] > received_ms + 5_000
                    or (self._last_id is not None and ev["a"] != self._last_id + 1)
                    or (self._last_stamp is not None and ev["T"] < self._last_stamp)):
                raise ValueError("SOURCE_EVENT_GAP_OR_CLOCK")
            item = {"seq": self._index + 1, "received_ms": received_ms, "event": ev}
            encoded = _canonical(item)
            if len(encoded) > MAX_BYTES_PER_LINE:
                raise ValueError("EVENT_TOO_LARGE")
            if self._segment is None or self._segment_events >= PER_SEGMENT:
                if self._segment is not None:
                    self._segment.flush()
                    os.fsync(self._segment.fileno())
                    self._segment.close()
                self._open_segment()
            new_hash = _hash(self._anchor, item)
            line = _canonical({"prev": self._anchor, "hash": new_hash, **item}) + b"\n"
            if self._bytes_written + len(line) > MAX_SESSION_BYTES:
                raise ValueError("LOCAL_DISK_BUDGET_EXCEEDED")
            self._segment.write(line)
            self._bytes_written += len(line)
            self._anchor = new_hash
            self._index += 1
            self._segment_events += 1
            self._segments[-1]["count"] += 1
            self._last_id = ev["a"]
            self._last_stamp = ev["T"]
        except Exception:
            self.abort()
            raise

    def abort(self):
        """Never silently seal disconnected, gapped or partial stream."""
        self._dead = True
        if self._segment is not None and not self._segment.closed:
            self._segment.close()

    def seal(self):
        if self._dead or self._sealed or self._index < 2 or self._segment is None:
            raise ValueError("ARCHIVE_UNSEALED_OR_INCOMPLETE")
        self._segment.flush()
        os.fsync(self._segment.fileno())
        self._segment.close()
        metadata = {
            **self.header, "event_count": self._index,
            "segments": self._segments, "final_hash": self._anchor,
            "last_agg_id": self._last_id,
            "last_trade_ms": self._last_stamp,
            "archive_data_bytes": self._bytes_written,
            "local_disk_budget_bytes": MAX_SESSION_BYTES,
            **AUTHORITY, "status": "SELF_HASH_CHAIN_SEALED_UNATTESTED",
        }
        with _open_exclusive(
            self.directory / f"{self.header['session_id']}.manifest.json"
        ) as file:
            file.write(_canonical(metadata) + b"\n")
            file.flush()
            os.fsync(file.fileno())
        self._sealed = True
        return metadata


def verify(directory, *, session_id):
    """Recompute all hashes & exact IDs; cannot authenticate the publisher."""
    def reject(reason):
        return {**AUTHORITY, "status": "NOT_PROVEN", "reason": reason}
    if not isinstance(session_id, str) or not _NAME.fullmatch(session_id):
        return reject("BAD_SESSION_ID")
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        return reject("INVALID_DIRECTORY")
    manifest = directory / (session_id + ".manifest.json")
    try:
        if manifest.is_symlink():
            return reject("SYMLINK_MANIFEST")
        raw = manifest.read_bytes()
        if len(raw) > 250_000:
            return reject("OVERSIZED_MANIFEST")
        meta = json.loads(raw)
        if (meta["session_id"] != session_id or meta["version"] != VERSION
                or meta["source"] != SOURCE
                or not _SYMBOL.fullmatch(meta["symbol"])
                or type(meta["generation"]) is not int or meta["generation"] <= 0
                or type(meta["started_ms"]) is not int or meta["started_ms"] <= 0
                or meta.get("status") != "SELF_HASH_CHAIN_SEALED_UNATTESTED"):
            return reject("INVALID_MANIFEST")
        segments = meta["segments"]
        if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
            return reject("INVALID_SEGMENTS")
        header = {k: meta[k] for k in (
            "version", "source", "symbol", "session_id",
            "generation", "started_ms"
        )}
        anchor = hashlib.sha256(_canonical(header)).hexdigest()
        count = 0
        total_bytes = 0
        last_id = last_stamp = None
        for idx, info in enumerate(segments):
            name = f"{session_id}.{idx:04d}.jsonl"
            if (info.get("file") != name or type(info.get("count")) is not int
                    or not 1 <= info["count"] <= PER_SEGMENT):
                return reject("INVALID_SEGMENT_ENTRY")
            path = directory / name
            if path.is_symlink():
                return reject("SYMLINK_SEGMENT")
            with path.open("rb") as stream:
                found = 0
                for line in stream:
                    total_bytes += len(line)
                    if total_bytes > MAX_SESSION_BYTES:
                        return reject("LOCAL_DISK_BUDGET_EXCEEDED")
                    if len(line) > MAX_BYTES_PER_LINE + 200:
                        return reject("OVERSIZED_EVENT")
                    record = json.loads(line)
                    item = {k: record[k] for k in ("seq", "received_ms", "event")}
                    ev = _normalize(item["event"], meta["symbol"])
                    if (record["prev"] != anchor
                            or record["hash"] != _hash(anchor, item)
                            or type(item["seq"]) is not int
                            or item["seq"] != count + 1
                            or type(item["received_ms"]) is not int
                            or item["received_ms"] < meta["started_ms"]
                            or ev["T"] > item["received_ms"] + 5_000
                            or (last_id is not None and ev["a"] != last_id + 1)
                            or (last_stamp is not None and ev["T"] < last_stamp)):
                        return reject("EVENT_TAMPER_OR_GAP")
                    anchor = record["hash"]
                    count += 1
                    found += 1
                    last_id = ev["a"]
                    last_stamp = ev["T"]
                if found != info["count"]:
                    return reject("TRUNCATED_SEGMENT")
        if (count < 2 or count != meta["event_count"]
                or meta.get("archive_data_bytes") != total_bytes
                or meta.get("local_disk_budget_bytes") != MAX_SESSION_BYTES
                or anchor != meta["final_hash"]
                or last_id != meta["last_agg_id"]
                or last_stamp != meta["last_trade_ms"]):
            return reject("MANIFEST_CHAIN_MISMATCH")
        return {
            **AUTHORITY, "status": "SELF_CHAIN_VERIFIED_UNATTESTED",
            "symbol": meta["symbol"], "event_count": count,
            "last_agg_id": last_id, "final_hash": anchor,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return reject("ARCHIVE_MISSING_OR_CORRUPT")


def prepare_anchor_receipt(directory, *, session_id):
    """Metadata-only local digest to be independently posted after review.

    Creating this record neither publishes an anchor nor proves storage
    durability, Binance authenticity, source completeness or actual fills.
    """
    checked = verify(directory, session_id=session_id)
    if checked.get("status") != "SELF_CHAIN_VERIFIED_UNATTESTED":
        return {
            **AUTHORITY, "status": "ANCHOR_REFUSED",
            "reason": checked.get("reason", "ARCHIVE_UNVERIFIED"),
            "github_anchor_published": False,
            "independent_source_attestation": False,
        }
    raw = (Path(directory) / (session_id + ".manifest.json")).read_bytes()
    if len(raw) > 250_000:
        return {**AUTHORITY, "status": "ANCHOR_REFUSED",
                "reason": "OVERSIZED_MANIFEST",
                "github_anchor_published": False}
    meta = json.loads(raw)
    return {
        **AUTHORITY,
        "status": "ANCHOR_PREPARED_NOT_PUBLISHED",
        "version": VERSION,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "final_event_chain_sha256": meta["final_hash"],
        "event_count": checked["event_count"],
        "data_bytes": meta["archive_data_bytes"],
        "github_anchor_published": False,
        "durable_remote_copy_proven": False,
        "independent_source_attestation": False,
        "real_exchange_fills_proven": False,
        "live_allowed": False,
    }
