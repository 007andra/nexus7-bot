"""P1-DEPLOY-1 — read-only Binance account inventory at startup.

Answers one question before the protection-lineage release: does the account
hold ANY position, normal open order or open algo (conditional) order that the
new durable algo -> lineage registry would have to inherit?

Exactly three authenticated GETs, all account-wide (no symbol parameter, so no
symbol can be missed): ``/fapi/v3/positionRisk``, ``/fapi/v1/openOrders`` and
``/fapi/v1/openAlgoOrders``. Rows are grouped per configured symbol and any
symbol outside the configured universe is reported too. A failed or malformed
read makes the inventory INCOMPLETE — never ZERO.

execution_effect=NONE decision_effect=NONE risk_effect=NONE score_effect=NONE:
no POST/PUT/DELETE, no order placement or cancel, nothing stored on the engine
except the diagnostic result. Identifiers are logged as a short prefix plus a
hash; credentials, signatures and headers are never logged.
"""
from __future__ import annotations

import hashlib
import math

ENDPOINTS = ("/fapi/v3/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders")
BGX_PREFIXES = ("bgx7-", "bgx-stop-")


def _symbol(value) -> str:
    return str(value or "").upper().strip()


def _ident(value) -> str:
    text = str(value or "")
    if not text:
        return "-"
    return f"{text[:5]}…#{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def _rows(payload, endpoint: str) -> list:
    if endpoint.endswith("openAlgoOrders") and isinstance(payload, dict):
        payload = payload.get("orders")
    if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
        raise ValueError(f"malformed_payload:{endpoint}")
    return payload


def _position_qty(row) -> float:
    qty = float(row["positionAmt"])
    if not math.isfinite(qty):
        raise ValueError("malformed_position_amount")
    return qty


def classify_algo(row: dict, has_position: bool) -> str:
    client_id = str(row.get("clientAlgoId") or "")
    if has_position:
        return "CURRENT_POSITION_PROTECTION" if client_id.startswith(BGX_PREFIXES) else "EXTERNAL_OR_MANUAL"
    if client_id.startswith(BGX_PREFIXES):
        return "LEGACY_BGX_CANDIDATE"
    if client_id:
        return "EXTERNAL_OR_MANUAL"
    return "UNKNOWN"


async def read_legacy_algo_inventory(client, symbols) -> dict:
    """Read-only. Returns {'complete': bool, 'symbols': {...}, 'totals': {...}}."""
    configured = [_symbol(s) for s in symbols if _symbol(s)]
    try:
        positions = _rows(await client._get(ENDPOINTS[0], auth=True), ENDPOINTS[0])
        normal = _rows(await client._get(ENDPOINTS[1], auth=True), ENDPOINTS[1])
        algos = _rows(await client._get(ENDPOINTS[2], auth=True), ENDPOINTS[2])
        live = {}
        for row in positions:
            if "positionAmt" not in row:
                raise ValueError("malformed_position_row")
            qty = _position_qty(row)
            if qty != 0:
                live[_symbol(row.get("symbol"))] = qty
    except Exception as exc:
        return {"complete": False, "reason": str(exc) if isinstance(exc, ValueError)
                else type(exc).__name__, "symbols": {}, "totals": {}}

    universe = list(dict.fromkeys(configured + sorted(
        {_symbol(r.get("symbol")) for r in normal + algos} | set(live))))
    per_symbol = {}
    totals = {"POSITIONS_TOTAL": 0, "NORMAL_OPEN_TOTAL": 0, "ALGO_OPEN_TOTAL": 0,
              "BGX7_TOTAL": 0, "EXTERNAL_TOTAL": 0, "UNKNOWN_TOTAL": 0,
              "LEGACY_BGX_CANDIDATE_TOTAL": 0, "CURRENT_POSITION_PROTECTION_TOTAL": 0}
    for sym in universe:
        qty = live.get(sym, 0.0)
        sym_normal = [r for r in normal if _symbol(r.get("symbol")) == sym]
        sym_algos = [r for r in algos if _symbol(r.get("symbol")) == sym]
        details = []
        for row in sym_algos:
            cls = classify_algo(row, qty != 0)
            details.append({
                "client_algo_id": _ident(row.get("clientAlgoId")),
                "algo_id": _ident(row.get("algoId")),
                "type": str(row.get("orderType") or row.get("type") or "-"),
                "side": str(row.get("side") or "-"),
                "close_position": bool(row.get("closePosition", False)),
                "reduce_only": bool(row.get("reduceOnly", False)),
                "trigger_price": str(row.get("triggerPrice") or "-"),
                "status": str(row.get("algoStatus") or row.get("status") or "-"),
                "classification": cls,
            })
            if str(row.get("clientAlgoId") or "").startswith(BGX_PREFIXES):
                totals["BGX7_TOTAL"] += 1
            if cls == "EXTERNAL_OR_MANUAL":
                totals["EXTERNAL_TOTAL"] += 1
            elif cls == "UNKNOWN":
                totals["UNKNOWN_TOTAL"] += 1
            elif cls == "LEGACY_BGX_CANDIDATE":
                totals["LEGACY_BGX_CANDIDATE_TOTAL"] += 1
            else:
                totals["CURRENT_POSITION_PROTECTION_TOTAL"] += 1
        totals["POSITIONS_TOTAL"] += int(qty != 0)
        totals["NORMAL_OPEN_TOTAL"] += len(sym_normal)
        totals["ALGO_OPEN_TOTAL"] += len(sym_algos)
        per_symbol[sym] = {
            "configured": sym in configured,
            "has_position": qty != 0,
            "position_side": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT"),
            "position_qty": f"{abs(qty):.8g}",
            "normal_open_count": len(sym_normal),
            "algo_open_count": len(sym_algos),
            "algos": details,
        }
    return {"complete": True, "reason": "", "symbols": per_symbol, "totals": totals}


def log_inventory(result: dict, log) -> None:
    if not result.get("complete"):
        log.critical("[BINANCE_LEGACY_ALGO_INVENTORY] result=INCOMPLETE reason=%s "
                     "inventory_zero=false execution_effect=NONE", result.get("reason") or "unknown")
        return
    for sym, row in result["symbols"].items():
        log.warning(
            "[BINANCE_LEGACY_ALGO_INVENTORY] symbol=%s configured=%s has_position=%s position_side=%s "
            "position_qty=%s normal_open_count=%s algo_open_count=%s total=%s execution_effect=NONE",
            sym, str(row["configured"]).lower(), str(row["has_position"]).lower(), row["position_side"],
            row["position_qty"], row["normal_open_count"], row["algo_open_count"],
            row["normal_open_count"] + row["algo_open_count"])
        for algo in row["algos"]:
            log.warning(
                "[BINANCE_LEGACY_ALGO_INVENTORY] symbol=%s algo client_algo_id=%s algo_id=%s type=%s "
                "side=%s close_position=%s reduce_only=%s trigger_price=%s status=%s "
                "classification=%s execution_effect=NONE",
                sym, algo["client_algo_id"], algo["algo_id"], algo["type"], algo["side"],
                str(algo["close_position"]).lower(), str(algo["reduce_only"]).lower(),
                algo["trigger_price"], algo["status"], algo["classification"])
    totals = result["totals"]
    zero = totals["POSITIONS_TOTAL"] == totals["NORMAL_OPEN_TOTAL"] == totals["ALGO_OPEN_TOTAL"] == 0
    log.warning(
        "[BINANCE_LEGACY_ALGO_INVENTORY] result=%s inventory_complete=true scope=ACCOUNT_WIDE "
        "symbols=%s %s execution_effect=NONE decision_effect=NONE risk_effect=NONE score_effect=NONE",
        "ZERO" if zero else "PRESENT", len(result["symbols"]),
        " ".join(f"{k}={v}" for k, v in totals.items()))


def install(TradingEngine, log) -> None:
    """Run the inventory ONCE after a successful LIVE connect (auth, instruments
    and account reads available). Never raises into the startup path."""
    if getattr(TradingEngine, "_binance_legacy_algo_inventory_installed", False):
        return
    original_connect = TradingEngine._connect

    async def _connect_with_inventory(self, *args, **kwargs):
        result = await original_connect(self, *args, **kwargs)
        if (getattr(self, "paper_trade", False) or not getattr(self, "connected", False)
                or getattr(self, "_binance_legacy_algo_inventory_done", False)):
            return result
        self._binance_legacy_algo_inventory_done = True
        try:
            from bot.config import cfg
            client = getattr(self, "client", None)
            client = getattr(client, "_client", client)
            inventory = await read_legacy_algo_inventory(client, list(getattr(cfg, "SYMBOLS", []) or []))
        except Exception as exc:
            inventory = {"complete": False, "reason": type(exc).__name__}
        self._binance_legacy_algo_inventory = inventory
        try:
            log_inventory(inventory, log)
        except Exception as exc:
            log.critical("[BINANCE_LEGACY_ALGO_INVENTORY] result=INCOMPLETE reason=log_%s "
                         "execution_effect=NONE", type(exc).__name__)
        return result

    TradingEngine._connect = _connect_with_inventory
    TradingEngine._binance_legacy_algo_inventory_installed = True
    log.warning("[BINANCE_LEGACY_ALGO_INVENTORY] installed=true reads=positionRisk,openOrders,"
                "openAlgoOrders scope=ACCOUNT_WIDE mutations=none execution_effect=NONE")
