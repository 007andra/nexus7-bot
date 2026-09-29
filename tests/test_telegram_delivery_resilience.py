"""Telegram delivery resilience: transport, classification, retry, breaker.

Every test is offline. HTTP scenarios run against a scripted loopback server
(``127.0.0.1``); connect-phase failures are injected at the socket layer. The
real ``api.telegram.org`` is never contacted (``socket.getaddrinfo`` for any
non-loopback host raises in the harness).
"""
from __future__ import annotations

import asyncio
import errno
import http.server
import io
import json
import logging
import os
import random
import re
import socket
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import telegram_transport as tt

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456789:SECRET-token-value-never-logged"
CHAT = "-1009876543210"
LOGGER = "kakazito-trade"
_REAL_GETADDRINFO = socket.getaddrinfo


def _loopback_only_getaddrinfo(host, *args, **kwargs):
    if host not in ("127.0.0.1", "localhost"):
        raise socket.gaierror(socket.EAI_NONAME, "offline test: external DNS blocked")
    return _REAL_GETADDRINFO(host, *args, **kwargs)


class ScriptedTelegram:
    """Loopback HTTP server answering from a script of responses."""

    def __init__(self):
        self.script: list = []
        self.requests: list = []
        self.default = ("json", 200, {"ok": True, "result": {"message_id": 1}})
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append({"path": self.path, "body": body})
                step = owner.script.pop(0) if owner.script else owner.default
                kind = step[0]
                if kind == "drop":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if kind == "sleep":
                    time.sleep(step[1])
                    step = owner.default
                    kind = step[0]
                status, payload = step[1], step[2]
                headers = step[3] if len(step) > 3 else {}
                raw = json.dumps(payload).encode() if kind == "json" else payload
                try:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    return  # client gave up (read-timeout scenarios): expected

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TransportHarness(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.NOTSET)
        self.server = ScriptedTelegram()
        self.addCleanup(self.server.close)
        self.clock = FakeClock()
        self.sleeps: list[float] = []

        async def fake_async_sleep(seconds):
            self.sleeps.append(seconds)

        patches = [
            patch.dict(os.environ, {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT": CHAT,
                                    "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}),
            patch.object(tt, "_SCHEME", "http"),
            patch.object(tt, "_HOST", "127.0.0.1"),
            patch.object(tt, "_PORT", self.server.port),
            patch.object(tt, "CONNECT_TIMEOUT_S", 1.0),
            patch.object(tt, "READ_TIMEOUT_S", 0.5),
            patch.object(tt, "_async_sleep", fake_async_sleep),
            patch.object(tt, "HEALTH", tt.TelegramHealth(clock=self.clock)),
            patch("socket.getaddrinfo", _loopback_only_getaddrinfo),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_async(self, coro):
        return asyncio.run(coro)

    def deliver(self, text="hello", **kw):
        return self.run_async(tt.deliver(text, **kw))

    def sync(self, text="hello", **kw):
        return tt.deliver_sync(text, sleep=self.sleeps.append, **kw)

    def fail_connect(self, exc):
        def boom(host, port, timeout):
            raise exc
        return patch.object(tt, "_connect", boom)


# ── 1–13: reproduction of every failure mode ─────────────────────────────
class FailureModeTests(TransportHarness):
    def test_01_http_success(self):
        res = self.deliver("hi", parse_mode="Markdown")
        self.assertTrue(res.sent)
        self.assertEqual((res.cls, res.attempts, res.status), (tt.OK, 1, 200))
        self.assertEqual(self.server.requests[0]["body"]["parse_mode"], "Markdown")
        self.assertEqual(self.server.requests[0]["path"], f"/bot{TOKEN}/sendMessage")

    def test_02_dns_failure_is_classified_and_retried_bounded(self):
        with self.fail_connect(socket.gaierror(socket.EAI_AGAIN, "temporary failure")):
            res = self.deliver()
        self.assertEqual((res.sent, res.cls, res.attempts), (False, tt.DNS_ERROR, tt.MAX_ATTEMPTS))
        self.assertEqual(len(self.sleeps), tt.MAX_ATTEMPTS - 1)

    def test_03_enetunreach_errno_101(self):
        with self.fail_connect(OSError(errno.ENETUNREACH, "Network is unreachable")):
            res = self.deliver()
        self.assertEqual((res.cls, res.attempts), (tt.NETWORK_UNREACHABLE, tt.MAX_ATTEMPTS))

    def test_04_connection_refused_real_socket(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        with patch.object(tt, "_PORT", closed_port):
            res = self.deliver()
        self.assertEqual(res.cls, tt.CONNECTION_REFUSED)
        self.assertEqual(res.attempts, tt.MAX_ATTEMPTS)

    def test_05_connect_timeout(self):
        with self.fail_connect(socket.timeout("timed out")):
            res = self.deliver()
        self.assertEqual((res.cls, res.attempts), (tt.CONNECT_TIMEOUT, tt.MAX_ATTEMPTS))

    def test_06_read_timeout_is_ambiguous_and_never_replayed(self):
        self.server.script = [("sleep", 1.5)]
        res = self.deliver()
        self.assertEqual((res.cls, res.attempts, res.ambiguous), (tt.READ_TIMEOUT, 1, True))
        time.sleep(1.2)
        self.assertEqual(len(self.server.requests), 1)  # no duplicate send

    def test_07_http_429_respects_retry_after(self):
        self.server.script = [("json", 429, {"ok": False, "parameters": {"retry_after": 3}})]
        res = self.deliver()
        self.assertTrue(res.sent)
        self.assertEqual(self.sleeps, [3.0])
        self.assertEqual(res.attempts, 2)

    def test_07b_429_retry_after_too_long_defers_and_suppresses(self):
        self.server.script = [("json", 429, {"ok": False, "parameters": {"retry_after": 60}})]
        res = self.deliver()
        self.assertEqual((res.sent, res.cls, res.attempts), (False, tt.RATE_LIMITED, 1))
        again = self.deliver()  # inside retry_after window: not sent at all
        self.assertEqual(again.skipped, "RATE_LIMITED")
        self.assertEqual(len(self.server.requests), 1)
        self.clock.t += 61
        self.assertTrue(self.deliver().sent)

    def test_07c_retry_after_header_used_when_body_lacks_it(self):
        self.server.script = [("json", 429, {"ok": False}, {"Retry-After": "2"})]
        self.assertTrue(self.deliver().sent)
        self.assertEqual(self.sleeps, [2.0])

    def test_08_http_500_is_not_replayed(self):
        self.server.script = [("json", 500, {"ok": False})]
        res = self.deliver()
        self.assertEqual((res.sent, res.cls, res.status, res.attempts), (False, tt.SERVER_ERROR, 500, 1))

    def test_09_http_502_503_retried_then_success(self):
        self.server.script = [("json", 502, {"ok": False}), ("json", 503, {"ok": False})]
        res = self.deliver()
        self.assertTrue(res.sent)
        self.assertEqual(res.attempts, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_10_invalid_response_is_ambiguous(self):
        self.server.script = [("raw", 200, b"<html>not json</html>")]
        res = self.deliver()
        self.assertEqual((res.sent, res.cls, res.ambiguous, res.attempts),
                         (False, tt.INVALID_RESPONSE, True, 1))
        self.server.script = [("json", 200, {"ok": False})]
        self.assertEqual(self.deliver("x2").cls, tt.INVALID_RESPONSE)

    def test_11_connection_dropped_during_send_is_ambiguous(self):
        self.server.script = [("drop",)]
        res = self.deliver()
        self.assertEqual((res.sent, res.cls, res.ambiguous, res.attempts),
                         (False, tt.CONNECTION_DROPPED, True, 1))
        self.assertEqual(len(self.server.requests), 1)

    def test_12_markdown_rejected_then_plain_text_fallback(self):
        self.server.script = [("json", 400, {"ok": False,
                                             "description": "Bad Request: can't parse entities"})]
        res = self.deliver("*broken_markdown", parse_mode="Markdown")
        self.assertTrue(res.sent)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(self.server.requests[0]["body"].get("parse_mode"), "Markdown")
        self.assertNotIn("parse_mode", self.server.requests[1]["body"])

    def test_12b_plain_fallback_network_failure_reports_class_not_http_0(self):
        self.server.script = [("json", 400, {"ok": False, "description": "can't parse entities"}),
                              ("drop",)]
        res = self.deliver("*x", parse_mode="Markdown")
        self.assertEqual((res.sent, res.cls), (False, tt.CONNECTION_DROPPED))
        self.assertIsNone(res.status)  # never the fake "HTTP 0"

    def test_13_recovery_after_failures(self):
        with self.fail_connect(OSError(errno.ENETUNREACH, "unreachable")):
            self.assertFalse(self.deliver().sent)
        self.assertEqual(tt.HEALTH.state, tt.DEGRADED)
        self.clock.t += tt.PROBE_INTERVAL_S
        with self.assertLogs(LOGGER, level="INFO") as logs:
            res = self.deliver()
        self.assertTrue(res.sent)
        self.assertEqual(tt.HEALTH.state, tt.HEALTHY)
        self.assertTrue(any("[TELEGRAM_HEALTH] state=RECOVERED" in x for x in logs.output))

    def test_client_errors_not_retried(self):
        for status in (400, 401, 403):
            self.server.script = [("json", status, {"ok": False, "description": "nope"})]
            res = self.deliver(f"m{status}")
            self.assertEqual((res.cls, res.status, res.attempts), (tt.CLIENT_ERROR, status, 1))
        self.assertEqual(tt.HEALTH.state, tt.HEALTHY)  # not a transport outage


# ── retry / backoff / jitter / breaker / dedup / concurrency / shutdown ──
class PolicyTests(TransportHarness):
    def test_retry_limited_and_backoff_exponential_with_jitter_in_range(self):
        rng = random.Random(7)
        for attempt in range(1, 8):
            base = min(tt.BACKOFF_CAP_S, tt.BACKOFF_BASE_S * 2 ** (attempt - 1))
            for _ in range(200):
                d = tt.backoff_delay(attempt, rng)
                self.assertGreaterEqual(d, base * (1 - tt.JITTER))
                self.assertLessEqual(d, base * (1 + tt.JITTER))
        with self.fail_connect(socket.timeout()):
            res = self.deliver()
        self.assertEqual(res.attempts, tt.MAX_ATTEMPTS)
        self.assertLess(self.sleeps[0], self.sleeps[1])

    def test_circuit_breaker_stops_request_storm(self):
        calls = []

        def boom(host, port, timeout):
            calls.append(1)
            raise OSError(errno.ENETUNREACH, "unreachable")

        with patch.object(tt, "_connect", boom), self.assertLogs(LOGGER, level="WARNING") as logs:
            first = self.deliver("a")
            skipped = [self.deliver(f"routine {i}") for i in range(20)]
        self.assertFalse(first.sent)
        self.assertEqual(len(calls), tt.DEGRADE_AFTER)
        self.assertTrue(all(r.skipped == "DEGRADED" for r in skipped))
        self.assertTrue(any("[TELEGRAM_HEALTH] state=DEGRADED reason=NETWORK_UNREACHABLE" in x
                            for x in logs.output))
        self.assertEqual(tt.HEALTH.snapshot()["suppressed"], 20)

    def test_probe_spacing_grows_and_critical_gets_single_attempt(self):
        calls = []

        def boom(host, port, timeout):
            calls.append(self.clock.t)
            raise socket.timeout()

        with patch.object(tt, "_connect", boom):
            self.deliver("a")
            n = len(calls)
            self.clock.t += tt.PROBE_INTERVAL_S
            self.deliver("probe 1")
            self.assertEqual(len(calls), n + 1)            # probe = one attempt
            self.clock.t += tt.PROBE_INTERVAL_S           # interval doubled: not due yet
            self.assertEqual(self.deliver("routine").skipped, "DEGRADED")
            res = self.deliver("✅ *ORDEM ABERTA* BTCUSDT")  # critical: always one attempt
            self.assertEqual((res.attempts, len(calls)), (1, n + 2))

    def test_dedup_no_duplicates_through_notify_layer(self):
        from bot import runtime_hardening, notifier
        import bot.engine as engine_module
        saved = (notifier.notify, engine_module.notify,
                 getattr(notifier, "_transport_hardening_patched", False))
        self.addCleanup(lambda: (setattr(notifier, "notify", saved[0]),
                                 setattr(engine_module, "notify", saved[1]),
                                 setattr(notifier, "_transport_hardening_patched", saved[2])))
        notifier._transport_hardening_patched = False
        with patch.object(runtime_hardening, "time", SimpleNamespace(monotonic=lambda: 10_000.0)), \
             patch("bot.config.cfg.TELEGRAM_TOKEN", TOKEN), patch("bot.config.cfg.TELEGRAM_CHAT", CHAT):
            runtime_hardening.install_telegram_fix(logging.getLogger(LOGGER))
            self.server.script = [("sleep", 1.5)]  # ambiguous first send

            async def twice():
                return await notifier.notify("same text"), await notifier.notify("same text")

            first, second = self.run_async(twice())
        self.assertEqual((first.cls, second.skipped), (tt.READ_TIMEOUT, "DEDUP"))
        time.sleep(1.2)
        self.assertEqual(len(self.server.requests), 1)

    def test_concurrency_10_notifications_failure_and_recovery(self):
        calls = []
        lock = threading.Lock()

        def boom(host, port, timeout):
            with lock:
                calls.append(1)
            raise OSError(errno.ENETUNREACH, "unreachable")

        async def burst(prefix):
            return await asyncio.gather(*(tt.deliver(f"{prefix} {i}") for i in range(10)))

        with patch.object(tt, "_connect", boom):
            results = self.run_async(burst("down"))
        self.assertEqual(len(results), 10)
        self.assertFalse(any(r.sent for r in results))
        # Bounded: never 10 × MAX_ATTEMPTS; the breaker cuts the storm.
        self.assertLess(len(calls), 10 * tt.MAX_ATTEMPTS)
        self.clock.t += tt.PROBE_INTERVAL_S
        recovered = self.run_async(burst("up"))
        self.assertEqual(sum(r.sent for r in recovered), 1)  # the probe
        self.assertEqual(tt.HEALTH.state, tt.HEALTHY)
        again = self.run_async(burst("up2"))
        self.assertTrue(all(r.sent for r in again))
        self.assertEqual(len({r["body"]["text"] for r in self.server.requests}),
                         len(self.server.requests))  # no duplicate text sent

    def test_shutdown_cancels_during_backoff_without_orphans(self):
        async def scenario():
            with patch.object(tt, "_async_sleep", asyncio.sleep), \
                 patch.object(tt, "backoff_delay", lambda attempt, rng=None: 30.0), \
                 self.fail_connect(socket.timeout()):
                task = asyncio.create_task(tt.deliver("x"))
                await asyncio.sleep(0.3)
                started = time.monotonic()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                elapsed = time.monotonic() - started
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                return elapsed, pending

        elapsed, pending = self.run_async(scenario())
        self.assertLess(elapsed, 1.0)
        self.assertEqual(pending, [])

    def test_event_loop_not_blocked_by_slow_telegram(self):
        self.server.script = [("sleep", 0.4)]
        ticks = []

        async def scenario():
            async def ticker():
                for _ in range(8):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.05)
            await asyncio.gather(tt.deliver("slow"), ticker())

        self.run_async(scenario())
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertLess(max(gaps), 0.25)

    def test_sync_worker_path_same_policy(self):
        with self.fail_connect(OSError(errno.ENETUNREACH, "x")):
            res = self.sync()
        self.assertEqual((res.cls, res.attempts), (tt.NETWORK_UNREACHABLE, tt.MAX_ATTEMPTS))
        self.assertEqual(len(self.sleeps), tt.MAX_ATTEMPTS - 1)
        self.clock.t += tt.PROBE_INTERVAL_S
        self.assertTrue(self.sync().sent)


class ConfigurationAndRedactionTests(TransportHarness):
    def test_telegram_disabled_and_missing_credentials(self):
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "", "TELEGRAM_CHAT": ""}):
            res = self.deliver()
        self.assertEqual((res.sent, res.skipped), (False, "NO_CREDENTIALS"))
        self.assertEqual(self.server.requests, [])
        from bot import logger
        with patch.dict(os.environ, {"NEXUS_TELEGRAM": "false"}):
            self.assertFalse(logger._tg_enabled())

    def test_secret_redaction_in_logs_and_stderr(self):
        from bot import logger
        stderr = io.StringIO()
        with self.assertLogs(LOGGER, level="DEBUG") as logs, redirect_stderr(stderr):
            with self.fail_connect(OSError(errno.ENETUNREACH, f"https://api.telegram.org/bot{TOKEN}")):
                self.deliver("secret payload text")
            self.server.script = [("json", 401, {"ok": False, "description": f"bad {TOKEN}"})]
            self.clock.t += 1000
            self.deliver("another")
            tt.HEALTH.reset()
            logger._AI_TG_QUEUE.put_nowait("worker text")
            with patch.object(tt, "_connect", lambda *a: (_ for _ in ()).throw(socket.timeout())):
                text = logger._AI_TG_QUEUE.get()
                res = tt.deliver_sync(text, sleep=lambda s: None)
                logger._diag(f"Telegram delivery failed: class={res.cls} attempts={res.attempts}")
                logger._AI_TG_QUEUE.task_done()
        blob = "\n".join(logs.output) + stderr.getvalue()
        for secret in (TOKEN, CHAT, "api.telegram.org/bot", "secret payload text"):
            self.assertNotIn(secret, blob)
        self.assertIn("class=CONNECT_TIMEOUT", blob)
        self.assertEqual(tt.mask_chat(CHAT), "***210")

    def test_ipv6_last_error_masking_reproduced_and_fixed(self):
        """Production root-cause mechanics: IPv4 SYN timeout, then IPv6 ENETUNREACH."""
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("149.154.166.110", 443))
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:67c:4e8:f004::9", 443, 0, 0))
        requested = []

        def gai(host, port, family=0, *a, **k):
            requested.append(family)
            return [v4] if family == socket.AF_INET else [v4, v6]

        class FakeSock:
            def __init__(self, family=socket.AF_INET, *a, **k):
                self.family = family

            def settimeout(self, t):
                pass

            def connect(self, addr):
                if self.family == socket.AF_INET6:
                    raise OSError(errno.ENETUNREACH, "Network is unreachable")
                raise socket.timeout("timed out")

            def close(self):
                pass

        with patch("socket.getaddrinfo", gai), patch("socket.socket", FakeSock):
            # What urllib/socket.create_connection (the old worker) reports:
            with self.assertRaises(OSError) as old:
                socket.create_connection(("api.telegram.org", 443), timeout=1)
            self.assertEqual(old.exception.errno, errno.ENETUNREACH)  # masks the timeout
            # Canonical transport: IPv4 only, truthful class.
            with patch.object(tt, "_SCHEME", "https"), patch.object(tt, "_HOST", "api.telegram.org"):
                self.assertEqual(tt.attempt_once("sendMessage", {}, TOKEN).cls, tt.CONNECT_TIMEOUT)
            with patch.dict(os.environ, {"NEXUS_TELEGRAM_IPV4_ONLY": "false"}), \
                 patch.object(tt, "_HOST", "api.telegram.org"):
                self.assertEqual(tt.attempt_once("sendMessage", {}, TOKEN).cls, tt.CONNECT_TIMEOUT)
        self.assertEqual(requested[1], socket.AF_INET)

    def test_verify_masks_chat_and_never_returns_raw_error(self):
        from bot import notifier
        with patch("bot.config.cfg.TELEGRAM_TOKEN", TOKEN), patch("bot.config.cfg.TELEGRAM_CHAT", CHAT):
            self.server.script = [("json", 200, {"ok": True, "result": {"username": "bgx_bot"}})]
            ok = self.run_async(notifier.test_telegram())
            with self.fail_connect(socket.timeout()):
                bad = self.run_async(notifier.test_telegram())
        self.assertEqual(ok, {"ok": True, "bot": "bgx_bot", "chat": "***210"})
        self.assertEqual(bad, {"ok": False, "reason": tt.CONNECT_TIMEOUT})


# ── truthful "sent" (base-fail: sent=true was logged on failed delivery) ──
class TruthfulSentTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.NOTSET)
        from bot import notifier, runtime_hardening
        import bot.engine as engine_module
        saved = (notifier.notify, engine_module.notify,
                 getattr(notifier, "_transport_hardening_patched", False),
                 dict(getattr(notifier, "_nexus_score_cache", {})))
        self.addCleanup(lambda: (setattr(notifier, "notify", saved[0]),
                                 setattr(engine_module, "notify", saved[1]),
                                 setattr(notifier, "_transport_hardening_patched", saved[2])))
        notifier._transport_hardening_patched = False
        for p in (patch("bot.config.cfg.TELEGRAM_TOKEN", TOKEN), patch("bot.config.cfg.TELEGRAM_CHAT", CHAT),
                  patch.dict(os.environ, {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT": CHAT}),
                  patch("socket.getaddrinfo", _loopback_only_getaddrinfo)):
            p.start()
            self.addCleanup(p.stop)
        if hasattr(__import__("bot.telegram_transport").telegram_transport, "HEALTH"):
            hp = patch.object(tt, "HEALTH", tt.TelegramHealth())
            hp.start()
            self.addCleanup(hp.stop)
        runtime_hardening.install_telegram_fix(logging.getLogger(LOGGER))
        self.notifier = notifier

    def test_nexus_score_logs_sent_false_when_telegram_unreachable(self):
        d = {"decision": "WAIT", "symbol": "TRUTHUSDT", "final_score": 51.5, "reason": "truth"}
        with self.assertLogs(LOGGER, level="INFO") as logs:
            ok = asyncio.run(self.notifier.notify_nexus_score(d))
        lines = [x for x in logs.output if "[NEXUS_TELEGRAM_SCORE]" in x and "TRUTHUSDT" in x]
        self.assertTrue(lines)
        self.assertFalse(ok)
        self.assertIn("sent=false", lines[-1])
        self.assertNotIn("sent=true", "\n".join(lines))

    def test_market_radar_reports_failed_delivery(self):
        from bot import market_radar
        with patch.object(market_radar, "build_message", lambda engine, radar: "radar"), \
             self.assertLogs(LOGGER, level="INFO") as logs:
            ok = asyncio.run(market_radar.send_once(None, logging.getLogger(LOGGER),
                                                    notify=self.notifier.notify))
        self.assertFalse(ok)
        self.assertFalse(any("[MARKET_RADAR] sent=true" in x for x in logs.output))


# ── Telegram is observability only: trading decisions are identical ─────
class TradingIndependenceTests(unittest.TestCase):
    OBSERVABILITY_ONLY = {"telegram_transport.py", "notifier.py", "runtime_hardening.py",
                          "logger.py", "funnel_metrics.py", "market_radar.py",
                          "startup_ready_notification.py", "nexus_latency_telegram.py"}

    def test_no_trading_module_depends_on_telegram_health(self):
        offenders = []
        for path in (ROOT / "bot").glob("*.py"):
            if path.name in self.OBSERVABILITY_ONLY:
                continue
            if re.search(r"telegram_transport|TELEGRAM_HEALTH|\bHEALTH\.state", path.read_text("utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_decisions_identical_with_telegram_down_or_up(self):
        from tests.test_binance_live_sizing_minimum_order import (ADA, CANDIDATES, RuntimeHarness)

        class H(RuntimeHarness):
            def runTest(self):
                pass

        def decisions():
            h = H()
            h.setUp()
            try:
                out = {}
                for sym, (info, entry, stop) in list(CANDIDATES.items()) + [("ADAUSDT", (ADA, 0.2595, 0.2578))]:
                    final, risk_qty, _ = h.run_hook(sym, info, entry, stop)
                    out[sym] = (final, risk_qty)
                return out
            finally:
                h.doCleanups()

        async def failing_notify(text, *a, **k):
            return tt.DeliveryResult(False, tt.NETWORK_UNREACHABLE, attempts=3)

        async def raising_notify(text, *a, **k):
            raise OSError(errno.ENETUNREACH, "down")

        async def ok_notify(text, *a, **k):
            return tt.DeliveryResult(True, tt.OK, attempts=1)

        results = []
        degraded = tt.TelegramHealth()
        for _ in range(tt.DEGRADE_AFTER):
            degraded.on_failure(tt.NETWORK_UNREACHABLE)
        for notify, health in ((ok_notify, tt.TelegramHealth()), (failing_notify, degraded),
                               (raising_notify, degraded)):
            with patch("bot.notifier.notify", notify), patch("bot.engine.notify", notify), \
                 patch.object(tt, "HEALTH", health):
                results.append(decisions())
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertEqual(results[0]["ADAUSDT"], (23.0, 23.0))
        self.assertEqual(results[0]["UNIUSDT"], (0.0, 0.0))

    def test_pilot_guard_unaffected_by_telegram_state(self):
        import bot.pilot as pilot
        self.assertTrue(hasattr(pilot, "PilotGuard"))
        for name in ("pilot.py", "pilot_live_runtime.py", "pilot_risk_cap_hardening.py",
                     "final_sizing_invariants.py", "risk_manager_v3.py", "professional_risk.py"):
            src = (ROOT / "bot" / name).read_text("utf-8")
            self.assertNotIn("telegram_transport", src, name)
            self.assertNotIn("TELEGRAM_HEALTH", src, name)


if __name__ == "__main__":
    unittest.main()
