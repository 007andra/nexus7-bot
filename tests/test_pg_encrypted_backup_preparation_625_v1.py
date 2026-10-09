"""Non-production safety tests: NO Railway DB, Binance or real secrets."""
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

from tools.backup import pg_encrypted_backup_v1 as b

PUB = "age1" + "q" * 59
OK_ID = "review_625_prep"
BASE_PG = {
    "PGHOST": "127.0.0.1",
    "PGPORT": "5432",
    "PGDATABASE": "nexus7_restore_test",
    "PGUSER": "test_reader",
    "PGPASSWORD": "synthetic_never_real",
}


class BackupPreparationTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name) / "out"
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)

    def _env(self, **kwargs):
        return dict(BASE_PG, **kwargs)

    @staticmethod
    def _action(action):
        return type("Args", (), {
            "action": action, "approval_ref": OK_ID, "execute": True,
        })()

    def test_default_plan_has_no_db_or_filesystem_effect(self):
        with patch.object(b, "_backup") as backup, patch.object(b, "_restore") as restore:
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(b.main([]), 0)
            self.assertEqual(
                json.loads(out.getvalue())["status"], b.PLAN_STATUS,
            )
            backup.assert_not_called()
            restore.assert_not_called()

    def test_backup_command_without_double_consent_cannot_start(self):
        out = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
            self.assertEqual(b.main(["backup"]), 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "EXPLICIT_APPROVAL_REQUIRED")
        self.assertFalse(list(self.root.iterdir()))

    def test_restore_command_without_double_consent_cannot_start(self):
        out = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
            self.assertEqual(b.main(["restore"]), 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "EXPLICIT_APPROVAL_REQUIRED")

    def test_check_pg_credentials_never_show_raw_password(self):
        env = self._env(PGOPTIONS="-c search_path=public")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(b.UnsafeOperation, "OVERRIDES_FORBIDDEN"):
                b._client_env()

    def test_cannot_restore_remote_or_non_disposable_database(self):
        env = self._env(
            PGHOST="railway-real-db.internal",
            NEXUS_APPROVE_ISOLATED_RESTORE="YES_THIS_SINGLE_RUN",
        )
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(b.UnsafeOperation, "NOT_LOOPBACK"):
                b._restore(self._action("restore"))
        env["PGHOST"] = "127.0.0.1"
        env["PGDATABASE"] = "postgres"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(b.UnsafeOperation, "NAME_INVALID"):
                b._restore(self._action("restore"))

    def test_public_recipient_cannot_be_an_age_identity_or_arbitrary_key(self):
        self.assertIsNone(b.PUBLIC_KEY.fullmatch("AGE-SECRET-KEY-" + "A" * 60))
        self.assertIsNone(b.PUBLIC_KEY.fullmatch("myrecipient"))
        self.assertIsNotNone(b.PUBLIC_KEY.fullmatch(PUB))

    def _fake_archive(self):
        directory = self.root / "nexus-hwm-synthetic"
        directory.mkdir(mode=0o700)
        encrypted = directory / b.ARCHIVE
        encrypted.write_bytes(b"synthetic ciphertext, NOT actual age encryption")
        encrypted.chmod(0o600)
        sha = hashlib.sha256(encrypted.read_bytes()).hexdigest()
        (directory / b.MANIFEST).write_text(json.dumps({
            "format": b.FMT,
            "created_utc": "2026-10-09T00:00:00+00:00",
            "encrypted_sha256": sha,
            "encrypted_bytes": encrypted.stat().st_size,
            "plaintext_persisted": False,
            "external_copy_verified": False,
            "restore_verified": False,
        }), encoding="utf-8")
        (directory / b.MANIFEST).chmod(0o600)
        return directory

    def test_manifest_detects_ciphertext_tampering(self):
        p = self._fake_archive()
        with patch.dict(os.environ, {"NEXUS_BACKUP_RESTORE_SOURCE_DIR": str(p)}):
            self.assertEqual(b._load_archive(), p / b.ARCHIVE)
            with (p / b.ARCHIVE).open("ab") as f:
                f.write(b"TAMPERED")
            with self.assertRaisesRegex(b.UnsafeOperation, "CHECKSUM_MISMATCH"):
                b._load_archive()

    def test_rejects_world_accessible_ciphertext(self):
        p = self._fake_archive()
        (p / b.ARCHIVE).chmod(0o644)
        with patch.dict(os.environ, {"NEXUS_BACKUP_RESTORE_SOURCE_DIR": str(p)}):
            with self.assertRaisesRegex(b.UnsafeOperation, "NOT_PRIVATE"):
                b._load_archive()

    def test_rejects_shared_parent_directory(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(b.UnsafeOperation, "NOT_PRIVATE"):
            b._private_dir(self.root)

    def test_rejects_symlink_cryptographic_identity(self):
        p = self._fake_archive()
        identity = self.root / "secret-identity"
        identity.write_text("synthetic age identity")
        identity.chmod(0o600)
        link = self.root / "identity-link"
        link.symlink_to(identity)
        env = self._env(
            NEXUS_APPROVE_ISOLATED_RESTORE="YES_THIS_SINGLE_RUN",
            NEXUS_BACKUP_RESTORE_SOURCE_DIR=str(p),
            NEXUS_BACKUP_AGE_IDENTITY_FILE=str(link),
        )
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(b.UnsafeOperation, "IDENTITY_UNAVAILABLE"):
                b._restore(self._action("restore"))

    def test_source_backup_streams_to_private_ciphertext_only(self):
        env = self._env(
            NEXUS_APPROVE_BACKUP_EXPORT="YES_THIS_SINGLE_RUN",
            NEXUS_BACKUP_PRIVATE_DIR=str(self.root),
            NEXUS_BACKUP_AGE_RECIPIENT=PUB,
        )
        def mock_pipe(left, right, output, **kwargs):
            self.assertEqual(left[0], "pg_dump")
            self.assertEqual(right, ["age", "--recipient", PUB])
            self.assertNotIn("synthetic_never_real", json.dumps([left, right]))
            output.write_bytes(b"dummy encrypted stream")
        out = StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(b, "_check_binaries"), \
             patch.object(b, "_client_major", return_value=18), \
             patch.object(b, "_run_pipe", side_effect=mock_pipe), \
             redirect_stdout(out):
            b._backup(self._action("backup"))
        dirs = list(self.root.iterdir())
        self.assertEqual(len(dirs), 1)
        directory = dirs[0]
        self.assertEqual([x.name for x in directory.iterdir()], [b.ARCHIVE, b.MANIFEST])
        self.assertEqual(stat.S_IMODE((directory / b.ARCHIVE).stat().st_mode), 0o600)
        data = json.loads((directory / b.MANIFEST).read_text())
        self.assertEqual(data["pg_dump_major"], 18)
        self.assertFalse(data["external_copy_verified"])
        self.assertFalse(data["restore_verified"])
        self.assertEqual(json.loads(out.getvalue())["status"], "LOCAL_ENCRYPTED_ARCHIVE_ONLY")

    def test_backup_failure_leaves_no_partial_ciphertext(self):
        env = self._env(
            NEXUS_APPROVE_BACKUP_EXPORT="YES_THIS_SINGLE_RUN",
            NEXUS_BACKUP_PRIVATE_DIR=str(self.root),
            NEXUS_BACKUP_AGE_RECIPIENT=PUB,
        )
        with patch.dict(os.environ, env, clear=True), \
             patch.object(b, "_check_binaries"), \
             patch.object(b, "_client_major", return_value=18), \
             patch.object(b, "_run_pipe", side_effect=b.UnsafeOperation("PIPELINE_EXIT_NONZERO")):
            with self.assertRaises(b.UnsafeOperation):
                b._backup(self._action("backup"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_restore_from_verified_ciphertext_only_to_empty_local_db(self):
        directory = self._fake_archive()
        identity = self.root / "identity"
        identity.write_text("synthetic test only", encoding="utf-8")
        identity.chmod(0o600)
        env = self._env(
            NEXUS_APPROVE_ISOLATED_RESTORE="YES_THIS_SINGLE_RUN",
            NEXUS_BACKUP_RESTORE_SOURCE_DIR=str(directory),
            NEXUS_BACKUP_AGE_IDENTITY_FILE=str(identity),
        )
        proc = MagicMock()
        proc.stdout = b"0\n"
        out = StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(b, "_check_binaries"), \
             patch.object(b, "_client_major", return_value=18), \
             patch.object(b.subprocess, "run", return_value=proc) as psql, \
             patch.object(b, "_restore_pipe") as decrypter, \
             redirect_stdout(out):
            b._restore(self._action("restore"))
        psql.assert_called_once()
        decrypter.assert_called_once()
        self.assertEqual(json.loads(out.getvalue())["status"],
                         "RESTORE_COMPLETED_FINANCIAL_ASSERTIONS_PENDING")

    def test_nonempty_disposable_target_refuses_restore(self):
        directory = self._fake_archive()
        identity = self.root / "identity"
        identity.write_text("synthetic test only", encoding="utf-8")
        identity.chmod(0o600)
        env = self._env(
            NEXUS_APPROVE_ISOLATED_RESTORE="YES_THIS_SINGLE_RUN",
            NEXUS_BACKUP_RESTORE_SOURCE_DIR=str(directory),
            NEXUS_BACKUP_AGE_IDENTITY_FILE=str(identity),
        )
        proc = MagicMock()
        proc.stdout = b"1\n"
        with patch.dict(os.environ, env, clear=True), \
             patch.object(b, "_check_binaries"), \
             patch.object(b, "_client_major", return_value=18), \
             patch.object(b.subprocess, "run", return_value=proc), \
             patch.object(b, "_restore_pipe") as decrypter:
            with self.assertRaisesRegex(b.UnsafeOperation, "NOT_EMPTY"):
                b._restore(self._action("restore"))
        decrypter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
