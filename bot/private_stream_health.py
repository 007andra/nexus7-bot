"""Canonical health authority for the Binance USD-M private/user-data stream.

One ``PrivateStreamHealth`` is owned by each ``BinanceClient``
(``client.private_stream_health``). The private stream loop is its only
writer; the LIVE preflight and PilotGuard gate 14 read it.

Semantics (never "a socket is open" alone):

- DISCONNECTED / CONNECTING: no usable stream.
- CONNECTED: handshake succeeded on the routed ``/private`` path with a
  listenKey that Binance REST confirmed (POST/PUT) within its 60 min validity;
  transport liveness is enforced by protocol ping/pong (a dead socket raises
  and flips the state to DISCONNECTED).
- EVENT_CAPABLE (``check()`` ok): CONNECTED, routed ``/private``, listenKey
  still valid, no auth failure, and an authenticated REST reconciliation has
  completed since this connection was (re)established. No order event is
  required: a flat account legitimately produces none.
- Any (re)connect, disconnect, keepalive failure, handler error or
  ``listenKeyExpired`` sets ``reconcile_required``: events may have been
  missed, so REST truth must be re-read before new exposure.

Freshness uses ``time.monotonic()``; exchange event times are recorded only
as evidence and never used as socket health.
"""
from __future__ import annotations

import threading
import time

PRIVATE_ROUTE = "/private"
LISTEN_KEY_VALIDITY_S = 60 * 60.0
# Binance recommends a keepalive well before expiry; the stream loop sends one
# every 30 min. Treat the key as unusable a little before the hard expiry.
LISTEN_KEY_SAFETY_MARGIN_S = 5 * 60.0

PRIVATE_EVENTS = frozenset({
    "ORDER_TRADE_UPDATE",
    "ACCOUNT_UPDATE",
    "ALGO_UPDATE",
    "TRADE_LITE",
    "ACCOUNT_CONFIG_UPDATE",
    "MARGIN_CALL",
    "CONDITIONAL_ORDER_TRIGGER_REJECT",
    "GRID_UPDATE",
    "STRATEGY_UPDATE",
    "LISTENKEYEXPIRED",
})

DISCONNECTED = "DISCONNECTED"
CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"


def mask_listen_key(key: str) -> str:
    key = str(key or "")
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


class PrivateStreamHealth:
    """Thread-safe, monotonic health state of the private user-data stream."""

    def __init__(self, *, monotonic=time.monotonic):
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self.state = DISCONNECTED
        self.route: str | None = None
        self.connection_epoch = 0
        self.reconciled_epoch = 0
        self.reconcile_required = True
        self.reconcile_reason = "never_reconciled"
        self.auth_failed_reason: str | None = None
        self.listen_key_expires_mono: float | None = None
        self.keepalive_failures = 0
        self.events_total = 0
        self.events_this_connection = 0
        self.last_event: str | None = None
        self.last_event_mono: float | None = None
        self.last_event_exchange_ms: int | None = None
        self.last_disconnect_reason: str | None = None

    # ── writers (private stream loop only) ──────────────────────────────
    def mark_connecting(self) -> None:
        with self._lock:
            self.state = CONNECTING

    def mark_listen_key_confirmed(self) -> None:
        """REST POST/PUT listenKey succeeded: key valid for 60 more minutes."""
        with self._lock:
            self.listen_key_expires_mono = self._monotonic() + LISTEN_KEY_VALIDITY_S
            self.auth_failed_reason = None

    def mark_keepalive_failed(self, reason: str) -> None:
        with self._lock:
            self.keepalive_failures += 1
            self._require(f"keepalive_failed:{reason}")

    def mark_auth_failed(self, reason: str) -> None:
        with self._lock:
            self.auth_failed_reason = str(reason)
            self.state = DISCONNECTED
            self._require("auth_failed")

    def mark_connected(self, route: str) -> int:
        with self._lock:
            self.state = CONNECTED
            self.route = str(route)
            self.connection_epoch += 1
            self.events_this_connection = 0
            # Every (re)connection may follow missed events: require REST
            # reconciliation before the stream is trusted for new exposure.
            self._require("connected" if self.connection_epoch == 1 else "reconnected")
            return self.connection_epoch

    def mark_disconnected(self, reason: str) -> None:
        with self._lock:
            if self.state != DISCONNECTED:
                self.last_disconnect_reason = str(reason)
            self.state = DISCONNECTED
            self._require(f"disconnected:{reason}")

    def require_reconciliation(self, reason: str) -> None:
        with self._lock:
            self._require(reason)

    def record_event(self, event: str, exchange_ms: int | None = None) -> bool:
        kind = str(event or "").upper()
        if kind not in PRIVATE_EVENTS:
            return False
        with self._lock:
            self.events_total += 1
            self.events_this_connection += 1
            self.last_event = kind
            self.last_event_mono = self._monotonic()
            try:
                self.last_event_exchange_ms = int(exchange_ms) if exchange_ms else None
            except (TypeError, ValueError):
                self.last_event_exchange_ms = None
            return True

    def mark_reconciled(self, epoch: int) -> bool:
        """REST reconciliation completed for connection ``epoch``."""
        with self._lock:
            if self.state != CONNECTED or int(epoch) != self.connection_epoch:
                return False
            self.reconcile_required = False
            self.reconcile_reason = "reconciled"
            self.reconciled_epoch = self.connection_epoch
            return True

    def _require(self, reason: str) -> None:
        self.reconcile_required = True
        self.reconcile_reason = str(reason)

    # ── readers ─────────────────────────────────────────────────────────
    def check(self) -> tuple[bool, str]:
        with self._lock:
            if self.auth_failed_reason:
                return False, "auth_failed"
            if self.state != CONNECTED:
                return False, "disconnected" if self.state == DISCONNECTED else "connecting"
            if self.route != PRIVATE_ROUTE:
                return False, "unrouted"
            expires = self.listen_key_expires_mono
            if expires is None or self._monotonic() > expires - LISTEN_KEY_SAFETY_MARGIN_S:
                return False, "listen_key_unconfirmed"
            if self.reconcile_required:
                return False, "reconcile_required"
            return True, "event_capable"

    def snapshot(self) -> dict:
        ok, reason = self.check()
        with self._lock:
            now = self._monotonic()
            return {
                "state": self.state,
                "route": self.route,
                "ok": ok,
                "reason": reason,
                "connection_epoch": self.connection_epoch,
                "reconciled_epoch": self.reconciled_epoch,
                "reconcile_required": self.reconcile_required,
                "reconcile_reason": self.reconcile_reason,
                "auth_failed": self.auth_failed_reason,
                "keepalive_failures": self.keepalive_failures,
                "events_total": self.events_total,
                "events_this_connection": self.events_this_connection,
                "last_event": self.last_event,
                "last_event_age_s": (
                    None if self.last_event_mono is None else now - self.last_event_mono
                ),
                "last_event_exchange_ms": self.last_event_exchange_ms,
                "listen_key_ttl_s": (
                    None if self.listen_key_expires_mono is None
                    else self.listen_key_expires_mono - now
                ),
            }
