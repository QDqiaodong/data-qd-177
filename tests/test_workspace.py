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
    return {"id": order_id, "material": material, "piece_mm": piece_mm, "quantity": quantity}


def stock_row(stock_id, length, material="oak", head=10, tail=10):
    return {"id": stock_id, "material": material, "length_mm": length,
            "trim_head_mm": head, "trim_tail_mm": tail}


def batch(job_id, orders, kerf=3, min_offcut=100):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut, "orders": orders}


DEFAULT_STOCK = {"stock": [stock_row("S1", 1200), stock_row("S2", 900)]}


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


class Ledger:
    """Independent available-length ledger replayed purely from commit results.

    Verifies per-material available-length conservation across every commit and
    that each inventory identity is consumed at most once at any point in time.
    """

    def __init__(self, ws_dir):
        self.ws = Path(ws_dir)
        self.consumed_ids = set()
        self.added_ids = set()
        self.applied = {}
        self.expected = self._available()

    def _available(self):
        totals = {}
        for entry in read_state(self.ws)["entries"].values():
            if entry["status"] == "available":
                totals[entry["material"]] = totals.get(entry["material"], 0) + entry["usable_mm"]
        return totals

    def check(self, result):
        state = read_state(self.ws)
        if result["job_id"] in self.applied:  # idempotent replay: no new effects
            assert self.applied[result["job_id"]] == result, "replay diverged"
            assert self.expected == self._available(), "replay changed inventory"
            return
        assert state["revision"] == result["revision"], "revision mismatch"
        for entry_id in result["consumed_entry_ids"]:
            assert entry_id not in self.consumed_ids, f"entry consumed twice: {entry_id}"
            entry = state["entries"][entry_id]
            assert entry["status"] == "consumed" and entry["consumed_by"] == result["job_id"]
            left = self.expected[entry["material"]] - entry["usable_mm"]
            if left:
                self.expected[entry["material"]] = left
            else:
                del self.expected[entry["material"]]
        self.consumed_ids.update(result["consumed_entry_ids"])
        for entry in result["added_entries"]:
            assert entry["entry_id"] not in self.added_ids, f"duplicate entry: {entry['entry_id']}"
            stored = state["entries"][entry["entry_id"]]
            assert stored["status"] == "available"
            assert stored["usable_mm"] == entry["usable_mm"]
            self.expected[entry["material"]] = (
                self.expected.get(entry["material"], 0) + entry["usable_mm"])
        self.added_ids.update(e["entry_id"] for e in result["added_entries"])
        self.applied[result["job_id"]] = result
        assert self.expected == self._available(), "available length not conserved"


class WorkspaceCase(unittest.TestCase):
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

    def init(self, stock=DEFAULT_STOCK):
        return workspace.init_workspace(str(self.ws), self.source(stock))

    def make_plan(self, request, name=None):
        self.seq += 1
        return workspace.plan(str(self.ws), self.source(request),
                              self.out(name or f"plan-{self.seq}.json"))

    def make_commit(self, plan_path, name=None):
        self.seq += 1
        return workspace.commit(str(self.ws), plan_path, self.out(name or f"result-{self.seq}.json"))

    def plan_file(self, plan_obj):
        return self.source(plan_obj)


class TwoBatchFlowTests(WorkspaceCase):
    def test_offcut_of_first_batch_feeds_second_batch(self):
        self.init()
        ledger = Ledger(self.ws)
        first = self.make_commit(self.plan_file(self.make_plan(
            batch("batch-1", [order("chair", 400, 4), order("shelf", 200)]))))
        ledger.check(first)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["consumed_entry_ids"], ["stock/S1", "stock/S2"])
        self.assertEqual(first["summary"],
                         {"complete": True, "used_stock": 2, "delivered_pieces": 5,
                          "unfilled_pieces": 0, "kerf_loss_mm": 15,
                          "reusable_offcut_mm": 171, "scrap_mm": 114})
        (added,) = first["added_entries"]
        self.assertEqual(added["entry_id"], "offcut/batch-1/stock/S1")
        self.assertEqual(added["kind"], "offcut")
        self.assertEqual(added["usable_mm"], 171)
        self.assertEqual(added["source"], {"type": "offcut", "job_id": "batch-1",
                                           "parent_entry_id": "stock/S1", "stock_id": "S1"})
        # The 74mm non-reusable remainder of S2 must not enter inventory.
        self.assertNotIn("offcut/batch-1/stock/S2", read_state(self.ws)["entries"])

        second = self.make_commit(self.plan_file(self.make_plan(
            batch("batch-2", [order("slat", 160)]))))
        ledger.check(second)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["consumed_entry_ids"], ["offcut/batch-1/stock/S1"])
        self.assertEqual(second["added_entries"], [])
        (ticket,) = second["tickets"]
        self.assertEqual(ticket["entry_kind"], "offcut")
        self.assertEqual(ticket["trim_head_mm"], 0)  # no trims re-applied to offcuts
        self.assertEqual(ticket["trim_tail_mm"], 0)
        self.assertEqual(ticket["stock_length_mm"], 171)
        self.assertEqual(ticket["cuts"],
                         [{"order_id": "slat", "unit": 1, "start_mm": 0,
                           "end_mm": 160, "kerf_end_mm": 163}])
        status = workspace.status(str(self.ws))
        self.assertEqual(status["revision"], 2)
        self.assertEqual(status["summary"]["available_entries"], 0)
        self.assertEqual(status["summary"]["consumed_entries"], 3)
        by_id = {e["entry_id"]: e for e in status["entries"]}
        self.assertEqual(by_id["stock/S1"]["consumed_by"], "batch-1")
        self.assertEqual(by_id["offcut/batch-1/stock/S1"]["consumed_by"], "batch-2")
        # Commit results stay queryable per job.
        self.assertEqual(workspace.status(str(self.ws), "batch-1"), first)
        self.assertEqual(workspace.status(str(self.ws), "batch-2"), second)


class IsolationTests(WorkspaceCase):
    def test_material_scrap_and_unallocated_stock_never_leak_into_tickets(self):
        self.init({"stock": [stock_row("S1", 1200), stock_row("P1", 500, "pine", 0, 0),
                             stock_row("W1", 1000, "walnut")]})
        ledger = Ledger(self.ws)
        first = self.make_commit(self.plan_file(self.make_plan(batch("b1", [
            order("chair", 400, 2), order("pin", 450, 1, "pine")]))))
        ledger.check(first)
        # Oak offcut 374 reusable; pine remainder 47 is scrap and stays out.
        self.assertEqual([e["entry_id"] for e in first["added_entries"]],
                         ["offcut/b1/stock/S1"])
        self.assertEqual(first["unused_entry_ids"], ["stock/W1"])
        # A pine order must not touch the oak offcut; walnut has no orders at all.
        second = self.make_commit(self.plan_file(self.make_plan(
            batch("b2", [order("pin2", 300, 1, "pine")]))))
        ledger.check(second)
        self.assertFalse(second["summary"]["complete"])
        self.assertEqual(second["unfilled"],
                         [{"order_id": "pin2", "unit": 1, "length_mm": 300}])
        self.assertEqual(second["tickets"], [])
        self.assertEqual(second["consumed_entry_ids"], [])
        status = workspace.status(str(self.ws))
        by_id = {e["entry_id"]: e for e in status["entries"]}
        self.assertEqual(by_id["offcut/b1/stock/S1"]["status"], "available")
        self.assertEqual(by_id["stock/W1"]["status"], "available")
        self.assertEqual(by_id["stock/P1"]["status"], "consumed")
        self.assertNotIn("offcut/b1/stock/P1", by_id)


class IdempotencyTests(WorkspaceCase):
    def test_retry_replays_and_conflicting_reuse_is_rejected(self):
        self.init()
        ledger = Ledger(self.ws)
        plan_path = self.plan_file(self.make_plan(
            batch("job-a", [order("chair", 400, 2)])))
        first = self.make_commit(plan_path, "first.json")
        ledger.check(first)
        before = (self.ws / "state.json").read_bytes()
        # Same request and same plan: replay, no new deduction or offcut.
        replay = self.make_commit(plan_path, "replay.json")
        self.assertEqual(replay, first)
        self.assertEqual(Path(self.out("replay.json")).read_bytes(),
                         Path(self.out("first.json")).read_bytes())
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        ledger.check(replay)  # consumed ids still seen exactly once overall
        # Same job_id with a different request conflicts.
        other_request = self.make_plan(batch("job-a", [order("chair", 300, 2)]))
        with self.assertRaises(ConflictError):
            self.make_commit(self.plan_file(other_request))
        # Same job_id and request but a different base revision also conflicts.
        replanned = self.make_plan(batch("job-a", [order("chair", 400, 2)]))
        self.assertNotEqual(replanned["base_revision"], first["base_revision"])
        with self.assertRaises(ConflictError):
            self.make_commit(self.plan_file(replanned))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "unknown job_id"):
            workspace.status(str(self.ws), "job-never-seen")

    def test_tampered_or_foreign_plan_is_rejected(self):
        self.init()
        plan_obj = self.make_plan(batch("job-a", [order("chair", 400, 2)]))
        before = (self.ws / "state.json").read_bytes()
        tampered = dict(plan_obj, consumed_entry_ids=["stock/S1", "stock/S2", "stock/NOPE"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.make_commit(self.plan_file(tampered))
        foreign = dict(plan_obj, workspace_id="elsewhere")
        with self.assertRaisesRegex(ValueError, "different workspace"):
            self.make_commit(self.plan_file(foreign))
        broken = dict(plan_obj, request_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "hash"):
            self.make_commit(self.plan_file(broken))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)


class StalePlanTests(WorkspaceCase):
    def test_first_commit_wins_and_loser_can_replan(self):
        self.init()
        ledger = Ledger(self.ws)
        plan_a = self.make_plan(batch("job-a", [order("slat", 160)]))
        plan_b = self.make_plan(batch("job-b", [order("slat", 160)]))
        self.assertEqual(plan_a["base_revision"], plan_b["base_revision"])
        first = self.make_commit(self.plan_file(plan_a))
        ledger.check(first)
        before = (self.ws / "state.json").read_bytes()
        with self.assertRaises(StalePlanError):
            self.make_commit(self.plan_file(plan_b))
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "unknown job_id"):
            workspace.status(str(self.ws), "job-b")
        replanned = self.make_plan(batch("job-b", [order("slat", 160)]))
        second = self.make_commit(self.plan_file(replanned))
        ledger.check(second)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(workspace.status(str(self.ws))["committed_jobs"], ["job-a", "job-b"])

    def test_concurrent_commits_on_same_revision_admit_exactly_one(self):
        self.init()
        ledger = Ledger(self.ws)
        plan_a = self.plan_file(self.make_plan(batch("job-a", [order("slat", 160)])))
        plan_b = self.plan_file(self.make_plan(batch("job-b", [order("slat", 160)])))
        results, errors = {}, {}

        def run(key, path):
            try:
                results[key] = workspace.commit(str(self.ws), path, self.out(f"{key}.json"))
            except (OSError, ValueError) as exc:
                errors[key] = exc

        threads = [threading.Thread(target=run, args=("job-a", plan_a)),
                   threading.Thread(target=run, args=("job-b", plan_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), StalePlanError)
        status = workspace.status(str(self.ws))
        self.assertEqual(status["revision"], 1)
        self.assertEqual(len(status["committed_jobs"]), 1)
        ledger.check(next(iter(results.values())))


class CrashRecoveryTests(WorkspaceCase):
    def test_failed_state_write_leaves_no_partial_commit(self):
        self.init()
        ledger = Ledger(self.ws)
        plan_path = self.plan_file(self.make_plan(batch("job-a", [order("chair", 400, 2)])))
        before = (self.ws / "state.json").read_bytes()
        with mock.patch("sawmill.workspace.os.replace",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.make_commit(plan_path)
        self.assertEqual((self.ws / "state.json").read_bytes(), before)
        self.assertEqual([p for p in self.ws.iterdir() if p.name.startswith(".state-")], [])
        result = self.make_commit(plan_path)  # retry recovers cleanly
        self.assertEqual(result["revision"], 1)
        ledger.check(result)

    def test_crash_after_state_write_replays_without_double_effects(self):
        self.init()
        ledger = Ledger(self.ws)
        plan_path = self.plan_file(self.make_plan(batch("job-a", [order("chair", 400, 2)])))
        output = self.out("result.json")
        with mock.patch("sawmill.workspace._publish_result",
                        side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                workspace.commit(str(self.ws), plan_path, output)
        self.assertFalse(Path(output).exists())
        self.assertEqual(read_state(self.ws)["revision"], 1)  # state was already durable
        replay = workspace.commit(str(self.ws), plan_path, output)
        self.assertTrue(Path(output).exists())
        self.assertEqual(replay["revision"], 1)
        status = workspace.status(str(self.ws))
        self.assertEqual(status["summary"]["available_entries"], 1)  # one offcut, no dupes
        self.assertEqual(status["committed_jobs"], ["job-a"])
        ledger.check(replay)


class UnfilledTests(WorkspaceCase):
    def test_unfilled_batch_commits_and_reports_accurately(self):
        self.init()
        ledger = Ledger(self.ws)
        result = self.make_commit(self.plan_file(self.make_plan(
            batch("job-a", [order("chair", 400, 2), order("huge", 5000)]))))
        ledger.check(result)
        self.assertFalse(result["summary"]["complete"])
        self.assertEqual(result["summary"]["unfilled_pieces"], 1)
        self.assertEqual(result["unfilled"],
                         [{"order_id": "huge", "unit": 1, "length_mm": 5000}])
        self.assertEqual(result["summary"]["delivered_pieces"], 2)
        self.assertEqual(workspace.status(str(self.ws), "job-a"), result)


class CliTests(WorkspaceCase):
    def run_cli(self, *args, module="sawmill.inventory_cli"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", module, *args],
                              capture_output=True, text=True, env=env)

    def test_end_to_end_and_failure_exits_preserve_outputs(self):
        ws = self.out("ws")
        stock = self.source(DEFAULT_STOCK)
        proc = self.run_cli("init", "--source", stock, "--workspace", ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("init", "--source", stock, "--workspace", ws)
        self.assertEqual(proc.returncode, 2)  # target exists
        self.assertIn("stock-workspace:", proc.stderr)

        plan_out = self.out("plan.json")
        batch_src = self.source(batch("batch-1", [order("chair", 400, 4), order("shelf", 200)]))
        proc = self.run_cli("plan", "--workspace", ws, "--source", batch_src,
                            "--output", plan_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan_bytes = Path(plan_out).read_bytes()
        proc = self.run_cli("plan", "--workspace", ws, "--source", batch_src,
                            "--output", plan_out)
        self.assertEqual(proc.returncode, 2)  # output exists, left untouched
        self.assertEqual(Path(plan_out).read_bytes(), plan_bytes)

        result_out = self.out("result.json")
        proc = self.run_cli("commit", "--workspace", ws, "--plan", plan_out,
                            "--output", result_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(Path(result_out).read_text())
        self.assertEqual(result["revision"], 1)
        # Idempotent retry against the same output path succeeds without changes.
        proc = self.run_cli("commit", "--workspace", ws, "--plan", plan_out,
                            "--output", result_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(Path(result_out).read_text()), result)

        proc = self.run_cli("status", "--workspace", ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = json.loads(proc.stdout)
        self.assertEqual(status["revision"], 1)
        self.assertEqual(status["committed_jobs"], ["batch-1"])
        proc = self.run_cli("status", "--workspace", ws, "--job", "batch-1")
        self.assertEqual(json.loads(proc.stdout), result)
        proc = self.run_cli("status", "--workspace", ws, "--job", "nope")
        self.assertEqual(proc.returncode, 2)

        # A stale plan fails and changes nothing.
        stale_plan = self.out("stale.json")
        proc = self.run_cli("plan", "--workspace", ws, "--source",
                            self.source(batch("batch-2", [order("slat", 160)])),
                            "--output", stale_plan)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        other_plan = self.out("other.json")
        self.run_cli("plan", "--workspace", ws, "--source",
                     self.source(batch("batch-3", [order("slat", 160)])),
                     "--output", other_plan)
        proc = self.run_cli("commit", "--workspace", ws, "--plan", other_plan,
                            "--output", self.out("other-result.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("commit", "--workspace", ws, "--plan", stale_plan,
                            "--output", self.out("stale-result.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("stale", proc.stderr)
        self.assertFalse(Path(self.out("stale-result.json")).exists())
        self.assertEqual(json.loads((Path(ws) / "state.json").read_text())["revision"], 2)

        # Invalid input and missing workspace are clean failures.
        proc = self.run_cli("plan", "--workspace", self.out("missing"),
                            "--source", batch_src, "--output", self.out("x.json"))
        self.assertEqual(proc.returncode, 2)
        bad = self.source({"job_id": "", "kerf_mm": 3, "min_offcut_mm": 100,
                           "orders": [order("a", 10)]})
        proc = self.run_cli("plan", "--workspace", ws, "--source", bad,
                            "--output", self.out("y.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(Path(self.out("y.json")).exists())

    def test_original_cut_ticket_command_unchanged(self):
        output = self.out("report.json")
        proc = self.run_cli("--source", str(ROOT / "fixtures" / "input.json"),
                            "--output", output, module="sawmill.cli")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(Path(output).read_text())
        self.assertEqual(report["summary"]["delivered_pieces"], 5)
        self.assertEqual(report["summary"]["reusable_offcut_mm"], 171)
        before = Path(output).read_bytes()
        proc = self.run_cli("--source", str(ROOT / "fixtures" / "input.json"),
                            "--output", output, module="sawmill.cli")
        self.assertEqual(proc.returncode, 2)  # existing output still rejected
        self.assertEqual(Path(output).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
