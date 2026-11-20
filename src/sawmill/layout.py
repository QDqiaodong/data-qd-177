"""Deterministic best-fit decreasing; this is a feasible heuristic, not an optimizer."""


def compile_tickets(stock, orders, kerf, min_offcut):
    available = {key: row["length_mm"] - row["trim_head_mm"] - row["trim_tail_mm"]
                 for key, row in stock.items()}
    pieces = [(row["piece_mm"], order_id, unit, row["material"])
              for order_id, row in orders.items() for unit in range(1, row["quantity"] + 1)]
    pieces.sort(key=lambda x: (-x[0], x[1], x[2]))
    allocations, unmet = {}, []
    for length, order_id, unit, material in pieces:
        fits = [(available[key] - length - kerf, key) for key, row in stock.items()
                if row["material"] == material and available[key] >= length + kerf]
        if not fits:
            unmet.append({"order_id": order_id, "unit": unit, "length_mm": length})
            continue
        _, stock_id = min(fits)
        available[stock_id] -= length + kerf
        allocations.setdefault(stock_id, []).append((order_id, unit, length))
    tickets = []
    for stock_id, assigned in sorted(allocations.items()):
        row = stock[stock_id]
        cursor = row["trim_head_mm"]
        cuts = []
        for order_id, unit, length in assigned:
            cuts.append({"order_id": order_id, "unit": unit, "start_mm": cursor,
                         "end_mm": cursor + length, "kerf_end_mm": cursor + length + kerf})
            cursor += length + kerf
        remainder = row["length_mm"] - row["trim_tail_mm"] - cursor
        tickets.append({"stock_id": stock_id, "material": row["material"],
                        "stock_length_mm": row["length_mm"],
                        "trim_head_mm": row["trim_head_mm"], "trim_tail_mm": row["trim_tail_mm"],
                        "cuts": cuts, "offcut_start_mm": cursor, "offcut_length_mm": remainder,
                        "offcut_reusable": remainder >= min_offcut and remainder > 0})
    return tickets, unmet
