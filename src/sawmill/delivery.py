"""Finished-goods kitting: request validation, normalization, and the solver.

A delivery request names real committed cut pieces as candidates and a set of
kits to fill. Each kit slot takes exactly one candidate of the same material
whose length falls inside the slot's closed window; slots of one kit sharing a
grain group must come from the same initial stock root (multi-generation
offcut pieces still count by their root). A kit is delivered whole or not at
all, and no piece is used twice.

The assignment is the single global optimum under four lexicographic levels:
most complete kits first; then the best 0/1 selection vector with kits ordered
by ``(priority, kit_id)``; then the least total ``length_mm - min_mm`` excess;
then the smallest piece-reference sequence with selected kits in
``(priority, kit_id)`` order and slots in ``slot_id`` order. References
compare field by field -- three strings, then the integer ``unit`` (never
stringified). Input array order carries no meaning: candidates, kits, and
slots are normalized into a canonical order before solving, so permuting the
request arrays yields the identical plan.

This module is pure: workspace state access and persistence live in
``workspace.py``.
"""
import itertools

from .io import integer, text, fingerprint

SCHEMA = 1

MAX_CANDIDATES = 32
MAX_KITS = 8
MAX_SLOTS_PER_KIT = 4
MAX_SLOTS_TOTAL = 24


def _ref(piece):
    """The identity tuple of a piece reference; unit stays an integer."""
    return (piece["job_id"], piece["entry_id"], piece["order_id"], piece["unit"])


def _ref_dict(piece):
    return {"job_id": piece["job_id"], "entry_id": piece["entry_id"],
            "order_id": piece["order_id"], "unit": piece["unit"]}


def _public_piece(piece):
    """A piece reference plus the facts delivery reports carry."""
    return {"job_id": piece["job_id"], "entry_id": piece["entry_id"],
            "order_id": piece["order_id"], "unit": piece["unit"],
            "material": piece["material"], "length_mm": piece["length_mm"],
            "root_stock_id": piece["root_stock_id"]}


def validate(data):
    """Delivery request: dispatch id, candidate piece refs, and kits.

    Returns ``(dispatch_id, candidates, kits)`` in canonical order: candidates
    sorted by reference, kits by ``(priority, kit_id)``, slots by ``slot_id``.
    """
    dispatch_id = text(data["dispatch_id"], "dispatch_id")
    raw_candidates = data["candidate_pieces"]
    if not isinstance(raw_candidates, list):
        raise ValueError("candidate_pieces must be an array")
    if not 1 <= len(raw_candidates) <= MAX_CANDIDATES:
        raise ValueError(f"require 1..{MAX_CANDIDATES} candidate pieces")
    candidates = []
    seen = set()
    for row in raw_candidates:
        if not isinstance(row, dict):
            raise ValueError("candidate_pieces rows must be objects")
        candidate = {"job_id": text(row["job_id"], "job_id"),
                     "entry_id": text(row["entry_id"], "entry_id"),
                     "order_id": text(row["order_id"], "order_id"),
                     "unit": integer(row["unit"], "unit", 1)}
        ref = _ref(candidate)
        if ref in seen:
            raise ValueError(f"duplicate candidate piece: {candidate}")
        seen.add(ref)
        candidates.append(candidate)
    candidates.sort(key=_ref)
    raw_kits = data["kits"]
    if not isinstance(raw_kits, list):
        raise ValueError("kits must be an array")
    if not 1 <= len(raw_kits) <= MAX_KITS:
        raise ValueError(f"require 1..{MAX_KITS} kits")
    kits = []
    kit_ids = set()
    total_slots = 0
    for row in raw_kits:
        if not isinstance(row, dict):
            raise ValueError("kits rows must be objects")
        kit_id = text(row["kit_id"], "kit_id")
        if kit_id in kit_ids:
            raise ValueError(f"duplicate kit_id: {kit_id}")
        kit_ids.add(kit_id)
        priority = integer(row["priority"], "priority")
        raw_slots = row["slots"]
        if not isinstance(raw_slots, list):
            raise ValueError("slots must be an array")
        if not 1 <= len(raw_slots) <= MAX_SLOTS_PER_KIT:
            raise ValueError(f"require 1..{MAX_SLOTS_PER_KIT} slots per kit")
        total_slots += len(raw_slots)
        slots = []
        slot_ids = set()
        for slot_row in raw_slots:
            if not isinstance(slot_row, dict):
                raise ValueError("slots rows must be objects")
            slot_id = text(slot_row["slot_id"], "slot_id")
            if slot_id in slot_ids:
                raise ValueError(f"duplicate slot_id in kit {kit_id}: {slot_id}")
            slot_ids.add(slot_id)
            min_mm = integer(slot_row["min_mm"], "min_mm", 1)
            max_mm = integer(slot_row["max_mm"], "max_mm", 1)
            if min_mm > max_mm:
                raise ValueError("min_mm must not exceed max_mm")
            slots.append({"slot_id": slot_id,
                          "material": text(slot_row["material"], "material"),
                          "min_mm": min_mm, "max_mm": max_mm,
                          "grain_group": text(slot_row["grain_group"], "grain_group")})
        slots.sort(key=lambda slot: slot["slot_id"])
        kits.append({"kit_id": kit_id, "priority": priority, "slots": slots})
    if total_slots > MAX_SLOTS_TOTAL:
        raise ValueError(f"require at most {MAX_SLOTS_TOTAL} slots in total")
    kits.sort(key=lambda kit: (kit["priority"], kit["kit_id"]))
    return dispatch_id, candidates, kits


def normalize_request(dispatch_id, candidates, kits):
    """Canonical request identity; already-normalized input is a fixed point."""
    return {"dispatch_id": dispatch_id,
            "candidate_pieces": [dict(candidate) for candidate in candidates],
            "kits": [{"kit_id": kit["kit_id"], "priority": kit["priority"],
                      "slots": [dict(slot) for slot in kit["slots"]]}
                     for kit in kits]}


# ---------------------------------------------------------------------------
# Exact global solver
# ---------------------------------------------------------------------------


def _slot_entries(kits, eligible):
    """All slots of the subset in canonical order, with compatible pieces.

    ``kits`` are in ``(priority, kit_id)`` order and each kit's slots in
    ``slot_id`` order, so the flat list is exactly the level-4 sequence order.
    ``eligible`` is sorted by reference, keeping every compat list in
    increasing reference order.
    """
    entries = []
    for kit in kits:
        for slot in kit["slots"]:
            compat = [index for index, piece in enumerate(eligible)
                      if piece["material"] == slot["material"]
                      and slot["min_mm"] <= piece["length_mm"] <= slot["max_mm"]]
            entries.append({"slot": slot, "group": (kit["kit_id"], slot["grain_group"]),
                            "compat": compat})
    return entries


def _feasible(entries, eligible):
    """Whether some assignment fills every slot; most-constrained slot first."""
    used = [False] * len(eligible)
    bound_root = {}
    order = sorted(range(len(entries)), key=lambda i: len(entries[i]["compat"]))

    def dfs(position):
        if position == len(order):
            return True
        entry = entries[order[position]]
        group = entry["group"]
        for index in entry["compat"]:
            if used[index]:
                continue
            root = eligible[index]["root_stock_id"]
            bound = bound_root.get(group)
            if bound is not None and bound != root:
                continue
            used[index] = True
            added = group not in bound_root
            if added:
                bound_root[group] = root
            if dfs(position + 1):
                return True
            used[index] = False
            if added:
                del bound_root[group]
        return False

    return dfs(0)


def _optimize(entries, eligible):
    """Best assignment for one subset: min excess, then min reference sequence.

    Returns ``(excess_mm_total, assignment)`` with ``assignment[i]`` the
    eligible-piece index for ``entries[i]``, or None when infeasible. Phase 1
    bounds the minimum achievable excess; phase 2 walks slots in canonical
    order trying pieces in increasing reference order, pruning every branch
    that cannot stay within that bound, so the first complete assignment is
    the lexicographically smallest reference sequence at minimum excess.
    """
    count = len(entries)
    lowest = []
    for entry in entries:
        if not entry["compat"]:
            return None
        lowest.append(min(eligible[index]["length_mm"] - entry["slot"]["min_mm"]
                          for index in entry["compat"]))
    suffix = [0] * (count + 1)
    for i in range(count - 1, -1, -1):
        suffix[i] = suffix[i + 1] + lowest[i]
    used = [False] * len(eligible)
    bound_root = {}
    best = [None]

    def dfs_excess(i, excess):
        if best[0] is not None and excess + suffix[i] >= best[0]:
            return
        if i == count:
            best[0] = excess
            return
        entry = entries[i]
        group = entry["group"]
        minimum = entry["slot"]["min_mm"]
        for index in entry["compat"]:
            if used[index]:
                continue
            root = eligible[index]["root_stock_id"]
            bound = bound_root.get(group)
            if bound is not None and bound != root:
                continue
            used[index] = True
            added = group not in bound_root
            if added:
                bound_root[group] = root
            dfs_excess(i + 1, excess + eligible[index]["length_mm"] - minimum)
            used[index] = False
            if added:
                del bound_root[group]

    dfs_excess(0, 0)
    if best[0] is None:
        return None
    target = best[0]
    assignment = [None] * count
    done = [False]

    def dfs_sequence(i, excess):
        if done[0] or excess + suffix[i] > target:
            return
        if i == count:
            done[0] = True
            return
        entry = entries[i]
        group = entry["group"]
        minimum = entry["slot"]["min_mm"]
        for index in entry["compat"]:
            if used[index]:
                continue
            root = eligible[index]["root_stock_id"]
            bound = bound_root.get(group)
            if bound is not None and bound != root:
                continue
            used[index] = True
            added = group not in bound_root
            if added:
                bound_root[group] = root
            assignment[i] = index
            dfs_sequence(i + 1, excess + eligible[index]["length_mm"] - minimum)
            if done[0]:
                return  # keep the winning assignment in place
            used[index] = False
            if added:
                del bound_root[group]
            assignment[i] = None

    dfs_sequence(0, 0)
    if not done[0]:
        return None
    return target, list(assignment)


def _subsets_by_vector(count, size):
    """``size``-subsets of ``range(count)``, best selection vector first.

    The vector of a subset has 1 at each included index; subsets are ordered
    so the lexicographically largest vector (1 before 0 at every position)
    comes first.
    """
    combos = itertools.combinations(range(count), size)
    return sorted(combos, key=lambda combo: tuple(0 if i in combo else 1
                                                  for i in range(count)))


def solve(eligible, kits):
    """Global optimum over all kit subsets and assignments.

    ``eligible`` are piece fact dicts, ``kits`` the normalized kits. Returns
    ``(selected, excess_mm_total)`` where ``selected`` lists ``(kit, pieces)``
    pairs in ``(priority, kit_id)`` order with one piece per slot in
    ``slot_id`` order. Zero selected kits is a valid optimum.
    """
    eligible = sorted(eligible, key=_ref)
    for size in range(len(kits), 0, -1):
        for combo in _subsets_by_vector(len(kits), size):
            subset = [kits[i] for i in combo]
            entries = _slot_entries(subset, eligible)
            if not _feasible(entries, eligible):
                continue
            outcome = _optimize(entries, eligible)
            if outcome is None:
                continue  # unreachable: feasible subsets optimize fine
            excess, assignment = outcome
            selected = []
            position = 0
            for kit in subset:
                pieces = [eligible[assignment[position + j]]
                          for j in range(len(kit["slots"]))]
                position += len(kit["slots"])
                selected.append((kit, pieces))
            return selected, excess
    return [], 0


def build_plan(workspace_id, finished_revision, catalog, delivered_by,
               covered_roots, data, returned_by=None):
    """Pure delivery preview against one consistent workspace snapshot.

    ``catalog`` maps committed piece references to their facts,
    ``delivered_by`` maps already-dispatched references to the owning
    dispatch id, and ``covered_roots`` maps stock roots to their active
    quality incident ids. Unknown candidate references fail; known but
    occupied or isolated candidates are listed as excluded, never allocated.
    A returned piece keeps its original delivery occupation forever, so it is
    excluded as delivered; ``returned_by`` additionally names the return
    receipt and rework identity when a candidate has already come back.
    """
    returned_by = returned_by or {}
    dispatch_id, candidates, kits = validate(data)
    request = normalize_request(dispatch_id, candidates, kits)
    excluded = []
    eligible = []
    for candidate in candidates:
        ref = _ref(candidate)
        facts = catalog.get(ref)
        if facts is None:
            raise ValueError(f"unknown candidate piece: {candidate}")
        owner = delivered_by.get(ref)
        return_id = returned_by.get(ref)
        incidents = list(covered_roots.get(facts["root_stock_id"], ()))
        reasons = []
        if owner is not None:
            reasons.append("delivered")
        if incidents:
            reasons.append("isolated")
        if reasons:
            row = {"piece": dict(candidate), "reasons": reasons,
                   "delivered_by": owner, "active_incidents": incidents}
            # The return fact is an extra dimension shown only when it exists,
            # so exclusion rows without a return keep their original shape.
            if return_id is not None:
                row["returned_by"] = return_id
            excluded.append(row)
        else:
            eligible.append(dict(facts))
    eligible.sort(key=_ref)
    selected, excess = solve(eligible, kits)
    used = {_ref(piece) for _, pieces in selected for piece in pieces}
    remaining = [piece for piece in eligible if _ref(piece) not in used]
    selected_ids = {kit["kit_id"] for kit, _ in selected}
    selected_kits = [
        {"kit_id": kit["kit_id"], "priority": kit["priority"],
         "slots": [{**slot, "piece": _public_piece(piece)}
                   for slot, piece in zip(kit["slots"], pieces)]}
        for kit, pieces in selected]
    return {
        "schema_version": SCHEMA,
        "kind": "stock-workspace-delivery-plan",
        "workspace_id": workspace_id,
        "base_finished_revision": finished_revision,
        "dispatch_id": dispatch_id,
        "request": request,
        "request_sha256": fingerprint(request),
        "selected_kits": selected_kits,
        "unselected_kit_ids": [kit["kit_id"] for kit in kits
                               if kit["kit_id"] not in selected_ids],
        "excluded_pieces": excluded,
        "remaining_pieces": [_public_piece(piece) for piece in remaining],
        "metrics": {
            "candidate_count": len(candidates),
            "eligible_piece_count": len(eligible),
            "excluded_piece_count": len(excluded),
            "kits_total": len(kits),
            "kits_selected": len(selected),
            "kit_order": [kit["kit_id"] for kit in kits],
            "selection_vector": [1 if kit["kit_id"] in selected_ids else 0
                                 for kit in kits],
            "excess_mm_total": excess,
            "piece_sequence": [_ref_dict(piece) for _, pieces in selected
                               for piece in pieces],
        },
    }
