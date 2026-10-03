"""Audit P0-4/P0-5: release/accounting state is one non-contradictory contract,
and the Binance path never claims KuCoin as venue or credential source."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import runtime_release_contract as rc

ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def _c(self, **kw):
        base = dict(exchange="binance", release_authorized=True, validation_lock=False,
                    missing_checks=())
        base.update(kw)
        return rc.ReleaseContract(**base)

    def test_authorized_release_never_logs_accounting_block(self):
        c = self._c().validate()
        self.assertIn("authorized=true", c.release_log())
        acc = c.accounting_log()
        self.assertIn("execution_effect=NONE", acc)
        self.assertIn("accounting_role=OBSERVABILITY_ONLY", acc)
        self.assertIn("release_authorized=true", acc)
        self.assertNotIn("BLOCK_LIVE_RELEASE", acc)

    def test_contradictory_states_are_rejected(self):
        bad = (
            dict(validation_lock=True),
            dict(missing_checks=("paper_disabled",)),
            dict(release_authorized=False, validation_lock=True, missing_checks=()),
            dict(release_authorized=False, validation_lock=False, missing_checks=("x",)),
            dict(accounting_role="AUTHORITY"),
        )
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(rc.ContradictoryRuntimeState):
                self._c(**kw).validate()

    def test_if_accounting_ever_gates_release_it_cannot_coexist_with_authorized(self):
        gated_locked = self._c(release_authorized=False, validation_lock=True,
                               missing_checks=("accounting",), accounting_gates_release=True,
                               accounting_role="RELEASE_PRECONDITION").validate()
        self.assertIn("execution_effect=BLOCK_LIVE_RELEASE", gated_locked.accounting_log())
        self.assertIn("authorized=false", gated_locked.release_log())
        authorized = self._c(accounting_gates_release=True, accounting_role="RELEASE_PRECONDITION")
        self.assertEqual(authorized.validate().accounting_execution_effect, "NONE")

    def test_current_matches_release_control(self):
        env = {k: "" for k in ("PAPER_TRADE", "LIVE_TRADING_CONFIRMED", "REAL_TRADING_PILOT")}
        with patch.dict(os.environ, env):
            c = rc.current()
        self.assertFalse(c.release_authorized)
        self.assertTrue(c.validation_lock)
        self.assertIn("paper_disabled", c.missing_checks)

    def test_no_source_emits_a_constant_block_live_release(self):
        # Only the contract may derive that effect (and only when accounting gates release).
        offenders = [p.name for p in (ROOT / "bot").glob("*.py")
                     if p.name != "runtime_release_contract.py"
                     and "BLOCK_LIVE_RELEASE" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])


def _import_output(exchange, code):
    env = dict(os.environ, EXCHANGE=exchange, PAPER_TRADE="false",
               LIVE_TRADING_CONFIRMED="I_UNDERSTAND_THE_RISK")
    for key in ("PYTHONSTARTUP", "PYTHONPATH"):
        env.pop(key, None)
    # Run outside the repo so the repo's sitecustomize is not auto-loaded and
    # only the imported modules speak.
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); " + code
    out = subprocess.run([sys.executable, "-c", code], cwd=tempfile.gettempdir(), env=env,
                         capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr


class BinanceVenueTruthTests(unittest.TestCase):
    def test_binance_live_banner_names_binance_never_kucoin(self):
        text = _import_output("binance", "import bot.exchange, bot.kucoin")
        self.assertIn("ordens serão enviadas à Binance USD-M Futures", text)
        self.assertNotIn("ordens serão enviadas à KuCoin", text)

    def test_kucoin_live_banner_unchanged_when_kucoin_is_active(self):
        text = _import_output("kucoin", "import bot.exchange, bot.kucoin")
        self.assertEqual(text.count("ordens serão enviadas à KuCoin"), 1)

    def test_signal_notification_names_active_venue(self):
        text = _import_output("binance", (
            "import asyncio, types, bot.notifier as n;"
            "s=types.SimpleNamespace(direction='LONG',symbol='ATOMUSDT',entry=4.0,sl=3.9,tp=4.2,rr=2.0,score=70,reason='t');"
            "print(asyncio.run(n.signal_msg(s)))"))
        self.assertIn("Ordem enviada para a Binance USD-M Futures", text)
        self.assertNotIn("KuCoin", text.split("OPERAÇÃO REAL ATIVA")[-1].split("[KUCOIN_MODULE]")[0])


class PilotCredentialVenueTests(unittest.TestCase):
    def test_binance_checks_binance_signing_material_not_kucoin(self):
        from bot import binance, exchange, pilot

        with patch.object(exchange, "is_binance", return_value=True), \
                patch.object(binance, "_assert_signing_credentials_available",
                             side_effect=RuntimeError("BINANCE_API_KEY_UNAVAILABLE")):
            blockers = pilot._venue_credential_blockers()
        self.assertEqual(blockers, ["1_AUTH: credenciais Binance indisponíveis (BINANCE_API_KEY_UNAVAILABLE)"])

        with patch.object(exchange, "is_binance", return_value=True), \
                patch.object(binance, "_assert_signing_credentials_available", return_value=None):
            self.assertEqual(pilot._venue_credential_blockers(), [])

    def test_unexpected_error_text_is_not_echoed(self):
        from bot import binance, exchange, pilot

        with patch.object(exchange, "is_binance", return_value=True), \
                patch.object(binance, "_assert_signing_credentials_available",
                             side_effect=ValueError("-----BEGIN PRIVATE KEY----- secret")):
            blockers = pilot._venue_credential_blockers()
        self.assertEqual(blockers, ["1_AUTH: credenciais Binance indisponíveis (ValueError)"])
        self.assertNotIn("secret", blockers[0])

    def test_kucoin_venue_keeps_kucoin_check(self):
        from bot import exchange, kucoin, pilot

        with patch.object(exchange, "is_binance", return_value=False), \
                patch.object(kucoin, "API_KEY", ""):
            self.assertEqual(pilot._venue_credential_blockers(), ["1_AUTH: credenciais KuCoin ausentes"])


if __name__ == "__main__":
    unittest.main()
