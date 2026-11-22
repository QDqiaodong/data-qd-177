"""Tests for local inspections and the salvage cutting process.

The optimizer is checked against a fully independent exhaustive reference
that shares no code with the solver under test, and every salvage ticket is
audited with a per-millimetre physical ledger proving each millimetre of the
consumed parent belongs to exactly one segment.
"""
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sawmill import salvage as salvage_mod
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


def batch(job_id, orders, kerf=1, min_offcut=2):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "orders": orders}


def salvage_request(job_id, candidates, orders, kerf=1, min_offcut=2):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "candidate_entry_ids": candidates, "orders": orders}


def inspection(inspection_id, entry_id, zones, expected=0):
    return {"inspection_id": inspection_id, "entry_id": entry_id,
            "expected_inventory_revision": expected, "reject_zones": zones}


def zone(start, end):
    return {"start_mm": start, "end_mm": end}


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


# ---------------------------------------------------------------------------
# Independent brute-force reference solver
# ---------------------------------------------------------------------------


def reference_lanes(entry, zones):
    """Lane spans computed independently from the production lane builder."""
    head, tail = entry["head"], entry["tail"]
    length = entry["length"]
    spans, cursor = [], head
    for z in sorted(zones, key=lambda row: (row["start_mm"], row["end_mm"])):
        if z["start_mm"] > cursor:
            spans.append((cursor, z["start_mm"]))
        cursor = max(cursor, z["end_mm"])
    if cursor < length - tail:
        spans.append((cursor, length - tail))
    return spans

def brute_force_salvage(candidates, zone_map, orders, kerf, min_offcut):
    """Exhaustive optimum over every demand->lane (or unfilled) assignment.

    Returns the objective tuple and the assignment in the same canonical
    demand order used by the production solver.
    """
    demands = sorted(
        ((oid, u, row["material"], row["piece_mm"])
         for oid, row in orders.items() for u in range(1, row["quantity"] + 1)),
        key=lambda item: (item[0], item[1]))
    lanes = []  # (entry_index, material, start, end)
    for index, entry in enumerate(candidates):
        for start, end in reference_lanes(entry, zone_map.get(entry["id"], [])):
            lanes.append((index, entry["material"], start, end))
    best = None

    def evaluate(assignment):
        cursor = [lane[2] for lane in lanes]
        used_lanes = set()
        pieces = 0
        delivered = 0
        for demand_index, lane_index in enumerate(assignment):
            if lane_index is None:
                continue
            _, _, _, piece_mm = demands[demand_index]
            cursor[lane_index] += piece_mm + kerf
            assert cursor[lane_index] <= lanes[lane_index][3]
            used_lanes.add(lane_index)
            pieces += 1
            delivered += piece_mm
        used_entries = {lanes[i][0] for i in used_lanes}
        kerf_loss = pieces * kerf
        tail_loss = 0
        for lane_index, lane in enumerate(lanes):
            if lane_index in used_lanes:
                remainder = lane[3] - cursor[lane_index]
                if 0 < remainder < min_offcut:
                    tail_loss += remainder
            elif lane[0] in used_entries:
                remainder = lane[3] - lane[2]
                if 0 < remainder < min_offcut:
                    tail_loss += remainder
        trim_loss, reject_loss = 0, 0
        for entry_index in used_entries:
            entry = candidates[entry_index]
            trim_loss += entry["head"] + entry["tail"]
            for z in zone_map.get(entry["id"], []):
                reject_loss += z["end_mm"] - z["start_mm"]
        # Every lane of a consumed parent ends in one remainder measured from
        # the lane's own processing cursor: an untouched lane keeps its full
        # span as the remainder, which is recovered or scrapped exactly once.
        loss = trim_loss + reject_loss + kerf_loss + tail_loss
        consumed = sum(candidates[i]["length"] for i in used_entries)
        sequence = []
        lane_cursor = [lane[2] for lane in lanes]
        for demand_index, lane_index in enumerate(assignment):
            if lane_index is None:
                sequence.append((1, "", 0))
            else:
                sequence.append((0, candidates[lanes[lane_index][0]]["entry_id"],
                                 lane_cursor[lane_index]))
                lane_cursor[lane_index] += demands[demand_index][3] + kerf
        high = (pieces, delivered, -loss, -consumed)
        return high, tuple(sequence)

    def rec(demand_index, assignment, lane_cursor):
        nonlocal best
        if demand_index == len(demands):
            high, sequence = evaluate(assignment)
            if (best is None or high > best[0][0]
                    or (high == best[0][0] and sequence < best[0][1])):
                best = ((high, sequence), tuple(assignment))
            return
        oid, unit, material, piece_mm = demands[demand_index]
        options = [i for i, lane in enumerate(lanes)
                   if lane[1] == material
                   and lane[3] - lane_cursor[i] >= piece_mm + kerf]
        for lane_index in options:
            lane_cursor[lane_index] += piece_mm + kerf
            rec(demand_index + 1, assignment + [lane_index], lane_cursor)
            lane_cursor[lane_index] -= piece_mm + kerf
        rec(demand_index + 1, assignment + [None], lane_cursor)

    rec(0, [], [lane[2] for lane in lanes])
    return best, demands, lanes


class SalvageCase(unittest.TestCase):
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
        return workspace.init_workspace(str(self.ws), self.source({"stock": stock}))

    def inspect(self, inspection_id, entry_id, zones, expected=None):
        state = read_state(self.ws)
        expected = state["revision"] if expected is None else expected
        self.seq += 1
        return workspace.register_inspection(
            str(self.ws),
            self.source(inspection(inspection_id, entry_id, zones, expected)),
            self.out(f"inspection-{self.seq}.json"))

    def salvage_plan(self, request):
        self.seq += 1
        return workspace.salvage_plan(
            str(self.ws), self.source(request),
            self.out(f"splan-{self.seq}.json"))

    def salvage_commit(self, plan=None, request=None):
        if plan is None:
            plan = self.salvage_plan(request)
        self.seq += 1
        return workspace.salvage_commit(
            str(self.ws), self.plan_path(plan),
            self.out(f"sresult-{self.seq}.json"))

    def plan_path(self, obj):
        return self.source(obj)

    def normal_commit(self, request):
        self.seq += 1
        plan_path = self.out(f"plan-{self.seq}.json")
        workspace.plan(str(self.ws), self.source(request), plan_path)
        self.seq += 1
        return workspace.commit(str(self.ws), plan_path,
                                self.out(f"result-{self.seq}.json"))

    def register_price(self, stock_id, amount, expected):
        self.seq += 1
        return workspace.register_price(
            str(self.ws),
            self.source({"price_id": f"P-{self.seq}", "stock_id": stock_id,
                         "amount_cents": amount,
                         "expected_price_revision": expected}),
            self.out(f"price-{self.seq}.json"))


class HandComputedExampleTests(SalvageCase):
    """The acceptance scenario: 20mm root, [6,8) reject, 4mm piece, kerf 1."""

    def setUp(self):
        super().setUp()
        self.init([stock_row("R1", 20)])
        self.inspect("INS-1", "stock/R1", [zone(6, 8)])

    def test_plan_picks_the_right_lane_and_partitions_every_millimetre(self):
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 4)]))
        (ticket,) = plan["tickets"]
        # The piece goes to the right lane [8,20), not the first-fit left lane.
        self.assertEqual(ticket["lanes"][1]["pieces"],
                         [{"order_id": "p", "unit": 1, "start_mm": 8,
                           "end_mm": 12, "kerf_end_mm": 13,
                           "root_start_mm": 8, "root_end_mm": 12,
                           "root_kerf_end_mm": 13}])
        self.assertEqual(
            [(row["kind"], row["start_mm"], row["end_mm"])
             for row in ticket["partitions"]],
            [("offcut", 0, 6), ("defect_reject", 6, 8),
             ("piece", 8, 12), ("kerf", 12, 13), ("offcut", 13, 20)])
        self.assertEqual(plan["metrics"], {
            "delivered_pieces": 1, "delivered_length_mm": 4,
            "unrecoverable_loss_mm": 3, "consumed_parent_length_mm": 20,
            "kerf_mm_total": 1})
        self.assertEqual([e["entry_id"] for e in plan["added_entries"]],
                         ["offcut/sv1/stock/R1#1", "offcut/sv1/stock/R1#2"])
        self.assertEqual(plan["placement_sequence"],
                         [{"tag": 0, "entry_id": "stock/R1", "start_mm": 8}])

    def test_full_cost_chain_then_normal_cut_of_the_seven_mm_branch(self):
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 4)]))
        self.salvage_commit(plan)
        self.register_price("R1", 20, 0)
        detail = workspace.cost_query(str(self.ws))
        self.assertEqual(
            {(row["order_id"], row["amount_cents"]) for row in detail["pieces"]},
            {("p", 4)})
        self.assertEqual(
            {(row["category"], row["amount_cents"]) for row in detail["losses"]},
            {("defect_reject", 2), ("kerf", 1)})
        self.assertEqual(
            {row["entry_id"]: row["amount_cents"] for row in detail["inventory"]},
            {"offcut/sv1/stock/R1#1": 6, "offcut/sv1/stock/R1#2": 7})
        # The normal batch must consume the 7mm branch (root offset 13),
        # turning its 1mm remainder into scrap; the 6mm branch stays in stock.
        result = self.normal_commit(batch("b2", [order("q", 5)]))
        self.assertEqual(result["consumed_entry_ids"],
                         ["offcut/sv1/stock/R1#2"])
        self.assertEqual(result["tickets"][0]["cuts"][0]["start_mm"], 0)
        state = read_state(self.ws)
        self.assertEqual(
            state["entries"]["offcut/sv1/stock/R1#2"]["root_offset_mm"], 13)
        detail = workspace.cost_query(str(self.ws))
        totals = detail["summary"]["by_root"]["R1"]
        self.assertEqual(totals, {"pieces_cents": 9, "loss_cents": 5,
                                  "inventory_cents": 6, "total_cents": 20,
                                  "price_cents": 20})
        losses = {(row["category"], row["amount_cents"])
                  for row in detail["losses"]}
        self.assertEqual(losses, {("defect_reject", 2), ("kerf", 1),
                                  ("kerf", 1), ("offcut_scrap", 1)})
        self.assertEqual([row["entry_id"] for row in detail["inventory"]],
                         ["offcut/sv1/stock/R1#1"])

    def test_left_lane_first_fit_is_rejected_when_it_costs_more_loss(self):
        # The canonical 20mm example proves non-greedy lane choice: with one
        # root, min_offcut 2, placing the piece in the left lane would leave a
        # 1mm scrap tail, so the global optimum uses the right lane instead.
        self.ws = self.dir / "ws-non-greedy"
        self.init([stock_row("H1", 20)])
        self.inspect("INS-H", "stock/H1", [zone(6, 8)])
        plan = self.salvage_plan(salvage_request(
            "sv-h", ["stock/H1"], [order("p", 4)], kerf=1, min_offcut=2))
        self.assertEqual(plan["tickets"][0]["cuts"][0]["start_mm"], 8)
        (ticket,) = plan["tickets"]
        left_lane, right_lane = ticket["lanes"]
        self.assertFalse(left_lane["processed"])
        self.assertTrue(right_lane["processed"])
        self.assertEqual(plan["metrics"]["unrecoverable_loss_mm"], 3)


class ZoneNormalizationTests(SalvageCase):
    def test_overlapping_touching_and_unsorted_zones_merge_to_union(self):
        self.init([stock_row("R1", 100, head=5, tail=5)])
        report = self.inspect("INS-1", "stock/R1", [
            zone(50, 60), zone(20, 30), zone(25, 40), zone(40, 45),
            zone(60, 70)])
        self.assertEqual(report["raw_reject_zones"], [
            zone(20, 30), zone(25, 40), zone(40, 45), zone(50, 60),
            zone(60, 70)])
        self.assertEqual(report["reject_zones"], [
            zone(20, 45), zone(50, 70)])

    def test_empty_zones_and_full_coverage_boundaries(self):
        self.init([stock_row("R1", 100, head=10, tail=10)])
        empty = self.inspect("INS-0", "stock/R1", [])
        self.assertEqual(empty["reject_zones"], [])
        self.assertFalse(empty["entry_requires_salvage"])
        full = self.inspect("INS-1", "stock/R1", [zone(10, 90)], expected=1)
        self.assertEqual(full["reject_zones"], [zone(10, 90)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 1)]))
        self.assertEqual(plan["tickets"], [])
        self.assertEqual(plan["consumed_entry_ids"], [])
        self.assertEqual(len(plan["unfilled"]), 1)

    def test_zone_boundaries_must_respect_trims(self):
        self.init([stock_row("R1", 100, head=10, tail=10)])
        for bad in ([zone(9, 20)], [zone(80, 91)], [zone(50, 50)],
                    [zone(60, 50)]):
            with self.assertRaises(ValueError):
                self.inspect("INS-X", "stock/R1", bad)
        # Exact boundaries are legal.
        report = self.inspect("INS-1", "stock/R1", [zone(10, 11), zone(89, 90)])
        self.assertEqual(report["reject_zones"], [zone(10, 11), zone(89, 90)])

    def test_booleans_and_other_types_are_rejected(self):
        self.init([stock_row("R1", 100)])
        bad_inputs = [
            inspection("X", "stock/R1", [{"start_mm": True, "end_mm": 5}]),
            inspection("X", "stock/R1", [{"start_mm": 1, "end_mm": False}]),
            inspection("X", "stock/R1", [{"start_mm": 1.0, "end_mm": 5}]),
            inspection("X", "stock/R1", [{"start_mm": "1", "end_mm": 5}]),
            {"inspection_id": "X", "entry_id": "stock/R1",
             "expected_inventory_revision": False, "reject_zones": []},
            {"inspection_id": "", "entry_id": "stock/R1",
             "expected_inventory_revision": 0, "reject_zones": []},
            {"inspection_id": "X", "entry_id": "stock/NOPE",
             "expected_inventory_revision": 0, "reject_zones": []},
            {"inspection_id": "X", "entry_id": "stock/R1",
             "expected_inventory_revision": 0, "reject_zones": "nope"},
        ]
        for value in bad_inputs:
            with self.assertRaises((ValueError, KeyError, TypeError)):
                workspace.register_inspection(
                    str(self.ws), self.source(value), self.out("never.json"))
            self.assertFalse(Path(self.out("never.json")).exists())

    def test_more_than_32_raw_zones_fails(self):
        self.init([stock_row("R1", 1000)])
        zones = [zone(2 * i, 2 * i + 1) for i in range(33)]
        with self.assertRaisesRegex(ValueError, "32"):
            self.inspect("INS-1", "stock/R1", zones)


class InspectionLifecycleTests(SalvageCase):
    def test_each_new_registration_advances_only_inventory_revision(self):
        self.init([stock_row("R1", 100)])
        before = read_state(self.ws)
        first = self.inspect("INS-1", "stock/R1", [zone(10, 20)])
        after_first = read_state(self.ws)
        self.assertEqual(after_first["revision"], before["revision"] + 1)
        self.assertEqual(after_first.get("price_revision", 0),
                         before.get("price_revision", 0))
        self.assertEqual(after_first["finished_revision"],
                         before["finished_revision"])
        # Identical zones still advance: it is a new process basis.
        second = self.inspect("INS-2", "stock/R1", [zone(10, 20)])
        self.assertEqual(second["registered_revision"], 2)
        self.assertEqual(second["replaced_inspection_id"], "INS-1")
        # Clearing with an empty inspection advances again.
        third = self.inspect("INS-3", "stock/R1", [], expected=2)
        self.assertEqual(third["registered_revision"], 3)
        self.assertFalse(third["entry_requires_salvage"])
        status = workspace.inspection_status(str(self.ws), "stock/R1")
        self.assertEqual(status["current_inspection_id"], "INS-3")
        self.assertEqual(status["current_reject_zones"], [])
        self.assertFalse(status["requires_salvage"])
        self.assertEqual([row["inspection_id"] for row in status["inspections"]],
                         ["INS-1", "INS-2", "INS-3"])
        self.assertTrue(all(row["is_current"] is False
                            for row in status["inspections"][:2]))
        self.assertTrue(status["inspections"][2]["is_current"])

    def test_replay_by_merged_union_even_when_revision_stale_or_consumed(self):
        self.init([stock_row("R1", 100)])
        raw = [zone(30, 40), zone(10, 25), zone(20, 30)]
        first = self.inspect("INS-1", "stock/R1", raw)
        # Same id, reordered/overlapping raw zones that merge to the same union
        # and the same expected revision: the frozen receipt comes back.
        replay = workspace.register_inspection(
            str(self.ws),
            self.source(inspection("INS-1", "stock/R1",
                                   [zone(10, 25), zone(20, 30), zone(30, 40)],
                                   expected=0)),
            self.out("replay.json"))
        self.assertEqual(replay, first)
        self.assertEqual(read_state(self.ws)["revision"], 1)
        # The same id still replays after the entry is later consumed.
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        replay_after = workspace.register_inspection(
            str(self.ws),
            self.source(inspection("INS-1", "stock/R1", raw, expected=0)),
            self.out("replay2.json"))
        self.assertEqual(replay_after, first)
        # A different expected revision is a different canonical input.
        with self.assertRaises(ConflictError):
            workspace.register_inspection(
                str(self.ws),
                self.source(inspection("INS-1", "stock/R1", raw, expected=99)),
                self.out("replay3.json"))
        # Same id with a genuinely different union conflicts as well.
        with self.assertRaises(ConflictError):
            self.inspect("INS-1", "stock/R1", [zone(10, 26)])
        # A brand new inspection cannot amend a consumed entry's history.
        with self.assertRaisesRegex(ValueError, "consumed"):
            self.inspect("INS-9", "stock/R1", [zone(1, 2)])

    def test_stale_expected_revision_is_rejected_for_new_registration(self):
        self.init([stock_row("R1", 100)])
        self.inspect("INS-1", "stock/R1", [zone(10, 20)])
        with self.assertRaisesRegex(ValueError, "expected_inventory_revision"):
            self.inspect("INS-2", "stock/R1", [zone(30, 40)], expected=0)

    def test_registration_invalidates_both_kinds_of_plans(self):
        self.init([stock_row("R1", 100), stock_row("R2", 50)])
        self.inspect("INS-1", "stock/R1", [zone(10, 20)])
        salvage_plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        normal_plan_path = self.out("normal.json")
        workspace.plan(str(self.ws),
                       self.source(batch("b1", [order("q", 5)])),
                       normal_plan_path)
        self.inspect("INS-2", "stock/R1", [zone(10, 21)])
        with self.assertRaises(StalePlanError):
            self.salvage_commit(salvage_plan)
        with self.assertRaises(StalePlanError):
            workspace.commit(str(self.ws), normal_plan_path,
                             self.out("nr.json"))

    def test_inspection_allowed_under_incident_but_does_not_lift_isolation(self):
        self.init([stock_row("R1", 100)])
        self.seq += 1
        workspace.register_incident(
            str(self.ws),
            self.source({"incident_id": "QC-1", "stock_ids": ["R1"]}),
            self.out("incident.json"))
        # Inspecting an isolated entry is allowed.
        report = self.inspect("INS-1", "stock/R1", [zone(10, 20)])
        self.assertEqual(report["entry_id"], "stock/R1")
        # But neither process may consume the isolated root.
        with self.assertRaisesRegex(ValueError, "isolated"):
            self.salvage_plan(salvage_request(
                "sv1", ["stock/R1"], [order("p", 5)]))
        self.seq += 1
        normal_path = self.out("normal-under-incident.json")
        workspace.plan(str(self.ws),
                       self.source(batch("b1", [order("p", 5)])), normal_path)
        normal = json.loads(Path(normal_path).read_text())
        self.assertEqual(normal["consumed_entry_ids"], [])


class NormalPlanGuardTests(SalvageCase):
    def test_normal_plan_ignores_no_zone_entries_but_excludes_zoned_ones(self):
        self.init([stock_row("R1", 100), stock_row("R2", 60)])
        self.inspect("INS-1", "stock/R1", [zone(10, 20)])
        plan = workspace.build_plan(read_state(self.ws),
                                    batch("b1", [order("p", 30)]))
        self.assertEqual(plan["consumed_entry_ids"], ["stock/R2"])
        status = workspace.status(str(self.ws))
        by_id = {entry["entry_id"]: entry for entry in status["entries"]}
        self.assertTrue(by_id["stock/R1"]["requires_salvage"])
        self.assertFalse(by_id["stock/R1"]["eligible_for_normal_plan"])
        self.assertEqual(by_id["stock/R1"]["status"], "available")
        self.assertFalse(by_id["stock/R2"]["requires_salvage"])
        self.assertTrue(by_id["stock/R2"]["eligible_for_normal_plan"])

    def test_salvage_offcuts_start_clean_and_enter_normal_plans(self):
        self.init([stock_row("R1", 40)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)], kerf=1, min_offcut=2))
        status = workspace.status(str(self.ws))
        for entry in status["entries"]:
            if entry["kind"] == "offcut":
                self.assertEqual(entry["reject_zones"], [])
                self.assertFalse(entry["requires_salvage"])
                self.assertTrue(entry["eligible_for_normal_plan"])


class SolverFuzzTests(SalvageCase):
    """Cross-check the exact solver against the independent brute force."""

    def fuzz_case(self, rng):
        materials = ["oak", "pine"]
        candidate_count = rng.randint(1, 3)
        candidates, zone_map = [], {}
        for i in range(candidate_count):
            length = rng.randint(12, 40)
            head = rng.randint(0, min(2, length // 4))
            tail = rng.randint(0, min(2, length // 4))
            stock_id = f"R{i}"
            entry_id = f"stock/{stock_id}"
            material = materials[rng.randint(0, 1)]
            candidates.append({"id": entry_id, "entry_id": entry_id,
                               "stock_id": stock_id,
                               "length": length, "head": head, "tail": tail,
                               "material": material})
            zones = []
            low, high = head, length - tail
            cursor = low
            while rng.random() < 0.4 and cursor < high - 1:
                start = rng.randint(cursor, high - 1)
                end = rng.randint(start + 1, high)
                zones.append(zone(start, end))
                cursor = end
            zone_map[entry_id] = zones
        orders = {}
        for j in range(rng.randint(1, 4)):
            orders[f"o{j}"] = {
                "id": f"o{j}", "material": materials[rng.randint(0, 1)],
                "piece_mm": rng.randint(2, 12),
                "quantity": rng.randint(1, 2)}
        kerf = rng.randint(0, 2)
        min_offcut = rng.randint(1, 5)
        return candidates, zone_map, orders, kerf, min_offcut

    def test_many_random_instances_match_brute_force(self):
        rng = random.Random(20261122)
        for case_index in range(120):
            self.ws = self.dir / f"fuzz-ws-{case_index}"
            candidates, zone_map, orders, kerf, min_offcut = self.fuzz_case(rng)
            stock = [stock_row(c["stock_id"], c["length"], c["material"],
                               c["head"], c["tail"]) for c in candidates]
            self.init(stock)
            candidate_ids = []
            for c in candidates:
                if zone_map[c["id"]]:
                    self.inspect(f"INS-{c['id']}", c["entry_id"],
                                 zone_map[c["id"]])
                candidate_ids.append(c["entry_id"])
            request = salvage_request("sv1", candidate_ids,
                                      list(orders.values()),
                                      kerf=kerf, min_offcut=min_offcut)
            # Lane/piece caps may reject oversized fuzz inputs; skip those.
            try:
                plan = self.salvage_plan(request)
            except ValueError as exc:
                self.assertIn("lanes", str(exc))
                continue
            (best, demands, ref_lanes) = brute_force_salvage(
                candidates, zone_map, orders, kerf, min_offcut)
            (ref_high, ref_sequence), ref_assignment = best
            metrics = plan["metrics"]
            self.assertEqual(metrics["delivered_pieces"], ref_high[0], case_index)
            self.assertEqual(metrics["delivered_length_mm"], ref_high[1],
                             case_index)
            self.assertEqual(metrics["unrecoverable_loss_mm"], -ref_high[2],
                             case_index)
            self.assertEqual(metrics["consumed_parent_length_mm"],
                             -ref_high[3], case_index)
            actual_sequence = tuple(
                (row["tag"], row["entry_id"], row["start_mm"])
                for row in plan["placement_sequence"])
            self.assertEqual(actual_sequence, ref_sequence, case_index)

    def test_input_array_reorder_does_not_change_the_plan(self):
        rng = random.Random(77)
        candidates, zone_map, orders, kerf, min_offcut = self.fuzz_case(rng)
        self.ws = self.dir / "ws-reorder"
        self.init([stock_row(c["stock_id"], c["length"], c["material"], c["head"],
                             c["tail"]) for c in candidates])
        for c in candidates:
            if zone_map[c["id"]]:
                self.inspect(f"INS-{c['id']}", c["entry_id"], zone_map[c["id"]])
        base = salvage_request("sv1", [c["entry_id"] for c in candidates],
                               list(orders.values()), kerf, min_offcut)
        first = self.salvage_plan(base)
        shuffled_candidates = list(reversed(base["candidate_entry_ids"]))
        shuffled_orders = list(reversed(list(orders.values())))
        second_request = salvage_request("sv1", shuffled_candidates,
                                         shuffled_orders, kerf, min_offcut)
        second = self.salvage_plan(second_request)
        self.assertEqual(second["request"], first["request"])
        self.assertEqual(second["request_sha256"], first["request_sha256"])
        self.assertEqual(
            {key: second[key] for key in
             ("tickets", "unfilled", "placement_sequence", "metrics",
              "consumed_entry_ids", "unused_candidate_entry_ids")},
            {key: first[key] for key in
             ("tickets", "unfilled", "placement_sequence", "metrics",
              "consumed_entry_ids", "unused_candidate_entry_ids")})


class ObjectiveLevelTests(SalvageCase):
    def test_piece_count_outranks_delivered_length(self):
        # One long demand versus two short demands on the same lanes: if all
        # three fit, count ties with the long piece at best, so make the long
        # piece too big for either lane. The count optimum then fills both
        # short demands even though the long piece alone would be longer.
        self.init([stock_row("R1", 30)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"],
            [order("big", 20, 1), order("small", 4, 2)], kerf=1, min_offcut=2))
        # Lanes [0,10) and [12,30): the 20mm piece fits nowhere; both 4mm
        # pieces fit, so the count optimum is the two small pieces.
        self.assertEqual(plan["metrics"]["delivered_pieces"], 2)
        self.assertEqual(plan["metrics"]["delivered_length_mm"], 8)
        self.assertEqual([row["order_id"] for row in plan["unfilled"]], ["big"])

    def test_loss_level_and_consumed_length_level_are_distinct(self):
        # Two identical-material roots; one carries a reject zone, the other
        # is clean. Placing the piece in the clean root avoids the reject loss
        # but the zoned root is the shorter parent: the loss level decides.
        self.init([stock_row("R1", 30), stock_row("R2", 24)])
        self.inspect("INS-1", "stock/R2", [zone(5, 9)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1", "stock/R2"], [order("p", 6)],
            kerf=1, min_offcut=2))
        self.assertEqual(plan["consumed_entry_ids"], ["stock/R1"])
        # When the loss is identical, the shorter parent wins: give both
        # roots the same reject pattern so only the length differs.
        self.ws = self.dir / "ws-level4"
        self.init([stock_row("A", 30), stock_row("B", 24)])
        self.inspect("IA", "stock/A", [zone(5, 9)])
        self.inspect("IB", "stock/B", [zone(5, 9)])
        plan = self.salvage_plan(salvage_request(
            "sv2", ["stock/A", "stock/B"], [order("p", 6)],
            kerf=1, min_offcut=2))
        self.assertEqual(plan["consumed_entry_ids"], ["stock/B"])

    def test_sequence_level_prefers_smaller_entry_then_coordinate(self):
        # Identical roots with identical reject patterns make the first four
        # objective levels tie, so only the canonical sequence can decide.
        self.ws = self.dir / "ws-sequence"
        self.init([stock_row("A", 30), stock_row("B", 30)])
        self.inspect("IA", "stock/A", [zone(10, 12)])
        self.inspect("IB", "stock/B", [zone(10, 12)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/A", "stock/B"], [order("p", 4)],
            kerf=1, min_offcut=2))
        self.assertEqual(plan["consumed_entry_ids"], ["stock/A"])
        self.assertEqual(plan["placement_sequence"],
                         [{"tag": 0, "entry_id": "stock/A", "start_mm": 0}])

    def test_unfilled_tuple_sorts_after_every_placement(self):
        # A feasible placement always beats leaving the demand unfilled at the
        # sequence level, even when an unfilled-first search would find it.
        self.init([stock_row("R1", 20)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 4), order("q", 40)],
            kerf=1, min_offcut=2))
        self.assertEqual([row["tag"] for row in plan["placement_sequence"]],
                         [0, 1])
        self.assertEqual([row["order_id"] for row in plan["unfilled"]], ["q"])


class CandidateValidationTests(SalvageCase):
    def test_candidate_bounds_and_identities(self):
        self.init([stock_row("R1", 50), stock_row("R2", 50),
                   stock_row("R3", 50), stock_row("R4", 50),
                   stock_row("R5", 50)])
        base = salvage_request("sv1", [f"stock/R{i}" for i in range(1, 6)],
                               [order("p", 4)])
        with self.assertRaisesRegex(ValueError, "1..4"):
            self.salvage_plan(base)
        with self.assertRaisesRegex(ValueError, "1..4"):
            self.salvage_plan(salvage_request("sv1", [], [order("p", 4)]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.salvage_plan(salvage_request(
                "sv1", ["stock/R1", "stock/R1"], [order("p", 4)]))
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.salvage_plan(salvage_request(
                "sv1", ["stock/NOPE"], [order("p", 4)]))

    def test_consumed_and_isolated_candidates_are_rejected(self):
        self.init([stock_row("R1", 50), stock_row("R2", 50)])
        self.normal_commit(batch("b1", [order("p", 30)]))
        with self.assertRaisesRegex(ValueError, "not available"):
            self.salvage_plan(salvage_request(
                "sv1", ["stock/R1"], [order("q", 4)]))
        self.seq += 1
        workspace.register_incident(
            str(self.ws),
            self.source({"incident_id": "QC-1", "stock_ids": ["R2"]}),
            self.out("incident.json"))
        with self.assertRaisesRegex(ValueError, "isolated"):
            self.salvage_plan(salvage_request(
                "sv2", ["stock/R2"], [order("q", 4)]))

    def test_lane_and_piece_caps_fail_without_heuristic_fallback(self):
        self.init([stock_row("R1", 200)])
        # Nine disjoint reject zones create ten lanes, over the cap of eight.
        zones = [zone(10 + 20 * i, 12 + 20 * i) for i in range(9)]
        self.inspect("INS-1", "stock/R1", zones)
        with self.assertRaisesRegex(ValueError, "8"):
            self.salvage_plan(salvage_request(
                "sv1", ["stock/R1"], [order("p", 1)]))
        # One zone gives two lanes (within the lane cap) but 13 pieces exceed
        # the twelve-piece cap.
        self.ws = self.dir / "ws-caps-2"
        self.init([stock_row("R2", 300)])
        self.inspect("INS-2", "stock/R2", [zone(50, 52)])
        with self.assertRaisesRegex(ValueError, "12"):
            self.salvage_plan(salvage_request(
                "sv2", ["stock/R2"], [order("p", 1, 13)]))


class PhysicalLedgerTests(SalvageCase):
    def audit_partition(self, plan, state):
        """Every millimetre of each consumed parent belongs to one segment."""
        for ticket in plan["tickets"]:
            entry = state["entries"][ticket["entry_id"]]
            length = entry["length_mm"]
            ownership = [None] * length
            for row in ticket["partitions"]:
                self.assertGreater(row["length_mm"], 0)
                self.assertEqual(
                    row["root_end_mm"] - row["root_start_mm"], row["length_mm"])
                self.assertEqual(row["root_start_mm"] - row["start_mm"],
                                 ticket["root_offset_mm"])
                for mm in range(row["start_mm"], row["end_mm"]):
                    self.assertIsNone(ownership[mm], f"mm {mm} double owned")
                    ownership[mm] = row["kind"]
            self.assertTrue(all(kind is not None for kind in ownership))
            # Reject zones and trims are represented explicitly.
            kinds = [row["kind"] for row in ticket["partitions"]]
            self.assertIn("defect_reject", kinds)
            starts = [row["start_mm"] for row in ticket["partitions"]]
            self.assertEqual(starts, sorted(starts))
            self.assertEqual(ticket["partitions"][0]["start_mm"], 0)
            self.assertEqual(ticket["partitions"][-1]["end_mm"], length)

    def test_full_physical_partition_and_root_coordinates(self):
        self.init([stock_row("R1", 60, head=3, tail=2)])
        self.inspect("INS-1", "stock/R1", [zone(20, 25), zone(40, 42)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5, 2)], kerf=1, min_offcut=3))
        self.audit_partition(plan, read_state(self.ws))
        commit = self.salvage_commit(plan)
        self.audit_partition(commit, read_state(self.ws))

    def test_unprocessed_lanes_of_a_consumed_parent_are_recovered_or_scrapped(self):
        self.init([stock_row("R1", 40)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        # min_offcut 8: the left 10mm lane is reusable when untouched; a 4mm
        # lane would be scrapped.
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)], kerf=1, min_offcut=8))
        (ticket,) = plan["tickets"]
        lane_kinds = [(lane["processed"], lane["remainder_reusable"])
                      for lane in ticket["lanes"]]
        # Piece lands in one lane; the other lane is recovered whole (10mm >= 8).
        self.assertEqual(sorted(lane_kinds),
                         [(False, True), (True, True)])
        self.assertEqual(len(plan["added_entries"]), 2)

    def test_multi_generation_root_offsets_through_legacy_and_salvage_chain(self):
        self.init([stock_row("R1", 100, head=4, tail=0)])
        # Normal cut first: piece 30 + kerf 2 at local [4,36), legacy offcut
        # starts at 36, so its root offset is 36.
        first = self.normal_commit(batch("b1", [order("rail", 30)],
                                         kerf=2, min_offcut=5))
        legacy_id = first["added_entries"][0]["entry_id"]
        self.inspect("INS-1", legacy_id, [zone(10, 13)])
        plan = self.salvage_plan(salvage_request(
            "sv1", [legacy_id], [order("pin", 8)], kerf=1, min_offcut=4))
        (ticket,) = plan["tickets"]
        self.assertEqual(ticket["root_offset_mm"], 36)
        for row in ticket["partitions"]:
            self.assertEqual(row["root_start_mm"], 36 + row["start_mm"])
        self.salvage_commit(plan)
        # A salvage offcut then feeds another salvage: offsets stay root-based.
        state = read_state(self.ws)
        branch = next(entry_id for entry_id, entry in state["entries"].items()
                      if entry["status"] == "available")
        self.inspect("INS-2", branch, [zone(1, 2)])
        plan2 = self.salvage_plan(salvage_request(
            "sv2", [branch], [order("peg", 3)], kerf=1, min_offcut=2))
        (ticket2,) = plan2["tickets"]
        stored_offset = state["entries"][branch]["root_offset_mm"]
        self.assertEqual(ticket2["root_offset_mm"], stored_offset)
        for row in ticket2["partitions"]:
            self.assertEqual(row["root_start_mm"],
                             stored_offset + row["start_mm"])

    def test_multiple_offcuts_get_distinct_stable_identities(self):
        self.init([stock_row("R1", 60)])
        self.inspect("INS-1", "stock/R1", [zone(20, 22), zone(40, 42)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 4)], kerf=1, min_offcut=4))
        ids = [entry["entry_id"] for entry in plan["added_entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids[0] < ids[1] for _ in [0]))
        commit = self.salvage_commit(plan)
        state = read_state(self.ws)
        for entry in commit["added_entries"]:
            stored = state["entries"][entry["entry_id"]]
            self.assertEqual(stored["trim_head_mm"], 0)
            self.assertEqual(stored["trim_tail_mm"], 0)
            self.assertEqual(stored["length_mm"], entry["length_mm"])
            self.assertEqual(stored["source"]["type"], "salvage_offcut")
            self.assertEqual(stored["source"]["parent_entry_id"], "stock/R1")


class ZeroPieceAndMaterialTests(SalvageCase):
    def test_zero_piece_salvage_commits_without_consuming_stock(self):
        self.init([stock_row("R1", 30)])
        self.inspect("INS-1", "stock/R1", [zone(10, 25)])
        before = read_state(self.ws)
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 40)]))
        self.assertEqual(plan["tickets"], [])
        self.assertEqual(plan["consumed_entry_ids"], [])
        self.assertEqual(plan["added_entries"], [])
        commit = self.salvage_commit(plan)
        after = read_state(self.ws)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["finished_revision"],
                         before["finished_revision"] + 1)
        self.assertEqual(after["price_revision"], before["price_revision"])
        self.assertEqual(after["entries"]["stock/R1"]["status"], "available")
        self.assertEqual(workspace.status(str(self.ws), "sv1"), commit)

    def test_cross_material_demand_is_unfilled_not_substituted(self):
        self.init([stock_row("R1", 40, material="oak")])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5, 1, "pine")]))
        self.assertEqual(plan["tickets"], [])
        self.assertEqual(plan["unfilled"][0]["material"], "pine")
        self.assertEqual(plan["metrics"]["delivered_pieces"], 0)

    def test_all_candidates_covered_by_zones_is_a_valid_zero_result(self):
        self.init([stock_row("R1", 20)])
        self.inspect("INS-1", "stock/R1", [zone(0, 20)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 1)]))
        self.assertEqual(plan["candidates"][0]["lanes"], [])
        self.assertEqual(plan["metrics"]["delivered_pieces"], 0)


class CommitSemanticsTests(SalvageCase):
    def test_shared_job_id_conflicts_between_normal_and_salvage(self):
        self.init([stock_row("R1", 60), stock_row("R2", 60)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        self.salvage_commit(request=salvage_request(
            "shared", ["stock/R1"], [order("p", 5)]))
        with self.assertRaises(ConflictError):
            self.normal_commit(batch("shared", [order("q", 5)]))
        # And the other direction.
        self.normal_commit(batch("normal-job", [order("q", 5)]))
        with self.assertRaises(ConflictError):
            self.salvage_commit(request=salvage_request(
                "normal-job", ["stock/R2"], [order("r", 5)]))

    def test_replay_is_idempotent_and_conflicting_reuse_fails(self):
        self.init([stock_row("R1", 60), stock_row("R2", 60)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        first = self.salvage_commit(plan)
        replay = self.salvage_commit(plan)
        self.assertEqual(replay, first)
        state = read_state(self.ws)
        self.assertEqual(len([j for j in state["jobs"]]), 1)
        # Same job id, different request conflicts even after the first
        # candidate is consumed; use the still-available second root.
        other = self.salvage_plan(salvage_request(
            "sv1", ["stock/R2"], [order("p", 6)]))
        with self.assertRaises(ConflictError):
            self.salvage_commit(other)

    def test_tampered_foreign_and_stale_plans_are_rejected(self):
        self.init([stock_row("R1", 60), stock_row("R2", 40)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        before = read_state(self.ws)
        foreign = dict(plan, workspace_id="elsewhere")
        with self.assertRaisesRegex(ValueError, "different workspace"):
            self.salvage_commit(foreign)
        tampered = dict(plan, consumed_entry_ids=["stock/R2"])
        with self.assertRaises(ValueError):
            self.salvage_commit(tampered)
        broken = dict(plan, request_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "hash"):
            self.salvage_commit(broken)
        # A competing normal commit advances the revision and stales the plan.
        self.normal_commit(batch("other", [order("q", 5)]))
        with self.assertRaises(StalePlanError):
            self.salvage_commit(plan)
        self.assertEqual(read_state(self.ws)["entries"]["stock/R1"]["status"],
                         "available")
        self.assertNotEqual(before, read_state(self.ws))

    def test_concurrent_competing_commits_admit_exactly_one(self):
        self.init([stock_row("R1", 60), stock_row("R2", 60)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        plan_a = self.plan_path(self.salvage_plan(salvage_request(
            "job-a", ["stock/R1"], [order("p", 5)])))
        plan_b = self.plan_path(self.salvage_plan(salvage_request(
            "job-b", ["stock/R2"], [order("p", 5)])))
        results, errors = {}, {}

        def run(key, path):
            try:
                results[key] = workspace.salvage_commit(
                    str(self.ws), path, self.out(f"{key}.json"))
            except (OSError, ValueError) as exc:
                errors[key] = exc

        threads = [threading.Thread(target=run, args=("a", plan_a)),
                   threading.Thread(target=run, args=("b", plan_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), StalePlanError)

    def test_output_conflict_is_detected_before_state_changes(self):
        self.init([stock_row("R1", 60)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        Path(self.out("taken.json")).write_text('{"not": "this"}\n')
        before = read_state(self.ws)
        with self.assertRaises(FileExistsError):
            workspace.salvage_commit(str(self.ws), self.plan_path(plan),
                                    self.out("taken.json"))
        self.assertEqual(read_state(self.ws), before)

    def test_publish_crash_replays_without_double_effects(self):
        self.init([stock_row("R1", 60)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        output = self.out("sv.json")
        with mock.patch("sawmill.workspace.publish",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.salvage_commit(str(self.ws), self.plan_path(plan),
                                         output)
        self.assertFalse(Path(output).exists())
        replay = workspace.salvage_commit(str(self.ws), self.plan_path(plan),
                                          output)
        self.assertTrue(Path(output).exists())
        self.assertEqual(replay["revision"], 2)
        self.assertEqual(len(read_state(self.ws)["jobs"]), 1)


class SiblingBranchTests(SalvageCase):
    def test_sibling_offcuts_are_consumed_by_normal_and_salvage_separately(self):
        self.init([stock_row("R1", 60)])
        self.inspect("INS-1", "stock/R1", [zone(20, 22), zone(40, 42)])
        first = self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 4)], kerf=1, min_offcut=4))
        branch_ids = [entry["entry_id"] for entry in first["added_entries"]]
        self.assertEqual(len(branch_ids), 3)
        # One sibling takes a later inspection and is consumed by salvage...
        self.inspect("INS-2", branch_ids[1], [zone(1, 2)])
        salvage_two = self.salvage_commit(request=salvage_request(
            "sv2", [branch_ids[1]], [order("r", 3)], kerf=1, min_offcut=2))
        self.assertEqual(salvage_two["consumed_entry_ids"], [branch_ids[1]])
        # ...while another clean sibling is consumed by the normal command
        # (the deterministic pool rule chooses among the remaining clean ones).
        normal_two = self.normal_commit(batch("b2", [order("s", 4)]))
        normal_target, = normal_two["consumed_entry_ids"]
        self.assertIn(normal_target, (branch_ids[0], branch_ids[2]))
        clean_sibling = (branch_ids[2] if normal_target == branch_ids[0]
                         else branch_ids[0])
        # The untouched clean sibling is still available; every consumed
        # identity stays consumed and no branch is double counted.
        state = read_state(self.ws)
        self.assertEqual(state["entries"][clean_sibling]["status"], "available")
        for consumed in (branch_ids[1], normal_target):
            self.assertEqual(state["entries"][consumed]["status"], "consumed")
    def test_empty_zone_inspection_keeps_the_entry_normal_plan_eligible(self):
        self.init([stock_row("R1", 40)])
        self.inspect("INS-1", "stock/R1", [])
        status = workspace.status(str(self.ws))
        entry = next(e for e in status["entries"]
                     if e["entry_id"] == "stock/R1")
        self.assertFalse(entry["requires_salvage"])
        self.assertTrue(entry["eligible_for_normal_plan"])
        result = self.normal_commit(batch("b1", [order("p", 10)]))
        self.assertEqual(result["consumed_entry_ids"], ["stock/R1"])


class CrashRecoverySalvageTests(SalvageCase):
    def test_failed_state_write_leaves_no_partial_salvage(self):
        self.init([stock_row("R1", 60)])
        self.inspect("INS-1", "stock/R1", [zone(20, 22)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)]))
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace.os.replace",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.salvage_commit(plan)
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertEqual([p for p in self.ws.iterdir()
                          if p.name.startswith(".state-")], [])
        # Retry completes cleanly with the offcuts present exactly once.
        result = self.salvage_commit(plan)
        state = read_state(self.ws)
        self.assertEqual(state["entries"]["stock/R1"]["status"], "consumed")
        self.assertEqual(len(result["added_entries"]),
                         len(state["entries"]) - 1)

    def test_failed_inspection_state_write_changes_nothing(self):
        self.init([stock_row("R1", 60)])
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace.os.replace",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        report = self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        self.assertEqual(report["registered_revision"], 1)


class NumericUnitTests(SalvageCase):
    def test_units_compare_as_integers_not_strings(self):
        self.init([stock_row("R1", 200)])
        plan = self.salvage_plan(salvage_request(
            "sv1", ["stock/R1"],
            [order("p", 4, 12)], kerf=0, min_offcut=1))
        units = [cut["unit"] for ticket in plan["tickets"]
                 for cut in ticket["cuts"]]
        self.assertEqual(units, list(range(1, 13)))
        self.assertEqual(
            [row["start_mm"] for row in plan["placement_sequence"]],
            [4 * i for i in range(12)])


class FractionalCentTests(SalvageCase):
    def test_single_apportionment_across_all_lanes_with_remainders(self):
        # 7mm of partitions sharing 3 cents guarantees sub-1-cent remainders;
        # the whole parent is apportioned exactly once across every lane.
        self.init([stock_row("R1", 7)])
        self.inspect("INS-1", "stock/R1", [zone(2, 3)])
        self.register_price("R1", 3, 0)
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 1, 2)], kerf=1, min_offcut=2))
        detail = workspace.cost_query(str(self.ws))
        ticket = next(t for t in detail["tickets"] if t["job_id"] == "sv1")
        amounts = [segment["amount_cents"] for segment in ticket["segments"]]
        self.assertEqual(sum(amounts), 3)
        # Largest remainder: exact shares are 3*len/7, the two leftover cents
        # go to the largest-remainder, smallest-start segments.
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 3)
        # Every reusable offcut carries its own integer cents.
        offcut_segments = [s for s in ticket["segments"]
                           if s["kind"] == "offcut" and s["reusable"]]
        self.assertTrue(all(s["amount_cents"] >= 0 for s in offcut_segments))
        self.assertEqual(len(offcut_segments),
                         len({s["entry_id"] for s in offcut_segments}))

    def test_price_correction_flows_through_both_branches(self):
        self.init([stock_row("R1", 20)])
        self.inspect("INS-1", "stock/R1", [zone(6, 8)])
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 4)]))
        self.register_price("R1", 20, 0)
        first = workspace.cost_query(str(self.ws))
        self.register_price("R1", 40, 1)
        second = workspace.cost_query(str(self.ws))
        for old_row, new_row in zip(
                sorted(first["pieces"], key=lambda r: r["order_id"]),
                sorted(second["pieces"], key=lambda r: r["order_id"])):
            self.assertEqual(new_row["amount_cents"],
                             old_row["amount_cents"] * 2)
        self.assertEqual(second["summary"]["by_root"]["R1"]["total_cents"], 40)


class DeliveryAndTraceIntegrationTests(SalvageCase):
    def test_salvage_pieces_deliver_and_match_grain_by_root(self):
        self.init([stock_row("R1", 60), stock_row("R2", 60)])
        self.inspect("IA", "stock/R1", [zone(10, 12)])
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1", "stock/R2"],
            [order("chair", 20, 2), order("slat", 10, 1)],
            kerf=1, min_offcut=4))
        catalog = workspace._finished_catalog(read_state(self.ws))
        roots = {ref: facts["root_stock_id"] for ref, facts in catalog.items()}
        self.assertTrue(all(root in ("R1", "R2") for root in roots.values()))
        # A kit with two same-grain slots can take two same-root salvage pieces.
        request = {
            "dispatch_id": "DSP-1",
            "candidate_pieces": [
                {"job_id": ref[0], "entry_id": ref[1],
                 "order_id": ref[2], "unit": ref[3]} for ref in catalog],
            "kits": [{
                "kit_id": "K1", "priority": 0,
                "slots": [
                    {"slot_id": "a", "material": "oak", "min_mm": 10,
                     "max_mm": 20, "grain_group": "G"},
                    {"slot_id": "b", "material": "oak", "min_mm": 10,
                     "max_mm": 20, "grain_group": "G"}]}]}
        plan = workspace.delivery_plan(
            str(self.ws), self.source(request), self.out("dplan.json"))
        self.assertEqual(len(plan["selected_kits"]), 1)
        roots_used = {slot["piece"]["root_stock_id"]
                      for slot in plan["selected_kits"][0]["slots"]}
        self.assertEqual(len(roots_used), 1)
        workspace.delivery_commit(str(self.ws), self.out("dplan.json"),
                                  self.out("dcommit.json"))
        self.assertEqual(read_state(self.ws)["dispatches"]["DSP-1"][
            "result"]["selected_kits"][0]["kit_id"], "K1")

    def test_incident_trace_covers_every_salvage_branch(self):
        self.init([stock_row("R1", 60)])
        self.inspect("INS-1", "stock/R1", [zone(20, 22), zone(40, 42)])
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)], kerf=1, min_offcut=4))
        self.seq += 1
        report = workspace.register_incident(
            str(self.ws),
            self.source({"incident_id": "QC-1", "stock_ids": ["R1"]}),
            self.out("incident.json"))
        (root_report,) = report["roots"]
        entry_ids = {entry["entry_id"] for entry in root_report["entries"]}
        state = read_state(self.ws)
        expected = {entry_id for entry_id in state["entries"]
                    if state["entries"][entry_id]["root_stock_id"] == "R1"}
        self.assertEqual(entry_ids, expected)
        # Every salvage piece is traced.
        self.assertEqual({(p["order_id"], p["unit"])
                          for p in root_report["pieces"]}, {("p", 1)})
        # The active incident now isolates the surviving clean branches.
        status = workspace.status(str(self.ws))
        for entry in status["entries"]:
            if entry["status"] == "isolated":
                self.assertEqual(entry["active_incidents"], ["QC-1"])
        affected = workspace.delivery_affected(str(self.ws), "QC-1")
        self.assertEqual(affected["incident_id"], "QC-1")


class CloseBridgeWithBranchesTests(SalvageCase):
    def close(self, close_id, previous, inventory_rev, price_rev):
        self.seq += 1
        return workspace.close_run(
            str(self.ws),
            self.source({"close_id": close_id, "previous_close_id": previous,
                         "expected_inventory_revision": inventory_rev,
                         "expected_price_revision": price_rev}),
            self.out(f"close-{self.seq}.json"))

    def test_branch_creation_reprice_consume_one_branch_and_freeze(self):
        self.init([stock_row("R1", 40)])
        self.inspect("INS-1", "stock/R1", [zone(10, 12)])
        self.salvage_commit(request=salvage_request(
            "sv1", ["stock/R1"], [order("p", 5)], kerf=1, min_offcut=4))
        self.register_price("R1", 40, 0)
        first = self.close("C1", None, 2, 1)
        branch_ids = [row["entry_id"] for row in first["detail"]["inventory"]]
        self.assertEqual(len(branch_ids), 2)
        # Reprice, then consume exactly one branch with a normal batch. The
        # normal rule prefers the longer of the all-salvage-pool candidates.
        self.register_price("R1", 80, 1)
        state = read_state(self.ws)
        branch_lengths = {entry_id: state["entries"][entry_id]["length_mm"]
                          for entry_id in branch_ids}
        consumed_branch = max(branch_lengths, key=lambda key: branch_lengths[key])
        surviving_branch = min(branch_lengths, key=lambda key: branch_lengths[key])
        self.normal_commit(batch(
            "b2", [order("q", branch_lengths[consumed_branch] - 2)],
            kerf=1, min_offcut=100))
        state = read_state(self.ws)
        second = self.close("C2", "C1", state["revision"], state["price_revision"])
        # The first close stays frozen at the old price.
        self.assertEqual(workspace.close_status(str(self.ws), "C1"), first)
        root_lines = [line for line in second["bridge"]["lines"]
                      if line["root_stock_id"] == "R1"]
        for line in root_lines:
            self.assertEqual(line["opening_cents"] + line["price_change_cents"]
                             + line["production_cents"], line["closing_cents"])
        summary = second["bridge"]["summary"]["by_root"]["R1"]
        self.assertEqual(summary["price_change_cents"], 40)
        self.assertEqual(summary["production_cents"], 0)
        self.assertEqual(summary["opening_cents"], 40)
        self.assertEqual(summary["closing_cents"], 80)
        # The surviving branch is still inventory exactly once.
        surviving = [row for row in second["detail"]["inventory"]
                     if row["entry_id"] == surviving_branch]
        self.assertEqual(len(surviving), 1)
        # Conservation under the new price.
        detail = second["detail"]
        self.assertEqual(detail["summary"]["by_root"]["R1"]["total_cents"], 80)


class OldWorkspaceCompatibilityTests(SalvageCase):
    def test_legacy_state_upgrades_and_legacy_offcuts_keep_identity(self):
        self.init([stock_row("R1", 120, head=10, tail=10)])
        self.normal_commit(batch("b1", [order("chair", 40, 1)],
                                 kerf=3, min_offcut=20))
        state = read_state(self.ws)
        legacy_id = next(entry_id for entry_id in state["entries"]
                         if state["entries"][entry_id]["kind"] == "offcut")
        # Simulate a workspace written before inspections existed.
        for key in ("inspections", "inspection_log"):
            state.pop(key, None)
        (self.ws / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        listing = workspace.inspection_status(str(self.ws))
        self.assertEqual(listing["inspections"], [])
        # The legacy offcut can now be inspected and salvaged.
        self.inspect("INS-1", legacy_id, [zone(5, 7)])
        plan = self.salvage_plan(salvage_request(
            "sv1", [legacy_id], [order("p", 20)]))
        self.assertEqual(plan["tickets"][0]["entry_kind"], "offcut")
        self.salvage_commit(plan)


class SalvageCliTests(SalvageCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "sawmill.inventory_cli", *args],
                              capture_output=True, text=True, env=env)

    def test_end_to_end_cli_and_exit_codes(self):
        ws = self.out("ws")
        self.assertEqual(self.run_cli(
            "init", "--source",
            self.source({"stock": [stock_row("R1", 40)]}),
            "--workspace", ws).returncode, 0)
        inspect_src = self.source(inspection(
            "INS-1", "stock/R1", [zone(10, 12)]))
        inspect_out = self.out("ins.json")
        self.assertEqual(self.run_cli(
            "inspection", "register", "--workspace", ws,
            "--source", inspect_src, "--output", inspect_out).returncode, 0)
        # A normal plan must not consume the zoned root.
        normal_out = self.out("normal.json")
        self.assertEqual(self.run_cli(
            "plan", "--workspace", ws,
            "--source", self.source(batch("b1", [order("p", 5)])),
            "--output", normal_out).returncode, 0)
        self.assertEqual(json.loads(Path(normal_out).read_text())["tickets"], [])
        plan_out = self.out("splan.json")
        proc = self.run_cli(
            "salvage", "plan", "--workspace", ws,
            "--source", self.source(salvage_request(
                "sv1", ["stock/R1"], [order("p", 5)])),
            "--output", plan_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result_out = self.out("sresult.json")
        self.assertEqual(self.run_cli(
            "salvage", "commit", "--workspace", ws, "--plan", plan_out,
            "--output", result_out).returncode, 0)
        self.assertEqual(self.run_cli(
            "status", "--workspace", ws, "--job", "sv1").returncode, 0)
        self.assertEqual(self.run_cli(
            "inspection", "status", "--workspace", ws,
            "--entry", "stock/R1").returncode, 0)
        # Replay against the same output path succeeds.
        self.assertEqual(self.run_cli(
            "salvage", "commit", "--workspace", ws, "--plan", plan_out,
            "--output", result_out).returncode, 0)
        # A different content at the output path is rejected.
        proc = self.run_cli(
            "salvage", "commit", "--workspace", ws, "--plan", plan_out,
            "--output", inspect_out)
        self.assertEqual(proc.returncode, 2)
        # Unknown entry inspection exits 2.
        proc = self.run_cli(
            "inspection", "register", "--workspace", ws,
            "--source", self.source(inspection("X", "stock/NOPE", [])),
            "--output", self.out("bad.json"))
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
