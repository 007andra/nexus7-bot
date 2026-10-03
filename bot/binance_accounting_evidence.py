"""Read-only Binance USD-M accounting evidence for NEXUS-7.

This adapter deliberately has NO trading or daily-PnL authority. It collects
bounded Binance USER_DATA evidence and classifies order ownership only when the
exchange order identity is explicit. It never submits/cancels orders, changes
leverage, changes sizing, or authorizes entries.

A Binance user trade is a fill, not a closed-position record. Therefore this
module MUST NOT synthesize a KuCoin-style closed position or mark BGX_CONFIRMED
until a separate lifecycle reconstruction proves the complete opening/closing
fill set and durable opening-order lineage.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation

from bot import database as db
from bot.logger import log

ACCOUNTING_SOURCE = "BINANCE_USDM_OBSERVATION"
ACCOUNTING_AUTHORITY = False

_TRADE_FIELDS = (
    "symbol", "id", "orderId", "side", "price", "qty", "quoteQty",
    "realizedPnl", "commission", "commissionAsset", "time", "buyer",
    "maker", "positionSide",
)
_INCOME_FIELDS = (
    "symbol", "incomeType", "income", "asset", "info", "time",
    "tranId", "tradeId",
)
_ORDER_FIELDS = (
    "symbol", "orderId", "clientOrderId", "status", "side", "positionSide",
    "type", "origType", "reduceOnly", "closePosition", "executedQty",
    "avgPrice", "time", "updateTime",
)
_ALGO_FIELDS = (
    "symbol", "algoId", "clientAlgoId", "algoType", "orderType", "side",
    "positionSide", "quantity", "algoStatus", "actualOrderId", "actualPrice",
    "triggerPrice", "closePosition", "reduceOnly", "createTime", "updateTime",
    "triggerTime",
)


def _bounded_window(start_ms: int, end_ms: int, max_ms: int) -> tuple[int, int]:
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if start_ms <= 0 or end_ms <= start_ms or end_ms - start_ms > max_ms:
        raise ValueError("invalid accounting window")
    return start_ms, end_ms


def _safe_row(row: dict, fields: tuple[str, ...]) -> dict:
    if not isinstance(row, dict):
        raise ValueError("accounting row is not an object")
    return {key: row[key] for key in fields if key in row}


async def collect_user_trades(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Collect a complete <=7d trade window for one symbol or fail closed."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol required")

    data = await client._get(
        "/fapi/v1/userTrades",
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        auth=True,
    )
    if not isinstance(data, list):
        raise ValueError("userTrades response unconfirmed")
    if len(data) >= 1000:
        # A full page does not prove complete coverage. Refuse to invent it.
        raise ValueError("userTrades coverage exceeds single-window budget")

    rows: dict[str, dict] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("invalid userTrades row")
        if str(raw.get("symbol") or "").upper() != symbol:
            raise ValueError("userTrades symbol mismatch")
        if raw.get("id") is None or raw.get("orderId") is None:
            raise ValueError("userTrades identity missing")
        ts = int(raw.get("time", 0) or 0)
        if not start_ms <= ts <= end_ms:
            raise ValueError("userTrades row outside requested window")
        safe = _safe_row(raw, _TRADE_FIELDS)
        token = str(raw["id"])
        if token in rows and rows[token] != safe:
            raise ValueError("conflicting duplicate user trade")
        rows[token] = safe
    return sorted(rows.values(), key=lambda row: (int(row.get("time", 0)), int(row["id"])))


async def collect_orders(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Collect Binance normal-order identity evidence for a <=7d symbol window."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol required")

    data = await client._get(
        "/fapi/v1/allOrders",
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        auth=True,
    )
    if not isinstance(data, list):
        raise ValueError("allOrders response unconfirmed")
    if len(data) >= 1000:
        raise ValueError("allOrders coverage exceeds single-window budget")

    rows: dict[str, dict] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("invalid allOrders row")
        if str(raw.get("symbol") or "").upper() != symbol:
            raise ValueError("allOrders symbol mismatch")
        if raw.get("orderId") is None:
            raise ValueError("allOrders identity missing")
        safe = _safe_row(raw, _ORDER_FIELDS)
        token = str(raw["orderId"])
        if token in rows and rows[token] != safe:
            raise ValueError("conflicting duplicate order")
        rows[token] = safe
    return list(rows.values())


async def collect_algo_orders(client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Collect Binance conditional-order history for a <=7d symbol window."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol required")

    data = await client._get(
        "/fapi/v1/allAlgoOrders",
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        auth=True,
    )
    if not isinstance(data, list):
        raise ValueError("allAlgoOrders response unconfirmed")
    if len(data) >= 1000:
        raise ValueError("allAlgoOrders coverage exceeds single-window budget")

    rows: dict[str, dict] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("invalid allAlgoOrders row")
        if str(raw.get("symbol") or "").upper() != symbol:
            raise ValueError("allAlgoOrders symbol mismatch")
        if raw.get("algoId") is None:
            raise ValueError("allAlgoOrders identity missing")
        safe = _safe_row(raw, _ALGO_FIELDS)
        token = str(raw["algoId"])
        if token in rows and rows[token] != safe:
            raise ValueError("conflicting duplicate algo order")
        rows[token] = safe
    return list(rows.values())


async def collect_income(client, start_ms: int, end_ms: int) -> list[dict]:
    """Collect paginated Binance futures income evidence for a <=7d window."""
    start_ms, end_ms = _bounded_window(start_ms, end_ms, 7 * 86400000)
    rows: dict[str, dict] = {}
    for page in range(1, 21):
        data = await client._get(
            "/fapi/v1/income",
            params={
                "startTime": start_ms,
                "endTime": end_ms,
                "page": page,
                "limit": 1000,
            },
            auth=True,
        )
        if not isinstance(data, list):
            raise ValueError("income response unconfirmed")
        for raw in data:
            if not isinstance(raw, dict):
                raise ValueError("invalid income row")
            if raw.get("incomeType") is None or raw.get("tranId") is None:
                raise ValueError("income identity missing")
            ts = int(raw.get("time", 0) or 0)
            if not start_ms <= ts <= end_ms:
                raise ValueError("income row outside requested window")
            safe = _safe_row(raw, _INCOME_FIELDS)
            token = f"{raw.get('incomeType')}:{raw.get('tranId')}:{raw.get('tradeId', '')}"
            if token in rows and rows[token] != safe:
                raise ValueError("conflicting duplicate income row")
            rows[token] = safe
        if len(data) < 1000:
            return sorted(
                rows.values(),
                key=lambda row: (
                    int(row.get("time", 0)),
                    str(row.get("incomeType", "")),
                    str(row.get("tranId", "")),
                ),
            )
    raise ValueError("income coverage exceeds pagination budget")


def _registry_order_ids(registry) -> set[str]:
    if not isinstance(registry, list):
        return set()
    return {
        str(row.get("order_id"))
        for row in registry
        if isinstance(row, dict) and row.get("order_id") is not None
    }


def classify_trade_origin(trade: dict, order: dict | None, registry) -> tuple[str, str]:
    """Classify exchange order ownership without claiming a full position lifecycle."""
    if not isinstance(trade, dict) or trade.get("orderId") is None:
        return "UNKNOWN_UNATTRIBUTED", "TRADE_ORDER_ID_MISSING"
    if not isinstance(order, dict):
        return "UNKNOWN_UNATTRIBUTED", "ORDER_IDENTITY_UNAVAILABLE"

    order_id = str(trade["orderId"])
    if str(order.get("orderId") or "") != order_id:
        return "UNKNOWN_UNATTRIBUTED", "ORDER_IDENTITY_MISMATCH"
    if str(order.get("symbol") or "").upper() != str(trade.get("symbol") or "").upper():
        return "UNKNOWN_UNATTRIBUTED", "ORDER_SYMBOL_MISMATCH"

    client_oid = order.get("clientOrderId")
    if not isinstance(client_oid, str) or not client_oid:
        return "UNKNOWN_UNATTRIBUTED", "CLIENT_ORDER_ID_MISSING"

    durable_ids = _registry_order_ids(registry)
    if client_oid.startswith("bgx7-"):
        if order_id in durable_ids:
            # Strong order-level evidence, deliberately not BGX_CONFIRMED:
            # a fill is not by itself a complete position lifecycle.
            return "BGX_ORDER_EVIDENCE", "BGX_CLIENT_ORDER_ID_AND_DURABLE_ORDER"
        return "UNKNOWN_UNATTRIBUTED", "BGX_CLIENT_ORDER_ID_WITHOUT_DURABLE_ORDER"

    if order_id in durable_ids:
        return "UNKNOWN_UNATTRIBUTED", "DURABLE_ORDER_CONFLICTS_WITH_NON_BGX_CLIENT_ID"
    return "MANUAL_EXTERNAL", "NON_BGX_CLIENT_ORDER_ID_CONFIRMED"


def _opening_fill_order_ids(receipt, row):
    """Compatibility helper for external-origin telemetry; never grants authority."""
    fills = receipt.get("fills") if isinstance(receipt, dict) else None
    if not isinstance(fills, list):
        return []
    wanted = {
        "LONG": "BUY",
        "SHORT": "SELL",
        "BUY": "BUY",
        "SELL": "SELL",
    }.get(str((row or {}).get("side") or "").upper())
    if not wanted:
        return []
    seen = set()
    result = []
    for fill in fills:
        if not isinstance(fill, dict) or str(fill.get("side") or "").upper() != wanted:
            continue
        order_id = str(fill.get("orderId") or "")
        if order_id and order_id not in seen:
            seen.add(order_id)
            result.append(order_id)
    return result


async def _classify_origin(client, receipt, registry, row):
    """Compatibility contract; full BGX lifecycle authority remains intentionally disabled."""
    if isinstance(receipt, dict) and receipt.get("origin_class") == "MANUAL_EXTERNAL":
        return "MANUAL_EXTERNAL", str(receipt.get("origin_reason") or "NON_BGX_ORDER")
    return "UNKNOWN_UNATTRIBUTED", "BINANCE_POSITION_LIFECYCLE_NOT_YET_RECONSTRUCTED"


def _decimal(value, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not number.is_finite():
        raise ValueError(f"nonfinite {field}")
    return number


def _registry_map(registry) -> dict[str, dict]:
    result = {}
    for row in registry if isinstance(registry, list) else []:
        if not isinstance(row, dict) or not row.get("order_id"):
            continue
        token = str(row["order_id"])
        if token in result and result[token] != row:
            raise ValueError("conflicting durable order identity")
        result[token] = row
    return result


def _algo_actual_order_map(algo_orders) -> dict[str, dict]:
    result = {}
    for row in algo_orders if isinstance(algo_orders, list) else []:
        if not isinstance(row, dict):
            raise ValueError("invalid algo order evidence")
        actual = str(row.get("actualOrderId") or "")
        if not actual:
            continue
        if actual in result and result[actual] != row:
            raise ValueError("conflicting algo actualOrderId")
        result[actual] = row
    return result


def _lineage_valid(lineage: dict | None, opening: dict, symbol: str, side: str, first_fill_ms: int) -> bool:
    if not isinstance(lineage, dict) or lineage.get("version") != 2:
        return False
    order_id = str(opening.get("order_id") or "")
    if str(lineage.get("order_id") or "") != order_id:
        return False
    if str(lineage.get("symbol") or "").upper().removesuffix("M") != symbol.upper().removesuffix("M"):
        return False
    wanted_direction = "LONG" if side == "BUY" else "SHORT"
    if str(lineage.get("direction") or "").upper() != wanted_direction:
        return False
    try:
        created_ms = int(lineage.get("order_created_at_ms", 0) or 0)
        captured_ms = int(lineage.get("captured_at_ms", 0) or 0)
    except (TypeError, ValueError):
        return False
    if created_ms <= 0 or captured_ms <= 0 or captured_ms < created_ms:
        return False
    return abs(first_fill_ms - created_ms) <= 120000


def _bgx_close_identity(
    order_id: str,
    side: str,
    symbol: str,
    order_map: dict[str, dict],
    algo_actual_map: dict[str, dict],
    registry_map: dict[str, dict],
) -> tuple[bool, str]:
    order = order_map.get(order_id)
    if isinstance(order, dict):
        if str(order.get("symbol") or "").upper() != symbol:
            return False, "CLOSE_ORDER_SYMBOL_MISMATCH"
        if str(order.get("side") or "").upper() != side:
            return False, "CLOSE_ORDER_SIDE_MISMATCH"
        client_oid = str(order.get("clientOrderId") or "")
        durable = registry_map.get(order_id)
        if client_oid.startswith("bgx7-") and isinstance(durable, dict):
            intent = str(durable.get("exposure_intent") or "").upper()
            reduce_only = bool(durable.get("reduce_only", False))
            if intent == "REDUCE" or reduce_only:
                return True, "BGX_DURABLE_REDUCE_ORDER"

    algo = algo_actual_map.get(order_id)
    if isinstance(algo, dict):
        if str(algo.get("symbol") or "").upper() != symbol:
            return False, "ALGO_CLOSE_SYMBOL_MISMATCH"
        if str(algo.get("side") or "").upper() != side:
            return False, "ALGO_CLOSE_SIDE_MISMATCH"
        if str(algo.get("positionSide") or "BOTH").upper() != "BOTH":
            return False, "ALGO_HEDGE_MODE_UNSUPPORTED"
        client_algo_id = str(algo.get("clientAlgoId") or "")
        if client_algo_id.startswith("bgx7-"):
            return True, "BGX_ALGO_CLOSE_ORDER"

    return False, "CLOSE_ORDER_OWNERSHIP_UNCONFIRMED"


def reconstruct_bgx_lifecycles(
    trades: list[dict],
    orders: list[dict],
    algo_orders: list[dict],
    registry: list[dict],
    lineage_by_order_id: dict[str, dict],
) -> list[dict]:
    """Reconstruct only provably flat->BGX->flat one-way lifecycles.

    The function is intentionally strict. It rejects mixed ownership, a
    non-zero pre-entry exposure, hedge-mode fills, reversals through zero,
    non-USDT commissions, missing durable lineage, or incomplete closes.
    """
    order_map = {
        str(row.get("orderId")): row
        for row in orders if isinstance(row, dict) and row.get("orderId") is not None
    }
    durable = _registry_map(registry)
    algo_actual = _algo_actual_order_map(algo_orders)
    ordered = sorted(
        [row for row in trades if isinstance(row, dict)],
        key=lambda row: (int(row.get("time", 0) or 0), int(row.get("id", 0) or 0)),
    )
    result = []
    consumed_trade_ids: set[str] = set()

    for opening_id, opening in durable.items():
        if str(opening.get("exposure_intent") or "").upper() != "INCREASE":
            continue
        if bool(opening.get("reduce_only", False)):
            continue
        if str(opening.get("client_oid") or "").startswith("bgx7-") is False:
            continue
        try:
            previous_qty = _decimal(opening.get("previous_position_qty"), "previous_position_qty")
        except ValueError:
            continue
        if previous_qty != 0:
            continue

        open_order = order_map.get(opening_id)
        if not isinstance(open_order, dict):
            continue
        symbol = str(open_order.get("symbol") or "").upper()
        side = str(open_order.get("side") or "").upper()
        if not symbol or side not in {"BUY", "SELL"}:
            continue
        if str(open_order.get("clientOrderId") or "") != str(opening.get("client_oid") or ""):
            continue
        if str(open_order.get("positionSide") or "BOTH").upper() != "BOTH":
            continue

        anchor_fills = [
            row for row in ordered
            if str(row.get("orderId") or "") == opening_id
            and str(row.get("symbol") or "").upper() == symbol
        ]
        if not anchor_fills:
            continue
        first_fill_ms = int(anchor_fills[0].get("time", 0) or 0)
        lineage = lineage_by_order_id.get(opening_id)
        if not _lineage_valid(lineage, opening, symbol, side, first_fill_ms):
            continue

        balance = Decimal("0")
        opening_fills = []
        closing_fills = []
        close_reasons = set()
        failed = False
        started = False

        for fill in ordered:
            trade_id = str(fill.get("id") or "")
            if not trade_id or trade_id in consumed_trade_ids:
                continue
            if str(fill.get("symbol") or "").upper() != symbol:
                continue
            fill_time = int(fill.get("time", 0) or 0)
            if fill_time < first_fill_ms:
                continue
            if str(fill.get("positionSide") or "BOTH").upper() != "BOTH":
                failed = True
                break

            fill_side = str(fill.get("side") or "").upper()
            if fill_side not in {"BUY", "SELL"}:
                failed = True
                break
            try:
                qty = _decimal(fill.get("qty"), "fill qty")
                price = _decimal(fill.get("price"), "fill price")
                realized = _decimal(fill.get("realizedPnl", "0"), "realizedPnl")
                commission = _decimal(fill.get("commission", "0"), "commission")
            except ValueError:
                failed = True
                break
            if qty <= 0 or price <= 0 or commission < 0:
                failed = True
                break
            if str(fill.get("commissionAsset") or "USDT").upper() != "USDT":
                failed = True
                break

            fill_order_id = str(fill.get("orderId") or "")
            if fill_side == side:
                if fill_order_id != opening_id:
                    if started:
                        failed = True
                        break
                    continue
                if realized != 0:
                    failed = True
                    break
                started = True
                balance += qty
                opening_fills.append(fill)
                continue

            if not started:
                continue
            owned_close, close_reason = _bgx_close_identity(
                fill_order_id, fill_side, symbol, order_map, algo_actual, durable
            )
            if not owned_close:
                failed = True
                break
            close_reasons.add(close_reason)
            balance -= qty
            closing_fills.append(fill)
            if balance < 0:
                failed = True
                break
            if balance == 0:
                break

        if failed or not opening_fills or not closing_fills or balance != 0:
            continue

        open_qty = sum((_decimal(x["qty"], "open qty") for x in opening_fills), Decimal("0"))
        close_qty = sum((_decimal(x["qty"], "close qty") for x in closing_fills), Decimal("0"))
        if open_qty <= 0 or open_qty != close_qty:
            continue
        open_vwap = sum(
            (_decimal(x["qty"], "open qty") * _decimal(x["price"], "open price") for x in opening_fills),
            Decimal("0"),
        ) / open_qty
        close_vwap = sum(
            (_decimal(x["qty"], "close qty") * _decimal(x["price"], "close price") for x in closing_fills),
            Decimal("0"),
        ) / close_qty
        commission_total = sum(
            (_decimal(x.get("commission", "0"), "commission") for x in opening_fills + closing_fills),
            Decimal("0"),
        )
        realized_total = sum(
            (_decimal(x.get("realizedPnl", "0"), "realizedPnl") for x in opening_fills + closing_fills),
            Decimal("0"),
        )
        confirmed_net = realized_total - commission_total
        last_trade_id = str(closing_fills[-1].get("id"))
        close_id = f"BINANCE:{symbol}:{opening_id}:{last_trade_id}"
        row = {
            "closeId": close_id,
            "symbol": symbol,
            "settleCurrency": "USDT",
            "side": "LONG" if side == "BUY" else "SHORT",
            "pnl": str(confirmed_net),
            "realizedPnl": str(realized_total),
            "tradeFee": str(commission_total),
            "openTime": int(opening_fills[0]["time"]),
            "closeTime": int(closing_fills[-1]["time"]),
            "openPrice": str(open_vwap),
            "closePrice": str(close_vwap),
        }
        receipt = {
            "source": "BINANCE_USDM_LIFECYCLE",
            "ownership": "BGX_ORDER_IDS",
            "fills_reconciled": True,
            "lineage_reconciled": True,
            "opening_order_ids": [opening_id],
            "closing_order_ids": sorted({str(x["orderId"]) for x in closing_fills}),
            "fills": opening_fills + closing_fills,
            "lineage": lineage,
            "close_identity": sorted(close_reasons),
            "funding_included": False,
            "accounting_authority": False,
        }
        consumed_trade_ids.update(str(x["id"]) for x in opening_fills + closing_fills)
        result.append({"row": row, "receipt": receipt})

    return sorted(result, key=lambda item: int(item["row"]["closeTime"]))


async def _load_lineage_map(registry: list[dict]) -> dict[str, dict]:
    from bot.post_trade_forensics import _lineage_key

    result = {}
    for row in registry:
        if not isinstance(row, dict) or not row.get("order_id"):
            continue
        if str(row.get("exposure_intent") or "").upper() != "INCREASE":
            continue
        order_id = str(row["order_id"])
        raw = await db.load_key_value(_lineage_key(order_id), strict=True)
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result[order_id] = value
    return result


async def _load_registry() -> list[dict]:
    from bot.durable_execution import _ORDER_KEY
    raw = await db.load_key_value(_ORDER_KEY, strict=True)
    if not raw:
        return []
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid durable order registry")
    orders = value.get("orders")
    if not isinstance(orders, list):
        raise ValueError("invalid durable order registry orders")
    return orders


def _normalize_registry_symbol(raw: str, instruments: set[str]) -> str | None:
    symbol = str(raw or "").upper()
    if symbol in instruments:
        return symbol
    if symbol.endswith("M") and symbol[:-1] in instruments:
        return symbol[:-1]
    return None


async def audit(engine):
    """Persist recent trade/order ownership evidence; authority remains false."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 48 * 3600000
    try:
        registry = await _load_registry()
        instruments = set(getattr(engine.client, "_instruments", {}) or {})
        symbols = {
            normalized
            for row in registry
            if isinstance(row, dict)
            for normalized in [_normalize_registry_symbol(row.get("symbol", ""), instruments)]
            if normalized
        }
        # Keep the observation path testable even before a Binance durable order
        # exists, while bounding shared-egress API weight to one symbol.
        if not symbols and "BTCUSDT" in instruments:
            symbols = {"BTCUSDT"}

        counts = {
            "trades": 0,
            "bgx_order_evidence": 0,
            "manual_external": 0,
            "unknown": 0,
        }
        lineage_map = await _load_lineage_map(registry)
        lifecycle_candidates = {
            str(row.get("symbol") or "").upper().removesuffix("M")
            for row in registry
            if isinstance(row, dict)
            and str(row.get("client_oid") or "").startswith("bgx7-")
            and str(row.get("order_id") or "").isdigit()
            and str(row.get("exposure_intent") or "").upper() == "INCREASE"
            and row.get("previous_position_qty") is not None
        }
        authoritative_cycles = 0
        for symbol in sorted(symbols)[:20]:
            need_lifecycle = symbol in lifecycle_candidates
            if need_lifecycle:
                trades, orders, algo_orders = await asyncio.gather(
                    collect_user_trades(engine.client, symbol, start_ms, end_ms),
                    collect_orders(engine.client, symbol, start_ms, end_ms),
                    collect_algo_orders(engine.client, symbol, start_ms, end_ms),
                )
            else:
                trades, orders = await asyncio.gather(
                    collect_user_trades(engine.client, symbol, start_ms, end_ms),
                    collect_orders(engine.client, symbol, start_ms, end_ms),
                )
                algo_orders = []
            order_map = {str(row["orderId"]): row for row in orders}
            for trade in trades:
                order = order_map.get(str(trade["orderId"]))
                origin_class, origin_reason = classify_trade_origin(trade, order, registry)
                receipt = dict(
                    trade,
                    source="BINANCE_USER_TRADES",
                    origin_class=origin_class,
                    origin_reason=origin_reason,
                    accounting_authority=False,
                )
                token = f"{trade['symbol']}:{trade['id']}"
                key = "binance:accounting:trade:" + hashlib.sha256(token.encode()).hexdigest()[:32]
                encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
                previous = await db.load_key_value(key, strict=True)
                if previous != encoded:
                    if await db.save_key_value(key, encoded, strict=True) is not True:
                        raise db.PersistenceError("Binance trade evidence persistence unconfirmed")
                counts["trades"] += 1
                if origin_class == "BGX_ORDER_EVIDENCE":
                    counts["bgx_order_evidence"] += 1
                elif origin_class == "MANUAL_EXTERNAL":
                    counts["manual_external"] += 1
                else:
                    counts["unknown"] += 1

            if need_lifecycle:
                lifecycles = reconstruct_bgx_lifecycles(
                    trades, orders, algo_orders, registry, lineage_map
                )
                for item in lifecycles:
                    row = item["row"]
                    receipt = item["receipt"]
                    token = str(row["closeId"])
                    key = "binance:accounting:lifecycle:" + hashlib.sha256(
                        token.encode()
                    ).hexdigest()[:32]
                    encoded = json.dumps(
                        {"row": row, "receipt": receipt},
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    previous = await db.load_key_value(key, strict=True)
                    if previous != encoded:
                        if await db.save_key_value(key, encoded, strict=True) is not True:
                            raise db.PersistenceError(
                                "Binance lifecycle evidence persistence unconfirmed"
                            )
                    authoritative_cycles += 1

        log.warning(
            "[BINANCE_ACCOUNTING_EVIDENCE] status=PASS symbols=%s trades=%s "
            "bgx_order_evidence=%s manual_external=%s unknown=%s "
            "authority=false lifecycle_reconstruction=true authoritative_cycles=%s "
            "release_state=AWAITING_CONTROLLED_LIVE_EVIDENCE execution_effect=NONE",
            len(symbols), counts["trades"], counts["bgx_order_evidence"],
            counts["manual_external"], counts["unknown"], authoritative_cycles,
        )
        return counts
    except Exception as exc:
        log.warning(
            "[BINANCE_ACCOUNTING_EVIDENCE] status=UNCONFIRMED error_type=%s "
            "authority=false execution_effect=NONE",
            type(exc).__name__,
        )
        return None


async def audit_ledger(engine):
    """Persist recent futures income evidence; never changes risk/accounting authority."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 48 * 3600000
    try:
        rows = await collect_income(engine.client, start_ms, end_ms)
        by_type: dict[str, dict[str, float | int]] = {}
        for row in rows:
            token = f"{row.get('incomeType')}:{row.get('tranId')}:{row.get('tradeId', '')}"
            key = "binance:accounting:income:" + hashlib.sha256(token.encode()).hexdigest()[:32]
            receipt = dict(row, source="BINANCE_INCOME", accounting_authority=False)
            encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
            previous = await db.load_key_value(key, strict=True)
            if previous != encoded:
                if await db.save_key_value(key, encoded, strict=True) is not True:
                    raise db.PersistenceError("Binance income evidence persistence unconfirmed")
            kind = str(row.get("incomeType") or "UNKNOWN")
            slot = by_type.setdefault(kind, {"count": 0, "income": 0.0})
            slot["count"] = int(slot["count"]) + 1
            slot["income"] = float(slot["income"]) + float(row.get("income", 0) or 0)

        log.warning(
            "[BINANCE_ACCOUNTING_LEDGER] status=PASS rows=%s types=%s "
            "authority=false execution_effect=NONE",
            len(rows),
            ",".join(
                f"{kind}:count={int(values['count'])}:income={float(values['income']):.8f}"
                for kind, values in sorted(by_type.items())
            ) or "NONE",
        )
        return rows
    except Exception as exc:
        log.warning(
            "[BINANCE_ACCOUNTING_LEDGER] status=UNCONFIRMED error_type=%s "
            "authority=false execution_effect=NONE",
            type(exc).__name__,
        )
        return None


async def _audit_all(engine):
    await audit(engine)
    now = time.monotonic()
    if now >= getattr(engine, "_binance_accounting_ledger_next", 0.0):
        engine._binance_accounting_ledger_next = now + 3600.0
        await audit_ledger(engine)


def schedule(engine):
    """Low-frequency read-only observation, isolated from PAPER decisions."""
    task = getattr(engine, "_binance_accounting_evidence_task", None)
    if task is not None and not task.done():
        return
    now = time.monotonic()
    interval = 3600.0 if getattr(engine, "paper_trade", True) else 600.0
    if now < getattr(engine, "_binance_accounting_evidence_next", 0.0):
        return
    engine._binance_accounting_evidence_next = now + interval
    task = asyncio.create_task(_audit_all(engine))
    engine._binance_accounting_evidence_task = task
    background = getattr(engine, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)
