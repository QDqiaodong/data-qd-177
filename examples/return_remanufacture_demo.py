#!/usr/bin/env python3
"""Offline end-to-end sample: the 11mm return-and-remanufacture flow.

Runs entirely from the standard library against a throwaway workspace under
the system temp directory; it writes nothing into the repository and needs no
network. Run with the source tree on PYTHONPATH, e.g.::

    PYTHONPATH=src python3 -I examples/return_remanufacture_demo.py

The printed numbers are the acceptance figures: salvage A(4mm)+B(5mm) with
1mm kerfs exhausts an 11mm root; B is delivered, returned into a 5mm rework
identity at root [5,10), and recut normally into a 4mm piece whose root span
is [5,9). The two close chains (with and without an intermediate close at the
returned-but-not-recut snapshot) are both demonstrated.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sawmill import workspace


def write(folder, name, value):
    path = folder / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def main():
    folder = Path(tempfile.mkdtemp(prefix="return-demo-"))
    ws = folder / "ws"
    workspace.init_workspace(str(ws), write(folder, "stock.json", {
        "stock": [{"id": "R1", "material": "oak", "length_mm": 11,
                   "trim_head_mm": 0, "trim_tail_mm": 0}]}))
    workspace.register_price(str(ws), write(folder, "price1.json", {
        "price_id": "P1", "stock_id": "R1", "amount_cents": 1,
        "expected_price_revision": 0}), str(folder / "price1-result.json"))
    salvage_req = {
        "job_id": "j1", "kerf_mm": 1, "min_offcut_mm": 2,
        "candidate_entry_ids": ["stock/R1"],
        "orders": [{"id": "A", "material": "oak", "piece_mm": 4, "quantity": 1},
                   {"id": "B", "material": "oak", "piece_mm": 5, "quantity": 1}]}
    salvage_plan = workspace.salvage_plan(
        str(ws), write(folder, "salvage.json", salvage_req),
        str(folder / "salvage-plan.json"))
    workspace.salvage_commit(str(ws), str(folder / "salvage-plan.json"),
                             str(folder / "salvage-result.json"))
    print("ticket partitions:",
          [(p["kind"], p["start_mm"], p["end_mm"])
           for p in salvage_plan["tickets"][0]["partitions"]])

    b_ref = {"job_id": "j1", "entry_id": "stock/R1", "order_id": "B", "unit": 1}
    delivery_req = {
        "dispatch_id": "D1", "candidate_pieces": [b_ref],
        "kits": [{"kit_id": "K1", "priority": 0, "slots": [
            {"slot_id": "s1", "material": "oak", "min_mm": 5, "max_mm": 5,
             "grain_group": "G"}]}]}
    workspace.delivery_plan(str(ws), write(folder, "delivery.json", delivery_req),
                            str(folder / "delivery-plan.json"))
    workspace.delivery_commit(str(ws), str(folder / "delivery-plan.json"),
                              str(folder / "delivery-result.json"))

    c0 = workspace.close_run(str(ws), write(folder, "c0.json", {
        "close_id": "C0", "previous_close_id": None,
        "expected_inventory_revision": 1, "expected_price_revision": 1}),
        str(folder / "c0-result.json"))
    print("C0 piece amounts:",
          {p["order_id"]: p["amount_cents"] for p in c0["detail"]["pieces"]})

    state = workspace._read_state(str(ws))
    return_req = {
        "return_id": "RT1",
        "expected_inventory_revision": state["revision"],
        "expected_finished_revision": state["finished_revision"],
        "items": [{"dispatch_id": "D1", "kit_id": "K1", "slot_id": "s1",
                   "piece": b_ref}]}
    receipt = workspace.register_return(
        str(ws), write(folder, "return.json", return_req),
        str(folder / "return-result.json"))
    item = receipt["items"][0]
    print("rework identity:", item["return_entry_id"],
          "length_mm:", item["length_mm"],
          "root span:", (item["root_start_mm"], item["root_end_mm"]))

    state = workspace._read_state(str(ws))
    c1 = workspace.close_run(str(ws), write(folder, "c1.json", {
        "close_id": "C1", "previous_close_id": "C0",
        "expected_inventory_revision": state["revision"],
        "expected_price_revision": 1}), str(folder / "c1-result.json"))
    for line in c1["bridge"]["lines"]:
        if line["kind"] in ("piece", "inventory"):
            print("C1 bridge:", line["line_id"],
                  (line["opening_cents"], line["price_change_cents"],
                   line["production_cents"], line["closing_cents"]))

    workspace.register_price(str(ws), write(folder, "price2.json", {
        "price_id": "P2", "stock_id": "R1", "amount_cents": 3,
        "expected_price_revision": 1}), str(folder / "price2-result.json"))
    recut_req = {"job_id": "j2", "kerf_mm": 1, "min_offcut_mm": 2,
                 "orders": [{"id": "C", "material": "oak",
                             "piece_mm": 4, "quantity": 1}]}
    recut_plan = workspace.plan(
        str(ws), write(folder, "recut.json", recut_req),
        str(folder / "recut-plan.json"))
    workspace.commit(str(ws), str(folder / "recut-plan.json"),
                     str(folder / "recut-result.json"))
    cut = recut_plan["tickets"][0]["cuts"][0]
    root_offset = workspace.root_offset_of(
        workspace._read_state(str(ws)), item["return_entry_id"])
    print("new piece root span:",
          (root_offset + cut["start_mm"], root_offset + cut["end_mm"]))

    state = workspace._read_state(str(ws))
    c2 = workspace.close_run(str(ws), write(folder, "c2.json", {
        "close_id": "C2", "previous_close_id": "C1",
        "expected_inventory_revision": state["revision"],
        "expected_price_revision": 2}), str(folder / "c2-result.json"))
    for line in c2["bridge"]["lines"]:
        if line["kind"] in ("piece", "inventory"):
            print("C2 bridge:", line["line_id"],
                  (line["opening_cents"], line["price_change_cents"],
                   line["production_cents"], line["closing_cents"]))
    print("frozen C0/C1 unchanged:",
          workspace.close_status(str(ws), "C0") == c0,
          workspace.close_status(str(ws), "C1") == c1)
    print("demo workspace:", ws)


if __name__ == "__main__":
    main()
