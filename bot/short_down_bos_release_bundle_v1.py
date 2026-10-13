"""Offline-only export of a verified public-market tape as a GitHub Release candidate.

Never uploads or publishes anything. GitHub repository 007andra/nexus7-bot
is PUBLIC: attaching this archive to a release WOULD expose all its entries.
Source consistency ≠ independent Binance authentication or durable storage.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import zipfile

from bot import short_down_bos_event_archive_v1 as tape

MAX_MANIFEST_BYTES = 250_000
MAX_RAW_BYTES = tape.MAX_SESSION_BYTES + MAX_MANIFEST_BYTES
MAX_ZIP_BYTES = MAX_RAW_BYTES + 1_000_000

AUTHORITY = {
    "research_only": True,
    "public_repository": True,
    "release_visibility": "PUBLIC",
    "github_release_created": False,
    "github_asset_uploaded": False,
    "storage_cost_verified_zero": False,
    "source_independently_attested": False,
    "durable_remote_copy_proven": False,
    "real_fills_proven": False,
    "promotion_allowed": False,
    "live_allowed": False,
    "execution_effect": "NONE",
    "decision_effect": "NONE",
}


def _sha(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            data = stream.read(128 * 1024)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def _member_names(session_id: str, metadata: dict):
    segments = metadata.get("segments", [])
    if not isinstance(segments, list) or not 1 <= len(segments) <= tape.MAX_SEGMENTS:
        raise ValueError("INVALID_SEGMENT_LIST")
    manifest = session_id + ".manifest.json"
    expected = [manifest]
    for index, segment in enumerate(segments):
        name = f"{session_id}.{index:04d}.jsonl"
        if not isinstance(segment, dict) or segment.get("file") != name:
            raise ValueError("INVALID_SEGMENT_PATH")
        expected.append(name)
    return expected


def verify_bundle(bundle):
    """Verify only locally self-chained archive and safe/complete zip structure."""
    def deny(reason):
        return {**AUTHORITY, "status": "BUNDLE_NOT_PROVEN", "reason": reason}
    bundle = Path(bundle)
    if not bundle.is_file() or bundle.is_symlink():
        return deny("BUNDLE_MISSING_OR_SYMLINK")
    try:
        if bundle.stat().st_size > MAX_ZIP_BYTES:
            return deny("ZIP_OVER_BUDGET")
        with zipfile.ZipFile(bundle, "r") as z:
            names = z.namelist()
            if len(names) < 2 or len(names) > tape.MAX_SEGMENTS + 1:
                return deny("INVALID_MEMBER_COUNT")
            if len(set(names)) != len(names):
                return deny("DUPLICATED_MEMBER")
            if any("/" in name or "\\" in name or name.startswith(".") for name in names):
                return deny("UNSAFE_MEMBER")
            manifests = [name for name in names if name.endswith(".manifest.json")]
            if len(manifests) != 1:
                return deny("MISSING_MANIFEST")
            manifest = manifests[0]
            session = manifest.removesuffix(".manifest.json")
            # Trusted parser accepts exact, bounded session ID token only.
            if not tape._NAME.fullmatch(session):
                return deny("INVALID_SESSION")
            sizes = [x.file_size for x in z.infolist()]
            if any(size < 0 for size in sizes) or sum(sizes) > MAX_RAW_BYTES:
                return deny("EXTRACT_BUDGET_EXCEEDED")
            # Do not accept symlinks or special POSIX mode entries in ZIP.
            if any(((x.external_attr >> 16) & 0o170000) not in (0, 0o100000)
                   for x in z.infolist()):
                return deny("UNSAFE_FILE_TYPE")
            raw = z.read(manifest)
            if len(raw) > MAX_MANIFEST_BYTES:
                return deny("MANIFEST_TOO_LARGE")
            import json
            metadata = json.loads(raw)
            expected = _member_names(session, metadata)
            if names != expected:
                return deny("MISSING_OR_REORDERED_SEGMENTS")
            with tempfile.TemporaryDirectory(prefix="nexus-proof-") as temp:
                directory = Path(temp)
                for item in z.infolist():
                    # Filenames and size now validated, extraction controlled.
                    (directory / item.filename).write_bytes(z.read(item.filename))
                checked = tape.verify(directory, session_id=session)
                if checked.get("status") != "SELF_CHAIN_VERIFIED_UNATTESTED":
                    return deny("CHAIN_UNVERIFIED")
                receipt = tape.prepare_anchor_receipt(directory, session_id=session)
                if receipt.get("status") != "ANCHOR_PREPARED_NOT_PUBLISHED":
                    return deny("RECEIPT_UNVERIFIED")
                return {
                    **AUTHORITY,
                    "status": "BUNDLE_SELF_VERIFIED_NOT_UPLOADED",
                    "event_count": checked["event_count"],
                    "manifest_sha256": receipt["manifest_sha256"],
                    "final_event_chain_sha256": checked["final_hash"],
                    "bundle_sha256": _sha(bundle),
                    "compressed_bytes": bundle.stat().st_size,
                }
    except (OSError, ValueError, KeyError, TypeError, IndexError,
            zipfile.BadZipFile, RuntimeError):
        return deny("UNREADABLE_OR_CORRUPT_ZIP")


def prepare_bundle(directory, *, session_id, destination):
    """Make a bounded, locally verified release *candidate*, never upload it."""
    checked = tape.verify(directory, session_id=session_id)
    if checked.get("status") != "SELF_CHAIN_VERIFIED_UNATTESTED":
        raise ValueError("SOURCE_CHAIN_NOT_VERIFIED")
    root = Path(directory)
    receipt = tape.prepare_anchor_receipt(root, session_id=session_id)
    if receipt.get("status") != "ANCHOR_PREPARED_NOT_PUBLISHED":
        raise ValueError("SOURCE_RECEIPT_MISSING")
    import json
    metadata = json.loads((root / f"{session_id}.manifest.json").read_bytes())
    names = _member_names(session_id, metadata)
    destination = Path(destination)
    if destination.parent.resolve() == root.resolve():
        raise ValueError("NO_PACKAGE_IN_SOURCE_DIRECTORY")
    if not destination.parent.is_dir() or destination.is_symlink():
        raise ValueError("DESTINATION_DIRECTORY_UNAVAILABLE")
    created = False
    try:
        # 'x' refuses overwrite: no mutation to existing tapes/bundles.
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=False) as out:
            created = True
            for name in names:
                member = root / name
                if member.is_symlink() or not member.is_file():
                    raise ValueError("SOURCE_MEMBER_UNSAFE")
                max_size = MAX_MANIFEST_BYTES if name.endswith(".json") else tape.MAX_SESSION_BYTES
                if member.stat().st_size > max_size:
                    raise ValueError("SOURCE_MEMBER_EXCEEDS_BUDGET")
                out.write(member, arcname=name)
        evidence = verify_bundle(destination)
        if evidence["status"] != "BUNDLE_SELF_VERIFIED_NOT_UPLOADED":
            raise ValueError("PACKAGE_DID_NOT_REVERIFY")
        if evidence["manifest_sha256"] != receipt["manifest_sha256"]:
            raise ValueError("PACKAGE_RECEIPT_CHANGED")
        return evidence
    except BaseException:
        # Never unlink a pre-existing target when exclusive creation fails.
        if created and destination.exists() and destination.is_file():
            destination.unlink()
        raise
