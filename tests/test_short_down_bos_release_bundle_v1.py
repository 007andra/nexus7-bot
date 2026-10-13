"""Offline deterministic GitHub-release *candidate* package, NO publishing."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from bot import short_down_bos_event_archive_v1 as tape
from bot import short_down_bos_release_bundle_v1 as bundle

SID = "research-20261012-0001"
MS = 1_800_000_000_000
SYMBOL = "BTCUSDT"


def event(i):
    return {"e": "aggTrade", "E": MS + i * 10 + 2,
            "s": SYMBOL, "a": 1000 + i,
            "p": "101.5", "q": "0.20",
            "f": 2000 + i, "l": 2000 + i,
            "T": MS + i * 10, "m": False}


def sealed(directory):
    w = tape.Archive(
        directory, symbol=SYMBOL, session_id=SID,
        generation=1, started_ms=MS)
    for i in range(3):
        w.append(event(i), received_ms=MS + 10 * i + 2)
    return w.seal()


class BundleTests(unittest.TestCase):
    def test_bundle_is_locally_verifiable_but_not_published_or_attested(self):
        with tempfile.TemporaryDirectory() as parent:
            origin = Path(parent) / "raw"
            out = Path(parent) / "export"
            origin.mkdir()
            out.mkdir()
            meta = sealed(origin)
            target = out / "research-trades.zip"
            receipt = bundle.prepare_bundle(
                origin, session_id=SID, destination=target)
            self.assertEqual(receipt["status"],
                             "BUNDLE_SELF_VERIFIED_NOT_UPLOADED")
            self.assertEqual(receipt["event_count"], 3)
            self.assertEqual(receipt["final_event_chain_sha256"],
                             meta["final_hash"])
            self.assertEqual(len(receipt["bundle_sha256"]), 64)
            self.assertTrue(receipt["public_repository"])
            self.assertFalse(receipt["github_release_created"])
            self.assertFalse(receipt["github_asset_uploaded"])
            self.assertFalse(receipt["durable_remote_copy_proven"])
            self.assertFalse(receipt["storage_cost_verified_zero"])
            self.assertFalse(receipt["source_independently_attested"])
            self.assertFalse(receipt["live_allowed"])
            self.assertEqual(
                bundle.verify_bundle(target)["bundle_sha256"],
                receipt["bundle_sha256"])

    def test_existing_target_is_not_overwritten_or_deleted(self):
        with tempfile.TemporaryDirectory() as parent:
            origin, dest = Path(parent) / "raw", Path(parent) / "bundle.zip"
            origin.mkdir()
            sealed(origin)
            dest.write_bytes(b"MY_EXACT_ORIGINAL_FILE")
            with self.assertRaises(FileExistsError):
                bundle.prepare_bundle(origin, session_id=SID,
                                      destination=dest)
            self.assertEqual(dest.read_bytes(), b"MY_EXACT_ORIGINAL_FILE")

    def test_incomplete_raw_archive_never_gets_packaged(self):
        with tempfile.TemporaryDirectory() as parent:
            origin = Path(parent) / "raw"
            origin.mkdir()
            target = Path(parent) / "bundle.zip"
            with self.assertRaises(ValueError):
                bundle.prepare_bundle(origin, session_id=SID,
                                      destination=target)
            self.assertFalse(target.exists())

    def test_deleted_segment_is_detected(self):
        with tempfile.TemporaryDirectory() as parent:
            origin = Path(parent) / "raw"
            origin.mkdir()
            sealed(origin)
            (origin / f"{SID}.0000.jsonl").unlink()
            target = Path(parent) / "bundle.zip"
            with self.assertRaises(ValueError):
                bundle.prepare_bundle(origin, session_id=SID,
                                      destination=target)

    def test_corrupted_or_extra_zip_member_fails(self):
        with tempfile.TemporaryDirectory() as parent:
            origin, out = Path(parent) / "raw", Path(parent) / "export"
            origin.mkdir()
            out.mkdir()
            sealed(origin)
            target = out / "bundle.zip"
            bundle.prepare_bundle(origin, session_id=SID, destination=target)
            with zipfile.ZipFile(target, "a") as z:
                z.writestr("../evil.txt", b"evil")
            self.assertEqual(bundle.verify_bundle(target)["status"],
                             "BUNDLE_NOT_PROVEN")
            self.assertFalse(bundle.verify_bundle(target)["live_allowed"])

    def test_missing_manifest_or_symlink_refused(self):
        with tempfile.TemporaryDirectory() as parent:
            dest = Path(parent) / "bad.zip"
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr(f"{SID}.0000.jsonl", b"something")
            self.assertEqual(bundle.verify_bundle(dest)["status"],
                             "BUNDLE_NOT_PROVEN")
            target = Path(parent) / "alias.zip"
            target.symlink_to(dest)
            self.assertEqual(bundle.verify_bundle(target)["reason"],
                             "BUNDLE_MISSING_OR_SYMLINK")

    def test_no_package_inside_raw_tape_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            raw = Path(parent)
            sealed(raw)
            with self.assertRaisesRegex(ValueError, "NO_PACKAGE_IN_SOURCE"):
                bundle.prepare_bundle(
                    raw, session_id=SID,
                    destination=raw / "artifact.zip")


if __name__ == "__main__":
    unittest.main()
