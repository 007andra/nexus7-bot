"""Read-only Binance USD-M exit forensics for disappearing BGX positions.

The collector never submits, cancels, amends, or retries trading orders. It reads
exchange history after a local BGX position disappears and persists a sanitized
receipt for post-trade reconciliation. Any collection/persistence failure is
telemetry-only and cannot change trading/risk state.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bot import database as db
from bot.binance_accounting_evidence import (
    collect_algo_orders,
    collect_income,
    collect_orders,
    collect_user_trades,
)

_FORCE_FIELDS = (
    "symbol",
    "orderId",
    "clientOrderId",
    "status",
    "side",
    "positionSide",
    "type",
    "origType",
    "timeInForce",
    "origQty",
    "executedQty",
    "avgPrice",
    "price",
    "stopPrice",
    "reduceOnly",
    "closePosition",
    "autoCloseType",
    "time",
    "updateTime",
)
_MAX_WINDOW_MS = 7 * 86400000
_BACKFILL_MAX_AGE_MS = 48 * 3600000


def _decimal(value, label: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not out.is_finite():
        raise ValueError(f"nonfinite {label}")
    return out


def _safe_force_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("force order row is not an object")
    return {key: row[key] for key in _FORCE_FIELDS if key in row}


async def collect_force_orders(
    client, symbol: str, start_ms: int, end_ms: int
) -> list[dict]:
    """Read Binance liquidation/ADL history for one bounded symbol window."""
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    symbol = str(symbol or "").upper()
    if not symbol:
        raise ValueError("symbol required")
    if start_ms <= 0 or end_ms <= start_ms or end_ms - start_ms > _MAX_WINDOW_MS:
        raise ValueError("invalid force-order window")
    data = await client._get(
        "/fapi/v1/forceOrders",
        params={
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 100,
        },
        auth=True,
    )
    if not isinstance(data, list):
        raise ValueError("forceOrders response unconfirmed")
    if len(data) >= 100:
        raise ValueError("forceOrders coverage exceeds single-window budget")

    rows: dict[str, dict] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("invalid forceOrders row")
        if str(raw.get("symbol") or "").upper() != symbol:
            raise ValueError("forceOrders symbol mismatch")
        if raw.get("orderId") is None:
            raise ValueError("forceOrders identity missing")
        safe = _safe_force_row(raw)
        token = str(raw["orderId"])
        if token in rows and rows[token] != safe:
            raise ValueError("conflicting duplicate force order")
        rows[token] = safe
    return list(rows.values())


def _vwap(rows: list[dict]) -> Decimal:
    qty = sum((_decimal(row.get("qty"), "qty") for row in rows), Decimal("0"))
    if qty <= 0:
        raise ValueError("zero fill quantity")
    notional = sum(
        (
            _decimal(row.get("qty"), "qty")
            * _decimal(row.get("price"), "price")
            for row in rows
        ),
        Decimal("0"),
    )
    return notional / qty


def _force_cause(row: dict) -> str:
    kind = str(row.get("autoCloseType") or "").upper()
    client_id = str(row.get("clientOrderId") or "").lower()
    if kind in {"LIQUIDATION", "ADL"}:
        return "EXCHANGE_" + kind
    if client_id.startswith("adl_autoclose"):
        return "EXCHANGE_ADL"
    if client_id.startswith("autoclose-"):
        return "EXCHANGE_LIQUIDATION"
    return "EXCHANGE_FORCE_ORDER"


def reconcile_exit_evidence(
    *,
    symbol: str,
    opening_order_id: str,
    trades: list[dict],
    orders: list[dict],
    algo_orders: list[dict],
    force_orders: list[dict],
    income: list[dict],
) -> dict:
    """Reconcile exact fills/PnL and classify close cause only when evidenced."""
    symbol = str(symbol or "").upper()
    opening_order_id = str(opening_order_id or "")
    if not symbol or not opening_order_id:
        raise ValueError("symbol and opening_order_id required")

    ordered_trades = sorted(
        [
            row
            for row in trades
            if isinstance(row, dict)
            and str(row.get("symbol") or "").upper() == symbol
        ],
        key=lambda row: (int(row.get("time", 0) or 0), int(row.get("id", 0) or 0)),
    )
    opening = [
        row for row in ordered_trades if str(row.get("orderId") or "") == opening_order_id
    ]
    if not opening:
        return {
            "status": "OPENING_FILL_UNCONFIRMED",
            "symbol": symbol,
            "opening_order_id": opening_order_id,
            "pnl_fill_authority": False,
            "cause_authority": False,
        }

    open_side = str(opening[0].get("side") or "").upper()
    if open_side not in {"BUY", "SELL"}:
        raise ValueError("opening fill side invalid")
    close_side = "SELL" if open_side == "BUY" else "BUY"
    first_fill_ms = min(int(row.get("time", 0) or 0) for row in opening)
    opening_qty = sum((_decimal(row.get("qty"), "opening qty") for row in opening), Decimal("0"))
    if opening_qty <= 0:
        raise ValueError("opening quantity invalid")

    remaining = opening_qty
    closing: list[dict] = []
    for row in ordered_trades:
        if int(row.get("time", 0) or 0) < first_fill_ms:
            continue
        row_side = str(row.get("side") or "").upper()
        row_order_id = str(row.get("orderId") or "")
        if row_side == open_side:
            if row_order_id == opening_order_id:
                continue
            return {
                "status": "MIXED_OWNERSHIP_OR_REENTRY",
                "symbol": symbol,
                "opening_order_id": opening_order_id,
                "pnl_fill_authority": False,
                "cause_authority": False,
            }
        if row_side != close_side:
            raise ValueError("trade side invalid")
        qty = _decimal(row.get("qty"), "closing qty")
        if qty <= 0:
            raise ValueError("closing quantity invalid")
        closing.append(row)
        remaining -= qty
        if remaining <= 0:
            break

    if not closing or remaining > 0:
        return {
            "status": "CLOSE_FILL_INCOMPLETE",
            "symbol": symbol,
            "opening_order_id": opening_order_id,
            "opening_qty": str(opening_qty),
            "observed_close_qty": str(opening_qty - remaining),
            "pnl_fill_authority": False,
            "cause_authority": False,
        }
    if remaining < 0:
        return {
            "status": "REVERSAL_OR_OVER_CLOSE",
            "symbol": symbol,
            "opening_order_id": opening_order_id,
            "pnl_fill_authority": False,
            "cause_authority": False,
        }

    order_map = {
        str(row.get("orderId")): row
        for row in orders
        if isinstance(row, dict) and row.get("orderId") is not None
    }
    algo_map = {
        str(row.get("actualOrderId")): row
        for row in algo_orders
        if isinstance(row, dict) and str(row.get("actualOrderId") or "")
    }
    force_map = {
        str(row.get("orderId")): row
        for row in force_orders
        if isinstance(row, dict) and row.get("orderId") is not None
    }

    close_order_ids = sorted({str(row.get("orderId") or "") for row in closing})
    close_trade_ids = [str(row.get("id") or "") for row in closing]
    causes: list[str] = []
    identities: dict[str, str] = {}
    for order_id in close_order_ids:
        algo = algo_map.get(order_id)
        if (
            isinstance(algo, dict)
            and str(algo.get("clientAlgoId") or "").startswith("bgx7-")
            and str(algo.get("symbol") or "").upper() == symbol
        ):
            order_type = str(algo.get("orderType") or "CONDITIONAL").upper()
            cause = "BGX_ALGO_" + order_type
        elif order_id in force_map:
            cause = _force_cause(force_map[order_id])
        else:
            order = order_map.get(order_id)
            client_id = str((order or {}).get("clientOrderId") or "")
            cause = (
                "BGX_DIRECT_CLOSE"
                if client_id.startswith("bgx7-")
                else "UNATTRIBUTED_CLOSE_ORDER"
            )
        identities[order_id] = cause
        causes.append(cause)

    commission_rows = opening + closing
    commission_assets = {
        str(row.get("commissionAsset") or "USDT").upper() for row in commission_rows
    }
    pnl_fill_authority = commission_assets == {"USDT"}
    realized = sum(
        (_decimal(row.get("realizedPnl", "0"), "realizedPnl") for row in commission_rows),
        Decimal("0"),
    )
    commission = sum(
        (_decimal(row.get("commission", "0"), "commission") for row in commission_rows),
        Decimal("0"),
    )

    close_end_ms = max(int(row.get("time", 0) or 0) for row in closing)
    funding_rows = [
        row
        for row in income
        if isinstance(row, dict)
        and str(row.get("symbol") or "").upper() == symbol
        and str(row.get("incomeType") or "").upper() == "FUNDING_FEE"
        and first_fill_ms <= int(row.get("time", 0) or 0) <= close_end_ms
    ]
    funding = sum(
        (_decimal(row.get("income", "0"), "funding income") for row in funding_rows),
        Decimal("0"),
    )

    realized_income_rows = [
        row
        for row in income
        if isinstance(row, dict)
        and str(row.get("symbol") or "").upper() == symbol
        and str(row.get("incomeType") or "").upper() == "REALIZED_PNL"
        and str(row.get("tradeId") or "") in set(close_trade_ids)
    ]
    realized_income = sum(
        (_decimal(row.get("income", "0"), "realized income") for row in realized_income_rows),
        Decimal("0"),
    )
    realized_crosscheck = bool(realized_income_rows) and realized_income == realized

    net_after_funding = realized - commission + funding
    cause_authority = bool(causes) and all(
        cause != "UNATTRIBUTED_CLOSE_ORDER" for cause in causes
    )
    if not cause_authority:
        cause = "UNATTRIBUTED"
    elif len(set(causes)) == 1:
        cause = causes[0]
    else:
        cause = "MULTI_CAUSE:" + ",".join(sorted(set(causes)))

    return {
        "status": "RECONCILED" if pnl_fill_authority else "PNL_ASSET_UNCONFIRMED",
        "symbol": symbol,
        "opening_order_id": opening_order_id,
        "opening_side": open_side,
        "opening_qty": str(opening_qty),
        "open_vwap": str(_vwap(opening)),
        "close_vwap": str(_vwap(closing)),
        "close_order_ids": close_order_ids,
        "close_trade_ids": close_trade_ids,
        "close_identity": identities,
        "cause": cause,
        "cause_authority": cause_authority,
        "pnl_fill_authority": pnl_fill_authority,
        "realized_pnl": str(realized),
        "commission": str(commission),
        "funding": str(funding),
        "net_after_funding": str(net_after_funding),
        "realized_income_crosscheck": realized_crosscheck,
        "realized_income": str(realized_income),
        "first_fill_ms": first_fill_ms,
        "close_fill_ms": close_end_ms,
    }


def _lineage_from_position_or_registry(engine, symbol: str, position) -> dict | None:
    lineage = getattr(position, "_forensic_lineage", None)
    if isinstance(lineage, dict) and lineage.get("order_id"):
        return dict(lineage)

    registry = getattr(engine, "orders", None)
    if registry is None or not hasattr(registry, "snapshot"):
        return None
    try:
        rows = registry.snapshot()
    except Exception:
        return None
    candidates = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").upper().removesuffix("M") != str(symbol).upper():
            continue
        if str(row.get("client_oid") or "").startswith("bgx7-") is False:
            continue
        if str(row.get("state") or "").upper() != "FILLED":
            continue
        if str(row.get("exposure_intent") or "").upper() != "INCREASE":
            continue
        if not row.get("order_id"):
            continue
        candidates.append(row)
    if not candidates:
        return None
    row = max(candidates, key=lambda item: float(item.get("created_at", 0) or 0))
    return {
        "order_id": str(row.get("order_id")),
        "client_oid": str(row.get("client_oid") or ""),
        "order_created_at_ms": int(float(row.get("created_at", 0) or 0) * 1000),
    }


def _window(lineage: dict, position=None) -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    start_ms = int(lineage.get("order_created_at_ms", 0) or 0)
    if start_ms <= 0 and position is not None:
        opened = getattr(position, "opened_at", None)
        if isinstance(opened, datetime):
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            start_ms = int(opened.timestamp() * 1000)
    if start_ms <= 0:
        start_ms = now_ms - 6 * 3600000
    start_ms = max(now_ms - _MAX_WINDOW_MS + 1000, start_ms - 5 * 60000)
    return start_ms, now_ms


def _receipt_key(symbol: str, opening_order_id: str) -> str:
    token = hashlib.sha256(f"{symbol}:{opening_order_id}".encode()).hexdigest()[:32]
    return "binance:exit_forensics:" + token


async def _handoff_confirmed_daily_pnl(
    engine, receipt: dict, *, symbol: str, opening_order_id: str, log
) -> bool:
    """Bridge fills-authoritative Binance exit evidence into durable daily PnL.

    This is accounting-only. It never changes orders, sizing, leverage, signals,
    or risk thresholds. A local estimate remains authoritative unless the
    existing forensic receipt proves the exact BGX opening lineage and
    fills-authoritative net PnL.
    """
    if not isinstance(receipt, dict):
        return False
    if (
        receipt.get("status") != "RECONCILED"
        or receipt.get("pnl_fill_authority") is not True
    ):
        return False
    try:
        close_ms = int(receipt.get("close_fill_ms", 0) or 0)
        confirmed = float(receipt.get("net_after_funding", "nan"))
        if close_ms <= 0 or not math.isfinite(confirmed):
            return False
        row = {
            "closeId": (
                f"BINANCE:{opening_order_id}:"
                f"{','.join(str(x) for x in receipt.get('close_order_ids', []) if x)}:"
                f"{close_ms}"
            ),
            "symbol": str(symbol),
            "closeTime": close_ms,
            "pnl": confirmed,
        }
        verified = {
            "ownership": "BGX_ORDER_IDS",
            "fills_reconciled": True,
            "lineage_reconciled": True,
            "opening_order_ids": [str(opening_order_id)],
            "exchange": "BINANCE",
        }
        from bot.durable_daily_pnl import reconcile_confirmed_exchange
        updated = bool(
            await reconcile_confirmed_exchange(engine, row, verified)
        )
        log.warning(
            "[BINANCE_DAILY_PNL_HANDOFF] symbol=%s opening_order_id=%s "
            "result=%s pnl_fill_authority=true execution_effect=ACCOUNTING_ONLY",
            symbol,
            opening_order_id,
            "RECONCILED" if updated else "NO_MATCHING_ESTIMATE",
        )
        return updated
    except Exception as exc:
        log.warning(
            "[BINANCE_DAILY_PNL_HANDOFF] symbol=%s opening_order_id=%s "
            "result=UNCONFIRMED error_type=%s execution_effect=NONE",
            symbol,
            opening_order_id,
            type(exc).__name__,
        )
        return False


async def capture_exit(
    engine,
    *,
    symbol: str,
    opening_order_id: str,
    start_ms: int,
    end_ms: int,
    log,
) -> dict | None:
    """Collect all relevant read-only exchange evidence for one disappeared position."""
    collectors = {
        "userTrades": collect_user_trades(engine.client, symbol, start_ms, end_ms),
        "allOrders": collect_orders(engine.client, symbol, start_ms, end_ms),
        "allAlgoOrders": collect_algo_orders(engine.client, symbol, start_ms, end_ms),
        "income": collect_income(engine.client, start_ms, end_ms),
        "forceOrders": collect_force_orders(engine.client, symbol, start_ms, end_ms),
    }
    names = list(collectors)
    results = await asyncio.gather(*collectors.values(), return_exceptions=True)
    payload: dict[str, list[dict]] = {}
    endpoint_state = {}
    for name, value in zip(names, results):
        if isinstance(value, Exception):
            endpoint_state[name] = "ERROR:" + type(value).__name__
            payload[name] = []
        else:
            endpoint_state[name] = "PASS"
            payload[name] = value

    if endpoint_state["userTrades"] != "PASS" or endpoint_state["allOrders"] != "PASS":
        log.warning(
            "[BINANCE_EXIT_FORENSICS] symbol=%s result=UNCONFIRMED opening_order_id=%s "
            "endpoints=%s pnl_fill_authority=false cause_authority=false "
            "decision_effect=NONE execution_effect=NONE",
            symbol,
            opening_order_id,
            json.dumps(endpoint_state, sort_keys=True, separators=(",", ":")),
        )
        return None

    receipt = reconcile_exit_evidence(
        symbol=symbol,
        opening_order_id=opening_order_id,
        trades=payload["userTrades"],
        orders=payload["allOrders"],
        algo_orders=payload["allAlgoOrders"],
        force_orders=payload["forceOrders"],
        income=payload["income"],
    )
    receipt["endpoints"] = endpoint_state
    receipt["captured_at_ms"] = int(time.time() * 1000)
    receipt["source"] = "BINANCE_EXIT_FORENSICS_READ_ONLY"
    receipt["execution_effect"] = "NONE"

    try:
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        await db.save_key_value(
            _receipt_key(symbol, opening_order_id), encoded, strict=True
        )
    except Exception as exc:
        log.warning(
            "[BINANCE_EXIT_FORENSICS] symbol=%s result=PERSISTENCE_UNCONFIRMED "
            "opening_order_id=%s error_type=%s decision_effect=NONE execution_effect=NONE",
            symbol,
            opening_order_id,
            type(exc).__name__,
        )

    await _handoff_confirmed_daily_pnl(
        engine,
        receipt,
        symbol=symbol,
        opening_order_id=opening_order_id,
        log=log,
    )

    # Bind any recovery post-trade decision to the Binance opening fill time.
    # Startup backfills older than the durable episode are ignored. A close that
    # belongs to the episode but lacks fills-authoritative PnL disarms Recovery
    # conservatively instead of silently continuing.
    try:
        from bot.drawdown_recovery import record_recovery_close
        pnl_authority = bool(receipt.get("pnl_fill_authority"))
        recovery_ok, recovery_reason = await record_recovery_close(
            float(receipt.get("net_after_funding", "nan")) if pnl_authority else float("nan"),
            opening_fill_ms=int(receipt.get("first_fill_ms", 0) or 0),
            strict=True,
        )
        if recovery_reason not in {"not_armed", "outside_episode"}:
            log.critical(
                "[RECOVERY_POST_TRADE] symbol=%s opening_order_id=%s "
                "result=%s reason=%s pnl_authority=%s opening_fill_ms=%s",
                symbol, opening_order_id,
                "CONTINUE" if recovery_ok else "DISARMED",
                recovery_reason,
                "fills" if pnl_authority else "unconfirmed",
                receipt.get("first_fill_ms", "NA"),
            )
    except Exception as exc:
        log.critical(
            "[RECOVERY_POST_TRADE] symbol=%s opening_order_id=%s "
            "result=UNCONFIRMED reason=%s execution_effect=BLOCK_ON_NEXT_RECOVERY_CHECK",
            symbol, opening_order_id, type(exc).__name__,
        )

    log.warning(
        "[BINANCE_EXIT_FORENSICS] symbol=%s result=%s opening_order_id=%s "
        "close_order_ids=%s cause=%s cause_authority=%s pnl_fill_authority=%s "
        "open_vwap=%s close_vwap=%s realized_pnl=%s commission=%s funding=%s "
        "net_after_funding=%s income_crosscheck=%s endpoints=%s "
        "decision_effect=NONE execution_effect=NONE",
        symbol,
        receipt.get("status", "UNKNOWN"),
        opening_order_id,
        ",".join(receipt.get("close_order_ids", [])) or "NONE",
        receipt.get("cause", "UNKNOWN"),
        str(bool(receipt.get("cause_authority"))).lower(),
        str(bool(receipt.get("pnl_fill_authority"))).lower(),
        receipt.get("open_vwap", "NA"),
        receipt.get("close_vwap", "NA"),
        receipt.get("realized_pnl", "NA"),
        receipt.get("commission", "NA"),
        receipt.get("funding", "NA"),
        receipt.get("net_after_funding", "NA"),
        str(bool(receipt.get("realized_income_crosscheck"))).lower(),
        json.dumps(endpoint_state, sort_keys=True, separators=(",", ":")),
    )
    return receipt


async def _capture_exit_safely(engine, *, symbol, opening_order_id, start_ms, end_ms, log):
    try:
        return await capture_exit(
            engine,
            symbol=symbol,
            opening_order_id=opening_order_id,
            start_ms=start_ms,
            end_ms=end_ms,
            log=log,
        )
    except Exception as exc:
        log.warning(
            "[BINANCE_EXIT_FORENSICS] symbol=%s result=UNCONFIRMED "
            "opening_order_id=%s error_type=%s decision_effect=NONE execution_effect=NONE",
            symbol,
            opening_order_id,
            type(exc).__name__,
        )
        return None


def _schedule(engine, coro) -> None:
    task = asyncio.create_task(coro)
    background = getattr(engine, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)


def _recent_flat_bgx_entries(engine, now_ms: int) -> list[dict]:
    registry = getattr(engine, "orders", None)
    if registry is None or not hasattr(registry, "snapshot"):
        return []
    try:
        rows = registry.snapshot()
    except Exception:
        return []
    active = {str(symbol).upper() for symbol in (getattr(engine, "positions", {}) or {})}
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().removesuffix("M")
        if not symbol or symbol in active:
            continue
        if str(row.get("state") or "").upper() != "FILLED":
            continue
        if str(row.get("exposure_intent") or "").upper() != "INCREASE":
            continue
        if bool(row.get("reduce_only", False)):
            continue
        if not str(row.get("client_oid") or "").startswith("bgx7-"):
            continue
        if not row.get("order_id"):
            continue
        created_ms = int(float(row.get("created_at", 0) or 0) * 1000)
        if created_ms <= 0 or now_ms - created_ms > _BACKFILL_MAX_AGE_MS:
            continue
        out.append(row)
    return sorted(out, key=lambda row: float(row.get("created_at", 0) or 0), reverse=True)[:3]


def install(TradingEngine, log) -> None:
    if getattr(TradingEngine, "_binance_exit_forensics_installed", False):
        return

    original_sync = TradingEngine._sync_positions

    async def _sync_positions_with_exit_forensics(self, *args, **kwargs):
        before = dict(getattr(self, "positions", {}) or {})
        result = await original_sync(self, *args, **kwargs)

        if getattr(self, "paper_trade", True):
            return result

        seen = getattr(self, "_binance_exit_forensics_seen", None)
        if not isinstance(seen, set):
            seen = set()
            self._binance_exit_forensics_seen = seen

        after = set((getattr(self, "positions", {}) or {}).keys())
        for symbol, position in before.items():
            if symbol in after:
                continue
            lineage = _lineage_from_position_or_registry(self, symbol, position)
            if not isinstance(lineage, dict):
                continue
            opening_order_id = str(lineage.get("order_id") or "")
            if not opening_order_id:
                continue
            token = (str(symbol), opening_order_id)
            if token in seen:
                continue
            seen.add(token)
            start_ms, end_ms = _window(lineage, position)
            _schedule(
                self,
                _capture_exit_safely(
                    self,
                    symbol=str(symbol),
                    opening_order_id=opening_order_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    log=log,
                ),
            )

        if not getattr(self, "_binance_exit_forensics_backfill_started", False):
            self._binance_exit_forensics_backfill_started = True
            now_ms = int(time.time() * 1000)
            for row in _recent_flat_bgx_entries(self, now_ms):
                symbol = str(row.get("symbol") or "").upper().removesuffix("M")
                opening_order_id = str(row.get("order_id") or "")
                token = (symbol, opening_order_id)
                if token in seen:
                    continue
                seen.add(token)
                lineage = {
                    "order_id": opening_order_id,
                    "order_created_at_ms": int(
                        float(row.get("created_at", 0) or 0) * 1000
                    ),
                }
                start_ms, end_ms = _window(lineage)
                _schedule(
                    self,
                    _capture_exit_safely(
                        self,
                        symbol=symbol,
                        opening_order_id=opening_order_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        log=log,
                    ),
                )
        return result

    TradingEngine._sync_positions = _sync_positions_with_exit_forensics
    TradingEngine._binance_exit_forensics_installed = True
    log.info(
        "[BINANCE_EXIT_FORENSICS] installed=true exchange_reads_only=true "
        "sources=userTrades,allOrders,allAlgoOrders,income,forceOrders "
        "backfill_hours=48 order_mutations=false risk_mutations=false "
        "decision_effect=NONE execution_effect=NONE"
    )
