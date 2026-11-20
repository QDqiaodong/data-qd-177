import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


def batch(job_id, orders, kerf=3, min_offcut=100):
    return {"job_id": job_id, "kerf_mm": kerf, "min_offcut_mm": min_offcut,
            "orders": orders}


def incident(incident_id, stock_ids):
    return {"incident_id": incident_id, "stock_ids": stock_ids}


def read_state(ws_dir):
    return json.loads((Path(ws_dir) / "state.json").read_text())


DEFAULT_STOCK = {"stock": [
    stock_row("S1", 1200),
    stock_row("W1", 800, "walnut"),
    stock_row("P1", 500, "pine"),
]}


class TraceChecker:
    """Independent ancestry verification derived only from the persisted state.

    Walks each entry's ``source`` back to its initial stock root without trusting
    the incident code, then proves every committed cut piece has exactly one root
    and that an incident report's piece set is exactly the pieces under its roots.
    """

    def __init__(self, ws_dir):
        self.ws = Path(ws_dir)
        self.entries = read_state(self.ws)["entries"]

    def root_of(self, entry_id, trail=None):
        trail = trail or []
        assert entry_id not in trail, f"cyclic source chain at {entry_id}"
        entry = self.entries[entry_id]
        source = entry["source"]
        if source["type"] == "initial":
            return source["stock_id"]
        parent = source["parent_entry_id"]
        assert self.entries[parent]["entry_id"] == parent
        return self.root_of(parent, trail + [entry_id])

    def pieces_by_root(self):
        pieces = {}
        for job in read_state(self.ws)["jobs"].values():
            for ticket in job["result"]["tickets"]:
                root = self.root_of(ticket["stock_id"])
                for cut in ticket["cuts"]:
                    key = (job["job_id"], ticket["stock_id"],
                           cut["order_id"], cut["unit"])
                    assert key not in pieces.get(root, ()), f"piece recorded twice: {key}"
                    pieces.setdefault(root, {})[key] = {
                        "start_mm": cut["start_mm"], "end_mm": cut["end_mm"],
                        "kerf_end_mm": cut["kerf_end_mm"],
                    }
        return pieces

    def check_every_piece_has_one_root(self):
        roots = set()
        for job in read_state(self.ws)["jobs"].values():
            for ticket in job["result"]["tickets"]:
                roots.add(self.root_of(ticket["stock_id"]))
        initial_roots = {entry["root_stock_id"] for entry in self.entries.values()}
        assert roots <= initial_roots, f"piece root not an initial stock: {roots - initial_roots}"
        # Every stored entry's ancestry must agree with its recorded root.
        for entry_id, entry in self.entries.items():
            assert self.root_of(entry_id) == entry["root_stock_id"], (
                f"root mismatch for {entry_id}")
        return roots

    def check_report(self, report):
        by_root = self.pieces_by_root()
        seen_roots = []
        total = 0
        for root_report in report["roots"]:
            root = root_report["stock_id"]
            assert root not in seen_roots, f"root repeated in report: {root}"
            seen_roots.append(root)
            expected = set(by_root.get(root, {}))
            actual = {(piece["job_id"], piece["entry_id"],
                       piece["order_id"], piece["unit"])
                      for piece in root_report["pieces"]}
            assert expected == actual, (
                f"report for {root} has {sorted(actual)} expected {sorted(expected)}")
            total += len(actual)
            expected_entries = {entry_id for entry_id in self.entries
                                if self.root_of(entry_id) == root}
            report_entries = {entry["entry_id"] for entry in root_report["entries"]}
            assert expected_entries == report_entries, (
                f"report entries for {root} incomplete: "
                f"{sorted(expected_entries - report_entries)}")
            for piece in root_report["pieces"]:
                ref = by_root[root][(piece["job_id"], piece["entry_id"],
                                     piece["order_id"], piece["unit"])]
                assert (piece["start_mm"], piece["end_mm"], piece["kerf_end_mm"]) == (
                    ref["start_mm"], ref["end_mm"], ref["kerf_end_mm"]), "coordinate drift"
                assert self.root_of(piece["entry_id"]) == root
                assert piece["root_stock_id"] == root
        assert total == sum(len(pieces) for root, pieces in by_root.items()
                            if root in seen_roots), (
            "report does not account for every cut piece under its roots")


class IncidentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ws = self.dir / "ws"
        self.seq = 0

    def source(self, value, name=None):
        self.seq += 1
        path = self.dir / (name or f"src-{self.seq}.json")
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return str(path)

    def out(self, name):
        return str(self.dir / name)

    def init(self, stock=DEFAULT_STOCK):
        return workspace.init_workspace(str(self.ws), self.source(stock, "stock.json"))

    def make_plan(self, request):
        self.seq += 1
        return workspace.plan(str(self.ws), self.source(request),
                              self.out(f"plan-{self.seq}.json"))

    def commit_new(self, request):
        self.seq += 1
        path = self.source(request)
        plan = workspace.plan(str(self.ws), path, self.out(f"plan-{self.seq}.json"))
        return workspace.commit(str(self.ws), self.out(f"plan-{self.seq}.json"),
                                self.out(f"result-{self.seq}.json"))

    def register(self, incident_id, roots, name=None, output=None):
        self.seq += 1
        src = self.source(incident(incident_id, roots), name)
        return workspace.register_incident(
            str(self.ws), src, output or self.out(f"incident-{self.seq}.json"))

    def release(self, incident_id, output=None):
        self.seq += 1
        return workspace.release_incident(
            str(self.ws), incident_id, output or self.out(f"release-{self.seq}.json"))


class ChainTests(IncidentCase):
    def build_chain(self):
        """batch-1 cuts two chairs from S1, batch-2 cuts a rail from its offcut."""
        self.init()
        first = self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        self.assertEqual(first["consumed_entry_ids"], ["stock/S1"])
        self.assertEqual([e["entry_id"] for e in first["added_entries"]],
                         ["offcut/batch-1/stock/S1"])
        second = self.commit_new(batch("batch-2", [order("rail", 250)]))
        self.assertEqual(second["consumed_entry_ids"], ["offcut/batch-1/stock/S1"])
        self.assertEqual([e["entry_id"] for e in second["added_entries"]],
                         ["offcut/batch-2/offcut/batch-1/stock/S1"])
        self.assertEqual(second["added_entries"][0]["usable_mm"], 141)
        return first, second

    def test_report_traces_two_batches_and_current_descendants(self):
        first, second = self.build_chain()
        report = self.register("INC-1", ["S1"])
        TraceChecker(self.ws).check_report(report)
        (root,) = report["roots"]
        self.assertEqual(root["stock_id"], "S1")
        self.assertEqual(root["summary"]["pieces"], 3)
        self.assertEqual(root["summary"]["committed_jobs"], ["batch-1", "batch-2"])
        self.assertEqual(root["summary"]["isolated_entries"], 1)
        piece_jobs = {piece["job_id"] for piece in root["pieces"]}
        self.assertEqual(piece_jobs, {"batch-1", "batch-2"})
        entry_status = {entry["entry_id"]: entry["status"] for entry in root["entries"]}
        self.assertEqual(entry_status, {
            "stock/S1": "consumed",
            "offcut/batch-1/stock/S1": "consumed",
            "offcut/batch-2/offcut/batch-1/stock/S1": "isolated",
        })
        # Piece identities and coordinates mirror the original committed reports.
        committed = {(t["stock_id"], c["order_id"], c["unit"]): c
                     for result in (first, second)
                     for t in result["tickets"] for c in t["cuts"]}
        for piece in root["pieces"]:
            ref = committed[(piece["entry_id"], piece["order_id"], piece["unit"])]
            self.assertEqual(piece["start_mm"], ref["start_mm"])
            self.assertEqual(piece["end_mm"], ref["end_mm"])
            self.assertEqual(piece["root_stock_id"], "S1")
        # Other roots are untouched by an S1-only incident.
        status = workspace.status(str(self.ws))
        by_id = {entry["entry_id"]: entry for entry in status["entries"]}
        self.assertEqual(by_id["stock/W1"]["status"], "available")
        self.assertEqual(by_id["stock/W1"]["active_incidents"], [])
        self.assertEqual(by_id["stock/P1"]["status"], "available")
        self.assertEqual(by_id["offcut/batch-2/offcut/batch-1/stock/S1"]
                         ["active_incidents"], ["INC-1"])

    def test_isolation_blocks_planning_until_last_incident_released(self):
        _, _ = self.build_chain()
        report = self.register("INC-A", ["S1"])
        TraceChecker(self.ws).check_report(report)
        second_incident = self.register("INC-B", ["S1"])
        TraceChecker(self.ws).check_report(second_incident)
        checker = TraceChecker(self.ws)
        self.assertEqual(checker.check_every_piece_has_one_root(), {"S1"})

        # Both incidents are active: the only usable descendant is isolated.
        blocked = self.make_plan(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(blocked["tickets"], [])
        self.assertEqual(blocked["unfilled"],
                         [{"order_id": "slat", "unit": 1, "length_mm": 100}])
        status = workspace.status(str(self.ws))
        by_id = {entry["entry_id"]: entry for entry in status["entries"]}
        self.assertEqual(by_id["offcut/batch-2/offcut/batch-1/stock/S1"]["status"],
                         "isolated")
        self.assertEqual(by_id["offcut/batch-2/offcut/batch-1/stock/S1"]
                         ["active_incidents"], ["INC-A", "INC-B"])

        released_a = self.release("INC-A")
        self.assertFalse(released_a["revision_changed"])
        self.assertEqual(released_a["roots"],
                         [{"stock_id": "S1", "still_isolated": True,
                           "active_incidents": ["INC-B"]}])
        still_blocked = self.make_plan(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(still_blocked["tickets"], [])

        released_b = self.release("INC-B")
        self.assertTrue(released_b["revision_changed"])
        self.assertEqual(released_b["roots"],
                         [{"stock_id": "S1", "still_isolated": False,
                           "active_incidents": []}])
        allowed = self.make_plan(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(allowed["consumed_entry_ids"],
                         ["offcut/batch-2/offcut/batch-1/stock/S1"])
        committed = self.commit_new(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(committed["revision"], released_b["revision"] + 1)

    def test_single_incident_isolates_then_release_restores_planning(self):
        self.build_chain()
        self.register("INC-1", ["S1"])
        blocked = self.make_plan(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(blocked["tickets"], [])
        self.assertEqual(blocked["unfilled"],
                         [{"order_id": "slat", "unit": 1, "length_mm": 100}])
        release = self.release("INC-1")
        self.assertTrue(release["revision_changed"])
        self.assertEqual(release["active_incidents"], [])
        reopened = self.make_plan(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(reopened["consumed_entry_ids"],
                         ["offcut/batch-2/offcut/batch-1/stock/S1"])
        committed = self.commit_new(batch("batch-3", [order("slat", 100)]))
        self.assertEqual(committed["revision"], reopened["base_revision"] + 1)
        self.assertTrue(committed["summary"]["complete"])

    def test_consumed_identity_never_resurrects(self):
        self.build_chain()
        before = {entry_id: entry for entry_id, entry in read_state(self.ws)["entries"].items()}
        self.register("INC-1", ["S1"])
        self.release("INC-1")
        after = read_state(self.ws)["entries"]
        for entry_id, entry in before.items():
            if entry["status"] == "consumed":
                self.assertEqual(after[entry_id]["status"], "consumed")
                self.assertEqual(after[entry_id]["consumed_by"], entry["consumed_by"])
        status = workspace.status(str(self.ws))
        by_id = {entry["entry_id"]: entry for entry in status["entries"]}
        self.assertEqual(by_id["offcut/batch-1/stock/S1"]["status"], "consumed")
        self.assertEqual(by_id["offcut/batch-2/offcut/batch-1/stock/S1"]
                         ["status"], "available")
        self.assertEqual(status["summary"]["available_entries"], 3)


class RevisionTests(IncidentCase):
    def test_registration_invalidates_existing_plan_without_state_change(self):
        self.init()
        self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        # Preview a plan against the pre-incident revision.
        stale_path = self.source(batch("batch-2", [order("slat", 100)]), "stale-src.json")
        stale = workspace.plan(str(self.ws), stale_path, self.out("stale-plan.json"))
        self.assertEqual(stale["consumed_entry_ids"], ["offcut/batch-1/stock/S1"])
        before = read_state(self.ws)
        report = self.register("INC-1", ["S1"])
        self.assertEqual(report["summary"]["isolated_entry_ids"],
                         ["offcut/batch-1/stock/S1"])
        after = read_state(self.ws)
        self.assertEqual(after["revision"], before["revision"] + 1)
        with self.assertRaises(StalePlanError):
            workspace.commit(str(self.ws), self.out("stale-plan.json"),
                             self.out("stale-result.json"))
        self.assertEqual(read_state(self.ws), after)
        self.assertFalse(Path(self.out("stale-result.json")).exists())
        # Releasing pushes the revision again, so a plan made while isolated is stale.
        isolated_path = self.source(batch("batch-2", [order("slat", 100)]),
                                    "isolated-src.json")
        workspace.plan(str(self.ws), isolated_path, self.out("isolated-plan.json"))
        blocked_plan = json.loads(Path(self.out("isolated-plan.json")).read_text())
        self.assertEqual(blocked_plan["tickets"], [])
        release = self.release("INC-1")
        self.assertTrue(release["revision_changed"])
        with self.assertRaises(StalePlanError):
            workspace.commit(str(self.ws), self.out("isolated-plan.json"),
                             self.out("isolated-result.json"))

    def test_release_that_changes_nothing_keeps_revision(self):
        # S1 is fully consumed; an incident on it isolates nothing.
        self.init({"stock": [stock_row("S1", 500)]})
        self.commit_new(batch("batch-1", [order("chair", 400)]))
        before = read_state(self.ws)
        report = self.register("INC-1", ["S1"])
        self.assertEqual(report["summary"]["isolated_entry_ids"], [])
        self.assertEqual(read_state(self.ws)["revision"], before["revision"])
        release = self.release("INC-1")
        self.assertEqual(release["revision"], before["revision"])
        self.assertFalse(release["revision_changed"])


class IdempotencyAndValidationTests(IncidentCase):
    def test_replay_conflict_and_release_are_stable(self):
        self.init()
        self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        first = self.register("INC-1", ["S1"])
        state_once = read_state(self.ws)
        # Identical replay returns the original result and changes nothing.
        replay = self.register("INC-1", ["S1"])
        self.assertEqual(replay, first)
        self.assertEqual(read_state(self.ws), state_once)
        # Reordered roots are the same set and therefore the same event.
        subset = self.register("INC-2", ["S1"])
        self.assertEqual(subset["incident_id"], "INC-2")
        reordered = self.register("INC-2", ["S1"])
        self.assertEqual(reordered, subset)
        # Same id with a different root set conflicts and changes nothing.
        with self.assertRaises(ConflictError):
            self.register("INC-1", ["S1", "W1"])
        with self.assertRaises(ConflictError):
            self.register("INC-1", ["W1"])
        self.assertEqual(read_state(self.ws), read_state(self.ws))
        # Release is idempotent, then the released id cannot be reactivated.
        released = self.release("INC-1")
        state_released = read_state(self.ws)
        self.assertEqual(self.release("INC-1"), released)
        self.assertEqual(read_state(self.ws), state_released)
        with self.assertRaises(ConflictError):
            self.register("INC-1", ["S1"])

    def test_invalid_inputs_fail_and_preserve_state(self):
        self.init()
        self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        baseline = read_state(self.ws)
        bad_inputs = [
            incident("", ["S1"]),
            {"incident_id": "X"},
            incident("X", []),
            incident("X", ["S1", "S1"]),
            incident("X", ["NOPE"]),
            {"incident_id": "X", "stock_id": "NOPE"},
            {"incident_id": 5, "stock_ids": ["S1"]},
        ]
        for value in bad_inputs:
            src = self.source(value)
            with self.assertRaises((ValueError, KeyError, TypeError)):
                workspace.register_incident(str(self.ws), src,
                                            self.out("never.json"))
            self.assertEqual(read_state(self.ws), baseline)
            self.assertFalse(Path(self.out("never.json")).exists())
        # A conflicting pre-existing output preserves state and events.
        self.register("INC-1", ["S1"])
        baseline = read_state(self.ws)
        Path(self.out("taken.json")).write_text('{"not": "this"}\n')
        with self.assertRaises(FileExistsError):
            self.register("INC-2", ["W1"], output=self.out("taken.json"))
        self.assertEqual(read_state(self.ws), baseline)
        self.assertEqual(Path(self.out("taken.json")).read_text(), '{"not": "this"}\n')
        self.assertEqual(workspace.incident_status(str(self.ws))["active_incidents"],
                         ["INC-1"])
        with self.assertRaisesRegex(ValueError, "unknown incident_id"):
            workspace.release_incident(str(self.ws), "nope", self.out("r.json"))

    def test_duplicate_stock_id_input_is_rejected(self):
        self.init()
        with self.assertRaisesRegex(ValueError, "duplicate stock_id"):
            self.register("INC-1", ["S1", "S1"])
        self.assertEqual(read_state(self.ws)["incidents"], {})


class CompatibilityTests(IncidentCase):
    def test_existing_reports_and_other_roots_are_unchanged(self):
        self.init()
        first = self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        second = self.commit_new(batch("batch-2", [order("slat", 100)]))
        other = self.commit_new(batch("walnut-job", [order("board", 400, 1, "walnut")]))
        reports = {job: workspace.status(str(self.ws), job)
                   for job in ("batch-1", "batch-2", "walnut-job")}
        self.register("INC-1", ["S1"])
        for job, result in reports.items():
            self.assertEqual(workspace.status(str(self.ws), job), result)
        # The walnut piece and pine stock are outside the S1 root entirely.
        checker = TraceChecker(self.ws)
        self.assertEqual(checker.check_every_piece_has_one_root(), {"S1", "W1"})
        status = workspace.status(str(self.ws))
        by_id = {entry["entry_id"]: entry for entry in status["entries"]}
        self.assertEqual(by_id["stock/P1"]["status"], "available")
        self.assertEqual(by_id["stock/W1"]["status"], "consumed")
        self.assertEqual(by_id["stock/W1"]["active_incidents"], [])
        # Committed results and their byte-identical replay semantics still hold.
        self.assertEqual(workspace.status(str(self.ws), "batch-1"), first)
        self.assertEqual(second["revision"], 2)

    def test_query_reports_active_and_released_incidents(self):
        self.init()
        self.commit_new(batch("batch-1", [order("chair", 400, 2)]))
        self.register("INC-1", ["S1"])
        self.register("INC-W", ["W1"])
        listing = workspace.incident_status(str(self.ws))
        self.assertEqual(listing["active_incidents"], ["INC-1", "INC-W"])
        self.assertEqual([row["incident_id"] for row in listing["incidents"]],
                         ["INC-1", "INC-W"])
        detail = workspace.incident_status(str(self.ws), "INC-1")
        self.assertEqual(detail["isolated_entry_ids"], ["offcut/batch-1/stock/S1"])
        TraceChecker(self.ws).check_report(detail["report"])
        # A walnut incident isolates its own available root, not oak.
        walnut = workspace.incident_status(str(self.ws), "INC-W")
        self.assertEqual(walnut["isolated_entry_ids"], ["stock/W1"])
        self.release("INC-1")
        self.assertEqual(workspace.incident_status(str(self.ws))["active_incidents"],
                         ["INC-W"])
        released = workspace.incident_status(str(self.ws), "INC-1")
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["isolated_entry_ids"], [])


class MultiRootTests(IncidentCase):
    def test_multi_root_report_is_order_independent_and_complete(self):
        self.init({"stock": [stock_row("S1", 1200), stock_row("S2", 900),
                             stock_row("W1", 800, "walnut")]})
        self.commit_new(batch("batch-1", [order("chair", 400, 4)]))
        self.commit_new(batch("batch-2", [order("slat", 160)]))
        self.commit_new(batch("batch-w", [order("drawer", 300, 1, "walnut")]))
        forward = self.register("INC-M", ["S1", "S2"])
        backward = self.register("INC-N", ["S2", "S1"])
        # The same root set gives the same normalized, sorted report shape.
        self.assertEqual([row["stock_id"] for row in forward["roots"]], ["S1", "S2"])
        self.assertEqual([row["stock_id"] for row in backward["roots"]], ["S1", "S2"])
        TraceChecker(self.ws).check_report(forward)
        # Both roots' totals cover every traced piece; walnut stays out of scope.
        scoped_roots = {row["stock_id"] for row in forward["roots"]}
        by_root = TraceChecker(self.ws).pieces_by_root()
        self.assertEqual(sum(len(pieces) for root, pieces in by_root.items()
                             if root in scoped_roots), forward["summary"]["pieces"])
        self.assertNotIn("W1", scoped_roots)
        status = workspace.status(str(self.ws))
        by_id = {entry["entry_id"]: entry for entry in status["entries"]}
        self.assertEqual(by_id["stock/W1"]["status"], "consumed")
        self.assertEqual(by_id["stock/W1"]["active_incidents"], [])
        # Releasing one multi-root incident restores both roots.
        released = self.release("INC-M")
        self.assertEqual([row["still_isolated"] for row in released["roots"]],
                         [True, True])  # INC-N still covers both roots
        self.release("INC-N")
        reopened = workspace.incident_status(str(self.ws))
        self.assertEqual(reopened["active_incidents"], [])


class IncidentCliTests(IncidentCase):
    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "sawmill.inventory_cli", *args],
                              capture_output=True, text=True, env=env)

    def test_end_to_end_and_failure_exits(self):
        ws = self.out("ws")
        stock = self.source(DEFAULT_STOCK, "stock.json")
        self.assertEqual(self.run_cli("init", "--source", stock,
                                      "--workspace", ws).returncode, 0)
        batch_src = self.source(batch("batch-1", [order("chair", 400, 2)]), "b1.json")
        plan_out = self.out("p1.json")
        self.assertEqual(self.run_cli("plan", "--workspace", ws, "--source", batch_src,
                                      "--output", plan_out).returncode, 0)
        result_out = self.out("r1.json")
        self.assertEqual(self.run_cli("commit", "--workspace", ws, "--plan", plan_out,
                                      "--output", result_out).returncode, 0)
        inc_src = self.source(incident("INC-1", ["S1"]), "inc.json")
        rep_out = self.out("incident.json")
        proc = self.run_cli("incident", "register", "--workspace", ws,
                            "--source", inc_src, "--output", rep_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(Path(rep_out).read_text())
        self.assertEqual(report["summary"]["isolated_entry_ids"],
                         ["offcut/batch-1/stock/S1"])
        # A second identical output path cannot be overwritten with new content.
        proc = self.run_cli("incident", "register", "--workspace", ws,
                            "--source", self.source(incident("INC-2", ["W1"])),
                            "--output", rep_out)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(Path(rep_out).read_text()), report)
        proc = self.run_cli("incident", "status", "--workspace", ws)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["active_incidents"], ["INC-1"])
        rel_out = self.out("release.json")
        proc = self.run_cli("incident", "release", "--workspace", ws,
                            "--incident", "INC-1", "--output", rel_out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(Path(rel_out).read_text())["revision_changed"])
        proc = self.run_cli("incident", "release", "--workspace", ws,
                            "--incident", "nope", "--output", self.out("no.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(Path(self.out("no.json")).exists())
        proc = self.run_cli("incident", "status", "--workspace", ws,
                            "--incident", "nope")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
