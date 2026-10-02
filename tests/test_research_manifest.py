import unittest

from bot.research_manifest import ResearchArtifact, ResearchManifest


class ResearchManifestTests(unittest.TestCase):
    def test_manifest_fingerprint_is_order_independent(self):
        a = ResearchArtifact(
            "binance", "klines", "BTCUSDT", "15m", "a" * 64,
            10, 1000, 2000,
        )
        b = ResearchArtifact(
            "binance", "funding", "BTCUSDT", None, "b" * 64,
            3, 1000, 2000,
        )
        m1 = ResearchManifest("v1", "abc", 3000, (a, b), ("f2", "f1"))
        m2 = ResearchManifest("v1", "abc", 3000, (b, a), ("f1", "f2"))
        self.assertEqual(m1.fingerprint, m2.fingerprint)

    def test_nonempty_artifact_requires_time_range(self):
        with self.assertRaises(ValueError):
            ResearchArtifact(
                "binance", "klines", "BTCUSDT", "15m",
                "a" * 64, 1, None, None,
            )


if __name__ == "__main__":
    unittest.main()
