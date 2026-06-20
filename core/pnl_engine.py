"""
core/pnl_engine.py — Module 7: Profit & Loss Engine.

Pure function, generated entirely from categorized + GST-resolved
transactions. Uses NET amount (GST-exclusive) for P&L lines, since GST
collected/paid is not the business's own revenue or expense — it's money
held for the ATO. This is standard Australian accounting treatment.
"""


def generate_pnl(transactions: list[dict]) -> dict:
    """
    transactions: list of dicts with category_name, pnl_group, net_amount, amount.
    Returns income lines, expense lines, totals, net profit.
    """
    income: dict[str, float] = {}
    expense: dict[str, float] = {}

    for t in transactions:
        cat = t.get("category_name") or "Uncategorized"
        group = t.get("pnl_group")
        net = t.get("net_amount", t["amount"])

        if group == "Income":
            income[cat] = income.get(cat, 0.0) + net
        elif group == "Expense":
            # expenses are typically negative amounts on a statement; store as positive cost
            expense[cat] = expense.get(cat, 0.0) + abs(net)
        # 'Excluded' (drawings, personal, capital, loan principal) deliberately omitted from P&L

    income_lines = [{"category": k, "amount": round(v, 2)} for k, v in sorted(income.items())]
    expense_lines = [{"category": k, "amount": round(v, 2)} for k, v in sorted(expense.items())]

    total_income = round(sum(i["amount"] for i in income_lines), 2)
    total_expense = round(sum(e["amount"] for e in expense_lines), 2)
    net_profit = round(total_income - total_expense, 2)

    return {
        "income_lines": income_lines,
        "expense_lines": expense_lines,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_profit": net_profit,
    }
