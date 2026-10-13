#!/usr/bin/env python3
"""Manual no-key Binance USD-M aggTrade collector, research ONLY.

NOT wired to the NEXUS-7 bot, Railway, CI or its engine. This intentionally
requires an explicit local-only acknowledgment and an already mounted writable
directory. It is NOT an external immutable proof, durable storage guarantee,
price-depth proof, or permission to execute an order.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import time
import uuid

from bot.short_down_bos_event_archive_v1 import Archive, verify


def _args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True,
                        help="Exact uppercase USD-M symbol, e.g. BTCUSDT")
    parser.add_argument("--directory", required=True,
                        help="Existing local directory (NOT guaranteed durable)")
    parser.add_argument("--duration-seconds", type=int, required=True,
                        help="Capture duration (60..14460); stops on disconnect")
    parser.add_argument("--ack-local-not-durable", action="store_true",
                        help="Explicit opt-in: local files alone are not proof")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Z0-9]{2,20}USDT", args.symbol)
            or not 60 <= args.duration_seconds <= 14_460
            or not args.ack_local_not_durable
            or not Path(args.directory).is_dir()):
        parser.error("invalid symbol/duration/directory or missing local-only acknowledgment")
    return args


async def capture(args):
    # No imports/connection until manual CLI activation, separate from live.
    import websockets

    started = int(time.time_ns() // 1_000_000)
    session = "research-" + uuid.uuid4().hex
    writer = Archive(
        args.directory, symbol=args.symbol, session_id=session,
        generation=1, started_ms=started,
    )
    url = (
        "wss://fstream.binance.com/market/ws/"
        + args.symbol.lower() + "@aggTrade"
    )
    deadline = time.monotonic() + args.duration_seconds
    try:
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20, close_timeout=5,
            max_queue=2048, max_size=4096,
        ) as connection:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # A silent market-feed stalls fail closed; no reconnect or
                # artificial restitch of epochs, and no claim about no trades.
                received = await asyncio.wait_for(
                    connection.recv(), timeout=min(15.0, remaining)
                )
                payload = json.loads(received)
                if not isinstance(payload, dict):
                    raise ValueError("UNEXPECTED_WS_PAYLOAD")
                writer.append(payload, received_ms=time.time_ns() // 1_000_000)
        sealed = writer.seal()
    except Exception:
        writer.abort()
        raise
    verified = verify(args.directory, session_id=session)
    if verified["status"] != "SELF_CHAIN_VERIFIED_UNATTESTED":
        raise RuntimeError("LOCAL_ARCHIVE_VERIFICATION_FAILED")
    # No IDs, exchange keys, raw event payload or unbounded tape in stdout.
    print(json.dumps({
        "status": verified["status"],
        "source_attested": False,
        "live_allowed": False,
        "symbol": args.symbol,
        "session_id": session,
        "event_count": sealed["event_count"],
        "segments": len(sealed["segments"]),
    }, sort_keys=True))


def main(argv=None):
    args = _args(argv)
    asyncio.run(capture(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
