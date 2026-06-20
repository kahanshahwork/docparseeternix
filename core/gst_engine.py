"""
core/gst_engine.py — Module 5: GST Calculation Engine.

Pure functions only — no DB, no Flask. This is deliberate: GST math is the
single most important correctness surface in the whole app, so it lives in
one small, independently testable file. If a GST number is ever wrong,
this is the only file you should need to open.

ATO standard method for a GST-inclusive amount at a 10% rate:
    GST  = amount / 11           (i.e. amount - amount/1.10)
    Net  = amount - GST
For a general rate r (rate as decimal, e.g. 0.10):
    GST  = amount - amount / (1 + r)
"""


def calc_gst(amount: float, gst_applicable: bool, gst_rate: float = 0.10) -> dict:
    """
    amount is GST-inclusive (what actually appears on the bank statement).
    Returns gst_amount (sign-matched to amount) and net_amount.
    """
    if not gst_applicable or not gst_rate:
        return {"gst_amount": 0.0, "net_amount": round(amount, 2)}

    gst_amount = amount - (amount / (1 + gst_rate))
    net_amount = amount - gst_amount
    return {"gst_amount": round(gst_amount, 2), "net_amount": round(net_amount, 2)}


def recalc_transaction_gst(amount: float, category: dict | None) -> dict:
    """category = row from core.category_master (dict with gst_applicable/gst_rate), or None."""
    if not category:
        return {"gst_amount": 0.0, "net_amount": round(amount, 2)}
    return calc_gst(amount, bool(category.get("gst_applicable")), category.get("gst_rate", 0.10))


def summarize_gst(transactions: list[dict]) -> dict:
    """
    transactions: list of dicts with keys amount, gst_amount, category_name, pnl_group, bas_label.
    Returns category-wise GST totals + BAS-style buckets (G1, G10, G11, GST collected/paid, net GST).
    """
    by_category: dict[str, dict] = {}
    bas_buckets: dict[str, float] = {}
    gst_collected = 0.0   # GST on income (G1)
    gst_paid = 0.0         # GST on expenses (G10 + G11)

    for t in transactions:
        cat = t.get("category_name") or "Uncategorized"
        bucket = by_category.setdefault(cat, {
            "category": cat,
            "pnl_group": t.get("pnl_group"),
            "gross": 0.0, "gst": 0.0, "net": 0.0, "count": 0,
        })
        bucket["gross"] += t["amount"]
        bucket["gst"]   += t.get("gst_amount", 0.0)
        bucket["net"]   += t.get("net_amount", t["amount"])
        bucket["count"] += 1

        label = t.get("bas_label") or "excluded"
        bas_buckets[label] = bas_buckets.get(label, 0.0) + t["amount"]

        gst_amt = t.get("gst_amount", 0.0) or 0.0
        if t.get("pnl_group") == "Income":
            gst_collected += gst_amt
        elif t.get("pnl_group") == "Expense":
            gst_paid += abs(gst_amt)

    for b in by_category.values():
        for k in ("gross", "gst", "net"):
            b[k] = round(b[k], 2)

    net_gst_payable = round(gst_collected - gst_paid, 2)

    return {
        "by_category": list(by_category.values()),
        "bas_buckets": {k: round(v, 2) for k, v in bas_buckets.items()},
        "gst_collected": round(gst_collected, 2),
        "gst_paid": round(gst_paid, 2),
        "net_gst_payable": net_gst_payable,   # positive = owe ATO, negative = refund due
    }
