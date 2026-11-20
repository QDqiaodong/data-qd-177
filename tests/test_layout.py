import json
import unittest
from pathlib import Path
from sawmill.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class CutTests(unittest.TestCase):
    def test_actual_coordinates_and_conservation(self):
        result = run(fixture())
        self.assertTrue(result["summary"]["complete"])
        self.assertEqual(result["summary"]["delivered_pieces"], 5)
        self.assertEqual(result["summary"]["kerf_loss_mm"], 15)
        self.assertEqual(result["summary"]["reusable_offcut_mm"], 171)
        for ticket in result["tickets"]:
            cursor = ticket["trim_head_mm"]
            for cut in ticket["cuts"]:
                self.assertEqual(cut["start_mm"], cursor)
                cursor = cut["kerf_end_mm"]
            self.assertEqual(ticket["stock_length_mm"],
                             cursor + ticket["offcut_length_mm"] + ticket["trim_tail_mm"])

    def test_last_piece_still_requires_kerf(self):
        data = fixture()
        data["stock"] = [{"id": "a", "material": "oak", "length_mm": 400, "trim_head_mm": 0, "trim_tail_mm": 0}]
        data["orders"] = [{"id": "x", "material": "oak", "piece_mm": 400, "quantity": 1}]
        result = run(data)
        self.assertFalse(result["summary"]["complete"])
        self.assertEqual(result["unused_stock_ids"], ["a"])
        data["kerf_mm"] = 0
        self.assertTrue(run(data)["summary"]["complete"])

    def test_material_not_substituted(self):
        data = fixture()
        data["orders"][0]["material"] = "pine"
        result = run(data)
        self.assertEqual(result["summary"]["unfilled_pieces"], 4)
        self.assertEqual(result["summary"]["delivered_pieces"], 1)

    def test_input_order_does_not_change_tickets(self):
        data = fixture()
        before = run(data)
        data["stock"].reverse()
        data["orders"].reverse()
        self.assertEqual(before, run(data))

    def test_bad_trim_and_duplicate_id_rejected(self):
        data = fixture()
        data["stock"][0]["trim_head_mm"] = 1200
        with self.assertRaisesRegex(ValueError, "trims"):
            run(data)
        data = fixture()
        data["stock"].append(data["stock"][0].copy())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            run(data)
