"""All Telegram emitters resolve credentials from one authority (aliases included)."""
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import telegram_credentials as tc

ROOT = Path(__file__).resolve().parents[1]
CLEAR = {k: "" for k in ("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT", "TELEGRAM_CHAT_ID")}


class TelegramCredentialAuthorityTests(unittest.TestCase):
    def test_canonical_names_win_then_railway_aliases(self):
        with patch.dict(os.environ, dict(CLEAR, TELEGRAM_BOT_TOKEN="alias", TELEGRAM_CHAT_ID="42")):
            self.assertEqual((tc.token(), tc.chat(), tc.configured()), ("alias", "42", True))
        with patch.dict(os.environ, dict(CLEAR, TELEGRAM_TOKEN="canon", TELEGRAM_BOT_TOKEN="alias",
                                         TELEGRAM_CHAT="1", TELEGRAM_CHAT_ID="2")):
            self.assertEqual((tc.token(), tc.chat()), ("canon", "1"))
        with patch.dict(os.environ, CLEAR):
            self.assertFalse(tc.configured())

    def test_production_alias_shape_enables_every_emitter(self):
        # Production (read-only check 2026-09-26) sets TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT.
        from bot import logger
        with patch.dict(os.environ, dict(CLEAR, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT="c",
                                         NEXUS_TELEGRAM="true")):
            self.assertTrue(logger._tg_enabled())

    def test_no_emitter_reads_telegram_env_directly(self):
        offenders = []
        for path in (ROOT / "bot").glob("*.py"):
            if path.name == "telegram_credentials.py":
                continue
            if re.search(r'environ\.get\(\s*["\']TELEGRAM_(TOKEN|CHAT|BOT_TOKEN|CHAT_ID)["\']',
                         path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
