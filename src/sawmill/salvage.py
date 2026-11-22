"""Salvage cutting for stock entries carrying local reject zones.

A salvage request works on a fixed, bounded list of candidate inventory
entries. Each candidate's processable area is the usable span (its full
length minus the existing head/tail trims) with its current reject zones
removed; every remaining maximal clean span is a *lane*. An order piece must
fit whole inside one same-material lane: pieces never span a reject zone,
never splice lanes together, and are never re-cut. Inside one lane the
selected pieces, ordered by ``(order_id, unit)``, are processed from the
left end with no internal gap and each piece -- including the last -- is
followed by the request kerf.

The assignment is the single global optimum under five lexicographic
levels: most delivered pieces; then greatest delivered length; then least
irrecoverable loss millimetres; then least full length of consumed parents;
then the lexicographically smallest canonical placement sequence. Every
candidate that processes at least one piece is consumed as a whole: head
and tail trims, every reject zone, all kerfs and every below-threshold lane
end become loss, while every positive at-threshold remainder becomes its
own clean offcut. A candidate that processes nothing is never consumed.

This module is pure: workspace state access, identity minting, root
coordinate recovery and persistence live in ``workspace.py``.
"""
from .io import fingerprint

SCHEMA = 1

MAX_CANDIDATES = 4
MAX_LANES_TOTAL = 8
MAX_PIECES_TOTAL = 12

# Placement tuple tags used in the canonical placement sequence.
PLACED_TAG = 0
UNFILLED_TAG = 1


def clean_lanes(entry, reject_zones):
    """Maximal clean lanes of one entry after trims and reject zones.

    Zones are already normalized and guaranteed inside the trimmed usable
    span. Lanes are returned left to right with positive length only.
    """
    head = entry["trim_head_mm"]
    tail_start = entry["length_mm"] - entry["trim_tail_mm"]
    lanes = []
    cursor = head
    for zone in reject_zones:
        if zone["start_mm"] > cursor:
            lanes.append({"start_mm": cursor, "end_mm": zone["start_mm"]})
        cursor = zone["end_mm"]
    if cursor < tail_start:
        lanes.append({"start_mm": cursor, "end_mm": tail_start})
    return lanes


def expand_demands(orders):
    """Every demanded unit as ``(order_id, unit, material, piece_mm)``, sorted."""
    demands = []
    for order_id, row in orders.items():
        for unit in range(1, row["quantity"] + 1):
            demands.append((order_id, unit, row["material"], row["piece_mm"]))
    demands.sort(key=lambda item: (item[0], item[1]))
    return demands


def normalize_request(job_id, orders, candidate_entry_ids, kerf_mm, min_offcut_mm):
    """Canonical request identity; input array order carries no meaning."""
    return {
        "job_id": job_id,
        "kerf_mm": kerf_mm,
        "min_offcut_mm": min_offcut_mm,
        "candidate_entry_ids": list(candidate_entry_ids),
        "orders": [
            {"id": row["id"], "material": row["material"],
             "piece_mm": row["piece_mm"], "quantity": row["quantity"]}
            for row in sorted(orders.values(), key=lambda row: row["id"])],
    }


class _Search:
    """Exact branch-and-bound search over lane assignments of the demands."""

    def __init__(self, demands, lanes, lane_entries, kerf, min_offcut):
        self.demands = demands
        self.lanes = lanes
        self.lane_entries = lane_entries
        self.kerf = kerf
        self.min_offcut = min_offcut
        # assignments[i] = lane index hosting demand i, or None when unfilled.
        self.assignments = [None] * len(demands)
        # cursor[lane] = next free local coordinate inside that lane.
        self.cursor = [lane["start_mm"] for lane in lanes]
        # start[demand] = local start coordinate of a placed demand.
        self.starts = [None] * len(demands)
        self.pieces = 0
        self.delivered_length = 0
        self.best_key = None
        self.best_assignment = None
        self.best_starts = None

    def _loss_and_consumed(self):
        """Irrecoverable loss and consumed parent length of the assignment."""
        kerf_loss = self.pieces * self.kerf
        lane_tail_loss = 0
        used_entries = set()
        for lane_index, lane in enumerate(self.lanes):
            if self.cursor[lane_index] != lane["start_mm"]:
                used_entries.add(lane["entry_index"])
        # A parent is consumed when any lane is processed; every lane of such a
        # parent is then settled -- processed lanes use their post-cut cursor,
        # untouched lanes leave their full span as the recoverable/scrap tail.
        for lane_index, lane in enumerate(self.lanes):
            if lane["entry_index"] not in used_entries:
                continue
            if self.cursor[lane_index] != lane["start_mm"]:
                remainder = lane["end_mm"] - self.cursor[lane_index]
            else:
                remainder = lane["end_mm"] - lane["start_mm"]
            if 0 < remainder < self.min_offcut:
                lane_tail_loss += remainder
        trim_loss = 0
        reject_loss = 0
        for entry_index in used_entries:
            entry = self.lane_entries[entry_index]
            trim_loss += entry["trim_head_mm"] + entry["trim_tail_mm"]
            for zone in entry["reject_zones"]:
                reject_loss += zone["end_mm"] - zone["start_mm"]
        unrecoverable_loss = trim_loss + reject_loss + kerf_loss + lane_tail_loss
        consumed_length = sum(self.lane_entries[i]["length_mm"]
                              for i in used_entries)
        return unrecoverable_loss, consumed_length

    def run(self):
        self._search(0)
        return self.best_key, self.best_assignment, tuple(self.best_starts)

    def _search(self, index):
        if index == len(self.demands):
            loss, consumed = self._loss_and_consumed()
            sequence = tuple(
                (UNFILLED_TAG, "", 0) if lane_index is None
                else (PLACED_TAG, self.lanes[lane_index]["entry_id"],
                      self.starts[demand_index])
                for demand_index, lane_index in enumerate(self.assignments))
            # The first four levels maximize; the final sequence minimizes.
            high = (self.pieces, self.delivered_length, -loss, -consumed)
            if (self.best_key is None
                    or high > self.best_key[0]
                    or (high == self.best_key[0]
                        and sequence < self.best_key[1])):
                self.best_key = (high, sequence)
                self.best_assignment = tuple(self.assignments)
                self.best_starts = tuple(self.starts)
            return
        # Even filling every remaining demand cannot match the incumbent count.
        if self.best_key is not None and \
                self.pieces + (len(self.demands) - index) < self.best_key[0][0]:
            return
        _, _, material, piece_mm = self.demands[index]
        # Placements come before unfilled in the canonical tuple ordering
        # (0,...) < (1,"",0); try lanes in increasing index so the first
        # complete solution at equal higher levels has the smallest sequence.
        for lane_index, lane in enumerate(self.lanes):
            if lane["material"] != material:
                continue
            needed = piece_mm + self.kerf
            if lane["end_mm"] - self.cursor[lane_index] < needed:
                continue
            start = self.cursor[lane_index]
            self.assignments[index] = lane_index
            self.starts[index] = start
            self.cursor[lane_index] = start + needed
            self.pieces += 1
            self.delivered_length += piece_mm
            self._search(index + 1)
            self.pieces -= 1
            self.delivered_length -= piece_mm
            self.cursor[lane_index] = start
            self.starts[index] = None
            self.assignments[index] = None
        # Leave this demand unfilled.
        self._search(index + 1)


def _lane_view(entry, reject_zones, entry_index, root_offset):
    """Lanes of one candidate enriched with material and root coordinates.

    ``root_offset`` is the coordinate of the candidate entry's local zero
    inside its initial root stock; it is added to every local lane coordinate,
    including when the candidate is a multi-generation descendant whose local
    zero does not coincide with the root (legacy offcuts, salvage branches,
    customer-return rework stock).
    """
    return [{
        "entry_index": entry_index,
        "entry_id": entry["entry_id"],
        "material": entry["material"],
        "start_mm": lane["start_mm"],
        "end_mm": lane["end_mm"],
        "length_mm": lane["end_mm"] - lane["start_mm"],
        "root_start_mm": root_offset + lane["start_mm"],
        "root_end_mm": root_offset + lane["end_mm"],
    } for lane in clean_lanes(entry, reject_zones)]


def build_plan(workspace_id, revision, job_id, candidates, inspections, orders,
               kerf_mm, min_offcut_mm, root_offsets):
    """Pure salvage preview against one consistent workspace snapshot.

    ``candidates`` is the ordered list of validated, available, non-isolated
    entry dicts; ``inspections`` maps entry id to its current inspection
    record (or None/absent for no local defects); ``root_offsets`` maps entry
    id to the coordinate of its local zero inside its root stock.
    """
    demands = expand_demands(orders)
    if len(demands) > MAX_PIECES_TOTAL:
        raise ValueError(f"require at most {MAX_PIECES_TOTAL} demanded pieces")
    lane_entries = []
    lanes = []
    candidate_views = []
    for entry_index, entry in enumerate(candidates):
        record = inspections.get(entry["entry_id"])
        reject_zones = record["reject_zones"] if record else []
        inspection_id = record["inspection_id"] if record else None
        entry_root_offset = root_offsets[entry["entry_id"]]
        entry_lanes = _lane_view(entry, reject_zones, entry_index,
                                 entry_root_offset)
        lanes.extend(entry_lanes)
        lane_entries.append({
            "entry_index": entry_index,
            "entry_id": entry["entry_id"],
            "length_mm": entry["length_mm"],
            "trim_head_mm": entry["trim_head_mm"],
            "trim_tail_mm": entry["trim_tail_mm"],
            "reject_zones": reject_zones,
        })
        candidate_views.append({
            "entry_id": entry["entry_id"],
            "kind": entry["kind"],
            "material": entry["material"],
            "length_mm": entry["length_mm"],
            "trim_head_mm": entry["trim_head_mm"],
            "trim_tail_mm": entry["trim_tail_mm"],
            "root_stock_id": entry["root_stock_id"],
            "inspection_id": inspection_id,
            "reject_zones": reject_zones,
            "lanes": entry_lanes,
        })
    if len(lanes) > MAX_LANES_TOTAL:
        raise ValueError(
            f"require at most {MAX_LANES_TOTAL} non-empty lanes across candidates")

    (high_key, sequence), assignment, starts = _Search(
        demands, lanes, lane_entries, kerf_mm, min_offcut_mm).run()
    pieces_count, delivered_length, neg_loss, neg_consumed = high_key
    unrecoverable_loss = -neg_loss
    consumed_length = -neg_consumed

    assignment_by_lane = {}
    for demand_index, lane_index in enumerate(assignment):
        if lane_index is not None:
            assignment_by_lane.setdefault(lane_index, []).append(demand_index)

    unfilled = []
    placement_sequence = []
    for demand_index, (order_id, unit, material, piece_mm) in enumerate(demands):
        lane_index = assignment[demand_index]
        if lane_index is None:
            unfilled.append({"order_id": order_id, "unit": unit,
                             "length_mm": piece_mm, "material": material})
            placement_sequence.append({"tag": UNFILLED_TAG, "entry_id": "",
                                       "start_mm": 0})
        else:
            placement_sequence.append({
                "tag": PLACED_TAG,
                "entry_id": lanes[lane_index]["entry_id"],
                "start_mm": starts[demand_index]})

    tickets = []
    consumed_entry_ids = []
    added_offcuts = []
    used_entry_indexes = sorted({lanes[lane_index]["entry_index"]
                                 for lane_index in assignment_by_lane})
    offcut_seq = 0
    for entry_index in used_entry_indexes:
        entry = candidates[entry_index]
        root_offset = root_offsets[entry["entry_id"]]
        entry_lane_indexes = [i for i, lane in enumerate(lanes)
                              if lane["entry_index"] == entry_index]
        cuts = []
        lane_reports = []
        partitions = []

        def partition(kind, start_mm, end_mm, **extra):
            if end_mm <= start_mm:
                return None
            row = {"kind": kind, "start_mm": start_mm, "end_mm": end_mm,
                   "length_mm": end_mm - start_mm,
                   "root_start_mm": root_offset + start_mm,
                   "root_end_mm": root_offset + end_mm, **extra}
            partitions.append(row)
            return row

        partition("trim_head", 0, entry["trim_head_mm"])
        events = [("zone", zone["start_mm"], zone)
                  for zone in candidate_views[entry_index]["reject_zones"]]
        events += [("lane", lanes[lane_index]["start_mm"], lane_index)
                   for lane_index in entry_lane_indexes]
        events.sort(key=lambda event: event[1])
        for event_kind, _, payload in events:
            if event_kind == "zone":
                partition("defect_reject", payload["start_mm"], payload["end_mm"])
                continue
            lane_index = payload
            lane = lanes[lane_index]
            hosted = sorted(assignment_by_lane.get(lane_index, []),
                            key=lambda i: (demands[i][0], demands[i][1]))
            lane_cursor = lane["start_mm"]
            piece_report = []
            for demand_index in hosted:
                order_id, unit, _, piece_mm = demands[demand_index]
                end = lane_cursor + piece_mm
                kerf_end = end + kerf_mm
                cuts.append({"order_id": order_id, "unit": unit,
                             "start_mm": lane_cursor, "end_mm": end,
                             "kerf_end_mm": kerf_end})
                partition("piece", lane_cursor, end, order_id=order_id, unit=unit)
                partition("kerf", end, kerf_end)
                piece_report.append({
                    "order_id": order_id, "unit": unit,
                    "start_mm": lane_cursor, "end_mm": end,
                    "kerf_end_mm": kerf_end,
                    "root_start_mm": root_offset + lane_cursor,
                    "root_end_mm": root_offset + end,
                    "root_kerf_end_mm": root_offset + kerf_end})
                lane_cursor = kerf_end
            remainder = lane["end_mm"] - lane_cursor
            reusable = remainder > 0 and remainder >= min_offcut_mm
            if remainder > 0:
                row = partition("offcut", lane_cursor, lane["end_mm"],
                                reusable=reusable)
                if reusable:
                    offcut_seq += 1
                    offcut_id = (f"offcut/{job_id}/{entry['entry_id']}"
                                 f"#{offcut_seq}")
                    row["entry_id"] = offcut_id
                    added_offcuts.append({
                        "entry_id": offcut_id,
                        "parent_entry_id": entry["entry_id"],
                        "job_id": job_id,
                        "start_mm": lane_cursor, "end_mm": lane["end_mm"],
                        "length_mm": remainder,
                        "root_offset_mm": root_offset + lane_cursor,
                        "root_start_mm": root_offset + lane_cursor,
                        "root_end_mm": root_offset + lane["end_mm"]})
                else:
                    row["scrap_category"] = "offcut_scrap"
            lane_reports.append({
                "lane_index": lane_index,
                "start_mm": lane["start_mm"], "end_mm": lane["end_mm"],
                "length_mm": lane["length_mm"],
                "root_start_mm": lane["root_start_mm"],
                "root_end_mm": lane["root_end_mm"],
                "pieces": piece_report,
                "remainder_mm": remainder,
                "remainder_reusable": reusable,
                "processed": bool(hosted)})
        partition("trim_tail",
                  entry["length_mm"] - entry["trim_tail_mm"], entry["length_mm"])
        tickets.append({
            "stock_id": entry["entry_id"],
            "entry_id": entry["entry_id"],
            "entry_kind": entry["kind"],
            "material": entry["material"],
            "stock_length_mm": entry["length_mm"],
            "trim_head_mm": entry["trim_head_mm"],
            "trim_tail_mm": entry["trim_tail_mm"],
            "cuts": cuts,
            "salvage": True,
            "reject_zones": candidate_views[entry_index]["reject_zones"],
            "lanes": lane_reports,
            "partitions": partitions,
            # Absolute coordinate of the entry's local zero inside its root;
            # salvage may consume initial stock (offset 0) or any descendant
            # such as a multi-generation offcut or a customer-return rework
            # identity, whose offsets are never zero.
            "root_offset_mm": root_offsets[entry["entry_id"]],
        })
        consumed_entry_ids.append(entry["entry_id"])

    request = normalize_request(
        job_id, orders, [entry["entry_id"] for entry in candidates],
        kerf_mm, min_offcut_mm)
    return {
        "schema_version": SCHEMA,
        "kind": "stock-workspace-salvage-plan",
        "workspace_id": workspace_id,
        "base_revision": revision,
        "job_id": job_id,
        "request": request,
        "request_sha256": fingerprint(request),
        "candidates": candidate_views,
        "tickets": tickets,
        "unfilled": unfilled,
        "placement_sequence": placement_sequence,
        "consumed_entry_ids": sorted(consumed_entry_ids),
        "unused_candidate_entry_ids": [
            entry["entry_id"] for entry in candidates
            if entry["entry_id"] not in set(consumed_entry_ids)],
        "added_offcuts": added_offcuts,
        "metrics": {
            "delivered_pieces": pieces_count,
            "delivered_length_mm": delivered_length,
            "unrecoverable_loss_mm": unrecoverable_loss,
            "consumed_parent_length_mm": consumed_length,
            "kerf_mm_total": pieces_count * kerf_mm,
        },
    }
