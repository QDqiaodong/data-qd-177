"""Ancestry, re-return rounding and per-snapshot physical/money ledgers.

These tests build deeper chains than the basic registration suite: a piece
cut from a multi-generation normal offcut, a salvage-branch piece, a second
customer return after an intermediate apportionment, and an independent
millimetre ledger proving every active millimetre belongs to exactly one
piece, loss segment or unconsumed inventory identity at each snapshot.
"""
import json
import tempfile
import unittest
from pathlib import Path

from sawmill import workspace

from test_returns import ReturnCase, order, stock_row


def price(price_id, stock_id, amount_cents, expected_price_revision):
    return {"price_id": price_id, "stock_id": stock_id,
            "amount_cents": amount_cents,
            "expected_price_revision": expected_price_revision}


class MultiGenerationSourceTests(ReturnCase):
    def test_piece_from_multi_generation_normal_offcut_keeps_true_root_coords(self):
        # R1: first cut 40 + 1mm kerf leaves a 59mm offcut whose local zero is
        # at root 41; a second cut on that offcut produces a 20mm piece whose
        # local [0,20) therefore maps to root [41,61).
        self.init({"stock": [stock_row("R1", 100)]})
        self.normal("b1", [order("rail", 40)], kerf=1, min_offcut=2)
        offcut1 = "offcut/b1/stock/R1"
        self.normal("b2", [order("slat", 20)], kerf=1, min_offcut=2)
        ticket2 = workspace.status(str(self.ws), "b2")["tickets"][0]
        self.assertEqual(ticket2["stock_id"], offcut1)
        slat = self.ref("b2", offcut1, "slat", 1)
        self.deliver("D1", [slat], [("K1", [self.slot("s1", 20)])])
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", slat)])
        item = receipt["items"][0]
        self.assertEqual(item["length_mm"], 20)
        self.assertEqual((item["root_start_mm"], item["root_end_mm"]),
                         (41, 61))
        entry = read_state(self.ws)["entries"][item["return_entry_id"]]
        self.assertEqual(entry["root_offset_mm"], 41)
        self.assertEqual(entry["source"]["original_entry_id"], offcut1)
        # root_offset_of walks the chain without treating local coords as root.
        self.assertEqual(
            workspace.root_offset_of(read_state(self.ws),
                                     item["return_entry_id"]), 41)

    def test_piece_from_salvage_branch_carries_branch_root_coordinates(self):
        self.init({"stock": [stock_row("R1", 60)]})
        # Inspect the root so salvage produces a clean branch offcut.
        state = workspace._read_state(str(self.ws))
        workspace.register_inspection(
            str(self.ws),
            self.source({"inspection_id": "INSP1", "entry_id": "stock/R1",
                         "expected_inventory_revision": state["revision"],
                         "reject_zones": [{"start_mm": 30, "end_mm": 34}]}),
            self.out("insp1.json"))
        salvage = self.salvage("sv1", ["stock/R1"], [order("peg", 8)],
                               kerf=1, min_offcut=4)
        # Two clean branches exist around the reject zone; the follow-up cut
        # chooses per the normal planner. Both carry absolute root offsets.
        branch_ids = [e["entry_id"] for e in salvage["added_entries"]]
        self.assertEqual(branch_ids,
                         ["offcut/sv1/stock/R1#1", "offcut/sv1/stock/R1#2"])
        result = self.normal("b2", [order("pin", 6)], kerf=1, min_offcut=2)
        ticket = result["tickets"][0]
        branch = ticket["stock_id"]
        self.assertIn(branch, branch_ids)
        branch_offset = read_state(self.ws)["entries"][branch]["root_offset_mm"]
        pin = self.ref("b2", branch, "pin", 1)
        self.deliver("D1", [pin], [("K1", [self.slot("s1", 6)])])
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", pin)])
        item = receipt["items"][0]
        # The rework root start is the branch offset plus the local cut start.
        self.assertEqual(item["root_start_mm"], branch_offset)
        self.assertEqual(item["root_end_mm"], branch_offset + 6)
        self.assertEqual(item["root_stock_id"], "R1")


class SecondGenerationReturnTests(ReturnCase):
    def test_re_return_keeps_intermediate_integer_apportionment(self):
        # A 6mm rework holding a whole cent; recut into two 2mm pieces each
        # followed by a 1mm kerf (2+1+2+1 = 6). Largest-remainder over the
        # segment lengths (2, 2, 1, 1) with one cent gives the cent to the
        # first 2mm piece (smallest start wins the tie).
        self.init({"stock": [stock_row("R1", 12)]})
        self.register_price(price("P1", "R1", 1, 0))
        self.salvage("j1", ["stock/R1"], [order("A", 4), order("B", 6)],
                     kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 6)])])
        first = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework1 = first["items"][0]["return_entry_id"]
        self.assertEqual(receipt_length(first), 6)
        result = self.normal("j2", [order("P", 2, 2)], kerf=1, min_offcut=2)
        ticket = result["tickets"][0]
        self.assertEqual([(c["order_id"], c["unit"]) for c in ticket["cuts"]],
                         [("P", 1), ("P", 2)])
        detail = workspace.cost_query(str(self.ws))
        amounts = {(r["order_id"], r["unit"]): r["amount_cents"]
                   for r in detail["pieces"] if r["job_id"] == "j2"}
        self.assertEqual(amounts, {("P", 1): 1, ("P", 2): 0})
        # Deliver and return the piece that actually holds the cent.
        p1 = self.ref("j2", rework1, "P", 1)
        self.deliver("D2", [p1], [("K2", [self.slot("t1", 2)])])
        second = self.return_register(
            "RT2", [self.item("D2", "K2", "t1", p1)])
        rework2 = second["items"][0]["return_entry_id"]
        self.assertNotEqual(rework1, rework2)
        detail = workspace.cost_query(str(self.ws))
        # The cent survives exactly once: on the second rework identity. The
        # ancestor B line and the j2 P/1 line are both zero, the P/2 piece and
        # every kerf are zero, and the root still totals exactly one cent.
        inventory = {r["entry_id"]: r["amount_cents"]
                     for r in detail["inventory"]}
        self.assertEqual(inventory.get(rework2), 1)
        self.assertNotIn(rework1, inventory)  # consumed by j2
        for row in detail["pieces"]:
            if (row["job_id"], row["order_id"], row["unit"]) in (
                    ("j1", "B", 1), ("j2", "P", 1)):
                self.assertEqual(row["amount_cents"], 0)
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 1)
        # Two transfers are recorded, each moving the exact integer amount
        # derived under current prices rather than a cached receipt figure.
        self.assertEqual([t["amount_cents"] for t in detail["value_transfers"]],
                         [1, 1])
        # The ancestor piece still cannot return again.
        state = workspace._read_state(str(self.ws))
        with self.assertRaisesRegex(ValueError, "already been returned"):
            self.return_register(
                "RT3", [self.item("D1", "K1", "s1", b_ref)],
                expected=(state["revision"], state["finished_revision"]))

    def test_price_correction_flows_through_both_returns_without_cached_money(self):
        self.init({"stock": [stock_row("R1", 12)]})
        self.register_price(price("P1", "R1", 1, 0))
        self.salvage("j1", ["stock/R1"], [order("A", 4), order("B", 6)],
                     kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 6)])])
        self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)])
        self.normal("j2", [order("P", 2, 2)], kerf=1, min_offcut=2)
        p1 = self.ref("j2", "return/RT1/1", "P", 1)
        self.deliver("D2", [p1], [("K2", [self.slot("t1", 2)])])
        self.return_register("RT2", [self.item("D2", "K2", "t1", p1)])
        # Correct the price after both returns: every transfer recomputes from
        # the real parent-ticket apportionment rather than a cached receipt
        # figure. The intermediate j2 ticket already rounded once, so each
        # transfer follows its own ticket's integer share.
        self.register_price(price("P2", "R1", 3, 1))
        detail = workspace.cost_query(str(self.ws))
        transfers = {t["return_id"]: t["amount_cents"]
                     for t in detail["value_transfers"]}
        # B's share of the root under 3 cents, derived through j1 exactly as
        # the live ticket computes it (never hard-coded here): the transferred
        # amount equals the segment apportionment under the adopted price even
        # though the displayed piece row is now zero.
        self.assertEqual(transfers["RT1"],
                         expected_transfer(detail, "j1", "B", 1))
        self.assertEqual(transfers["RT2"],
                         expected_transfer(detail, "j2", "P", 1))
        self.assertEqual(
            next(r["amount_cents"] for r in detail["inventory"]
                 if r["entry_id"] == "return/RT2/1"), transfers["RT2"])
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 3)
        # The frozen receipts themselves contain no money fields.
        for return_id in ("RT1", "RT2"):
            self.assertNotIn("amount_cents",
                             json.dumps(workspace.return_status(
                                 str(self.ws), return_id)["items"]))


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


def receipt_length(receipt):
    return receipt["items"][0]["length_mm"]


def independent_share(amount_cents, lengths, segment_index):
    """Independent largest-remainder apportionment, written for the test.

    Mirrors the documented integer rule without importing the production
    allocator: floor shares first, then leftover cents go to the largest
    exact remainders, ties broken by smaller physical start (lower index).
    """
    total = sum(lengths)
    floors = [amount_cents * length // total for length in lengths]
    leftover = amount_cents - sum(floors)
    remainders = sorted(
        range(len(lengths)),
        key=lambda i: (-(amount_cents * lengths[i] % total), i))
    for index in remainders[:leftover]:
        floors[index] += 1
    return floors[segment_index]


def expected_transfer(detail, job_id, order_id, unit):
    """Hand-compute a piece's integer share from its physical ticket."""
    ticket = next(t for t in detail["tickets"] if t["job_id"] == job_id)
    lengths = [seg["end_mm"] - seg["start_mm"] for seg in ticket["segments"]]
    segment_index = next(i for i, seg in enumerate(ticket["segments"])
                         if seg.get("order_id") == order_id
                         and seg.get("unit") == unit)
    return independent_share(ticket["input_cents"], lengths, segment_index)


class SnapshotLedgerTests(ReturnCase):
    """Independent physical and money ownership audit at each snapshot."""

    def test_each_millimetre_and_cent_has_one_owner_at_every_snapshot(self):
        self.init({"stock": [stock_row("R1", 30)]})
        self.register_price(price("P1", "R1", 7, 0))
        # Salvage: 2x5mm pieces with kerfs, leaving reusable lane remainders.
        self.salvage("j1", ["stock/R1"], [order("B", 5, 2)],
                     kerf=1, min_offcut=2)
        b1 = self.ref("j1", "stock/R1", "B", 1)
        b2 = self.ref("j1", "stock/R1", "B", 2)
        self.deliver("D1", [b1, b2], [
            ("K1", [self.slot("s1", 5), self.slot("s2", 5)])])
        revision_after_delivery = workspace._read_state(str(self.ws))["revision"]
        # Return only the first piece.
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b1)])
        rework = receipt["items"][0]["return_entry_id"]
        revision_after_return = workspace._read_state(str(self.ws))["revision"]
        self.normal("j2", [order("C", 3)], kerf=1, min_offcut=2)
        revisions = [0, revision_after_delivery, revision_after_return,
                     workspace._read_state(str(self.ws))["revision"]]
        root_price = 7
        for revision in revisions:
            state = workspace._read_state(str(self.ws))
            detail = workspace._detail_at(state, revision, 1)
            self.assert_root_space_ledger(state, detail, revision,
                                          root_length=30)
            self.assert_money_conservation(detail, root_price)
        # After the recut the returned ancestor is excluded from active wood:
        # its millimetres now belong to j2 piece/kerf only, never twice.
        state = workspace._read_state(str(self.ws))
        latest = workspace._detail_at(state, state["revision"], 1)
        active_inventory = {r["entry_id"] for r in latest["inventory"]}
        self.assertNotIn(rework, active_inventory)

    def assert_root_space_ledger(self, state, detail, revision, root_length):
        """Every root millimetre belongs to exactly one owner at this snapshot.

        Owners are derived independently from the physical facts: each
        committed ticket contributes its loss segments and produced pieces in
        absolute root coordinates (ticket root offset plus local spans), each
        unconsumed inventory identity contributes its own root span, and a
        returned-but-not-recut piece counts as rework inventory rather than an
        active finished piece. The audit asserts full, gapless, unique cover.
        """
        entries = state["entries"]
        jobs = {job["job_id"]: job for job in state["jobs"].values()
                if job["revision"] <= revision}
        returns = [state["returns"][rid] for rid in state.get("return_order", ())
                   if state["returns"][rid]["inventory_revision"] <= revision]
        consumed = {ticket["stock_id"] for job in jobs.values()
                    for ticket in job["result"]["tickets"]}
        # Root spans returned to rework stock at this snapshot: the original
        # piece no longer owns the wood, the rework inventory identity does.
        returned_spans = set()
        for record in returns:
            for item in record["result"]["items"]:
                returned_spans.add((item["root_start_mm"], item["root_end_mm"]))
        owner = [None] * root_length

        def claim(kind, root_start, root_end):
            for mm in range(root_start, root_end):
                assert owner[mm] is None, (
                    f"mm {mm} double-owned at revision {revision}: "
                    f"{owner[mm]} vs {kind}")
                owner[mm] = kind

        # Loss segments and produced pieces of every ticket, mapped to root.
        for job_id, job in jobs.items():
            for ticket in job["result"]["tickets"]:
                entry = entries[ticket["stock_id"]]
                base = workspace.root_offset_of(state, ticket["stock_id"])
                segments = ticket.get("partitions")
                if segments is None:
                    # Normal ticket: rebuild its physical spans in order.
                    segments = []
                    if entry["trim_head_mm"]:
                        segments.append(("trim_head", 0,
                                         entry["trim_head_mm"], False))
                    for cut in ticket["cuts"]:
                        segments.append(("piece", cut["start_mm"],
                                         cut["end_mm"], False))
                        segments.append(("kerf", cut["end_mm"],
                                         cut["kerf_end_mm"], False))
                    if ticket["offcut_length_mm"] > 0:
                        segments.append(("offcut",
                                         ticket["offcut_start_mm"],
                                         ticket["offcut_start_mm"]
                                         + ticket["offcut_length_mm"],
                                         ticket["offcut_reusable"]))
                    tail_start = entry["length_mm"] - entry["trim_tail_mm"]
                    if entry["trim_tail_mm"]:
                        segments.append(("trim_tail", tail_start,
                                         entry["length_mm"], False))
                else:
                    segments = [(p["kind"], p["start_mm"], p["end_mm"],
                                 p.get("reusable", False))
                                for p in segments]
                for kind, start, end, *rest in segments:
                    reusable = rest[0] if rest else False
                    # A *reusable* offcut segment is the future inventory
                    # identity, claimed once by the inventory pass. A scrapped
                    # (non-reusable) remainder is real loss and owned here.
                    if kind == "offcut" and (reusable
                                             or ticket.get("offcut_reusable")):
                        continue
                    root_span = (base + start, base + end)
                    # Returned original piece spans now belong to the rework
                    # inventory identity claimed in the inventory pass.
                    if kind == "piece" and root_span in returned_spans:
                        continue
                    claim(f"{job_id}:{kind}", base + start, base + end)

        # Unconsumed inventory identities existing at this snapshot hold their
        # own root spans; rework identities are included once the return that
        # creates them is visible and until the ticket that consumes them.
        for entry_id, entry in entries.items():
            if entry["kind"] == "offcut" \
                    and entry["source"]["job_id"] not in jobs:
                continue
            if entry["kind"] == "return" and entry["source"]["return_id"] \
                    not in {r["return_id"] for r in returns}:
                continue
            if entry_id in consumed:
                continue
            base = workspace.root_offset_of(state, entry_id)
            claim(f"inventory:{entry_id}", base,
                  base + entry["length_mm"])

        uncovered = [mm for mm, who in enumerate(owner) if who is None]
        assert not uncovered, f"unowned millimetres at revision {revision}: {uncovered}"

    def assert_money_conservation(self, detail, root_price):
        root = detail["summary"]["by_root"]["R1"]
        self.assertEqual(root["pieces_cents"] + root["loss_cents"]
                         + root["inventory_cents"], root_price)
        self.assertEqual(root["total_cents"], root_price)
        for ticket in detail["tickets"]:
            self.assertEqual(
                sum(seg["amount_cents"] for seg in ticket["segments"]),
                ticket["input_cents"])
        # Transfers at this snapshot are balanced against existing targets.
        for transfer in detail["value_transfers"]:
            target = transfer["target_entry_id"]
            held = next((r["amount_cents"] for r in detail["inventory"]
                         if r["entry_id"] == target), None)
            if held is not None:
                self.assertEqual(held, transfer["amount_cents"])


if __name__ == "__main__":
    unittest.main()
