"""Task-local research scope; no execution authority or logger reconfiguration."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

POPULATION = "HARD_GATE_SHADOW"
AUTHORITY = {
    "shadow_only": True, "population": POPULATION,
    "live_entries_blocked": True, "live_block_reason": "DRAWDOWN_HARD_GATE",
    "live_eligible": False, "live_candidate": False,
    "decision_effect": "NONE", "execution_effect": "NONE",
    "live_authority_unchanged": True,
}


@dataclass
class Observation:
    signal: object = None
    pullback: str = "NOT_APPLICABLE"


_CURRENT = ContextVar("hard_gate_shadow_context", default=None)


def active():
    return _CURRENT.get() is not None


def observation():
    return _CURRENT.get()


@contextmanager
def scope():
    value = Observation()
    token = _CURRENT.set(value)
    try:
        yield value
    finally:
        _CURRENT.reset(token)


def mark(signal):
    for key, value in AUTHORITY.items():
        setattr(signal, key, value)
    signal.evaluation_context = POPULATION
    return signal
