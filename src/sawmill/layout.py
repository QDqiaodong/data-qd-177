"""Deterministic best-fit decreasing; this is a feasible heuristic, not an optimizer."""


def compile_tickets(stock, orders, kerf, min_offcut, prefer_longest_ids=None):
    """Allocate pieces to stock with the fixed best-fit decreasing rule.

    ``prefer_longest_ids`` names salvage-generated clean offcuts. When every
    entry fitting one piece is one of those new-pool identities, the longest
    fitting entry is chosen instead of the shortest, preserving shorter clean
    branches for later smaller pieces; any mix with legacy entries keeps the
    original best-fit rule exactly, so legacy plans are byte-identical.
    """
    prefer_longest_ids = prefer_longest_ids or frozenset()
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
        if fits and all(key in prefer_longest_ids for _, key in fits):
            _, stock_id = max(fits, key=lambda item: (item[0], item[1]))
        else:
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
