"""Venue quantity boundaries (contract venues and base-asset venues).

minQty/lotSize/qtyStep: integer contracts. multiplier: base asset/contract.
minNotional: quote currency (USDT), zero if the exchange has no such rule.
All engine/Position/RiskManager quantities are base asset. Only _round_qty
converts an outgoing base quantity to native contracts.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('boolean is not a quantity')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('invalid quantity metadata')
    return result


def is_base_asset_instrument(info):
    """True for venues whose native order quantity is the base asset (Binance)."""
    return str(info.get('quantityUnit', '') or '').strip().upper() == 'BASE_ASSET'


def quantity_rules(info):
    """Return (multiplier, lot, minimum, min_notional) in integer "step units".

    Contract venues (KuCoin): multiplier = base asset per contract; lotSize and
    minQty are integer contract counts.

    Base-asset venues (Binance USD-M, ``quantityUnit=BASE_ASSET``): the native
    quantity is base asset with a fractional ``qtyStep`` (stepSize) and
    ``minQty``. They are expressed in the same integer-unit model with
    multiplier = qtyStep, lot = 1 and minimum = minQty / qtyStep, so every
    caller computing ``units * multiplier`` gets a stepSize-aligned base qty.
    ``minQty`` must be an exact multiple of ``qtyStep`` or this fails closed.
    """
    if is_base_asset_instrument(info):
        step = number(info['qtyStep'], positive=True)
        minimum = number(info['minQty'], positive=True) / step
        if minimum != minimum.to_integral_value():
            raise ValueError('base-asset minQty must be a multiple of qtyStep')
        notional = number(info.get('minNotional', 0))
        return step, number(1), minimum, notional
    multiplier = number(info['multiplier'], positive=True)
    lot = number(info.get('lotSize', info.get('qtyStep', info['minQty'])), positive=True)
    minimum = number(info['minQty'], positive=True)
    if lot != lot.to_integral_value() or minimum != minimum.to_integral_value():
        raise ValueError('contract venue lotSize/minQty must be integer contracts')
    notional = number(info.get('minNotional', 0))
    return multiplier, lot, minimum, notional


def minimum_base_quantity(info, price):
    """Smallest valid order as base asset, rounded UP to a whole native lot."""
    multiplier, lot, minimum, notional = quantity_rules(info)
    price = number(price, positive=True)
    contracts = max(minimum, notional / (price * multiplier))
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    return float(contracts * multiplier)


def validate_base_quantity(qty, info, price):
    """Validate in base units; do not convert an outgoing qty to contracts."""
    multiplier, lot, minimum, notional = quantity_rules(info)
    qty, price = number(qty, positive=True), number(price, positive=True)
    if qty < minimum * multiplier or qty % (lot * multiplier) != 0:
        raise ValueError('base qty violates minQty or lotSize')
    if qty * price < notional:
        raise ValueError('quote notional below exchange minimum')


def contracts_to_base(contracts, info):
    """Convert an exchange-native contract count into engine base-asset units.

    KuCoin Futures position ``currentQty`` is expressed in contracts. The
    engine, Position and RiskManager contract is base-asset quantity, so every
    inbound position quantity must cross this boundary exactly once.
    """
    if is_base_asset_instrument(info):
        # Base-asset venues have no contract unit; converting would silently
        # rescale exposure. Callers already treat ValueError as fail-closed.
        raise ValueError('base-asset instrument has no contract conversion')
    multiplier, lot, _, _ = quantity_rules(info)
    contracts = number(contracts)
    if contracts != contracts.to_integral_value() or contracts % lot != 0:
        raise ValueError('position contracts violate native lotSize')
    return float(contracts * multiplier)


def base_to_contracts(qty, info):
    """Single outgoing conversion. Floor to a native lot; never grow exposure."""
    if is_base_asset_instrument(info):
        raise ValueError('base-asset instrument has no contract conversion')
    multiplier, lot, minimum, _ = quantity_rules(info)
    qty = number(qty, positive=True)
    contracts = (qty / (multiplier * lot)).to_integral_value(rounding=ROUND_FLOOR) * lot
    if contracts < minimum:
        raise ValueError("quantity below exchange minimum")
    return int(contracts)