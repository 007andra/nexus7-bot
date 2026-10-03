"""Single observable contract for LIVE release and exchange-accounting state.

Truth established by the 2026-09-26 audit (P0-5): the LIVE pilot release gate
is exactly ``pilot_release_control.live_pilot_release_authorized()`` (explicit
operator tokens plus ``BINANCE_LIVE_MIGRATION_READY`` on Binance). Binance
accounting evidence (``binance_accounting_evidence``) is reconstructed and
persisted for audit, but nothing in the entry path reads it: it is
OBSERVABILITY_ONLY and has no execution effect.

Previously a constant log claimed ``execution_effect=BLOCK_LIVE_RELEASE`` for
accounting while ``[CONTROLLED_PILOT_RELEASE] authorized=true`` was printed in
the same process. Both lines are now rendered from one ``ReleaseContract`` that
refuses contradictory states. If accounting is ever promoted to a release
precondition it must be added to ``release_checks`` *and* flipped here, and
``validate`` keeps the two consistent.
"""
from __future__ import annotations

from dataclasses import dataclass

ACCOUNTING_ROLE = "OBSERVABILITY_ONLY"
ACCOUNTING_EVIDENCE_STATE = "AWAITING_CONTROLLED_LIVE_EVIDENCE"


class ContradictoryRuntimeState(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseContract:
    exchange: str
    release_authorized: bool
    validation_lock: bool
    missing_checks: tuple[str, ...]
    accounting_gates_release: bool = False
    accounting_role: str = ACCOUNTING_ROLE
    accounting_evidence_state: str = ACCOUNTING_EVIDENCE_STATE

    @property
    def accounting_execution_effect(self) -> str:
        if not self.accounting_gates_release:
            return "NONE"
        return "NONE" if self.release_authorized else "BLOCK_LIVE_RELEASE"

    def validate(self) -> "ReleaseContract":
        if self.release_authorized == self.validation_lock:
            raise ContradictoryRuntimeState("release_authorized and validation_lock must differ")
        if self.release_authorized and self.missing_checks:
            raise ContradictoryRuntimeState("authorized release with missing checks")
        if not self.release_authorized and not self.missing_checks:
            raise ContradictoryRuntimeState("unauthorized release without a missing check")
        if self.release_authorized and self.accounting_execution_effect != "NONE":
            raise ContradictoryRuntimeState("authorized release blocked by accounting")
        if not self.accounting_gates_release and self.accounting_role != ACCOUNTING_ROLE:
            raise ContradictoryRuntimeState("non-gating accounting must be observability-only")
        return self

    def release_log(self) -> str:
        if self.release_authorized:
            return (
                "[CONTROLLED_PILOT_RELEASE] authorized=true validation_lock=false "
                f"exchange={self.exchange} accounting_role={self.accounting_role} "
                "scope=pilot_only max_positions=2 external_positions=count_and_read_only"
            )
        return (
            "[CONTROLLED_PILOT_RELEASE] authorized=false validation_lock=true "
            f"exchange={self.exchange} missing={','.join(self.missing_checks)} "
            "execution_effect=NONE"
        )

    def accounting_log(self) -> str:
        return (
            "[BINANCE_ACCOUNTING] adapter=LIFECYCLE_READY source=BINANCE_USDM "
            "user_trades=true income=true normal_order_identity=true "
            "algo_order_identity=true lifecycle_reconstruction=true "
            "flat_anchor_proof=true durable_lineage_required=true "
            f"live_accounting_authority={str(self.accounting_gates_release).lower()} "
            f"accounting_role={self.accounting_role} "
            f"accounting_evidence_state={self.accounting_evidence_state} "
            f"release_authorized={str(self.release_authorized).lower()} "
            "paper_decisions_unchanged=true "
            f"execution_effect={self.accounting_execution_effect}"
        )


def current() -> ReleaseContract:
    from bot import exchange
    from bot import pilot_release_control

    authorized = bool(pilot_release_control.live_pilot_release_authorized())
    missing = tuple(pilot_release_control.missing_release_checks())
    return ReleaseContract(
        exchange=str(exchange.EXCHANGE_NAME),
        release_authorized=authorized,
        validation_lock=not authorized,
        missing_checks=() if authorized else (missing or ("unknown",)),
    ).validate()
