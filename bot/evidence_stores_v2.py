"""Append-only stores for NEXUS research and release evidence.

These stores persist telemetry only. They expose no trading decision or exchange
mutation interface and use conflict-ignore semantics to preserve immutability.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Mapping

from bot.binance_execution_parity import ExecutionPlan
from bot.experiment_registry import ExperimentRecord


def _canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


async def persist_execution_plan(
    db,
    *,
    candidate_id: str,
    stage: str,
    plan: ExecutionPlan,
    timestamp: float | None = None,
) -> bool:
    if not str(candidate_id).strip():
        raise ValueError("candidate_id is required")
    if stage not in {"backtest", "shadow", "paper", "live"}:
        raise ValueError("invalid parity stage")

    plan.validate()
    payload = {
        field: getattr(plan, field)
        for field in plan.__dataclass_fields__
    }
    raw = _canonical(payload)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    ddl = """CREATE TABLE IF NOT EXISTS execution_parity_evidence (
        candidate_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        timestamp REAL NOT NULL,
        plan_json TEXT NOT NULL,
        plan_sha256 TEXT NOT NULL,
        PRIMARY KEY(candidate_id,stage)
    )"""
    await db._exec(ddl)
    return bool(
        await db._exec(
            """INSERT INTO execution_parity_evidence
               (candidate_id,stage,timestamp,plan_json,plan_sha256)
               VALUES (?,?,?,?,?)
               ON CONFLICT(candidate_id,stage) DO NOTHING""",
            (
                str(candidate_id),
                stage,
                float(time.time() if timestamp is None else timestamp),
                raw,
                digest,
            ),
        )
    )


async def persist_experiment(db, record: ExperimentRecord) -> bool:
    experiment_id = record.experiment_id()
    raw = _canonical(record.canonical_payload())

    ddl = """CREATE TABLE IF NOT EXISTS nexus_experiment_registry (
        experiment_id TEXT PRIMARY KEY,
        created_epoch REAL NOT NULL,
        payload_json TEXT NOT NULL
    )"""
    await db._exec(ddl)
    return bool(
        await db._exec(
            """INSERT INTO nexus_experiment_registry
               (experiment_id,created_epoch,payload_json)
               VALUES (?,?,?)
               ON CONFLICT(experiment_id) DO NOTHING""",
            (experiment_id, time.time(), raw),
        )
    )


async def persist_release_manifest(
    db,
    manifest: Mapping[str, object],
    *,
    created_epoch: float | None = None,
) -> bool:
    digest = str(manifest.get("manifest_sha256") or "")
    sha = str(manifest.get("sha") or "")
    if len(digest) != 64 or not sha:
        raise ValueError("valid manifest_sha256 and sha are required")

    raw = _canonical(dict(manifest))
    ddl = """CREATE TABLE IF NOT EXISTS nexus_release_manifests (
        manifest_sha256 TEXT PRIMARY KEY,
        sha TEXT NOT NULL,
        created_epoch REAL NOT NULL,
        payload_json TEXT NOT NULL
    )"""
    await db._exec(ddl)
    return bool(
        await db._exec(
            """INSERT INTO nexus_release_manifests
               (manifest_sha256,sha,created_epoch,payload_json)
               VALUES (?,?,?,?)
               ON CONFLICT(manifest_sha256) DO NOTHING""",
            (
                digest,
                sha,
                float(time.time() if created_epoch is None else created_epoch),
                raw,
            ),
        )
    )
