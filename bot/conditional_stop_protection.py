"""Read-only recognition of KuCoin Futures conditional protective stops.

This module never creates, modifies, cancels, or adopts an exchange order.
It only reads currently untriggered stop orders and decides whether they
fully protect an already-open exchange position.

Fail-closed rules:
- inline position stopLoss > 0 remains authoritative;
- otherwise the KuCoin /api/v1/stopOrders read must succeed;
- stop must be active/untriggered, on the same symbol, opposite the position;
- it must be reduce-only or a close-order;
- trigger price must be on the protective side of the position reference price;
- a reduce-only stop must cover the full position size after both quantities
  are normalized to engine base-asset units.
"""

import math

from bot.quantity import contracts_to_base


def _finite_positive(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) and out > 0 else 0.0


def inline_stop_confirmed(position: dict) -> bool:
    if not isinstance(position, dict):
        return False
    return _finite_positive(position.get("stopLoss", position.get("stop_loss", 0))) > 0


def _position_direction(position: dict) -> str:
    raw = str(position.get("side", "") or "").strip().lower()
    if raw in {"buy", "long"}:
        return "long"
    if raw in {"sell", "short"}:
        return "short"
    try:
        qty = float(position.get("currentQty", position.get("size", 0)) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return ""


def _reference_price(position: dict) -> float:
    for key in ("markPrice", "mark_price", "entryPrice", "avgEntryPrice"):
        px = _finite_positive(position.get(key))
        if px > 0:
            return px
    return 0.0


def _order_active(order: dict) -> bool:
    if not isinstance(order, dict):
        return False
    if order.get("stopTriggered") is True:
        return False
    if order.get("isActive") is False:
        return False
    status = str(order.get("status", "") or "").strip().lower()
    if status in {"done", "cancelled", "canceled", "filled", "triggered"}:
        return False
    return True


def _normalized_symbol(raw: str) -> str:
    sym = str(raw or "").upper()
    if sym == "XBTUSDTM":
        return "BTCUSDT"
    if sym.endswith("USDTM"):
        return f"{sym[:-5]}USDT"
    return sym


def _instrument_info(client, symbol: str):
    """Return canonical KuCoin instrument metadata without exchange I/O."""
    standard = _normalized_symbol(symbol)
    getter = getattr(client, "get_instruments", None)
    if callable(getter):
        try:
            instruments = getter() or {}
        except Exception:
            instruments = {}
        if isinstance(instruments, dict):
            info = instruments.get(standard)
            if isinstance(info, dict):
                return info
    instruments = getattr(client, "_instruments", None)
    if isinstance(instruments, dict):
        info = instruments.get(standard)
        if isinstance(info, dict):
            return info
    return None


def _to_base_size(value, unit: str, info) -> float:
    """Normalize one quantity to base units, failing closed on ambiguity."""
    amount = _finite_positive(value)
    if amount <= 0:
        return 0.0
    if str(unit or "").strip().upper() == "BASE_ASSET":
        return amount
    if not isinstance(info, dict):
        return 0.0
    try:
        return _finite_positive(contracts_to_base(amount, info))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _protective_order(
    order: dict,
    position: dict,
    symbol: str,
    instrument_info,
) -> tuple[bool, bool, float]:
    """Return (qualifies, closes_full_position, covered_base_size)."""
    if not _order_active(order):
        return False, False, 0.0
    if _normalized_symbol(order.get("symbol")) != _normalized_symbol(symbol):
        return False, False, 0.0

    direction = _position_direction(position)
    side = str(order.get("side", "") or "").strip().lower()
    if not direction or side not in {"buy", "sell"}:
        return False, False, 0.0
    if direction == "long" and side != "sell":
        return False, False, 0.0
    if direction == "short" and side != "buy":
        return False, False, 0.0

    stop_price = _finite_positive(order.get("stopPrice", order.get("stop_price", 0)))
    reference = _reference_price(position)
    if stop_price <= 0 or reference <= 0:
        return False, False, 0.0
    if direction == "long" and stop_price >= reference:
        return False, False, 0.0
    if direction == "short" and stop_price <= reference:
        return False, False, 0.0

    close_order = order.get("closeOrder") is True
    reduce_only = order.get("reduceOnly") is True
    if not close_order and not reduce_only:
        return False, False, 0.0

    # KuCoin closeOrder means close the entire side and therefore does not
    # depend on a native size field. No unit metadata/conversion is needed.
    if close_order:
        return True, True, float("inf")

    covered_base = _to_base_size(
        order.get("size", order.get("qty", 0)),
        order.get("sizeUnit", "CONTRACTS"),
        instrument_info,
    )
    if covered_base <= 0:
        return False, False, 0.0
    return True, False, covered_base


async def read_stop_orders(client, symbol: str):
    """Read untriggered stop orders. Returns None when unconfirmed."""
    getter = getattr(client, "get_stop_orders", None)
    if not callable(getter):
        return None
    try:
        data = await getter(symbol)
    except Exception:
        return None

    if data is None:
        return None
    if isinstance(data, dict):
        items = data.get("items")
        if items is None:
            items = data.get("data") if isinstance(data.get("data"), list) else None
        return list(items) if isinstance(items, list) else None
    if isinstance(data, list):
        return list(data)
    return None


async def conditional_stop_confirmed(client, position: dict) -> tuple[bool, str]:
    """Return (protected, evidence) without mutating the exchange."""
    if inline_stop_confirmed(position):
        return True, "inline_stop"
    if not isinstance(position, dict):
        return False, "invalid_position"

    symbol = str(position.get("symbol", "") or "")
    if not symbol:
        return False, "invalid_position"

    # Read/evaluate full closeOrder protection first. A valid closeOrder closes
    # the entire position side by exchange semantics, so instrument quantity
    # metadata is irrelevant. Only quantitative reduceOnly coverage requires
    # contract→base normalization.
    orders = await read_stop_orders(client, symbol)
    if orders is None:
        return False, "stop_orders_unconfirmed"

    info = _instrument_info(client, symbol)
    covered = 0.0
    for order in orders:
        qualifies, full_close, amount = _protective_order(
            order, position, symbol, info
        )
        if not qualifies:
            continue
        if full_close:
            return True, "conditional_close_order"
        covered += amount

    if covered <= 0:
        return False, "no_full_protective_stop"

    position_size = _to_base_size(
        position.get("size", 0),
        position.get("sizeUnit", "CONTRACTS"),
        info,
    )
    if position_size <= 0:
        return False, "position_size_unconfirmed"

    if covered + max(1e-12, position_size * 1e-9) >= position_size:
        return True, "conditional_reduce_only"
    return False, "no_full_protective_stop"
