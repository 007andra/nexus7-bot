import unittest

from bot import runtime_truth as truth
from bot import runtime_truth_rest as truth_rest


class RuntimeTruthProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.old_enabled = truth._ENABLED
        self.old_recorder = truth.RECORDER
        truth._ENABLED = True
        truth.RECORDER = truth.Recorder()
        truth.RECORDER.enabled = True

    def tearDown(self):
        truth._ENABLED = self.old_enabled
        truth.RECORDER = self.old_recorder

    def test_rest_raw_row5_row6_fields_are_preserved_per_candle(self):
        raw = (
            b'{"code":"200000","data":['
            b'[1790178300000,"65","66","63","64","28469","169529.255"],'
            b'[1790177400000,"66","67","64","65","19434","117511.153"]]}'
        )
        fields = truth_rest.remember_raw_kline_fields(raw)
        self.assertEqual(fields[1790178300000]["rest_row_5_volume"], "28469")
        self.assertEqual(fields[1790178300000]["rest_row_6_turnover"], "169529.255")
        self.assertEqual(fields[1790177400000]["rest_row_5_volume"], "19434")
        self.assertEqual(fields[1790177400000]["rest_row_6_turnover"], "117511.153")
        self.assertEqual(truth_rest.current_raw_kline_fields(), fields)


if __name__ == "__main__":
    unittest.main()
