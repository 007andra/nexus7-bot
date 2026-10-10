import os
import unittest
from unittest.mock import patch

from bot import hwm_namespace


class HwmNamespaceV2Tests(unittest.TestCase):
    def test_namespace_is_stable_and_non_secret(self):
        env = {
            "KUCOIN_API_KEY": "secret-account-identifier",
            "DATABASE_URL": "postgresql://user:password@db.internal:5432/nexus",
            "RAILWAY_ENVIRONMENT_NAME": "production",
        }
        with patch.dict(os.environ, env, clear=True):
            ns = hwm_namespace.hwm_namespace()
            key = hwm_namespace.equity_peak_key()
            legacy = hwm_namespace.legacy_hwm_namespace()
        # NOVO-03 (v3): no Railway identity in the financial namespace.
        self.assertNotIn("environment=", ns)
        self.assertIn("exchange=kucoin", ns)
        self.assertNotIn("secret-account-identifier", ns)
        self.assertNotIn("password", ns)
        self.assertTrue(key.startswith("risk:account_equity_peak:v3:"))
        self.assertIn("environment=production", legacy, "legacy v2 kept only as read source")

    def test_different_accounts_have_different_keys(self):
        base = {
            "DATABASE_URL": "postgresql://u:p@db.internal:5432/nexus",
            "RAILWAY_ENVIRONMENT_NAME": "production",
        }
        with patch.dict(os.environ, {**base, "KUCOIN_API_KEY": "account-a"}, clear=True):
            a = hwm_namespace.equity_peak_key()
        with patch.dict(os.environ, {**base, "KUCOIN_API_KEY": "account-b"}, clear=True):
            b = hwm_namespace.equity_peak_key()
        self.assertNotEqual(a, b)

    def test_railway_environment_change_keeps_the_same_key(self):
        # NOVO-03 inverted the v2 contract: a Railway environment/project move
        # must not make the HWM peak (drawdown gate) disappear.
        base = {
            "DATABASE_URL": "postgresql://u:p@db.internal:5432/nexus",
            "KUCOIN_API_KEY": "account-a",
        }
        with patch.dict(os.environ, {**base, "RAILWAY_ENVIRONMENT_NAME": "production"}, clear=True):
            prod = hwm_namespace.equity_peak_key()
        with patch.dict(os.environ, {**base, "RAILWAY_ENVIRONMENT_NAME": "renamed"}, clear=True):
            moved = hwm_namespace.equity_peak_key()
        self.assertEqual(prod, moved)

    def test_binance_binds_the_binance_account(self):
        base = {"DATABASE_URL": "postgresql://u:p@db.internal:5432/nexus", "EXCHANGE": "binance"}
        with patch.dict(os.environ, {**base, "BINANCE_API_KEY": "acct-1"}, clear=True):
            a = hwm_namespace.hwm_namespace()
        with patch.dict(os.environ, {**base, "BINANCE_API_KEY": "acct-2"}, clear=True):
            b = hwm_namespace.hwm_namespace()
        self.assertIn("exchange=binance", a)
        self.assertNotEqual(a, b, "two Binance accounts on one database never share HWM")

    def test_missing_account_identity_is_explicit(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "postgresql://u:p@db.internal:5432/nexus",
            "RAILWAY_ENVIRONMENT_NAME": "production",
        }, clear=True):
            self.assertIn("account=UNCONFIGURED", hwm_namespace.equity_peak_key())


if __name__ == "__main__":
    unittest.main()
