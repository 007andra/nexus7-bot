"""Durable fail-closed submission cap for the controlled LIVE pilot.

BGX-PREDISPATCH-001 adds durable dispatch provenance without changing the
cumulative two-token durable budget. Tokens are never decremented. Production
budget authorization is performed at the final transport boundary, after all
other deterministic gates and immediately before the monotonic dispatch marker
and HTTP POST.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from bot.logger import log
from bot.critical_state import critical_state

_LOCK = asyncio.Lock()
_STATE_VERSION = 1
_PROVENANCE_SOURCE = "BGX_PREDISPATCH_001"


def _session_id() -> str:
    raw = (
        os.environ.get("PILOT_SESSION_ID")
        or os.environ.get("RAILWAY_DEPLOYMENT_ID")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "local"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _state_key() -> str:
    return f"pilot_submission_budget_v1:{_session_id()}"


def _order_token(symbol: str, side: str, qty, idem_key: str | None) -> str:
    raw = f"{symbol}|{side}|{qty}|{idem_key or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _decode(value) -> list[str]:
    if value is None:
        return []
    try:
        obj = json.loads(str(value))
        if not isinstance(obj, dict) or obj.get("version") != _STATE_VERSION:
            raise ValueError("invalid durable pilot state")
        ids = obj.get("order_tokens")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ValueError("invalid durable pilot order token list")
        return list(dict.fromkeys(ids))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("durable pilot state malformed") from exc


def _encode(tokens: list[str]) -> str:
    return json.dumps({
        "version": _STATE_VERSION,
        "order_tokens": tokens,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, sort_keys=True, separators=(",", ":"))


async def _reserve_db(token: str, limit: int) -> tuple[bool, int]:
    """Atomically reserve one cumulative unique submission token."""
    from bot import database as db
    critical_state.assert_available_for_new_risk()
    if limit <= 0:
        raise RuntimeError("invalid pilot submission limit")
    conn = getattr(db, "_conn", None)
    if conn is None:
        raise db.PersistenceError("pilot reservation blocked: database unavailable")
    if db.configured_postgres_unavailable():
        raise db.PersistenceError("pilot reservation blocked: PostgreSQL unavailable")
    key = _state_key()
    async with _LOCK:
        async with db._io_lock:
            if getattr(db, "_is_pg", False):
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
                    row = await conn.fetchrow("SELECT value FROM key_value WHERE key=$1 FOR UPDATE", key)
                    tokens = _decode(row[0] if row else None)
                    if token in tokens:
                        return True, len(tokens)
                    if len(tokens) >= limit:
                        return False, len(tokens)
                    tokens.append(token)
                    await conn.execute(
                        "INSERT INTO key_value (key,value,updated_at) VALUES ($1,$2,$3) "
                        "ON CONFLICT (key) DO UPDATE SET value=$2,updated_at=$3",
                        key, _encode(tokens), datetime.now(timezone.utc).isoformat(),
                    )
                    return True, len(tokens)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                async with conn.execute("SELECT value FROM key_value WHERE key=?", (key,)) as cur:
                    row = await cur.fetchone()
                tokens = _decode(row[0] if row else None)
                if token in tokens:
                    await conn.commit(); return True, len(tokens)
                if len(tokens) >= limit:
                    await conn.commit(); return False, len(tokens)
                tokens.append(token)
                await conn.execute(
                    "INSERT OR REPLACE INTO key_value (key,value,updated_at) VALUES (?,?,?)",
                    (key, _encode(tokens), datetime.now(timezone.utc).isoformat()),
                )
                await conn.commit(); return True, len(tokens)
            except Exception:
                await conn.rollback(); raise


async def reserve_submission(symbol: str, side: str, qty, idem_key: str | None, limit: int) -> tuple[bool, int]:
    return await _reserve_db(_order_token(symbol, side, qty, idem_key), limit)


def _provenance(order) -> tuple[bool | None, str]:
    attempted = None
    abort_reason = ""
    for event in list(getattr(order, "history", []) or []):
        if not isinstance(event, (list, tuple)) or len(event) < 4 or not isinstance(event[3], dict):
            continue
        info = event[3]
        if info.get("dispatch_attempted") is True:
            attempted = True
        if attempted is not True and info.get("dispatch_attempted") is False:
            attempted = False
        if info.get("predispatch_abort_reason"):
            abort_reason = str(info["predispatch_abort_reason"])
    return attempted, abort_reason


def _record_same_state(order, **info) -> None:
    now = time.time(); state = order.state.value
    order.updated_at = now; order.last_source = _PROVENANCE_SOURCE
    order.history.append((now, state, state, {"source": _PROVENANCE_SOURCE, **info}))


def _managed_order_by_oid(client, oid: str):
    registry = getattr(client, "_order_registry", None)
    return registry.get(oid) if registry is not None and oid else None


def _managed_order_for(client, symbol: str, side: str, qty, idem_key):
    registry = getattr(client, "_order_registry", None)
    if registry is None: return None
    try: oid = client.build_client_oid(symbol, side, qty, idem_key)
    except Exception: return None
    return registry.get(oid)


async def _persist_registry(client, reason: str, *, strict: bool = True) -> bool:
    engine = getattr(client, "_engine", None)
    if engine is None: return not strict
    from bot import durable_execution as durable
    return bool(await durable.persist_orders(engine, reason, strict=strict))


async def _fail_order_predispatch(client, order, reason: str) -> bool:
    from bot.order_state import OrderState, InvalidTransition
    if order is None: return False
    attempted, _ = _provenance(order)
    if attempted is True: return False
    if order.is_terminal: return order.state == OrderState.FAILED
    _record_same_state(order, dispatch_attempted=False, predispatch_abort_reason=reason, exchange_dispatch="NONE")
    try: order.transition(OrderState.FAILED, source=reason)
    except InvalidTransition:
        if order.state != OrderState.FAILED: raise
    await _persist_registry(client, "proven_not_dispatched_failed", strict=True)
    log.critical("[PREDISPATCH_ABORT] clientOid=%s symbol=%s state=FAILED dispatch_attempted=false reason=%s", order.client_oid, order.symbol, reason)
    return True


async def _fail_predispatch(client, symbol: str, side: str, qty, idem_key, reason: str) -> bool:
    return await _fail_order_predispatch(client, _managed_order_for(client, symbol, side, qty, idem_key), reason)


async def _mark_dispatch_attempted(client, body: dict) -> None:
    oid = str(body.get("clientOid") or "") if isinstance(body, dict) else ""
    if not oid.startswith("bgx7-"): return
    order = _managed_order_by_oid(client, oid)
    if order is None: raise RuntimeError("managed order missing at dispatch boundary")
    attempted, _ = _provenance(order)
    if attempted is True: return
    _record_same_state(order, dispatch_attempted=True, exchange_dispatch="ATTEMPTED")
    if not await _persist_registry(client, "dispatch_boundary", strict=True):
        raise RuntimeError("dispatch provenance persistence failed")


def _install_pilot_session_boundary() -> None:
    from bot.pilot import PilotGuard, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION
    if getattr(PilotGuard, "_bgx_predispatch_session_boundary_installed", False): return

    def check_submission(self, symbol: str) -> bool:
        if not self.enabled: return True
        with self._submission_lock:
            return self.state.new_order_submissions_this_session < MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION

    def commit_submission(self, symbol: str, order_token: str) -> bool:
        if not self.enabled: return True
        with self._submission_lock:
            committed = getattr(self, "_bgx_committed_submission_tokens", None)
            if committed is None:
                committed = set(); self._bgx_committed_submission_tokens = committed
            if order_token in committed: return True
            if self.state.new_order_submissions_this_session >= MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION: return False
            committed.add(order_token); self.state.new_order_submissions_this_session += 1
            if self.state.first_order_ts == 0.0: self.state.first_order_ts = time.time()
        log.critical("[PILOT] symbol=%s submission_committed boundary=HTTP_POST", symbol)
        return True

    def rollback_submission(self, order_token: str) -> bool:
        """Rollback only a locally committed slot proven not dispatched."""
        if not self.enabled: return True
        with self._submission_lock:
            committed = getattr(self, "_bgx_committed_submission_tokens", set())
            if order_token not in committed: return True
            committed.remove(order_token)
            if self.state.new_order_submissions_this_session > 0:
                self.state.new_order_submissions_this_session -= 1
            return True

    PilotGuard.reserve_submission = check_submission
    PilotGuard.commit_submission = commit_submission
    PilotGuard.rollback_submission = rollback_submission
    PilotGuard._bgx_predispatch_session_boundary_installed = True


def _install_reconciler_provenance() -> None:
    from bot import durable_live_reconciliation as reconciler
    from bot import durable_execution as durable
    from bot.order_state import OrderState, InvalidTransition
    if getattr(reconciler, "_bgx_predispatch_001_installed", False): return
    original = reconciler.reconcile_pending
    async def wrapped(engine, *args, **kwargs):
        registry = getattr(engine, "orders", None); reader = getattr(registry, "pending_orders", None); changed = False
        if callable(reader):
            for order in list(reader() or []):
                attempted, reason = _provenance(order)
                if attempted is False and reason and not order.is_terminal:
                    try: order.transition(OrderState.FAILED, source=reason)
                    except InvalidTransition:
                        if order.state != OrderState.FAILED: raise
                    changed = True
        if changed and not await durable.persist_orders(engine, "predispatch_abort_reconcile", strict=True):
            durable._block(engine, "orders"); return False
        return await original(engine, *args, **kwargs)
    reconciler.reconcile_pending = wrapped
    reconciler._bgx_predispatch_001_installed = True


def install(KuCoinClient, log_obj=log) -> None:
    if getattr(KuCoinClient, "_pilot_durable_submission_counter_installed", False): return
    _install_pilot_session_boundary(); _install_reconciler_provenance()
    original_place_order = KuCoinClient.place_order
    original_fenced_entry_post = getattr(KuCoinClient, "_fenced_entry_post", None)

    async def legacy_place_order(self, *args, **kwargs):
        if bool(kwargs.get("reduce_only", False)) or not bool(kwargs.get("single_submission", False)):
            return await original_place_order(self, *args, **kwargs)
        from bot.pilot import MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION
        symbol = kwargs.get("symbol") or (args[0] if len(args) > 0 else "")
        side = kwargs.get("side") or (args[1] if len(args) > 1 else "")
        qty = kwargs.get("qty") if "qty" in kwargs else (args[2] if len(args) > 2 else 0)
        idem = kwargs.get("idem_key")
        try: allowed, _ = await reserve_submission(str(symbol), str(side), qty, idem, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION)
        except Exception: return {}
        if not allowed: return {}
        return await original_place_order(self, *args, **kwargs)

    if original_fenced_entry_post is None:
        KuCoinClient.place_order = legacy_place_order
        KuCoinClient._pilot_durable_submission_counter_installed = True
        return

    async def place_order_with_boundary_budget(self, *args, **kwargs):
        reduce_only = bool(kwargs.get("reduce_only", False))
        single_submission = bool(kwargs.get("single_submission", False))
        if reduce_only or not single_submission:
            return await original_place_order(self, *args, **kwargs)
        symbol = kwargs.get("symbol") or (args[0] if len(args) > 0 else "")
        side = kwargs.get("side") or (args[1] if len(args) > 1 else "")
        qty = kwargs.get("qty") if "qty" in kwargs else (args[2] if len(args) > 2 else 0)
        idem = kwargs.get("idem_key")
        try:
            oid = self.build_client_oid(str(symbol), str(side), qty, idem)
        except Exception:
            oid = ""
        pending = getattr(self, "_bgx_pilot_boundary_context", None)
        if pending is None:
            pending = {}; self._bgx_pilot_boundary_context = pending
        if oid:
            pending[oid] = (str(symbol), str(side), qty, idem)
        try:
            return await original_place_order(self, *args, **kwargs)
        finally:
            if oid: pending.pop(oid, None)

    @asynccontextmanager
    async def fenced_entry_post_with_provenance(self, endpoint, body, url, **kwargs):
        detector = getattr(self, "_is_new_risk_request", None)
        if callable(detector):
            is_new_risk = bool(detector(endpoint, body))
        else:
            is_new_risk = (
                endpoint in ("/api/v1/orders", "/api/v1/st-orders")
                and body.get("reduceOnly") is not True
                and body.get("closeOrder") is not True
            )
        if not is_new_risk:
            async with original_fenced_entry_post(self, endpoint, body, url, **kwargs) as response:
                yield response
            return
        from bot.execution_ownership import validate_execution_ownership
        from bot.pilot import MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION
        ownership = getattr(self, "_execution_ownership", None)
        if ownership is None: raise RuntimeError("OPEN_NEW_RISK missing execution ownership at transport boundary")
        await validate_execution_ownership(ownership)
        oid = str(body.get("clientOid") or body.get("newClientOrderId") or ""); order = _managed_order_by_oid(self, oid)
        context = getattr(self, "_bgx_pilot_boundary_context", {}).get(oid)
        if context is None:
            symbol = str(body.get("symbol") or getattr(order, "symbol", "")); side = str(body.get("side") or ""); qty = body.get("size", body.get("quantity")); idem = oid
        else:
            symbol, side, qty, idem = context
        try:
            post_context = self._entry_safe_post(endpoint, body, url, **kwargs)
        except Exception:
            await _fail_order_predispatch(self, order, "PRE_DISPATCH_TRANSPORT_GATE_DENIED")
            raise
        engine = getattr(self, "_engine", None); pilot = getattr(engine, "pilot", None) if engine is not None else None
        session_committed = False
        if pilot is not None:
            commit = getattr(pilot, "commit_submission", None)
            if callable(commit):
                session_committed = bool(commit(symbol, oid))
                if not session_committed:
                    await _fail_order_predispatch(self, order, "PRE_DISPATCH_PILOT_SESSION_DENIED")
                    raise RuntimeError("PILOT session submission cap reached at POST boundary")
        try:
            allowed, count = await reserve_submission(symbol, side, qty, idem, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION)
        except Exception:
            if session_committed and pilot is not None: pilot.rollback_submission(oid)
            await _fail_order_predispatch(self, order, "PRE_DISPATCH_PILOT_BUDGET_ERROR")
            raise
        if not allowed:
            if session_committed and pilot is not None: pilot.rollback_submission(oid)
            await _fail_order_predispatch(self, order, "PRE_DISPATCH_PILOT_BUDGET_DENIED")
            log_obj.critical("[PILOT_DURABLE_COUNTER] symbol=%s result=BLOCK reserved=%s/%s exchange_dispatch=NONE", symbol, count, MAX_NEW_ORDER_SUBMISSIONS_PER_SESSION)
            raise RuntimeError("durable PILOT submission budget exhausted")
        # No intentional authorization gate is permitted below this line.
        await _mark_dispatch_attempted(self, body)
        async with post_context as response:
            yield response

    KuCoinClient.place_order = place_order_with_boundary_budget
    KuCoinClient._fenced_entry_post = fenced_entry_post_with_provenance
    KuCoinClient._pilot_durable_submission_counter_installed = True
    log_obj.info("[PILOT_DURABLE_COUNTER] installed: final-boundary cumulative budget + dispatch provenance")
