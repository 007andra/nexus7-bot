"""Preparation-only encrypted PostgreSQL backup and isolated restore runner.

This CLI is not imported by the bot. Default is an offline PLAN with NO
network/SQL/filesystem mutation. All execution requires new, explicit approval
references and individual operator-provided credentials. No commands are run
from this module in production merely by landing the file in a branch.

Raw pg_dump custom output is piped directly to age X25519 encryption; no
plaintext archive is written to disk. This is NOT a replacement for PITR.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ARCHIVE = "backup.pgdump.age"
MANIFEST = "manifest.json"
FMT = "PG_DUMP_CUSTOM_AGE_V1"
RESTORE_SUFFIX = "_restore_test"
PLAN_STATUS = "PLAN_ONLY_NO_CONNECT_NO_WRITE"
SQL_EMPTY = (
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON "
    "c.relnamespace=n.oid WHERE c.relkind IN ('r','p','v','m','S','f') "
    "AND n.nspname NOT IN ('pg_catalog','information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%'"
)
PUBLIC_KEY = re.compile(r"^age1[023456789acdefghjklmnpqrstuvwxyz]{35,100}$")


class UnsafeOperation(Exception):
    """Fail closed without leaking raw exception messages."""


def report(**fields):
    print(json.dumps(fields, sort_keys=True, allow_nan=False))


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise UnsafeOperation("MISSING_REQUIRED_CONFIGURATION")
    return value


def _client_env():
    # libpq credentials are inherited by child processes, never CLI args.
    # The caller must provide an isolated, least-privilege source identity.
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER"):
        _env(name)
    try:
        port = int(os.environ["PGPORT"])
        if not 1 <= port <= 65535:
            raise ValueError("port")
    except ValueError as exc:
        raise UnsafeOperation("INVALID_PGPORT") from exc
    if "PGPASSWORD" not in os.environ and "PGPASSFILE" not in os.environ:
        raise UnsafeOperation("PG_AUTH_SOURCE_MISSING")
    if os.environ.get("PGOPTIONS") or os.environ.get("PGSERVICE"):
        raise UnsafeOperation("PG_CONNECTION_OVERRIDES_FORBIDDEN")
    return os.environ.copy()


def _pg_args(name: str):
    return [
        name, "--host", os.environ["PGHOST"], "--port", os.environ["PGPORT"],
        "--username", os.environ["PGUSER"], "--dbname", os.environ["PGDATABASE"],
    ]


def _client_major(binary: str):
    if not shutil.which(binary):
        raise UnsafeOperation("REQUIRED_BINARY_MISSING")
    try:
        x = subprocess.run(
            [binary, "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=7, check=True,
        )
        match = re.search(rb"\b(\d{1,2})(?:\.\d+)?\b", x.stdout)
    except (subprocess.SubprocessError, OSError) as exc:
        raise UnsafeOperation("CLIENT_VERSION_CHECK_FAILED") from exc
    if not match:
        raise UnsafeOperation("CLIENT_VERSION_UNRECOGNIZED")
    return int(match.group(1))


def _check_binaries(*names):
    for n in names:
        if not shutil.which(n):
            raise UnsafeOperation("REQUIRED_BINARY_MISSING")


def _require_execute(a, env_name: str):
    if not a.execute or not a.approval_ref or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{5,79}", a.approval_ref,
    ):
        raise UnsafeOperation("EXPLICIT_APPROVAL_REQUIRED")
    if os.getenv(env_name) != "YES_THIS_SINGLE_RUN":
        raise UnsafeOperation("OPERATOR_APPROVAL_ENV_MISSING")


def _private_dir(path: Path):
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise UnsafeOperation("PRIVATE_DIRECTORY_REQUIRED")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise UnsafeOperation("DIRECTORY_PERMISSIONS_NOT_PRIVATE")
    if path.stat().st_uid != os.getuid():
        raise UnsafeOperation("PRIVATE_DIRECTORY_NOT_OWNED")


def _sha256(p: Path):
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_pipe(args_left, args_right, out, *, timeout: int = 3600):
    """Stream with NO plaintext temp file, no shell interpolation, no stderr."""
    left = right = None
    try:
        left = subprocess.Popen(
            args_left, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        with out.open("xb") as dest:
            right = subprocess.Popen(
                args_right, stdin=left.stdout, stdout=dest,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
            left.stdout.close()
            right_code = right.wait(timeout=timeout)
            left_code = left.wait(timeout=30)
            dest.flush()
            os.fsync(dest.fileno())
        if right_code != 0 or left_code != 0:
            raise UnsafeOperation("PIPELINE_EXIT_NONZERO")
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsafeOperation("PIPELINE_FAILED") from exc
    finally:
        for proc in (left, right):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def _backup(a):
    _require_execute(a, "NEXUS_APPROVE_BACKUP_EXPORT")
    env = _client_env()
    source_parent = Path(_env("NEXUS_BACKUP_PRIVATE_DIR"))
    if source_parent.is_symlink():
        raise UnsafeOperation("PRIVATE_DIRECTORY_REQUIRED")
    parent = source_parent.resolve(strict=True)
    _private_dir(parent)
    recipient = _env("NEXUS_BACKUP_AGE_RECIPIENT")
    if not PUBLIC_KEY.fullmatch(recipient):
        raise UnsafeOperation("INVALID_AGE_PUBLIC_RECIPIENT")
    _check_binaries("age", "pg_dump")
    major = _client_major("pg_dump")
    if major < 18:
        raise UnsafeOperation("PG_DUMP_18_OR_NEWER_REQUIRED")
    # The output directory is private and freshly generated. On any failure
    # delete only this run's own ciphertext and metadata, NEVER source data.
    created = Path(tempfile.mkdtemp(prefix="nexus-hwm-", dir=str(parent)))
    os.chmod(created, 0o700)
    try:
        tmp = created / "backup.partial.age"
        _run_pipe(
            _pg_args("pg_dump") + [
                "--format=custom", "--no-owner", "--no-privileges",
            ],
            ["age", "--recipient", recipient],
            tmp,
        )
        if not tmp.stat().st_size:
            raise UnsafeOperation("ENCRYPTED_ARTIFACT_EMPTY")
        target = created / ARCHIVE
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        manifest = {
            "format": FMT,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "encrypted_sha256": _sha256(target),
            "encrypted_bytes": target.stat().st_size,
            "pg_dump_major": major,
            "plaintext_persisted": False,
            "external_copy_verified": False,
            "restore_verified": False,
        }
        p = created / MANIFEST
        with p.open("x", encoding="utf-8") as out:
            json.dump(manifest, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(p, 0o600)
        report(
            status="LOCAL_ENCRYPTED_ARCHIVE_ONLY",
            sha256=manifest["encrypted_sha256"],
            ciphertext_bytes=manifest["encrypted_bytes"],
            offsite_verified=False, restore_verified=False, live_allowed=False,
        )
    except Exception:
        shutil.rmtree(created, ignore_errors=True)
        raise


def _load_archive():
    source = Path(_env("NEXUS_BACKUP_RESTORE_SOURCE_DIR"))
    if source.is_symlink():
        raise UnsafeOperation("PRIVATE_DIRECTORY_REQUIRED")
    d = source.resolve(strict=True)
    _private_dir(d)
    encrypted = d / ARCHIVE
    man = d / MANIFEST
    if any(x.is_symlink() or not x.is_file() for x in (encrypted, man)):
        raise UnsafeOperation("BACKUP_ARTIFACTS_MISSING")
    if stat.S_IMODE(encrypted.stat().st_mode) & 0o077:
        raise UnsafeOperation("ARCHIVE_PERMISSIONS_NOT_PRIVATE")
    try:
        doc = json.loads(man.read_text(encoding="utf-8"))
        valid = (
            doc.get("format") == FMT
            and doc.get("plaintext_persisted") is False
            and doc.get("encrypted_bytes") == encrypted.stat().st_size
            and bool(re.fullmatch("[a-f0-9]{64}", doc["encrypted_sha256"]))
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid or _sha256(encrypted) != doc["encrypted_sha256"]:
        raise UnsafeOperation("ENCRYPTED_CHECKSUM_MISMATCH")
    return encrypted


def _restore(a):
    _require_execute(a, "NEXUS_APPROVE_ISOLATED_RESTORE")
    _client_env()
    if os.environ["PGHOST"] not in ("localhost", "127.0.0.1", "::1"):
        raise UnsafeOperation("RESTORE_DESTINATION_NOT_LOOPBACK")
    if not os.environ["PGDATABASE"].endswith(RESTORE_SUFFIX):
        raise UnsafeOperation("RESTORE_DESTINATION_NAME_INVALID")
    if os.environ["PGDATABASE"] == RESTORE_SUFFIX:
        raise UnsafeOperation("RESTORE_DESTINATION_NAME_INVALID")
    identity_source = Path(_env("NEXUS_BACKUP_AGE_IDENTITY_FILE"))
    if identity_source.is_symlink():
        raise UnsafeOperation("DECRYPTION_IDENTITY_UNAVAILABLE")
    identity = identity_source.resolve(strict=True)
    if not identity.is_file():
        raise UnsafeOperation("DECRYPTION_IDENTITY_UNAVAILABLE")
    if stat.S_IMODE(identity.stat().st_mode) & 0o077:
        raise UnsafeOperation("DECRYPTION_IDENTITY_NOT_PRIVATE")
    encrypted = _load_archive()
    _check_binaries("age", "pg_restore", "psql")
    if _client_major("pg_restore") < 18:
        raise UnsafeOperation("PG_RESTORE_18_OR_NEWER_REQUIRED")
    # Destination is an explicitly pre-created local disposable DB. Never
    # run pg_restore --create or --clean against unknown existing data.
    try:
        check = subprocess.run(
            _pg_args("psql") + [
                "--no-psqlrc", "--tuples-only", "--no-align",
                "--command", SQL_EMPTY,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=True,
        )
        empty = check.stdout.strip() == b"0"
    except (subprocess.SubprocessError, OSError) as exc:
        raise UnsafeOperation("ISOLATED_DB_PREFLIGHT_FAILED") from exc
    if not empty:
        raise UnsafeOperation("RESTORE_DESTINATION_NOT_EMPTY")
    _restore_pipe(a, encrypted, identity)
    report(
        status="RESTORE_COMPLETED_FINANCIAL_ASSERTIONS_PENDING",
        checksum_verified=True, finance_invariants_verified=False,
        source_untouched=True, live_allowed=False,
    )


def _restore_pipe(a, encrypted, identity):
    """Never materialize a decrypted backup, even for restore."""
    dec = restore = None
    try:
        dec = subprocess.Popen(
            ["age", "--decrypt", "--identity", str(identity), str(encrypted)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        restore = subprocess.Popen(
            _pg_args("pg_restore") + [
                "--exit-on-error", "--single-transaction",
                "--no-owner", "--no-privileges",
            ],
            stdin=dec.stdout, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dec.stdout.close()
        restored = restore.wait(timeout=3600)
        decrypted = dec.wait(timeout=30)
        if restored or decrypted:
            raise UnsafeOperation("ISOLATED_RESTORE_PIPELINE_FAILED")
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsafeOperation("ISOLATED_RESTORE_PIPELINE_FAILED") from exc
    finally:
        for proc in (dec, restore):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preparation-only encrypted PG backup safety runner")
    parser.add_argument("action", choices=("plan", "backup", "restore"), nargs="?", default="plan")
    parser.add_argument("--execute", action="store_true", default=False)
    parser.add_argument("--approval-ref")
    a = parser.parse_args(argv)
    if a.action == "plan":
        report(
            status=PLAN_STATUS, source_connected=False,
            filesystem_mutated=False, backup_performed=False,
            restore_performed=False, live_allowed=False,
        )
        return 0
    try:
        if a.action == "backup":
            _backup(a)
        elif a.action == "restore":
            _restore(a)
    except UnsafeOperation as exc:
        report(status="REFUSED", reason=str(exc), restore_pass=False,
               offsite_verified=False, live_allowed=False)
        return 2
    except Exception:
        # Never log native subprocess error, DSN or the raw decrypted stream.
        report(status="FAILED_CLOSED", live_allowed=False)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
