"""
core/pnl_engine.py — Profit & Loss Engine.

P&L structure (standard Australian format):
  Revenue                  (GST on Income + GST Free Income)
  less: Direct Costs       (GST on Expenses tagged as Direct Cost)
  ─────────────────────────
  Gross Profit
  less: Expenses           (GST on Expenses + GST Free Expenses)
  ─────────────────────────
  Net Profit / Loss

BAS Excluded pnl_group → excluded from P&L entirely (wages, super, depreciation etc.)
GST amounts are always stripped — P&L uses net_amount (ex-GST).
GST Free items: net_amount = amount (no GST to strip).
"""


def generate_pnl(transactions: list[dict]) -> dict:
    income:      dict[str, float] = {}
    direct_cost: dict[str, float] = {}
    expense:     dict[str, float] = {}

    for t in transactions:
        cat   = t.get("category_name") or "Uncategorized"
        group = t.get("pnl_group")
        # Use net_amount (ex-GST). Falls back to amount for GST-free / uncategorized.
        net   = t.get("net_amount") if t.get("net_amount") is not None else t.get("amount", 0)

        if group == "Income":
            income[cat] = income.get(cat, 0.0) + net
        elif group == "Direct Cost":
            direct_cost[cat] = direct_cost.get(cat, 0.0) + abs(net)
        elif group == "Expense":
            expense[cat] = expense.get(cat, 0.0) + abs(net)
        # "Excluded" → skip entirely

    income_lines      = [{"category": k, "amount": round(v, 2)} for k, v in sorted(income.items())]
    direct_cost_lines = [{"category": k, "amount": round(v, 2)} for k, v in sorted(direct_cost.items())]
    expense_lines     = [{"category": k, "amount": round(v, 2)} for k, v in sorted(expense.items())]

    total_income      = round(sum(i["amount"] for i in income_lines), 2)
    total_direct_cost = round(sum(d["amount"] for d in direct_cost_lines), 2)
    gross_profit      = round(total_income - total_direct_cost, 2)
    total_expense     = round(sum(e["amount"] for e in expense_lines), 2)
    net_profit        = round(gross_profit - total_expense, 2)

    return {
        "income_lines":      income_lines,
        "direct_cost_lines": direct_cost_lines,
        "expense_lines":     expense_lines,
        "total_income":      total_income,
        "total_direct_cost": total_direct_cost,
        "gross_profit":      gross_profit,
        "total_expense":     total_expense,
        "net_profit":        net_profit,
    }
