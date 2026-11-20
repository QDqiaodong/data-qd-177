"""Purchase-cost propagation along inventory source chains.

Pure integer computation over committed cutting tickets. Each committed ticket
apportions the integer cost of the inventory entry it consumes across the
ticket's physical segments -- head trim, one span per piece, one per kerf, the
offcut, and the tail trim -- in proportion to segment length, ignoring
zero-length segments. Apportionment uses largest-remainder rounding on exact
integer remainders, never floats: every segment first gets
``floor(cost * length / total)`` and the leftover cents go one apiece to the
largest exact remainders, ties broken by smaller physical start.

A reusable offcut carries its integer share into the next generation, where
the consuming ticket re-apportions that share by the same rule; root cost is
never re-split directly across final pieces. Non-reusable offcuts, trims, and
kerfs become loss. Because every ticket conserves its input, every root
conserves at any snapshot: pieces + losses + unconsumed inventory equals the
root's adopted purchase price.
"""

# Loss segment categories; a reusable offcut is inventory, not loss.
LOSS_CATEGORIES = ("trim_head", "trim_tail", "kerf", "offcut_scrap")


def allocate_cents(amount_cents, lengths):
    """Largest-remainder apportionment of ``amount_cents`` across segments.

    ``lengths`` are strictly positive segment lengths in physical order.
    Returns one integer per segment, summing exactly to ``amount_cents``.
    Pure integer math; no float is ever involved.
    """
    total = sum(lengths)
    exacts = [amount_cents * length for length in lengths]
    amounts = [exact // total for exact in exacts]
    leftover = amount_cents - sum(amounts)
    # Largest exact remainder first; equal remainders keep physical order.
    ranked = sorted(range(len(lengths)), key=lambda i: (-(exacts[i] % total), i))
    for index in ranked[:leftover]:
        amounts[index] += 1
    return amounts


def ticket_segments(ticket, entry):
    """Physical segments of a committed ticket over its consumed entry.

    Zero-length segments (absent trims, zero kerf, empty offcut) are omitted.
    Segments come back in physical-start order and their lengths sum to the
    entry's full length.
    """
    segments = []

    def add(kind, start_mm, end_mm, **extra):
        if end_mm > start_mm:
            segments.append({"kind": kind, "start_mm": start_mm, "end_mm": end_mm,
                             "length_mm": end_mm - start_mm, **extra})

    add("trim_head", 0, entry["trim_head_mm"])
    for cut in ticket["cuts"]:
        add("piece", cut["start_mm"], cut["end_mm"],
            order_id=cut["order_id"], unit=cut["unit"])
        add("kerf", cut["end_mm"], cut["kerf_end_mm"])
    add("offcut", ticket["offcut_start_mm"],
        ticket["offcut_start_mm"] + ticket["offcut_length_mm"],
        reusable=ticket["offcut_reusable"])
    add("trim_tail", entry["length_mm"] - entry["trim_tail_mm"], entry["length_mm"])
    return segments


def _piece_row(job_id, entry, segment, root, material, amount):
    return {"job_id": job_id, "entry_id": entry["entry_id"],
            "order_id": segment["order_id"], "unit": segment["unit"],
            "root_stock_id": root, "material": material,
            "start_mm": segment["start_mm"], "end_mm": segment["end_mm"],
            "amount_cents": amount}


def _loss_row(job_id, entry, segment, category, root, material, amount):
    return {"job_id": job_id, "entry_id": entry["entry_id"], "category": category,
            "root_stock_id": root, "material": material,
            "start_mm": segment["start_mm"], "end_mm": segment["end_mm"],
            "amount_cents": amount}


def build_cost_detail(state, revision, prices):
    """Cost detail at an inventory snapshot under the given root prices.

    ``revision`` selects committed jobs (only jobs committed at a revision not
    greater than ``revision`` exist at the snapshot); ``prices`` maps every
    root stock id to its adopted purchase cost. Inventory is reconstructed
    from the source chain at that snapshot, never from the live ``status``
    field, so consumed-at-snapshot and not-yet-existing entries are exact.
    """
    jobs = sorted((job for job in state["jobs"].values()
                   if job["revision"] <= revision),
                  key=lambda job: job["revision"])
    entries = state["entries"]
    consumed = set()
    offcut_costs = {}
    pieces, losses, tickets = [], [], []
    for job in jobs:
        for ticket in job["result"]["tickets"]:
            entry = entries[ticket["stock_id"]]
            root = entry["root_stock_id"]
            if entry["kind"] == "stock":
                input_cents = prices[root]
            else:
                input_cents = offcut_costs[entry["entry_id"]]
            segments = ticket_segments(ticket, entry)
            amounts = allocate_cents(input_cents,
                                     [segment["length_mm"] for segment in segments])
            consumed.add(entry["entry_id"])
            rows = []
            for segment, amount in zip(segments, amounts):
                row = {"kind": segment["kind"], "start_mm": segment["start_mm"],
                       "end_mm": segment["end_mm"], "amount_cents": amount}
                if segment["kind"] == "piece":
                    row["order_id"] = segment["order_id"]
                    row["unit"] = segment["unit"]
                    pieces.append(_piece_row(job["job_id"], entry, segment,
                                             root, ticket["material"], amount))
                elif segment["kind"] == "offcut":
                    row["reusable"] = segment["reusable"]
                    if segment["reusable"]:
                        offcut_id = f"offcut/{job['job_id']}/{entry['entry_id']}"
                        row["entry_id"] = offcut_id
                        offcut_costs[offcut_id] = amount
                    else:
                        losses.append(_loss_row(job["job_id"], entry, segment,
                                                "offcut_scrap", root,
                                                ticket["material"], amount))
                else:
                    losses.append(_loss_row(job["job_id"], entry, segment,
                                            segment["kind"], root,
                                            ticket["material"], amount))
                rows.append(row)
            tickets.append({"job_id": job["job_id"], "revision": job["revision"],
                            "entry_id": entry["entry_id"], "entry_kind": entry["kind"],
                            "root_stock_id": root, "material": ticket["material"],
                            "input_cents": input_cents, "segments": rows})
    included = {job["job_id"] for job in jobs}
    inventory = []
    for entry_id in sorted(entries):
        entry = entries[entry_id]
        if entry["kind"] == "offcut" and entry["source"]["job_id"] not in included:
            continue  # the offcut does not exist yet at this snapshot
        if entry_id in consumed:
            continue  # already consumed at this snapshot: no longer inventory
        if entry["kind"] == "stock":
            amount = prices[entry["root_stock_id"]]
        else:
            amount = offcut_costs[entry_id]
        inventory.append({"entry_id": entry_id, "kind": entry["kind"],
                          "root_stock_id": entry["root_stock_id"],
                          "material": entry["material"],
                          "length_mm": entry["length_mm"], "amount_cents": amount})
    return {"pieces": pieces, "losses": losses, "inventory": inventory,
            "tickets": tickets,
            "summary": _detail_summary(pieces, losses, inventory, entries, prices)}


def _blank_detail_row():
    return {"pieces_cents": 0, "loss_cents": 0, "inventory_cents": 0,
            "total_cents": 0, "price_cents": 0}


def _detail_summary(pieces, losses, inventory, entries, prices):
    roots = {entry["root_stock_id"]: entry["material"]
             for entry in entries.values() if entry["kind"] == "stock"}
    by_root = {root: _blank_detail_row() for root in sorted(roots)}
    by_material = {}
    for root, material in roots.items():
        by_root[root]["price_cents"] = prices[root]
        by_material.setdefault(material, _blank_detail_row())
        by_material[material]["price_cents"] += prices[root]
    for rows, key in ((pieces, "pieces_cents"), (losses, "loss_cents"),
                      (inventory, "inventory_cents")):
        for row in rows:
            by_root[row["root_stock_id"]][key] += row["amount_cents"]
            by_material[row["material"]][key] += row["amount_cents"]
    total = _blank_detail_row()
    for row in by_root.values():
        row["total_cents"] = (row["pieces_cents"] + row["loss_cents"]
                              + row["inventory_cents"])
        for key in total:
            total[key] += row[key]
    for row in by_material.values():
        row["total_cents"] = (row["pieces_cents"] + row["loss_cents"]
                              + row["inventory_cents"])
    return {"by_root": by_root,
            "by_material": {key: by_material[key] for key in sorted(by_material)},
            "total": total}


def _line_key(kind, row):
    if kind == "piece":
        return ("piece", row["job_id"], row["entry_id"], row["order_id"], row["unit"])
    if kind == "loss":
        return ("loss", row["job_id"], row["entry_id"], row["category"],
                row["start_mm"], row["end_mm"])
    return ("inventory", row["entry_id"])


def _line_info(kind, row):
    if kind == "piece":
        return {"line_id": "piece/{job_id}/{entry_id}/{order_id}/{unit}".format(**row),
                "kind": "piece", "job_id": row["job_id"], "entry_id": row["entry_id"],
                "order_id": row["order_id"], "unit": row["unit"],
                "root_stock_id": row["root_stock_id"], "material": row["material"],
                "start_mm": row["start_mm"], "end_mm": row["end_mm"]}
    if kind == "loss":
        return {"line_id": "loss/{job_id}/{entry_id}/{category}/{start_mm}-{end_mm}".format(**row),
                "kind": "loss", "job_id": row["job_id"], "entry_id": row["entry_id"],
                "category": row["category"], "root_stock_id": row["root_stock_id"],
                "material": row["material"],
                "start_mm": row["start_mm"], "end_mm": row["end_mm"]}
    return {"line_id": f"inventory/{row['entry_id']}",
            "kind": "inventory", "entry_id": row["entry_id"],
            "entry_kind": row["kind"],
            "root_stock_id": row["root_stock_id"], "material": row["material"]}


def _lines_of(detail):
    """line key -> (amount_cents, identity info) for every detail row."""
    lines = {}
    for row in detail["pieces"]:
        key = _line_key("piece", row)
        lines[key] = (row["amount_cents"], _line_info("piece", row))
    for row in detail["losses"]:
        key = _line_key("loss", row)
        lines[key] = (row["amount_cents"], _line_info("loss", row))
    for row in detail["inventory"]:
        key = _line_key("inventory", row)
        lines[key] = (row["amount_cents"], _line_info("inventory", row))
    return lines


_KIND_RANK = {"piece": 0, "loss": 1, "inventory": 2}


def _line_sort_key(key):
    return (_KIND_RANK[key[0]],) + key[1:]


def _blank_bridge_row():
    return {"opening_cents": 0, "price_change_cents": 0,
            "production_cents": 0, "closing_cents": 0}


def build_bridge(detail_opening, detail_repriced, detail_current, previous_close_id):
    """Bridge between two closes, split into two separately checkable moves.

    ``detail_opening`` is the previous frozen detail (first close: the
    revision-0 opening projection under current prices), ``detail_repriced``
    is the previous inventory snapshot recomputed under this round's prices,
    and ``detail_current`` is this round's snapshot under the same prices.
    Historical price change is ``repriced - opening``; production transfer is
    ``current - repriced``. Lines are the identity union of all three
    projections with missing amounts treated as zero.
    """
    opening = _lines_of(detail_opening)
    repriced = _lines_of(detail_repriced)
    current = _lines_of(detail_current)
    keys = sorted(set(opening) | set(repriced) | set(current), key=_line_sort_key)
    lines = []
    for key in keys:
        opening_cents = opening.get(key, (0, None))[0]
        repriced_cents = repriced.get(key, (0, None))[0]
        closing_cents = current.get(key, (0, None))[0]
        info = (current.get(key) or repriced.get(key) or opening[key])[1]
        lines.append({**info,
                      "opening_cents": opening_cents,
                      "price_change_cents": repriced_cents - opening_cents,
                      "production_cents": closing_cents - repriced_cents,
                      "closing_cents": closing_cents})
    by_root, by_material, total = {}, {}, _blank_bridge_row()
    for line in lines:
        buckets = (by_root.setdefault(line["root_stock_id"], _blank_bridge_row()),
                   by_material.setdefault(line["material"], _blank_bridge_row()),
                   total)
        for bucket in buckets:
            for field in bucket:
                bucket[field] += line[field]
    return {"kind": "opening" if previous_close_id is None else "continuing",
            "previous_close_id": previous_close_id,
            "lines": lines,
            "summary": {"by_root": {key: by_root[key] for key in sorted(by_root)},
                        "by_material": {key: by_material[key]
                                        for key in sorted(by_material)},
                        "total": total}}
