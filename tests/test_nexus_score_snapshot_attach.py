"""Audit P1-2: explicit, tested replacement for a silent except in NEXUS consistency."""
import unittest
from dataclasses import dataclass

from bot.nexus_decision_consistency import _attach_score_snapshot


class _Log:
    def __init__(self):
        self.debugs = []

    def debug(self, msg, *args):
        self.debugs.append(msg % args)


@dataclass(frozen=True)
class _Frozen:
    ok: bool = True


class _Plain:
    pass


class _Exploding:
    def __setattr__(self, name, value):
        raise RuntimeError("boom")


class AttachScoreSnapshotTests(unittest.TestCase):
    def test_attaches_on_mutable_decision(self):
        d = _Plain()
        self.assertTrue(_attach_score_snapshot(d, {"score": 70}, _Log()))
        self.assertEqual(d._bgx_score_snapshot, {"score": 70})

    def test_frozen_and_raising_decisions_are_unchanged_and_reported(self):
        for decision in (_Frozen(), _Exploding(), {"k": 1}):
            log = _Log()
            self.assertFalse(_attach_score_snapshot(decision, {"score": 1}, log))
            self.assertEqual(len(log.debugs), 1)
            self.assertIn("decision_effect=NONE", log.debugs[0])

    def test_logger_without_debug_does_not_raise(self):
        self.assertFalse(_attach_score_snapshot(_Frozen(), {}, object()))

    def test_silent_except_audit_no_longer_flags_this_module(self):
        import logging
        lines = []

        class H(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        logger = logging.getLogger("silent_except_audit_test")
        logger.addHandler(H())
        logger.setLevel(logging.INFO)
        from bot.silent_except_audit import audit_silent_excepts
        audit_silent_excepts(logger)
        for mod in ("nexus_decision_consistency.py", "final_loss_budget.py"):
            self.assertFalse([l for l in lines if f"file={mod}" in l], mod)


if __name__ == "__main__":
    unittest.main()
