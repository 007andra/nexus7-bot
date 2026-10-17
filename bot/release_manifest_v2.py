"""Deterministic release manifest for reproducible NEXUS deployments."""
from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence


def build_release_manifest(*, sha: str, symbols: Sequence[str], parameters: Mapping[str, object], risk_policy: Mapping[str, object], sizing_policy: Mapping[str, object], drawdown_policy: Mapping[str, object], execution_chain: Sequence[str], strategy_version: str, nexus_version: str, risk_version: str, execution_model_version: str, dataset_fingerprint: str | None = None) -> dict[str, object]:
    if not sha.strip():
        raise ValueError("sha is required")
    payload = {
        "sha": sha,
        "symbols": sorted({str(x).upper() for x in symbols}),
        "parameters": dict(parameters),
        "risk_policy": dict(risk_policy),
        "sizing_policy": dict(sizing_policy),
        "drawdown_policy": dict(drawdown_policy),
        "execution_chain": list(execution_chain),
        "versions": {"strategy": strategy_version, "nexus": nexus_version, "risk": risk_version, "execution_model": execution_model_version},
        "dataset_fingerprint": dataset_fingerprint,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["manifest_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload
