"""Standalone Binance USD-M RESEARCH-ONLY HTTPS GET transport for V3.1.

The actual preflight, research-only key equality check and endpoint permission
checks remain in short_down_bos_v31_evidence_collector. This adapter accepts
only those exact allowlisted HTTPS routes, never mutates exchange state and
never schedules work. Caller owns the separate aiohttp.ClientSession.

No LIVE imports, DB access, environment reads, logs, retries or side effects at
import time. Error messages contain constant reason codes, not keys, signed
query strings, Binance response bodies or wallet information.
"""
from __future__ import annotations

import json

import aiohttp

from bot.short_down_bos_v31_evidence_collector import (
    ALLOWED_GET, CollectorRefused,
)

MAX_RESPONSE_BYTES = 1_048_576
HTTP_TIMEOUT_SECONDS = 7


class ReadOnlyResearchHTTPS:
    """Callable transport(host, path, query, headers) for the V3.1 client."""

    def __init__(self, session: aiohttp.ClientSession):
        if session is None or not callable(getattr(session, "get", None)):
            raise CollectorRefused("RESEARCH_HTTP_SESSION_REQUIRED")
        self._session = session

    async def __call__(self, host: str, path: str, query: dict, headers: dict):
        if (host, path) not in ALLOWED_GET:
            raise CollectorRefused("ENDPOINT_NOT_ALLOWLISTED")
        url = "https://" + host + path
        try:
            async with self._session.get(
                url,
                params=dict(query),
                headers=dict(headers),
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
                allow_redirects=False,
                ssl=True,
            ) as response:
                if response.status != 200:
                    # No raw exchange error messages: they can include
                    # account, signed URL and other credential material.
                    raise CollectorRefused("RESEARCH_HTTP_NON_200")
                if (response.content_length is not None
                        and response.content_length > MAX_RESPONSE_BYTES):
                    raise CollectorRefused("RESEARCH_HTTP_RESPONSE_TOO_LARGE")
                body = await response.content.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CollectorRefused("RESEARCH_HTTP_RESPONSE_TOO_LARGE")
        except CollectorRefused:
            raise
        except Exception:
            raise CollectorRefused("RESEARCH_HTTP_TRANSPORT_FAILURE") from None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise CollectorRefused("RESEARCH_HTTP_JSON_INVALID") from None
        if not isinstance(payload, (dict, list)):
            raise CollectorRefused("RESEARCH_HTTP_JSON_INVALID")
        return payload
