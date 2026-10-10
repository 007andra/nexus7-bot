"""Canonical startup reconciliation authority for BGX readiness.

This module is read-only with respect to the exchange. It never submits,
cancels, amends, adopts or closes orders/positions. Its only job is to prove
that startup reconciliation actually happened before the readiness bit may be
set true.

Invariant: DURABLE_STATE_READY_IS_NOT_RECONCILIATION_COMPLETE.
"""
from __future__ import annotations

from bot.conditional_stop_protection import conditional_stop_confirmed
from bot.logger import log


_UNCONFIRMED_PROTECTION = {
    "stop_orders_unconfirmed",
    "invalid_position",
    "position_size_unconfirmed",
}


def _active_items(payload):
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "orders"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return []
    return None


def _live_positions(rows):
    live = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            size = abs(float(row.get("size", 0) or 0))
        except (TypeError, ValueError):
            continue
        if size > 0:
            live.append(row)
    return live


def _registry_snapshot(engine):
    registry = getattr(engine, "orders", None)
    snapshot = getattr(registry, "snapshot", None)
    if not callable(snapshot):
        return None
    records = snapshot() or []
    return [row for row in records if isinstance(row, dict)]


def _order_id(row):
    return str(row.get("orderId") or row.get("id") or row.get("order_id") or "")


def _client_oid(row):
    return str(
        row.get("clientOid")
        or row.get("client_oid")
        or row.get("clientOrderId")
        or ""
    )


async def finalize_initial_reconciliation(engine, *, orders_reconciled: bool) -> bool:
    """Set no authority itself; return True only after all startup proofs pass."""
    engine._initial_reconciliation_complete = False
    evidence = {
        "exchange_truth_loaded": False,
        "positions_reconciled": False,
        "active_orders_reconciled": False,
        "ownership_attribution_classified": False,
        "protection_state_reconciled": False,
    }
    engine._initial_reconciliation_evidence = evidence

    if not bool(getattr(engine, "connected", False)):
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false reason=exchange_not_connected"
        )
        return False

    try:
        rows = await engine.client.get_positions()
    except Exception as exc:
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=positions_read error=%s",
            type(exc).__name__,
        )
        return False
    if not isinstance(rows, list):
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=positions_read reason=malformed_payload"
        )
        return False

    live = _live_positions(rows)
    evidence["exchange_truth_loaded"] = True

    live_symbols = [str(row.get("symbol", "") or "") for row in live]
    if any(not symbol for symbol in live_symbols) or len(live_symbols) != len(set(live_symbols)):
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=position_identity "
            "reason=missing_or_duplicate_symbol"
        )
        return False

    if getattr(engine, "paper_trade", False):
        evidence["positions_reconciled"] = True
        evidence["ownership_attribution_classified"] = True
    else:
        live_set = set(live_symbols)
        recovered = set(getattr(engine, "_recovered_position_symbols", set()) or set())
        external = set(getattr(engine, "_external_position_symbols", set()) or set())
        local = set((getattr(engine, "positions", {}) or {}).keys())

        classified = (
            recovered.isdisjoint(external)
            and recovered.union(external) == live_set
        )
        positions_match = recovered.issubset(local) and external.isdisjoint(local)
        if not classified or not positions_match:
            log.critical(
                "[INITIAL_RECONCILIATION] complete=false stage=position_attribution "
                "live=%s recovered=%s external=%s local=%s",
                len(live_set), len(recovered), len(external), len(local),
            )
            return False
        evidence["positions_reconciled"] = True
        evidence["ownership_attribution_classified"] = True

    # Protection reconciliation is a readback proof, not HTTP-submit success.
    # A deterministically observed unprotected position is reconciled (the
    # protection readiness gate decides whether it may authorize new entries).
    # An unconfirmed read is not reconciliation.
    for row in live:
        try:
            _protected, source = await conditional_stop_confirmed(engine.client, row)
        except Exception as exc:
            log.critical(
                "[INITIAL_RECONCILIATION] complete=false stage=protection_readback "
                "error=%s",
                type(exc).__name__,
            )
            return False
        if source in _UNCONFIRMED_PROTECTION:
            log.critical(
                "[INITIAL_RECONCILIATION] complete=false stage=protection_readback "
                "reason=%s",
                source,
            )
            return False
    evidence["protection_state_reconciled"] = True

    if not orders_reconciled:
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=durable_orders reason=unresolved"
        )
        return False

    # PAPER orders and positions are intentionally isolated from the exchange.
    # Once durable PAPER order state is reconciled, there is no legitimate
    # authenticated exchange-order authority to query.
    if getattr(engine, "paper_trade", False):
        evidence["active_orders_reconciled"] = True
        evidence["ownership_attribution_classified"] = True
        engine._initial_reconciliation_evidence = dict(evidence)
        complete = all(evidence.values())
        log.info(
            "[INITIAL_RECONCILIATION] complete=%s mode=PAPER "
            "exchange_active_orders_required=false durable_state_ready=%s",
            str(complete).lower(),
            str(bool(getattr(engine, "_durable_state_ok", False))).lower(),
        )
        return complete

    registry = _registry_snapshot(engine)
    if registry is None:
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=active_orders reason=registry_unavailable"
        )
        return False

    try:
        get_open_orders = getattr(engine.client, "get_open_orders", None)
        if callable(get_open_orders):
            payload = await get_open_orders()
        else:
            payload = await engine.client._get(
                "/api/v1/orders", {"status": "active"}, auth=True
            )
    except Exception as exc:
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=active_orders_read error=%s",
            type(exc).__name__,
        )
        return False
    active = _active_items(payload)
    if active is None:
        log.critical(
            "[INITIAL_RECONCILIATION] complete=false stage=active_orders_read "
            "reason=malformed_payload"
        )
        return False

    registry_order_ids = {_order_id(row) for row in registry if _order_id(row)}
    registry_client_oids = {_client_oid(row) for row in registry if _client_oid(row)}
    bgx_active = 0
    external_active = 0
    for row in active:
        order_id = _order_id(row)
        client_oid = _client_oid(row)
        claims_bgx = client_oid.startswith("bgx7-")
        exact_registry_match = (
            (bool(order_id) and order_id in registry_order_ids)
            or (bool(client_oid) and client_oid in registry_client_oids)
        )
        if claims_bgx and not exact_registry_match:
            log.critical(
                "[INITIAL_RECONCILIATION] complete=false stage=active_order_attribution "
                "reason=bgx_claim_without_durable_match"
            )
            return False
        if claims_bgx or exact_registry_match:
            bgx_active += 1
        else:
            external_active += 1

    evidence["active_orders_reconciled"] = True
    evidence["ownership_attribution_classified"] = True
    engine._initial_reconciliation_evidence = dict(evidence)
    complete = all(evidence.values())
    log.info(
        "[INITIAL_RECONCILIATION] complete=%s live_positions=%s "
        "active_bgx_orders=%s active_external_orders=%s "
        "durable_state_ready=%s invariant=DURABLE_STATE_READY_IS_NOT_RECONCILIATION_COMPLETE",
        str(complete).lower(),
        len(live),
        bgx_active,
        external_active,
        str(bool(getattr(engine, "_durable_state_ok", False))).lower(),
    )
    return complete
