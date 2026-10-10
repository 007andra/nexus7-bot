import base64
import hashlib
import hmac
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

import bot.binance as binance


def test_hmac_signing_remains_compatible():
    payload = "symbol=BTCUSDT&timestamp=123&recvWindow=5000"
    secret = "unit-test-secret"
    expected = hmac.new(
        secret.encode(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    with patch.object(binance, "SIGNING_METHOD", "hmac"), patch.object(
        binance, "API_SECRET", secret
    ):
        assert binance._sign_payload(payload) == expected


def test_ed25519_signing_produces_verifiable_base64():
    payload = "symbol=BTCUSDT&timestamp=123&recvWindow=5000"
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )

    with patch.object(binance, "SIGNING_METHOD", "ed25519"), patch.object(
        binance, "ED25519_PRIVATE_KEY_B64", base64.b64encode(pem).decode("ascii")
    ), patch.object(binance, "ED25519_PRIVATE_KEY_PEM", ""), patch.object(
        binance, "_ED25519_PRIVATE_KEY_CACHE", None
    ):
        signature = base64.b64decode(
            binance._sign_payload(payload), validate=True
        )
        private_key.public_key().verify(signature, payload.encode("ascii"))


def test_ed25519_missing_private_key_fails_closed():
    with patch.object(binance, "SIGNING_METHOD", "ed25519"), patch.object(
        binance, "ED25519_PRIVATE_KEY_B64", ""
    ), patch.object(binance, "ED25519_PRIVATE_KEY_PEM", ""), patch.object(
        binance, "_ED25519_PRIVATE_KEY_CACHE", None
    ):
        try:
            binance._sign_payload("timestamp=123")
        except RuntimeError as exc:
            assert str(exc) == "BINANCE_ED25519_PRIVATE_KEY_UNAVAILABLE"
        else:
            raise AssertionError(
                "Ed25519 signer must fail closed without a private key"
            )

def test_signing_credential_gate_accepts_ed25519_without_hmac_secret():
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )

    with patch.object(binance, "API_KEY", "unit-test-api-key"), patch.object(
        binance, "API_SECRET", ""
    ), patch.object(binance, "SIGNING_METHOD", "ed25519"), patch.object(
        binance, "ED25519_PRIVATE_KEY_B64", ""
    ), patch.object(
        binance, "ED25519_PRIVATE_KEY_PEM", pem.decode("utf-8")
    ), patch.object(binance, "_ED25519_PRIVATE_KEY_CACHE", None):
        binance._assert_signing_credentials_available()


def test_signing_credential_gate_requires_hmac_secret():
    with patch.object(binance, "API_KEY", "unit-test-api-key"), patch.object(
        binance, "API_SECRET", ""
    ), patch.object(binance, "SIGNING_METHOD", "hmac"):
        try:
            binance._assert_signing_credentials_available()
        except RuntimeError as exc:
            assert str(exc) == "BINANCE_API_SECRET_UNAVAILABLE"
        else:
            raise AssertionError(
                "HMAC signer must fail closed without API secret"
            )

