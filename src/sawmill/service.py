from .model import validate
from .layout import compile_tickets


def run(data):
    stock, orders = validate(data)
    tickets, unmet = compile_tickets(stock, orders, data["kerf_mm"], data["min_offcut_mm"])
    used = {t["stock_id"] for t in tickets}
    delivered = sum(len(t["cuts"]) for t in tickets)
    kerf_total = delivered * data["kerf_mm"]
    reusable = sum(t["offcut_length_mm"] for t in tickets if t["offcut_reusable"])
    scrap = sum(t["trim_head_mm"] + t["trim_tail_mm"] +
                (0 if t["offcut_reusable"] else t["offcut_length_mm"]) for t in tickets)
    return {"schema_version": 1, "tickets": tickets, "unfilled": unmet,
            "unused_stock_ids": sorted(set(stock) - used),
            "summary": {"complete": not unmet, "used_stock": len(tickets), "delivered_pieces": delivered,
                        "unfilled_pieces": len(unmet), "kerf_loss_mm": kerf_total,
                        "reusable_offcut_mm": reusable, "scrap_mm": scrap}}
