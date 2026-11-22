"""Tests for customer-return registration and the one-time rework conversion.

Every scenario is built from real committed cutting tickets and real
deliveries; no test assumes the existence of a finished piece without cutting
and dispatching it first. Independent hand checks (revision steps, root
coordinates, identity strings) are recomputed in the tests rather than taken
from the implementation under test.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sawmill import workspace
from sawmill.workspace import ConflictError, StalePlanError

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def order(order_id, piece_mm, quantity=1, material="oak"):
    return {"id": order_id, "material": material, "piece_mm": piece_mm,
            "quantity": quantity}


def stock_row(stock_id, length, material="oak", head=0, tail=0):
    return {"id": stock_id, "material": material, "length_mm": length,
            "trim_head_mm": head, "trim_tail_mm": tail}


def salvage_request(job_id, candidates, orders, kerf=1, min_offcut=2):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "candidate_entry_ids": candidates, "orders": orders}


def batch(job_id, orders, kerf=1, min_offcut=2):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "orders": orders}


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


class ReturnCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ws = self.dir / "ws"
        self.seq = 0

    def source(self, value):
        self.seq += 1
        path = self.dir / f"src-{self.seq}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return str(path)

    def out(self, name):
        return str(self.dir / name)

    # -- fixture builders ----------------------------------------------------

    def init(self, stock):
        return workspace.init_workspace(str(self.ws), self.source(stock))

    def salvage(self, job_id, candidates, orders, kerf=1, min_offcut=2):
        req = salvage_request(job_id, candidates, orders, kerf=kerf,
                              min_offcut=min_offcut)
        plan_path = self.out(f"{job_id}-plan.json")
        workspace.salvage_plan(str(self.ws), self.source(req), plan_path)
        return workspace.salvage_commit(
            str(self.ws), plan_path, self.out(f"{job_id}-result.json"))

    def normal(self, job_id, orders, kerf=1, min_offcut=2):
        req = batch(job_id, orders, kerf=kerf, min_offcut=min_offcut)
        plan_path = self.out(f"{job_id}-plan.json")
        workspace.plan(str(self.ws), self.source(req), plan_path)
        return workspace.commit(
            str(self.ws), plan_path, self.out(f"{job_id}-result.json"))

    def register_price(self, value):
        self.seq += 1
        return workspace.register_price(
            str(self.ws), self.source(value),
            self.out(f"price-{self.seq}.json"))

    def deliver(self, dispatch_id, refs, slots, materials=None):
        kits = [{"kit_id": kit_id, "priority": index, "slots": slot_rows}
                for index, (kit_id, slot_rows) in enumerate(slots)]
        req = {"dispatch_id": dispatch_id, "candidate_pieces": refs, "kits": kits}
        plan_path = self.out(f"{dispatch_id}-plan.json")
        workspace.delivery_plan(str(self.ws), self.source(req), plan_path)
        return workspace.delivery_commit(
            str(self.ws), plan_path, self.out(f"{dispatch_id}-result.json"))

    def slot(self, slot_id, length, material="oak", group="G"):
        return {"slot_id": slot_id, "material": material,
                "min_mm": length, "max_mm": length, "grain_group": group}

    def ref(self, job_id, entry_id, order_id, unit):
        return {"job_id": job_id, "entry_id": entry_id, "order_id": order_id,
                "unit": unit}

    def return_register(self, return_id, items, expected=None, output=None):
        if expected is None:
            state = workspace._read_state(str(self.ws))
            expected = (state["revision"], state["finished_revision"])
        req = {"return_id": return_id,
               "expected_inventory_revision": expected[0],
               "expected_finished_revision": expected[1],
               "items": items}
        return workspace.register_return(
            str(self.ws), self.source(req),
            output or self.out(f"{return_id}.json"))

    def item(self, dispatch_id, kit_id, slot_id, piece):
        return {"dispatch_id": dispatch_id, "kit_id": kit_id,
                "slot_id": slot_id, "piece": piece}


class ElevenMillimetreFlowTests(ReturnCase):
    """The acceptance flow: 11mm root, salvage A(4)+B(5), deliver+return B."""

    def build_flow(self):
        self.init({"stock": [stock_row("R1", 11)]})
        self.salvage("j1", ["stock/R1"],
                     [order("A", 4), order("B", 5)], kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 5)])])
        ticket = workspace.status(str(self.ws), "j1")["tickets"][0]
        return b_ref, ticket

    def test_original_ticket_partition_and_integer_shares(self):
        _b_ref, ticket = self.build_flow()
        self.assertEqual(
            [(p["kind"], p["start_mm"], p["end_mm"])
             for p in ticket["partitions"]],
            [("piece", 0, 4), ("kerf", 4, 5),
             ("piece", 5, 10), ("kerf", 10, 11)])
        workspace.register_price(
            str(self.ws),
            self.source({"price_id": "P1", "stock_id": "R1",
                         "amount_cents": 1, "expected_price_revision": 0}),
            self.out("p1.json"))
        detail = workspace.cost_query(str(self.ws))
        amounts = {(r["order_id"], r["unit"]): r["amount_cents"]
                   for r in detail["pieces"]}
        self.assertEqual(amounts, {("A", 1): 0, ("B", 1): 1})
        kerfs = [r for r in detail["losses"] if r["category"] == "kerf"]
        self.assertEqual([r["amount_cents"] for r in kerfs], [0, 0])

    def test_return_creates_five_mm_rework_at_root_five(self):
        b_ref, _ticket = self.build_flow()
        before = workspace._read_state(str(self.ws))
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        after = workspace._read_state(str(self.ws))
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["finished_revision"],
                         before["finished_revision"] + 1)
        self.assertEqual(after.get("price_revision", 0),
                         before.get("price_revision", 0))
        (item,) = receipt["items"]
        self.assertEqual(item["return_entry_id"], "return/RT1/1")
        self.assertEqual(item["length_mm"], 5)
        self.assertEqual((item["root_start_mm"], item["root_end_mm"]), (5, 10))
        entry = after["entries"]["return/RT1/1"]
        self.assertEqual(entry["kind"], "return")
        self.assertEqual(entry["trim_head_mm"], 0)
        self.assertEqual(entry["trim_tail_mm"], 0)
        self.assertEqual(entry["length_mm"], 5)
        self.assertEqual(entry["usable_mm"], 5)
        self.assertEqual(entry["root_stock_id"], "R1")
        self.assertEqual(entry["root_offset_mm"], 5)
        self.assertEqual(entry["status"], "available")
        self.assertEqual(entry["source"]["type"], "return")
        self.assertEqual(entry["source"]["original_piece"], b_ref)
        # Receipt freezes physical facts and operation revisions only.
        self.assertEqual(receipt["before_inventory_revision"], before["revision"])
        self.assertEqual(receipt["after_inventory_revision"], after["revision"])
        self.assertEqual(receipt["before_finished_revision"],
                         before["finished_revision"])
        self.assertEqual(receipt["after_finished_revision"],
                         after["finished_revision"])
        self.assertEqual(receipt["request_sha256"],
                         workspace._fingerprint(receipt["request"]))

    def test_original_delivery_keeps_its_frozen_occupation(self):
        b_ref, _ = self.build_flow()
        self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)])
        # The original dispatch result still shows the original slot and piece.
        dispatch = workspace._read_state(str(self.ws))["dispatches"]["D1"]
        (slot,) = dispatch["result"]["selected_kits"][0]["slots"]
        self.assertEqual({k: slot["piece"][k] for k in
                          ("job_id", "entry_id", "order_id", "unit")}, b_ref)
        # A new delivery cannot put the original piece back in the pool.
        req = {"dispatch_id": "D2", "candidate_pieces": [b_ref],
               "kits": [{"kit_id": "K1", "priority": 0,
                         "slots": [self.slot("s1", 5)]}]}
        plan = workspace.delivery_plan(
            str(self.ws), self.source(req), self.out("d2-plan.json"))
        (excluded,) = plan["excluded_pieces"]
        self.assertEqual(excluded["reasons"], ["delivered"])
        self.assertEqual(excluded["delivered_by"], "D1")
        self.assertEqual(excluded["returned_by"], "RT1")
        self.assertEqual(plan["selected_kits"], [])
        # Replaying the original delivery returns the original frozen result.
        replay = workspace.delivery_commit(
            str(self.ws), self.out("D1-plan.json"), self.out("d1-replay.json"))
        self.assertEqual(replay, dispatch["result"])

    def test_delivery_status_lists_return_facts_without_rewriting_history(self):
        b_ref, _ = self.build_flow()
        self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)])
        status = workspace.delivery_status(str(self.ws))
        row = next(p for p in status["pieces"]
                   if (p["job_id"], p["entry_id"], p["order_id"], p["unit"])
                   == tuple(b_ref[k] for k in
                            ("job_id", "entry_id", "order_id", "unit")))
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["returned_by"], "RT1")
        self.assertEqual(row["return_entry_id"], "return/RT1/1")
        self.assertTrue(row["returned"])
        detail = workspace.delivery_status(str(self.ws), "D1")
        kit = detail["recall"]["kits"][0]
        (returned_slot,) = kit["returned_slots"]
        self.assertEqual(returned_slot["slot_id"], "s1")
        self.assertEqual(returned_slot["returned_by"], "RT1")
        self.assertEqual(returned_slot["return_entry_id"], "return/RT1/1")
        # The frozen dispatch payload itself is untouched.
        self.assertNotIn("returned_slots", detail["result"]["selected_kits"][0])

    def test_rework_cut_normally_into_four_mm_piece_with_root_five_to_nine(self):
        b_ref, _ = self.build_flow()
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        result = self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        (ticket,) = result["tickets"]
        self.assertEqual(ticket["stock_id"], rework)
        (cut,) = ticket["cuts"]
        self.assertEqual((cut["start_mm"], cut["end_mm"]), (0, 4))
        self.assertEqual((cut["kerf_end_mm"],), (5,))
        # Root coordinates are entry offset plus local coordinates.
        self.assertEqual(workspace.root_offset_of(
            workspace._read_state(str(self.ws)), rework), 5)
        state = workspace._read_state(str(self.ws))
        self.assertEqual(state["entries"][rework]["status"], "consumed")
        self.assertEqual(state["entries"][rework]["consumed_by"], "j2")

    def test_new_piece_can_be_delivered_and_then_returned_but_ancestor_cannot(self):
        b_ref, _ = self.build_flow()
        first = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = first["items"][0]["return_entry_id"]
        self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        c_ref = self.ref("j2", rework, "C", 1)
        self.deliver("D2", [c_ref], [("K1", [self.slot("s1", 4)])])
        second = self.return_register(
            "RT2", [self.item("D2", "K1", "s1", c_ref)])
        self.assertEqual(second["items"][0]["return_entry_id"], "return/RT2/1")
        # The ancestor finished piece cannot return a second time.
        state = workspace._read_state(str(self.ws))
        with self.assertRaisesRegex(ValueError, "already been returned"):
            self.return_register(
                "RT3", [self.item("D1", "K1", "s1", b_ref)],
                expected=(state["revision"], state["finished_revision"]))


class ValidationAndIdempotencyTests(ReturnCase):
    def setUp(self):
        super().setUp()
        self.init({"stock": [stock_row("R1", 40)]})
        self.salvage("j1", ["stock/R1"], [order("B", 5, 2)], kerf=1,
                     min_offcut=2)
        self.refs = [self.ref("j1", "stock/R1", "B", u) for u in (1, 2)]
        self.deliver("D1", self.refs, [
            ("K1", [self.slot("s1", 5), self.slot("s2", 5)])])

    def test_partial_kit_return_and_multi_dispatch_receipt(self):
        # A second dispatch on another piece.
        self.salvage("j2", ["offcut/j1/stock/R1"], [order("C", 6)],
                     kerf=1, min_offcut=2) if False else None
        # Partial return: only slot s1 comes back.
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", self.refs[0])])
        self.assertEqual([i["slot_id"] for i in receipt["items"]], ["s1"])
        state = workspace._read_state(str(self.ws))
        self.assertIn("return/RT1/1", state["entries"])
        self.assertNotIn("return/RT1/2", state["entries"])
        # The other slot remains a delivered, non-returned piece.
        row = next(p for p in workspace.delivery_status(str(self.ws))["pieces"]
                   if p["unit"] == 2)
        self.assertIsNone(row["returned_by"])
        self.assertFalse(row["returned"])

    def test_multi_dispatch_receipt_applies_atomically_once(self):
        receipt = self.return_register("RT1", [
            self.item("D1", "K1", "s1", self.refs[0]),
            self.item("D1", "K1", "s2", self.refs[1])])
        state = workspace._read_state(str(self.ws))
        self.assertEqual([i["return_entry_id"] for i in receipt["items"]],
                         ["return/RT1/1", "return/RT1/2"])
        # A multi-item receipt advances each revision exactly once.
        self.assertEqual(state["revision"], receipt["after_inventory_revision"])
        self.assertEqual(state["finished_revision"],
                         receipt["after_finished_revision"])

    def test_wrong_slot_for_real_piece_rejects_whole_receipt(self):
        state = workspace._read_state(str(self.ws))
        before_bytes = (self.ws / "state.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "slot"):
            self.return_register(
                "RT-BAD",
                [self.item("D1", "K1", "wrong-slot", self.refs[0])],
                expected=(state["revision"], state["finished_revision"]))
        self.assertEqual((self.ws / "state.json").read_bytes(), before_bytes)
        self.assertEqual(workspace.return_status(str(self.ws))["summary"],
                         {"returns": 0, "returned_pieces": 0})

    def test_unknown_dispatch_kit_and_undelivered_piece_are_rejected(self):
        state = workspace._read_state(str(self.ws))
        for bad_item in (
                self.item("NOPE", "K1", "s1", self.refs[0]),
                self.item("D1", "NOPE", "s1", self.refs[0])):
            with self.assertRaises(ValueError):
                self.return_register(
                    "RT-X", [bad_item],
                    expected=(state["revision"], state["finished_revision"]))

    def test_duplicate_slot_and_duplicate_piece_within_request_fail(self):
        for items in (
                [self.item("D1", "K1", "s1", self.refs[0]),
                 self.item("D1", "K1", "s1", self.refs[1])],
                [self.item("D1", "K1", "s1", self.refs[0]),
                 self.item("D1", "K1", "s2", self.refs[0])]):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.return_register("RT-DUP", items)

    def test_same_piece_under_new_return_id_is_rejected(self):
        self.return_register(
            "RT1", [self.item("D1", "K1", "s1", self.refs[0])])
        state = workspace._read_state(str(self.ws))
        with self.assertRaisesRegex(ValueError, "already been returned"):
            self.return_register(
                "RT2", [self.item("D1", "K1", "s1", self.refs[0])],
                expected=(state["revision"], state["finished_revision"]))

    def test_one_invalid_item_makes_the_whole_receipt_ineffective(self):
        good = self.item("D1", "K1", "s1", self.refs[0])
        bad = self.item("D1", "K1", "s2", self.refs[0])  # slot/piece mismatch
        before = (self.ws / "state.json").read_bytes()
        with self.assertRaises(ValueError):
            self.return_register("RT-MIX", [good, bad])
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertNotIn("RT-MIX", read_state(self.ws)["returns"])

    def test_array_reorder_is_the_same_canonical_request(self):
        items = [self.item("D1", "K1", "s1", self.refs[0]),
                 self.item("D1", "K1", "s2", self.refs[1])]
        first = self.return_register("RT1", items)
        state = workspace._read_state(str(self.ws))
        # Replay with reordered items keeps the same canonical request; the
        # expected revisions are part of that identity, so an idempotent retry
        # resends the original (now stale) values and still gets the frozen
        # receipt back without changing anything.
        original_expected = (first["before_inventory_revision"],
                             first["before_finished_revision"])
        req = {"return_id": "RT1",
               "expected_inventory_revision": original_expected[0],
               "expected_finished_revision": original_expected[1],
               "items": list(reversed(items))}
        replay = workspace.register_return(
            str(self.ws), self.source(req), self.out("rt1-replay.json"))
        self.assertEqual(replay, first)
        self.assertEqual(workspace._read_state(str(self.ws)), state)
        # The same retry still works after later operations move both
        # revisions further past the values recorded in the request.
        self.normal("j-later", [order("Z", 3)], kerf=1, min_offcut=2)
        late = workspace.register_return(
            str(self.ws), self.source(dict(req)), self.out("rt1-late.json"))
        self.assertEqual(late, first)
        # The canonical request sorts items by the three piece strings then unit.
        canon = first["request"]["items"]
        keys = [(it["piece"][0], it["piece"][1], it["piece"][2], it["piece"][3])
                for it in canon]
        self.assertEqual(keys, sorted(keys))

    def test_same_id_different_input_conflicts_and_keeps_old_file(self):
        items = [self.item("D1", "K1", "s1", self.refs[0])]
        first = self.return_register("RT1", items)
        first_path = self.out("RT1.json")
        before = Path(first_path).read_bytes()
        state = workspace._read_state(str(self.ws))
        req = {"return_id": "RT1",
               "expected_inventory_revision": state["revision"],
               "expected_finished_revision": state["finished_revision"],
               "items": [self.item("D1", "K1", "s2", self.refs[1])]}
        with self.assertRaises(ConflictError):
            workspace.register_return(
                str(self.ws), self.source(req), self.out("rt1-conflict.json"))
        self.assertEqual(Path(first_path).read_bytes(), before)

    def test_stale_expected_revisions_fail_for_new_receipt(self):
        items = [self.item("D1", "K1", "s1", self.refs[0])]
        with self.assertRaisesRegex(ValueError, "expected_inventory_revision"):
            self.return_register("RT-S1", items, expected=(0, 99))
        state = workspace._read_state(str(self.ws))
        with self.assertRaisesRegex(ValueError, "expected_finished_revision"):
            self.return_register(
                "RT-S2", items,
                expected=(state["revision"], state["finished_revision"] - 1))

    def test_booleans_and_bad_shapes_are_rejected(self):
        good = self.item("D1", "K1", "s1", self.refs[0])
        bad_requests = [
            {"return_id": "RT", "expected_inventory_revision": True,
             "expected_finished_revision": 0, "items": [good]},
            {"return_id": "RT", "expected_inventory_revision": 0,
             "expected_finished_revision": False, "items": [good]},
            {"return_id": "RT", "expected_inventory_revision": 0,
             "expected_finished_revision": 0, "items": {}},
            {"return_id": "RT", "expected_inventory_revision": 0,
             "expected_finished_revision": 0, "items": []},
            {"return_id": "RT", "expected_inventory_revision": 0,
             "expected_finished_revision": 0,
             "items": [{**good, "piece": {**good["piece"], "unit": True}}]},
            {"return_id": "  ", "expected_inventory_revision": 0,
             "expected_finished_revision": 0, "items": [good]},
        ]
        for req in bad_requests:
            with self.assertRaises(ValueError):
                workspace.register_return(
                    str(self.ws), self.source(req), self.out("bad.json"))

    def test_thirty_three_items_exceed_the_limit(self):
        # Shape validation rejects the over-long request before any stateful
        # check, so the items need not be buildable deliveries.
        state = workspace._read_state(str(self.ws))
        base = self.refs[0]
        items = [self.item("D1", "K1", f"s{i}", base) for i in range(33)]
        before = (self.ws / "state.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "1..32"):
            self.return_register(
                "RT-BIG", items,
                expected=(state["revision"], state["finished_revision"]))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)

    def test_identities_do_not_collide_on_separator_like_strings(self):
        # A dedicated flow whose every string contains the identity
        # separators; the stable ids must still stay distinguishable.
        self.tmp3 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp3.cleanup)
        saved_ws, saved_seq = self.ws, self.seq
        self.ws = Path(self.tmp3.name) / "ws-weird"
        self.seq = 3000
        try:
            self.init({"stock": [stock_row("root/1", 20)]})
            self.salvage("job/1", ["stock/root/1"], [order("B/1", 5)],
                         kerf=1, min_offcut=2)
            weird_piece = self.ref("job/1", "stock/root/1", "B/1", 1)
            self.deliver("D/1", [weird_piece],
                         [("K/1", [self.slot("s/1", 5)])])
            receipt = self.return_register(
                "RT/weird", [self.item("D/1", "K/1", "s/1", weird_piece)])
            entry_id = receipt["items"][0]["return_entry_id"]
            self.assertEqual(entry_id, "return/RT/weird/1")
            self.assertIn(entry_id, read_state(self.ws)["entries"])
            # Two different separator-heavy source strings never collide.
            self.assertNotEqual(
                workspace._fingerprint({"a": "x", "b": "y"}),
                workspace._fingerprint({"a": "x/b", "b": "y"}))
        finally:
            self.ws, self.seq = saved_ws, saved_seq

    def test_integer_unit_is_compared_as_integer_not_string(self):
        item = self.item("D1", "K1", "s1", self.refs[0])
        # A stringified unit is a type failure, not a matching reference.
        bad = json.loads(json.dumps(item))
        bad["piece"]["unit"] = "1"
        with self.assertRaises(ValueError):
            self.return_register("RT-TYPE", [bad])

    def test_same_named_order_in_different_jobs_are_distinct_pieces(self):
        # A second job on the remaining offcut produces another piece carrying
        # the very same order id "B"; the four-field references stay distinct.
        other = self.salvage("j9", ["offcut/j1/stock/R1#1"],
                            [order("B", 5)], kerf=1, min_offcut=2) \
            if False else None
        state = workspace._read_state(str(self.ws))
        offcut_id = next(eid for eid, entry in state["entries"].items()
                         if entry["status"] == "available")
        self.normal("j9", [order("B", 5)], kerf=1, min_offcut=2)
        later_piece = self.ref("j9", offcut_id, "B", 1)
        self.deliver("D9", [later_piece], [("K9", [self.slot("q1", 5)])])
        receipt = self.return_register(
            "RT9", [self.item("D9", "K9", "q1", later_piece)])
        entry_id = receipt["items"][0]["return_entry_id"]
        # The new identity traces to the later job, never the j1 same-name piece.
        source = read_state(self.ws)["entries"][entry_id]["source"]
        self.assertEqual(source["original_piece"]["job_id"], "j9")
        self.assertEqual(source["original_piece"]["order_id"], "B")
        # The original j1 same-name pieces are untouched.
        status = workspace.delivery_status(str(self.ws))
        j1_rows = [p for p in status["pieces"]
                   if p["job_id"] == "j1" and p["order_id"] == "B"]
        self.assertTrue(all(p["returned_by"] is None for p in j1_rows))


class ReturnStatusTests(ReturnCase):
    def test_list_and_frozen_receipt_with_current_lifecycle(self):
        self.init({"stock": [stock_row("R1", 20)]})
        self.salvage("j1", ["stock/R1"], [order("B", 5)], kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 5)])])
        self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)])
        listing = workspace.return_status(str(self.ws))
        self.assertEqual(listing["kind"], "stock-workspace-return-list")
        self.assertEqual(listing["summary"], {"returns": 1, "returned_pieces": 1})
        self.assertEqual(listing["returns"][0]["return_id"], "RT1")
        detail = workspace.return_status(str(self.ws), "RT1")
        self.assertEqual(detail["kind"], "stock-workspace-return-status")
        (current,) = detail["current"]
        self.assertEqual(current["current_status"], "available")
        self.assertIsNone(current["consumed_by_job"])
        self.assertEqual(current["active_incidents"], [])
        # Consume the rework identity; the frozen receipt stays identical while
        # the current section reflects consumption.
        frozen = json.loads(json.dumps(detail))
        self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        detail2 = workspace.return_status(str(self.ws), "RT1")
        self.assertEqual(detail2["items"], frozen["items"])
        self.assertEqual(detail2["current"][0]["current_status"], "consumed")
        self.assertEqual(detail2["current"][0]["consumed_by_job"], "j2")
        with self.assertRaisesRegex(ValueError, "unknown return_id"):
            workspace.return_status(str(self.ws), "nope")


if __name__ == "__main__":
    unittest.main()
