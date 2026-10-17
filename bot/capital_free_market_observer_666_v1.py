"""#666 capital-independent research observer; isolated subprocess, NO LIVE.

Reproduces the production SHA's *effective* ``Analyzer.analyze_mtf`` stack
(``runtime_bootstrap`` installs score/entry wrappers over the class method, so
calling ``bot.strategy`` directly would NOT be the strategy production runs)
on one public-candle snapshot at a completed UTC 15m boundary ``t``.

Isolation contract, enforced by ``run_isolated`` and the child entrypoint:
- separate interpreter, credential/DB/Telegram variables removed, EXCHANGE
  binance and PAPER_TRADE true, frozen scoring variables pinned;
- an audit hook blocks every non-loopback socket before any bot import;
- the analysis runs inside ``hard_gate_shadow_context.scope()``, which skips
  the score log and every shadow observer, and the decorator's NEXUS paths;
- production code must hash to the digest of SHA 45ebd59 or nothing runs.

No exchange client, engine instance, database, NEXUS call, order or account
balance is used. Output is an *unattested* envelope for
``capital_free_market_oos_666_v1.freeze_offline``; it never enrolls itself.
The live engine never imports this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

CONFIG_SOURCE_SHA = "45ebd59d4ca54072e1233ce0667085d92b70ab1d"
# sha256 over sorted "path\0sha256(bytes)\n" for bot/**/*.py, sitecustomize.py
# and requirements.txt in the CONFIG_SOURCE_SHA tree (343 files).
PRODUCTION_CODE_DIGEST = (
    "cc1b2dfd91ee8b7e19d7b46e3f5d317c9b6797eafad7eb8988db93a709af68d7"
)
RESEARCH_PREFIX = "bot/capital_free_market_"
ROOT = Path(__file__).resolve().parents[1]

# Code defaults at CONFIG_SOURCE_SHA. Whether Railway overrides any of these
# is NOT verified here; the prereg must confirm before effective_t0.
FROZEN_ENV = {
    "MIN_ENTRY_SCORE": "60",
    "FEE_MULTIPLIER": "2.0",
    "MIN_VOLUME_MULT": "0.5",
    "MIN_RR_RATIO": "2.0",
    "TAKER_FEE": "0.0006",
}
FORCED_ENV = {
    "EXCHANGE": "binance",
    "PAPER_TRADE": "true",
    "NEXUS_TELEGRAM": "false",
    "BGX_RUNTIME_TRUTH_ENABLED": "false",
    "PYTHONHASHSEED": "0",
}
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASS", "DATABASE", "DSN",
                   "PG", "TELEGRAM", "WEBHOOK", "COOKIE", "AUTH")
_PERIOD_MS = {"k15": 900_000, "k1h": 3_600_000, "k4h": 14_400_000}
# Same windows the production HARD_GATE_SHADOW scan reads (limit, minimum).
_WINDOW = {"k15": (200, 60), "k1h": (100, 40), "k4h": (120, 20)}
SOURCE_ALLOWED = ("BINANCE_USDM_PUBLIC_KLINES",)
AUTHORITY = {
    "population": "MARKET_ONLY", "market_only": True, "research_only": True,
    "nexus_called": False, "nexus_allowed": False, "live_eligible": False,
    "canonical_nexus_approval": False, "eligible_for_609_enrollment": False,
    "source_attested": False, "cohort_membership_proven": False,
    "promotion_allowed": False, "live_allowed": False,
    "decision_effect": "NONE", "execution_effect": "NONE",
}


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def production_code_digest(root: Path = ROOT) -> str:
    names = sorted(
        [p.relative_to(root).as_posix() for p in (root / "bot").rglob("*.py")
         if "__pycache__" not in p.parts]
        + ["sitecustomize.py", "requirements.txt"]
    )
    h = hashlib.sha256()
    for name in names:
        if name.startswith(RESEARCH_PREFIX):
            continue
        data = (root / name).read_bytes()
        h.update(name.encode() + b"\0"
                 + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return h.hexdigest()


def _price(value):
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def _volume(value):
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    return v if math.isfinite(v) and v >= 0 else None


def validate_snapshot(snapshot):
    """Return (reason, prepared). ``prepared`` is None unless fully valid.

    Closed candles only, contiguous, ending exactly at the last period that
    closed at or before ``t``; anything at or after ``t`` is lookahead.
    """
    if not isinstance(snapshot, dict):
        return "INVALID_SNAPSHOT", None
    t, observed = snapshot.get("boundary_ms"), snapshot.get("observed_ms")
    symbol = snapshot.get("symbol")
    if (type(t) is not int or t <= 0 or t % _PERIOD_MS["k15"]
            or type(observed) is not int
            or not t <= observed < t + _PERIOD_MS["k15"]):
        return "BOUNDARY_OR_OBSERVATION_TIME_INVALID", None
    if not isinstance(symbol, str) or not symbol.endswith("USDT"):
        return "INVALID_SYMBOL", None
    if snapshot.get("source") not in SOURCE_ALLOWED:
        return "SOURCE_NOT_ALLOWED", None
    raw_hash = snapshot.get("raw_payload_sha256")
    if (not isinstance(raw_hash, str) or len(raw_hash) != 64
            or any(c not in "0123456789abcdef" for c in raw_hash)):
        return "RAW_PAYLOAD_HASH_MISSING", None
    prepared = {}
    for key, period in _PERIOD_MS.items():
        rows = snapshot.get(key)
        limit, minimum = _WINDOW[key]
        if not isinstance(rows, list) or not minimum <= len(rows) <= limit:
            return "LOOKBACK_INCOMPLETE", None
        last_open = (t // period) * period - period
        out = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {
                    "ts", "o", "h", "l", "c", "v", "close_ms"}:
                return "MALFORMED_CANDLE", None
            ts, close_ms = row["ts"], row["close_ms"]
            o, h, lo, c = (_price(row[k]) for k in ("o", "h", "l", "c"))
            v = _volume(row["v"])
            if (type(ts) is not int or type(close_ms) is not int
                    or ts % period or close_ms != ts + period - 1
                    or None in (o, h, lo, c, v)
                    or h < max(o, c, lo) or lo > min(o, c, h)):
                return "MALFORMED_CANDLE", None
            if close_ms >= t:
                return "LOOKAHEAD_CANDLE", None
            out.append({"ts": ts, "o": o, "h": h, "l": lo, "c": c, "v": v})
        for i, row in enumerate(out):
            if row["ts"] != last_open - (len(out) - 1 - i) * period:
                return "GAP_OR_DISORDER", None
        # The strategy drops the final element as the still-forming bar.
        # Append a zero-elapsed placeholder at the period containing t: it
        # carries no information after t and is discarded by that rule
        # (every window here exceeds the 20/15/10 drop thresholds).
        current = (t // period) * period
        last_close = out[-1]["c"]
        out.append({"ts": current, "o": last_close, "h": last_close,
                    "l": last_close, "c": last_close, "v": 0.0,
                    "_placeholder": True})
        prepared[key] = out
    return "OK", {"symbol": symbol, "t": t, "observed_ms": observed,
                  "entry_close": prepared["k15"][-2]["c"], **prepared}


def envelope(signal_view, prepared, *, snapshot_sha256, implementation_sha):
    """Map one analyzer result to a #666 candidate envelope or a typed skip."""
    if signal_view is None:
        return {**AUTHORITY, "status": "NO_SIGNAL"}
    if (signal_view.get("direction") != "SHORT"
            or signal_view.get("regime") != "TRENDING_DOWN"
            or signal_view.get("entry_type") != "BOS_BREAK"):
        return {**AUTHORITY, "status": "OUT_OF_SCOPE_SIGNAL",
                "direction": signal_view.get("direction"),
                "regime": signal_view.get("regime"),
                "entry_type": signal_view.get("entry_type")}
    t, symbol = prepared["t"], prepared["symbol"]
    return {
        **AUTHORITY,
        "status": "CANDIDATE_ENVELOPE_UNATTESTED",
        "candidate_id": f"MARKET_ONLY:{symbol}:SHORT:BOS_BREAK:{t}",
        "symbol": symbol, "captured_ms": t,
        "observed_ms": prepared["observed_ms"],
        "side": "SHORT", "regime": "TRENDING_DOWN", "setup": "BOS_BREAK",
        "entry_close": prepared["entry_close"],
        "config_sha": CONFIG_SOURCE_SHA,
        "snapshot_complete": True, "no_future_inputs": True,
        "source_sha256": snapshot_sha256,
        "implementation_sha": implementation_sha,
        "frozen_env": dict(FROZEN_ENV),
        "analyzer_score": signal_view.get("score"),
    }


def _child_env(base=None):
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if not any(m in k.upper() for m in _SECRET_MARKERS)
           and k not in FROZEN_ENV and not k.startswith("NEXUS_")}
    env.update(FROZEN_ENV)
    env.update(FORCED_ENV)
    return env


def run_isolated(snapshot: dict, *, implementation_sha: str, timeout=120):
    """Run one observation in a fresh, network-blocked interpreter."""
    if (not isinstance(implementation_sha, str) or len(implementation_sha) != 40
            or any(c not in "0123456789abcdef" for c in implementation_sha)):
        return {**AUTHORITY, "status": "IMPLEMENTATION_SHA_INVALID"}
    payload = _canonical({"snapshot": snapshot,
                          "implementation_sha": implementation_sha})
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-E", "-s", "-m", __name__, "--child"],
        input=payload, capture_output=True, cwd=str(ROOT),
        env=_child_env(), timeout=timeout, check=False,
    )
    lines = [x for x in proc.stdout.splitlines() if x.startswith(b"{")]
    if proc.returncode != 0 or not lines:
        return {**AUTHORITY, "status": "OBSERVER_FAILED",
                "returncode": proc.returncode}
    return json.loads(lines[-1])


def _install_network_block():
    import ipaddress

    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            host = address[0] if isinstance(address, tuple) else None
        elif event == "socket.getaddrinfo":
            host = args[0]
        else:
            return
        try:
            if host and ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise PermissionError("research observer: network blocked")
    sys.addaudithook(audit)


def _child_main():
    _install_network_block()
    request = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024))
    snapshot, impl = request["snapshot"], request["implementation_sha"]
    out = sys.stdout
    sys.stdout = sys.stderr  # keep production log lines off the JSON channel
    try:
        if any(os.environ.get(k) != v for k, v in FROZEN_ENV.items()):
            result = {**AUTHORITY, "status": "FROZEN_ENV_MISMATCH"}
        elif production_code_digest() != PRODUCTION_CODE_DIGEST:
            result = {**AUTHORITY, "status": "PRODUCTION_CODE_DIGEST_MISMATCH"}
        else:
            reason, prepared = validate_snapshot(snapshot)
            if prepared is None:
                result = {**AUTHORITY, "status": "SOURCE_NOT_PROVEN",
                          "reason": reason}
            else:
                result = _analyze(prepared, snapshot, impl)
    finally:
        sys.stdout = out
    out.write(_canonical(result).decode() + "\n")
    out.flush()


def _analyze(prepared, snapshot, impl):
    from bot.runtime_bootstrap import install
    install()
    from bot.config import cfg
    from bot.hard_gate_shadow_context import scope
    from bot.strategy import Analyzer

    klines = []
    for key in ("k15", "k1h", "k4h"):
        klines.append([{k: v for k, v in row.items() if k != "_placeholder"}
                       for row in prepared[key]])
    with scope() as context:
        sig = Analyzer().analyze_mtf(
            prepared["symbol"], *klines, min_score=cfg.MIN_ENTRY_SCORE,
            fee_mult=cfg.FEE_MULTIPLIER, vol_mult=cfg.MIN_VOLUME_MULT,
        )
        sig = sig if sig is not None else context.signal
    view = None if sig is None else {
        "direction": str(getattr(sig, "direction", "")),
        "regime": str(getattr(sig, "regime", "")),
        "entry_type": str(getattr(sig, "entry_type", "")),
        "score": int(getattr(sig, "score", 0)),
    }
    return envelope(view, prepared,
                    snapshot_sha256=hashlib.sha256(_canonical(snapshot)).hexdigest(),
                    implementation_sha=impl)


if __name__ == "__main__" and sys.argv[1:] == ["--child"]:
    _child_main()
