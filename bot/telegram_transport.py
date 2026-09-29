"""Canonical Telegram delivery transport (observability only).

Every Telegram emitter (async ``notifier.notify`` and the two background audit
workers in ``bot.logger`` / ``bot.funnel_metrics``) delivers through this
module, so one policy governs timeouts, retries, classification and health.

Trading independence: nothing here reads or writes trading, risk, sizing,
order or exchange state. A delivery never raises; it returns a
``DeliveryResult`` whose truthiness is "Telegram confirmed ``ok: true``".

Production evidence (2026-09-27, Railway ams): ``api.telegram.org`` resolves to
IPv4 and IPv6, but the container has no public IPv6 route. ``urllib`` tried
IPv4 first, the SYNs went unanswered until the timeout, then the IPv6 address
failed instantly with ``ENETUNREACH`` and Python raised that *last* error, so
connect timeouts were reported as "Network is unreachable". Resolving IPv4
only (``NEXUS_TELEGRAM_IPV4_ONLY``, default true) removes the dead IPv6 attempt
and makes the classification truthful.

Never logged: token, request URL, chat id (only a masked suffix), message text.
"""
from __future__ import annotations

import asyncio
import errno
import http.client
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass

# ── error classes ─────────────────────────────────────────────────────────
NETWORK_UNREACHABLE = "NETWORK_UNREACHABLE"
DNS_ERROR = "DNS_ERROR"
CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
CONNECTION_REFUSED = "CONNECTION_REFUSED"
READ_TIMEOUT = "READ_TIMEOUT"
CONNECTION_DROPPED = "CONNECTION_DROPPED"
RATE_LIMITED = "RATE_LIMITED"
SERVER_ERROR = "SERVER_ERROR"
CLIENT_ERROR = "CLIENT_ERROR"
INVALID_RESPONSE = "INVALID_RESPONSE"
UNKNOWN = "UNKNOWN"
OK = "OK"

# Failed before any byte of the request could reach Telegram: safe to retry.
CONNECT_PHASE = frozenset({NETWORK_UNREACHABLE, DNS_ERROR, CONNECT_TIMEOUT, CONNECTION_REFUSED})
# The request may have been processed: never replayed (no duplicate messages).
AMBIGUOUS = frozenset({READ_TIMEOUT, CONNECTION_DROPPED, INVALID_RESPONSE})
RETRYABLE_STATUS = frozenset({502, 503, 504})

# ── policy ────────────────────────────────────────────────────────────────
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 8.0
JITTER = 0.25                 # delay × uniform(1 − JITTER, 1 + JITTER)
CONNECT_TIMEOUT_S = 4.0       # TCP + TLS handshake
READ_TIMEOUT_S = 8.0          # request write + response read
MAX_INLINE_RETRY_AFTER_S = 5.0
MAX_INLINE_RETRY_AFTER_CRITICAL_S = 30.0
DEGRADE_AFTER = 3             # consecutive failed attempts → DEGRADED
PROBE_INTERVAL_S = 30.0       # first probe while DEGRADED
PROBE_INTERVAL_MAX_S = 300.0

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
RECOVERING = "RECOVERING"

_SCHEME = "https"
_HOST = "api.telegram.org"
_PORT = 443

CRITICAL_MARKERS = (
    "ORDEM ABERTA", "ORDEM NÃO ABERTA", "TRADE FECHADO", "POSIÇÃO FECHADA",
    "STOP-LOSS", "DRAWDOWN", "PERDAS CONSECUTIVAS", "BUG", "BLOQUEADO NO STARTUP",
    "EMERG", "LIQUID",
)

_log = logging.getLogger("kakazito-trade")
_async_sleep = asyncio.sleep  # injectable for deterministic tests


def is_critical(text: str) -> bool:
    upper = (text or "")[:200].upper()
    return any(marker in upper for marker in CRITICAL_MARKERS)


def mask_chat(chat) -> str:
    value = str(chat or "")
    return "none" if not value else "***" + value[-3:]


def ipv4_only() -> bool:
    return os.environ.get("NEXUS_TELEGRAM_IPV4_ONLY", "true").strip().lower() != "false"


def backoff_delay(attempt: int, rng=random) -> float:
    """Delay before attempt ``attempt + 1`` (attempt counts from 1)."""
    base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
    return base * rng.uniform(1.0 - JITTER, 1.0 + JITTER)


@dataclass
class AttemptResult:
    cls: str
    status: int | None = None
    retry_after: float | None = None
    parse_error: bool = False
    errno: int | None = None
    latency_ms: int = 0
    result: dict | None = None


@dataclass
class DeliveryResult:
    sent: bool
    cls: str
    attempts: int = 0
    status: int | None = None
    latency_ms: int = 0
    skipped: str | None = None
    ambiguous: bool = False

    def __bool__(self) -> bool:
        return self.sent


def delivered(result) -> bool:
    """Truthful "sent" for a notify() return value.

    ``None`` is what legacy or test doubles return; it carries no failure
    information, so it keeps the historical meaning (no exception = sent).
    """
    if result is None:
        return True
    return bool(result)


# ── health / circuit breaker ──────────────────────────────────────────────
class TelegramHealth:
    """Telegram-only circuit breaker. Never consulted by trading code."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.state = HEALTHY
            self.consecutive_failures = 0
            self.reason = None
            self.last_success = None
            self.next_probe_at = 0.0
            self.probe_interval = PROBE_INTERVAL_S
            self.blocked_until = 0.0
            self.suppressed = 0
            self.counters: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def admit(self, critical: bool) -> tuple[str, int]:
        """Return (decision, max_attempts). decision: SEND | PROBE | SKIP_*."""
        with self._lock:
            now = self._clock()
            if now < self.blocked_until and not critical:
                self.suppressed += 1
                return "SKIP_RATE_LIMITED", 0
            if self.state == HEALTHY:
                return "SEND", MAX_ATTEMPTS
            if critical or now >= self.next_probe_at:
                self.state = RECOVERING
                self.probe_interval = min(PROBE_INTERVAL_MAX_S, self.probe_interval * 2)
                self.next_probe_at = now + self.probe_interval
                return "PROBE", 1
            self.suppressed += 1
            return "SKIP_DEGRADED", 0

    def on_success(self) -> str | None:
        with self._lock:
            previous = self.state
            self.state = HEALTHY
            self.consecutive_failures = 0
            self.reason = None
            self.last_success = self._clock()
            self.probe_interval = PROBE_INTERVAL_S
            self._count(OK)
            return "RECOVERED" if previous != HEALTHY else None

    def on_failure(self, cls: str) -> str | None:
        with self._lock:
            self._count(cls)
            if cls == CLIENT_ERROR:
                # Telegram answered (credential/format problem): the route is
                # reachable, so the transport breaker closes.
                previous = self.state
                self.state = HEALTHY
                self.consecutive_failures = 0
                self.reason = None
                self.probe_interval = PROBE_INTERVAL_S
                return "RECOVERED" if previous != HEALTHY else None
            self.consecutive_failures += 1
            self.reason = cls
            if self.state == RECOVERING:
                self.state = DEGRADED
                return None
            if self.state == HEALTHY and self.consecutive_failures >= DEGRADE_AFTER:
                self.state = DEGRADED
                self.next_probe_at = self._clock() + self.probe_interval
                return DEGRADED
            return None

    def on_rate_limited(self, retry_after: float) -> None:
        with self._lock:
            self._count(RATE_LIMITED)
            if self.state == RECOVERING:
                self.state = DEGRADED  # a probe that hit 429 did not recover
            self.blocked_until = max(self.blocked_until, self._clock() + max(0.0, retry_after))

    def snapshot(self) -> dict:
        with self._lock:
            age = None if self.last_success is None else round(self._clock() - self.last_success, 1)
            return {
                "state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "last_success_age": age,
                "reason": self.reason,
                "suppressed": self.suppressed,
                "counters": dict(self.counters),
            }


HEALTH = TelegramHealth()


def _log_health(event: str) -> None:
    snap = HEALTH.snapshot()
    age = "NA" if snap["last_success_age"] is None else f"{snap['last_success_age']}s"
    if event == "RECOVERED":
        _log.warning("[TELEGRAM_HEALTH] state=RECOVERED consecutive_failures=0 "
                     "last_success_age=0s suppressed=%d decision_effect=NONE", snap["suppressed"])
    else:
        _log.warning("[TELEGRAM_HEALTH] state=%s reason=%s consecutive_failures=%d "
                     "last_success_age=%s decision_effect=NONE",
                     event, snap["reason"], snap["consecutive_failures"], age)


# ── one HTTP attempt (blocking; run in a worker thread) ───────────────────
def _resolve(host: str, port: int):
    family = socket.AF_INET if ipv4_only() else socket.AF_UNSPEC
    return [info[4] for info in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)]


def _connect(host: str, port: int, timeout: float):
    first_error = None
    for addr in _resolve(host, port):
        family = socket.AF_INET6 if len(addr) == 4 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return sock
        except OSError as exc:
            sock.close()
            # Keep the FIRST error: it belongs to the preferred address.
            first_error = first_error or exc
    raise first_error or OSError(errno.EHOSTUNREACH, "no address")


def _classify_connect_error(exc: BaseException) -> AttemptResult:
    if isinstance(exc, socket.gaierror):
        return AttemptResult(DNS_ERROR, errno=getattr(exc, "errno", None))
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return AttemptResult(CONNECT_TIMEOUT)
    if isinstance(exc, ConnectionRefusedError):
        return AttemptResult(CONNECTION_REFUSED, errno=errno.ECONNREFUSED)
    code = getattr(exc, "errno", None)
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return AttemptResult(NETWORK_UNREACHABLE, errno=code)
    if code == errno.ECONNREFUSED:
        return AttemptResult(CONNECTION_REFUSED, errno=code)
    if code == errno.ETIMEDOUT:
        return AttemptResult(CONNECT_TIMEOUT, errno=code)
    return AttemptResult(UNKNOWN, errno=code)


def _classify_http(status: int, raw: bytes, headers) -> AttemptResult:
    body = None
    try:
        body = json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        body = None
    if status == 200:
        if isinstance(body, dict) and body.get("ok") is True:
            result = body.get("result")
            return AttemptResult(OK, status=status, result=result if isinstance(result, dict) else None)
        return AttemptResult(INVALID_RESPONSE, status=status)
    if status == 429:
        retry_after = None
        if isinstance(body, dict):
            retry_after = (body.get("parameters") or {}).get("retry_after")
        if retry_after is None and headers is not None:
            retry_after = headers.get("Retry-After")
        try:
            retry_after = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after = None
        return AttemptResult(RATE_LIMITED, status=status, retry_after=retry_after)
    if status >= 500:
        return AttemptResult(SERVER_ERROR, status=status)
    description = str(body.get("description", "")) if isinstance(body, dict) else ""
    return AttemptResult(CLIENT_ERROR, status=status,
                         parse_error=status == 400 and "parse entities" in description.lower())


def attempt_once(method: str, payload: dict, token: str) -> AttemptResult:
    """One HTTP POST. Never raises; never includes the URL/token in errors."""
    started = time.monotonic()
    result = _attempt(method, payload, token)
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def _attempt(method: str, payload: dict, token: str) -> AttemptResult:
    try:
        sock = _connect(_HOST, _PORT, CONNECT_TIMEOUT_S)
        if _SCHEME == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=_HOST)
    except (OSError, ssl.SSLError) as exc:
        return _classify_connect_error(exc)
    conn = http.client.HTTPConnection(_HOST, _PORT, timeout=READ_TIMEOUT_S)
    conn.sock = sock
    try:
        sock.settimeout(READ_TIMEOUT_S)
        body = json.dumps(payload).encode("utf-8")
        conn.request("POST", f"/bot{token}/{method}", body=body,
                     headers={"Host": _HOST, "Content-Type": "application/json",
                              "Connection": "close"})
        response = conn.getresponse()
        raw = response.read(65536)
        return _classify_http(response.status, raw, response.headers)
    except (socket.timeout, TimeoutError):
        return AttemptResult(READ_TIMEOUT)
    except (ConnectionError, http.client.HTTPException, ssl.SSLError, OSError):
        return AttemptResult(CONNECTION_DROPPED)
    finally:
        conn.close()


# ── delivery policy (shared by async and sync entry points) ───────────────
def _credentials():
    from bot import telegram_credentials
    return telegram_credentials.token(), telegram_credentials.chat()


def _payload(chat, text, parse_mode):
    payload = {"chat_id": chat, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return payload


def _log_attempt(source, res: AttemptResult, attempt, max_attempts, outcome) -> None:
    level = logging.DEBUG if outcome == "SENT" and attempt == 1 else logging.WARNING
    if outcome == "SENT" and attempt > 1:
        level = logging.INFO
    _log.log(level,
             "[TELEGRAM_DELIVERY] source=%s class=%s attempt=%d/%d latency_ms=%d status=%s "
             "errno=%s result=%s decision_effect=NONE",
             source, res.cls, attempt, max_attempts, res.latency_ms,
             res.status if res.status is not None else "NA",
             res.errno if res.errno is not None else "NA", outcome)


class _Plan:
    """State machine for one delivery; I/O and sleeping are injected."""

    def __init__(self, text, parse_mode, critical, source):
        self.text, self.parse_mode, self.critical, self.source = text, parse_mode, critical, source
        self.attempt = 0
        self.max_attempts = 0
        self.plain_fallback_used = False
        self.last: AttemptResult | None = None

    def start(self) -> DeliveryResult | None:
        token, chat = _credentials()
        if not token or not chat:
            return DeliveryResult(False, UNKNOWN, skipped="NO_CREDENTIALS")
        self.token, self.chat = token, chat
        decision, self.max_attempts = HEALTH.admit(self.critical)
        if decision.startswith("SKIP_"):
            _log.debug("[TELEGRAM_DELIVERY] source=%s result=SKIPPED reason=%s decision_effect=NONE",
                       self.source, decision[5:])
            return DeliveryResult(False, HEALTH.snapshot()["reason"] or RATE_LIMITED,
                                  skipped=decision[5:])
        if decision == "PROBE":
            _log.info("[TELEGRAM_HEALTH] state=RECOVERING probe=true critical=%s decision_effect=NONE",
                      str(self.critical).lower())
        return None

    def call_args(self):
        return "sendMessage", _payload(self.chat, self.text, self.parse_mode), self.token

    def after(self, res: AttemptResult):
        """Return ('done', DeliveryResult) or ('retry', delay_seconds)."""
        self.attempt += 1
        self.last = res
        if res.cls == OK:
            _log_attempt(self.source, res, self.attempt, self.max_attempts, "SENT")
            if HEALTH.on_success() == "RECOVERED":
                _log_health("RECOVERED")
            return "done", DeliveryResult(True, OK, self.attempt, res.status, res.latency_ms)
        if res.cls == CLIENT_ERROR and res.parse_error and not self.plain_fallback_used:
            # Content fallback, not a route fallback: same request as plain text.
            self.plain_fallback_used = True
            self.parse_mode = None
            self.max_attempts = max(self.max_attempts, self.attempt + 1)
            _log_attempt(self.source, res, self.attempt, self.max_attempts, "RETRY_PLAIN_TEXT")
            return "retry", 0.0
        if res.cls == RATE_LIMITED:
            wait = res.retry_after if res.retry_after is not None else BACKOFF_CAP_S
            HEALTH.on_rate_limited(wait)
            cap = MAX_INLINE_RETRY_AFTER_CRITICAL_S if self.critical else MAX_INLINE_RETRY_AFTER_S
            if self.attempt < self.max_attempts and wait <= cap:
                _log_attempt(self.source, res, self.attempt, self.max_attempts, "RETRY")
                return "retry", wait
            _log_attempt(self.source, res, self.attempt, self.max_attempts, "FAILED")
            return "done", self._failed(res)
        transient = res.cls in CONNECT_PHASE or (res.cls == SERVER_ERROR and res.status in RETRYABLE_STATUS)
        if transient and self.attempt < self.max_attempts:
            if HEALTH.on_failure(res.cls) == DEGRADED:
                # Breaker opened: stop here instead of adding to the storm.
                _log_attempt(self.source, res, self.attempt, self.max_attempts, "FAILED")
                _log_health(DEGRADED)
                return "done", self._result(res)
            _log_attempt(self.source, res, self.attempt, self.max_attempts, "RETRY")
            return "retry", backoff_delay(self.attempt)
        outcome = "AMBIGUOUS" if res.cls in AMBIGUOUS else "FAILED"
        _log_attempt(self.source, res, self.attempt, self.max_attempts, outcome)
        return "done", self._failed(res)

    def _failed(self, res: AttemptResult) -> DeliveryResult:
        if res.cls != RATE_LIMITED:
            event = HEALTH.on_failure(res.cls)
            if event:
                _log_health(event)
        return self._result(res)

    def _result(self, res: AttemptResult) -> DeliveryResult:
        return DeliveryResult(False, res.cls, self.attempt, res.status, res.latency_ms,
                              ambiguous=res.cls in AMBIGUOUS)


async def deliver(text: str, *, parse_mode: str | None = None, critical: bool | None = None,
                  source: str = "notify") -> DeliveryResult:
    """Async delivery: the blocking attempt runs in a worker thread and every
    wait is ``asyncio.sleep`` — the event loop is never blocked."""
    plan = _Plan(text, parse_mode, is_critical(text) if critical is None else critical, source)
    early = plan.start()
    if early is not None:
        return early
    while True:
        res = await asyncio.to_thread(attempt_once, *plan.call_args())
        action, value = plan.after(res)
        if action == "done":
            return value
        if value:
            await _async_sleep(value)


def deliver_sync(text: str, *, parse_mode: str | None = None, critical: bool | None = None,
                 source: str = "worker", sleep=time.sleep) -> DeliveryResult:
    """Blocking delivery for the background daemon workers (never the loop)."""
    plan = _Plan(text, parse_mode, is_critical(text) if critical is None else critical, source)
    early = plan.start()
    if early is not None:
        return early
    while True:
        action, value = plan.after(attempt_once(*plan.call_args()))
        if action == "done":
            return value
        if value:
            sleep(value)


__all__ = [
    "AMBIGUOUS", "CONNECT_PHASE", "DEGRADED", "HEALTH", "HEALTHY", "RECOVERING",
    "DeliveryResult", "TelegramHealth", "attempt_once", "backoff_delay", "deliver",
    "deliver_sync", "delivered", "is_critical", "mask_chat",
]
