"""
core/pnl_engine.py — Profit & Loss Engine.

GROSS P&L (gross_*):
  All amounts at GST-inclusive bank statement values (raw amounts).
  Income categories summed as credits, Expense/Direct Cost as debits.
  gross_net_profit = total_income_gross - total_expense_gross
  This EXACTLY equals the raw (all credits - all debits) on the bank statement
  for categorized transactions.

NET P&L (net_*):
  Income: GST-inclusive (GST-free income has no GST to strip).
  Expenses: ex-GST (GST stripped from taxable expenses via net_amount).
  net_profit = total_income_net - total_expense_net
  This is the accounting / tax P&L.

"Excluded" pnl_group → excluded from P&L entirely (uncategorized, depreciation, etc.)
"Direct Cost" pnl_group → shown as a deduction from Income before Gross Profit line.
"""


def generate_pnl(transactions: list[dict]) -> dict:
    # Per-category accumulators
    income_gross:      dict[str, float] = {}  # GST-inclusive
    direct_cost_gross: dict[str, float] = {}
    expense_gross:     dict[str, float] = {}

    income_net:        dict[str, float] = {}  # ex-GST on expenses
    direct_cost_net:   dict[str, float] = {}
    expense_net:       dict[str, float] = {}

    for t in transactions:
        cat    = t.get("category_name") or "Uncategorized"
        group  = t.get("pnl_group") or "Excluded"
        amount = float(t.get("amount") or 0)

        # net_amount is stored in DB after categorization (ex-GST for taxable, = amount for GST-free)
        net_amt = t.get("net_amount")
        if net_amt is None:
            net_amt = amount

        if group == "Income":
            # Income amounts are positive (credits); sum them as-is
            income_gross[cat] = income_gross.get(cat, 0.0) + amount
            income_net[cat]   = income_net.get(cat, 0.0)   + (net_amt if net_amt is not None else amount)

        elif group == "Direct Cost":
            # Expense amounts are negative (debits); take absolute value for display
            direct_cost_gross[cat] = direct_cost_gross.get(cat, 0.0) + abs(amount)
            direct_cost_net[cat]   = direct_cost_net.get(cat, 0.0)   + abs(net_amt)

        elif group == "Expense":
            expense_gross[cat] = expense_gross.get(cat, 0.0) + abs(amount)
            expense_net[cat]   = expense_net.get(cat, 0.0)   + abs(net_amt)

        # "Excluded" → skip entirely

    def to_lines(d: dict) -> list:
        return [{"category": k, "amount": round(v, 2)} for k, v in sorted(d.items())]

    # ── Gross P&L (GST-inclusive, matches raw bank DR/CR) ─────────────────
    income_lines_g      = to_lines(income_gross)
    direct_cost_lines_g = to_lines(direct_cost_gross)
    expense_lines_g     = to_lines(expense_gross)

    total_income_g      = round(sum(x["amount"] for x in income_lines_g), 2)
    total_direct_cost_g = round(sum(x["amount"] for x in direct_cost_lines_g), 2)
    gross_profit_g      = round(total_income_g - total_direct_cost_g, 2)
    total_expense_g     = round(sum(x["amount"] for x in expense_lines_g), 2)
    net_profit_g        = round(gross_profit_g - total_expense_g, 2)

    # ── Net P&L (ex-GST on expenses — accounting / tax view) ──────────────
    income_lines        = to_lines(income_net)
    direct_cost_lines   = to_lines(direct_cost_net)
    expense_lines       = to_lines(expense_net)

    total_income        = round(sum(x["amount"] for x in income_lines), 2)
    total_direct_cost   = round(sum(x["amount"] for x in direct_cost_lines), 2)
    gross_profit        = round(total_income - total_direct_cost, 2)
    total_expense       = round(sum(x["amount"] for x in expense_lines), 2)
    net_profit          = round(gross_profit - total_expense, 2)

    # ── Category-wise rows for the table view ─────────────────────────────
    # Each row: {category, pnl_group, amount (gross), net_amount, count}
    # We need per-category totals for both gross and net, keeping raw sign for income (positive)
    # and abs for expense (but we track pnl_group so the frontend knows direction)
    cat_gross: dict[str, dict] = {}  # key=(cat,group)
    cat_net:   dict[str, dict] = {}

    for t in transactions:
        cat   = t.get("category_name") or "Uncategorized"
        group = t.get("pnl_group") or "Excluded"
        amount  = float(t.get("amount") or 0)
        net_amt = t.get("net_amount")
        if net_amt is None:
            net_amt = amount

        key = (cat, group)
        if key not in cat_gross:
            cat_gross[key] = {"category": cat, "pnl_group": group, "amount": 0.0, "net_amount": 0.0, "count": 0}
        cat_gross[key]["amount"]     += amount
        cat_gross[key]["net_amount"] += net_amt
        cat_gross[key]["count"]      += 1

    gross_cat_rows = [{"category": k[0], "pnl_group": k[1], "amount": round(v["amount"], 2), "net_amount": round(v["net_amount"], 2), "count": v["count"]} for k, v in cat_gross.items()]
    # Net rows are same structure but using net_amount values
    net_cat_rows   = [{"category": r["category"], "pnl_group": r["pnl_group"], "amount": round(r["amount"], 2), "net_amount": round(r["net_amount"], 2), "count": r["count"]} for r in gross_cat_rows]

    return {
        # ── Net P&L keys (backward-compatible) ──
        "income_lines":        income_lines,
        "direct_cost_lines":   direct_cost_lines,
        "expense_lines":       expense_lines,
        "total_income":        total_income,
        "total_direct_cost":   total_direct_cost,
        "gross_profit":        gross_profit,
        "total_expense":       total_expense,
        "net_profit":          net_profit,

        # ── Gross P&L keys ──
        "gross_income_lines":       income_lines_g,
        "gross_direct_cost_lines":  direct_cost_lines_g,
        "gross_expense_lines":      expense_lines_g,
        "gross_total_income":       total_income_g,
        "gross_total_direct_cost":  total_direct_cost_g,
        "gross_profit_gross":       gross_profit_g,
        "gross_total_expense":      total_expense_g,
        "gross_net_profit":         net_profit_g,

        # ── Category-wise rows for table view ──
        "gross_category_rows": gross_cat_rows,
        "net_category_rows":   net_cat_rows,
    }
