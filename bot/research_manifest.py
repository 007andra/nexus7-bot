"""Immutable research-dataset manifest for reproducible NEXUS evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ResearchArtifact:
    source: str
    dataset: str
    symbol: str
    interval: str | None
    sha256: str
    rows: int
    first_ts: int | None
    last_ts: int | None

    def __post_init__(self) -> None:
        if not self.source or not self.dataset or not self.symbol:
            raise ValueError("research artifact identity required")
        if len(self.sha256) != 64:
            raise ValueError("research artifact sha256 required")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("invalid research artifact sha256") from exc
        if self.rows < 0:
            raise ValueError("negative research row count")
        if self.rows > 0:
            if self.first_ts is None or self.last_ts is None:
                raise ValueError("non-empty artifact needs timestamp range")
            if int(self.first_ts) > int(self.last_ts):
                raise ValueError("invalid research timestamp range")


@dataclass(frozen=True)
class ResearchManifest:
    version: str
    code_sha: str
    created_at_ms: int
    artifacts: tuple[ResearchArtifact, ...]
    instrument_snapshot_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.version or not self.code_sha or self.created_at_ms <= 0:
            raise ValueError("manifest identity required")
        identities = [
            (a.dataset, a.symbol, a.interval, a.first_ts, a.last_ts)
            for a in self.artifacts
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate artifact in manifest")

    def canonical_dict(self) -> dict:
        return {
            "version": self.version,
            "code_sha": self.code_sha,
            "created_at_ms": int(self.created_at_ms),
            "artifacts": [
                asdict(item)
                for item in sorted(
                    self.artifacts,
                    key=lambda x: (
                        x.dataset, x.symbol, x.interval or "",
                        x.first_ts or -1, x.last_ts or -1,
                    ),
                )
            ],
            "instrument_snapshot_hashes": sorted(self.instrument_snapshot_hashes),
        }

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
