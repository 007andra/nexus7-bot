"""Logging-only normalization for authoritative LIVE sizing semantics.

Installed before runtime bootstrap. A LogRecordFactory wrapper is used because
filters attached to the root logger are not applied to records emitted by child
loggers that propagate to root handlers, and handlers may be created after
sitecustomize runs. Execution behavior is untouched.

Since F-003 the only sizing authority is the stop-loss risk budget
(RISK_BUDGET_V3: equity x MAX_RISK_PCT, MAX_MARGIN_PCT as a ceiling). Legacy
messages that still describe a percentage-of-available margin or notional
target are rewritten so logs never claim an authority that no longer exists.
"""
from __future__ import annotations

import logging

_AUTHORITY = "sizing_authority=RISK_BUDGET_V3 margin_role=CEILING"


def normalize_record(record: logging.LogRecord) -> logging.LogRecord:
    msg = str(record.msg)

    if "[PILOT_LEGACY_TARGET]" in msg:
        record.msg = (
            "[SIZING_SEMANTICS] legacy_target_telemetry=true authoritative_policy=risk_budget "
            f"{_AUTHORITY} execution_effect=NONE"
        )
        record.args = ()
        return record

    for legacy in ("sizing_authority=RiskManagerV3_plus_operator_50pct_margin_cap",
                   "sizing_authority=OPERATOR_50PCT_EQUITY risk_manager_role=VALIDATION_GATE",
                   "sizing_authority=OPERATOR_50PCT_EQUITY"):
        if legacy in msg:
            msg = msg.replace(legacy, _AUTHORITY)
            record.msg = msg
    return record


class SizingSemanticsFilter(logging.Filter):
    """Compatibility surface for unit tests and explicitly attached handlers."""
    def filter(self, record: logging.LogRecord) -> bool:
        normalize_record(record)
        return True


def install() -> None:
    """Normalize every subsequently-created LogRecord, independent of logger setup."""
    root = logging.getLogger()
    if getattr(root, "_sizing_semantics_filter_installed", False):
        return

    previous_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        return normalize_record(previous_factory(*args, **kwargs))

    logging.setLogRecordFactory(_factory)
    root._sizing_semantics_filter_installed = True
