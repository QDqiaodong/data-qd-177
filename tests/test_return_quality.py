"""Quality isolation, preview staleness, concurrency and CLI tests for returns.

Covers: goods receipt under an active root incident with both cutting
processes blocked; partial multi-incident release; historical delivery
location keeping both the original and the remanufactured dispatch; the two
preview revisions going stale on return registration while price corrections
do not stale previews; competing registrations; crash/output atomicity; and
the command-line surface including exit codes.
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

from test_returns import ReturnCase, order, stock_row

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


class QualityFlowTests(ReturnCase):
    def build_delivered(self, length=20, piece_mm=5):
        self.init({"stock": [stock_row("R1", length)]})
        self.salvage("j1", ["stock/R1"], [order("B", piece_mm)],
                     kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", piece_mm)])])
        return b_ref

    def test_return_under_active_incident_succeeds_but_blocks_both_processes(self):
        b_ref = self.build_delivered()
        workspace.register_incident(
            str(self.ws), self.source({"incident_id": "INC1",
                                       "stock_ids": ["R1"]}),
            self.out("inc1.json"))
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        # Physical receipt is durable and the new identity is isolated.
        view = next(e for e in workspace.status(str(self.ws))["entries"]
                    if e["entry_id"] == rework)
        self.assertEqual(view["status"], "isolated")
        self.assertEqual(view["active_incidents"], ["INC1"])
        # Normal process: the isolated identity is simply unavailable.
        normal = workspace.plan(
            str(self.ws),
            self.source({"job_id": "j2", "kerf_mm": 1, "min_offcut_mm": 2,
                         "orders": [order("C", 4)]}),
            self.out("nplan.json"))
        self.assertEqual(normal["consumed_entry_ids"], [])
        self.assertTrue(normal["unfilled"])
        # Salvage process: explicit isolation rejection.
        with self.assertRaisesRegex(ValueError, "isolated"):
            workspace.salvage_plan(
                str(self.ws),
                self.source({"job_id": "j3", "kerf_mm": 1, "min_offcut_mm": 2,
                             "candidate_entry_ids": [rework],
                             "orders": [order("C", 4)]}),
                self.out("splan.json"))

    def test_partial_release_keeps_isolation_until_last_event_goes(self):
        b_ref = self.build_delivered()
        self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = "return/RT1/1"
        workspace.register_incident(
            str(self.ws), self.source({"incident_id": "INC1",
                                       "stock_ids": ["R1"]}),
            self.out("inc1.json"))
        workspace.register_incident(
            str(self.ws), self.source({"incident_id": "INC2",
                                       "stock_ids": ["R1"]}),
            self.out("inc2.json"))
        workspace.release_incident(str(self.ws), "INC1",
                                   self.out("rel1.json"))
        view = next(e for e in workspace.status(str(self.ws))["entries"]
                    if e["entry_id"] == rework)
        self.assertEqual(view["status"], "isolated")
        self.assertEqual(view["active_incidents"], ["INC2"])
        workspace.release_incident(str(self.ws), "INC2",
                                   self.out("rel2.json"))
        view = next(e for e in workspace.status(str(self.ws))["entries"]
                    if e["entry_id"] == rework)
        self.assertEqual(view["status"], "available")
        # Releasing never revives consumed identities.
        root = next(e for e in workspace.status(str(self.ws))["entries"]
                    if e["entry_id"] == "stock/R1")
        self.assertEqual(root["status"], "consumed")
        # The now-usable rework identity cuts normally.
        result = self.normal("j2", [order("C", 4)], kerf=1, min_offcut=2)
        self.assertEqual(result["consumed_entry_ids"], [rework])

    def test_local_defect_blocks_normal_process_but_salvage_creates_descendants(self):
        b_ref = self.build_delivered()
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", b_ref)])
        rework = receipt["items"][0]["return_entry_id"]
        state = workspace._read_state(str(self.ws))
        workspace.register_inspection(
            str(self.ws),
            self.source({"inspection_id": "INSP1", "entry_id": rework,
                         "expected_inventory_revision": state["revision"],
                         "reject_zones": [{"start_mm": 2, "end_mm": 3}]}),
            self.out("insp1.json"))
        normal = workspace.plan(
            str(self.ws),
            self.source({"job_id": "j2", "kerf_mm": 1, "min_offcut_mm": 1,
                         "orders": [order("C", 2)]}),
            self.out("nplan.json"))
        self.assertNotIn(rework, normal["consumed_entry_ids"])
        salvage = workspace.salvage_plan(
            str(self.ws),
            self.source({"job_id": "j3", "kerf_mm": 1, "min_offcut_mm": 1,
                         "candidate_entry_ids": [rework],
                         "orders": [order("C", 1, 2)]}),
            self.out("splan.json"))
        ticket = next(t for t in salvage["tickets"]
                      if t["entry_id"] == rework)
        self.assertIn("defect_reject",
                      [p["kind"] for p in ticket["partitions"]])
        workspace.salvage_commit(str(self.ws), self.out("splan.json"),
                                 self.out("sresult.json"))
        # The salvage offcuts are real descendants on the same root.
        state = workspace._read_state(str(self.ws))
        for offcut in salvage["added_entries"]:
            entry = state["entries"][offcut["entry_id"]]
            self.assertEqual(entry["root_stock_id"], "R1")
            self.assertEqual(entry["status"], "available")

    def test_history_locates_both_deliveries_and_recall_keeps_other_roots_clean(self):
        # Two roots, each producing one same-named piece in its own salvage.
        self.init({"stock": [stock_row("R1", 12), stock_row("R2", 12)]})
        self.salvage("j1", ["stock/R1"], [order("B", 5)], kerf=1, min_offcut=2)
        self.salvage("j2", ["stock/R2"], [order("B", 5)], kerf=1, min_offcut=2)
        r1_piece = self.ref("j1", "stock/R1", "B", 1)
        r2_piece = self.ref("j2", "stock/R2", "B", 1)
        # Distinct grain groups allow one kit to span the two roots.
        slots = [{"slot_id": "s1", "material": "oak", "min_mm": 5,
                  "max_mm": 5, "grain_group": "G1"},
                 {"slot_id": "s2", "material": "oak", "min_mm": 5,
                  "max_mm": 5, "grain_group": "G2"}]
        self.deliver("D1", [r1_piece, r2_piece], [("K1", slots)])
        # Return the R1 piece, recut it, and dispatch the new piece again.
        receipt = self.return_register(
            "RT1", [self.item("D1", "K1", "s1", r1_piece)])
        rework = receipt["items"][0]["return_entry_id"]
        self.normal("j3", [order("C", 4)], kerf=1, min_offcut=2)
        c_ref = self.ref("j3", rework, "C", 1)
        self.deliver("D2", [c_ref], [("K2", [self.slot("t1", 4)])])
        workspace.register_incident(
            str(self.ws), self.source({"incident_id": "INC1",
                                       "stock_ids": ["R1"]}),
            self.out("inc1.json"))
        affected = workspace.delivery_affected(str(self.ws), "INC1")
        dispatch_ids = {d["dispatch_id"] for d in affected["deliveries"]}
        # Both the original delivery and the remanufactured one are located.
        self.assertEqual(dispatch_ids, {"D1", "D2"})
        d1 = next(d for d in affected["deliveries"]
                  if d["dispatch_id"] == "D1")
        (located,) = d1["kits"][0]["pieces"]
        self.assertEqual({k: located[k] for k in
                          ("job_id", "entry_id", "order_id", "unit")},
                         r1_piece)  # never the R2 same-kit piece
        self.assertEqual(located["root_stock_id"], "R1")
        # Slot-level return facts remain present on the original dispatch.
        detail = workspace.return_status(str(self.ws), "RT1")
        self.assertEqual(detail["current"][0]["active_incidents"], ["INC1"])


class StalenessAndConcurrencyTests(ReturnCase):
    def build(self):
        self.init({"stock": [stock_row("R1", 12)]})
        self.salvage("j1", ["stock/R1"], [order("B", 5)], kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 5)])])
        return b_ref

    def test_return_stales_cutting_and_delivery_previews(self):
        b_ref = self.build()
        cutting = workspace.plan(
            str(self.ws),
            self.source({"job_id": "jx", "kerf_mm": 1, "min_offcut_mm": 2,
                         "orders": [order("Z", 3)]}),
            self.out("cut-preview.json"))
        delivery = workspace.delivery_plan(
            str(self.ws),
            self.source({"dispatch_id": "DX", "candidate_pieces": [b_ref],
                         "kits": [{"kit_id": "K1", "priority": 0,
                                   "slots": [self.slot("s1", 5)]}]}),
            self.out("del-preview.json"))
        self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)])
        with self.assertRaises(StalePlanError):
            workspace.commit(str(self.ws), self.out("cut-preview.json"),
                             self.out("cut-result.json"))
        with self.assertRaises(StalePlanError):
            workspace.delivery_commit(str(self.ws),
                                      self.out("del-preview.json"),
                                      self.out("del-result.json"))

    def test_price_correction_does_not_stale_either_preview(self):
        b_ref = self.build()
        workspace.register_price(
            str(self.ws),
            self.source({"price_id": "P1", "stock_id": "R1",
                         "amount_cents": 4, "expected_price_revision": 0}),
            self.out("p1.json"))
        cutting = workspace.plan(
            str(self.ws),
            self.source({"job_id": "jx", "kerf_mm": 1, "min_offcut_mm": 2,
                         "orders": [order("Z", 3)]}),
            self.out("cut-preview.json"))
        delivery = workspace.delivery_plan(
            str(self.ws),
            self.source({"dispatch_id": "DX", "candidate_pieces": [b_ref],
                         "kits": [{"kit_id": "K1", "priority": 0,
                                   "slots": [self.slot("s1", 5)]}]}),
            self.out("del-preview.json"))
        workspace.register_price(
            str(self.ws),
            self.source({"price_id": "P2", "stock_id": "R1",
                         "amount_cents": 9, "expected_price_revision": 1}),
            self.out("p2.json"))
        # Both previews remain committable; clean up the cutting one first.
        workspace.delivery_commit(str(self.ws), self.out("del-preview.json"),
                                  self.out("del-result.json"))

    def test_competing_registrations_of_one_piece_admit_one(self):
        b_ref = self.build()
        state = workspace._read_state(str(self.ws))
        expected = (state["revision"], state["finished_revision"])
        results, errors = {}, {}

        def run(return_id):
            req = {"return_id": return_id,
                   "expected_inventory_revision": expected[0],
                   "expected_finished_revision": expected[1],
                   "items": [self.item("D1", "K1", "s1", b_ref)]}
            try:
                results[return_id] = workspace.register_return(
                    str(self.ws), self.source(req),
                    self.out(f"{return_id}.json"))
            except (OSError, ValueError) as exc:
                errors[return_id] = exc

        threads = [threading.Thread(target=run, args=("RT1",)),
                   threading.Thread(target=run, args=("RT2",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        state = workspace._read_state(str(self.ws))
        self.assertEqual(len(state["return_order"]), 1)
        self.assertEqual(len([e for e in state["entries"].values()
                              if e["kind"] == "return"]), 1)

    def test_register_vs_cut_are_serialized_in_actual_order(self):
        b_ref = self.build()
        cutting = workspace.plan(
            str(self.ws),
            self.source({"job_id": "jx", "kerf_mm": 1, "min_offcut_mm": 2,
                         "orders": [order("Z", 3)]}),
            self.out("cut-preview.json"))
        state = workspace._read_state(str(self.ws))
        # The cutting preview is current right now; commit it first.
        result = workspace.commit(str(self.ws), self.out("cut-preview.json"),
                                  self.out("cut-result.json"))
        self.assertEqual(result["revision"], state["revision"] + 1)
        # The return request pinned to the older inventory revision is stale.
        with self.assertRaisesRegex(ValueError, "expected_inventory_revision"):
            self.return_register("RT1", [self.item("D1", "K1", "s1", b_ref)],
                                 expected=(state["revision"],
                                           state["finished_revision"]))

    def test_output_conflict_is_detected_before_state_changes(self):
        b_ref = self.build()
        blocking = self.out("blocking.json")
        Path(blocking).write_text('{"different": true}\n')
        before = (self.ws / "state.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.return_register(
                "RT1", [self.item("D1", "K1", "s1", b_ref)], output=blocking)
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertEqual(workspace.return_status(str(self.ws))["summary"],
                         {"returns": 0, "returned_pieces": 0})

    def test_failed_state_write_leaves_no_partial_return(self):
        b_ref = self.build()
        state = workspace._read_state(str(self.ws))
        req = {"return_id": "RT1",
               "expected_inventory_revision": state["revision"],
               "expected_finished_revision": state["finished_revision"],
               "items": [self.item("D1", "K1", "s1", b_ref)]}
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace._write_state",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.register_return(
                    str(self.ws), self.source(req), self.out("rt1.json"))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertFalse(Path(self.out("rt1.json")).exists())
        # Retry completes cleanly with a single rework identity.
        result = workspace.register_return(
            str(self.ws), self.source(req), self.out("rt1.json"))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(
            len([e for e in read_state(self.ws)["entries"].values()
                 if e["kind"] == "return"]), 1)

    def test_crash_after_state_write_replays_without_double_entry(self):
        b_ref = self.build()
        state = workspace._read_state(str(self.ws))
        req = {"return_id": "RT1",
               "expected_inventory_revision": state["revision"],
               "expected_finished_revision": state["finished_revision"],
               "items": [self.item("D1", "K1", "s1", b_ref)]}
        output = self.out("rt1.json")
        with mock.patch("sawmill.workspace.publish",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.register_return(
                    str(self.ws), self.source(req), output)
        self.assertFalse(Path(output).exists())
        replay = workspace.register_return(
            str(self.ws), self.source(req), output)
        self.assertTrue(Path(output).exists())
        state = read_state(self.ws)
        self.assertEqual(len([e for e in state["entries"].values()
                              if e["kind"] == "return"]), 1)
        self.assertEqual(state["return_order"], ["RT1"])
        self.assertEqual(json.loads(Path(output).read_text()), replay)


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


class ReturnCliTests(ReturnCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "sawmill.inventory_cli",
                               *args], capture_output=True, text=True, env=env)

    def test_cli_register_status_and_failure_exit_codes(self):
        self.init({"stock": [stock_row("R1", 12)]})
        self.salvage("j1", ["stock/R1"], [order("B", 5)], kerf=1, min_offcut=2)
        b_ref = self.ref("j1", "stock/R1", "B", 1)
        self.deliver("D1", [b_ref], [("K1", [self.slot("s1", 5)])])
        state = workspace._read_state(str(self.ws))
        req = {"return_id": "RT1",
               "expected_inventory_revision": state["revision"],
               "expected_finished_revision": state["finished_revision"],
               "items": [self.item("D1", "K1", "s1", b_ref)]}
        source = self.source(req)
        output = self.out("rt1.json")
        proc = self.run_cli("return", "register", "--workspace", str(self.ws),
                            "--source", source, "--output", output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(Path(output).read_text())
        self.assertEqual(receipt["kind"], "stock-workspace-return")
        # Replay succeeds and is byte-identical.
        proc = self.run_cli("return", "register", "--workspace", str(self.ws),
                            "--source", source, "--output", output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Status list and single-receipt views.
        proc = self.run_cli("return", "status", "--workspace", str(self.ws))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listing = json.loads(proc.stdout)
        self.assertEqual(listing["summary"]["returns"], 1)
        proc = self.run_cli("return", "status", "--workspace", str(self.ws),
                            "--return", "RT1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        detail = json.loads(proc.stdout)
        self.assertEqual(detail["return_id"], "RT1")
        self.assertEqual(detail["current"][0]["current_status"], "available")
        proc = self.run_cli("return", "status", "--workspace", str(self.ws),
                            "--return", "missing")
        self.assertEqual(proc.returncode, 2)
        # Invalid input exits 2.
        bad = self.source({"return_id": "RTX",
                           "expected_inventory_revision": True,
                           "expected_finished_revision": 0, "items": []})
        proc = self.run_cli("return", "register", "--workspace", str(self.ws),
                            "--source", bad, "--output", self.out("bad.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("stock-workspace:", proc.stderr)
        self.assertFalse(Path(self.out("bad.json")).exists())


if __name__ == "__main__":
    unittest.main()
