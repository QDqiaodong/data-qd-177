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
