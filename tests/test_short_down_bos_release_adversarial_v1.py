"""Adversarial #610 public-export regressions: timing, ZIP shape, privacy.

Synthetic fixtures only. Passing these proves fail-closed handling of hostile
inputs, never that a real Binance capture exists or is authentic.
"""
import json
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from bot import short_down_bos_event_archive_v1 as tape
from bot import short_down_bos_release_bundle_v1 as bundle

SID = "research-20261013-adv1"
MS = 1_800_000_000_000
SYMBOL = "BTCUSDT"


def event(i):
    return {"e": "aggTrade", "E": MS + i * 10 + 2, "s": SYMBOL,
            "a": 1000 + i, "p": "101.5", "q": "0.20",
            "f": 2000 + i, "l": 2000 + i, "T": MS + i * 10, "m": False}


def sealed(directory, n=3):
    w = tape.Archive(directory, symbol=SYMBOL, session_id=SID,
                     generation=1, started_ms=MS)
    for i in range(n):
        w.append(event(i), received_ms=MS + 10 * i + 2)
    return w.seal()


def rehash(origin, mutate):
    """Rebuild a fully self-consistent chain after `mutate(rows)`."""
    segment = origin / f"{SID}.0000.jsonl"
    rows = [json.loads(x) for x in segment.read_bytes().splitlines()]
    mutate(rows)
    previous = rows[0]["prev"]
    for row in rows:
        row["prev"] = previous
        item = {k: row[k] for k in ("seq", "received_ms", "event")}
        row["hash"] = tape._hash(previous, item)
        previous = row["hash"]
    data = b"".join(tape._canonical(r) + b"\n" for r in rows)
    segment.write_bytes(data)
    manifest = origin / f"{SID}.manifest.json"
    meta = json.loads(manifest.read_bytes())
    meta["final_hash"] = previous
    meta["archive_data_bytes"] = len(data)
    manifest.write_bytes(tape._canonical(meta) + b"\n")


class ReceiptTimingTests(unittest.TestCase):
    def _writer(self, d):
        return tape.Archive(d, symbol=SYMBOL, session_id=SID,
                            generation=1, started_ms=MS)

    def test_receipt_clock_going_backwards_aborts_session(self):
        with tempfile.TemporaryDirectory() as d:
            w = self._writer(d)
            w.append(event(0), received_ms=MS + 900)
            with self.assertRaises(ValueError):
                w.append(event(1), received_ms=MS + 100)
            with self.assertRaises(ValueError):
                w.seal()
            self.assertFalse(list(Path(d).glob("*.manifest.json")))

    def test_stale_backlogged_receipt_aborts_session(self):
        with tempfile.TemporaryDirectory() as d:
            w = self._writer(d)
            w.append(event(0), received_ms=MS + 2)
            lag = tape.MAX_RECEIVE_LAG_MS + 1
            with self.assertRaises(ValueError):
                w.append(event(1), received_ms=MS + 10 + lag)

    def test_trade_time_far_ahead_of_receipt_aborts_session(self):
        with tempfile.TemporaryDirectory() as d:
            w = self._writer(d)
            future = dict(event(0), T=MS + tape.MAX_CLOCK_LEAD_MS + 50)
            with self.assertRaises(ValueError):
                w.append(future, received_ms=MS + 10)

    def test_rehashed_chain_with_backward_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            origin = Path(d)
            sealed(origin)

            def backwards(rows):
                rows[2]["received_ms"] = rows[0]["received_ms"]
            rehash(origin, backwards)
            self.assertEqual(tape.verify(origin, session_id=SID)["reason"],
                             "EVENT_TAMPER_OR_GAP")

    def test_rehashed_chain_with_out_of_order_trade_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            origin = Path(d)
            sealed(origin)

            def disorder(rows):
                rows[2]["event"]["T"] = rows[0]["event"]["T"] - 1
            rehash(origin, disorder)
            self.assertEqual(tape.verify(origin, session_id=SID)["status"],
                             "NOT_PROVEN")

    def test_rehashed_chain_with_agg_id_gap_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            origin = Path(d)
            sealed(origin)

            def gap(rows):
                rows[2]["event"]["a"] += 1
            rehash(origin, gap)
            self.assertEqual(tape.verify(origin, session_id=SID)["status"],
                             "NOT_PROVEN")


class ZipShapeTests(unittest.TestCase):
    def _bundle(self, root):
        origin = root / "raw"
        origin.mkdir()
        sealed(origin)
        out = root / "out"
        out.mkdir()
        target = out / "ok.zip"
        bundle.prepare_bundle(origin, session_id=SID, destination=target)
        return origin, target

    def _members(self, origin):
        return [origin / f"{SID}.manifest.json", origin / f"{SID}.0000.jsonl"]

    def _assert_denied(self, path, reason=None):
        proof = bundle.verify_bundle(path)
        self.assertEqual(proof["status"], "BUNDLE_NOT_PROVEN")
        self.assertFalse(proof["live_allowed"])
        self.assertFalse(proof["github_asset_uploaded"])
        if reason:
            self.assertEqual(proof["reason"], reason)

    def test_duplicate_member_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, _ = self._bundle(root)
            bad = root / "dup.zip"
            with zipfile.ZipFile(bad, "w") as z:
                for m in self._members(origin):
                    z.write(m, arcname=m.name)
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    z.write(self._members(origin)[1],
                            arcname=self._members(origin)[1].name)
            self._assert_denied(bad, "DUPLICATED_MEMBER")

    def test_absolute_and_traversal_paths_rejected_without_writing(self):
        for evil in ("/tmp/evil.jsonl", "../evil.jsonl", "sub/evil.jsonl",
                     "..\\evil.jsonl", ".hidden"):
            with self.subTest(evil=evil), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                origin, _ = self._bundle(root)
                bad = root / "evil.zip"
                with zipfile.ZipFile(bad, "w") as z:
                    for m in self._members(origin):
                        z.write(m, arcname=m.name)
                    info = zipfile.ZipInfo(evil)
                    z.writestr(info, b"x")
                self._assert_denied(bad)
                self.assertFalse((root / "evil.jsonl").exists())

    def test_symlink_entry_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, _ = self._bundle(root)
            bad = root / "link.zip"
            with zipfile.ZipFile(bad, "w") as z:
                z.write(self._members(origin)[0],
                        arcname=self._members(origin)[0].name)
                info = zipfile.ZipInfo(self._members(origin)[1].name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(info, "/etc/passwd")
            self._assert_denied(bad)

    def test_declared_expansion_over_budget_rejected_before_extract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, _ = self._bundle(root)
            bad = root / "bomb.zip"
            with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED,
                                 allowZip64=True) as z:
                z.write(self._members(origin)[0],
                        arcname=self._members(origin)[0].name)
                with z.open(self._members(origin)[1].name, "w",
                            force_zip64=True) as w:
                    chunk = b"0" * (1024 * 1024)
                    for _ in range(bundle.MAX_RAW_BYTES // len(chunk) + 2):
                        w.write(chunk)
            self.assertLess(bad.stat().st_size, bundle.MAX_ZIP_BYTES)
            self._assert_denied(bad, "EXTRACT_BUDGET_EXCEEDED")

    def test_compressed_file_over_zip_budget_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "big.zip"
            with bad.open("wb") as f:
                f.truncate(bundle.MAX_ZIP_BYTES + 1)
            self._assert_denied(bad, "ZIP_OVER_BUDGET")

    def test_reordered_members_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, _ = self._bundle(root)
            bad = root / "order.zip"
            with zipfile.ZipFile(bad, "w") as z:
                for m in reversed(self._members(origin)):
                    z.write(m, arcname=m.name)
            self._assert_denied(bad, "MISSING_OR_REORDERED_SEGMENTS")

    def test_invalid_hash_after_repack_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, _ = self._bundle(root)
            seg = self._members(origin)[1]
            rows = [json.loads(x) for x in seg.read_bytes().splitlines()]
            rows[1]["hash"] = "0" * 64
            seg.write_bytes(b"".join(tape._canonical(r) + b"\n" for r in rows))
            bad = root / "hash.zip"
            with zipfile.ZipFile(bad, "w") as z:
                for m in self._members(origin):
                    z.write(m, arcname=m.name)
            self._assert_denied(bad)

    def test_truncated_zip_bytes_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _, good = self._bundle(root)
            bad = root / "trunc.zip"
            bad.write_bytes(good.read_bytes()[:-40])
            self._assert_denied(bad)

    def test_manifest_authority_flip_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, _ = self._bundle(root)
            manifest = self._members(origin)[0]
            meta = json.loads(manifest.read_bytes())
            meta["live_allowed"] = True
            manifest.write_bytes(tape._canonical(meta) + b"\n")
            bad = root / "flip.zip"
            with zipfile.ZipFile(bad, "w") as z:
                for m in self._members(origin):
                    z.write(m, arcname=m.name)
            self._assert_denied(bad, "PRIVATE_OR_UNEXPECTED_FIELD_IN_PUBLIC_TAPE")

    def test_credential_shaped_value_in_public_field_is_not_whitelisted(self):
        # Field-name allowlist does not inspect values: a secret smuggled in an
        # allowed key must still fail typed validation of that key.
        with tempfile.TemporaryDirectory() as d:
            origin = Path(d)
            sealed(origin)

            def smuggle(rows):
                rows[0]["event"]["p"] = "api_key=PLACEHOLDER_NOT_A_SECRET"
            rehash(origin, smuggle)
            self.assertEqual(tape.verify(origin, session_id=SID)["status"],
                             "NOT_PROVEN")

    def test_session_byte_ceiling_is_16_mib(self):
        self.assertEqual(tape.MAX_SESSION_BYTES, 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
