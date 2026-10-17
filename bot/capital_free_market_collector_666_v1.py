"""#666 real-time public-market collector; research process, NO LIVE.

Runs as its own process (never imported by the trading runtime). At every
completed UTC 15m boundary ``t`` it reads PUBLIC Binance USD-M klines (no API
key), builds one closed-candle snapshot per universe symbol, runs the
isolated observer, and records the result:

- every symbol/boundary attempt -> ``observations.jsonl`` (status + raw hash);
- every candidate envelope -> ``manifest.jsonl`` (enrolled or excluded,
  written before any outcome exists);
- for enrolled members only, the 15m candle closing at ``t+60m`` and
  ``t+240m`` is read inside that same 15m window -> ``outcomes.jsonl``. A
  missed window is never filled later: the pair stays censored.

Both chains are self-hashed. Their heads are submitted to public
OpenTimestamps calendars (free, credential-free) and printed to the log, so
an external witness exists for each state. Raw REST bodies are kept gzipped
only for boundaries that produced an envelope or outcome.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

from bot import capital_free_market_manifest_666_v1 as manifest
from bot import capital_free_market_observer_666_v1 as observer
from bot import capital_free_market_oos_666_v1 as contract
from bot import capital_free_market_stats_666_v1 as stats

BASE_URL = "https://fapi.binance.com"
P15, P1H, P4H = 900_000, 3_600_000, 14_400_000
FETCH = (("k15", "15m", P15, 200), ("k1h", "1h", P1H, 100),
         ("k4h", "4h", P4H, 120))
SETTLE_MS = 20_000          # wait after the boundary before reading candles
OTS_CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
)
OUTCOME_KEYS = ("candidate_id", "symbol", "horizon_minutes", "close_ms",
                "observed_ms", "complete_candle", "no_interpolation",
                "source_sha256", "exit_close")


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def http_get(path, params, *, timeout=10.0):
    """Public GET; returns (raw bytes, parsed JSON). No credentials, ever."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    request = urllib.request.Request(
        f"{BASE_URL}{path}?{query}",
        headers={"User-Agent": "nexus7-research-666/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read(4 * 1024 * 1024)
    return raw, json.loads(raw)


def build_snapshot(symbol, t, observed_ms, bodies):
    """bodies: {key: parsed REST rows}. Keeps only rows closed before t."""
    snap = {"symbol": symbol, "boundary_ms": t, "observed_ms": observed_ms,
            "source": "BINANCE_USDM_PUBLIC_KLINES"}
    for key, _, period, limit in FETCH:
        rows = []
        for r in bodies[key]:
            if not isinstance(r, list) or len(r) < 7:
                raise ValueError("MALFORMED_REST_ROW")
            if r[6] >= t:      # still forming at t: not usable
                continue
            rows.append({"ts": r[0], "close_ms": r[6], "o": r[1], "h": r[2],
                         "l": r[3], "c": r[4], "v": r[5]})
        snap[key] = rows[-limit:]
    return snap


class Chain:
    """Append-only self-hashed JSONL for observations and outcomes."""

    def __init__(self, path: Path):
        self.path = path
        self.head = "0" * 64
        self.count = 0
        if path.exists():
            for line in path.read_bytes().splitlines():
                rec = json.loads(line)
                body = {k: v for k, v in rec.items() if k not in ("prev", "hash")}
                if (rec["prev"] != self.head or rec["seq"] != self.count
                        or rec["hash"] != hashlib.sha256(
                            bytes.fromhex(self.head) + _canonical(body)).hexdigest()):
                    raise ValueError(f"CHAIN_BROKEN:{path.name}")
                self.head = rec["hash"]
                self.count += 1

    def append(self, body):
        body = {"seq": self.count, **body}
        digest = hashlib.sha256(bytes.fromhex(self.head)
                                + _canonical(body)).hexdigest()
        line = _canonical({**body, "prev": self.head, "hash": digest})
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(self.path, flags, 0o600), "ab") as f:
            f.write(line + b"\n")
            f.flush()
            os.fsync(f.fileno())
        self.head, self.count = digest, self.count + 1
        return body


class Collector:
    def __init__(self, data_dir, *, effective_t0_ms, implementation_sha,
                 get=http_get, run_observer=observer.run_isolated,
                 now_ms=lambda: int(time.time() * 1000), stamp=None,
                 log=print):
        self.dir = Path(data_dir)
        if not self.dir.is_dir() or self.dir.is_symlink():
            raise ValueError("DATA_DIR_MUST_EXIST")
        self.t0, self.impl = effective_t0_ms, implementation_sha
        self.get, self.run_observer, self.now_ms = get, run_observer, now_ms
        self.stamp = stamp if stamp is not None else submit_timestamp
        self.log = log
        self.manifest_path = self.dir / "manifest.jsonl"
        if not self.manifest_path.exists():
            manifest.create(self.manifest_path, effective_t0_ms=effective_t0_ms,
                            implementation_sha=implementation_sha,
                            recorded_ms=self.now_ms())
        checked = manifest.verify(self.manifest_path)
        if checked["status"] != "MANIFEST_SELF_CHAIN_VERIFIED_UNANCHORED":
            raise ValueError(f"MANIFEST_INVALID:{checked['reason']}")
        if checked["effective_t0_ms"] != effective_t0_ms:
            raise ValueError("EFFECTIVE_T0_MISMATCH")
        self.observations = Chain(self.dir / "observations.jsonl")
        self.outcomes = Chain(self.dir / "outcomes.jsonl")
        (self.dir / "raw").mkdir(exist_ok=True)
        (self.dir / "ots").mkdir(exist_ok=True)

    # ── capture ────────────────────────────────────────────────────
    def _fetch_symbol(self, symbol, t):
        bodies, raws = {}, []
        for key, interval, _, limit in FETCH:
            raw, parsed = self.get("/fapi/v1/klines", {
                "symbol": symbol, "interval": interval, "limit": limit + 1})
            bodies[key] = parsed
            raws.append(raw)
        raw_blob = b"\n".join(raws)
        observed = self.now_ms()
        snap = build_snapshot(symbol, t, observed, bodies)
        snap["raw_payload_sha256"] = hashlib.sha256(raw_blob).hexdigest()
        return snap, raw_blob

    def capture(self, t):
        changed = False
        for symbol in contract.UNIVERSE:
            try:
                snap, raw = self._fetch_symbol(symbol, t)
                result = self.run_observer(snap, implementation_sha=self.impl)
                status = result.get("status", "OBSERVER_FAILED")
            except (OSError, ValueError, KeyError, TypeError,
                    urllib.error.URLError) as exc:
                snap, raw, result = None, b"", {}
                status = f"SOURCE_FETCH_FAILED:{type(exc).__name__}"
            self.observations.append({
                "boundary_ms": t, "symbol": symbol, "status": status,
                "raw_payload_sha256": (snap or {}).get("raw_payload_sha256"),
                "observed_ms": (snap or {}).get("observed_ms"),
                "reason": result.get("reason"),
            })
            if status == "CANDIDATE_ENVELOPE_UNATTESTED":
                kind = manifest.append(self.manifest_path, result,
                                       recorded_ms=self.now_ms())
                self._keep_raw(f"{t}-{symbol}", raw)
                self.log(json.dumps({"event": "MANIFEST_APPEND", "kind": kind,
                                     "candidate_id": result["candidate_id"],
                                     "entry_close": result["entry_close"]}))
                changed = True
        return changed

    # ── outcomes ───────────────────────────────────────────────────
    def _recorded(self):
        done = set()
        if self.outcomes.path.exists():
            for line in self.outcomes.path.read_bytes().splitlines():
                rec = json.loads(line)
                done.add((rec["outcome"]["candidate_id"],
                          rec["outcome"]["horizon_minutes"]))
        return done

    def record_outcomes(self):
        now = self.now_ms()
        members = manifest.verify(self.manifest_path)["enrolled"]
        done = self._recorded()
        changed = False
        for env in members:
            for h in contract.HORIZONS:
                expected = env["captured_ms"] + h * 60_000
                key = (env["candidate_id"], h)
                if key in done or not expected <= now < expected + P15:
                    continue        # not due, or window missed: stays censored
                try:
                    raw, rows = self.get("/fapi/v1/klines", {
                        "symbol": env["symbol"], "interval": "15m",
                        "startTime": expected - P15, "limit": 1})
                    row = rows[0]
                    observed = self.now_ms()
                    if (row[0] != expected - P15 or row[6] != expected - 1
                            or not expected <= observed < expected + P15):
                        raise ValueError("EXIT_CANDLE_NOT_CLOSED_OR_LATE")
                except (OSError, ValueError, IndexError, TypeError, KeyError,
                        urllib.error.URLError) as exc:
                    self.log(json.dumps({"event": "OUTCOME_FETCH_FAILED",
                                         "candidate_id": key[0], "horizon": h,
                                         "error": type(exc).__name__}))
                    continue
                outcome = {
                    "candidate_id": env["candidate_id"], "symbol": env["symbol"],
                    "horizon_minutes": h, "close_ms": expected,
                    "observed_ms": observed, "complete_candle": True,
                    "no_interpolation": True,
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "exit_close": float(row[4]),
                }
                self.outcomes.append({"outcome": outcome})
                self._keep_raw(f"{expected}-{env['symbol']}-{h}m", raw)
                self.log(json.dumps({"event": "OUTCOME_APPEND", **outcome}))
                changed = True
        return changed

    # ── evidence ───────────────────────────────────────────────────
    def _keep_raw(self, name, raw):
        path = self.dir / "raw" / f"{name}.json.gz"
        if raw and not path.exists():
            path.write_bytes(gzip.compress(raw, mtime=0))

    def heads(self):
        m = manifest.verify(self.manifest_path)
        return {"manifest_head": m["head_sha256"],
                "outcomes_head": self.outcomes.head,
                "observations_head": self.observations.head,
                "enrolled": m["enrolled_count"],
                "excluded": m["excluded_count"],
                "outcomes": self.outcomes.count}

    def anchor(self, t):
        heads = self.heads()
        digest = hashlib.sha256(_canonical(heads)).digest()
        receipts = self.stamp(digest)
        for i, blob in enumerate(receipts):
            (self.dir / "ots" / f"{t}-{i}.ots").write_bytes(blob)
        self.log(json.dumps({"event": "ANCHOR", "boundary_ms": t,
                             "digest": digest.hex(), "calendars_ok": len(receipts),
                             **heads}))

    def evaluate(self):
        selection = contract.freeze_offline(
            list(manifest.verify(self.manifest_path)["enrolled"]),
            effective_cutoff_ms=self.t0, source_complete=True)
        outcome_map = {}
        if self.outcomes.path.exists():
            for line in self.outcomes.path.read_bytes().splitlines():
                o = json.loads(line)["outcome"]
                outcome_map[(o["candidate_id"], o["horizon_minutes"])] = o
        paired = contract.paired_market_observations(selection, outcome_map)
        return stats.evaluate(paired)

    def step(self, t):
        if t <= self.t0:
            # Nothing at or before effective_t0 is ever captured or enrolled.
            self.log(json.dumps({"event": "WAITING_FOR_T0", "boundary_ms": t,
                                 "effective_t0_ms": self.t0}))
            return False
        changed = self.capture(t)
        changed = self.record_outcomes() or changed
        self.anchor(t)
        self.log(json.dumps({"event": "EVALUATION", "boundary_ms": t,
                             **_summary(self.evaluate())}))
        return changed


def submit_timestamp(digest: bytes):
    """POST a 32-byte digest to public OpenTimestamps calendars."""
    receipts = []
    for url in OTS_CALENDARS:
        try:
            request = urllib.request.Request(
                f"{url}/digest", data=digest, method="POST",
                headers={"Accept": "application/vnd.opentimestamps.v1",
                         "User-Agent": "nexus7-research-666/1"})
            with urllib.request.urlopen(request, timeout=10) as r:  # noqa: S310
                receipts.append(r.read(64 * 1024))
        except (OSError, urllib.error.URLError):
            continue
    return receipts


def main():
    data_dir = os.environ["COLLECTOR_DATA_DIR"]
    t0 = int(os.environ["EFFECTIVE_T0_MS"])
    impl = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    if t0 % P15 or t0 < contract.ISSUE_CREATED_MS:
        raise SystemExit("EFFECTIVE_T0_MS invalid")
    collector = Collector(data_dir, effective_t0_ms=t0, implementation_sha=impl,
                          log=lambda line: print(line, flush=True))
    print(json.dumps({"event": "START", "effective_t0_ms": t0,
                      "implementation_sha": impl, **collector.heads()}),
          flush=True)
    while True:
        now = int(time.time() * 1000)
        t = (now - SETTLE_MS) // P15 * P15 + P15
        time.sleep(max(0.0, (t + SETTLE_MS - now) / 1000))
        collector.step(t)
        sys.stdout.flush()


def _summary(result):
    keep = {"status", "reason", "symbol_counts", "top_symbol_share",
            "corr_60m_240m", "missing_or_unverified", "paired_count"}
    out = {k: v for k, v in result.items() if k in keep}
    for h, report in (result.get("horizons") or {}).items():
        out[f"h{h}"] = {k: report[k] for k in (
            "n", "mean", "median", "positive_fraction",
            "bootstrap_lower_bound_95_one_sided", "gates", "passed")}
    return out


if __name__ == "__main__":
    main()
