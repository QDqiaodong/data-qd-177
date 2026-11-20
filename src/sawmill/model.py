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
