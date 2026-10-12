"""One-shot, redacted Binance USD-M internal transfer destination probe (#653).

Explicit command only: no scheduler, trading hook, database access or side effects.
Four allowlisted signed USER_DATA GET queries against api.binance.com. No POST.
This cross-wallet lookup is not independent proof of the receiving wallet balance
or of who initiated a transfer. Never log account/transaction IDs or raw rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import aiohttp

from bot.binance_accounting_evidence import collect_income

SAPI_URL = "https://api.binance.com/sapi/v1/asset/transfer"
ROUTES = {
    "UMFUTURE_MAIN": "SPOT",
    "UMFUTURE_FUNDING": "FUNDING",
    "UMFUTURE_MARGIN": "CROSS_MARGIN",
    "UMFUTURE_OPTION": "OPTIONS",
}
MAX_WINDOW_MS = 20 * 60 * 1000
MATCH_TOLERANCE_MS = 2 * 60 * 1000
MAX_ROWS = 100


def _window(start_ms: int, end_ms: int) -> tuple[int, int]:
    start_ms, end_ms = int(start_ms), int(end_ms)
    if start_ms <= 0 or end_ms <= start_ms or end_ms - start_ms > MAX_WINDOW_MS:
        raise ValueError("transfer probe requires a positive <=20 minute window")
    return start_ms, end_ms


def _amount(value) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid transfer amount") from exc
    if not parsed.is_finite():
        raise ValueError("nonfinite transfer amount")
    return parsed


async def read_route(client, transfer_type: str, start_ms: int, end_ms: int) -> list[dict]:
    """Authorized GET-only SAPI transfer lookup; never returns error response text."""
    if transfer_type not in ROUTES:
        raise ValueError("transfer route not allowlisted")
    start_ms, end_ms = _window(start_ms, end_ms)
    # Import here so the signing credentials are not duplicated in this module.
    from bot import binance
    if not binance.API_KEY:
        raise RuntimeError("BINANCE_SAPI_READ_KEY_UNAVAILABLE")
    await client._ensure_session()
    params = client._signed_params({
        "type": transfer_type, "startTime": start_ms, "endTime": end_ms,
        "current": 1, "size": MAX_ROWS,
    })
    await client._throttle()
    try:
        async with client._rate_sem:
            async with client._session.request(
                "GET", SAPI_URL, params=params,
                headers={"X-MBX-APIKEY": binance.API_KEY},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    raise RuntimeError("BINANCE_SAPI_READ_REJECTED")
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise RuntimeError("BINANCE_SAPI_HISTORY_UNAVAILABLE") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise RuntimeError("BINANCE_SAPI_HISTORY_INCOMPLETE")
    rows = payload["rows"]
    try:
        total = int(payload["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("BINANCE_SAPI_HISTORY_INCOMPLETE") from None
    if total < 0 or total != len(rows) or len(rows) >= MAX_ROWS:
        # A truncated page can never establish the absence of another match.
        raise RuntimeError("BINANCE_SAPI_HISTORY_PAGINATION_UNCONFIRMED")
    result = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise RuntimeError("BINANCE_SAPI_HISTORY_UNREADABLE")
        try:
            asset = str(raw["asset"]).upper()
            amount = _amount(raw["amount"])
            event_ms = int(raw["timestamp"])
            kind = str(raw["type"])
            status = str(raw["status"])
            identity = str(raw["tranId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("BINANCE_SAPI_HISTORY_UNREADABLE") from None
        if kind != transfer_type or event_ms < start_ms or event_ms > end_ms or not identity:
            raise RuntimeError("BINANCE_SAPI_HISTORY_UNREADABLE")
        # Keep identifiers only in volatile in-process memory, never in the final report.
        result.append({
            "asset": asset, "amount": amount, "timestamp": event_ms,
            "type": kind, "status": status, "tranId": identity,
        })
    return result


def compare_income_and_routes(income_rows: list[dict], by_route: dict[str, list[dict]]) -> dict:
    """Corroborate unique USD-M transfer direction with fixed-route SAPI data."""
    relevant = []
    for raw in income_rows:
        if raw.get("incomeType") != "TRANSFER" or raw.get("asset") != "USDT":
            continue
        amount = _amount(raw.get("income"))
        if amount >= 0:
            continue
        t = int(raw["time"])
        if t <= 0 or not raw.get("tranId"):
            return {"status": "INCONCLUSIVE_INCOME_IDENTITY", "destination": "UNVERIFIED"}
        relevant.append({"amount": -amount, "t": t, "id": str(raw["tranId"])})
    if len(relevant) != 1:
        return {"status": "INCONCLUSIVE_INCOME_CARDINALITY", "destination": "UNVERIFIED"}
    if set(by_route) != set(ROUTES):
        return {"status": "INCOMPLETE_ROUTE_COVERAGE", "destination": "UNVERIFIED"}

    observed = relevant[0]
    matches = []
    for kind, rows in by_route.items():
        for raw in rows:
            if (raw["status"] != "CONFIRMED" or raw["asset"] != "USDT"
                    or raw["amount"] != observed["amount"]
                    or abs(raw["timestamp"] - observed["t"]) > MATCH_TOLERANCE_MS):
                continue
            matches.append((kind, raw["tranId"] == observed["id"]))
    if len(matches) != 1:
        return {
            "status": "UNMATCHED" if not matches else "AMBIGUOUS_MULTIPLE_MATCHES",
            "destination": "UNVERIFIED",
        }
    kind, id_matches = matches[0]
    return {
        "status": "EXACT_ID_AND_AMOUNT_TIME" if id_matches else "AMOUNT_TIME_ONLY",
        "destination": ROUTES[kind] if id_matches else "UNVERIFIED",
        "candidate_route": kind,
        "destination_credit_confirmed": False,
        "transfer_initiator_verified": False,
        "authority": False,
        "decision_effect": "NONE",
        "execution_effect": "NONE",
    }


async def probe(client, start_ms: int, end_ms: int, reader=None) -> dict:
    start_ms, end_ms = _window(start_ms, end_ms)
    # All five calls are authenticated GETs; not called automatically by the bot.
    try:
        income = await collect_income(client, start_ms, end_ms)
    except Exception:
        # Exchange failures may include response bodies in exception messages.
        # Preserve neither the message nor a misleading destination inference.
        return {"status": "INCOMPLETE_INCOME_EVIDENCE", "destination": "UNVERIFIED",
                "authority": False, "decision_effect": "NONE",
                "execution_effect": "NONE"}
    history_reader = reader or read_route
    by_route = {}
    for kind in ROUTES:
        try:
            by_route[kind] = await history_reader(client, kind, start_ms, end_ms)
        except Exception:
            # Any unavailable route invalidates absence/uniqueness claims.
            return {"status": "INCOMPLETE_ROUTE_COVERAGE", "destination": "UNVERIFIED",
                    "authority": False, "decision_effect": "NONE",
                    "execution_effect": "NONE"}
    return compare_income_and_routes(income, by_route)


def _parse_utc(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("explicit UTC timestamp required")
    return int(stamp.timestamp() * 1000)


async def _main(args):
    client = None
    try:
        # Include parsing and client initialization within the redacted boundary.
        start_ms = _parse_utc(args.start_utc)
        end_ms = _parse_utc(args.end_utc)
        _window(start_ms, end_ms)
        from bot.binance import BinanceClient
        client = BinanceClient()
        result = await probe(client, start_ms, end_ms)
        # No transaction IDs, credentials or raw API errors in the report.
        print(json.dumps(result, sort_keys=True))
        return 2 if result["status"] in {
            "INCOMPLETE_INCOME_EVIDENCE", "INCOMPLETE_ROUTE_COVERAGE"
        } else 0
    except Exception:
        # A traceback can expose signed HTTP response details. Fail closed with
        # a constant, non-sensitive envelope; an absent result is never proof.
        print(json.dumps({
            "status": "PROBE_FAILED_CLOSED", "destination": "UNVERIFIED",
            "authority": False, "decision_effect": "NONE",
            "execution_effect": "NONE",
        }, sort_keys=True))
        return 2
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                # Close failures must not print raw driver or HTTP messages.
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-time, Binance signed GET-only transfer probe")
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
