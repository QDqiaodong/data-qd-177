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

from sawmill import delivery, workspace
from sawmill.workspace import ConflictError, StalePlanError

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def ref(job_id, entry_id, order_id, unit):
    return {"job_id": job_id, "entry_id": entry_id,
            "order_id": order_id, "unit": unit}


def ref_of(piece):
    return (piece["job_id"], piece["entry_id"], piece["order_id"], piece["unit"])


def piece(job_id, entry_id, order_id, unit, length_mm, root, material="oak"):
    return {**ref(job_id, entry_id, order_id, unit), "material": material,
            "length_mm": length_mm, "root_stock_id": root}


def slot(slot_id, min_mm, max_mm, material="oak", group="G"):
    return {"slot_id": slot_id, "material": material, "min_mm": min_mm,
            "max_mm": max_mm, "grain_group": group}


def kit(kit_id, priority, slots):
    return {"kit_id": kit_id, "priority": priority, "slots": slots}


def request(dispatch_id, candidates, kits):
    return {"dispatch_id": dispatch_id, "candidate_pieces": candidates,
            "kits": kits}


def order(order_id, piece_mm, quantity=1, material="oak"):
    return {"id": order_id, "material": material, "piece_mm": piece_mm,
            "quantity": quantity}


def stock_row(stock_id, length, material="oak", head=0, tail=0):
    return {"id": stock_id, "material": material, "length_mm": length,
            "trim_head_mm": head, "trim_tail_mm": tail}


def batch(job_id, orders, kerf=3, min_offcut=100):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "orders": orders}


def catalog_of(pieces):
    return {ref_of(p): p for p in pieces}


def plan_of(pieces, kits, dispatch_id="D", delivered_by=None, covered_roots=None):
    candidates = [ref(p["job_id"], p["entry_id"], p["order_id"], p["unit"])
                  for p in pieces]
    return delivery.build_plan(
        workspace_id="ws-test", finished_revision=0,
        catalog=catalog_of(pieces), delivered_by=delivered_by or {},
        covered_roots=covered_roots or {},
        data=request(dispatch_id, candidates, kits))


def brute_force(pieces, kits):
    """Independent exhaustive reference: every subset, every assignment.

    Shares no code with the solver under test. Returns the selected kit id
    set, the minimum total excess, and the winning reference sequence.
    """
    kits = sorted(kits, key=lambda k: (k["priority"], k["kit_id"]))
    results = []  # ((count, vector), (excess, sequence), subset indices)
    for size in range(len(kits) + 1):
        for combo in itertools.combinations(range(len(kits)), size):
            slots = [(kits[i]["kit_id"], s)
                     for i in combo
                     for s in sorted(kits[i]["slots"], key=lambda x: x["slot_id"])]
            assignments = []

            def rec(index, used, roots, assign, excess):
                if index == len(slots):
                    assignments.append((excess, tuple(ref_of(p) for p in assign)))
                    return
                kit_id, s = slots[index]
                group = (kit_id, s["grain_group"])
                for p in pieces:
                    if p["material"] != s["material"]:
                        continue
                    if not s["min_mm"] <= p["length_mm"] <= s["max_mm"]:
                        continue
                    key = ref_of(p)
                    if key in used:
                        continue
                    if group in roots and roots[group] != p["root_stock_id"]:
                        continue
                    added = group not in roots
                    if added:
                        roots[group] = p["root_stock_id"]
                    used.add(key)
                    rec(index + 1, used, roots, assign + [p],
                        excess + p["length_mm"] - s["min_mm"])
                    used.discard(key)
                    if added:
                        del roots[group]

            rec(0, set(), {}, [], 0)
            if not assignments:
                continue
            best_assign = min(assignments)
            vector = tuple(1 if i in combo else 0 for i in range(len(kits)))
            results.append(((size, vector), best_assign, combo))
    assert results, "the empty subset is always feasible"
    best_selection = max(entry[0] for entry in results)
    (winner,) = [entry for entry in results if entry[0] == best_selection]
    _, (excess, sequence), combo = winner
    return frozenset(kits[i]["kit_id"] for i in combo), excess, sequence


def check_plan_structure(case, plan, pieces, kits):
    """Structural invariants of any plan, checked independently of the solver."""
    catalog = catalog_of(pieces)
    used = []
    for selected in plan["selected_kits"]:
        groups = {}
        for s in selected["slots"]:
            p = s["piece"]
            facts = catalog[ref_of(p)]
            # Piece facts come from the committed catalog, not the request.
            case.assertEqual(p["material"], facts["material"])
            case.assertEqual(p["length_mm"], facts["length_mm"])
            case.assertEqual(p["root_stock_id"], facts["root_stock_id"])
            case.assertEqual(p["material"], s["material"])
            case.assertGreaterEqual(p["length_mm"], s["min_mm"])
            case.assertLessEqual(p["length_mm"], s["max_mm"])
            used.append(ref_of(p))
            groups.setdefault(s["grain_group"], set()).add(p["root_stock_id"])
        for group, roots in groups.items():
            case.assertEqual(len(roots), 1,
                             f"grain group {group} spans multiple roots")
    case.assertEqual(len(used), len(set(used)), "piece used twice")
    selected_ids = {k["kit_id"] for k in plan["selected_kits"]}
    case.assertEqual(sorted(plan["unselected_kit_ids"]),
                     sorted(k["kit_id"] for k in kits
                            if k["kit_id"] not in selected_ids))
    metrics = plan["metrics"]
    case.assertEqual(metrics["kits_total"], len(kits))
    case.assertEqual(metrics["kits_selected"], len(plan["selected_kits"]))
    case.assertEqual(metrics["selection_vector"],
                     [1 if k["kit_id"] in selected_ids else 0
                      for k in sorted(kits, key=lambda k: (k["priority"], k["kit_id"]))])
    case.assertEqual(metrics["kit_order"],
                     [k["kit_id"] for k in
                      sorted(kits, key=lambda k: (k["priority"], k["kit_id"]))])
    excess = sum(s["piece"]["length_mm"] - s["min_mm"]
                 for k in plan["selected_kits"] for s in k["slots"])
    case.assertEqual(metrics["excess_mm_total"], excess)
    sequence = [ref(p["job_id"], p["entry_id"], p["order_id"], p["unit"])
                for k in plan["selected_kits"] for s in k["slots"]
                for p in [s["piece"]]]
    case.assertEqual(metrics["piece_sequence"], sequence)
    # Remaining + used + excluded partition the candidate list.
    remaining = {ref_of(p) for p in plan["remaining_pieces"]}
    excluded = {ref_of(p["piece"]) for p in plan["excluded_pieces"]}
    case.assertFalse(remaining & set(used))
    case.assertFalse(remaining & excluded)
    case.assertFalse(set(used) & excluded)


def check_against_brute_force(case, plan, pieces, kits):
    want_ids, want_excess, want_sequence = brute_force(pieces, kits)
    case.assertEqual({k["kit_id"] for k in plan["selected_kits"]}, want_ids)
    case.assertEqual(plan["metrics"]["excess_mm_total"], want_excess)
    case.assertEqual(tuple(ref_of(p) for p in plan["metrics"]["piece_sequence"]),
                     want_sequence)


class SolverTests(unittest.TestCase):
    """Pure-solver behaviour over synthetic catalogs."""

    def test_two_smaller_kits_beat_one_priority_kit(self):
        # The spec's first non-greedy example: K0 wants both pieces, K1 and
        # K2 want one each; delivering K1+K2 (two kits) beats K0 alone.
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                  piece("j", "stock/A", "o2", 1, 200, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 100), slot("b", 200, 200)]),
                kit("K1", 1, [slot("a", 100, 100)]),
                kit("K2", 2, [slot("a", 200, 200)])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual({k["kit_id"] for k in plan["selected_kits"]},
                         {"K1", "K2"})
        self.assertEqual(plan["unselected_kit_ids"], ["K0"])
        self.assertEqual(plan["metrics"]["selection_vector"], [0, 1, 1])

    def test_feasible_assignment_is_not_greedy(self):
        # The spec's second example: slot a accepts 100..200, slot b only
        # 100; the 200 piece must go to a and the 100 piece to b.
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                  piece("j", "stock/A", "o2", 1, 200, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 200), slot("b", 100, 100)])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        (selected,) = plan["selected_kits"]
        by_slot = {s["slot_id"]: s["piece"] for s in selected["slots"]}
        self.assertEqual(by_slot["a"]["length_mm"], 200)
        self.assertEqual(by_slot["b"]["length_mm"], 100)

    def test_same_grain_group_requires_same_root(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                  piece("j", "stock/B", "o2", 1, 100, "B")]
        kits = [kit("K0", 0, [slot("a", 100, 100, group="G"),
                              slot("b", 100, 100, group="G")])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(plan["selected_kits"], [])  # cross-root is not allowed
        # Distinct groups in one kit may span roots.
        kits = [kit("K0", 0, [slot("a", 100, 100, group="G1"),
                              slot("b", 100, 100, group="G2")])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(len(plan["selected_kits"]), 1)
        # A second piece from root A makes the same-group kit feasible.
        pieces.append(piece("j", "stock/A", "o3", 1, 100, "A"))
        kits = [kit("K0", 0, [slot("a", 100, 100, group="G"),
                              slot("b", 100, 100, group="G")])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        (selected,) = plan["selected_kits"]
        self.assertEqual({s["piece"]["root_stock_id"] for s in selected["slots"]},
                         {"A"})

    def test_grain_group_names_are_unrelated_across_kits(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                  piece("j", "stock/B", "o2", 1, 100, "B")]
        kits = [kit("K0", 0, [slot("a", 100, 100, group="G")]),
                kit("K1", 1, [slot("a", 100, 100, group="G")])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(len(plan["selected_kits"]), 2)
        roots = [s["piece"]["root_stock_id"]
                 for k in plan["selected_kits"] for s in k["slots"]]
        self.assertEqual(sorted(roots), ["A", "B"])

    def test_length_window_boundaries_are_closed(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                      piece("j", "stock/A", "o2", 1, 200, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 100), slot("b", 200, 200)])]
        plan = plan_of(pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(len(plan["selected_kits"]), 1)
        # One millimetre outside the window on either side fails the kit.
        kits = [kit("K0", 0, [slot("a", 101, 199)])]
        plan = plan_of(pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(plan["selected_kits"], [])
        kits = [kit("K0", 0, [slot("a", 100, 199), slot("b", 201, 300)])]
        plan = plan_of(pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(plan["selected_kits"], [])

    def test_materials_are_never_substituted(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A", "oak"),
                  piece("j", "stock/B", "o2", 1, 100, "B", "pine"),
                  piece("j", "stock/C", "o3", 1, 100, "C", "walnut")]
        kits = [kit("K0", 0, [slot("a", 100, 100, "oak", "G1"),
                              slot("b", 100, 100, "pine", "G2")]),
                kit("K1", 1, [slot("a", 100, 100, "walnut")]),
                kit("K2", 2, [slot("a", 100, 100, "cedar")])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual({k["kit_id"] for k in plan["selected_kits"]},
                         {"K0", "K1"})
        self.assertEqual(plan["unselected_kit_ids"], ["K2"])

    def test_excess_is_minimized_before_references(self):
        # A 150mm piece fits the 100..200 window with less excess than 200mm.
        pieces = [piece("j", "stock/A", "o1", 1, 150, "A"),
                  piece("j", "stock/A", "o2", 1, 200, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 200)])]
        plan = plan_of(pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        (selected,) = plan["selected_kits"]
        self.assertEqual(selected["slots"][0]["piece"]["length_mm"], 150)
        self.assertEqual(plan["metrics"]["excess_mm_total"], 50)

    def test_reference_sequence_breaks_remaining_ties_with_integer_units(self):
        # Identical lengths and roots: only the reference differs. Integer
        # comparison makes unit 2 lexicographically smaller than unit 10;
        # stringified comparison would pick unit 10 ("10" < "2").
        pieces = [piece("j", "stock/A", "o", 10, 100, "A"),
                  piece("j", "stock/A", "o", 2, 100, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 100)])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        (selected,) = plan["selected_kits"]
        self.assertEqual(selected["slots"][0]["piece"]["unit"], 2)
        self.assertEqual(plan["metrics"]["piece_sequence"],
                         [ref("j", "stock/A", "o", 2)])

    def test_tie_breaks_walk_all_four_levels(self):
        # Two assignments tie on count, vector, and excess; the reference
        # sequence decides between them.
        pieces = [piece("j", "stock/A", "o1", 1, 150, "A"),
                  piece("j", "stock/A", "o2", 1, 200, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 200), slot("b", 150, 200)])]
        # a=150,b=200 and a=200,b=150 both give excess 100; the sequence
        # (o1,o2) beats (o2,o1), so a gets the 150mm piece o1.
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        (selected,) = plan["selected_kits"]
        by_slot = {s["slot_id"]: s["piece"] for s in selected["slots"]}
        self.assertEqual(by_slot["a"]["order_id"], "o1")
        self.assertEqual(by_slot["b"]["order_id"], "o2")

    def test_selection_vector_prefers_earlier_kits(self):
        # One piece, two kits that each need it: the earlier kit in
        # (priority, kit_id) order wins the vector tie-break.
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 100)]),
                kit("K1", 1, [slot("a", 100, 100)])]
        plan = plan_of(pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual([k["kit_id"] for k in plan["selected_kits"]], ["K0"])
        self.assertEqual(plan["metrics"]["selection_vector"], [1, 0])
        # Same priority: kit_id order decides.
        kits = [kit("KB", 0, [slot("a", 100, 100)]),
                kit("KA", 0, [slot("a", 100, 100)])]
        plan = plan_of(pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual([k["kit_id"] for k in plan["selected_kits"]], ["KA"])

    def test_array_reordering_does_not_change_the_plan(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                  piece("j", "stock/A", "o2", 1, 150, "A"),
                      piece("j", "stock/B", "o3", 1, 200, "B"),
                      piece("j", "stock/B", "o4", 1, 250, "B")]
        kits = [kit("K0", 0, [slot("a", 100, 200, group="G1"),
                              slot("b", 100, 300, group="G2")]),
                kit("K1", 1, [slot("x", 100, 150), slot("y", 200, 300)]),
                kit("K2", 2, [slot("z", 90, 260)])]
        baseline = plan_of(pieces, kits)
        rng = random.Random(7)
        for _ in range(20):
            shuffled_kits = [kit(k["kit_id"], k["priority"],
                                 random.sample(k["slots"], len(k["slots"])))
                             for k in kits]
            rng.shuffle(shuffled_kits)
            shuffled_pieces = random.sample(pieces, len(pieces))
            plan = plan_of(shuffled_pieces, shuffled_kits)
            self.assertEqual(plan, baseline)
            self.assertEqual(plan["request"], baseline["request"])

    def test_excluded_pieces_are_listed_and_never_allocated(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A"),
                  piece("j", "stock/B", "o2", 1, 100, "B"),
                  piece("j", "stock/C", "o3", 1, 100, "C")]
        delivered_by = {ref_of(pieces[0]): "DSP-OLD"}
        covered_roots = {"B": ["INC-1", "INC-2"]}
        kits = [kit("K0", 0, [slot("a", 100, 100)])]
        plan = plan_of(pieces, kits, delivered_by=delivered_by,
                       covered_roots=covered_roots)
        check_plan_structure(self, plan, pieces, kits)
        excluded = {ref_of(e["piece"]): e for e in plan["excluded_pieces"]}
        self.assertEqual(set(excluded), {ref_of(pieces[0]), ref_of(pieces[1])})
        self.assertEqual(excluded[ref_of(pieces[0])],
                         {"piece": ref("j", "stock/A", "o1", 1),
                          "reasons": ["delivered"], "delivered_by": "DSP-OLD",
                          "active_incidents": []})
        self.assertEqual(excluded[ref_of(pieces[1])],
                         {"piece": ref("j", "stock/B", "o2", 1),
                          "reasons": ["isolated"], "delivered_by": None,
                          "active_incidents": ["INC-1", "INC-2"]})
        # Only the C-rooted piece remains eligible and is allocated.
        (selected,) = plan["selected_kits"]
        self.assertEqual(selected["slots"][0]["piece"]["root_stock_id"], "C")
        # A piece that is both delivered and isolated reports both reasons.
        delivered_by[ref_of(pieces[1])] = "DSP-OTHER"
        plan = plan_of(pieces, kits, delivered_by=delivered_by,
                       covered_roots=covered_roots)
        both = {ref_of(e["piece"]): e for e in plan["excluded_pieces"]}
        self.assertEqual(both[ref_of(pieces[1])]["reasons"],
                         ["delivered", "isolated"])
        self.assertEqual(both[ref_of(pieces[1])]["delivered_by"], "DSP-OTHER")
        self.assertEqual(both[ref_of(pieces[1])]["active_incidents"],
                         ["INC-1", "INC-2"])

    def test_zero_kits_is_a_valid_optimum(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A")]
        kits = [kit("K0", 0, [slot("a", 500, 600)]),
                kit("K1", 1, [slot("a", 100, 100, "pine")])]
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        check_against_brute_force(self, plan, pieces, kits)
        self.assertEqual(plan["selected_kits"], [])
        self.assertEqual(plan["unselected_kit_ids"], ["K0", "K1"])
        self.assertEqual(plan["metrics"]["kits_selected"], 0)
        self.assertEqual(plan["metrics"]["selection_vector"], [0, 0])
        self.assertEqual(plan["metrics"]["excess_mm_total"], 0)
        self.assertEqual(plan["metrics"]["piece_sequence"], [])
        self.assertEqual([ref_of(p) for p in plan["remaining_pieces"]],
                         [ref_of(pieces[0])])

    def test_unknown_and_duplicate_candidates_fail(self):
        pieces = [piece("j", "stock/A", "o1", 1, 100, "A")]
        kits = [kit("K0", 0, [slot("a", 100, 100)])]
        candidates = [ref("j", "stock/A", "o1", 1), ref("j", "stock/A", "o9", 1)]
        with self.assertRaisesRegex(ValueError, "unknown candidate"):
            delivery.build_plan(workspace_id="w", finished_revision=0,
                                catalog=catalog_of(pieces), delivered_by={},
                                covered_roots={},
                                data=request("D", candidates, kits))
        with self.assertRaisesRegex(ValueError, "duplicate candidate"):
            delivery.build_plan(workspace_id="w", finished_revision=0,
                                catalog=catalog_of(pieces), delivered_by={},
                                covered_roots={},
                                data=request("D", [candidates[0], candidates[0]],
                                             kits))


class FuzzTests(unittest.TestCase):
    """Small random instances checked against the exhaustive reference."""

    def test_random_instances_match_brute_force(self):
        rng = random.Random(20261120)
        entries = {"stock/A": "A", "stock/B": "B",
                   "offcut/j1/stock/A": "A",
                   "offcut/j2/offcut/j1/stock/A": "A"}
        for trial in range(150):
            pieces = []
            used = set()
            for _ in range(rng.randint(3, 6)):
                entry = rng.choice(sorted(entries))
                order_id = rng.choice(["o1", "o2"])
                unit = rng.randint(1, 2)
                if ("j", entry, order_id, unit) in used:
                    continue
                used.add(("j", entry, order_id, unit))
                pieces.append(piece("j", entry, order_id, unit,
                                    rng.choice([100, 150, 200]),
                                    entries[entry],
                                    rng.choice(["oak", "pine"])))
            kits = []
            for i in range(rng.randint(1, 3)):
                slots = []
                for j in range(rng.randint(1, 2)):
                    lo = rng.choice([100, 150])
                    hi = rng.choice([lo, 200])
                    slots.append(slot(f"s{j}", lo, hi,
                                      rng.choice(["oak", "pine"]),
                                      rng.choice(["g1", "g2"])))
                kits.append(kit(f"K{i}", rng.randint(0, 2), slots))
            plan = plan_of(pieces, kits, dispatch_id=f"F{trial}")
            check_plan_structure(self, plan, pieces, kits)
            check_against_brute_force(self, plan, pieces, kits)


class ValidationTests(unittest.TestCase):
    """Request shape and bound violations all fail before any solving."""

    def assert_invalid(self, data):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            delivery.build_plan(workspace_id="w", finished_revision=0,
                                catalog={}, delivered_by={}, covered_roots={},
                                data=data)

    def valid(self):
        return request("D", [ref("j", "stock/A", "o", 1)],
                       [kit("K", 0, [slot("a", 1, 100)])])

    def test_field_errors_and_bool_impostors(self):
        bad = []
        good = self.valid()
        good["dispatch_id"] = ""
        bad.append(good)
        bad.append(request("D", [], [kit("K", 0, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)] * 1, []))
        bad.append(request("D", "not-a-list", [kit("K", 0, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)], "not-a-list"))
        bad.append(request("D", [{"job_id": "j", "entry_id": "e",
                                  "order_id": "o", "unit": True}],
                           [kit("K", 0, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 0)],
                           [kit("K", 0, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", "1")],
                           [kit("K", 0, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "", 1)],
                           [kit("K", 0, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", True, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", -1, [slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [slot("a", 0, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [slot("a", 5, 4)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [slot("a", 1, True)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [slot("a", 1, 2, group=" ")])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [slot("a", 1, 2), slot("a", 1, 2)])]))
        bad.append(request("D", [ref("j", "e", "o", 1)],
                           [kit("K", 0, [slot("a", 1, 2)]),
                            kit("K", 1, [slot("b", 1, 2)])]))
        for data in bad:
            self.assert_invalid(data)

    def test_size_bounds(self):
        self.assert_invalid(request(
            "D", [ref("j", "e", "o", i) for i in range(1, 34)],
            [kit("K", 0, [slot("a", 1, 2)])]))
        self.assert_invalid(request(
            "D", [ref("j", "e", "o", 1)],
            [kit(f"K{i}", i, [slot("a", 1, 2)]) for i in range(9)]))
        self.assert_invalid(request(
            "D", [ref("j", "e", "o", 1)],
            [kit("K", 0, [slot(f"s{i}", 1, 2) for i in range(5)])]))
        self.assert_invalid(request(  # 7 kits x 4 slots = 28 > 24 total
            "D", [ref("j", "e", "o", 1)],
            [kit(f"K{i}", i, [slot(f"s{j}", 1, 2) for j in range(4)])
             for i in range(7)]))
        # Boundary values are accepted by validation and the plan builds.
        pieces = [piece("j", "e", "o", i, 100, "A") for i in range(1, 33)]
        kits = [kit(f"K{i}", i, [slot(f"s{j}", 1, 100) for j in range(3)])
                for i in range(8)]  # 8 kits x 3 slots = 24 total
        plan = plan_of(pieces, kits)
        check_plan_structure(self, plan, pieces, kits)
        self.assertEqual(plan["metrics"]["candidate_count"], 32)
        self.assertEqual(plan["metrics"]["kits_total"], 8)


class DeliveryWorkspaceCase(unittest.TestCase):
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

    def read_state(self):
        return json.loads((self.ws / "state.json").read_text())

    def init(self, stock):
        return workspace.init_workspace(str(self.ws), self.source(stock))

    def commit_batch(self, request):
        self.seq += 1
        plan_path = self.out(f"bplan-{self.seq}.json")
        workspace.plan(str(self.ws), self.source(request), plan_path)
        return workspace.commit(str(self.ws), plan_path,
                                self.out(f"bresult-{self.seq}.json"))

    def plan_delivery(self, value, name=None):
        self.seq += 1
        return workspace.delivery_plan(str(self.ws), self.source(value),
                                       self.out(name or f"dplan-{self.seq}.json"))

    def commit_delivery(self, plan_value, name=None):
        self.seq += 1
        plan_path = plan_value if isinstance(plan_value, str) \
            else self.source(plan_value)
        return workspace.delivery_commit(str(self.ws), plan_path,
                                         self.out(name or f"dresult-{self.seq}.json"))

    def register(self, incident_id, roots):
        return workspace.register_incident(
            str(self.ws), self.source({"incident_id": incident_id,
                                       "stock_ids": roots}),
            self.out(f"incident-{incident_id}.json"))

    def release(self, incident_id):
        return workspace.release_incident(str(self.ws), incident_id,
                                          self.out(f"release-{incident_id}.json"))

    def register_price(self, price_id, stock_id, amount, expected):
        return workspace.register_price(
            str(self.ws),
            self.source({"price_id": price_id, "stock_id": stock_id,
                         "amount_cents": amount,
                         "expected_price_revision": expected}),
            self.out(f"price-{price_id}.json"))

    def close(self, close_id, previous, inventory_revision, price_revision):
        return workspace.close_run(
            str(self.ws),
            self.source({"close_id": close_id, "previous_close_id": previous,
                         "expected_inventory_revision": inventory_revision,
                         "expected_price_revision": price_revision}),
            self.out(f"close-{close_id}.json"))

    def init_two_stock_workspace(self):
        """S1/S2 oak: batch-1 cuts chair x2 from S1, shelf from S2."""
        self.init({"stock": [stock_row("S1", 1000), stock_row("S2", 250)]})
        first = self.commit_batch(batch("b1", [order("chair", 400, 2),
                                               order("shelf", 200)]))
        self.assertEqual(first["consumed_entry_ids"], ["stock/S1", "stock/S2"])
        return first


class DispatchLedger:
    """Independent occupancy audit replayed from committed delivery results."""

    def __init__(self):
        self.owners = {}

    def check(self, result, state):
        assert state["finished_revision"] == result["finished_revision"]
        assert result["base_finished_revision"] == result["finished_revision"] - 1
        for kit_row in result["selected_kits"]:
            for slot_row in kit_row["slots"]:
                key = ref_of(slot_row["piece"])
                assert key not in self.owners, f"piece dispatched twice: {key}"
                self.owners[key] = result["dispatch_id"]
        # Every piece this result excludes as delivered is already owned.
        for excluded in result["excluded_pieces"]:
            if excluded["delivered_by"] is not None:
                assert self.owners.get(ref_of(excluded["piece"])) == \
                    excluded["delivered_by"]


class FinishedRevisionTests(DeliveryWorkspaceCase):
    def test_revision_progression_across_all_operations(self):
        self.init({"stock": [stock_row("S1", 1000), stock_row("S2", 250)]})
        self.assertEqual(self.read_state()["finished_revision"], 0)
        self.register_price("P1", "S1", 1000, 0)
        self.assertEqual(self.read_state()["finished_revision"], 0)
        self.commit_batch(batch("b1", [order("chair", 400)]))
        state = self.read_state()
        self.assertEqual(state["finished_revision"], 1)
        self.assertEqual(state["revision"], 1)
        # A delivery commit advances only the finished revision.
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        self.assertEqual(plan["base_finished_revision"], 1)
        result = self.commit_delivery(plan)
        state = self.read_state()
        self.assertEqual(result["finished_revision"], 2)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["price_revision"], 1)
        # Incident registration and release each advance it once (and the
        # inventory revision moves too, since S2 is still available stock).
        self.register("INC-1", ["S2"])
        self.assertEqual(self.read_state()["finished_revision"], 3)
        self.release("INC-1")
        self.assertEqual(self.read_state()["finished_revision"], 4)
        # Price corrections and closes never advance it.
        self.register_price("P2", "S2", 500, 1)
        self.register_price("P1C", "S1", 900, 2)
        self.assertEqual(self.read_state()["finished_revision"], 4)
        self.close("C1", None, 3, 3)
        state = self.read_state()
        self.assertEqual(state["finished_revision"], 4)
        self.assertEqual(state["price_revision"], 3)
        # Idempotent replays of any of these operations do not advance it.
        self.register("INC-2", ["S2"])
        before = self.read_state()
        self.register("INC-2", ["S2"])
        self.release("INC-2")
        after_release = self.read_state()
        self.assertEqual(after_release["finished_revision"],
                         before["finished_revision"] + 1)
        self.release("INC-2")
        self.assertEqual(self.read_state(), after_release)

    def test_zero_kit_commit_still_registers_and_advances(self):
        self.init_two_stock_workspace()
        ledger = DispatchLedger()
        plan = self.plan_delivery(request(
            "D0", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 900, 950)])]))  # no piece fits
        self.assertEqual(plan["selected_kits"], [])
        self.assertEqual(plan["metrics"]["kits_selected"], 0)
        before = self.read_state()
        result = self.commit_delivery(plan)
        after = self.read_state()
        self.assertEqual(result["selected_kits"], [])
        self.assertEqual(result["finished_revision"],
                         before["finished_revision"] + 1)
        ledger.check(result, after)
        # The dispatch is registered and queryable like any other.
        detail = workspace.delivery_status(str(self.ws), "D0")
        self.assertEqual(detail["result"], result)
        self.assertEqual(detail["recall"]["kits"], [])
        # Replay returns the frozen result without advancing again.
        replay = self.commit_delivery(plan)
        self.assertEqual(replay, result)
        self.assertEqual(self.read_state(), after)


class DeliveryFlowTests(DeliveryWorkspaceCase):
    def test_commit_occupies_pieces_and_blocks_every_later_use(self):
        self.init_two_stock_workspace()
        ledger = DispatchLedger()
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1),
                   ref("b1", "stock/S1", "chair", 2),
                   ref("b1", "stock/S2", "shelf", 1)],
            [kit("K", 0, [slot("a", 400, 400, group="G"),
                          slot("b", 400, 400, group="G")])]))
        check_plan_structure(self, plan, self_pieces(self, plan), plan["request"]["kits"])
        result = self.commit_delivery(plan)
        ledger.check(result, self.read_state())
        # A later preview sees the delivered pieces as excluded, not eligible.
        second = self.plan_delivery(request(
            "D2", [ref("b1", "stock/S1", "chair", 1),
                   ref("b1", "stock/S1", "chair", 2),
                   ref("b1", "stock/S2", "shelf", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        excluded = {ref_of(e["piece"]): e for e in second["excluded_pieces"]}
        self.assertEqual(set(excluded),
                         {("b1", "stock/S1", "chair", 1),
                          ("b1", "stock/S1", "chair", 2)})
        for entry in excluded.values():
            self.assertEqual(entry["reasons"], ["delivered"])
            self.assertEqual(entry["delivered_by"], "D1")
        self.assertEqual([ref_of(p) for p in second["remaining_pieces"]],
                         [("b1", "stock/S2", "shelf", 1)])
        # Committing it leaves the delivered pieces with D1 forever.
        result2 = self.commit_delivery(second)
        ledger.check(result2, self.read_state())
        self.assertEqual(result2["selected_kits"], [])
        # Releasing incidents can never free delivered pieces either.
        self.register("INC-X", ["S1"])
        self.release("INC-X")
        status = workspace.delivery_status(str(self.ws))
        by_ref = {ref_of(p): p for p in status["pieces"]}
        self.assertEqual(by_ref[("b1", "stock/S1", "chair", 1)]["status"],
                         "delivered")
        self.assertEqual(by_ref[("b1", "stock/S1", "chair", 1)]["delivered_by"],
                         "D1")

    def test_same_name_pieces_on_other_jobs_or_entries_are_distinct(self):
        self.init({"stock": [stock_row("S1", 500), stock_row("S2", 500)]})
        self.commit_batch(batch("b1", [order("chair", 400)]))
        self.commit_batch(batch("b2", [order("chair", 400)]))
        # Both jobs cut "chair" unit 1; the references stay distinct.
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1),
                   ref("b2", "stock/S2", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400, group="G"),
                          slot("b", 400, 400, group="G")])]))
        self.assertEqual(plan["selected_kits"], [])  # different roots
        # A reference naming the wrong entry for a real piece is unknown.
        with self.assertRaisesRegex(ValueError, "unknown candidate"):
            self.plan_delivery(request(
                "D2", [ref("b1", "stock/S2", "chair", 1)],
                [kit("K", 0, [slot("a", 400, 400)])]))

    def test_uncommitted_unfilled_offcut_and_waste_refs_are_not_pieces(self):
        self.init({"stock": [stock_row("S1", 1000)]})
        self.commit_batch(batch("b1", [order("chair", 400), order("huge", 5000)]))
        kits = [kit("K", 0, [slot("a", 1, 10000)])]
        # A piece from a plan that was never committed.
        workspace.plan(str(self.ws), self.source(batch("b2", [order("x", 100)])),
                       self.out("uncommitted.json"))
        for bad_ref in [ref("b2", "stock/S1", "x", 1),          # uncommitted job
                        ref("b1", "stock/S1", "huge", 1),       # unfilled order
                        ref("b1", "offcut/b1/stock/S1", "chair", 1),  # offcut entry
                        ref("b1", "stock/S1", "chair", 2)]:     # never cut
            with self.assertRaisesRegex(ValueError, "unknown candidate"):
                self.plan_delivery(request("D", [bad_ref], kits))

    def test_multi_generation_offcut_pieces_count_by_root(self):
        self.init({"stock": [stock_row("S1", 1200)]})
        self.commit_batch(batch("b1", [order("chair", 400, 2)]))
        self.commit_batch(batch("b2", [order("rail", 250)]))
        self.commit_batch(batch("b3", [order("slat", 100)]))
        # Pieces from the stock, its offcut, and the offcut's offcut all
        # share root S1 and satisfy one grain group.
        candidates = [ref("b1", "stock/S1", "chair", 1),
                      ref("b2", "offcut/b1/stock/S1", "rail", 1),
                      ref("b3", "offcut/b2/offcut/b1/stock/S1", "slat", 1)]
        plan = self.plan_delivery(request(
            "D1", candidates,
            [kit("K", 0, [slot("a", 400, 400, group="G"),
                          slot("b", 250, 250, group="G"),
                          slot("c", 100, 100, group="G")])]))
        check_plan_structure(self, plan, self_pieces(self, plan),
                             plan["request"]["kits"])
        (selected,) = plan["selected_kits"]
        self.assertEqual({s["piece"]["root_stock_id"] for s in selected["slots"]},
                         {"S1"})
        generations = {s["piece"]["entry_id"] for s in selected["slots"]}
        self.assertEqual(generations, {"stock/S1", "offcut/b1/stock/S1",
                                       "offcut/b2/offcut/b1/stock/S1"})

    def test_delivery_does_not_stale_cutting_plans_or_cost(self):
        self.init({"stock": [stock_row("S1", 1000), stock_row("S2", 250)]})
        self.register_price("P1", "S1", 1000, 0)
        self.register_price("P2", "S2", 500, 1)
        self.commit_batch(batch("b1", [order("chair", 400)]))
        cutting_plan = workspace.plan(str(self.ws),
                                      self.source(batch("b2", [order("shelf", 200)])),
                                      self.out("cplan.json"))
        cost_before = workspace.cost_query(str(self.ws))
        delivery_plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        result = self.commit_delivery(delivery_plan)
        self.assertEqual(self.read_state()["revision"], 1)  # unchanged
        self.assertEqual(workspace.cost_query(str(self.ws)), cost_before)
        # The cutting plan made before the delivery still commits fine.
        committed = workspace.commit(str(self.ws), self.out("cplan.json"),
                                     self.out("cresult.json"))
        self.assertEqual(committed["revision"], 2)
        self.assertEqual(committed["base_revision"],
                         cutting_plan["base_revision"])
        # Cost conservation still holds with the delivered piece accounted
        # exactly as before: delivery never touches cost detail.
        self.assertEqual(workspace.cost_query(str(self.ws))["summary"]["total"]
                         ["total_cents"], 1500)
        self.assertEqual(result["finished_revision"], 2)

    def test_cutting_commit_stales_delivery_preview(self):
        self.init_two_stock_workspace()
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S2", "shelf", 1)],
            [kit("K", 0, [slot("a", 200, 200)])]))
        self.commit_batch(batch("b2", [order("slat", 100)]))
        with self.assertRaises(StalePlanError):
            self.commit_delivery(plan)
        replanned = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S2", "shelf", 1)],
            [kit("K", 0, [slot("a", 200, 200)])]))
        self.commit_delivery(replanned)


def self_pieces(case, plan):
    """Reconstruct the catalog piece facts a plan was built over."""
    pieces = []
    for section in ("remaining_pieces",):
        pieces.extend(plan[section])
    for kit_row in plan["selected_kits"]:
        pieces.extend(s["piece"] for s in kit_row["slots"])
    return pieces


class IncidentInteractionTests(DeliveryWorkspaceCase):
    def test_incident_on_fully_consumed_root_still_invalidates_preview(self):
        # S1 is consumed entirely (offcut too small to keep): no available
        # inventory remains, yet the event must stale delivery previews
        # because the already-cut pieces become isolated.
        self.init({"stock": [stock_row("S1", 500)]})
        self.commit_batch(batch("b1", [order("chair", 400)]))
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        before = self.read_state()
        report = self.register("INC-1", ["S1"])
        after = self.read_state()
        self.assertEqual(report["summary"]["isolated_entry_ids"], [])
        self.assertEqual(after["revision"], before["revision"])  # no entry change
        self.assertEqual(after["finished_revision"],
                         before["finished_revision"] + 1)
        with self.assertRaises(StalePlanError):
            self.commit_delivery(plan)
        # A fresh preview excludes the isolated piece and selects zero kits.
        blocked = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        self.assertEqual(blocked["selected_kits"], [])
        (excluded,) = blocked["excluded_pieces"]
        self.assertEqual(excluded["reasons"], ["isolated"])
        self.assertEqual(excluded["active_incidents"], ["INC-1"])
        self.commit_delivery(blocked)  # zero-kit commit is still valid

    def test_multiple_incidents_cover_and_release(self):
        self.init_two_stock_workspace()
        candidates = [ref("b1", "stock/S1", "chair", 1),
                      ref("b1", "stock/S1", "chair", 2)]
        kits = [kit("K", 0, [slot("a", 400, 400, group="G"),
                             slot("b", 400, 400, group="G")])]
        self.register("INC-A", ["S1"])
        revision_one = self.read_state()["finished_revision"]
        # A second covering event is a real finished-goods change as well.
        self.register("INC-B", ["S1"])
        self.assertEqual(self.read_state()["finished_revision"],
                         revision_one + 1)
        blocked = self.plan_delivery(request("D1", candidates, kits))
        self.assertEqual(blocked["selected_kits"], [])
        self.assertEqual({tuple(e["active_incidents"])
                          for e in blocked["excluded_pieces"]},
                         {("INC-A", "INC-B")})
        revision_after_register = self.read_state()["finished_revision"]
        inventory_revision = self.read_state()["revision"]
        # Releasing one of two covering events advances the finished revision
        # even though the inventory entries stay isolated under the other.
        self.release("INC-A")
        self.assertEqual(self.read_state()["finished_revision"],
                         revision_after_register + 1)
        self.assertEqual(self.read_state()["revision"], inventory_revision)
        still_blocked = self.plan_delivery(request("D1", candidates, kits))
        self.assertEqual(still_blocked["selected_kits"], [])
        self.assertEqual({tuple(e["active_incidents"])
                          for e in still_blocked["excluded_pieces"]},
                         {("INC-B",)})
        self.release("INC-B")
        open_plan = self.plan_delivery(request("D1", candidates, kits))
        self.assertEqual(len(open_plan["selected_kits"]), 1)
        self.commit_delivery(open_plan)

    def test_recall_after_dispatch_names_only_truly_affected_pieces(self):
        self.init({"stock": [stock_row("S1", 403), stock_row("S2", 203),
                             stock_row("S3", 303)]})
        self.commit_batch(batch("b1", [order("chair", 400)]))
        self.commit_batch(batch("b2", [order("shelf", 200)]))
        self.commit_batch(batch("b3", [order("rail", 300)]))
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1),
                   ref("b2", "stock/S2", "shelf", 1),
                   ref("b3", "stock/S3", "rail", 1)],
            [kit("K1", 0, [slot("a", 400, 400, group="G1"),
                           slot("b", 200, 200, group="G2")]),
             kit("K2", 1, [slot("a", 300, 300)])]))
        result = self.commit_delivery(plan)
        # Quality problem discovered in S1 after the dispatch.
        self.register("INC-1", ["S1"])
        detail = workspace.delivery_status(str(self.ws), "D1")
        self.assertEqual(detail["result"], result)  # frozen manifest intact
        self.assertEqual(detail["recall"]["active_incidents"], ["INC-1"])
        (k1, k2) = detail["recall"]["kits"]
        self.assertTrue(k1["recall"])
        self.assertEqual(k1["active_incidents"], ["INC-1"])
        # Only the S1-rooted piece is named; the S2 piece of the same kit is
        # not mislabeled as quality-affected.
        self.assertEqual([ref_of(p) for p in k1["affected_pieces"]],
                         [("b1", "stock/S1", "chair", 1)])
        self.assertFalse(k2["recall"])
        self.assertEqual(k2["affected_pieces"], [])
        # The affected query locates the same delivery from the incident side.
        affected = workspace.delivery_affected(str(self.ws), "INC-1")
        self.assertEqual(affected["status"], "active")
        (located,) = affected["deliveries"]
        self.assertEqual(located["dispatch_id"], "D1")
        self.assertEqual([k["kit_id"] for k in located["kits"]], ["K1"])
        self.assertEqual([ref_of(p) for p in located["kits"][0]["pieces"]],
                         [("b1", "stock/S1", "chair", 1)])
        # Delivered pieces keep status delivered; isolation is a second axis.
        status = workspace.delivery_status(str(self.ws))
        by_ref = {ref_of(p): p for p in status["pieces"]}
        chair = by_ref[("b1", "stock/S1", "chair", 1)]
        self.assertEqual(chair["status"], "delivered")
        self.assertEqual(chair["active_incidents"], ["INC-1"])
        # Releasing the incident clears the recall but never the dispatch.
        self.release("INC-1")
        detail = workspace.delivery_status(str(self.ws), "D1")
        self.assertFalse(any(k["recall"] for k in detail["recall"]["kits"]))
        by_ref = {ref_of(p): p
                  for p in workspace.delivery_status(str(self.ws))["pieces"]}
        self.assertEqual(by_ref[("b1", "stock/S1", "chair", 1)]["status"],
                         "delivered")

    def test_released_incident_still_locates_later_deliveries(self):
        self.init_two_stock_workspace()
        self.register("INC-1", ["S1"])
        self.release("INC-1")
        # The root is clear again: a new delivery may dispatch its pieces.
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        self.commit_delivery(plan)
        # The released incident still locates the delivery by root...
        affected = workspace.delivery_affected(str(self.ws), "INC-1")
        self.assertEqual(affected["status"], "released")
        (located,) = affected["deliveries"]
        self.assertEqual(located["dispatch_id"], "D1")
        self.assertEqual([ref_of(p) for p in located["kits"][0]["pieces"]],
                         [("b1", "stock/S1", "chair", 1)])
        # ...but nothing is recalled by an inactive event.
        detail = workspace.delivery_status(str(self.ws), "D1")
        self.assertEqual(detail["recall"]["active_incidents"], [])
        self.assertFalse(any(k["recall"] for k in detail["recall"]["kits"]))
        with self.assertRaisesRegex(ValueError, "unknown incident_id"):
            workspace.delivery_affected(str(self.ws), "INC-NOPE")

    def test_price_correction_and_close_do_not_stale_delivery_preview(self):
        self.init({"stock": [stock_row("S1", 1000), stock_row("S2", 250)]})
        self.register_price("P1", "S1", 1000, 0)
        self.register_price("P2", "S2", 500, 1)
        self.commit_batch(batch("b1", [order("chair", 400, 2),
                                       order("shelf", 200)]))
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        self.register_price("P1C", "S1", 900, 2)  # price correction
        self.close("C1", None, 1, 3)
        result = self.commit_delivery(plan)  # still valid
        self.assertEqual(result["finished_revision"], 2)
        # The frozen close and cost queries are untouched by the delivery.
        close = workspace.close_status(str(self.ws), "C1")
        self.assertEqual(close["inventory_revision"], 1)
        self.assertEqual(workspace.cost_query(str(self.ws))["summary"]["total"]
                         ["total_cents"], 1400)


class ConcurrencyAndReplayTests(DeliveryWorkspaceCase):
    def test_competing_previews_admit_exactly_one_commit(self):
        self.init_two_stock_workspace()
        kits = [kit("K", 0, [slot("a", 400, 400)])]
        plan_a = self.plan_delivery(
            request("DA", [ref("b1", "stock/S1", "chair", 1)], kits))
        plan_b = self.plan_delivery(
            request("DB", [ref("b1", "stock/S1", "chair", 1)], kits))
        self.assertEqual(plan_a["base_finished_revision"],
                         plan_b["base_finished_revision"])
        self.commit_delivery(plan_a)
        before = (self.ws / "state.json").read_bytes()
        with self.assertRaises(StalePlanError):
            self.commit_delivery(plan_b)
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        replanned = self.plan_delivery(
            request("DB", [ref("b1", "stock/S1", "chair", 1)], kits))
        self.assertEqual(replanned["selected_kits"], [])  # piece now delivered
        self.commit_delivery(replanned)

    def test_concurrent_delivery_commits_admit_exactly_one(self):
        self.init_two_stock_workspace()
        kits = [kit("K", 0, [slot("a", 400, 400)])]
        path_a = self.source(self.plan_delivery(
            request("DA", [ref("b1", "stock/S1", "chair", 1)], kits)))
        path_b = self.source(self.plan_delivery(
            request("DB", [ref("b1", "stock/S1", "chair", 1)], kits)))
        results, errors = {}, {}

        def run(key, path):
            try:
                results[key] = workspace.delivery_commit(
                    str(self.ws), path, self.out(f"{key}.json"))
            except (OSError, ValueError) as exc:
                errors[key] = exc

        threads = [threading.Thread(target=run, args=("DA", path_a)),
                   threading.Thread(target=run, args=("DB", path_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), StalePlanError)
        state = self.read_state()
        self.assertEqual(len(state["dispatches"]), 1)
        DispatchLedger().check(next(iter(results.values())), state)

    def test_replay_returns_frozen_result_and_conflicting_reuse_fails(self):
        self.init_two_stock_workspace()
        kits = [kit("K", 0, [slot("a", 400, 400)])]
        plan = self.plan_delivery(
            request("D1", [ref("b1", "stock/S1", "chair", 1)], kits))
        result = self.commit_delivery(plan, "d1.json")
        before = (self.ws / "state.json").read_bytes()
        replay = self.commit_delivery(plan, "d1-replay.json")
        self.assertEqual(replay, result)
        self.assertEqual(Path(self.out("d1-replay.json")).read_bytes(),
                         Path(self.out("d1.json")).read_bytes())
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        # Later events never block re-exporting the original frozen result.
        self.register("INC-1", ["S2"])
        self.commit_batch(batch("b9", [order("slat", 100)]))
        other = self.plan_delivery(
            request("D2", [ref("b1", "stock/S2", "shelf", 1)],
                    [kit("K", 0, [slot("a", 200, 200)])]))
        self.commit_delivery(other)
        reexported = self.commit_delivery(plan, "d1-late.json")
        self.assertEqual(reexported, result)
        # Same dispatch_id with a different request, plan, or base revision
        # conflicts; re-previewing cannot replace the original delivery.
        changed_request = self.plan_delivery(
            request("D1", [ref("b1", "stock/S1", "chair", 2)], kits))
        with self.assertRaises(ConflictError):
            self.commit_delivery(changed_request)
        replanned_same_request = self.plan_delivery(
            request("D1", [ref("b1", "stock/S1", "chair", 1)], kits))
        self.assertNotEqual(replanned_same_request["base_finished_revision"],
                            plan["base_finished_revision"])
        with self.assertRaises(ConflictError):
            self.commit_delivery(replanned_same_request)
        tampered = dict(plan, selected_kits=[])
        with self.assertRaises(ConflictError):
            self.commit_delivery(tampered)

    def test_tampered_or_foreign_delivery_plan_is_rejected(self):
        self.init_two_stock_workspace()
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1),
                   ref("b1", "stock/S1", "chair", 2)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        before = (self.ws / "state.json").read_bytes()
        # Hand-edited allocation: swap in the other chair piece.
        fake = json.loads(json.dumps(plan))
        fake["selected_kits"][0]["slots"][0]["piece"]["unit"] = 2
        fake["metrics"]["piece_sequence"][0]["unit"] = 2
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.commit_delivery(fake)
        # A non-canonical optimum (valid constraints, wrong choice) fails too.
        self.assertEqual(plan["selected_kits"][0]["slots"][0]["piece"]["unit"], 1)
        foreign = dict(plan, workspace_id="elsewhere")
        with self.assertRaisesRegex(ValueError, "different workspace"):
            self.commit_delivery(foreign)
        broken = dict(plan, request_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "hash"):
            self.commit_delivery(broken)
        missing = dict(plan)
        del missing["metrics"]
        with self.assertRaisesRegex(ValueError, "missing"):
            self.commit_delivery(missing)
        self.assertEqual((self.ws / "state.json").read_bytes(), before)

    def test_output_conflict_is_detected_before_state_changes(self):
        self.init_two_stock_workspace()
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        before = (self.ws / "state.json").read_bytes()
        Path(self.out("taken.json")).write_text('{"not": "this"}\n')
        with self.assertRaises(FileExistsError):
            workspace.delivery_commit(str(self.ws), self.source(plan),
                                      self.out("taken.json"))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertEqual(Path(self.out("taken.json")).read_text(),
                         '{"not": "this"}\n')
        self.assertEqual(self.read_state()["dispatches"], {})


class CrashRecoveryTests(DeliveryWorkspaceCase):
    def test_failed_state_write_leaves_no_partial_dispatch(self):
        self.init_two_stock_workspace()
        plan_path = self.source(self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])])))
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace.os.replace",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.delivery_commit(str(self.ws), plan_path,
                                          self.out("d1.json"))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertEqual([p for p in self.ws.iterdir()
                          if p.name.startswith(".state-")], [])
        result = workspace.delivery_commit(str(self.ws), plan_path,
                                           self.out("d1.json"))
        self.assertEqual(result["finished_revision"], 2)
        DispatchLedger().check(result, self.read_state())

    def test_crash_after_state_write_recovers_without_double_dispatch(self):
        self.init_two_stock_workspace()
        plan_path = self.source(self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])])))
        output = self.out("d1.json")
        with mock.patch("sawmill.workspace.publish",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.delivery_commit(str(self.ws), plan_path, output)
        self.assertFalse(Path(output).exists())
        state = self.read_state()
        self.assertEqual(state["finished_revision"], 2)  # state was durable
        self.assertEqual(list(state["dispatches"]), ["D1"])
        replay = workspace.delivery_commit(str(self.ws), plan_path, output)
        self.assertTrue(Path(output).exists())
        self.assertEqual(replay["finished_revision"], 2)
        self.assertEqual(self.read_state()["finished_revision"], 2)
        DispatchLedger().check(replay, self.read_state())


class CompatibilityTests(DeliveryWorkspaceCase):
    def test_workspace_predating_delivery_needs_no_migration(self):
        self.init_two_stock_workspace()
        # Simulate a workspace written before the delivery feature existed.
        state_path = self.ws / "state.json"
        state = json.loads(state_path.read_text())
        del state["finished_revision"]
        del state["dispatches"]
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n")
        # Piece facts are recovered from the existing commits; revision is 0.
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        self.assertEqual(plan["base_finished_revision"], 0)
        result = self.commit_delivery(plan)
        self.assertEqual(result["finished_revision"], 1)
        self.assertEqual(len(plan["selected_kits"]), 1)

    def test_existing_queries_and_reports_are_unchanged_by_delivery(self):
        self.init_two_stock_workspace()
        jobs_before = {job: workspace.status(str(self.ws), job)
                       for job in ("b1",)}
        status_before = workspace.status(str(self.ws))
        self.register("INC-1", ["S2"])
        report_before = workspace.incident_status(str(self.ws), "INC-1")
        plan = self.plan_delivery(request(
            "D1", [ref("b1", "stock/S1", "chair", 1)],
            [kit("K", 0, [slot("a", 400, 400)])]))
        self.commit_delivery(plan)
        self.assertEqual(workspace.status(str(self.ws), "b1"), jobs_before["b1"])
        current = workspace.status(str(self.ws))
        self.assertEqual(current["committed_jobs"],
                         status_before["committed_jobs"])
        self.assertEqual(current["summary"]["available_entries"],
                         status_before["summary"]["available_entries"])
        self.assertEqual(workspace.incident_status(str(self.ws), "INC-1"),
                         report_before)


class CliTests(DeliveryWorkspaceCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "sawmill.inventory_cli", *args],
                              capture_output=True, text=True, env=env)

    def test_delivery_cli_end_to_end_and_failures(self):
        ws = self.out("ws")
        stock = self.source({"stock": [stock_row("S1", 1000),
                                       stock_row("S2", 250)]})
        self.assertEqual(self.run_cli("init", "--source", stock,
                                      "--workspace", ws).returncode, 0)
        batch_src = self.source(batch("b1", [order("chair", 400, 2),
                                             order("shelf", 200)]))
        self.assertEqual(self.run_cli("plan", "--workspace", ws, "--source",
                                      batch_src, "--output",
                                      self.out("p1.json")).returncode, 0)
        self.assertEqual(self.run_cli("commit", "--workspace", ws, "--plan",
                                      self.out("p1.json"), "--output",
                                      self.out("r1.json")).returncode, 0)
        delivery_src = self.source(request(
            "D1", [ref("b1", "stock/S1", "chair", 1),
                   ref("b1", "stock/S1", "chair", 2)],
            [kit("K", 0, [slot("a", 400, 400, group="G"),
                          slot("b", 400, 400, group="G")])]))
        dplan = self.out("dp.json")
        proc = self.run_cli("delivery", "plan", "--workspace", ws,
                            "--source", delivery_src, "--output", dplan)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("delivery", "plan", "--workspace", ws,
                            "--source", delivery_src, "--output", dplan)
        self.assertEqual(proc.returncode, 2)  # output exists
        dresult = self.out("dr.json")
        proc = self.run_cli("delivery", "commit", "--workspace", ws,
                            "--plan", dplan, "--output", dresult)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(Path(dresult).read_text())
        self.assertEqual(result["finished_revision"], 2)
        # Idempotent retry against the same output path succeeds.
        proc = self.run_cli("delivery", "commit", "--workspace", ws,
                            "--plan", dplan, "--output", dresult)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(Path(dresult).read_text()), result)
        # Status queries.
        proc = self.run_cli("delivery", "status", "--workspace", ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = json.loads(proc.stdout)
        self.assertEqual(status["summary"]["delivered"], 2)
        proc = self.run_cli("delivery", "status", "--workspace", ws,
                            "--dispatch", "D1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["result"], result)
        proc = self.run_cli("delivery", "status", "--workspace", ws,
                            "--dispatch", "NOPE")
        self.assertEqual(proc.returncode, 2)
        # Incident after dispatch: recall shows up in both queries.
        incident_src = self.source({"incident_id": "INC-1", "stock_ids": ["S1"]})
        proc = self.run_cli("incident", "register", "--workspace", ws,
                            "--source", incident_src, "--output",
                            self.out("inc.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("delivery", "affected", "--workspace", ws,
                            "--incident", "INC-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        affected = json.loads(proc.stdout)
        self.assertEqual(affected["deliveries"][0]["dispatch_id"], "D1")
        proc = self.run_cli("delivery", "status", "--workspace", ws,
                            "--dispatch", "D1")
        recalled = json.loads(proc.stdout)
        self.assertTrue(recalled["recall"]["kits"][0]["recall"])
        proc = self.run_cli("delivery", "affected", "--workspace", ws,
                            "--incident", "NOPE")
        self.assertEqual(proc.returncode, 2)
        # A stale delivery plan exits 2 and changes nothing.
        stale_src = self.source(request(
            "D2", [ref("b1", "stock/S2", "shelf", 1)],
            [kit("K", 0, [slot("a", 200, 200)])]))
        proc = self.run_cli("delivery", "plan", "--workspace", ws,
                            "--source", stale_src, "--output", self.out("sp.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.run_cli("incident", "release", "--workspace", ws,
                     "--incident", "INC-1", "--output", self.out("rel.json"))
        proc = self.run_cli("delivery", "commit", "--workspace", ws,
                            "--plan", self.out("sp.json"),
                            "--output", self.out("sr.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("stale", proc.stderr)
        self.assertFalse(Path(self.out("sr.json")).exists())


if __name__ == "__main__":
    unittest.main()
