import unittest

from bot.binance_collector_output_receipts_v1 import validate_collector_outputs


def fills(**changes):
    return {"status": "FILLS_WINDOWS_SHAPE_VALID", "start_ms": 1000,
            "end_ms": 1010, "terminal_page_verified": True,
            "live_allowed": False, "execution_effect": "NONE",
            "records": [{"time": 1002, "id": 1}], **changes}


def income(**changes):
    return {"status": "INCOME_WINDOW_SHAPE_VALID", "start_ms": 1000,
            "end_ms": 1010, "terminal_page_verified": True,
            "records": [], **changes}


def run(f=None, i=None):
    return validate_collector_outputs(
        fills_by_symbol={"BTCUSDT": f if f is not None else fills()},
        income_result=i if i is not None else income(),
        required_symbols=["BTCUSDT"], start_ms=1000, end_ms=1010)


class CollectorOutputReceiptTests(unittest.TestCase):
    def test_bounded_collector_output_roundtrip(self):
        result = run()
        self.assertEqual(result["status"], "COLLECTOR_RECEIPTS_SHAPE_VALID")
        self.assertFalse(result["live_allowed"])

    def test_unverified_fills_terminal(self):
        self.assertEqual(run(f=fills(terminal_page_verified=False))["status"],
                         "PROOF_MISSING")

    def test_incorrect_income_window(self):
        self.assertEqual(run(i=income(end_ms=1011))["status"], "PROOF_MISSING")

    def test_missing_fills_records(self):
        self.assertEqual(run(f=fills(records=None))["status"], "PROOF_MISSING")

    def test_income_not_terminal(self):
        self.assertEqual(run(i=income(terminal_page_verified=False))["status"],
                         "PROOF_MISSING")


    def test_live_capable_fills_rejected(self):
        self.assertEqual(run(f=fills(live_allowed=True))["status"], "PROOF_MISSING")

    def test_income_execution_effect_rejected(self):
        self.assertEqual(run(i=income(execution_effect="ORDER"))["status"],
                         "PROOF_MISSING")


if __name__ == "__main__":
    unittest.main()
