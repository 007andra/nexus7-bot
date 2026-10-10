"""Read-only exchange account balance semantics shared by SHADOW and LIVE paths.

Capital health is based on accountEquity. New-order affordability prefers
availableMargin when KuCoin exposes it (the current cross-margin buying-power
field) and falls back to availableBalance for legacy/isolated responses. This
module performs no exchange mutations.
"""
import math

_FIELDS = (
    "accountEquity",
    "marginBalance",
    "availableBalance",
    "availableMargin",
    "unrealisedPNL",
    "positionMargin",
    "orderMargin",
    "frozenFunds",
)


def _finite_number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} boolean")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} nonfinite")
    return out


async def read_account_state(client):
    """Return validated read-only equity/collateral from the active exchange."""
    venue_reader = getattr(client, "get_account_state", None)
    if callable(venue_reader):
        data = await venue_reader()
    else:
        # KuCoin compatibility path for the existing production adapter.
        data = await client._get(
            "/api/v1/account-overview", {"currency": "USDT"}, auth=True
        )
    if not isinstance(data, dict):
        raise RuntimeError("account overview unavailable")

    raw_equity = data.get("equity", data.get("accountEquity"))
    equity = _finite_number(raw_equity, "equity")

    if data.get("available") not in (None, ""):
        available = _finite_number(data.get("available"), "available")
        available_source = data.get("available_source") or "available"
    elif data.get("availableMargin") not in (None, ""):
        available = _finite_number(data.get("availableMargin"), "availableMargin")
        available_source = "availableMargin"
    else:
        available = _finite_number(data.get("availableBalance"), "availableBalance")
        available_source = "availableBalance"

    if equity < 0 or available < 0:
        raise RuntimeError("negative account balance")

    state = {key: data.get(key) for key in _FIELDS}
    state["equity"] = equity
    state["available"] = available
    state["available_source"] = available_source
    state["currency"] = data.get("currency") or "USDT"
    return state


def update_risk_from_equity(risk, equity):
    """Initialize/update RiskManager from account equity only."""
    if not getattr(risk, "_ready", False):
        risk.init(equity)
    else:
        risk.update(equity)
    return risk


def collateral_allows(qty, entry, available, leverage, fee_rate):
    """Conservative free-collateral check for a prospective order."""
    if qty <= 0 or entry <= 0 or available <= 0 or leverage <= 0:
        return False, float("inf")
    required = qty * entry * (1.0 / leverage + fee_rate)
    return required <= available, required
