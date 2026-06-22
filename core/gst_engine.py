"""
core/gst_engine.py — Module 5: GST Calculation Engine.

Pure functions only — no DB, no Flask. GST math is the single most important
correctness surface in the whole app, so it lives in one small, independently
testable file. If a GST number is ever wrong, this is the only file you need.

ATO standard method for a GST-inclusive amount (what bank statements always show):
    GST  = amount ÷ 11          (i.e. amount - amount/1.10)
    Net  = amount - GST         (i.e. amount × 10/11)

Source: ATO — Completing your BAS for GST
  https://www.ato.gov.au/businesses-and-organisations/gst-excise-and-indirect-taxes/
  gst/in-detail/managing-gst-in-your-business/reporting-paying-and-activity-statements/
  completing-your-bas-for-gst/

BAS label mapping (ATO Quarterly GST Reporting):
  G1   = Total sales (all income, GST-inclusive)           → bas_label "G1"
  1A   = GST on sales  = G1 taxable portion ÷ 11
  G10  = Capital purchases (GST-inclusive)                 → bas_label "G10"
  G11  = Non-capital purchases (GST-inclusive)             → bas_label "G11"
  1B   = GST on purchases = (G10 + G11) taxable portion ÷ 11
  Net  = 1A − 1B  (positive = owe ATO; negative = refund due)

Source: ATO — Quarterly GST reporting
  https://www.ato.gov.au/businesses-and-organisations/gst-excise-and-indirect-taxes/
  gst/lodging-your-bas-or-annual-gst-return/options-for-reporting-and-paying-gst/
  quarterly-gst-reporting
"""


def calc_gst(amount: float, gst_applicable: bool, gst_rate: float = 0.10) -> dict:
    """
    amount is GST-inclusive (what actually appears on the bank statement).
    Returns gst_amount (sign-matched to amount) and net_amount.

    ATO formula: GST = amount - (amount / (1 + rate))
    At 10%: GST = amount / 11,  Net = amount × 10/11
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
    Compute BAS-ready GST summary from a list of categorized transactions.

    Returns:
      by_category  — list of per-category totals (gross, gst, net, count, pnl_group, bas_label)
      bas          — ATO BAS labels: G1, G10, G11, 1A, 1B, net_gst_payable
      gst_collected — alias for 1A (for backward compatibility)
      gst_paid      — alias for 1B (for backward compatibility)
      net_gst_payable — 1A − 1B
    """
    by_category: dict[str, dict] = {}

    # BAS accumulators (all GST-inclusive amounts per ATO)
    G1  = 0.0   # Total sales (income, GST-inclusive)            → CR with bas_label G1
    G10 = 0.0   # Capital purchases (GST-inclusive)              → DR with bas_label G10
    G11 = 0.0   # Non-capital purchases (GST-inclusive)          → DR with bas_label G11

    # GST amounts (÷11 of taxable portions)
    gst_on_G1   = 0.0   # → 1A
    gst_on_G1011 = 0.0  # → 1B

    for t in transactions:
        cat  = t.get("category_name") or "Uncategorized"
        label = (t.get("bas_label") or "excluded").upper()
        amount = t.get("amount", 0.0) or 0.0
        gst_amt = t.get("gst_amount", 0.0) or 0.0
        net_amt = t.get("net_amount", amount) or amount
        pnl   = t.get("pnl_group", "") or ""
        gst_ok = bool(t.get("gst_applicable"))

        # ── Category-wise accumulation ──────────────────────────────────────
        bucket = by_category.setdefault(cat, {
            "category":   cat,
            "pnl_group":  pnl,
            "bas_label":  label,
            "gst_applicable": gst_ok,
            "gross": 0.0, "gst": 0.0, "net": 0.0, "count": 0,
        })
        bucket["gross"] += amount
        bucket["gst"]   += gst_amt
        bucket["net"]   += net_amt
        bucket["count"] += 1

        # ── BAS label accumulation ──────────────────────────────────────────
        abs_amount = abs(amount)   # bank statement amounts are signed; BAS uses absolute

        if label == "G1":
            G1 += abs_amount
            if gst_ok:
                gst_on_G1 += abs(gst_amt)

        elif label == "G10":
            G10 += abs_amount
            if gst_ok:
                gst_on_G1011 += abs(gst_amt)

        elif label == "G11":
            G11 += abs_amount
            if gst_ok:
                gst_on_G1011 += abs(gst_amt)

    # Round category buckets
    for b in by_category.values():
        for k in ("gross", "gst", "net"):
            b[k] = round(b[k], 2)

    # ATO labels (rounded to 2dp)
    label_1A  = round(gst_on_G1, 2)      # GST on sales  (ATO: G8 ÷ 11 → 1A)
    label_1B  = round(gst_on_G1011, 2)   # GST on purchases (ATO: G19 ÷ 11 → 1B)
    net_gst   = round(label_1A - label_1B, 2)

    return {
        "by_category": sorted(by_category.values(), key=lambda x: (x["pnl_group"], x["category"])),
        "bas": {
            "G1":   round(G1, 2),
            "G10":  round(G10, 2),
            "G11":  round(G11, 2),
            "G1011": round(G10 + G11, 2),
            "1A":   label_1A,
            "1B":   label_1B,
            "net_gst_payable": net_gst,
        },
        # backward-compat keys (used by existing frontend/routes)
        "gst_collected":    label_1A,
        "gst_paid":         label_1B,
        "net_gst_payable":  net_gst,
    }
