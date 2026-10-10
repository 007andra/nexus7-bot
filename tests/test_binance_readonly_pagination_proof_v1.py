"""Offline pagination proof regression tests."""
import unittest

from bot.binance_readonly_pagination_proof_v1 import validate_pages


def run(pages):
    return validate_pages(pages=pages, window_start_ms=1000,
                          window_end_ms=3000, page_limit=2, id_field="id")


class PaginationProofTests(unittest.TestCase):
    def test_full_then_partial_terminal_page(self):
        self.assertEqual(run([[{"id": 1, "time": 1000}, {"id": 2, "time": 2000}],
                              [{"id": 3, "time": 3000}]])["status"],
                         "PAGINATION_SHAPE_VALID")

    def test_full_terminal_page_not_complete(self):
        self.assertEqual(run([[{"id": 1, "time": 1000},
                               {"id": 2, "time": 2000}]])["status"], "PROOF_MISSING")

    def test_duplicate_page_boundary_fails(self):
        self.assertEqual(run([[{"id": 1, "time": 1000}, {"id": 2, "time": 2000}],
                              [{"id": 2, "time": 2000}]])["status"], "PROOF_MISSING")

    def test_out_of_order_fails(self):
        self.assertEqual(run([[{"id": 2, "time": 2000},
                               {"id": 1, "time": 1000}], []])["status"], "PROOF_MISSING")

    def test_missing_page_fails(self):
        self.assertEqual(run([])["status"], "PROOF_MISSING")

    def test_empty_terminal_page_is_valid(self):
        self.assertEqual(run([[]])["status"], "PAGINATION_SHAPE_VALID")


if __name__ == "__main__":
    unittest.main()
