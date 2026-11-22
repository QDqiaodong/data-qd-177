from .io import integer, text, unique


def validate_stock_rows(data):
    """Initial stock list for an inventory workspace; same row contract as validate."""
    stock = unique(data["stock"], "stock")
    if not stock or len(stock) > 1000:
        raise ValueError("require 1..1000 stock rows")
    for row in stock.values():
        text(row["material"], "material")
        length = integer(row["length_mm"], "length_mm", 1)
        head = integer(row["trim_head_mm"], "trim_head_mm")
        tail = integer(row["trim_tail_mm"], "trim_tail_mm")
        if head + tail >= length:
            raise ValueError("trims must leave usable stock")
    return stock


def validate_batch(data):
    """Workspace batch request: existing orders/kerf_mm/min_offcut_mm rules plus job_id."""
    job_id = text(data["job_id"], "job_id")
    orders = unique(data["orders"], "order")
    if len(orders) > 1000:
        raise ValueError("require at most 1000 orders")
    integer(data["kerf_mm"], "kerf_mm")
    integer(data["min_offcut_mm"], "min_offcut_mm")
    total = 0
    for row in orders.values():
        text(row["material"], "material")
        integer(row["piece_mm"], "piece_mm", 1)
        total += integer(row["quantity"], "quantity", 1)
    if total > 10000:
        raise ValueError("maximum 10000 demanded pieces")
    return job_id, orders


def validate_price(data):
    """Cost registration: stable id, one initial root, replacement total cost."""
    price_id = text(data["price_id"], "price_id")
    stock_id = text(data["stock_id"], "stock_id")
    integer(data["amount_cents"], "amount_cents")
    integer(data["expected_price_revision"], "expected_price_revision")
    return price_id, stock_id, data["amount_cents"], data["expected_price_revision"]


def validate_close(data):
    """Close request: stable id, chain predecessor, and both expected revisions."""
    close_id = text(data["close_id"], "close_id")
    previous = data["previous_close_id"]
    if previous is not None:
        text(previous, "previous_close_id")
    integer(data["expected_inventory_revision"], "expected_inventory_revision")
    integer(data["expected_price_revision"], "expected_price_revision")
    return (close_id, previous, data["expected_inventory_revision"],
            data["expected_price_revision"])


def validate_incident(data):
    """Quality incident registration: stable id plus one or more initial stock roots."""
    incident_id = text(data["incident_id"], "incident_id")
    roots = data["stock_ids"]
    if not isinstance(roots, list):
        raise ValueError("stock_ids must be an array")
    if not roots:
        raise ValueError("require at least one stock_id")
    if len(roots) > 1000:
        raise ValueError("require at most 1000 stock_ids")
    seen = set()
    for root in roots:
        text(root, "stock_id")
        if root in seen:
            raise ValueError(f"duplicate stock_id: {root}")
        seen.add(root)
    return incident_id, sorted(seen)


def merge_reject_zones(zones):
    """Union of half-open intervals; overlapping and touching intervals join.

    Accepts ``(start_mm, end_mm)`` pairs in any order and returns merged
    ``{"start_mm", "end_mm"}`` rows sorted by physical start.
    """
    ordered = sorted(zones)
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [{"start_mm": start, "end_mm": end} for start, end in merged]


def validate_inspection(data):
    """Local inspection registration: stable id, one entry, revision, zones."""
    inspection_id = text(data["inspection_id"], "inspection_id")
    entry_id = text(data["entry_id"], "entry_id")
    expected = integer(data["expected_inventory_revision"],
                       "expected_inventory_revision")
    raw_zones = data["reject_zones"]
    if not isinstance(raw_zones, list):
        raise ValueError("reject_zones must be an array")
    if len(raw_zones) > 32:
        raise ValueError("require at most 32 reject zones")
    zones = []
    for row in raw_zones:
        if not isinstance(row, dict):
            raise ValueError("reject_zones rows must be objects")
        start = integer(row["start_mm"], "start_mm")
        end = integer(row["end_mm"], "end_mm")
        if start >= end:
            raise ValueError("reject zone must satisfy start_mm < end_mm")
        zones.append((start, end))
    return inspection_id, entry_id, expected, zones


def validate_piece_ref(row, label="piece"):
    """The shared four-field physical piece reference object."""
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    return {
        "job_id": text(row["job_id"], f"{label}.job_id"),
        "entry_id": text(row["entry_id"], f"{label}.entry_id"),
        "order_id": text(row["order_id"], f"{label}.order_id"),
        "unit": integer(row["unit"], f"{label}.unit", 1),
    }


def validate_return(data):
    """Customer return registration: shape and intra-request uniqueness only.

    Existence, delivery occupation and revision checks need workspace state and
    live in ``workspace.py``. Returns the canonical ``(return_id, expected
    inventory revision, expected finished revision, items)``; item order carries
    no meaning and is normalized by the caller into the canonical request.
    """
    return_id = text(data.get("return_id"), "return_id")
    expected_inventory = integer(data.get("expected_inventory_revision"),
                                 "expected_inventory_revision")
    expected_finished = integer(data.get("expected_finished_revision"),
                                "expected_finished_revision")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("items must be an array")
    if not 1 <= len(raw_items) <= 32:
        raise ValueError("require 1..32 return items")
    items = []
    slots = set()
    pieces = set()
    for row in raw_items:
        if not isinstance(row, dict):
            raise ValueError("items rows must be objects")
        dispatch_id = text(row.get("dispatch_id"), "dispatch_id")
        kit_id = text(row.get("kit_id"), "kit_id")
        slot_id = text(row.get("slot_id"), "slot_id")
        slot_key = (dispatch_id, kit_id, slot_id)
        if slot_key in slots:
            raise ValueError(f"duplicate slot in return request: {slot_key}")
        slots.add(slot_key)
        piece = validate_piece_ref(row.get("piece"))
        piece_key = (piece["job_id"], piece["entry_id"],
                     piece["order_id"], piece["unit"])
        if piece_key in pieces:
            raise ValueError(f"duplicate piece in return request: {piece}")
        pieces.add(piece_key)
        items.append({"dispatch_id": dispatch_id, "kit_id": kit_id,
                      "slot_id": slot_id, "piece": piece})
    return return_id, expected_inventory, expected_finished, items


def validate_salvage(data):
    """Salvage request: batch fields plus a bounded candidate entry list."""
    job_id, orders = validate_batch(data)
    candidates = data["candidate_entry_ids"]
    if not isinstance(candidates, list):
        raise ValueError("candidate_entry_ids must be an array")
    if not 1 <= len(candidates) <= 4:
        raise ValueError("require 1..4 candidate_entry_ids")
    seen = set()
    for entry_id in candidates:
        text(entry_id, "candidate_entry_ids")
        if entry_id in seen:
            raise ValueError(f"duplicate candidate_entry_ids: {entry_id}")
        seen.add(entry_id)
    return job_id, orders, sorted(seen)


def validate(data):
    stock = unique(data["stock"], "stock")
    orders = unique(data["orders"], "order")
    if not stock or len(stock) > 1000 or len(orders) > 1000:
        raise ValueError("require 1..1000 stock rows and at most 1000 orders")
    integer(data["kerf_mm"], "kerf_mm")
    integer(data["min_offcut_mm"], "min_offcut_mm")
    for row in stock.values():
        text(row["material"], "material")
        length = integer(row["length_mm"], "length_mm", 1)
        head = integer(row["trim_head_mm"], "trim_head_mm")
        tail = integer(row["trim_tail_mm"], "trim_tail_mm")
        if head + tail >= length:
            raise ValueError("trims must leave usable stock")
    total = 0
    for row in orders.values():
        text(row["material"], "material")
        integer(row["piece_mm"], "piece_mm", 1)
        total += integer(row["quantity"], "quantity", 1)
    if total > 10000:
        raise ValueError("maximum 10000 demanded pieces")
    return stock, orders
