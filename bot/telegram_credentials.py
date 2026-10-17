"""Single authority for Telegram credential resolution.

Canonical names first (``TELEGRAM_TOKEN``, ``TELEGRAM_CHAT``), then the Railway
aliases already used by this project (``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID``). Every Telegram emitter must resolve credentials here so
that one emitter cannot be silently disabled while another works. Values are
never logged. No bot imports, so it is safe from logger/config import cycles.
"""
from __future__ import annotations

import os


def token() -> str:
    return (os.environ.get("TELEGRAM_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())


def chat() -> str:
    return (os.environ.get("TELEGRAM_CHAT", "").strip()
            or os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def configured() -> bool:
    return bool(token()) and bool(chat())
