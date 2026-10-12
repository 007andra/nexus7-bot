"""V3.1 prospective evidence collector: READ-ONLY Binance, research credentials only.

Builds exactly the evidence object ``short_down_bos_v31_net_evaluator`` consumes,
from exchange responses captured at the moment they describe:

* at entry:   server time, commissionRate (taker), exchangeInfo filters,
              leverageBracket maintenance rate, bookTicker (bid/ask + exchange time)
* at 60/240m: bookTicker at the horizon
* after 240m: closed mark-price (trigger series) bars and funding events with
              a coverage proof of the exposure window

Isolation (fail closed, checked before any market read):
* only GET, only the endpoints in ``ALLOWED_GET``; anything else raises;
* credentials come only from ``V31_RESEARCH_BINANCE_API_KEY/SECRET``; a key equal
  to the LIVE ``BINANCE_API_KEY`` is refused;
* ``apiRestrictions`` must prove the research key can read and can NOT trade
  futures/spot, withdraw or transfer;
* no database, no order client, no LIVE module import. Persistence is the
  separately reviewed research PostgreSQL adapter's job.

Starting a collection requires the human-approved capture start of #658; this
module has no scheduler and never decides enrollment.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import ROUND_FLOOR, Decimal
from urllib.parse import urlencode

FAPI = "fapi.binance.com"
SAPI = "api.binance.com"
ALLOWED_GET = {
    (FAPI, "/fapi/v1/time"): False,           # value: signed?
    (FAPI, "/fapi/v1/exchangeInfo"): False,
    (FAPI, "/fapi/v1/ticker/bookTicker"): False,
    (FAPI, "/fapi/v1/markPriceKlines"): False,
    (FAPI, "/fapi/v1/fundingRate"): False,
    (FAPI, "/fapi/v1/commissionRate"): True,
    (FAPI, "/fapi/v1/leverageBracket"): True,
    (SAPI, "/sapi/v1/account/apiRestrictions"): True,
}
RESEARCH_KEY_ENV = "V31_RESEARCH_BINANCE_API_KEY"
RESEARCH_SECRET_ENV = "V31_RESEARCH_BINANCE_API_SECRET"
LIVE_KEY_ENV = "BINANCE_API_KEY"
FORBIDDEN_PERMISSIONS = (
    "enableFutures", "enableSpotAndMarginTrading", "enableMargin",
    "enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer",
    "enableVanillaOptions", "enablePortfolioMarginTrading",
    "enableFixApiTrade",
)
BAR_MS = 15 * 60_000
FUNDING_PAGE_LIMIT = 1000


class CollectorRefused(RuntimeError):
    """Constant reason codes only; never keys, IDs or raw payloads."""


class ReadOnlyBinanceResearchClient:
    """GET-only, allowlisted access. ``transport(host, path, query) -> json``."""

    def __init__(self, transport, *, environ=None):
        env = os.environ if environ is None else environ
        key = env.get(RESEARCH_KEY_ENV, "")
        secret = env.get(RESEARCH_SECRET_ENV, "")
        if not key or not secret:
            raise CollectorRefused("RESEARCH_CREDENTIALS_MISSING")
        live = env.get(LIVE_KEY_ENV, "")
        if live and hmac.compare_digest(live, key):
            raise CollectorRefused("LIVE_CREDENTIAL_REUSE")
        self._key = key
        self._secret = secret.encode()
        self._transport = transport
        self.verified_read_only = False

    async def get(self, host: str, path: str, params: dict | None = None, *, now_ms=None):
        if (host, path) not in ALLOWED_GET:
            raise CollectorRefused("ENDPOINT_NOT_ALLOWLISTED")
        if path != "/sapi/v1/account/apiRestrictions" and not self.verified_read_only:
            raise CollectorRefused("READ_ONLY_PREFLIGHT_REQUIRED")
        query = dict(params or {})
        headers = {}
        if ALLOWED_GET[(host, path)]:
            query["timestamp"] = int(now_ms if now_ms is not None else time.time() * 1000)
            query["recvWindow"] = 5000
            payload = urlencode(query)
            query["signature"] = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self._key
        return await self._transport(host, path, query, headers)

    async def preflight(self) -> None:
        restrictions = await self.get(SAPI, "/sapi/v1/account/apiRestrictions")
        if not isinstance(restrictions, dict) or restrictions.get("enableReading") is not True:
            raise CollectorRefused("RESEARCH_KEY_CANNOT_READ")
        for perm in FORBIDDEN_PERMISSIONS:
            if restrictions.get(perm, False) is not False:
                raise CollectorRefused("RESEARCH_KEY_HAS_WRITE_PERMISSION")
        self.verified_read_only = True


# ─── pure normalizers (exchange JSON -> evaluator evidence) ─────────────────

def _s(value) -> str:
    out = str(value)
    Decimal(out)  # raises on garbage; floats never reach here from JSON strings
    return out


def normalize_quote(book: dict, symbol: str) -> dict:
    if not isinstance(book, dict) or book.get("symbol") != symbol:
        raise CollectorRefused("BOOK_TICKER_INVALID")
    t = book.get("time")
    if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
        raise CollectorRefused("BOOK_TICKER_WITHOUT_EXCHANGE_TIME")
    return {"observed_ms": t, "bid": _s(book["bidPrice"]), "ask": _s(book["askPrice"])}


def normalize_filters(exchange_info: dict, symbol: str, observed_ms: int) -> dict:
    sym = next((s for s in exchange_info.get("symbols", []) if s.get("symbol") == symbol), None)
    if sym is None or sym.get("status") != "TRADING":
        raise CollectorRefused("SYMBOL_NOT_TRADING")
    f = {x.get("filterType"): x for x in sym.get("filters", [])}
    # MARKET orders are bound by MARKET_LOT_SIZE, not LOT_SIZE.
    lot = f.get("MARKET_LOT_SIZE") or {}
    try:
        return {
            "observed_ms": observed_ms,
            "tick_size": _s(f["PRICE_FILTER"]["tickSize"]),
            "step_size": _s(lot["stepSize"]),
            "min_notional": _s(f["MIN_NOTIONAL"]["notional"]),
        }
    except KeyError:
        raise CollectorRefused("EXCHANGE_FILTERS_INCOMPLETE") from None


def normalize_fee(commission: dict, symbol: str, observed_ms: int) -> dict:
    if not isinstance(commission, dict) or commission.get("symbol") != symbol:
        raise CollectorRefused("COMMISSION_RATE_INVALID")
    return {"source": "BINANCE_USDM_COMMISSION_RATE", "observed_ms": observed_ms,
            "taker_rate": _s(commission["takerCommissionRate"])}


def maint_margin_rate(brackets, symbol: str, notional: Decimal) -> str:
    rows = brackets if isinstance(brackets, list) else [brackets]
    entry = next((b for b in rows if isinstance(b, dict) and b.get("symbol") == symbol), None)
    if entry is None:
        raise CollectorRefused("LEVERAGE_BRACKET_MISSING")
    for b in entry.get("brackets", []):
        if Decimal(_s(b["notionalFloor"])) <= notional < Decimal(_s(b["notionalCap"])):
            return _s(b["maintMarginRatio"])
    raise CollectorRefused("LEVERAGE_BRACKET_MISSING")


def normalize_mark_bars(klines: list, start_ms: int, end_ms: int) -> list[dict]:
    """[openTime, o, h, l, c, _, closeTime, ...] -> contiguous half-open bars."""
    bars = []
    for k in klines:
        open_ms, close_ms = int(k[0]), int(k[6]) + 1
        if close_ms - open_ms != BAR_MS:
            raise CollectorRefused("MARK_KLINE_INTERVAL_INVALID")
        if close_ms <= start_ms or open_ms >= end_ms:
            continue
        bars.append({"open_ms": open_ms, "close_ms": close_ms,
                     "o": _s(k[1]), "h": _s(k[2]), "l": _s(k[3]), "c": _s(k[4])})
    return bars


def normalize_funding(events: list, symbol: str, start_ms: int, end_ms: int) -> dict:
    if not isinstance(events, list) or len(events) >= FUNDING_PAGE_LIMIT:
        # a full page could hide more events: no coverage proof
        raise CollectorRefused("FUNDING_PAGE_NOT_COMPLETE")
    out = []
    for e in events:
        if e.get("symbol") != symbol:
            raise CollectorRefused("FUNDING_SYMBOL_MISMATCH")
        t = int(e["fundingTime"])
        if start_ms < t <= end_ms:
            # rateType absent -> left absent: the evaluator refuses ambiguity
            out.append({"funding_ms": t, "rate": _s(e["fundingRate"]),
                        "mark_price": _s(e["markPrice"]), "rate_type": e.get("rateType")})
    return {"coverage": {"complete": True, "start_ms": start_ms, "end_ms": end_ms},
            "events": out}


def research_quantity(reference_notional: str, bid: str, step: str) -> str:
    """Frozen per-candidate research notional, floored to the market step."""
    qty = Decimal(reference_notional) / Decimal(bid)
    step_d = Decimal(step)
    return str((qty / step_d).to_integral_value(rounding=ROUND_FLOOR) * step_d)


def isolated_margin(quantity: str, bid: str, leverage: int, slippage: str) -> str:
    """Initial isolated margin bound that never exceeds qty*entry_fill/leverage."""
    worst_entry = Decimal(bid) * (1 - 2 * Decimal(slippage))
    return str(Decimal(quantity) * worst_entry / leverage)


# ─── capture steps (each call is made at the moment it describes) ───────────

async def capture_entry(client, candidate: dict, *, reference_notional: str, leverage: int,
                        slippage_fraction: str, protection: dict) -> dict:
    symbol = candidate["symbol"]
    server = await client.get(FAPI, "/fapi/v1/time")
    now = int(server["serverTime"])
    book = normalize_quote(
        await client.get(FAPI, "/fapi/v1/ticker/bookTicker", {"symbol": symbol}), symbol)
    filters = normalize_filters(await client.get(FAPI, "/fapi/v1/exchangeInfo"), symbol, now)
    fee = normalize_fee(
        await client.get(FAPI, "/fapi/v1/commissionRate", {"symbol": symbol}, now_ms=now),
        symbol, now)
    qty = research_quantity(reference_notional, book["bid"], filters["step_size"])
    mmr = maint_margin_rate(
        await client.get(FAPI, "/fapi/v1/leverageBracket", {"symbol": symbol}, now_ms=now),
        symbol, Decimal(qty) * Decimal(book["bid"]))
    return {
        "candidate_id": candidate["candidate_id"], "symbol": symbol, "synthetic": False,
        "entry_ms": book["observed_ms"], "entry_quote": book, "fee": fee, "filters": filters,
        "slippage": {"model": "DECLARED_STRESS_FLOOR", "fraction": slippage_fraction},
        "quantity": qty, "protection": dict(protection),
        "bars_price_type": "MARK_PRICE",  # what capture_path actually collects
        "margin": {"mode": "ISOLATED", "leverage": leverage, "maint_margin_rate": mmr,
                   "margin_balance": isolated_margin(qty, book["bid"], leverage, slippage_fraction)},
        "exit_quotes": {},
    }


async def capture_horizon(client, evidence: dict, horizon_min: int) -> dict:
    symbol = evidence["symbol"]
    quote = normalize_quote(
        await client.get(FAPI, "/fapi/v1/ticker/bookTicker", {"symbol": symbol}), symbol)
    target = evidence["entry_ms"] + horizon_min * 60_000
    if quote["observed_ms"] > target:
        # captured too late: never substitute; the evaluator marks it stale/future
        quote = {**quote, "late_capture": True}
    evidence["exit_quotes"][str(horizon_min)] = quote
    return evidence


async def capture_path(client, evidence: dict) -> dict:
    symbol, entry = evidence["symbol"], evidence["entry_ms"]
    end = entry + 240 * 60_000
    first_open = entry - entry % BAR_MS
    klines = await client.get(FAPI, "/fapi/v1/markPriceKlines", {
        "symbol": symbol, "interval": "15m", "startTime": first_open,
        "endTime": end + BAR_MS, "limit": 40})
    evidence["bars"] = normalize_mark_bars(klines, entry, end)
    funding = await client.get(FAPI, "/fapi/v1/fundingRate", {
        "symbol": symbol, "startTime": entry - 1, "endTime": end, "limit": FUNDING_PAGE_LIMIT})
    evidence["funding"] = normalize_funding(funding, symbol, entry - 1, end)
    return evidence
