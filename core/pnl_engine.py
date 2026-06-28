"""
core/pnl_engine.py — Profit & Loss Engine.

Returns TWO P&L views:

  GROSS P&L (gross_*)
    All amounts at GST-inclusive bank statement values.
    Revenue gross - Expenses gross = gross_net
    This equals the raw DR/CR net on the bank statement.

  NET P&L (net_*/existing keys for backward compat)
    Income: GST-inclusive (GST-free income has no GST to strip)
    Expenses: ex-GST (GST is stripped from taxable expenses)
    This is the accounting P&L used for tax purposes.

BAS Excluded pnl_group → excluded from P&L entirely.
"""


def generate_pnl(transactions: list[dict]) -> dict:
    # Accumulators — gross (GST-inclusive) and net (ex-GST for expenses)
    income_gross:       dict[str, float] = {}
    direct_cost_gross:  dict[str, float] = {}
    expense_gross:      dict[str, float] = {}

    income_net:         dict[str, float] = {}
    direct_cost_net:    dict[str, float] = {}
    expense_net:        dict[str, float] = {}

    for t in transactions:
        cat   = t.get("category_name") or "Uncategorized"
        group = t.get("pnl_group")
        amount = t.get("amount", 0) or 0
        # net_amount = ex-GST amount (equals amount for GST-free items)
        net_amt = t.get("net_amount") if t.get("net_amount") is not None else amount

        if group == "Income":
            income_gross[cat] = income_gross.get(cat, 0.0) + amount
            income_net[cat]   = income_net.get(cat, 0.0)   + net_amt

        elif group == "Direct Cost":
            direct_cost_gross[cat] = direct_cost_gross.get(cat, 0.0) + abs(amount)
            direct_cost_net[cat]   = direct_cost_net.get(cat, 0.0)   + abs(net_amt)

        elif group == "Expense":
            expense_gross[cat] = expense_gross.get(cat, 0.0) + abs(amount)
            expense_net[cat]   = expense_net.get(cat, 0.0)   + abs(net_amt)
        # "Excluded" → skip

    def to_lines(d): return [{"category": k, "amount": round(v, 2)} for k, v in sorted(d.items())]

    # Gross P&L (bank statement values, GST-inclusive)
    income_lines_g      = to_lines(income_gross)
    direct_cost_lines_g = to_lines(direct_cost_gross)
    expense_lines_g     = to_lines(expense_gross)
    total_income_g      = round(sum(i["amount"] for i in income_lines_g), 2)
    total_direct_cost_g = round(sum(d["amount"] for d in direct_cost_lines_g), 2)
    gross_profit_g      = round(total_income_g - total_direct_cost_g, 2)
    total_expense_g     = round(sum(e["amount"] for e in expense_lines_g), 2)
    net_profit_g        = round(gross_profit_g - total_expense_g, 2)

    # Net P&L (ex-GST on expenses — accounting/tax view)
    income_lines      = to_lines(income_net)
    direct_cost_lines = to_lines(direct_cost_net)
    expense_lines     = to_lines(expense_net)
    total_income      = round(sum(i["amount"] for i in income_lines), 2)
    total_direct_cost = round(sum(d["amount"] for d in direct_cost_lines), 2)
    gross_profit      = round(total_income - total_direct_cost, 2)
    total_expense     = round(sum(e["amount"] for e in expense_lines), 2)
    net_profit        = round(gross_profit - total_expense, 2)

    return {
        # ── Net P&L (ex-GST on expenses) — backward-compat keys ──
        "income_lines":        income_lines,
        "direct_cost_lines":   direct_cost_lines,
        "expense_lines":       expense_lines,
        "total_income":        total_income,
        "total_direct_cost":   total_direct_cost,
        "gross_profit":        gross_profit,
        "total_expense":       total_expense,
        "net_profit":          net_profit,

        # ── Gross P&L (GST-inclusive, matches bank DR/CR net) ──
        "gross_income_lines":       income_lines_g,
        "gross_direct_cost_lines":  direct_cost_lines_g,
        "gross_expense_lines":      expense_lines_g,
        "gross_total_income":       total_income_g,
        "gross_total_direct_cost":  total_direct_cost_g,
        "gross_profit_gross":       gross_profit_g,
        "gross_total_expense":      total_expense_g,
        "gross_net_profit":         net_profit_g,
    }
