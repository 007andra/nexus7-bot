"""V3.1 explicit, one-shot research-key capability probe (NO LIVE).

Default mode performs NO network or exchange access. Real --probe requires a
separate human command and an isolated environment containing ONLY dedicated
research credentials. Never prints API keys, signatures, response bodies,
candidate IDs, fee amounts or account data.

This checks whether a read-only key can fetch Binance USD-M fee/bracket
metadata, in addition to public market data. It does NOT authorize candidate
enrollment, immutable manifest writes, a scheduler or LIVE operations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re

from bot.short_down_bos_v31_evidence_collector import (
    FAPI, CollectorRefused, ReadOnlyBinanceResearchClient,
)
from bot.short_down_bos_v31_https_transport import ReadOnlyResearchHTTPS

RESEARCH_KEY = "V31_RESEARCH_BINANCE_API_KEY"
RESEARCH_SECRET = "V31_RESEARCH_BINANCE_API_SECRET"
DISALLOWED_RUNTIME_VARS = (
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
    "BINANCE_ED25519_PRIVATE_KEY", "BINANCE_ED25519_PRIVATE_KEY_B64",
    "DATABASE_URL", "DB_AUTHORITY_ID", "EXECUTION_CAPABILITY",
    "LIVE_TRADING_CONFIRMED", "CONTROLLED_LIVE_REENTRY_ARM",
)
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,18}USDT$")


def isolation_preflight(environ: dict, symbol: str) -> None:
    """Require an isolated process; no production trading credential aliases."""
    if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        raise CollectorRefused("RESEARCH_SYMBOL_INVALID")
    if any(name in environ for name in DISALLOWED_RUNTIME_VARS):
        raise CollectorRefused("LIVE_RUNTIME_ENVIRONMENT_PRESENT")
    if not environ.get(RESEARCH_KEY) or not environ.get(RESEARCH_SECRET):
        raise CollectorRefused("RESEARCH_CREDENTIALS_MISSING")


async def probe_capabilities(transport, environ: dict, symbol: str) -> dict:
    """Bounded authorized GET-only capability test; returns booleans only."""
    isolation_preflight(environ, symbol)
    client = ReadOnlyBinanceResearchClient(transport, environ=environ)
    await client.preflight()
    # Capability check only, not market execution or as-of proof.
    server = await client.get(FAPI, "/fapi/v1/time")
    if not isinstance(server, dict) or type(server.get("serverTime")) is not int:
        raise CollectorRefused("RESEARCH_SERVER_CLOCK_UNVERIFIED")
    now_ms = server["serverTime"]
    if now_ms <= 0:
        raise CollectorRefused("RESEARCH_SERVER_CLOCK_UNVERIFIED")

    info = await client.get(FAPI, "/fapi/v1/exchangeInfo")
    if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
        raise CollectorRefused("RESEARCH_EXCHANGE_INFO_UNVERIFIED")
    from bot.short_down_bos_v31_evidence_collector import (
        normalize_filters, normalize_quote, normalize_fee, maint_margin_rate,
    )
    normalize_filters(info, symbol, now_ms)
    quote = await client.get(FAPI, "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
    normalize_quote(quote, symbol)
    fee = await client.get(
        FAPI, "/fapi/v1/commissionRate", {"symbol": symbol}, now_ms=now_ms
    )
    normalize_fee(fee, symbol, now_ms)
    brackets = await client.get(
        FAPI, "/fapi/v1/leverageBracket", {"symbol": symbol}, now_ms=now_ms
    )
    from decimal import Decimal
    maint_margin_rate(brackets, symbol, Decimal("1"))
    marks = await client.get(
        FAPI, "/fapi/v1/markPriceKlines", {"symbol": symbol, "interval": "15m", "limit": 2}
    )
    if not isinstance(marks, list) or not marks or not isinstance(marks[0], list):
        raise CollectorRefused("RESEARCH_MARK_BARS_UNVERIFIED")
    funding = await client.get(
        FAPI, "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1}
    )
    if not isinstance(funding, list):
        raise CollectorRefused("RESEARCH_FUNDING_ENDPOINT_UNVERIFIED")
    # No content, ID, price, quantity, fee or sensitive identity is returned.
    return {
        "status": "READ_ONLY_API_CAPABILITIES_PASS",
        "read_only_permissions_verified": True,
        "research_key_used": True,
        "live_env_present": False,
        "all_required_get_endpoints_reachable": True,
        "research_only": True,
        "candidate_enrollment_started": False,
        "manifest_durable": False,
        "live_allowed": False,
        "execution_effect": "NONE",
    }


def dry_run() -> dict:
    """Safe even when invoked in production: does not inspect the environment."""
    return {
        "status": "DRY_RUN_ONLY",
        "network_calls": 0,
        "credentials_loaded": False,
        "isolated_credentials_required": [RESEARCH_KEY, RESEARCH_SECRET],
        "live_runtime_variables_forbidden": list(DISALLOWED_RUNTIME_VARS),
        "candidate_enrollment_started": False,
        "live_allowed": False,
    }


async def _real_probe(symbol: str):
    # Import here: default dry-run must not create network resources.
    import aiohttp
    async with aiohttp.ClientSession(trust_env=False) as session:
        return await probe_capabilities(
            ReadOnlyResearchHTTPS(session), dict(os.environ), symbol
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Binance USD-M V3.1 read-only key preflight; DRY RUN by default"
    )
    parser.add_argument("--probe", action="store_true", help="perform bounded GET calls")
    parser.add_argument("--i-confirm-readonly-network-only", action="store_true")
    parser.add_argument("--symbol", default="BTCUSDT")
    args = parser.parse_args(argv)
    if not args.probe:
        print(json.dumps(dry_run(), sort_keys=True))
        return 0
    if not args.i_confirm_readonly_network_only:
        print(json.dumps({"status": "REFUSED", "reason": "EXPLICIT_READONLY_PROBE_APPROVAL_REQUIRED"}))
        return 2
    try:
        report = asyncio.run(_real_probe(args.symbol))
    except CollectorRefused as exc:
        # Only our bounded constant reason codes are returned. Never expose
        # raw exceptions, response data or request URLs.
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"status": "REFUSED", "reason": "RESEARCH_PREFLIGHT_FAILED"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
