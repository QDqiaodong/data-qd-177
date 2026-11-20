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
from sawmill.workspace import ConflictError

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def order(order_id, piece_mm, quantity=1, material="oak"):
    return {"id": order_id, "material": material, "piece_mm": piece_mm,
            "quantity": quantity}


def stock_row(stock_id, length, material="oak", head=0, tail=0):
    return {"id": stock_id, "material": material, "length_mm": length,
            "trim_head_mm": head, "trim_tail_mm": tail}


def batch(job_id, orders, kerf=3, min_offcut=100):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "orders": orders}


def price(price_id, stock_id, amount_cents, expected_price_revision):
    return {"price_id": price_id, "stock_id": stock_id,
            "amount_cents": amount_cents,
            "expected_price_revision": expected_price_revision}


def close(close_id, previous_close_id, inventory_revision, price_revision):
    return {"close_id": close_id, "previous_close_id": previous_close_id,
            "expected_inventory_revision": inventory_revision,
            "expected_price_revision": price_revision}


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


class ConservationChecker:
    """Independent conservation audit of a cost detail.

    Rebuilds every summary bucket from the raw detail lines, proves each
    ticket's segments sum to its input cost, proves offcut costs flow exactly
    once into either inventory or a later ticket's input, and proves no
    inventory identity is counted twice.
    """

    def check(self, detail):
        by_root = {}
        for key, rows in (("pieces_cents", detail["pieces"]),
                          ("loss_cents", detail["losses"]),
                          ("inventory_cents", detail["inventory"])):
            for row in rows:
                bucket = by_root.setdefault(row["root_stock_id"], {
                    "pieces_cents": 0, "loss_cents": 0, "inventory_cents": 0})
                bucket[key] += row["amount_cents"]
        summary = detail["summary"]["by_root"]
        assert set(by_root) == set(summary), "root coverage mismatch"
        for root, bucket in by_root.items():
            for key, value in bucket.items():
                assert summary[root][key] == value, f"{root} {key} mismatch"
            assert sum(bucket.values()) == summary[root]["price_cents"], (
                f"root {root} does not conserve its price")
        for ticket in detail["tickets"]:
            assert (sum(segment["amount_cents"] for segment in ticket["segments"])
                    == ticket["input_cents"]), f"ticket {ticket['job_id']} not conserved"
        inventory_ids = [row["entry_id"] for row in detail["inventory"]]
        assert len(inventory_ids) == len(set(inventory_ids)), "inventory double counted"
        # Every reusable offcut's carried cost lands in exactly one place:
        # either still inventory, or the input of exactly one later ticket.
        inputs = {}
        for ticket in detail["tickets"]:
            assert ticket["entry_id"] not in inputs, "entry consumed twice"
            inputs[ticket["entry_id"]] = ticket["input_cents"]
        inventory_cost = dict(zip(inventory_ids,
                                  (row["amount_cents"] for row in detail["inventory"])))
        for ticket in detail["tickets"]:
            for segment in ticket["segments"]:
                if segment["kind"] != "offcut" or not segment["reusable"]:
                    continue
                offcut_id = segment["entry_id"]
                landed = []
                if offcut_id in inventory_cost:
                    landed.append(inventory_cost[offcut_id])
                if offcut_id in inputs:
                    landed.append(inputs[offcut_id])
                assert landed == [segment["amount_cents"]], (
                    f"offcut {offcut_id} cost lost or duplicated")


class CostCase(unittest.TestCase):
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

    def init(self, stock):
        return workspace.init_workspace(str(self.ws), self.source(stock))

    def commit_new(self, request):
        self.seq += 1
        plan_path = self.out(f"plan-{self.seq}.json")
        workspace.plan(str(self.ws), self.source(request), plan_path)
        return workspace.commit(str(self.ws), plan_path, self.out(f"result-{self.seq}.json"))

    def register_price(self, value, output=None):
        self.seq += 1
        return workspace.register_price(
            str(self.ws), self.source(value), output or self.out(f"price-{self.seq}.json"))

    def cost(self):
        return workspace.cost_query(str(self.ws))

    def close_run(self, value, output=None):
        self.seq += 1
        return workspace.close_run(
            str(self.ws), self.source(value), output or self.out(f"close-{self.seq}.json"))

    def piece_amounts(self, detail):
        return {(row["job_id"], row["entry_id"], row["order_id"], row["unit"]):
                row["amount_cents"] for row in detail["pieces"]}

    def bridge_lines(self, result):
        return {line["line_id"]: line for line in result["bridge"]["lines"]}


class TrimAndMultiMaterialTests(CostCase):
    """Hand-computed conservation across trims, kerfs, and two materials."""

    STOCK = {"stock": [stock_row("OAK1", 100, "oak", 5, 5),
                       stock_row("OAK2", 50, "oak", 0, 0),
                       stock_row("PIN1", 40, "pine", 0, 0)]}

    def setUp(self):
        super().setUp()
        self.init(self.STOCK)
        self.register_price(price("P-OAK1", "OAK1", 1000, 0))
        self.register_price(price("P-OAK2", "OAK2", 700, 1))
        self.register_price(price("P-PIN1", "PIN1", 250, 2))
        self.commit_new(batch("b1", [order("oak-a", 30, 2),
                                     order("pine-a", 20, 1, "pine")],
                              kerf=2, min_offcut=10))
        self.commit_new(batch("b2", [order("oak-b", 40)], kerf=2, min_offcut=10))

    def test_hand_computed_amounts(self):
        detail = self.cost()
        self.assertEqual(self.piece_amounts(detail), {
            ("b1", "stock/OAK2", "oak-a", 1): 420,      # 700 * 30/50
            ("b1", "stock/OAK1", "oak-a", 2): 300,      # 1000 * 30/100
            ("b1", "stock/PIN1", "pine-a", 1): 125,     # 250 * 20/40
            ("b2", "offcut/b1/stock/OAK1", "oak-b", 1): 400,  # 580 * 40/58
        })
        inventory = {row["entry_id"]: row["amount_cents"]
                     for row in detail["inventory"]}
        self.assertEqual(inventory, {
            "offcut/b1/stock/OAK2": 252,                # 700 * 18/50
            "offcut/b1/stock/PIN1": 112,                # floor(250*18/40), loses tie
            "offcut/b2/offcut/b1/stock/OAK1": 160,      # 580 * 16/58
        })
        losses = {(row["job_id"], row["entry_id"], row["category"],
                   row["start_mm"], row["end_mm"]): row["amount_cents"]
                  for row in detail["losses"]}
        # PIN1 kerf [20,22) wins the 1-cent remainder tie over offcut [22,40)
        # because its physical start is smaller.
        self.assertEqual(losses[("b1", "stock/PIN1", "kerf", 20, 22)], 13)
        self.assertEqual(losses[("b1", "stock/OAK1", "trim_head", 0, 5)], 50)
        self.assertEqual(losses[("b1", "stock/OAK1", "trim_tail", 95, 100)], 50)
        self.assertEqual(losses[("b1", "stock/OAK1", "kerf", 35, 37)], 20)
        self.assertEqual(losses[("b1", "stock/OAK2", "kerf", 30, 32)], 28)
        self.assertEqual(losses[("b2", "offcut/b1/stock/OAK1", "kerf", 40, 42)], 20)

    def test_conservation_summaries(self):
        detail = self.cost()
        ConservationChecker().check(detail)
        self.assertEqual(detail["summary"]["by_root"], {
            "OAK1": {"pieces_cents": 700, "loss_cents": 140, "inventory_cents": 160,
                     "total_cents": 1000, "price_cents": 1000},
            "OAK2": {"pieces_cents": 420, "loss_cents": 28, "inventory_cents": 252,
                     "total_cents": 700, "price_cents": 700},
            "PIN1": {"pieces_cents": 125, "loss_cents": 13, "inventory_cents": 112,
                     "total_cents": 250, "price_cents": 250},
        })
        self.assertEqual(detail["summary"]["by_material"], {
            "oak": {"pieces_cents": 1120, "loss_cents": 168, "inventory_cents": 412,
                    "total_cents": 1700, "price_cents": 1700},
            "pine": {"pieces_cents": 125, "loss_cents": 13, "inventory_cents": 112,
                     "total_cents": 250, "price_cents": 250},
        })
        self.assertEqual(detail["summary"]["total"],
                         {"pieces_cents": 1245, "loss_cents": 181,
                          "inventory_cents": 524, "total_cents": 1950,
                          "price_cents": 1950})

    def test_ticket_inputs_and_segments(self):
        detail = self.cost()
        tickets = {(t["job_id"], t["entry_id"]): t for t in detail["tickets"]}
        oak1 = tickets[("b1", "stock/OAK1")]
        self.assertEqual(oak1["input_cents"], 1000)
        self.assertEqual(
            [(s["kind"], s["start_mm"], s["end_mm"], s["amount_cents"])
             for s in oak1["segments"]],
            [("trim_head", 0, 5, 50), ("piece", 5, 35, 300), ("kerf", 35, 37, 20),
             ("offcut", 37, 95, 580), ("trim_tail", 95, 100, 50)])
        offcut_segment = oak1["segments"][3]
        self.assertEqual(offcut_segment["entry_id"], "offcut/b1/stock/OAK1")
        self.assertTrue(offcut_segment["reusable"])
        # The second-generation ticket re-apportions the carried 580, not the root 1000.
        gen2 = tickets[("b2", "offcut/b1/stock/OAK1")]
        self.assertEqual(gen2["input_cents"], 580)
        self.assertEqual([s["amount_cents"] for s in gen2["segments"]], [400, 20, 160])


class OneCentChainTests(CostCase):
    """The acceptance scenario: 10mm zero-trim root priced at 1 cent."""

    def setUp(self):
        super().setUp()
        self.init({"stock": [stock_row("T1", 10)]})
        self.register_price(price("P1", "T1", 1, 0))
        self.commit_new(batch("batch-1", [order("a", 4)], kerf=1, min_offcut=1))
        self.commit_new(batch("batch-2", [order("b", 4)], kerf=1, min_offcut=1))

    def test_single_cent_flows_to_second_piece(self):
        detail = self.cost()
        amounts = {(row["job_id"], row["order_id"]): row["amount_cents"]
                   for row in detail["pieces"]}
        self.assertEqual(amounts, {("batch-1", "a"): 0, ("batch-2", "b"): 1})
        # The intermediate offcut carried the cent; the root was not re-split
        # directly across the two final pieces.
        ticket1 = next(t for t in detail["tickets"] if t["job_id"] == "batch-1")
        self.assertEqual([s["amount_cents"] for s in ticket1["segments"]], [0, 0, 1])
        ConservationChecker().check(detail)

    def test_reprice_to_two_cents_gives_one_each(self):
        self.register_price(price("P2", "T1", 2, 1))
        detail = self.cost()
        amounts = {(row["job_id"], row["order_id"]): row["amount_cents"]
                   for row in detail["pieces"]}
        self.assertEqual(amounts, {("batch-1", "a"): 1, ("batch-2", "b"): 1})
        ConservationChecker().check(detail)


class CloseBridgeTests(CostCase):
    """Hand-computed first/second close bridges on the 1-cent scenario."""

    def setUp(self):
        super().setUp()
        self.init({"stock": [stock_row("T1", 10)]})
        self.register_price(price("P1", "T1", 1, 0))
        self.commit_new(batch("batch-1", [order("a", 4)], kerf=1, min_offcut=1))

    def test_close_reprice_cut_close(self):
        first = self.close_run(close("C1", None, 1, 1))
        self.assertEqual(first["inventory_revision"], 1)
        self.assertEqual(first["price_revision"], 1)
        self.assertEqual(first["prices"], {"T1": 1})
        self.assertEqual(first["bridge"]["kind"], "opening")
        lines = self.bridge_lines(first)
        # Opening projection: revision 0 at this round's price, all stock held.
        self.assertEqual(
            [tuple(lines[key][field] for field in
                   ("opening_cents", "price_change_cents", "production_cents",
                    "closing_cents")) for key in
             ("inventory/stock/T1", "inventory/offcut/batch-1/stock/T1",
              "piece/batch-1/stock/T1/a/1", "loss/batch-1/stock/T1/kerf/4-5")],
            [(1, 0, -1, 0), (0, 0, 1, 1), (0, 0, 0, 0), (0, 0, 0, 0)])
        self.assertEqual(first["bridge"]["summary"]["by_root"]["T1"],
                         {"opening_cents": 1, "price_change_cents": 0,
                          "production_cents": 0, "closing_cents": 1})

        self.register_price(price("P2", "T1", 2, 1))
        self.commit_new(batch("batch-2", [order("b", 4)], kerf=1, min_offcut=1))
        second = self.close_run(close("C2", "C1", 2, 2))
        self.assertEqual(second["bridge"]["kind"], "continuing")
        self.assertEqual(second["bridge"]["previous_close_id"], "C1")
        lines = self.bridge_lines(second)
        fields = ("opening_cents", "price_change_cents", "production_cents",
                  "closing_cents")
        # The old offcut: revalued on the old snapshot (still 1), then consumed.
        self.assertEqual(tuple(lines["inventory/offcut/batch-1/stock/T1"][f] for f in fields),
                         (1, 0, -1, 0))
        # The first piece's reprice is historical price change, not production.
        self.assertEqual(tuple(lines["piece/batch-1/stock/T1/a/1"][f] for f in fields),
                         (0, 1, 0, 1))
        # The new piece is pure production transfer.
        self.assertEqual(
            tuple(lines["piece/batch-2/offcut/batch-1/stock/T1/b/1"][f] for f in fields),
            (0, 0, 1, 1))
        # Per root: production nets to zero, price change equals the 1->2 reprice.
        self.assertEqual(second["bridge"]["summary"]["by_root"]["T1"],
                         {"opening_cents": 1, "price_change_cents": 1,
                          "production_cents": 0, "closing_cents": 2})
        for line in second["bridge"]["lines"]:
            self.assertEqual(line["opening_cents"] + line["price_change_cents"]
                             + line["production_cents"], line["closing_cents"])
        # The frozen first close is untouched by the reprice and the new cut.
        self.assertEqual(workspace.close_status(str(self.ws), "C1"), first)
        self.assertEqual(workspace.close_status(str(self.ws), "C2"), second)
        self.assertEqual([row["close_id"] for row in
                          workspace.close_status(str(self.ws))["closes"]], ["C1", "C2"])

    def test_close_without_cut_or_reprice_has_zero_changes(self):
        first = self.close_run(close("C1", None, 1, 1))
        second = self.close_run(close("C2", "C1", 1, 1))
        for line in second["bridge"]["lines"]:
            self.assertEqual(line["price_change_cents"], 0)
            self.assertEqual(line["production_cents"], 0)
            self.assertEqual(line["opening_cents"], line["closing_cents"])
        self.assertEqual(second["bridge"]["summary"]["total"],
                         {"opening_cents": 1, "price_change_cents": 0,
                          "production_cents": 0, "closing_cents": 1})
        self.assertEqual(second["detail"], first["detail"])


class MultiGenerationTests(CostCase):
    def test_three_generations_conserve_without_double_counting(self):
        self.init({"stock": [stock_row("G1", 20)]})
        self.register_price(price("P1", "G1", 100, 0))
        self.commit_new(batch("g1", [order("p1", 10)], kerf=0, min_offcut=1))
        self.commit_new(batch("g2", [order("p2", 5)], kerf=0, min_offcut=1))
        self.commit_new(batch("g3", [order("p3", 2)], kerf=0, min_offcut=1))
        detail = self.cost()
        amounts = {(row["job_id"], row["order_id"]): row["amount_cents"]
                   for row in detail["pieces"]}
        self.assertEqual(amounts, {("g1", "p1"): 50, ("g2", "p2"): 25, ("g3", "p3"): 10})
        # Only the latest, unconsumed generation remains in inventory.
        self.assertEqual([row["entry_id"] for row in detail["inventory"]],
                         ["offcut/g3/offcut/g2/offcut/g1/stock/G1"])
        self.assertEqual(detail["inventory"][0]["amount_cents"], 15)
        self.assertEqual(detail["summary"]["by_root"]["G1"],
                         {"pieces_cents": 85, "loss_cents": 0, "inventory_cents": 15,
                          "total_cents": 100, "price_cents": 100})
        ConservationChecker().check(detail)
        # A price correction propagates through every consumed generation.
        self.register_price(price("P2", "G1", 200, 1))
        detail = self.cost()
        amounts = {(row["job_id"], row["order_id"]): row["amount_cents"]
                   for row in detail["pieces"]}
        self.assertEqual(amounts, {("g1", "p1"): 100, ("g2", "p2"): 50, ("g3", "p3"): 20})
        self.assertEqual(detail["inventory"][0]["amount_cents"], 30)
        ConservationChecker().check(detail)

    def test_non_lexicographic_job_revisions(self):
        self.init({"stock": [stock_row("NL1", 20)]})
        self.register_price(price("P1", "NL1", 100, 0))
        self.commit_new(batch("job-9", [order("p1", 10)], kerf=0, min_offcut=1))
        self.commit_new(batch("job-10", [order("p2", 4)], kerf=0, min_offcut=1))
        detail = self.cost()
        amounts = {(row["job_id"], row["order_id"]): row["amount_cents"]
                   for row in detail["pieces"]}
        # job-10 consumed job-9's offcut: revision order, not string order.
        self.assertEqual(amounts, {("job-9", "p1"): 50, ("job-10", "p2"): 20})
        self.assertEqual(detail["inventory"][0]["entry_id"],
                         "offcut/job-10/offcut/job-9/stock/NL1")
        self.assertEqual(detail["inventory"][0]["amount_cents"], 30)
        ConservationChecker().check(detail)


class PriceRegistrationTests(CostCase):
    def test_invalid_inputs_fail_and_preserve_state(self):
        self.init({"stock": [stock_row("S1", 100), stock_row("S2", 100)]})
        baseline = read_state(self.ws)
        bad_inputs = [
            price("P", "S1", -1, 0),                 # negative amount
            price("P", "S1", 1.5, 0),                # non-integer amount
            price("P", "S1", True, 0),               # bool is not an integer
            price("P", "S1", "100", 0),              # string amount
            price("", "S1", 1, 0),                   # empty price_id
            price("P", "", 1, 0),                    # empty stock_id
            price("P", "NOPE", 1, 0),                # unknown root
            price("P", "offcut/x/stock/S1", 1, 0),   # not an initial root
            price("P", "S1", 1, -1),                 # negative expected revision
            price("P", "S1", 1, 3),                  # stale expected revision
            {"price_id": "P", "stock_id": "S1", "amount_cents": 1},  # missing key
        ]
        for value in bad_inputs:
            with self.assertRaises((ValueError, KeyError, TypeError)):
                self.register_price(value)
            self.assertEqual(read_state(self.ws), baseline)
            self.assertFalse(any(self.dir.glob("price-*.json")))

    def test_replay_priority_conflict_and_plan_stability(self):
        self.init({"stock": [stock_row("S1", 500), stock_row("S2", 500)]})
        first = self.register_price(price("P1", "S1", 100, 0))
        self.assertEqual(first["price_revision"], 1)
        self.assertIsNone(first["previous_amount_cents"])
        second = self.register_price(price("P2", "S1", 250, 1))
        self.assertEqual(second["previous_amount_cents"], 100)
        # Identical retry returns the original result even though the expected
        # revision recorded in the request is long past.
        self.assertEqual(self.register_price(price("P1", "S1", 100, 0)), first)
        self.assertEqual(read_state(self.ws)["price_revision"], 2)
        # Same id with any different input conflicts.
        for value in (price("P1", "S1", 101, 2), price("P1", "S2", 100, 2),
                      price("P1", "S1", 100, 2)):
            with self.assertRaises(ConflictError):
                self.register_price(value)
        self.assertEqual(read_state(self.ws)["price_revision"], 2)
        # Registration never advances the inventory revision or stales plans.
        plan_path = self.out("plan.json")
        workspace.plan(str(self.ws), self.source(batch("b1", [order("x", 100)])),
                       plan_path)
        self.register_price(price("P3", "S2", 7, 2))
        self.assertEqual(read_state(self.ws)["revision"], 0)
        committed = workspace.commit(str(self.ws), plan_path, self.out("r.json"))
        self.assertEqual(committed["revision"], 1)
        status = workspace.price_status(str(self.ws))
        self.assertEqual(status["prices"], {"S1": 250, "S2": 7})
        self.assertEqual(status["missing_stock_ids"], [])
        self.assertEqual([row["price_id"] for row in status["registrations"]],
                         ["P1", "P2", "P3"])


class MissingAndZeroPriceTests(CostCase):
    def test_zero_price_is_registered_and_missing_roots_are_listed(self):
        self.init({"stock": [stock_row("S1", 100), stock_row("S2", 100)]})
        self.register_price(price("P1", "S1", 0, 0))  # zero is a legal price
        status = workspace.price_status(str(self.ws))
        self.assertEqual(status["prices"], {"S1": 0})
        self.assertEqual(status["missing_stock_ids"], ["S2"])
        with self.assertRaisesRegex(ValueError, "S2"):
            self.cost()
        with self.assertRaisesRegex(ValueError, "S2"):
            self.close_run(close("C1", None, 0, 1))
        self.assertEqual(read_state(self.ws)["closes"], {})
        self.register_price(price("P2", "S2", 5, 1))
        detail = self.cost()
        self.assertEqual(detail["summary"]["by_root"]["S1"],
                         {"pieces_cents": 0, "loss_cents": 0, "inventory_cents": 0,
                          "total_cents": 0, "price_cents": 0})
        self.assertEqual(detail["summary"]["total"]["total_cents"], 5)
        ConservationChecker().check(detail)
        result = self.close_run(close("C1", None, 0, 2))
        self.assertEqual(result["prices"], {"S1": 0, "S2": 5})


class IsolationCostTests(CostCase):
    def test_isolation_and_release_do_not_change_amounts(self):
        self.init({"stock": [stock_row("S1", 1200, "oak", 10, 10)]})
        self.register_price(price("P1", "S1", 120000, 0))
        self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        before = self.cost()
        self.assertEqual(before["inventory"][0]["entry_id"], "offcut/batch-1/stock/S1")
        self.assertEqual(before["inventory"][0]["amount_cents"], 37400)
        # Isolating the root's offcut changes the revision but not any amount.
        self.seq += 1
        workspace.register_incident(str(self.ws), self.source(
            {"incident_id": "INC-1", "stock_ids": ["S1"]}), self.out("inc.json"))
        self.assertEqual(read_state(self.ws)["revision"], 2)
        during = self.cost()
        for key in ("pieces", "losses", "inventory", "tickets", "summary", "prices"):
            self.assertEqual(during[key], before[key])
        self.assertEqual(during["revision"], 2)
        # A quality-event-only revision is a valid close snapshot.
        closed = self.close_run(close("C1", None, 2, 1))
        self.assertEqual(closed["detail"]["summary"], before["summary"])
        # Releasing changes nothing either; a no-change close has zero bridge.
        workspace.release_incident(str(self.ws), "INC-1", self.out("rel.json"))
        self.assertEqual(read_state(self.ws)["revision"], 3)
        after = self.cost()
        self.assertEqual(after["summary"], before["summary"])
        second = self.close_run(close("C2", "C1", 3, 1))
        self.assertEqual(second["bridge"]["summary"]["total"],
                         {"opening_cents": 120000, "price_change_cents": 0,
                          "production_cents": 0, "closing_cents": 120000})


class CloseChainTests(CostCase):
    def setUp(self):
        super().setUp()
        self.init({"stock": [stock_row("S1", 500), stock_row("S2", 500)]})
        self.register_price(price("P1", "S1", 100, 0))
        self.register_price(price("P2", "S2", 200, 1))
        self.commit_new(batch("b1", [order("x", 100)]))

    def test_version_and_chain_conflicts(self):
        # The first close must have a null predecessor.
        with self.assertRaises(ConflictError):
            self.close_run(close("C1", "NOPE", 1, 2))
        # Stale expected revisions are rejected before anything is recorded.
        with self.assertRaisesRegex(ValueError, "expected_inventory_revision"):
            self.close_run(close("C1", None, 0, 2))
        with self.assertRaisesRegex(ValueError, "expected_price_revision"):
            self.close_run(close("C1", None, 1, 1))
        self.assertEqual(read_state(self.ws)["closes"], {})
        first = self.close_run(close("C1", None, 1, 2))
        second = self.close_run(close("C2", "C1", 1, 2))
        # Identical retry returns the frozen result even though newer closes
        # exist and the recorded expected revisions are stale.
        self.assertEqual(self.close_run(close("C1", None, 1, 2)), first)
        # Same id with different input conflicts rather than re-closing.
        with self.assertRaises(ConflictError):
            self.close_run(close("C1", None, 0, 0))
        # Forks are rejected: the predecessor must be the latest close.
        with self.assertRaises(ConflictError):
            self.close_run(close("C3", "C1", 1, 2))
        with self.assertRaises(ConflictError):
            self.close_run(close("C3", None, 1, 2))
        third = self.close_run(close("C3", "C2", 1, 2))
        self.assertEqual(third["previous_close_id"], "C2")
        self.assertEqual([row["close_id"] for row in
                          workspace.close_status(str(self.ws))["closes"]],
                         ["C1", "C2", "C3"])
        self.assertEqual(workspace.close_status(str(self.ws), "C2"), second)
        with self.assertRaisesRegex(ValueError, "unknown close_id"):
            workspace.close_status(str(self.ws), "NOPE")

    def test_close_replay_output_is_byte_identical(self):
        first = self.close_run(close("C1", None, 1, 2), output=self.out("c1.json"))
        replay = self.close_run(close("C1", None, 1, 2), output=self.out("c1b.json"))
        self.assertEqual(replay, first)
        self.assertEqual(Path(self.out("c1.json")).read_bytes(),
                         Path(self.out("c1b.json")).read_bytes())


class CrashRecoveryTests(CostCase):
    def setUp(self):
        super().setUp()
        self.init({"stock": [stock_row("S1", 500)]})
        self.register_price(price("P1", "S1", 100, 0))
        self.commit_new(batch("b1", [order("x", 100)]))

    def test_failed_price_state_write_leaves_no_partial_registration(self):
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace.os.replace",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.register_price(price("P2", "S1", 300, 1))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        result = self.register_price(price("P2", "S1", 300, 1))
        self.assertEqual(result["price_revision"], 2)
        self.assertEqual(len(read_state(self.ws)["price_log"]), 2)

    def test_price_publish_crash_replays_without_double_registration(self):
        output = self.out("p2.json")
        with mock.patch("sawmill.workspace.publish",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.register_price(str(self.ws), self.source(
                    price("P2", "S1", 300, 1)), output)
        self.assertFalse(Path(output).exists())
        state = read_state(self.ws)
        self.assertEqual(state["price_revision"], 2)
        self.assertEqual(len(state["price_log"]), 2)
        replay = workspace.register_price(str(self.ws), self.source(
            price("P2", "S1", 300, 1)), output)
        self.assertTrue(Path(output).exists())
        self.assertEqual(replay["price_revision"], 2)
        self.assertEqual(len(read_state(self.ws)["price_log"]), 2)

    def test_failed_close_state_write_leaves_no_partial_close(self):
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace.os.replace",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.close_run(close("C1", None, 1, 1))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        result = self.close_run(close("C1", None, 1, 1))
        self.assertEqual(read_state(self.ws)["close_order"], ["C1"])
        self.assertEqual(result["close_id"], "C1")

    def test_close_publish_crash_replays_frozen_result(self):
        output = self.out("c1.json")
        with mock.patch("sawmill.workspace.publish",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.close_run(str(self.ws), self.source(close("C1", None, 1, 1)),
                                    output)
        self.assertFalse(Path(output).exists())
        self.assertEqual(read_state(self.ws)["close_order"], ["C1"])
        replay = workspace.close_run(str(self.ws), self.source(close("C1", None, 1, 1)),
                                     output)
        self.assertTrue(Path(output).exists())
        self.assertEqual(replay, workspace.close_status(str(self.ws), "C1"))
        self.assertEqual(read_state(self.ws)["close_order"], ["C1"])

    def test_conflicting_output_is_rejected_before_state_changes(self):
        Path(self.out("taken.json")).write_text('{"not": "this"}\n')
        before = (self.ws / "state.json").read_bytes()
        with self.assertRaises(FileExistsError):
            workspace.close_run(str(self.ws), self.source(close("C1", None, 1, 1)),
                                self.out("taken.json"))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        with self.assertRaises(FileExistsError):
            workspace.register_price(str(self.ws), self.source(price("P2", "S1", 1, 1)),
                                     self.out("taken.json"))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertEqual(Path(self.out("taken.json")).read_text(), '{"not": "this"}\n')


class ConcurrencyTests(CostCase):
    def test_concurrent_identical_closes_and_competing_closes(self):
        self.init({"stock": [stock_row("S1", 500)]})
        self.register_price(price("P1", "S1", 100, 0))
        self.commit_new(batch("b1", [order("x", 100)]))
        results, errors = {}, {}

        def run(key, value):
            try:
                results[key] = workspace.close_run(
                    str(self.ws), self.source(value), self.out(f"{key}.json"))
            except (OSError, ValueError) as exc:
                errors[key] = exc

        threads = [threading.Thread(target=run, args=(f"c1-{i}", close("C1", None, 1, 1)))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, {})
        self.assertEqual(len({json.dumps(r, sort_keys=True) for r in results.values()}), 1)
        self.assertEqual(read_state(self.ws)["close_order"], ["C1"])
        # Two competing next closes: exactly one extends the chain.
        results.clear()
        threads = [threading.Thread(target=run, args=("c2", close("C2", "C1", 1, 1))),
                   threading.Thread(target=run, args=("c3", close("C3", "C1", 1, 1)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), ConflictError)
        self.assertEqual(len(read_state(self.ws)["close_order"]), 2)


class UnfilledAndPlanTests(CostCase):
    def test_plans_and_unfilled_orders_produce_no_cost(self):
        self.init({"stock": [stock_row("S1", 100)]})
        self.register_price(price("P1", "S1", 1000, 0))
        # An uncommitted plan changes nothing.
        workspace.plan(str(self.ws), self.source(batch("b1", [order("x", 30)])),
                       self.out("plan.json"))
        detail = self.cost()
        self.assertEqual(detail["pieces"], [])
        self.assertEqual(detail["inventory"][0]["amount_cents"], 1000)
        # Unfilled pieces have no cost lines; conservation still holds.
        self.commit_new(batch("b1", [order("x", 30), order("huge", 5000)],
                              kerf=0, min_offcut=1))
        detail = self.cost()
        self.assertEqual([row["order_id"] for row in detail["pieces"]], ["x"])
        self.assertEqual(self.piece_amounts(detail),
                         {("b1", "stock/S1", "x", 1): 300})
        ConservationChecker().check(detail)


class OldWorkspaceCompatibilityTests(CostCase):
    def test_workspace_without_cost_fields_upgrades_in_place(self):
        self.init({"stock": [stock_row("S1", 500)]})
        self.commit_new(batch("b1", [order("x", 100)]))
        # Simulate a workspace written before costs and incidents existed.
        state = read_state(self.ws)
        for key in ("incidents", "price_revision", "price_log", "closes", "close_order"):
            state.pop(key, None)
        (self.ws / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        self.register_price(price("P1", "S1", 100, 0))
        detail = self.cost()
        self.assertEqual(detail["summary"]["by_root"]["S1"]["total_cents"], 100)
        closed = self.close_run(close("C1", None, 1, 1))
        self.assertEqual(closed["close_id"], "C1")
        ConservationChecker().check(detail)


class CostCliTests(CostCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "sawmill.inventory_cli", *args],
                              capture_output=True, text=True, env=env)

    def test_end_to_end_and_failure_exits(self):
        ws = self.out("ws")
        stock = self.source({"stock": [stock_row("T1", 10)]})
        self.assertEqual(self.run_cli("init", "--source", stock,
                                      "--workspace", ws).returncode, 0)
        # Cost query before any price: lists the missing root, exits 2.
        proc = self.run_cli("cost", "--workspace", ws)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("T1", proc.stderr)
        price_src = self.source(price("P1", "T1", 1, 0))
        price_out = self.out("price.json")
        proc = self.run_cli("price", "register", "--workspace", ws,
                            "--source", price_src, "--output", price_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("price", "status", "--workspace", ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["prices"], {"T1": 1})
        batch_src = self.source(batch("batch-1", [order("a", 4)], kerf=1, min_offcut=1))
        self.assertEqual(self.run_cli("plan", "--workspace", ws, "--source", batch_src,
                                      "--output", self.out("plan.json")).returncode, 0)
        self.assertEqual(self.run_cli("commit", "--workspace", ws, "--plan",
                                      self.out("plan.json"), "--output",
                                      self.out("r1.json")).returncode, 0)
        proc = self.run_cli("cost", "--workspace", ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        detail = json.loads(proc.stdout)
        self.assertEqual(detail["summary"]["total"]["total_cents"], 1)
        close_src = self.source(close("C1", None, 1, 1))
        close_out = self.out("close.json")
        proc = self.run_cli("close", "run", "--workspace", ws,
                            "--source", close_src, "--output", close_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        frozen = json.loads(Path(close_out).read_text())
        # Idempotent retry against the same output path succeeds unchanged.
        proc = self.run_cli("close", "run", "--workspace", ws,
                            "--source", close_src, "--output", close_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(Path(close_out).read_text()), frozen)
        proc = self.run_cli("close", "status", "--workspace", ws, "--close", "C1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), frozen)
        proc = self.run_cli("close", "status", "--workspace", ws)
        self.assertEqual([row["close_id"] for row in json.loads(proc.stdout)["closes"]],
                         ["C1"])
        # Stale versions, conflicts, and unknown closes exit 2.
        stale = self.source(close("C2", "C1", 0, 1))
        proc = self.run_cli("close", "run", "--workspace", ws, "--source", stale,
                            "--output", self.out("c2.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(Path(self.out("c2.json")).exists())
        proc = self.run_cli("close", "status", "--workspace", ws, "--close", "NOPE")
        self.assertEqual(proc.returncode, 2)
        bad_price = self.source(price("P2", "T1", -5, 1))
        proc = self.run_cli("price", "register", "--workspace", ws,
                            "--source", bad_price, "--output", self.out("bad.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(Path(self.out("bad.json")).exists())


if __name__ == "__main__":
    unittest.main()
