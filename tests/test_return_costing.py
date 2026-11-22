"""Costing across customer returns: lossless value transfer and close bridges.

The reference amounts are hand-computed independently of the production
costing code: the 11mm acceptance flow is checked cent by cent, including the
two close-chain variants (with and without an intermediate close at the
returned-but-not-recut snapshot), and every projection is re-audited for
per-root conservation.
"""
import json
import tempfile
import unittest
from pathlib import Path

from sawmill import workspace

from test_returns import (ReturnCase, order, stock_row, salvage_request)


def price(price_id, stock_id, amount_cents, expected_price_revision):
    return {"price_id": price_id, "stock_id": stock_id,
            "amount_cents": amount_cents,
            "expected_price_revision": expected_price_revision}


def close(close_id, previous, inventory_revision, price_revision):
    return {"close_id": close_id, "previous_close_id": previous,
            "expected_inventory_revision": inventory_revision,
            "expected_price_revision": price_revision}


class ReturnCostCase(ReturnCase):
    def register_price(self, value):
        self.seq += 1
        return workspace.register_price(
            str(self.ws), self.source(value),
            self.out(f"price-{self.seq}.json"))

    def close_run(self, value):
        self.seq += 1
        return workspace.close_run(
            str(self.ws), self.source(value),
            self.out(f"close-{self.seq}.json"))

    def piece_amounts(self, detail):
        return {(r["job_id"], r["entry_id"], r["order_id"], r["unit"]):
                r["amount_cents"] for r in detail["pieces"]}

    def inventory_amounts(self, detail):
        return {r["entry_id"]: r["amount_cents"] for r in detail["inventory"]}

    def bridge(self, result):
        return {line["line_id"]: line for line in result["bridge"]["lines"]}


class HandComputedFlowTests(ReturnCostCase):
    """The exact 11mm example from the acceptance statement."""

    def build(self):
        self.init({"stock": [stock_row("R1", 11)]})
        self.register_price(price("P1", "R1", 1, 0))
        self.salvage("j1", ["stock/R1"], [order("A", 4), order("B", 5)],
                     kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 5)])])
        return b_ref

    def test_original_production_shares_are_hand_computed(self):
        self.build()
        detail = workspace.cost_query(str(self.ws))
        self.assertEqual(self.piece_amounts(detail),
                         {("j1", "stock/R1", "A", 1): 0,
                          ("j1", "stock/R1", "B", 1): 1})
        # Root conserves its one cent.
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 1)

    def test_return_moves_the_whole_cent_without_reapportionment(self):
        b_ref = self.build()
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        detail = workspace.cost_query(str(self.ws))
        amounts = self.piece_amounts(detail)
        # The original B line survives with a zero current value and names the
        # rework identity; A keeps its own zero.
        b_row = next(r for r in detail["pieces"] if r["order_id"] == "B")
        self.assertEqual(b_row["amount_cents"], 0)
        self.assertEqual(b_row["returned_by"], "RT1")
        self.assertEqual(b_row["return_entry_id"], rework)
        self.assertEqual(amounts[("j1", "stock/R1", "A", 1)], 0)
        # The rework stock holds the exact transferred cent, not a length split.
        self.assertEqual(self.inventory_amounts(detail), {rework: 1})
        (transfer,) = detail["value_transfers"]
        self.assertEqual(transfer["return_id"], "RT1")
        self.assertEqual(transfer["source_piece"], b_ref)
        self.assertEqual(transfer["target_entry_id"], rework)
        self.assertEqual(transfer["amount_cents"], 1)
        # Transfers are checkable evidence, never an extra asset: the summary
        # still conserves exactly one cent on the root.
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 1)
        self.assertEqual(detail["summary"]["by_root"]["R1"]["inventory_cents"], 1)
        self.assertEqual(detail["summary"]["by_root"]["R1"]["pieces_cents"], 0)

    def test_recut_apportions_the_transferred_cent_once_on_the_new_ticket(self):
        b_ref = self.build()
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        result = self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        # Input to the new ticket is the transferred cent, apportioned across a
        # 4mm piece and a 1mm kerf; the piece takes the whole cent.
        ticket = result["tickets"][0]
        self.assertEqual(ticket["entry_kind"], "return")
        detail = workspace.cost_query(str(self.ws))
        c_row = next(r for r in detail["pieces"] if r["order_id"] == "C")
        self.assertEqual(c_row["amount_cents"], 1)
        # Rework identity consumed => no longer inventory; B stays at zero.
        self.assertNotIn(rework, self.inventory_amounts(detail))
        b_row = next(r for r in detail["pieces"] if r["order_id"] == "B")
        self.assertEqual(b_row["amount_cents"], 0)
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 1)

    def test_three_close_chain_c0_c1_c2_matches_hand_figures(self):
        b_ref = self.build()
        c0 = self.close_run(close("C0", None, 1, 1))
        # C0: B holds 1, A 0, no rework identity exists yet.
        c0_pieces = self.piece_amounts(c0["detail"])
        self.assertEqual(c0_pieces[("j1", "stock/R1", "B", 1)], 1)
        self.assertFalse(any(r["entry_id"].startswith("return/")
                             for r in c0["detail"]["inventory"]))
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        state = workspace._read_state(str(self.ws))
        c1 = self.close_run(close("C1", "C0", state["revision"], 1))
        lines = self.bridge(c1)
        # B production -1, rework inventory +1, no historical price change.
        b_line = lines["piece/j1/stock/R1/B/1"]
        self.assertEqual((b_line["opening_cents"], b_line["price_change_cents"],
                          b_line["production_cents"], b_line["closing_cents"]),
                         (1, 0, -1, 0))
        rw_line = lines[f"inventory/{rework}"]
        self.assertEqual((rw_line["opening_cents"], rw_line["price_change_cents"],
                          rw_line["production_cents"], rw_line["closing_cents"]),
                         (0, 0, 1, 1))
        # Reprice to 3 cents and recut the rework into a 4mm piece + 1mm kerf.
        self.register_price(price("P2", "R1", 3, 1))
        self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        state = workspace._read_state(str(self.ws))
        c2 = self.close_run(close("C2", "C1", state["revision"], 2))
        lines = self.bridge(c2)
        a_line = lines["piece/j1/stock/R1/A/1"]
        self.assertEqual((a_line["opening_cents"], a_line["price_change_cents"],
                          a_line["production_cents"], a_line["closing_cents"]),
                         (0, 1, 0, 1))
        b_line = lines["piece/j1/stock/R1/B/1"]
        self.assertEqual((b_line["opening_cents"], b_line["price_change_cents"],
                          b_line["production_cents"], b_line["closing_cents"]),
                         (0, 0, 0, 0))
        rw_line = lines[f"inventory/{rework}"]
        # At the C1 snapshot under 3-cent pricing the rework holds 2; the
        # recut then moves all 2 into the new piece.
        self.assertEqual((rw_line["opening_cents"], rw_line["price_change_cents"],
                          rw_line["production_cents"], rw_line["closing_cents"]),
                         (1, 1, -2, 0))
        c_line = lines[f"piece/j2/{rework}/C/1"]
        self.assertEqual((c_line["opening_cents"], c_line["price_change_cents"],
                          c_line["production_cents"], c_line["closing_cents"]),
                         (0, 0, 2, 2))
        # Kerfs stay zero throughout; final pieces total the new root price.
        for line in lines.values():
            if line["kind"] == "loss":
                self.assertEqual(line["closing_cents"], 0)
        self.assertEqual(c2["detail"]["summary"]["by_root"]["R1"]["pieces_cents"], 3)
        # Per-root production transfer nets to zero in every bridge.
        for result in (c1, c2):
            root_bucket = result["bridge"]["summary"]["by_root"]["R1"]
            self.assertEqual(root_bucket["production_cents"], 0)
            self.assertEqual(root_bucket["opening_cents"]
                             + root_bucket["price_change_cents"]
                             + root_bucket["production_cents"],
                             root_bucket["closing_cents"])
        # Frozen earlier closes never move.
        self.assertEqual(workspace.close_status(str(self.ws), "C0"), c0)
        self.assertEqual(workspace.close_status(str(self.ws), "C1"), c1)

    def test_skipping_c1_reprices_the_old_snapshot_without_back_filling_returns(self):
        b_ref = self.build()
        c0 = self.close_run(close("C0", None, 1, 1))
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        self.register_price(price("P2", "R1", 3, 1))
        self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        state = workspace._read_state(str(self.ws))
        c1 = self.close_run(close("C1", "C0", state["revision"], 2))
        lines = self.bridge(c1)
        b_line = lines["piece/j1/stock/R1/B/1"]
        # At the old snapshot the B piece is revalued 1 -> 2 (+1 historical),
        # then the return+recut move both cents out (-2 production).
        self.assertEqual((b_line["opening_cents"], b_line["price_change_cents"],
                          b_line["production_cents"], b_line["closing_cents"]),
                         (1, 1, -2, 0))
        a_line = lines["piece/j1/stock/R1/A/1"]
        self.assertEqual((a_line["price_change_cents"], a_line["closing_cents"]),
                         (1, 1))
        c_line = lines[f"piece/j2/{rework}/C/1"]
        self.assertEqual((c_line["opening_cents"], c_line["price_change_cents"],
                          c_line["production_cents"], c_line["closing_cents"]),
                         (0, 0, 2, 2))
        # The rework identity never appears as an inventory line: it did not
        # exist at the old snapshot and is already consumed at the new one.
        self.assertNotIn(f"inventory/{rework}", lines)
        # Frozen C0 still prices B at the original one cent and has no rework.
        frozen = workspace.close_status(str(self.ws), "C0")
        self.assertEqual(frozen, c0)
        self.assertEqual(
            next(r for r in frozen["detail"]["pieces"] if r["order_id"] == "B")
            ["amount_cents"], 1)
        self.assertFalse(any(r["entry_id"] == rework
                             for r in frozen["detail"]["inventory"]))


class MissingPriceTests(ReturnCostCase):
    def test_goods_receipt_succeeds_but_costing_fails_until_priced(self):
        self.init({"stock": [stock_row("R1", 12)]})
        self.salvage("j1", ["stock/R1"], [order("B", 5)], kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 5)])])
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        # No price on R1: physical facts are durable, money queries refuse.
        with self.assertRaisesRegex(ValueError, "missing registered prices"):
            workspace.cost_query(str(self.ws))
        state = workspace._read_state(str(self.ws))
        with self.assertRaisesRegex(ValueError, "missing registered prices"):
            self.close_run(close("CX", None, state["revision"], 0))
        # Once priced, costing works and the receipt carried no cached amount;
        # the value transfer shows how many of the new cents moved wholesale.
        self.register_price(price("P1", "R1", 2, 0))
        detail = workspace.cost_query(str(self.ws))
        b_row = next(r for r in detail["pieces"] if r["order_id"] == "B")
        self.assertEqual(b_row["amount_cents"], 0)
        self.assertEqual(b_row["return_entry_id"], "return/RT1/1")
        (transfer,) = detail["value_transfers"]
        inventory = self.inventory_amounts(detail)
        self.assertEqual(inventory["return/RT1/1"], transfer["amount_cents"])
        # Root conservation still holds with the reusable offcut present.
        root = detail["summary"]["by_root"]["R1"]
        self.assertEqual(root["pieces_cents"] + root["loss_cents"]
                         + root["inventory_cents"], 2)
        # The frozen receipt itself never stored a money figure.
        frozen = workspace.return_status(str(self.ws), "RT1")
        self.assertNotIn("amount_cents", json.dumps(frozen["items"]))


class ConservationAuditTests(ReturnCostCase):
    def test_every_projection_conserves_each_root_through_return_and_recut(self):
        self.init({"stock": [stock_row("R1", 30), stock_row("R2", 20)]})
        self.register_price(price("P1", "R1", 7, 0))
        self.register_price(price("P2", "R2", 5, 1))
        result = self.salvage("j1", ["stock/R1", "stock/R2"],
                             [order("B", 5, 2), order("C", 4, 1)],
                             kerf=1, min_offcut=2)
        # The optimizer chooses the placement; resolve the real B piece ref.
        b_ticket = next(t for t in result["tickets"]
                        if any(c["order_id"] == "B" for c in t["cuts"]))
        b_entry = b_ticket["entry_id"]
        b1 = self.ref("j1", b_entry, "B", 1)
        self.deliver("D1", [b1], [("K1", [self.slot("s1", 5)])])
        self.return_register("RT1", [self.item("D1", "K1", "s1", b1)])
        # Recut the rework normally.
        self.normal("j2", [order("E", 3)], kerf=1, min_offcut=2)
        for price_revision, prices in ((2, {"R1": 7, "R2": 5}),
                                       (3, {"R1": 11, "R2": 5})):
            if price_revision == 3:
                self.register_price(price("P3", "R1", 11, 2))
            detail = workspace._detail_at(
                workspace._read_state(str(self.ws)),
                workspace._read_state(str(self.ws))["revision"],
                price_revision)
            for root, adopted in prices.items():
                bucket = detail["summary"]["by_root"][root]
                self.assertEqual(
                    bucket["pieces_cents"] + bucket["loss_cents"]
                    + bucket["inventory_cents"], adopted)
                self.assertEqual(bucket["total_cents"], adopted)
            # Every ticket's segments still sum to its integer input.
            for ticket in detail["tickets"]:
                self.assertEqual(
                    sum(seg["amount_cents"] for seg in ticket["segments"]),
                    ticket["input_cents"])
            # Each transfer is balanced by a zeroed original piece line; the
            # transferred cents live on in the rework chain (inventory or the
            # pieces produced after consuming it), so root totals still match.
            transferred = sum(t["amount_cents"]
                              for t in detail["value_transfers"])
            zeroed = sum(1 for t in detail["value_transfers"]
                         for r in detail["pieces"]
                         if (r["job_id"], r["entry_id"], r["order_id"], r["unit"])
                         == tuple(t["source_piece"][k] for k in
                                  ("job_id", "entry_id", "order_id", "unit"))
                         and r["amount_cents"] == 0)
            self.assertEqual(zeroed, len(detail["value_transfers"]))
            self.assertGreaterEqual(transferred, 0)


if __name__ == "__main__":
    unittest.main()
